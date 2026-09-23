from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from rich.text import Text
from textual import events
from textual.widgets import Input, OptionList, Static

from ouro.application.configuration import ConfigurationFacade
from ouro.application.models import AvailableModel
from ouro.application.runtime import RuntimeService
from ouro.application.runtime_environment import RuntimeEnvironment
from ouro.automation.models import TaskConfiguration, TaskCounts, TaskSchedule
from ouro.automation.schedule import next_occurrence
from ouro.core.adapter import RuntimeAdapter
from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.core.reasoning import ReasoningCapability, ReasoningRequest
from ouro.local_application import LocalApplication
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import ToolRegistry
from ouro.tui.app import OuroTuiApp
from ouro.tui.commands import resolve_command
from ouro.tui.controller import TuiSystemStatus
from ouro.tui.screens import ChoiceScreen, ConfirmationScreen
from ouro.tui.task_screens import TaskConfigurationScreen
from ouro.tui.theme import theme_for
from ouro.tui.widgets import Composer, ConversationView, QueuedInputPanel, WelcomePanel
from tests.tui.helpers import run_ready_app
from tests.tui.test_app import _application, _wait_for_screen


async def choose(pilot, app, value):
    app.screen.query_one("#choice-filter", Input).value = value
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()


@pytest.mark.parametrize(
    ("timezone", "expected"),
    [
        ("Asia/Shanghai", "2026-09-05T19:50:00+00:00"),
        ("America/New_York", "2026-09-06T07:50:00+00:00"),
    ],
)
def test_new_task_uses_system_wall_time_and_edit_preserves_explicit_zone(
    tmp_path,
    monkeypatch,
    timezone,
    expected,
):
    monkeypatch.setattr("ouro.tui.task_screens.get_localzone_name", lambda: timezone)
    model = AvailableModel(
        key="test/plain",
        provider_id="test",
        endpoint_id="plain",
        model_id="plain",
    )
    screen = TaskConfigurationScreen(
        models=(model,), workspace=str(tmp_path), default_model=model.key
    )
    screen._values.update(
        {
            "description": "Run at local 03:50",
            "starts": "2026-09-06 03:44:28",
            "at": "03:50",
        }
    )
    config = screen.draft()
    assert config.schedule.timezone == timezone
    assert next_occurrence(config.schedule, config.schedule.starts_at) == datetime.fromisoformat(
        expected
    )
    explicit = config.model_copy(
        update={
            "schedule": config.schedule.model_copy(update={"timezone": "UTC"}),
        }
    )
    edited = TaskConfigurationScreen(models=(model,), workspace=None, configuration=explicit)
    assert edited.draft().schedule.timezone == "UTC"
    screen._values["model"] = "default"
    with pytest.raises(ValueError, match="System default endpoint is not configured"):
        screen.draft()
    screen._system_default_model = model
    assert screen.draft().model == "default"
    with_default = TaskConfigurationScreen(
        models=(model,),
        workspace=str(tmp_path),
        default_model="default",
        system_default_model=model,
    )
    assert with_default._values["model"] == "default"


async def test_new_task_preselects_configured_system_default_model(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    ConfigurationFacade(tmp_path).add_provider(
        display_name="Primary", provider_type="openai", make_default=True
    )
    app = OuroTuiApp(application=LocalApplication.build(tmp_path))
    async with run_ready_app(app, size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await choose(pilot, app, "+ task")
        await _wait_for_screen(app, TaskConfigurationScreen, title="CREATE TASK")
        assert app.screen._values["model"] == "default"


async def test_task_menu_saves_multiline_instructions_and_pauses_schedule(tmp_path):
    application = _application(tmp_path)
    app = OuroTuiApp(application=application)
    async with run_ready_app(app, size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await choose(pilot, app, "+ task")
        await _wait_for_screen(app, TaskConfigurationScreen, title="CREATE TASK")
        form = app.screen
        assert next(row for row in form._choices if row.value == "title").label == "Title"
        save = next(row for row in form._choices if row.value == "save")
        assert (save.label, save.group) == ("Save", "ACTIONS")
        await choose(pilot, app, "Instruction")
        assert app.screen is form
        options = form.query_one(OptionList)
        prompt = options.get_option("description").prompt
        assert isinstance(prompt, Text) and len(prompt.plain.split("\n")) == 3
        description = "Summarize the repository.\nInclude tests and open questions."
        form.post_message(events.Paste("Summarize the repository."))
        await pilot.pause()
        await pilot.press("shift+enter")
        form.post_message(events.Paste("Include tests and open questions."))
        await pilot.pause()
        assert form._edit_text == description
        await pilot.press("enter")
        assert form._editing_value is None
        assert form._values["description"] == description
        row = next(row for row in form._choices if row.value == "description")
        assert row.trailing == "Summarize the repository."
        assert len(app.console.render_lines(options.get_option("description").prompt)) == 1
        await choose(pilot, app, "Instruction")
        await pilot.press("ctrl+u")
        form.post_message(events.Paste("Cancelled\r\npaste\nthird\nfourth"))
        await pilot.pause()
        assert form._edit_text == "Cancelled\npaste\nthird\nfourth"
        prompt = options.get_option("description").prompt
        assert isinstance(prompt, Text) and len(prompt.plain.split("\n")) == 3
        await pilot.press("up", "home", "X", "escape")
        assert form._values["description"] == description
        initial_timezone = form._values["timezone"]
        await choose(pilot, app, "Time zone")
        await _wait_for_screen(app, ChoiceScreen, title="TASK TIME ZONE")
        options = app.screen.query_one(OptionList)
        assert options.get_option_at_index(options.highlighted).id == initial_timezone
        assert [choice.value for choice in app.screen._choices if choice.marker] == [
            initial_timezone
        ]
        await pilot.press("escape")
        await _wait_for_screen(app, TaskConfigurationScreen, title="CREATE TASK")
        assert form._values["timezone"] == initial_timezone
        await choose(pilot, app, "Time zone")
        await _wait_for_screen(app, ChoiceScreen, title="TASK TIME ZONE")
        await choose(pilot, app, "Shanghai")
        await _wait_for_screen(app, TaskConfigurationScreen, title="CREATE TASK")
        await choose(pilot, app, "Save")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        tasks = await application.tasks.list()
        assert len(tasks) == 1
        task = tasks[0]
        assert task.configuration.description == description
        assert task.configuration.approval_mode == "afk"
        assert task.configuration.session_mode == "new"
        assert task.configuration.schedule.timezone == "Asia/Shanghai"
        await choose(pilot, app, "Manage tasks")
        await _wait_for_screen(app, ChoiceScreen, title="Tasks")
        assert [row.value for row in app.screen._choices] == [task.task_id]
        await choose(pilot, app, "Summarize")
        await choose(pilot, app, "Pause")
        assert not (await application.tasks.get(task.task_id)).enabled
        await app._sync_task_status()
        assert app._system_status.task_counts.paused == 1
        await pilot.press("escape", "escape", "escape")
        rendered = str(app.query_one("#welcome-task-counts", Static).content)
        assert rendered == "0/0 completed"


async def test_task_delete_and_status_actions_stay_on_shared_menu(tmp_path):
    application = _application(tmp_path)
    app = OuroTuiApp(application=application)
    async with run_ready_app(app, size=(100, 34)) as pilot:
        model = (await app._controller.models())[0]
        task = await application.tasks.create(
            TaskConfiguration(
                description="Delete this scheduled task",
                workspace=str(tmp_path),
                model=model.key,
                schedule=TaskSchedule(
                    frequency="daily", starts_at=datetime.now(UTC) + timedelta(days=1)
                ),
            )
        )
        await pilot.press("ctrl+p")
        await choose(pilot, app, "Update status")
        assert app._system_status.task_counts.waiting == 1
        await choose(pilot, app, "Manage tasks")
        await _wait_for_screen(app, ChoiceScreen, title="Tasks")
        await choose(pilot, app, "Delete this scheduled task")
        rows = app.screen._choices
        assert [(row.label, row.group) for row in rows] == [
            ("Waiting", "STATUS"),
            ("History", "STATUS"),
            ("Edit", "CONFIGURATION"),
            ("Pause", "CONFIGURATION"),
            ("Delete", "CONFIGURATION"),
            ("Interrupt", "ACTIONS"),
            ("Refresh", "ACTIONS"),
        ]
        assert next(row for row in rows if row.value == "interrupt").disabled
        delete_prompt = app.screen.query_one(OptionList).get_option("delete").prompt
        assert isinstance(delete_prompt, Text)
        color = delete_prompt.get_style_at_offset(app.console, 0).color
        assert color is not None and color.triplet.hex == theme_for(app).danger
        await choose(pilot, app, "Delete")
        await _wait_for_screen(app, ConfirmationScreen, title="CONFIRM DELETE TASK")
        await pilot.press("escape")
        assert await application.tasks.get(task.task_id)
        await choose(pilot, app, "Delete")
        await _wait_for_screen(app, ConfirmationScreen, title="CONFIRM DELETE TASK")
        await choose(pilot, app, "Delete")
        await _wait_for_screen(app, ChoiceScreen, title="Tasks")
        assert not await application.tasks.list()
        assert app._system_status.task_counts.waiting == 0
        assert app.screen._choices[0].label == "No tasks yet"


async def test_task_list_lowlight_keeps_inactive_tasks_selectable(tmp_path, monkeypatch):
    application = _application(tmp_path)
    app = OuroTuiApp(application=application)
    async with run_ready_app(app, size=(100, 34)) as pilot:
        task = await application.tasks.create(
            TaskConfiguration(
                description="Paused task",
                workspace=str(tmp_path),
                model=(await app._controller.models())[0].key,
                schedule=TaskSchedule(
                    frequency="daily", starts_at=datetime.now(UTC) + timedelta(days=1)
                ),
            )
        )
        await application.tasks.set_enabled(task.task_id, False, expected_revision=task.revision)
        paused = await application.tasks.get(task.task_id)
        records = [paused] + [
            task.model_copy(update={"task_id": status, "status": status})
            for status in ("waiting", "running", "completed", "failed", "interrupted")
        ]
        monkeypatch.setattr(app._controller, "tasks", AsyncMock(return_value=records))
        await pilot.press("ctrl+t")
        await _wait_for_screen(app, ChoiceScreen, title="Tasks")
        for row, record in zip(app.screen._choices, records, strict=True):
            assert not row.disabled
            assert row.accent == (
                None if record.status in {"waiting", "running"} else theme_for(app).text_lowlight
            )
        await choose(pilot, app, "Paused task")
        assert next(row for row in app.screen._choices if row.value == "toggle").label == "Enable"
        await choose(pilot, app, "Enable")
        assert (await application.tasks.get(task.task_id)).enabled
        await choose(pilot, app, "Refresh")
        assert next(row for row in app.screen._choices if row.value == "toggle").label == "Pause"


async def test_welcome_task_counts_show_only_actionable_activity_in_semantic_colors(tmp_path):
    app = OuroTuiApp(application=_application(tmp_path))
    async with run_ready_app(app, size=(140, 34)) as pilot:
        welcome = app.query_one(WelcomePanel)
        counts = TaskCounts(
            waiting=2,
            running=1,
            paused=4,
            completed=5,
            failed=3,
            interrupted=2,
            awaiting_approval=1,
            completed_executions=7,
            failed_executions=3,
        )
        welcome.set_system_status(TuiSystemStatus(task_counts=counts))
        await pilot.pause()
        content = app.query_one("#welcome-task-counts", Static).content
        assert isinstance(content, Text)
        assert content.plain == "7/10 completed • 2 waiting • 1 running • 1 approval"
        theme = theme_for(welcome)
        for label, color in (
            ("waiting", theme.text_lowlight),
            ("running", theme.accent),
            ("approval", theme.warning),
            ("•", theme.text_faint),
        ):
            style = content.get_style_at_offset(app.console, content.plain.index(label))
            assert style.color is not None and style.color.triplet.hex == color
        welcome.set_system_status(
            TuiSystemStatus(task_counts=TaskCounts(completed_executions=8, failed_executions=3))
        )
        assert app.query_one("#welcome-task-counts", Static).content.plain == "8/11 completed"


@pytest.mark.parametrize(
    ("field", "title", "current"),
    [
        ("Schedule", "TASK FREQUENCY", "daily"),
        ("Session creation", "TASK SESSION", "new"),
        ("Approval", "TOOL APPROVAL", "afk"),
        ("Repeat unit", "TASK UNIT", "days"),
    ],
)
async def test_task_picker_marks_and_focuses_selected_option(tmp_path, field, title, current):
    application = _application(tmp_path)
    app = OuroTuiApp(application=application)
    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.press("ctrl+p")
        await choose(pilot, app, "+ task")
        await _wait_for_screen(app, TaskConfigurationScreen, title="CREATE TASK")
        if field == "Repeat unit":
            await choose(pilot, app, "Schedule")
            await _wait_for_screen(app, ChoiceScreen, title="TASK FREQUENCY")
            await choose(pilot, app, "Custom")
            await _wait_for_screen(app, TaskConfigurationScreen, title="CREATE TASK")
        await choose(pilot, app, field)
        await _wait_for_screen(app, ChoiceScreen, title=title)
        screen = app.screen
        selected = [choice for choice in screen._choices if choice.marker]
        assert len(selected) == 1
        assert selected[0].value == current
        assert selected[0].marker == theme_for(screen).selected_choice_marker
        options = screen.query_one(OptionList)
        assert options.get_option_at_index(options.highlighted).id == current
        if field == "Approval":
            command, _ = resolve_command("/approval")
            assert [
                (choice.value, choice.label, choice.trailing) for choice in screen._choices
            ] == [
                (argument.value, argument.display_label, argument.description)
                for argument in command.argument_options
            ]


async def test_new_task_uses_symbolic_default_independently_of_session_model(tmp_path):
    configuration = ConfigurationFacade(tmp_path)
    routes = {}
    for provider_name in ("alpha", "zeta"):
        created = configuration.add_provider(
            display_name=provider_name.title(),
            provider_type="custom",
            protocol="openai-chat-completions",
            base_url=f"https://{provider_name}.example.invalid/v1",
            api_key="test-key",
        )
        provider_id = created.changed_provider_id
        assert provider_id is not None
        endpoints = configuration.add_endpoint(
            provider_id, display_name="Chat", model=f"{provider_name}-model"
        )
        endpoint_id = endpoints.changed_endpoint_id
        assert endpoint_id is not None
        routes[provider_name] = f"{provider_id}/{endpoint_id}"
    zeta_provider, zeta_endpoint = routes["zeta"].split("/", 1)
    configuration.use_endpoint(zeta_provider, zeta_endpoint)
    application = LocalApplication.build(tmp_path)
    app = OuroTuiApp(application=application)
    async with run_ready_app(app, size=(100, 34)) as pilot:
        assert {model.key for model in await app._controller.models()} == set(routes.values())
        await app._controller.select_model(routes["alpha"])
        await pilot.press("ctrl+p")
        await choose(pilot, app, "+ task")
        await _wait_for_screen(app, TaskConfigurationScreen, title="CREATE TASK")
        assert app.screen._values["model"] == "default"
        assert app.screen._system_default_model.key == routes["zeta"]
        model_row = next(choice for choice in app.screen._choices if choice.value == "model")
        assert model_row.trailing == "Default model"
        await pilot.press("escape")
        alpha_provider, alpha_endpoint = routes["alpha"].split("/", 1)
        configuration.use_endpoint(alpha_provider, alpha_endpoint)
        await choose(pilot, app, "+ task")
        await _wait_for_screen(app, TaskConfigurationScreen, title="CREATE TASK")
        assert app.screen._values["model"] == "default"
        assert app.screen._system_default_model.key == routes["alpha"]
        model_row = next(choice for choice in app.screen._choices if choice.value == "model")
        assert model_row.trailing == "Default model"
        await pilot.press("escape")


async def test_ctrl_t_uses_shared_task_navigation_without_stacking_or_replacing_editor(tmp_path):
    app = OuroTuiApp(application=_application(tmp_path))
    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.press("ctrl+t")
        await _wait_for_screen(app, ChoiceScreen, title="Tasks")
        menu = app.screen
        assert menu.navigation_id == "tasks"
        await pilot.press("ctrl+t", "ctrl+t")
        assert app.screen is menu and len(app.screen_stack) == 2
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        assert app.screen is menu
        await pilot.press("ctrl+t")
        await _wait_for_screen(app, ChoiceScreen, title="Tasks")
        assert app.screen is menu
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        await choose(pilot, app, "+ task")
        await _wait_for_screen(app, TaskConfigurationScreen, title="CREATE TASK")
        form = app.screen
        await choose(pilot, app, "Instruction")
        await pilot.press("a", "ctrl+t")
        assert app.screen is form and form._edit_text == "a"
        await pilot.press("escape", "escape")


@pytest.mark.parametrize("task_backend", ["files", "sqlite"])
async def test_tui_composes_selected_task_backend_without_changing_file_sessions(
    tmp_path, task_backend
):
    app = OuroTuiApp(home=tmp_path, task_backend=task_backend, session_backend="files")
    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.press("ctrl+t")
        await _wait_for_screen(app, ChoiceScreen, title="Tasks")
        assert app.screen._choices[0].label == "No tasks yet"
        assert (tmp_path / "tasks.sqlite3").exists() == (task_backend == "sqlite")
        assert (tmp_path / "tasks" / "layout.json").exists() == (task_backend == "files")
        assert not (tmp_path / "sessions.sqlite3").exists()


async def test_tab_switches_user_and_task_domains_even_when_one_is_empty(tmp_path):
    application = _application(tmp_path)
    task_session = application.runtime.sessions.create(title="Background", namespace="task").session
    app = OuroTuiApp(application=application)
    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+s")
        await _wait_for_screen(app, ChoiceScreen, title="SESSION • USER")
        assert not app.screen._choices
        await pilot.press("tab")
        await _wait_for_screen(app, ChoiceScreen, title="SESSION • TASK")
        assert [choice.value for choice in app.screen._choices] == [task_session.session_id]
        await pilot.press("tab")
        await _wait_for_screen(app, ChoiceScreen, title="SESSION • USER")
        assert not app.screen._choices


class StreamingTaskProvider:
    provider_id = "faux"

    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.prompts: list[str] = []

    async def stream(self, *, model, messages, system_prompt="", tools=None, request_context=None):
        self.prompts.append(messages[-1].text_content)
        yield ProviderStreamEvent(type="text_delta", text="Before attachment. ")
        self.started.set()
        await self.release.wait()
        yield ProviderStreamEvent(type="text_delta", text="After attachment.")
        yield ProviderStreamEvent(type="done")


def streaming_application(tmp_path, provider, *, backend="files", model=None):
    store = (
        LocalSessionStore(tmp_path)
        if backend == "files"
        else SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    )
    store.initialize()
    runtime = RuntimeService(
        store=store,
        tools=ToolRegistry(),
        default_workspace=tmp_path,
        adapter=RuntimeAdapter(
            provider, model or ModelMetadata(model_id="faux", provider_id="faux")
        ),
        environment=RuntimeEnvironment.local(
            tmp_path, store.sessions_dir if backend == "files" else None
        ),
    )
    return LocalApplication.from_runtime(runtime)


@pytest.mark.parametrize("entry", ["sessions", "history"])
async def test_background_run_stays_silent_and_attaching_restores_live_stream(tmp_path, entry):
    provider = StreamingTaskProvider()
    application = streaming_application(tmp_path, provider)
    runtime = application.runtime
    app = OuroTuiApp(application=application)
    async with run_ready_app(app, size=(120, 40)) as pilot:
        await pilot.pause()
        now = datetime.now(UTC)
        task = await application.tasks.create(
            TaskConfiguration(
                title="Background stream",
                description="Describe the workspace",
                workspace=str(tmp_path),
                model="faux/faux",
                schedule=TaskSchedule(
                    frequency="custom", unit="minutes", starts_at=now + timedelta(seconds=0.1)
                ),
            )
        )
        async with asyncio.timeout(8):
            await provider.started.wait()
        await pilot.pause()
        assert app._controller.session_id is None
        assert app.query_one(ConversationView).showing_welcome
        current = await application.tasks.get(task.task_id)
        if entry == "sessions":
            await pilot.press("ctrl+s", "tab")
            await _wait_for_screen(app, ChoiceScreen, title="SESSION • TASK")
            await pilot.press("enter")
        else:
            await pilot.press("ctrl+t")
            await _wait_for_screen(app, ChoiceScreen, title="Tasks")
            await choose(pilot, app, "Background stream")
            await choose(pilot, app, "History")
            await pilot.press("enter")
        async with asyncio.timeout(5):
            while app._projection is None or app._projection.session_id != current.session_id:
                await pilot.pause()
        assert not app._visible_read_only
        async with asyncio.timeout(5):
            while "Before" not in app.export_screenshot():
                await pilot.pause()
        app.query_one(Composer).load_text("Follow up from the task session")
        await pilot.press("enter")
        async with asyncio.timeout(5):
            while not app.query_one(QueuedInputPanel).display:
                await pilot.pause()
        provider.release.set()
        async with asyncio.timeout(8):
            while app._projection.activity.status != "completed":
                await pilot.pause()
            while not all(text in app.export_screenshot() for text in ("Before", "After")):
                await pilot.pause()
            while app.query_one(QueuedInputPanel).display:
                await pilot.pause()
        assert app._controller.session_id == current.session_id
        snapshot = runtime.store.load_session(current.session_id)
        assert any(
            message.text_content == "Follow up from the task session"
            for message in snapshot.messages
        )
        assert len(await application.tasks.executions(task.task_id)) == 1


@pytest.mark.parametrize("entry", ["session", "menu"])
async def test_tui_cancels_a_queued_task_before_it_can_start(tmp_path, entry):
    provider = StreamingTaskProvider()
    application = streaming_application(tmp_path, provider)
    application.supervisor.max_concurrency = 1
    app = OuroTuiApp(application=application)
    async with run_ready_app(app, size=(100, 34)) as pilot:
        occupying = await application.supervisor.submit("Occupy the worker")
        async with asyncio.timeout(5):
            await provider.started.wait()
        task = await application.tasks.create(
            TaskConfiguration(
                description="Task must never reach the model",
                workspace=str(tmp_path),
                model="faux/faux",
                schedule=TaskSchedule(
                    frequency="custom",
                    unit="minutes",
                    starts_at=datetime.now(UTC) + timedelta(seconds=0.1),
                ),
            )
        )
        async with asyncio.timeout(5):
            while not (await application.tasks.get(task.task_id)).active_execution_id:
                await pilot.pause()
        task = await application.tasks.get(task.task_id)
        async with asyncio.timeout(5):
            while application.supervisor.status(task.session_id).status != "queued":
                await pilot.pause()
        if entry == "session":
            await app._open_session(task.session_id)
            assert app._projection.activity.status == "queued", app._projection
            app.action_abort()
        else:
            await pilot.press("ctrl+t")
            await choose(pilot, app, "Task must never reach the model")
            rows = {row.value: row for row in app.screen._choices}
            assert rows["delete"].disabled
            assert not rows["interrupt"].disabled
            await choose(pilot, app, "Interrupt")
        async with asyncio.timeout(5):
            while (await application.tasks.get(task.task_id)).active_execution_id is not None:
                await pilot.pause()
        assert (await application.tasks.get(task.task_id)).status == "waiting"
        assert (await application.tasks.executions(task.task_id))[0].status == "interrupted"
        provider.release.set()
        await application.supervisor.wait(occupying)
        application.supervisor.release_handle(occupying)
        assert provider.prompts == ["Occupy the worker"]


async def test_task_model_change_resets_and_validates_thinking(tmp_path):
    application = _application(tmp_path)
    plain = AvailableModel(
        key="test/plain",
        provider_id="test",
        endpoint_id="plain",
        model_id="plain",
    )
    thinking = plain.model_copy(
        update={
            "key": "test/thinking",
            "endpoint_id": "thinking",
            "model_id": "thinking",
            "reasoning": ReasoningCapability(
                activation="always_on",
                supported_efforts=("low", "high"),
                default_effort="low",
                default=ReasoningRequest(mode="enabled", effort="low"),
            ),
        }
    )
    start = datetime(2026, 9, 7, 9, 12, 34, 123456, tzinfo=UTC)
    config = TaskConfiguration(
        description="Do the scheduled work",
        workspace=str(tmp_path),
        model=thinking.key,
        reasoning=ReasoningRequest(mode="enabled", effort="high"),
        schedule=TaskSchedule(frequency="daily", starts_at=start),
    )
    screen = TaskConfigurationScreen(models=(thinking, plain), workspace=None, configuration=config)
    assert screen.draft() == config
    assert "high" in screen.reasoning_options()
    assert "disabled" not in screen.reasoning_options()
    app = OuroTuiApp(application=application)
    async with run_ready_app(app, size=(100, 34)) as pilot:
        app.push_screen(screen)
        await pilot.pause()
        await choose(pilot, app, "Model thinking")
        await _wait_for_screen(app, ChoiceScreen, title="MODEL THINKING")
        assert [choice.value for choice in app.screen._choices] == ["low", "high"]
        assert [choice.value for choice in app.screen._choices if choice.marker] == ["high"]
        await choose(pilot, app, "Low")
        await _wait_for_screen(app, TaskConfigurationScreen, title="EDIT TASK")
        assert screen.draft().reasoning.effort == "low"
        screen.query_one("#choice-filter", Input).value = ""
        await pilot.pause()
        options = screen.query_one(OptionList)
        options.highlighted = options.get_option_index("model")
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="TASK MODEL")
        assert [choice.value for choice in app.screen._choices if choice.marker] == [thinking.key]
        await choose(pilot, app, "plain")
        await _wait_for_screen(app, TaskConfigurationScreen, title="EDIT TASK")
        assert screen.draft().reasoning == ReasoningRequest()
        assert "high" not in screen.reasoning_options()
        assert next(row for row in screen._choices if row.value == "reasoning").disabled
        screen._reasoning = ReasoningRequest(mode="enabled", effort="high")
        with pytest.raises(ValueError):
            screen.draft()
        assert next(row for row in screen._task_choices() if row.value == "save").disabled
