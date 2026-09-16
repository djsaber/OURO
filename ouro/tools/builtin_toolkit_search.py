"""Filesystem search primitives backing the built-in Glob and Grep tools.

Traversal is shared by both tools: a single top-down walk that prunes ignored
and VCS directories before descending, never follows symlinks, and skips
sensitive dotfiles. Glob matching is delegated to ``wcmatch`` (isolated to
this module); content matching uses the standard-library ``re`` engine.
"""

from __future__ import annotations

import os
import re
from bisect import bisect_right
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from wcmatch import glob as wcglob

from ouro.tools.builtin_toolkit_ignore import (
    IgnoreRules,
    is_sensitive_file,
    is_vcs_directory,
)

# CASE pins ripgrep-style case-sensitive matching on every platform; DOTGLOB
# lets wildcards match dotfiles, which the tools include by default.
_GLOB_FLAGS = wcglob.GLOBSTAR | wcglob.BRACE | wcglob.DOTGLOB | wcglob.CASE

_BINARY_SNIFF_BYTES = 8 * 1_024
_MAX_SEARCHABLE_FILE_BYTES = 32 * 1_024 * 1_024
# Safety valve bounding content-mode result memory; counting continues so the
# reported match total stays exact.
_MAX_COLLECTED_MATCHES = 20_000

# Small ripgrep-style file type table for the Grep `type` filter.
_FILE_TYPES: dict[str, tuple[str, ...]] = {
    "c": ("*.c", "*.h"),
    "cpp": ("*.cpp", "*.cc", "*.cxx", "*.hpp", "*.hh", "*.hxx"),
    "css": ("*.css",),
    "go": ("*.go",),
    "html": ("*.html", "*.htm"),
    "java": ("*.java",),
    "js": ("*.js", "*.jsx", "*.mjs", "*.cjs"),
    "json": ("*.json",),
    "md": ("*.md", "*.markdown"),
    "py": ("*.py", "*.pyi"),
    "rust": ("*.rs",),
    "sh": ("*.sh", "*.bash", "*.zsh"),
    "sql": ("*.sql",),
    "toml": ("*.toml",),
    "ts": ("*.ts", "*.tsx", "*.mts", "*.cts"),
    "txt": ("*.txt",),
    "xml": ("*.xml",),
    "yaml": ("*.yaml", "*.yml"),
}


@dataclass(frozen=True)
class FileEntry:
    """One searchable file discovered under the search root."""

    path: Path
    relative: str  # POSIX separators, relative to the search root
    mtime: float


@dataclass(frozen=True)
class GlobResult:
    matches: tuple[FileEntry, ...]  # most recently modified first, capped at limit
    total: int


@dataclass(frozen=True)
class MatchedLine:
    entry: FileEntry
    number: int  # 1-based line number
    text: str
    before: tuple[tuple[int, str], ...] = ()
    after: tuple[tuple[int, str], ...] = ()


@dataclass(frozen=True)
class FileMatchCount:
    entry: FileEntry
    count: int


@dataclass(frozen=True)
class GrepResult:
    matches: tuple[MatchedLine, ...]  # deterministic walk order, bounded by the safety valve
    total: int  # exact matched-line count across all searched files
    collection_capped: bool
    file_counts: tuple[FileMatchCount, ...]  # only files with at least one match


def iter_files(
    root: Path,
    *,
    include_ignored: bool,
    check_cancelled: Callable[[], None] | None = None,
) -> Iterator[FileEntry]:
    """Yield regular files under ``root`` in a deterministic, pruned walk."""

    rules = IgnoreRules(root, enabled=not include_ignored)
    for dirpath, dirnames, filenames in os.walk(root):
        if check_cancelled is not None:
            check_cancelled()
        base = Path(dirpath)
        kept: list[str] = []
        for name in sorted(dirnames):
            candidate = base / name
            if is_vcs_directory(name) or candidate.is_symlink():
                continue
            if rules.ignores(candidate, is_dir=True):
                continue
            kept.append(name)
        dirnames[:] = kept
        for index, name in enumerate(sorted(filenames)):
            if check_cancelled is not None and index % 128 == 0:
                check_cancelled()
            candidate = base / name
            if is_sensitive_file(name) or candidate.is_symlink():
                continue
            try:
                stat = candidate.stat()
            except OSError:
                continue
            if not candidate.is_file():
                continue
            if rules.ignores(candidate, is_dir=False):
                continue
            yield FileEntry(
                path=candidate,
                relative=candidate.relative_to(root).as_posix(),
                mtime=stat.st_mtime,
            )


def compile_path_glob(pattern: str) -> tuple[re.Pattern[str], ...]:
    """Compile one Glob-tool pattern into anchored regexes over relative POSIX paths.

    A bare pattern without a path separator matches recursively at any depth,
    so ``*.py`` is equivalent to ``**/*.py``.
    """

    normalized = pattern if "/" in pattern else f"**/{pattern}"
    return _translate(normalized)


@dataclass(frozen=True)
class NameGlob:
    """Compiled ripgrep-style ``--glob`` filter.

    A pattern without a path separator matches only the file basename; a
    pattern containing one is matched against the file's full POSIX path.
    """

    matchers: tuple[re.Pattern[str], ...]
    anchored: bool

    def matches(self, entry: FileEntry) -> bool:
        subject = entry.path.as_posix() if self.anchored else entry.path.name
        return _matches_any(self.matchers, subject)


def compile_name_glob(pattern: str) -> NameGlob:
    return NameGlob(matchers=_translate(pattern), anchored="/" in pattern)


def supported_file_types() -> tuple[str, ...]:
    return tuple(sorted(_FILE_TYPES))


def compile_type_filter(file_type: str) -> tuple[re.Pattern[str], ...]:
    patterns = _FILE_TYPES.get(file_type)
    if patterns is None:
        supported = ", ".join(supported_file_types())
        raise ValueError(f"unsupported file type '{file_type}' (supported: {supported})")
    compiled: list[re.Pattern[str]] = []
    for pattern in patterns:
        compiled.extend(_translate(pattern))
    return tuple(compiled)


def search_glob(
    root: Path,
    pattern: str,
    *,
    include_ignored: bool,
    limit: int,
    check_cancelled: Callable[[], None] | None = None,
) -> GlobResult:
    matchers = compile_path_glob(pattern)
    found = [
        entry
        for entry in iter_files(
            root,
            include_ignored=include_ignored,
            check_cancelled=check_cancelled,
        )
        if _matches_any(matchers, entry.relative)
    ]
    found.sort(key=lambda entry: (-entry.mtime, entry.relative))
    return GlobResult(matches=tuple(found[:limit]), total=len(found))


def search_grep(
    root: Path,
    pattern: str,
    *,
    ignore_case: bool,
    multiline: bool,
    name_filter: NameGlob | None,
    type_filter: tuple[re.Pattern[str], ...] | None,
    include_ignored: bool,
    context_before: int,
    context_after: int,
    check_cancelled: Callable[[], None] | None = None,
) -> GrepResult:
    regex = _compile_search_regex(pattern, ignore_case=ignore_case, multiline=multiline)
    matches: list[MatchedLine] = []
    file_counts: list[FileMatchCount] = []
    total = 0
    collection_capped = False
    for entry in iter_files(
        root,
        include_ignored=include_ignored,
        check_cancelled=check_cancelled,
    ):
        if name_filter is not None and not name_filter.matches(entry):
            continue
        if type_filter is not None and not _matches_any(type_filter, entry.path.name):
            continue
        text = _read_searchable_text(entry.path)
        if text is None:
            continue
        lines = text.splitlines()
        matched_numbers = _matched_line_numbers(lines, regex, multiline=multiline)
        if not matched_numbers:
            continue
        file_counts.append(FileMatchCount(entry=entry, count=len(matched_numbers)))
        total += len(matched_numbers)
        for number in matched_numbers:
            if len(matches) >= _MAX_COLLECTED_MATCHES:
                collection_capped = True
                break
            matches.append(
                MatchedLine(
                    entry=entry,
                    number=number,
                    text=lines[number - 1],
                    before=_context(lines, number, -context_before),
                    after=_context(lines, number, context_after),
                )
            )
    return GrepResult(
        matches=tuple(matches),
        total=total,
        collection_capped=collection_capped,
        file_counts=tuple(file_counts),
    )


def grep_single_file(
    path: Path,
    pattern: str,
    *,
    ignore_case: bool,
    multiline: bool,
    context_before: int,
    context_after: int,
    check_cancelled: Callable[[], None] | None = None,
) -> GrepResult:
    """Search one explicitly named file; ignore rules and glob filters do not apply."""

    if check_cancelled is not None:
        check_cancelled()
    regex = _compile_search_regex(pattern, ignore_case=ignore_case, multiline=multiline)
    try:
        stat = path.stat()
    except OSError:
        stat = None
    entry = FileEntry(
        path=path,
        relative=path.name,
        mtime=stat.st_mtime if stat is not None else 0.0,
    )
    text = _read_searchable_text(path)
    if check_cancelled is not None:
        check_cancelled()
    if text is None:
        return GrepResult(matches=(), total=0, collection_capped=False, file_counts=())
    lines = text.splitlines()
    matched_numbers = _matched_line_numbers(lines, regex, multiline=multiline)
    matches = tuple(
        MatchedLine(
            entry=entry,
            number=number,
            text=lines[number - 1],
            before=_context(lines, number, -context_before),
            after=_context(lines, number, context_after),
        )
        for number in matched_numbers[:_MAX_COLLECTED_MATCHES]
    )
    file_counts = (
        (FileMatchCount(entry=entry, count=len(matched_numbers)),) if matched_numbers else ()
    )
    return GrepResult(
        matches=matches,
        total=len(matched_numbers),
        collection_capped=len(matched_numbers) > _MAX_COLLECTED_MATCHES,
        file_counts=file_counts,
    )


def _translate(pattern: str) -> tuple[re.Pattern[str], ...]:
    inclusive, exclusive = wcglob.translate([pattern], flags=_GLOB_FLAGS)
    if exclusive:
        raise ValueError(f"glob pattern must not use negation: {pattern!r}")
    return tuple(re.compile(expression) for expression in inclusive)


def _matches_any(matchers: tuple[re.Pattern[str], ...], value: str) -> bool:
    return any(matcher.match(value) is not None for matcher in matchers)


def _compile_search_regex(pattern: str, *, ignore_case: bool, multiline: bool) -> re.Pattern[str]:
    flags = re.IGNORECASE if ignore_case else 0
    if multiline:
        flags |= re.MULTILINE | re.DOTALL
    try:
        return re.compile(pattern, flags)
    except re.error as exc:
        raise ValueError(f"invalid regular expression: {exc}") from exc


def _matched_line_numbers(
    lines: list[str],
    regex: re.Pattern[str],
    *,
    multiline: bool,
) -> list[int]:
    if not multiline:
        return [index + 1 for index, line in enumerate(lines) if regex.search(line) is not None]
    offsets = _line_start_offsets(lines)
    matched: set[int] = set()
    text = "\n".join(lines)
    for hit in regex.finditer(text):
        first = bisect_right(offsets, hit.start())
        last = bisect_right(offsets, max(hit.start(), hit.end() - 1))
        matched.update(range(first, last + 1))
    return sorted(matched)


def _line_start_offsets(lines: list[str]) -> list[int]:
    offsets: list[int] = []
    position = 0
    for line in lines:
        offsets.append(position)
        position += len(line) + 1
    return offsets


def _context(lines: list[str], number: int, span: int) -> tuple[tuple[int, str], ...]:
    if span == 0:
        return ()
    if span < 0:
        start = max(1, number + span)
        stop = number
    else:
        start = number + 1
        stop = min(len(lines), number + span) + 1
    return tuple((index, lines[index - 1]) for index in range(start, stop))


def _read_searchable_text(path: Path) -> str | None:
    """Decode a file as UTF-8 text, returning ``None`` for binary or unreadable files."""

    try:
        with path.open("rb") as stream:
            head = stream.read(_BINARY_SNIFF_BYTES)
            if b"\x00" in head:
                return None
            rest = stream.read(_MAX_SEARCHABLE_FILE_BYTES + 1 - len(head))
            if len(rest) + len(head) > _MAX_SEARCHABLE_FILE_BYTES:
                return None
        return (head + rest).decode("utf-8", errors="replace")
    except OSError:
        return None


__all__ = [
    "FileEntry",
    "FileMatchCount",
    "GlobResult",
    "GrepResult",
    "MatchedLine",
    "NameGlob",
    "compile_name_glob",
    "compile_path_glob",
    "compile_type_filter",
    "grep_single_file",
    "iter_files",
    "search_glob",
    "search_grep",
    "supported_file_types",
]
