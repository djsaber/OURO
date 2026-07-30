from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ouro.plugins.manifest import plugin_module_namespace, validate_plugin_id
from ouro.plugins.package import ValidatedPluginPackage, validate_plugin_package


@dataclass(frozen=True)
class PluginScaffoldResult:
    root: Path
    package: ValidatedPluginPackage
    files: tuple[str, ...]


def create_plugin_scaffold(
    destination: str | Path,
    *,
    plugin_id: str,
    name: str,
    namespace: str | None = None,
) -> PluginScaffoldResult:
    """Create one validated, minimal in-process user plugin atomically."""

    normalized_id = validate_plugin_id(plugin_id)
    display_name = name.strip()
    if not display_name:
        raise ValueError("plugin scaffold name cannot be blank")
    local_namespace = (namespace or normalized_id.rsplit(".", 1)[-1]).strip().lower()
    module_name = plugin_module_namespace(local_namespace)
    target = Path(destination).expanduser().resolve()
    if target.exists():
        raise FileExistsError(f"plugin scaffold destination already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=target.parent))
    try:
        manifest_dir = staging / ".ouro-plugin"
        manifest_dir.mkdir()
        manifest = {
            "schema_version": 3,
            "plugin_id": normalized_id,
            "name": display_name,
            "version": "0.1.0",
            "description": "A local OURO user plugin.",
            "namespace": local_namespace,
            "engines": {"ouro": ">=0.1.0,<0.2.0", "plugin_api": "^2.0.0"},
            "runtime": {
                "kind": "python-inprocess",
                "entrypoint": f"{module_name}:create_plugin",
            },
            "contributes": {"commands": [{"id": "hello", "description": "Return a greeting."}]},
            "permissions": [],
            "dependencies": {"plugins": []},
            "metadata": {},
        }
        (manifest_dir / "plugin.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (staging / f"{module_name}.py").write_text(
            _INPROCESS_TEMPLATE,
            encoding="utf-8",
        )
        (staging / "README.md").write_text(
            _readme_template(normalized_id),
            encoding="utf-8",
        )
        package = validate_plugin_package(staging)
        files = tuple(
            sorted(
                path.relative_to(staging).as_posix()
                for path in staging.rglob("*")
                if path.is_file()
            )
        )
        staging.rename(target)
        return PluginScaffoldResult(
            root=target,
            package=ValidatedPluginPackage(
                root=target,
                manifest=package.manifest,
                package_digest=package.package_digest,
                file_count=package.file_count,
                total_bytes=package.total_bytes,
            ),
            files=files,
        )
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise


_INPROCESS_TEMPLATE = """from __future__ import annotations

from ouro.sdk.plugins.v1 import CommandContribution, ContributionSet


class Plugin:
    async def prepare(self) -> ContributionSet:
        async def hello(call):
            subject = call.argument.strip() or "OURO"
            return f"Hello, {subject}!"

        return ContributionSet(
            commands=[
                CommandContribution(
                    id="hello",
                    description="Return a greeting.",
                    handler=hello,
                )
            ]
        )

    async def start(self) -> None:
        return None

    async def stop(self, reason, deadline) -> None:
        del reason, deadline


def create_plugin(context) -> Plugin:
    del context
    return Plugin()
"""


def _readme_template(plugin_id: str) -> str:
    return f"""# {plugin_id}

This directory is an OURO user plugin. `python-inprocess` plugins are trusted, full-process
extensions; install and execution remain separate.

```bash
uv run ouro plugin validate . --json
uv run ouro plugin install . --home ./.ouro-dev --json
uv run ouro plugin authorize {plugin_id} --home ./.ouro-dev --json
uv run ouro plugin trust-code {plugin_id} --home ./.ouro-dev --json
uv run ouro plugin enable {plugin_id} --home ./.ouro-dev --json
uv run ouro plugin run-command {plugin_id}:hello world --home ./.ouro-dev
```
"""


__all__ = ["PluginScaffoldResult", "create_plugin_scaffold"]
