from __future__ import annotations

import json

import pytest

from ouro.application.plugins import PluginService
from ouro.plugins.store import PluginPackageStore
from ouro.tui.plugin_configuration_schema import configuration_fields, schema_placeholder
from ouro.tui.plugin_configuration_screen import PluginConfigurationScreen
from ouro.tui.screens import ChoiceScreen, ConfirmationScreen, EditableChoiceScreen
from tests.plugins.test_plugin_system import _write_native_plugin
from tests.tui.test_choice_menu_performance import ChoiceApp


def _screen(tmp_path):
    source = tmp_path / "package"
    _write_native_plugin(source, extra={"configuration": {"schema": "./config.json"}})
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "title": "Display name"},
            "count": {"type": "integer", "minimum": 1, "default": 2},
            "toggle": {"type": "boolean", "default": False},
            "nullable": {"type": ["string", "null"]},
            "choice": {"enum": [False, 2, {"key": 1}]},
            "mapping": {
                "type": "object",
                "additionalProperties": {"type": "integer"},
                "examples": [{"sample": 4}],
            },
            "branch": {"oneOf": [{"type": "string"}, {"type": "integer"}]},
        },
        "additionalProperties": True,
    }
    (source / "config.json").write_text(json.dumps(schema), encoding="utf-8")
    service = PluginService(PluginPackageStore(tmp_path / "home"))
    service.install(source)
    view = service.configuration("dev.ouro.echo")

    async def preview(candidate):
        return service.preview_configuration(candidate)

    async def reload():
        return service.configuration("dev.ouro.echo", package_digest=view.package_digest)

    screen = PluginConfigurationScreen(
        view, preview=preview, apply=service.configure, reload=reload
    )
    return screen, service


def test_schema_projection_keeps_complex_models_json_and_placeholders_bounded(tmp_path):
    screen, _ = _screen(tmp_path)
    fields = {field.key: field for field in screen._fields.values()}
    assert {key: field.kind for key, field in fields.items()} == {
        "name": "string",
        "count": "integer",
        "toggle": "select",
        "nullable": "string",
        "choice": "select",
        "mapping": "json",
        "branch": "json",
    }
    assert fields["nullable"].nullable
    assert fields["choice"].options == (False, 2, {"key": 1})
    assert schema_placeholder(fields["mapping"].schema, {}) == '{"sample":4}'
    recursive = {
        "type": "object",
        "$defs": {"node": {"type": "object", "properties": {"child": {"$ref": "#/$defs/node"}}}},
        "properties": {"tree": {"$ref": "#/$defs/node"}},
    }
    assert len(schema_placeholder(recursive, recursive)) < 200
    assert configuration_fields(recursive)[0].kind == "json"


async def test_common_controls_complex_json_and_unknown_fields_round_trip(tmp_path):
    screen, service = _screen(tmp_path)
    app = ChoiceApp()
    async with app.run_test(size=(100, 35)) as pilot:
        await app.push_screen(screen)
        assert isinstance(screen, EditableChoiceScreen)
        field_ids = {field.key: key for key, field in screen._fields.items()}
        mapping = next(choice for choice in screen._choices if choice.value == field_ids["mapping"])
        assert mapping.edit_lines == 3 and '{"sample":4}' in mapping.edit_placeholder
        worker = app.run_worker(screen._select_choice(field_ids["choice"]))
        await pilot.pause()
        assert isinstance(app.screen, ChoiceScreen) and app.screen is not screen
        await app.screen._select_choice("2")
        await worker.wait()
        assert screen._draft.values["choice"] == {"key": 1}
        await screen._save_field(
            "values-json", '{"count":3,"extension":{"keep":[1,false]},"choice":{"key":1}}'
        )
        await screen._save_field(field_ids["name"], "")
        await screen._save_field(field_ids["mapping"], '{"custom":5}')
        assert screen._draft.values["name"] == ""
        assert screen._draft.values["extension"] == {"keep": [1, False]}
        await screen._select_choice("save")
        await pilot.pause()
        saved = service.configuration("dev.ouro.echo").values
        assert saved["count"] == 3 and saved["mapping"] == {"custom": 5}
        assert saved["extension"] == {"keep": [1, False]}
        assert saved["choice"] == {"key": 1}


async def test_validation_and_conflict_keep_draft_until_explicit_reload(tmp_path):
    screen, service = _screen(tmp_path)
    app = ChoiceApp()
    async with app.run_test(size=(80, 28)) as pilot:
        await app.push_screen(screen)
        count_id = next(key for key, field in screen._fields.items() if field.key == "count")
        await screen._save_field(count_id, "0")
        await screen._select_choice("save")
        assert app.screen is screen and screen._draft.values["count"] == 0
        assert screen._issues[0].path == "/values/count"
        branch_id = next(key for key, field in screen._fields.items() if field.key == "branch")
        await screen._save_field(branch_id, "false")
        rows = screen._configuration_choices()
        assert any(row.trailing == "oneOf" and row.label == "/values/branch" for row in rows)
        assert any(row.trailing == "type" and row.label == "/values/branch" for row in rows)
        await screen._save_field(branch_id, '"accepted"')
        await screen._save_field(count_id, "4")
        await service.configure(
            service.configuration("dev.ouro.echo").candidate(values={"count": 6})
        )
        await screen._select_choice("save")
        assert app.screen is screen and screen._draft.values["count"] == 4
        assert screen._issues[-1].code == "configuration_conflict"
        worker = app.run_worker(screen._select_choice("reload"))
        await pilot.pause()
        await app.screen._select_choice("__confirmation_cancel__")
        await worker.wait()
        assert screen._draft.values["count"] == 4
        worker = app.run_worker(screen._select_choice("reload"))
        await pilot.pause()
        await app.screen._select_choice(ConfirmationScreen.CONFIRM_VALUE)
        await worker.wait()
        assert screen._draft.values["count"] == 6
        assert (
            screen._draft.expected_configuration_revision_id
            == service.configuration("dev.ouro.echo").configuration_revision_id
        )


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1e9999", "[1, NaN]"])
async def test_nonfinite_json_never_replaces_the_editable_draft(tmp_path, value):
    screen, _ = _screen(tmp_path)
    app = ChoiceApp()
    async with app.run_test():
        await app.push_screen(screen)
        before = screen._draft.model_dump()
        field = next(key for key, field in screen._fields.items() if field.key == "mapping")
        assert await screen._save_field(field, value) is None
        assert screen._draft.model_dump() == before
