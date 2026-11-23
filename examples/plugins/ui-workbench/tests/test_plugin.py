from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from ui_workbench_plugin.files import WorkbenchError, publish_bundle, read_limited, workspace_file
from ui_workbench_plugin.plugin import create_plugin
from ui_workbench_plugin.settings import Settings

from ouro.sdk.plugins import v6 as sdk

_ROOT = Path(__file__).resolve().parents[1]


def _context(tmp_path, *, configuration=None, permissions=frozenset(), credentials=None):
    return cast(
        sdk.PluginContext,
        SimpleNamespace(
            configuration=configuration or {},
            identity=SimpleNamespace(version="0.1.0"),
            artifact_root=str(tmp_path / "artifacts"),
            permissions=permissions,
            credentials=credentials,
        ),
    )


async def test_declared_contributions_and_default_capabilities(tmp_path):
    manifest = json.loads((_ROOT / ".ouro-plugin/plugin.json").read_text(encoding="utf-8"))
    plugin = create_plugin(_context(tmp_path))
    contributions = await plugin.prepare()
    await plugin.start()
    commands = {command.id: command for command in contributions.commands}
    tools = {tool.id: tool for tool in contributions.tools}
    assert commands.keys() == {info["id"] for info in manifest["contributes"]["commands"]}
    assert tools.keys() == {info["id"] for info in manifest["contributes"]["tools"]}
    for info in manifest["contributes"]["tools"]:
        tool = tools[info["id"]]
        assert tool.exposed_name == info["exposed_name"]
        assert tool.description == info["description"]
        assert tool.permission_mode == info["permission_mode"]
        assert tool.batch_behavior == info.get("batch_behavior", "compatible")
        assert tool.effect == info["effect"]
    status = commands["status"].handler(sdk.CommandCall("status"))
    assert isinstance(status, sdk.CommandOutput)
    assert status.data == {
        "version": manifest["version"],
        "bundled_skill": "ui-workbench",
        "comparison_available": True,
        "capture_configured": False,
        "media_enabled": False,
        "image_api_configured": False,
    }
    assert not (tmp_path / "artifacts").exists()
    await plugin.stop("disable", datetime.now(UTC))
    await plugin.stop("shutdown", datetime.now(UTC))


async def test_workflow_commands_keep_user_brief_separate_from_plugin_instructions(tmp_path):
    contribution = await create_plugin(_context(tmp_path)).prepare()
    commands = {command.id: command.handler for command in contribution.commands}
    brief = "A keyboard-friendly archive"
    result = commands["explore"](sdk.CommandCall("explore", argument=brief))
    assert isinstance(result, sdk.CommandOutput)
    (batch,) = result.session_input_batches
    instructions, user_input = batch.inputs
    assert instructions.role == "harness"
    assert instructions.semantic_tag == "ui-workbench"
    assert brief not in instructions.prompt
    assert user_input.role == "user"
    assert user_input.prompt == brief
    missing = commands["explore"](sdk.CommandCall("explore", argument=" "))
    assert isinstance(missing, sdk.CommandOutput)
    assert not missing.session_input_batches
    review = commands["review"](sdk.CommandCall("review"))
    assert isinstance(review, sdk.CommandOutput)
    assert len(review.session_input_batches[0].inputs) == 1


async def test_compare_command_uses_only_workspace_and_publishes_separate_bundles(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for name in ("first", "second"):
        (workspace / f"{name}.html").write_text(f"<h1>{name}</h1>", encoding="utf-8")
    (workspace / "manifest.json").write_text(
        json.dumps(
            {
                "title": "Board",
                "candidates": [
                    {"title": name, "path": f"{name}.html"} for name in ("first", "second")
                ],
            }
        ),
        encoding="utf-8",
    )
    contribution = await create_plugin(_context(tmp_path)).prepare()
    compare = next(command.handler for command in contribution.commands if command.id == "compare")
    call = sdk.CommandCall("compare", argument="manifest.json", workspace=str(workspace))
    first = await compare(call)
    second = await compare(call)
    (one,), (two,) = first.data["files"], second.data["files"]
    assert one != two
    assert Path(one).is_relative_to(tmp_path / "artifacts")
    assert Path(one).read_bytes() == Path(two).read_bytes()
    assert not (workspace / "comparison.html").exists()
    error = await compare(sdk.CommandCall("compare", argument="manifest.json"))
    assert "workspace" in error.data["error"]


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ({"webdriver_url": "https://remote.example:9515"}, "webdriver_url"),
        ({"webdriver_url": "http://127.0.0.1:65536"}, "webdriver_url"),
        ({"image_model": "example"}, "both"),
        (
            {"image_api_base": "https://example.test/v1?secret=value", "image_model": "m"},
            "image_api_base",
        ),
    ],
)
def test_settings_reject_incomplete_or_untrusted_endpoints(values, expected):
    with pytest.raises(WorkbenchError, match=expected) as error:
        Settings.from_configuration(values)
    assert "secret=value" not in str(error.value)


def test_file_boundaries_and_artifact_names(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "visible.txt").write_bytes(b"1234")
    (workspace / ".private").write_text("not for export", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("outside", encoding="utf-8")
    source = workspace_file(workspace, "visible.txt")
    assert read_limited(source, 4) == b"1234"
    with pytest.raises(WorkbenchError, match="size limit"):
        read_limited(source, 3)
    for relative in (".private", "../outside.txt", "C:\\outside.txt", "visible.txt:stream"):
        with pytest.raises(WorkbenchError, match="inside the current workspace"):
            workspace_file(workspace, relative)
    for names in (("../escape",), ("CON.txt",), ("x.PNG", "x.png")):
        with pytest.raises(WorkbenchError, match="Invalid artifact bundle"):
            publish_bundle(tmp_path / "artifacts", "evidence", dict.fromkeys(names, b"1"))
    assert not (tmp_path / "artifacts").exists()
