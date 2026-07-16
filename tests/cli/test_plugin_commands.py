from __future__ import annotations

import json

from ouro.cli.main import main


def test_plugin_cli_full_lifecycle_without_model_provider(tmp_path, capsys) -> None:
    source = tmp_path / "plugin"
    manifest_dir = source / ".ouro-plugin"
    manifest_dir.mkdir(parents=True)
    manifest = {
        "schema_version": 1,
        "plugin_id": "dev.ouro.cli",
        "name": "CLI plugin",
        "version": "1.0.0",
        "namespace": "cli_plugin",
        "engines": {"ouro": ">=0.1.0,<0.2.0", "plugin_api": "^1.0.0"},
        "runtime": {"kind": "python-inprocess", "entrypoint": "plugin:create_plugin"},
        "contributes": {"commands": [{"id": "hello", "description": "Say hello."}]},
        "permissions": [],
        "dependencies": {"plugins": []},
        "metadata": {},
    }
    (manifest_dir / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (source / "plugin.py").write_text(
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
    assert json.loads(capsys.readouterr().out)[0]["plugin_id"] == "dev.ouro.cli"
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
