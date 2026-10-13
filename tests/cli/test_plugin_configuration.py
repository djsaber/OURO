from __future__ import annotations

import json

from ouro.application.plugins import PluginService
from ouro.cli.main import main
from ouro.plugins.store import PluginPackageStore
from tests.application.test_plugin_configuration import _package


def test_cli_configuration_export_preview_apply_and_offline_conflict(tmp_path, capsys):
    source = tmp_path / "package"
    home = tmp_path / "home"
    _package(source, runtime=True)
    (source / "echo_plugin.py").write_text(
        "raise RuntimeError('must not execute')", encoding="utf-8"
    )
    service = PluginService(PluginPackageStore(home))
    service.install(source)
    exported = tmp_path / "draft.json"
    suffix = ["--home", str(home), "--json"]
    assert (
        main(["plugin", "configuration", "dev.ouro.echo", "--export", str(exported), *suffix]) == 0
    )
    view = json.loads(capsys.readouterr().out)
    assert view["schema_document"]["properties"]["count"]["type"] == "integer"
    candidate = json.loads(exported.read_text(encoding="utf-8"))
    assert candidate["expected_configuration_revision_id"] == view["configuration_revision_id"]
    candidate["values"] = {"count": 6}
    exported.write_text(json.dumps(candidate), encoding="utf-8")
    before = service.store.read_state()
    command = ["plugin", "configure", "dev.ouro.echo", "--file", str(exported), *suffix]
    assert main([*command, "--preview"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["candidate"]["values"] == {"count": 6, "fail": False}
    assert service.store.read_state() == before
    assert main(command) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "saved"
    assert main(command) == 1
    conflict = json.loads(capsys.readouterr().out)
    assert conflict["issues"][-1]["code"] == "configuration_conflict"
    assert main(["plugin", "configure", "dev.ouro.other", "--file", str(exported), *suffix]) != 0
    assert "target" in json.loads(capsys.readouterr().out)["error"]


def test_cli_static_preview_of_enabled_plugin_does_not_start_runtime(tmp_path, capsys):
    source = tmp_path / "package"
    home = tmp_path / "home"
    _package(source, runtime=True)
    (source / "echo_plugin.py").write_text(
        "raise RuntimeError('must not execute')", encoding="utf-8"
    )
    service = PluginService(PluginPackageStore(home))
    service.install(source)

    def enable(state):
        state.plugins["dev.ouro.echo"].activation.desired_enabled = True
        return state

    service.store.mutate_state(enable)
    before = service.store.read_state()
    assert (
        main(
            [
                "plugin",
                "configure",
                "dev.ouro.echo",
                "--values",
                '{"count": 5}',
                "--preview",
                "--json",
                "--home",
                str(home),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["valid"] and result["scope"] == "current_host"
    assert service.store.read_state() == before
