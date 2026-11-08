"""Shared search paths, file traversal, glob matching, and ignore rules."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from pathspec import GitIgnoreSpec
from wcmatch import glob as wcglob

from ouro.tools.base import ToolExecutionContext

_MAX_SEARCHABLE_FILE_BYTES = 32 * 1_024 * 1_024


# CASE pins case-sensitive matching on every platform; DOTGLOB includes dotfiles.
_GLOB_FLAGS = wcglob.GLOBSTAR | wcglob.BRACE | wcglob.DOTGLOB | wcglob.CASE


# File kind takes precedence over directory depth (ripgrep semantics).
_IGNORE_FILENAMES = (".gitignore", ".ignore", ".rgignore")


# VCS metadata is never searched, even when ignore rules are disabled.
_VCS_DIRECTORY_NAMES = frozenset({".git", ".hg", ".svn"})


def _path_arg(raw: str | None, context: ToolExecutionContext) -> Path:
    if raw is None:
        resolved = context.resolve_working_directory(None)
        if resolved is None:
            raise ValueError("tool argument 'path' is required when no workspace is configured")
        return Path(resolved)
    return Path(context.resolve_path(raw))


@dataclass(frozen=True)
class FileEntry:
    """One searchable file discovered under the search root."""

    path: Path
    relative: str  # POSIX separators, relative to the search root
    mtime: float


@dataclass(frozen=True)
class DirectoryExclusions:
    names: frozenset[str] = frozenset()
    paths: frozenset[str] = frozenset()

    @classmethod
    def parse(cls, values: list[str]) -> DirectoryExclusions:
        names: set[str] = set()
        paths: set[str] = set()
        for value in values:
            path = PurePosixPath(value.replace("\\", "/"))
            if (
                path.is_absolute()
                or PureWindowsPath(value).drive
                or ".." in path.parts
                or not path.parts
            ):
                raise ValueError(
                    "exclude_dirs entries must be directory names or root-relative paths"
                )
            (paths if len(path.parts) > 1 else names).add(path.as_posix())
        return cls(frozenset(names), frozenset(paths))

    def matches(self, name: str, relative: str) -> bool:
        return name in self.names or relative in self.paths


def iter_files(
    root: Path,
    *,
    include_ignored: bool,
    check_cancelled: Callable[[], None] | None = None,
    file_filter: Callable[[str], bool] | None = None,
    exclude_dirs: DirectoryExclusions | None = None,
) -> Iterator[FileEntry]:
    """Yield regular files under ``root`` in a deterministic, pruned walk."""

    rules = IgnoreRules(root, enabled=not include_ignored)
    pending = [(root, "")]
    while pending:
        base, prefix = pending.pop()
        if check_cancelled is not None:
            check_cancelled()
        try:
            # Keep DirEntry's cached type/stat information, and close the directory
            # before yielding so cancellation or a partial consumer cannot leak it.
            with os.scandir(base) as directory:
                entries = sorted(directory, key=lambda entry: entry.name)
        except OSError:
            continue
        ignored = rules.matcher(base)
        directories: list[tuple[Path, str]] = []
        for index, entry in enumerate(entries):
            if check_cancelled is not None and index % 128 == 0:
                check_cancelled()
            try:
                if entry.is_symlink():
                    continue
                name = entry.name
                relative = prefix + name
                if entry.is_dir(follow_symlinks=False):
                    if (
                        not is_vcs_directory(name)
                        and (exclude_dirs is None or not exclude_dirs.matches(name, relative))
                        and (ignored is None or not ignored(name + "/"))
                    ):
                        directories.append((base / name, relative + "/"))
                    continue
                if is_sensitive_file(name) or (
                    file_filter is not None and not file_filter(relative)
                ):
                    continue
                if ignored is not None and ignored(name):
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            yield FileEntry(
                path=base / name,
                relative=relative,
                mtime=stat.st_mtime,
            )
        # Match os.walk's files-first, lexicographic depth-first ordering.
        pending.extend(reversed(directories))


def _translate(pattern: str) -> tuple[re.Pattern[str], ...]:
    inclusive, exclusive = wcglob.translate([pattern], flags=_GLOB_FLAGS)
    if exclusive:
        raise ValueError(f"glob pattern must not use negation: {pattern!r}")
    return tuple(re.compile(expression) for expression in inclusive)


def _matches_any(matchers: tuple[re.Pattern[str], ...], value: str) -> bool:
    for matcher in matchers:
        if matcher.match(value) is not None:
            return True
    return False


def is_vcs_directory(name: str) -> bool:
    return name in _VCS_DIRECTORY_NAMES


def is_sensitive_file(name: str) -> bool:
    """Secrets-bearing dotfiles are excluded from every search, unconditionally."""

    return name == ".env" or name.startswith(".env.")


class IgnoreRules:
    """Hierarchical ignore matcher rooted at one search directory.

    Rules are loaded lazily per directory and cached. Higher-priority ignore
    file kinds win; within each kind, deeper directories take precedence.
    """

    def __init__(self, root: Path, *, enabled: bool = True) -> None:
        self._root = root
        self._enabled = enabled
        self._chains: dict[Path, tuple[tuple[Path, dict[str, GitIgnoreSpec]], ...]] = {}

    def matcher(self, directory: Path) -> Callable[[str], bool] | None:
        """Bind rule prefixes once per directory; inputs are names with / for directories."""
        if not self._enabled:
            return None
        chain = self._chain(directory)
        checks: list[tuple[str, GitIgnoreSpec]] = []
        for filename in reversed(_IGNORE_FILENAMES):
            for base, specs in chain:
                spec = specs.get(filename)
                if spec is not None:
                    relative = directory.relative_to(base).as_posix()
                    checks.append(("" if relative == "." else relative + "/", spec))
        if not checks:
            return None

        def ignored(name: str) -> bool:
            for prefix, spec in checks:
                result = spec.check_file(prefix + name)
                if result.include is not None:
                    return result.include
            return False

        return ignored

    def _chain(self, directory: Path) -> tuple[tuple[Path, dict[str, GitIgnoreSpec]], ...]:
        """Cache only applicable rule sets per directory for this search invocation."""
        missing: list[Path] = []
        current = directory
        while current not in self._chains:
            missing.append(current)
            if current == self._root:
                break
            current = current.parent
        chain = self._chains.get(current, ())
        for base in reversed(missing):
            specs = self._read_specs(base)
            if specs:
                chain = ((base, specs), *chain)
            self._chains[base] = chain
        return chain

    def _read_specs(self, directory: Path) -> dict[str, GitIgnoreSpec]:
        specs: dict[str, GitIgnoreSpec] = {}
        for filename in _IGNORE_FILENAMES:
            candidate = directory / filename
            if not candidate.is_file():
                continue
            try:
                text = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            specs[filename] = GitIgnoreSpec.from_lines(text.splitlines())
        return specs
