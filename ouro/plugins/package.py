from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from ouro import __version__
from ouro.plugins.configuration_schema import ConfigurationSchema, ConfigurationSchemaError
from ouro.plugins.manifest import PluginManifest
from ouro.plugins.paths import portable_relative_path
from ouro.plugins.semver import version_satisfies

OURO_VERSION = __version__
PLUGIN_API_VERSION = "5.0.0"
WORKER_PROTOCOL_VERSION = "3.0"
NATIVE_MANIFEST = Path(".ouro-plugin") / "plugin.json"

_REJECTED_PARTS = {".git", ".hg", ".svn", "__pycache__", ".pytest_cache", ".mypy_cache"}


class PluginPackageError(ValueError):
    pass


@dataclass(frozen=True)
class PackageLimits:
    max_files: int = 10_000
    max_file_bytes: int = 32 * 1024 * 1024
    max_total_bytes: int = 256 * 1024 * 1024


@dataclass(frozen=True)
class ValidatedPluginPackage:
    root: Path
    manifest: PluginManifest
    package_digest: str
    file_count: int
    total_bytes: int
    configuration_schema: ConfigurationSchema | None = None


def validate_plugin_package(
    root: str | Path,
    *,
    limits: PackageLimits | None = None,
    check_compatibility: bool = True,
) -> ValidatedPluginPackage:
    package_root = Path(root).expanduser().resolve()
    if not package_root.is_dir():
        raise PluginPackageError(f"plugin package is not a directory: {package_root}")
    manifest = load_plugin_manifest(package_root)
    file_count, total_bytes = validate_package_tree(package_root, limits=limits)
    configuration_schema = validate_manifest_paths(package_root, manifest)
    if manifest.contributes.mcp_servers:
        # Local import keeps package-path primitives below the MCP adapter while
        # still making installation a complete, side-effect-free static audit.
        from ouro.plugins.mcp import validate_plugin_mcp_descriptors

        validate_plugin_mcp_descriptors(manifest, package_root)
    if check_compatibility:
        validate_manifest_compatibility(manifest)
    return ValidatedPluginPackage(
        root=package_root,
        manifest=manifest,
        package_digest=compute_tree_digest(package_root),
        file_count=file_count,
        total_bytes=total_bytes,
        configuration_schema=configuration_schema,
    )


def load_plugin_manifest(root: str | Path) -> PluginManifest:
    native_path = Path(root) / NATIVE_MANIFEST
    if not native_path.is_file():
        raise PluginPackageError(f"plugin package has no manifest: {native_path}")
    return PluginManifest.model_validate(_read_json_object(native_path))


def validate_manifest_compatibility(manifest: PluginManifest) -> None:
    if not version_satisfies(OURO_VERSION, manifest.engines.ouro):
        raise PluginPackageError(
            f"plugin requires OURO {manifest.engines.ouro}; current version is {OURO_VERSION}"
        )
    if not version_satisfies(PLUGIN_API_VERSION, manifest.engines.plugin_api):
        raise PluginPackageError(
            "plugin API is incompatible: "
            f"requires {manifest.engines.plugin_api}, host is {PLUGIN_API_VERSION}"
        )
    if manifest.runtime.kind == "worker" and manifest.runtime.protocol != WORKER_PROTOCOL_VERSION:
        raise PluginPackageError(
            "plugin worker protocol is incompatible: "
            f"requires {manifest.runtime.protocol}, host is {WORKER_PROTOCOL_VERSION}"
        )


def validate_manifest_paths(root: Path, manifest: PluginManifest) -> ConfigurationSchema | None:
    paths: list[tuple[str, bool]] = []
    if manifest.configuration is not None and manifest.configuration.schema_path is not None:
        paths.append((manifest.configuration.schema_path, False))
    paths.extend((item.path, True) for item in manifest.contributes.skills)
    paths.extend((item.path, False) for item in manifest.contributes.mcp_servers)
    for value, allow_directory in paths:
        resolve_manifest_path(root, value, allow_directory=allow_directory)
    schema = load_configuration_schema(root, manifest)
    if manifest.runtime.kind == "python-inprocess":
        assert manifest.runtime.entrypoint is not None
        module_name = manifest.runtime.entrypoint.partition(":")[0]
        module_path = "./" + module_name.replace(".", "/")
        module_file = root.joinpath(*PurePosixPath(module_path[2:]).parts).with_suffix(".py")
        package_init = root.joinpath(*PurePosixPath(module_path[2:]).parts) / "__init__.py"
        if not module_file.is_file() and not package_init.is_file():
            raise PluginPackageError(f"python entrypoint module does not exist: {module_name}")
        _assert_plain_path(module_file if module_file.is_file() else package_init, root=root)
    if manifest.runtime.kind == "worker":
        _validate_worker_command(root, manifest.runtime.command)
    return schema


def load_configuration_schema(root: Path, manifest: PluginManifest) -> ConfigurationSchema | None:
    specification = manifest.configuration
    if specification is None or specification.schema_path is None:
        return None
    path = resolve_manifest_path(root, specification.schema_path, allow_directory=False)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        return ConfigurationSchema(document)
    except OSError, UnicodeError, ValueError, ConfigurationSchemaError:
        raise PluginPackageError("invalid plugin configuration schema") from None


def _validate_worker_command(root: Path, command: list[str]) -> None:
    executable = command[0]
    if executable != "{python}":
        resolve_manifest_path(root, executable, allow_directory=False)
    for argument in command[1:]:
        if argument.startswith("./"):
            resolve_manifest_path(root, argument, allow_directory=True)
        elif argument.startswith(("../", "/")) or "\\" in argument:
            raise PluginPackageError(
                f"worker command path must be normalized and package-relative: {argument}"
            )


def resolve_manifest_path(root: Path, value: str, *, allow_directory: bool) -> Path:
    if not value.startswith("./"):
        raise PluginPackageError(f"manifest path must start with './': {value}")
    try:
        relative = portable_relative_path(value[2:])
    except ValueError as exc:
        raise PluginPackageError(f"invalid manifest path {value}: {exc}") from exc
    candidate = root.joinpath(*relative.parts)
    _assert_plain_path(candidate, root=root)
    if allow_directory:
        if not candidate.exists() or not (candidate.is_file() or candidate.is_dir()):
            raise PluginPackageError(f"manifest path does not exist: {value}")
    elif not candidate.is_file():
        raise PluginPackageError(f"manifest path must reference a file: {value}")
    return candidate


def validate_package_tree(
    root: Path,
    *,
    limits: PackageLimits | None = None,
) -> tuple[int, int]:
    effective = limits or PackageLimits()
    file_count = 0
    total_bytes = 0
    portable_identities: dict[str, str] = {}
    for path in _tree_entries(root):
        relative = path.relative_to(root)
        display = relative.as_posix()
        try:
            portable_relative_path(display)
        except ValueError as exc:
            raise PluginPackageError(f"package contains invalid path {display}: {exc}") from exc
        identity = display.casefold()
        previous = portable_identities.setdefault(identity, display)
        if previous != display:
            raise PluginPackageError(
                f"package paths collide on a case-insensitive filesystem: {previous}, {display}"
            )
        if any(part in _REJECTED_PARTS for part in relative.parts):
            raise PluginPackageError(f"package contains rejected path: {relative.as_posix()}")
        _assert_plain_path(path, root=root)
        if path.is_dir():
            continue
        if not path.is_file():
            raise PluginPackageError(f"package contains unsupported entry: {relative.as_posix()}")
        size = path.stat().st_size
        if size > effective.max_file_bytes:
            raise PluginPackageError(f"plugin file exceeds size limit: {relative.as_posix()}")
        file_count += 1
        total_bytes += size
        if file_count > effective.max_files:
            raise PluginPackageError("plugin package exceeds file count limit")
        if total_bytes > effective.max_total_bytes:
            raise PluginPackageError("plugin package exceeds total size limit")
    return file_count, total_bytes


def compute_tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in _tree_entries(root):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        if path.is_dir():
            digest.update(b"D\0" + relative + b"\0")
            continue
        size = path.stat().st_size
        digest.update(b"F\0" + relative + b"\0" + str(size).encode("ascii") + b"\0")
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _tree_entries(root: Path) -> list[Path]:
    return sorted(
        (path for path in root.rglob("*") if path != root),
        key=lambda path: path.relative_to(root).as_posix(),
    )


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


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PluginPackageError(f"invalid plugin manifest {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise PluginPackageError(f"plugin manifest must be an object: {path}")
    return raw
