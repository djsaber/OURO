"""Shared filesystem traversal and glob matching for built-in workspace search."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from wcmatch import glob as wcglob

from ouro.tools.builtin_ignore import IgnoreRules, is_sensitive_file, is_vcs_directory

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


def _translate(pattern: str) -> tuple[re.Pattern[str], ...]:
    inclusive, exclusive = wcglob.translate([pattern], flags=_GLOB_FLAGS)
    if exclusive:
        raise ValueError(f"glob pattern must not use negation: {pattern!r}")
    return tuple(re.compile(expression) for expression in inclusive)


def _matches_any(matchers: tuple[re.Pattern[str], ...], value: str) -> bool:
    return any(matcher.match(value) is not None for matcher in matchers)
