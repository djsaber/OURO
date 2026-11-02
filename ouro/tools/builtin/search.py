"""Shared filesystem traversal and glob matching for built-in workspace search."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from wcmatch import glob as wcglob

from ouro.tools.builtin.ignore import IgnoreRules, is_sensitive_file, is_vcs_directory

# CASE pins case-sensitive matching on every platform; DOTGLOB includes dotfiles.
_GLOB_FLAGS = wcglob.GLOBSTAR | wcglob.BRACE | wcglob.DOTGLOB | wcglob.CASE


@dataclass(frozen=True)
class FileEntry:
    """One searchable file discovered under the search root."""

    path: Path
    relative: str  # POSIX separators, relative to the search root
    mtime: float


def iter_files(
    root: Path,
    *,
    include_ignored: bool,
    check_cancelled: Callable[[], None] | None = None,
) -> Iterator[FileEntry]:
    """Yield regular files under ``root`` in a deterministic, pruned walk."""

    rules = IgnoreRules(root, enabled=not include_ignored)
    pending = [root]
    while pending:
        base = pending.pop()
        if check_cancelled is not None:
            check_cancelled()
        try:
            # Keep DirEntry's cached type/stat information, and close the directory
            # before yielding so cancellation or a partial consumer cannot leak it.
            with os.scandir(base) as directory:
                entries = sorted(directory, key=lambda entry: entry.name)
        except OSError:
            continue
        directories: list[Path] = []
        for index, entry in enumerate(entries):
            if check_cancelled is not None and index % 128 == 0:
                check_cancelled()
            try:
                if entry.is_symlink():
                    continue
                candidate = base / entry.name
                if entry.is_dir(follow_symlinks=False):
                    if not is_vcs_directory(entry.name) and not rules.ignores(
                        candidate, is_dir=True
                    ):
                        directories.append(candidate)
                    continue
                if is_sensitive_file(entry.name) or not entry.is_file(follow_symlinks=False):
                    continue
                if rules.ignores(candidate, is_dir=False):
                    continue
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            yield FileEntry(
                path=candidate,
                relative=candidate.relative_to(root).as_posix(),
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
    return any(matcher.match(value) is not None for matcher in matchers)
