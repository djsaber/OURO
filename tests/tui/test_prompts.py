from textual import events
from textual.app import App
from textual.widgets import Input, OptionList

from ouro.application.configuration import ConfigurationFacade
from ouro.config.prompts import PromptConfig, PromptConfigManager, PromptDraft, PromptEntry
from ouro.tui.app import OuroTuiApp
from ouro.tui.prompt_screens import PromptEntryScreen, prompt_choices
from ouro.tui.screens import ConfirmationScreen
from ouro.tui.theme import DEFAULT_TUI_THEME
from tests.tui.helpers import run_ready_app, two_route_application


async def test_prompt_editor_requires_content_and_preserves_multiline_and_cancel():
    app = App()
    results = []
    async with app.run_test(size=(80, 24)) as pilot:
        form = PromptEntryScreen("addition")
        app.push_screen(form, callback=results.append)
        await pilot.pause()
        assert form.query_one(OptionList).get_option("save").disabled
        assert all(choice.value != "id" for choice in form._choices)
        form.query_one(Input).value = "Content"
        await pilot.pause()
        await pilot.press("enter", "a", "ctrl+enter", "b", "enter")
        assert form._values["text"] == "a\nb"
        save = next(choice for choice in form._choices if choice.value == "save")
        assert not save.disabled
        assert save.trailing == ""
        await pilot.press("enter")
        form.post_message(events.Paste("\r\n中文"))
        await pilot.pause()
        await pilot.press("escape")
        assert form._values["text"] == "a\nb"
        form.query_one(Input).value = "Save"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert results[0] == PromptDraft(text="a\nb")


def test_prompt_choices_use_concise_group_and_add_labels():
    choices = prompt_choices(
        PromptConfig(
            replacements=(PromptEntry(id="base", text="Base", enabled=True),),
            additions=(PromptEntry(id="note", text="Note"),),
        )
    )

    assert {choice.group for choice in choices} == {
        "ASSISTANT BEHAVIOR",
        "ADDITIONAL GUIDANCE",
    }
    assert [choice.label for choice in choices if choice.value.startswith("add:")] == ["+", "+"]


def test_prompt_editor_uses_action_label_and_danger_for_replacement_activation():
    screen = PromptEntryScreen("replacement")

    description = next(choice for choice in screen._choices if choice.value == "description")
    activation = next(choice for choice in screen._choices if choice.value == "enabled")

    assert description.label == "Description"
    assert screen.page_title == "ADD ASSISTANT BEHAVIOR"
    assert activation.label == "Enable"
    assert activation.trailing == ""
    assert activation.description == ""
    assert activation.description_accent is None

    enabled_screen = PromptEntryScreen(
        "replacement", PromptEntry(id="base", text="Base", enabled=True)
    )
    activation = next(choice for choice in enabled_screen._choices if choice.value == "enabled")

    assert enabled_screen.page_title == "EDIT ASSISTANT BEHAVIOR"
    assert activation.label == "Disable"
    assert activation.description == "Enabling overrides OURO's default system prompt."
    assert activation.description_accent == DEFAULT_TUI_THEME.danger


async def test_prompt_actions_manage_order_activation_edit_and_delete(tmp_path):
    facade = ConfigurationFacade(tmp_path)
    for revision, (kind, entry) in enumerate(
        (
            ("replacement", PromptEntry(id="base", text="Base", enabled=True)),
            ("replacement", PromptEntry(id="other", text="Other")),
            (
                "addition",
                PromptEntry(id="first", description="First annotation", text="First", enabled=True),
            ),
            ("addition", PromptEntry(id="last", text="Last", enabled=True)),
        )
    ):
        facade.save_prompt(
            kind,
            PromptDraft.model_validate(entry.model_dump(exclude={"id"})),
            expected_revision=revision,
        )
    app = OuroTuiApp(application=two_route_application(tmp_path))

    async with run_ready_app(app, size=(100, 34)) as pilot:

        async def choose(label):
            app.screen.query_one(Input).value = label
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause(0.15)

        await pilot.press("ctrl+p")
        await pilot.pause()
        await choose("Personal prompts")
        assert any(c.label == "First annotation" for c in app.screen._choices)
        await choose("other")
        choices = {choice.value: choice for choice in app.screen._choices}
        assert choices["edit"].label == "Edit"
        assert choices["edit"].accent is None
        assert choices["delete"].label == "Delete"
        assert choices["delete"].accent == DEFAULT_TUI_THEME.danger
        assert choices["delete"].destructive
        assert choices["toggle"].description == ("Enabling overrides OURO's default system prompt.")
        assert choices["toggle"].description_accent == DEFAULT_TUI_THEME.danger
        await choose("Enable")
        config = PromptConfigManager(tmp_path).snapshot().config
        assert [p.enabled for p in config.replacements] == [False, True]
        await choose("last")
        await choose("Move up")
        assert (
            PromptConfigManager(tmp_path).snapshot().config.additional_system_prompt
            == "Last\n\nFirst"
        )
        await choose("First annotation")
        await choose("Edit")
        assert isinstance(app.screen, PromptEntryScreen)
        await choose("Content")
        await pilot.press("ctrl+u", "n", "ctrl+enter", "e", "enter")
        await choose("Save")
        assert PromptConfigManager(tmp_path).snapshot().config.additions[1].text == "n\ne"
        await choose("last")
        await choose("Disable")
        assert PromptConfigManager(tmp_path).snapshot().config.additional_system_prompt == "n\ne"
        await choose("last")
        await choose("Delete")
        assert isinstance(app.screen, ConfirmationScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert len(PromptConfigManager(tmp_path).snapshot().config.additions) == 2
        await choose("Delete")
        await pilot.press("down", "enter")
        await pilot.pause(0.2)
        assert len(PromptConfigManager(tmp_path).snapshot().config.additions) == 1
        await pilot.press("escape", "escape")


async def test_prompt_creation_and_stale_editor_do_not_overwrite_other_changes(tmp_path):
    app = OuroTuiApp(application=two_route_application(tmp_path))
    facade = ConfigurationFacade(tmp_path)
    async with run_ready_app(app, size=(80, 24), notifications=True) as pilot:

        async def choose(label):
            app.screen.query_one(Input).value = label
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause(0.15)

        await pilot.press("ctrl+p")
        await pilot.pause()
        await choose("Personal prompts")
        app.screen.query_one(Input).value = "+"
        await pilot.pause()
        await pilot.press("right", "enter")
        await pilot.pause(0.15)
        assert isinstance(app.screen, PromptEntryScreen)
        assert all(choice.value != "id" for choice in app.screen._choices)
        await choose("Content")
        await pilot.press("h", "i", "enter")
        await choose("Enable")
        assert (
            next(choice for choice in app.screen._choices if choice.value == "enabled").label
            == "Disable"
        )
        await choose("Save")
        config = facade.system_prompt().config
        assert len(config.additions) == 1
        entry_id = config.additions[0].id
        assert entry_id.startswith("prompt_")
        assert config.additions[0].text == "hi"
        await choose("hi")
        await choose("Edit")
        # Another Host saves after the edit form was opened.
        facade.save_prompt(
            "addition",
            PromptDraft(text="External edit", enabled=True),
            entry_id=entry_id,
            expected_revision=config.revision,
        )
        await choose("Content")
        await pilot.press("ctrl+u", "x", "enter")
        await choose("Save")
        assert facade.system_prompt().config.additions[0].text == "External edit"
        assert any("revision conflict" in str(item.message) for item in app._notifications)
        # Refresh and edit again against the current revision.
        await choose("External edit")
        await choose("Disable")
        assert facade.system_prompt().config.additional_system_prompt == ""
        await pilot.press("escape", "escape")
