from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import Field

from ouro.core.base import OuroModel
from ouro.plugins.package import NATIVE_MANIFEST, load_plugin_manifest

CompatibilityOutcome = Literal["supported", "converted", "ignored", "incompatible"]


class CompatibilityItem(OuroModel):
    kind: str
    identity: str
    outcome: CompatibilityOutcome
    reason: str = ""
    ouro_target: str | None = None


class CompatibilityReport(OuroModel):
    source_format: str
    source_version: str | None = None
    detected_resources: list[str] = Field(default_factory=list)
    items: list[CompatibilityItem] = Field(default_factory=list)
    lost_semantics: list[str] = Field(default_factory=list)
    security_differences: list[str] = Field(default_factory=list)
    required_permissions: list[str] = Field(default_factory=list)
    suggested_target: str = "native"


def inspect_community_package(source: str | Path) -> CompatibilityReport:
    root = Path(source).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"compatibility source must be a directory: {root}")
    if (root / NATIVE_MANIFEST).is_file():
        manifest = load_plugin_manifest(root)
        return CompatibilityReport(
            source_format="ouro",
            source_version=str(manifest.schema_version),
            detected_resources=sorted(manifest.capabilities()),
            items=[
                CompatibilityItem(
                    kind=kind,
                    identity=item.id,
                    outcome="supported",
                    ouro_target=f"contributes.{kind}[id={item.id}]",
                )
                for kind, values in manifest.contributes.by_kind().items()
                for item in values
            ],
        )

    manifest_path, source_format = _community_manifest(root)
    raw = _read_object(manifest_path) if manifest_path is not None else {}
    items: list[CompatibilityItem] = []
    detected: list[str] = []

    skill_paths = _skill_paths(root)
    for skill in skill_paths:
        items.append(
            CompatibilityItem(
                kind="skill",
                identity=str(skill.relative_to(root).parent.as_posix()),
                outcome="supported",
                reason="Agent Skill resources are portable.",
                ouro_target=(
                    "Declare contributes.skills with this SKILL.md path; "
                    "OURO validates and injects it through the Run Skill Catalog."
                ),
            )
        )
    if skill_paths:
        detected.append("skills")

    mcp = _community_mcp_servers(root, raw)
    required_permissions: set[str] = set()
    if isinstance(mcp, dict):
        detected.append("mcp_servers")
        for identity, descriptor in mcp.items():
            outcome: CompatibilityOutcome = (
                "converted" if isinstance(descriptor, dict) else "incompatible"
            )
            items.append(
                CompatibilityItem(
                    kind="mcp_server",
                    identity=str(identity),
                    outcome=outcome,
                    reason=(
                        "Transport and approval policy must be converted to OURO configuration."
                        if outcome == "converted"
                        else "MCP descriptor is not an object."
                    ),
                    ouro_target=(
                        "Declare contributes.mcp_servers with a package-relative descriptor; "
                        "declare process.spawn or network.outbound explicitly."
                        if outcome == "converted"
                        else None
                    ),
                )
            )
            if isinstance(descriptor, dict):
                if descriptor.get("command"):
                    required_permissions.add("process.spawn")
                if descriptor.get("url") or descriptor.get("transport") in {
                    "http",
                    "streamable-http",
                    "sse",
                }:
                    required_permissions.add("network.outbound")

    for key, kind in (
        ("hooks", "hook"),
        ("agents", "agent"),
        ("commands", "prompt_command"),
        ("lspServers", "lsp"),
        ("monitors", "monitor"),
        ("themes", "theme"),
        ("apps", "app"),
    ):
        value = raw.get(key)
        if value:
            detected.append(key)
            if isinstance(value, dict):
                identities = list(value)
            elif isinstance(value, list):
                identities = [str(index) for index in range(len(value))]
            else:
                identities = [key]
            for identity in identities:
                items.append(
                    CompatibilityItem(
                        kind=kind,
                        identity=str(identity),
                        outcome="ignored",
                        reason="This host-specific executable surface is not portable to OURO.",
                        ouro_target=None,
                    )
                )

    if source_format in {"pi", "opencode"}:
        items.append(
            CompatibilityItem(
                kind="runtime",
                identity=source_format,
                outcome="incompatible",
                reason="In-process JavaScript APIs require a worker or MCP port.",
                ouro_target=(
                    "Port executable behavior to an OURO stdio worker or MCP server; "
                    "do not execute the source host runtime directly."
                ),
            )
        )

    return CompatibilityReport(
        source_format=source_format,
        source_version=str(raw.get("version")) if raw.get("version") is not None else None,
        detected_resources=sorted(set(detected)),
        items=items,
        lost_semantics=[
            item.identity for item in items if item.outcome in {"ignored", "incompatible"}
        ],
        security_differences=[
            "Imported executable resources never inherit the source host's auto-start or trust."
        ],
        required_permissions=sorted(required_permissions),
        suggested_target=(
            "worker-or-mcp" if source_format in {"pi", "opencode"} else "native-declarative"
        ),
    )


def _community_manifest(root: Path) -> tuple[Path | None, str]:
    candidates = (
        (root / ".claude-plugin" / "plugin.json", "claude"),
        (root / ".codex-plugin" / "plugin.json", "codex"),
        (root / "package.json", "javascript"),
    )
    for path, source_format in candidates:
        if path.is_file():
            raw = _read_object(path)
            if source_format == "javascript":
                keywords = raw.get("keywords", [])
                text = json.dumps(keywords).lower()
                if "opencode" in text:
                    source_format = "opencode"
                elif "pi" in text:
                    source_format = "pi"
            return path, source_format
    return None, "agent-resources"


def _skill_paths(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("SKILL.md")
        if not any(part in {".git", "node_modules", "__pycache__"} for part in path.parts)
    )


def _community_mcp_servers(
    root: Path,
    manifest: dict[str, object],
) -> dict[str, object]:
    servers: dict[str, object] = {}
    declared = manifest.get("mcpServers") or manifest.get("mcp_servers")
    if isinstance(declared, dict):
        servers.update({str(key): value for key, value in declared.items()})
    descriptor = root / ".mcp.json"
    if descriptor.is_file():
        payload = _read_object(descriptor)
        nested = payload.get("mcpServers") or payload.get("mcp_servers")
        values = nested if isinstance(nested, dict) else payload
        servers.update({str(key): value for key, value in values.items()})
    return servers


def _read_object(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid community manifest {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"community manifest must be an object: {path}")
    return {str(key): value for key, value in raw.items()}
