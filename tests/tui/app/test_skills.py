"""TUI application scenarios: skills."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.widgets import Input, OptionList, Static

from ouro.application.contracts.configuration import SkillCatalogView, SkillView
from ouro.local_configuration import build_local_configuration
from ouro.skills import SkillLoader
from ouro.tui.app import OuroTuiApp
from ouro.tui.composer.input import Composer
from ouro.tui.composer.popup import CommandPopup
from ouro.tui.features.configuration.skills import SkillInstallScreen
from ouro.tui.features.configuration.workflows import _skill_choice, _skill_choices
from ouro.tui.menus.choice import ChoiceScreen
from ouro.tui.menus.editable import EditableChoiceScreen
from ouro.tui.view_models import TuiPluginInstallConfiguration
from tests.tui.app.support import _application, _write_skill_plugin
from tests.tui.helpers import choose_menu, run_ready_app, wait_for_events
from tests.tui.helpers import wait_for_screen as _wait_for_screen
from tests.tui.helpers import wait_for_worker_group as _wait_for_worker_group
from tests.tui.helpers import wait_until as _wait_until


@pytest.mark.parametrize(
    ("owner", "label"),
    [
        ({"plugin_id": "dev.ouro.calendar", "plugin_name": "Calendar"}, "Calendar • plugin"),
        ({"plugin_id": "dev.ouro.calendar"}, "dev.ouro.calendar • plugin"),
        ({}, "plugin"),
    ],
)
def test_plugin_skill_choice_links_to_its_source(owner, label) -> None:
    choice = _skill_choice(
        SkillView(
            name="calendar",
            description="Manage calendars.",
            location="C:/plugins/calendar/SKILL.md",
            source="plugin",
            origin="plugin",
            enabled=True,
            extensions={"ouro": owner},
        )
    )

    assert choice.disabled is False
    assert choice.trailing == label


def test_skill_catalog_sorts_by_source_without_groups_or_status_labels() -> None:
    def skill(name: str, origin: str, source: str = "user", **kwargs) -> SkillView:
        return SkillView(
            name=name,
            description="",
            location=f"/{name}/SKILL.md",
            origin=origin,
            source=source,
            enabled=True,
            **kwargs,
        )

    choices = _skill_choices(
        [
            skill("z", "ouro"),
            skill("plugin-b", "plugin", "plugin", extensions={"ouro": {"plugin_id": "b"}}),
            skill("a", "codex"),
            skill("b", "ouro"),
            skill("plugin-a", "plugin", "plugin", extensions={"ouro": {"plugin_id": "a"}}),
        ]
    )
    assert [c.value for c in choices] == ["a", "b", "z", "plugin-a", "plugin-b"]
    assert [c.trailing for c in choices] == [
        "codex • user",
        "ouro • user",
        "ouro • user",
        "a • plugin",
        "b • plugin",
    ]
    assert all(not c.group and not c.disabled for c in choices)


@pytest.mark.asyncio
@pytest.mark.parametrize("isolated", [False, True])
async def test_skill_actions_open_skill_catalog(
    tmp_path: Path, user_profile: Path, monkeypatch, isolated
) -> None:
    home = tmp_path / "home" if isolated else user_profile / ".ouro"
    for name, skill_dir in (
        ("demo", home / "skills" / "demo"),
        ("codex", user_profile / ".codex" / "skills" / "codex"),
        ("claude", user_profile / ".claude" / "skills" / "claude"),
    ):
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Demo skill.\n---\n\nUse the demo workflow.\n",
            encoding="utf-8",
        )
    monkeypatch.chdir(user_profile)
    app = OuroTuiApp(home=home, application=_application(home))

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        await choose_menu(pilot, "Manage skills", title="SKILLS")

        assert isinstance(app.screen, ChoiceScreen)
        assert app.screen.query_one("#choice-title", Static).render().plain == "SKILLS"
        assert [choice.value for choice in app.screen._choices] == ["claude", "codex", "demo"]
        assert app.screen._choices[0].description == ""
        assert [choice.trailing for choice in app.screen._choices] == [
            "claude • user",
            "codex • user",
            "ouro • user",
        ]


@pytest.mark.asyncio
async def test_skill_directory_sources_use_shared_menu_and_persist_toggles(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo skill.\n---\n",
        encoding="utf-8",
    )
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        action_screen = app.screen
        action_screen.query_one("#choice-filter", Input).value = "Skill sources"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="SKILL SOURCES")

        assert app.screen is action_screen
        assert [choice.value for choice in action_screen._choices] == [
            "discovery",
            "agents.user",
            "claude.user",
            "copilot.user",
            "codex.user",
            "ouro.user",
            "agents.workspace",
            "claude.workspace",
            "github.workspace",
            "codex.workspace",
            "ouro.workspace",
        ]
        assert [choice.group for choice in action_screen._choices] == [
            "GENERAL",
            *("USER",) * 5,
            *("WORKSPACE",) * 5,
        ]
        assert [choice.label for choice in action_screen._choices[1:]] == [
            "agents",
            "claude",
            "copilot",
            "codex",
            "ouro",
            "agents",
            "claude",
            "github",
            "codex",
            "ouro",
        ]
        assert all(choice.enabled_state is True for choice in action_screen._choices)
        assert "plugin" not in {choice.value for choice in action_screen._choices}

        assert not action_screen.query_one("#choice-filter", Input).display
        assert "Enter toggle" in action_screen.query_one("#choice-hint", Static).render().plain
        options = action_screen.query_one(OptionList)
        options.highlighted = options.get_option_index("ouro.user")
        await pilot.pause()
        await pilot.press("enter")
        await _wait_until(
            lambda: (
                next(
                    choice for choice in action_screen._choices if choice.value == "ouro.user"
                ).enabled_state
                is False
            ),
            pilot,
            description="skill source update",
        )

        policy = build_local_configuration(tmp_path).skill_policy()
        assert policy.disabled_sources == ["ouro.user"]
        assert await app._controller.runtime_skill_names() == ()
        assert (
            next(
                choice for choice in action_screen._choices if choice.value == "ouro.user"
            ).enabled_state
            is False
        )

        options.highlighted = options.get_option_index("discovery")
        await pilot.pause()
        await pilot.press("enter")
        await _wait_until(
            lambda: (
                next(
                    choice for choice in action_screen._choices if choice.value == "discovery"
                ).enabled_state
                is False
            ),
            pilot,
            description="skill discovery update",
        )

        policy = build_local_configuration(tmp_path).skill_policy()
        assert policy.enabled is False
        assert policy.disabled_sources == ["ouro.user"]
        assert (
            next(
                choice for choice in action_screen._choices if choice.value == "discovery"
            ).enabled_state
            is False
        )


@pytest.mark.asyncio
async def test_skill_menu_opens_live_plugin_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "skill-plugin"
    _write_skill_plugin(source)
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.pause()
        installed = await app._controller.install_plugin(
            TuiPluginInstallConfiguration(source=str(source))
        )
        assert (await app._controller.enable_plugin(installed.plugin_id)).status == "active"

        catalog_started, release_catalog = asyncio.Event(), asyncio.Event()
        load_catalog = app._controller.skill_catalog

        async def held_catalog() -> SkillCatalogView:
            catalog_started.set()
            await release_catalog.wait()
            return await load_catalog()

        monkeypatch.setattr(app._controller, "skill_catalog", held_catalog)
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        try:
            await choose_menu(pilot, "Manage skills")
            await wait_for_events(catalog_started, pilot=pilot, description="skill catalog read")
            await pilot.pause()
            # UI idleness does not complete the catalog read owned by the selection worker.
            assert app.screen.query_one("#choice-title", Static).render().plain == "ACTIONS"
        finally:
            release_catalog.set()
        await _wait_for_screen(app, ChoiceScreen, title="SKILLS")

        assert isinstance(app.screen, ChoiceScreen)
        assert app.screen.query_one("#choice-title", Static).render().plain == "SKILLS"
        assert [choice.value for choice in app.screen._choices] == ["calendar"]
        assert app.screen._choices[0].disabled is False
        assert app.screen._choices[0].trailing == "TUI Skill • plugin"
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="PLUGIN • TUI Skill")
        assert any(c.value == "contribution:skills:calendar" for c in app.screen._choices)
        await choose_menu(pilot, "Disable")
        await _wait_until(
            lambda: any(c.value == "enable" for c in app.screen._choices),
            pilot,
            description="plugin disabled from skills",
        )
        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="SKILLS")
        await _wait_until(
            lambda: not any(c.value == "calendar" for c in app.screen._choices),
            pilot,
            description="plugin contributions removed from skill catalog",
        )


@pytest.mark.asyncio
async def test_skill_disable_action_is_owned_by_skill_submenu(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo skill.\n---\n\nUse the demo workflow.\n",
        encoding="utf-8",
    )
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+s")
        await _wait_for_screen(app, ChoiceScreen, title="SKILLS")
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="SKILL • demo")

        assert app.screen.query_one("#choice-title", Static).render().plain == "SKILL • demo"
        assert [choice.value for choice in app.screen._choices] == ["disable"]
        assert app.screen._choices[-1].label == "Disable"
        description = app.screen.query_one("#choice-description", Static).render().plain
        assert description == f"Demo skill.\n\n{skill_dir / 'SKILL.md'}"

        options = app.screen.query_one("#choice-list", OptionList)
        options.focus()
        await pilot.press("enter")
        await _wait_until(
            lambda: app.screen._choices[-1].value == "enable",
            pilot,
            description="skill disable completion",
        )
        assert await app._controller.runtime_skill_names() == ()
        assert app.screen._choices[-1].label == "Enable"
        assert [choice.value for choice in app.screen._choices] == ["enable"]

        await pilot.press("escape")
        await pilot.pause()
        assert (
            next(choice for choice in app.screen._choices if choice.value == "demo").trailing
            == "ouro • user"
        )
        disabled = next(c for c in app.screen._choices if c.value == "demo")
        assert not disabled.disabled
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="SKILL • demo")
        await choose_menu(pilot, "Enable")
        await _wait_until(
            lambda: app.screen._choices[-1].value == "disable",
            pilot,
            description="skill enable completion",
        )
        assert await app._controller.runtime_skill_names() == ("demo",)
        assert app.screen._choices[-1].label == "Disable"

        await pilot.press("escape")
        await pilot.pause()
        assert (
            next(choice for choice in app.screen._choices if choice.value == "demo").trailing
            == "ouro • user"
        )
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_skill_install_uses_shared_grouped_inline_menu(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    results = []

    async with run_ready_app(app, size=(80, 24)) as pilot:
        await pilot.pause()
        screen = SkillInstallScreen()
        app.push_screen(screen, callback=results.append)
        await pilot.pause()

        assert isinstance(screen, EditableChoiceScreen)
        assert not list(screen.query("Button"))
        assert not list(screen.query("Checkbox"))
        assert not list(screen.query("Select"))
        assert [choice.group for choice in screen._choices] == [
            *("OPTIONS",) * 6,
            "ACTIONS",
        ]
        install = next(choice for choice in screen._choices if choice.value == "skill-install")
        assert install.disabled is True
        assert install.trailing == "Source is required"

        screen._refresh("skill-source")
        await pilot.press("enter")
        await pilot.pause()
        assert screen._editor_placeholder() == "Local path or github:owner/repo"
        await pilot.press("escape")

        screen._values["skill-source"] = "github:openai/example"
        screen._refresh("skill-scope")
        await pilot.press("enter")
        await pilot.pause()
        assert screen._values["skill-scope"] == "workspace"

        screen._refresh("skill-install")
        await pilot.press("enter")
        await pilot.pause()
        assert len(results) == 1
        assert results[0].source == "github:openai/example"
        assert results[0].scope == "workspace"


@pytest.mark.asyncio
async def test_skill_command_uses_inline_catalog_and_durable_activation(tmp_path: Path) -> None:
    skill_root = tmp_path / "skills" / "reviewer"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        """---
name: reviewer
description: Review source code.
argument-hint: <task>
---
Review the requested code carefully.
""",
        encoding="utf-8",
    )
    skill = SkillLoader(skill_root.parent).load_metadata()[0]
    application = _application(tmp_path, skills=(skill,))
    app = OuroTuiApp(application=application)

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.press("@", *"skill", "enter")
        await pilot.pause()

        popup = app.query_one(CommandPopup)
        assert app.query_one(Composer).text == "@skill "
        assert popup.argument_command is not None
        assert popup.argument_command.name == "@skill"
        assert popup.selected_argument is not None
        assert popup.selected_argument.value == "reviewer"

        await pilot.press("enter")
        await pilot.pause()
        composer = app.query_one(Composer)
        assert composer.text == "@reviewer "
        assert composer.suggestion == ""

        await pilot.press(*"inspect", "space", *"app.py", "enter")
        await _wait_for_worker_group(app, pilot, group="run")
        await _wait_until(
            lambda: app._projection is not None and app._projection.activity.status == "completed",
            pilot,
            description="skill-activated run",
        )

        assert app._controller.session_id is not None
        snapshot = application.runtime.store.load_session(app._controller.session_id)
        message = next(item for item in snapshot.messages if item.skill_activations)
        assert message.skill_activations[0].name == "reviewer"
        assert message.text_content == "inspect app.py"


@pytest.mark.asyncio
async def test_skill_selection_creates_an_element_without_submitting(
    tmp_path: Path,
) -> None:
    skill_root = tmp_path / "skills" / "imagegen"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        """---
name: imagegen
description: Generate or edit raster images.
---
Create the requested image.
""",
        encoding="utf-8",
    )
    skill = SkillLoader(skill_root.parent).load_metadata()[0]
    app = OuroTuiApp(application=_application(tmp_path, skills=(skill,)))

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.press("@", *"skill", "enter")
        await pilot.pause()

        await pilot.press("enter")
        await pilot.pause()

        composer = app.query_one(Composer)
        assert composer.text == "@imagegen "
        assert composer.input_content.skills[0].name == "imagegen"
        assert composer.suggestion == ""
        assert app.query_one(CommandPopup).display is False
        assert app._controller.session_id is None
