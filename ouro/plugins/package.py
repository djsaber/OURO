from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from ouro.plugins.manifest import (
    CommandContributionSpec,
    HookContributionSpec,
    LegacyPluginManifest,
    NamedContributionSpec,
    PathContributionSpec,
    PermissionRequest,
    PluginContributes,
    PluginDependencies,
    PluginEngines,
    PluginManifest,
    PluginRuntimeSpec,
    ToolContributionSpec,
)
from ouro.plugins.paths import portable_relative_path
from ouro.plugins.semver import version_satisfies

OURO_VERSION = "0.1.0"
PLUGIN_API_VERSION = "1.0.0"
WORKER_PROTOCOL_VERSION = "1.0"
NATIVE_MANIFEST = Path(".ouro-plugin") / "plugin.json"
LEGACY_MANIFEST = Path("plugin.json")

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
    legacy_manifest: LegacyPluginManifest | None = None

    @property
    def legacy(self) -> bool:
        return self.legacy_manifest is not None


def validate_plugin_package(
    root: str | Path,
    *,
    limits: PackageLimits | None = None,
    check_compatibility: bool = True,
) -> ValidatedPluginPackage:
    package_root = Path(root).expanduser().resolve()
    if not package_root.is_dir():
        raise PluginPackageError(f"plugin package is not a directory: {package_root}")
    manifest, legacy = load_plugin_manifest(package_root)
    file_count, total_bytes = validate_package_tree(package_root, limits=limits)
    validate_manifest_paths(package_root, manifest)
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
        legacy_manifest=legacy,
    )


def load_plugin_manifest(
    root: str | Path,
) -> tuple[PluginManifest, LegacyPluginManifest | None]:
    package_root = Path(root)
    native_path = package_root / NATIVE_MANIFEST
    legacy_path = package_root / LEGACY_MANIFEST
    if native_path.is_file() and legacy_path.is_file():
        native_raw = _read_json_object(native_path)
        legacy_raw = _read_json_object(legacy_path)
        if native_raw != legacy_raw:
            raise PluginPackageError("native and legacy plugin manifests conflict")
        return PluginManifest.model_validate(native_raw), None
    if native_path.is_file():
        return PluginManifest.model_validate(_read_json_object(native_path)), None
    if legacy_path.is_file():
        legacy = LegacyPluginManifest.model_validate(_read_json_object(legacy_path))
        return convert_legacy_manifest(legacy), legacy
    raise PluginPackageError("plugin package has no .ouro-plugin/plugin.json or legacy plugin.json")


def convert_legacy_manifest(legacy: LegacyPluginManifest) -> PluginManifest:
    supported = {
        "tools",
        "commands",
        "context_providers",
        "skills",
        "runtime_hooks",
        "mcp_servers",
        "compaction_strategies",
    }
    unsupported = sorted(set(legacy.capabilities) - supported)
    if unsupported:
        raise PluginPackageError(
            "legacy manifest declares unsupported capabilities: " + ", ".join(unsupported)
        )

    namespace = legacy.plugin_id.rsplit(".", 1)[-1].replace("-", "_")
    tools = [_legacy_tool(item, namespace=namespace) for item in legacy.tools]
    commands = [_legacy_command(item) for item in legacy.commands]
    hooks = [
        HookContributionSpec(
            id=f"{spec.hook_name}_{index}",
            event=spec.hook_name,
            priority=spec.priority,
            timeout_seconds=spec.timeout_seconds,
        )
        for index, spec in enumerate(legacy.runtime_hooks, start=1)
    ]
    context_providers = (
        [NamedContributionSpec(id="legacy_context")]
        if "context_providers" in legacy.capabilities
        else []
    )
    compaction_strategies = (
        [NamedContributionSpec(id="legacy_compaction")]
        if "compaction_strategies" in legacy.capabilities
        else []
    )
    permissions: list[PermissionRequest] = []
    if legacy.permissions.read_write:
        permissions.append(
            PermissionRequest(
                id="filesystem.write",
                scopes=["**/*"],
                reason="Converted from legacy read_write permission.",
            )
        )
    if legacy.permissions.network:
        permissions.append(
            PermissionRequest(
                id="network.outbound",
                scopes=["*"],
                reason="Converted from legacy network permission.",
            )
        )
    return PluginManifest(
        plugin_id=legacy.plugin_id,
        name=legacy.name,
        version=legacy.version,
        description=legacy.description,
        namespace=namespace,
        engines=PluginEngines(ouro=">=0.1.0,<0.2.0", plugin_api="^1.0.0"),
        runtime=PluginRuntimeSpec(
            kind="python-inprocess" if legacy.entrypoint else "declarative",
            entrypoint=legacy.entrypoint,
        ),
        contributes=PluginContributes(
            tools=tools,
            commands=commands,
            hooks=hooks,
            context_providers=context_providers,
            compaction_strategies=compaction_strategies,
            skills=[_legacy_path(item, kind="skill") for item in legacy.skills],
            mcp_servers=[_legacy_path(item, kind="mcp") for item in legacy.mcp_servers],
        ),
        permissions=permissions,
        dependencies=PluginDependencies(),
        metadata={
            **legacy.metadata,
            "legacy": {
                "declared_source_type": legacy.source_type,
                "capabilities": legacy.capabilities,
                "config_schema": legacy.config_schema,
            },
        },
    )


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


def validate_manifest_paths(root: Path, manifest: PluginManifest) -> None:
    paths: list[tuple[str, bool]] = []
    if manifest.configuration is not None:
        paths.append((manifest.configuration.schema_path, False))
    paths.extend((item.path, True) for item in manifest.contributes.skills)
    paths.extend((item.path, False) for item in manifest.contributes.mcp_servers)
    for value, allow_directory in paths:
        resolve_manifest_path(root, value, allow_directory=allow_directory)
    if manifest.configuration is not None:
        schema_path = resolve_manifest_path(
            root,
            manifest.configuration.schema_path,
            allow_directory=False,
        )
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(schema)
        except (OSError, UnicodeError, json.JSONDecodeError, SchemaError, ValueError) as exc:
            raise PluginPackageError(
                f"invalid plugin configuration schema: {type(exc).__name__}: {exc}"
            ) from exc
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


def _legacy_tool(item: dict[str, Any], *, namespace: str) -> ToolContributionSpec:
    identity = str(item.get("id") or item.get("name") or "tool").lower()
    exposed = str(item.get("exposed_name") or f"{namespace}__{identity}")
    return ToolContributionSpec(
        id=identity,
        exposed_name=exposed,
        description=str(item.get("description") or identity),
        permission_mode=item.get("permission_mode", "approval"),
    )


def _legacy_command(item: dict[str, Any]) -> CommandContributionSpec:
    identity = str(item.get("id") or item.get("name") or "command").lower()
    return CommandContributionSpec(
        id=identity,
        description=str(item.get("description") or identity),
    )


def _legacy_path(item: dict[str, Any], *, kind: str) -> PathContributionSpec:
    identity = str(item.get("id") or item.get("name") or kind).lower()
    path = item.get("path")
    if not isinstance(path, str):
        raise PluginPackageError(f"legacy {kind} contribution requires path")
    return PathContributionSpec(
        id=identity,
        path=path,
        auto_start=bool(item.get("auto_start", False)),
    )
