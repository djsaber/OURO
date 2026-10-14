from __future__ import annotations

import json
from dataclasses import replace

import pytest
from rich.console import Console
from rich.text import Text
from textual import events
from textual.widgets import OptionList

from ouro.application.plugin_configuration_models import (
    InlinePluginCredential,
    PluginCredentialSlotView,
)
from ouro.application.plugins import PluginService
from ouro.plugins.store import PluginPackageStore
from ouro.tui.plugin_configuration_schema import configuration_fields, schema_placeholder
from ouro.tui.plugin_configuration_screen import PluginConfigurationScreen
from ouro.tui.screens import ChoiceScreen, ConfirmationScreen, EditableChoiceScreen
from ouro.tui.theme import DEFAULT_TUI_THEME
from tests.plugins.test_plugin_system import _write_native_plugin
from tests.tui.test_choice_menu_performance import ChoiceApp


def _screen(tmp_path):
    source = tmp_path / "package"
    _write_native_plugin(
        source,
        extra={
            "configuration": {
                "schema": "./config.json",
                "credentials": {"key": {"title": "API key"}},
            },
            "permissions": [
                {
                    "id": "credentials.use",
                    "scopes": ["key"],
                    "reason": "Exercise credential editing.",
                }
            ],
        },
    )
    schema = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "title": "Display name",
                "description": "A name shown to the user.",
            },
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
        assert mapping.edit_lines == 1 and '{"sample":4}' in mapping.edit_placeholder
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


def test_configuration_rows_hide_absent_values_and_keep_explicit_empty_values(tmp_path):
    screen, _ = _screen(tmp_path)
    screen._view.credential_slots = (
        PluginCredentialSlotView(slot="key", title="API key", required=True),
    )
    field_ids = {field.key: key for key, field in screen._fields.items()}
    screen._fields = {key: replace(field, required=True) for key, field in screen._fields.items()}
    choices = {choice.value: choice for choice in screen._configuration_choices()}
    for name in ("name", "nullable", "mapping", "branch"):
        assert choices[field_ids[name]].trailing == ""
        assert choices[field_ids[name]].trailing_badge == "required"
    assert "A name shown to the user." in choices[field_ids["name"]].edit_placeholder
    assert choices["credential:key"].trailing == ""
    assert choices["credential:key"].trailing_badge == "required"
    screen._draft.values.update(name="", nullable=None, toggle=False, mapping={}, count=0)
    screen._draft.credential_refs["key"] = "env:EXAMPLE_API_KEY"
    choices = {choice.value: choice for choice in screen._configuration_choices()}
    assert choices[field_ids["name"]].trailing == '""'
    assert choices[field_ids["nullable"]].trailing == "null"
    assert choices[field_ids["toggle"]].trailing == "false"
    assert choices[field_ids["mapping"]].trailing == "{}"
    assert choices["credential:key"].trailing == DEFAULT_TUI_THEME.mask_character * 5 + " [ENV]"
    for name in ("name", "nullable", "toggle", "mapping", "count"):
        assert choices[field_ids[name]].trailing_badge == ""
    assert choices["credential:key"].trailing_badge == ""
    screen._draft.values.pop("name")
    screen._draft.credential_refs.clear()
    choices = {choice.value: choice for choice in screen._configuration_choices()}
    assert choices[field_ids["name"]].trailing_badge == "required"
    assert choices["credential:key"].trailing_badge == "required"


@pytest.mark.parametrize("source", ("env", "inline"))
async def test_plugin_credentials_are_masked_without_changing_saved_values(tmp_path, source):
    screen, service = _screen(tmp_path)
    screen._initial_tui_theme = replace(DEFAULT_TUI_THEME, mask_character="*")
    original = "env:ORIGINAL_API_KEY"
    updated = "env:UPDATED_API_KEY" if source == "env" else "env:literal-key-with-prefix"
    screen._draft.credential_refs["key"] = original
    screen._choices = screen._configuration_choices()
    app = ChoiceApp()
    async with app.run_test(size=(80, 30)) as pilot:
        await app.push_screen(screen)
        await pilot.pause()
        options = screen.query_one(OptionList)
        row = options.get_option("credential:key")
        rendered = "".join(segment.text for segment in app.console.render(row.prompt))
        assert original not in rendered
        assert "***** [ENV]" in rendered

        async def open_source():
            options.highlighted = options.get_option_index("credential:key")
            await pilot.press("enter")
            await pilot.pause()
            assert app.screen is screen
            if screen._credential_source != source:
                await pilot.press("tab")
            return screen, options

        editor, fields = await open_source()
        assert editor._edit_text == (original if source == "env" else "")
        await pilot.press("ctrl+u")
        editor.post_message(events.Paste(updated))
        await pilot.pause()
        prompt = fields.get_option("credential:key").prompt.plain
        assert prompt.startswith("*" * len(updated))
        assert prompt.endswith("[KEY]" if source == "inline" else "[ENV]")
        await pilot.press("enter")
        await pilot.pause()
        binding = screen._draft.credential_refs["key"]
        assert (
            binding.value.get_secret_value()
            if isinstance(binding, InlinePluginCredential)
            else binding
        ) == updated
        await open_source()
        await pilot.press("backspace", "escape")
        assert screen._draft.credential_refs["key"] == binding
        await open_source()
        await pilot.press("ctrl+u", "enter")
        await pilot.pause()
        assert "key" not in screen._draft.credential_refs
        choice = next(choice for choice in screen._choices if choice.value == "credential:key")
        assert choice.trailing == ""
        editor, _ = await open_source()
        editor.post_message(events.Paste(updated))
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        name_id = next(key for key, field in screen._fields.items() if field.key == "name")
        options.highlighted = options.get_option_index(name_id)
        await pilot.press("enter")
        await pilot.press(*"Visible")
        assert options.get_option(name_id).prompt.plain == "Visible "
        await pilot.press("enter")
        await screen._select_choice("save")
        await pilot.pause()
        saved = service.configuration("dev.ouro.echo")
        assert saved.credential_refs == {"key": binding}
        assert saved.values["name"] == "Visible"


def test_configuration_labels_and_action_hints_render_on_one_line(tmp_path):
    screen, _ = _screen(tmp_path)
    console = Console(width=60)
    for option in screen._options(screen._configuration_choices()):
        if option.id is None:
            continue
        lines = console.render_lines(option.prompt)
        assert len(lines) == 1
        choice = next(choice for choice in screen._choices if choice.value == option.id)
        assert "".join(segment.text for segment in lines[0]).startswith(choice.label)


async def test_plugin_json_editing_stays_single_line_and_preserves_full_values(tmp_path):
    screen, _ = _screen(tmp_path)
    mapping_id = next(key for key, field in screen._fields.items() if field.key == "mapping")
    value = {f"key_{index}": index for index in range(30)}
    app = ChoiceApp()
    async with app.run_test(size=(80, 30)) as pilot:
        await app.push_screen(screen)
        await pilot.pause()
        options = screen.query_one(OptionList)
        options.highlighted = options.get_option_index(mapping_id)
        await pilot.press("enter")
        assert screen._editing_value == mapping_id
        prompt = options.get_option(mapping_id).prompt
        assert isinstance(prompt, Text) and prompt.cell_len <= screen._editor_width()
        screen.post_message(events.Paste(json.dumps(value, indent=2)))
        await pilot.pause()
        assert "\n" not in screen._edit_text
        assert options.get_option(mapping_id).prompt.cell_len <= screen._editor_width()
        await pilot.press("enter")
        assert screen._draft.values["mapping"] == value
        await pilot.press("enter")
        assert json.loads(screen._edit_text) == value
        await pilot.press("escape")
        for field_id, label in ((mapping_id, "mapping"), ("values-json", "Settings (JSON)")):
            lines = app.console.render_lines(
                options.get_option(field_id).prompt, app.console.options.update_width(40)
            )
            assert len(lines) == 1
            assert "".join(segment.text for segment in lines[0]).startswith(label)


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
