"""Git-style ignore rule evaluation for built-in workspace search.

This module is the only one that imports ``pathspec``: the rest of the tools
consumes the small :class:`IgnoreRules` interface so the third-party
dependency stays isolated at the infrastructure edge.
"""

from __future__ import annotations

from pathlib import Path

from pathspec import GitIgnoreSpec

# Ignore files recognized in each directory, ordered by ascending precedence:
# patterns from later files override earlier ones (ripgrep semantics).
_IGNORE_FILENAMES = (".gitignore", ".ignore", ".rgignore")

# VCS metadata is never searched, even when ignore rules are disabled.
_VCS_DIRECTORY_NAMES = frozenset({".git", ".hg", ".svn"})


def is_vcs_directory(name: str) -> bool:
    return name in _VCS_DIRECTORY_NAMES


def is_sensitive_file(name: str) -> bool:
    """Secrets-bearing dotfiles are excluded from every search, unconditionally."""

    return name == ".env" or name.startswith(".env.")


class IgnoreRules:
    """Hierarchical ignore matcher rooted at one search directory.

    Rules are loaded lazily per directory and cached. Evaluation walks from
    the deepest ancestor towards the root and lets the first decisive pattern
    win, mirroring git's "lower-level files override higher-level" semantics.
    """

    def __init__(self, root: Path, *, enabled: bool = True) -> None:
        self._root = root
        self._enabled = enabled
        self._chains: dict[Path, tuple[tuple[Path, GitIgnoreSpec], ...]] = {}

    def ignores(self, path: Path, *, is_dir: bool) -> bool:
        if is_dir and is_vcs_directory(path.name):
            return True
        if not self._enabled:
            return False
        for base, spec in self._chain(path.parent):
            relative = path.relative_to(base).as_posix()
            result = spec.check_file(f"{relative}/" if is_dir else relative)
            if result.include is not None:
                return result.include
        return False

    def _chain(self, directory: Path) -> tuple[tuple[Path, GitIgnoreSpec], ...]:
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
            spec = self._read_spec(base)
            if spec is not None:
                chain = ((base, spec), *chain)
            self._chains[base] = chain
        return chain

    def _read_spec(self, directory: Path) -> GitIgnoreSpec | None:
        """Combined spec for one directory, or ``None`` when it has no ignore file.

        Merging the directory's ignore files into a single spec is sound
        because gitwildmatch evaluation is last-match-wins and the files are
        appended in ascending precedence order.
        """

        lines: list[str] = []
        for filename in _IGNORE_FILENAMES:
            candidate = directory / filename
            if not candidate.is_file():
                continue
            try:
                text = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            lines.extend(text.splitlines())
        return GitIgnoreSpec.from_lines(lines) if lines else None


__all__ = ["IgnoreRules", "is_sensitive_file", "is_vcs_directory"]
