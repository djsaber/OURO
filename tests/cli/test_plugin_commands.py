from __future__ import annotations

import json

import pytest

from ouro.cli.main import main


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
        "engines": {"ouro": ">=0.1.0,<0.2.0", "plugin_api": "^2.0.0"},
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
        """from ouro.sdk.plugins.v1 import CommandContribution, ContributionSet
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
    assert json.loads(capsys.readouterr().out)["runtime_kind"] == "python-inprocess"

    assert main(["plugin", "authorize", "dev.ouro.cli", "--home", str(home), "--json"]) == 0
    capsys.readouterr()
    assert main(["plugin", "trust-code", "dev.ouro.cli", "--home", str(home), "--json"]) == 0
    capsys.readouterr()
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

    assert main(["plugin", "revoke", "dev.ouro.cli", "--home", str(home), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "needs_authorization"
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
    for command in ("authorize", "trust-code", "enable"):
        assert main(["plugin", command, "dev.ouro.starter", "--home", str(home), "--json"]) == 0
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
    pending = json.loads(capsys.readouterr().out)
    # Capability authorization is semantic and remains valid because the declared
    # permissions/contributions did not change; executable bytes require exact trust.
    assert pending["status"] == "needs_code_trust"
    assert pending["package_digest"] != first_digest
    assert main(["plugin", "show", "dev.ouro.starter", "--home", str(home), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["package_digest"] == first_digest

    assert (
        main(
            [
                "plugin",
                "reload",
                "dev.ouro.starter",
                str(source),
                "--authorize",
                "--trust-code",
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
