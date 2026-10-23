"""Package validation errors and safe declared-path resolution."""

from __future__ import annotations

import os
import stat
from pathlib import Path, PurePath

from ouro.plugins.paths import portable_relative_path

_CACHE_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache"}


_CACHE_SUFFIXES = (".pyc", ".pyo")


def is_plugin_cache_path(path: PurePath) -> bool:
    return any(part in _CACHE_PARTS or part.endswith(_CACHE_SUFFIXES) for part in path.parts)


def ignore_plugin_caches(directory: str, names: list[str]) -> list[str]:
    """Use the same cache exclusion for source copies and package publication."""

    return [name for name in names if is_plugin_cache_path(PurePath(name))]


class PluginPackageError(ValueError):
    pass


def resolve_manifest_path(root: Path, value: str, *, allow_directory: bool) -> Path:
    if not value.startswith("./"):
        raise PluginPackageError(f"manifest path must start with './': {value}")
    try:
        relative = portable_relative_path(value[2:])
    except ValueError as exc:
        raise PluginPackageError(f"invalid manifest path {value}: {exc}") from exc
    if is_plugin_cache_path(relative):
        raise PluginPackageError(f"manifest path cannot reference a cache: {value}")
    candidate = root.joinpath(*relative.parts)
    _assert_plain_path(candidate, root=root)
    if allow_directory:
        if not candidate.exists() or not (candidate.is_file() or candidate.is_dir()):
            raise PluginPackageError(f"manifest path does not exist: {value}")
    elif not candidate.is_file():
        raise PluginPackageError(f"manifest path must reference a file: {value}")
    return candidate


def _assert_plain_path(path: Path, *, root: Path) -> None:
    current = path
    while current != root:
        try:
            info = current.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info):
            raise PluginPackageError(
                f"plugin package cannot contain links or reparse points: "
                f"{current.relative_to(root).as_posix()}"
            )
        current = current.parent


def _is_reparse_point(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse)
