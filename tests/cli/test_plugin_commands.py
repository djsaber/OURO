from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

from ouro.billing import LocalUsageLedger
from ouro.cli.main import main
from ouro.config import ProviderConfigManager
from ouro.core.adapter import RuntimeAdapter
from ouro.core.billing import UsageFilter
from ouro.core.provider import ModelMetadata
from ouro.providers import FauxProvider


@pytest.mark.parametrize("exit_code", [0, 9])
def test_maintenance_cli_uses_private_directory_and_reports_command_status(
    tmp_path, capsys, exit_code
):
    plugin_id = "dev.ouro.removed"
    private = tmp_path / "plugins/data" / plugin_id
    private.mkdir(parents=True)
    result = main(
        [
            "plugin",
            "maintain",
            plugin_id,
            "--home",
            str(tmp_path),
            "--json",
            "--execute",
            sys.executable,
            "-c",
            f"from pathlib import Path; print(Path.cwd()); exit({exit_code})",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["stdout"].strip() == str(private)
    assert payload["returncode"] == exit_code
    assert result == (0 if exit_code == 0 else 1)


def test_purge_cli_removes_orphan_private_data_and_preserves_other_facts(tmp_path, capsys):
    plugin = "dev.ouro.removed"
    private = tmp_path / "plugins/data" / plugin
    private.mkdir(parents=True)
    (private / "owned.txt").write_text("private", encoding="utf-8")
    preserved = [
        tmp_path / "artifacts/report.txt",
        tmp_path / "plugins/data/dev.ouro.other/owned.txt",
    ]
    for path in preserved:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("retained", encoding="utf-8")
    args = ["plugin", "purge-data", plugin, "--home", str(tmp_path), "--json"]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "disabled"
    assert not private.exists()
    assert all(path.read_text(encoding="utf-8") == "retained" for path in preserved)
    assert main(args) == 0
    capsys.readouterr()
    assert main(["plugin", "purge-data", "../artifacts", "--home", str(tmp_path), "--json"]) != 0
    assert "plugin_id" in json.loads(capsys.readouterr().out)["error"]
    assert preserved[0].exists()


def test_package_validation_does_not_open_unrelated_usage_database(tmp_path, capsys):
    ledger = LocalUsageLedger(tmp_path)
    ledger.initialize()
    with closing(sqlite3.connect(ledger.path)) as connection:
        connection.execute("PRAGMA user_version=1")
    original = ledger.path.read_bytes()
    source = Path(__file__).parents[2] / "examples/plugins/model-review"
    assert main(["plugin", "validate", str(source), "--home", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True
    assert ledger.path.read_bytes() == original


def test_model_command_cli_uses_configured_route_and_closes_provider(tmp_path, capsys, monkeypatch):
    configs = ProviderConfigManager(tmp_path)
    provider_id = configs.create_provider(
        display_name="Test",
        provider_type="custom",
        protocol="openai-chat-completions",
        base_url="https://model.test",
    ).provider_id
    endpoint_id = configs.create_endpoint(
        provider_id, model_id="review", make_default=True
    ).endpoint_id
    configs.set_default_provider(provider_id)
    opened, closed = [], []

    class Provider(FauxProvider):
        async def close(self):
            closed.append(self)

    def build(options):
        provider = Provider()
        opened.append(provider)
        return RuntimeAdapter(
            provider,
            ModelMetadata(
                provider_id=options.provider_id,
                endpoint_id=options.endpoint_id,
                model_id=options.model,
            ),
        )

    monkeypatch.setattr(
        "ouro.local_application._authenticated_adapter_builder",
        lambda *args, provider_binder=None: build,
    )
    source = Path(__file__).parents[2] / "examples/plugins/model-review"
    args = ["--home", str(tmp_path), "--json"]
    plugin = "dev.ouro.examples.model_review"
    assert main(["plugin", "install", str(source), *args]) == 0
    capsys.readouterr()
    assert main(["plugin", "enable", plugin, *args]) == 0
    capsys.readouterr()
    assert not opened
    assert (
        main(["plugin", "run-command", f"{plugin}:model-review", "default Review this", *args]) == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["output"] == "Hello from OURO."
    assert len(opened) == 1 and opened == closed
    (entry,) = LocalUsageLedger(tmp_path).list_entries(UsageFilter(plugin_ids={plugin})).entries
    assert entry.endpoint_id == endpoint_id and entry.run_id is None


def test_plugin_cli_full_lifecycle_without_model_provider(tmp_path, capsys) -> None:
    source = tmp_path / "plugin"
    manifest_dir = source / ".ouro-plugin"
    manifest_dir.mkdir(parents=True)
    manifest = {
        "schema_version": 3,
        "plugin_id": "dev.ouro.cli",
        "name": "CLI plugin",
        "version": "1.0.0",
        "namespace": "cli_plugin",
        "engines": {"ouro": ">=0.3.0,<0.4.0", "plugin_api": "^3.0.0"},
        "runtime": {"kind": "python-inprocess", "entrypoint": "cli_plugin:create_plugin"},
        "contributes": {"commands": [{"id": "hello", "description": "Say hello."}]},
        "permissions": [],
        "configuration": {"schema": "./configuration.schema.json", "version": 1},
        "dependencies": {"plugins": []},
        "metadata": {},
    }
    (manifest_dir / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (source / "configuration.schema.json").write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {"mode": {"type": "string"}},
                "additionalProperties": False,
            }
        ),
        encoding="utf-8",
    )
    (source / "cli_plugin.py").write_text(
        """from ouro.sdk.plugins.v3 import CommandContribution, ContributionSet
class Plugin:
    async def prepare(self):
        async def hello(call): return 'hello ' + call.argument
        return ContributionSet(commands=[CommandContribution(
            id='hello', description='Say hello.', handler=hello,
        )])
    async def start(self): print('plugin-start-noise')
    async def stop(self, reason, deadline): print('plugin-stop-noise')
def create_plugin(context): return Plugin()
""",
        encoding="utf-8",
    )
    home = tmp_path / "home"

    assert main(["plugin", "validate", str(source), "--home", str(home), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True
    assert main(["plugin", "install", str(source), "--home", str(home), "--json"]) == 0
    installed = json.loads(capsys.readouterr().out)
    assert installed["status"] == "disabled"

    assert main(["plugin", "list", "--home", str(home), "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)[0]
    assert listed["plugin_id"] == "dev.ouro.cli"
    assert listed["plugin_class"] == "user"
    assert "scope" not in listed
    assert main(["plugin", "show", "dev.ouro.cli", "--home", str(home), "--json"]) == 0
    detail = json.loads(capsys.readouterr().out)
    assert detail["runtime_kind"] == "python-inprocess"
    assert "trust" not in detail
    assert main(["plugin", "enable", "dev.ouro.cli", "--home", str(home), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "active"

    assert main(["plugin", "commands", "--home", str(home), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["command_id"] == "dev.ouro.cli:hello"
    assert (
        main(
            [
                "plugin",
                "run-command",
                "dev.ouro.cli:hello",
                "world",
                "--home",
                str(home),
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["output"] == "hello world"

    assert (
        main(
            [
                "plugin",
                "configure",
                "dev.ouro.cli",
                "--values",
                '{"mode":"test"}',
                "--home",
                str(home),
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "active"
    assert main(["plugin", "check", "dev.ouro.cli", "--home", str(home), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "active"

    assert main(["plugin", "disable", "dev.ouro.cli", "--home", str(home), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "disabled"
    assert main(["plugin", "remove", "dev.ouro.cli", "--home", str(home), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "remove"
    assert main(["plugin", "gc", "--home", str(home), "--json"]) == 0
    assert "removed" in json.loads(capsys.readouterr().out)


def test_plugin_cli_has_no_workspace_install_scope(tmp_path) -> None:
    with pytest.raises(SystemExit):
        main(
            [
                "plugin",
                "install",
                str(tmp_path),
                "--scope",
                "workspace",
                "--home",
                str(tmp_path / "home"),
            ]
        )


def test_plugin_cli_strict_state_error_is_typed_and_does_not_quarantine(tmp_path, capsys) -> None:
    plugin_root = tmp_path / "home" / "plugins"
    plugin_root.mkdir(parents=True)
    state = plugin_root / "state.json"
    state.write_text(json.dumps({"schema_version": 1, "plugins": {}}), encoding="utf-8")

    code = main(["plugin", "list", "--home", str(tmp_path / "home"), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 7
    assert payload["status"] == "failed"
    assert payload["error_type"] == "PluginStateFormatError"
    assert state.exists()
    assert not list(plugin_root.glob("state.invalid-*.json"))


def test_plugin_cli_reports_validation_and_compatibility(tmp_path, capsys) -> None:
    invalid = tmp_path / "invalid"
    invalid.mkdir()
    assert main(["plugin", "validate", str(invalid), "--home", str(tmp_path), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["valid"] is False
    assert result["diagnostics"][0]["code"] == "plugin_validation_failed"

    community = tmp_path / "community"
    (community / ".claude-plugin").mkdir(parents=True)
    (community / ".claude-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
    assert main(["plugin", "import-report", str(community), "--home", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["source_format"] == "claude"


def test_plugin_cli_scaffold_and_explicit_reload_flow(tmp_path, capsys) -> None:
    source = tmp_path / "starter"
    home = tmp_path / "home"
    scaffold = [
        "plugin",
        "scaffold",
        str(source),
        "--plugin-id",
        "dev.ouro.starter",
        "--name",
        "Starter plugin",
        "--home",
        str(home),
        "--json",
    ]

    assert main(scaffold) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["plugin_id"] == "dev.ouro.starter"
    assert created["runtime_kind"] == "python-inprocess"
    assert ".ouro-plugin/plugin.json" in created["files"]
    assert main(["plugin", "validate", str(source), "--home", str(home), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True
    assert main(["plugin", "install", str(source), "--home", str(home), "--json"]) == 0
    first_digest = json.loads(capsys.readouterr().out)["package_digest"]
    assert main(["plugin", "enable", "dev.ouro.starter", "--home", str(home), "--json"]) == 0
    capsys.readouterr()

    module = source / "starter_plugin.py"
    module.write_text(module.read_text(encoding="utf-8") + "\n# local reload\n", encoding="utf-8")
    assert (
        main(
            [
                "plugin",
                "reload",
                "dev.ouro.starter",
                str(source),
                "--home",
                str(home),
                "--json",
            ]
        )
        == 0
    )
    reloaded = json.loads(capsys.readouterr().out)
    assert reloaded["action"] == "reload"
    assert reloaded["status"] == "active"
    assert reloaded["package_digest"] != first_digest


def test_plugin_compatibility_report_categorizes_each_outcome(tmp_path, capsys) -> None:
    community = tmp_path / "pi-package"
    (community / "skills" / "review").mkdir(parents=True)
    (community / "skills" / "review" / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review work.\n---\n\nInstructions.\n",
        encoding="utf-8",
    )
    (community / "package.json").write_text(
        json.dumps(
            {
                "version": "1.0.0",
                "keywords": ["pi"],
                "mcpServers": {"notes": {"command": "notes-server"}},
                "hooks": {"observe": {"event": "after_run"}},
            }
        ),
        encoding="utf-8",
    )

    assert main(["plugin", "import-report", str(community), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["source_format"] == "pi"
    assert report["outcome_counts"] == {
        "supported": 1,
        "converted": 1,
        "ignored": 1,
        "incompatible": 1,
    }
    targets = [item.get("ouro_target") for item in report["items"]]
    assert any(target and "Run Skill Catalog" in target for target in targets)
    assert any(target and "stdio worker or MCP" in target for target in targets)
