from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from rich.console import Console
from rich.segment import Segment
from rich.text import Text
from textual import events
from textual.color import Color
from textual.containers import Vertical
from textual.content import Content
from textual.strip import Strip
from textual.widgets import Input, OptionList, Static, TextArea

import ouro.tui.app as tui_app_module
import ouro.tui.conversation_history as conversation_history
import ouro.tui.widgets as tui_widgets
from ouro import __version__
from ouro.application.agent_models import AgentToolOptionView, AgentTreeNode
from ouro.application.configuration import ConfigurationFacade
from ouro.application.configuration_models import SkillView
from ouro.application.event_models import project_runtime_event
from ouro.application.models import AvailableModel
from ouro.application.plugin_models import (
    PluginCommandArgumentView,
    PluginCommandExecutionView,
    PluginCommandView,
)
from ouro.application.queued_inputs import QueuedUserInputView
from ouro.application.runtime import RuntimeService
from ouro.application.runtime_environment import RuntimeEnvironment
from ouro.application.session_models import SessionContextStatus
from ouro.application.supervisor_models import RuntimeActivitySnapshot, RuntimeToolCallActivity
from ouro.builtin_plugins.plan.interaction import PlanConfirmationRequest
from ouro.config import reasoning_preset
from ouro.core.adapter import RuntimeAdapter
from ouro.core.agents import (
    AgentCatalogEntry,
    AgentCatalogSnapshot,
    AgentDefinition,
    DelegationRecord,
    ToolSelection,
)
from ouro.core.compaction import CompactionRecord
from ouro.core.context import ContextMetrics
from ouro.core.errors import PersistenceError
from ouro.core.events import EventRecord
from ouro.core.messages import (
    MessageRecord,
    ModelMessage,
    ModelResponseRecord,
    TextContent,
    ToolExecutionTiming,
    ToolResultReference,
)
from ouro.core.provider import (
    ModelMetadata,
    ProviderCheckResult,
    ProviderRetryNotice,
    ProviderStreamEvent,
)
from ouro.core.reasoning import ReasoningCapability, ReasoningRequest
from ouro.core.results import RunResult
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tools import ToolCallRequest, ToolSource
from ouro.local_application import LocalApplication
from ouro.mcp import (
    MCPManager,
    MCPRemoteTool,
    MCPRemoteToolResult,
    MCPRuntimeResource,
    MCPServerCapabilities,
    MCPServerConfig,
    MCPServerConfigUpdate,
    MCPServerInfo,
)
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore
from ouro.skills import SkillLoader, SkillMetadata
from ouro.tools import (
    Tool,
    ToolDefinition,
    ToolInputRequest,
    ToolRegistry,
    ToolResult,
    builtin_tools,
)
from ouro.tui.app import OuroTuiApp, _active_stream_tail, _event_record, _session_choices
from ouro.tui.commands import TUI_COMMANDS, TuiCommand, TuiCommandArgument
from ouro.tui.configuration_screens import (
    AgentDefinitionConfigurationScreen,
    AgentDispatchPolicyScreen,
    CompactionConfigurationScreen,
    MCPConfigurationScreen,
    PluginInstallScreen,
    SkillInstallScreen,
    ToolExecutionConfigurationScreen,
)
from ouro.tui.configuration_workflows import _mcp_management_choices, _skill_choice
from ouro.tui.controller import (
    LoadedSession,
    McpServerStatus,
    TuiBootstrap,
    TuiEndpointConfiguration,
    TuiPluginCommandExecution,
    TuiPluginInstallConfiguration,
    TuiProviderConfiguration,
    TuiSessionControlStatus,
)
from ouro.tui.conversation_history import (
    AssistantTailLines,
    CompactionPresentation,
    ReasoningLines,
    ToolNavigationRequested,
    _ReasoningContent,
)
from ouro.tui.input_drafts import TuiInputDrafts, TuiInputDraftStore
from ouro.tui.preferences import TuiPreferences, TuiPreferencesStore
from ouro.tui.provider_screens import (
    EndpointConfigurationScreen,
    ProviderConfigurationScreen,
    ProviderSetupProfile,
)
from ouro.tui.screens import (
    Choice,
    ChoiceScreen,
    ConfirmationScreen,
    EditableChoiceScreen,
    HelpScreen,
    MultiChoiceScreen,
)
from ouro.tui.state import (
    PendingInteraction,
    TuiSessionProjection,
    project_run_presentation,
)
from ouro.tui.subagent_navigation import SubagentNavigationPanel
from ouro.tui.theme import DEFAULT_TUI_THEME, TUI_THEMES
from ouro.tui.widgets import (
    ActivityIndicator,
    AskUserPanel,
    AssistantTurn,
    BrandHeader,
    CommandPopup,
    Composer,
    ConversationHistory,
    ConversationView,
    InteractionBar,
    PlanConfirmationPanel,
    QueuedInputPanel,
    StatusLine,
    WelcomePanel,
    _brand_header_content,
    _tool_activities_from_messages,
)


async def _wait_for_screen(
    app: OuroTuiApp,
    screen_type: type[ChoiceScreen],
    *,
    title: str,
    timeout: float = 2.0,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        screen = app.screen
        if (
            isinstance(screen, screen_type)
            and screen._title == title
            and len(screen.query("#choice-filter")) == 1
        ):
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"Timed out waiting for {title}")
        await asyncio.sleep(0.01)


def test_brand_header_renders_session_detail_as_lowlight_metadata() -> None:
    detail = "Inspect the event pipeline"
    content = _brand_header_content(
        "Scout",
        detail,
        None,
        width=80,
    )

    assert f"Scout{DEFAULT_TUI_THEME.inline_separator}{detail}" in content.plain
    title_style = content.get_style_at_offset(Console(), content.plain.index("Scout"))
    assert not title_style.bold
    style = content.get_style_at_offset(Console(), content.plain.index(detail))
    assert style.color is not None
    assert style.color.triplet is not None
    assert style.color.triplet.hex == DEFAULT_TUI_THEME.text_lowlight


async def _wait_for_active_session(
    app: OuroTuiApp,
    pilot,
    session_id: str,
    *,
    timeout: float = 5.0,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while app._controller.session_id != session_id or app.query_one(Composer).disabled:
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"Timed out waiting for active Session {session_id}")
        await asyncio.sleep(0.01)
    await pilot._wait_for_screen()


async def _wait_until(
    predicate: Callable[[], bool],
    pilot,
    *,
    description: str,
    timeout: float = 5.0,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"Timed out waiting for {description}")
        await pilot.pause()


async def _wait_for_worker_group(
    app: OuroTuiApp,
    pilot,
    *,
    group: str,
    timeout: float = 5.0,
) -> None:
    workers = tuple(worker for worker in app.workers if worker.group == group)
    if workers:
        await asyncio.wait_for(app.workers.wait_for_complete(workers), timeout=timeout)
    await pilot._wait_for_screen()


async def _wait_for_welcome_render(
    app: OuroTuiApp,
    pilot,
    *,
    contains: str,
    timeout: float = 2.0,
) -> WelcomePanel:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        matches = list(app.query(WelcomePanel))
        welcome = matches[0] if matches else None
        if (
            welcome is not None
            and welcome.has_class("welcome-page")
            and welcome._logo.region.width > 0
            and contains in _welcome_plain_text(welcome)
        ):
            await pilot._wait_for_screen()
            return welcome
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"Timed out waiting for welcome content: {contains}")
        await asyncio.sleep(0.01)


def _welcome_plain_text(welcome: WelcomePanel) -> str:
    return "\n".join(getattr(widget.render(), "plain", "") for widget in welcome.query(Static))


def test_session_choices_group_by_local_last_activity_date(tmp_path: Path) -> None:
    application = _application(tmp_path)
    application.runtime.sessions.create(session_id="ses_template", title="Template")
    template = application.runtime.sessions.catalog().entries[0]
    today = date(2026, 8, 9)
    local_noon = datetime(2026, 8, 9, 12, tzinfo=UTC)
    ages = (0, 1, 2, 6, 7, 29, 30, 45)
    sessions = tuple(
        template.model_copy(
            update={
                "session_id": f"ses_{age}",
                "title": f"Session {age}",
                "last_activity_at": local_noon - timedelta(days=age),
                "archived": age in {2, 30},
            }
        )
        for age in ages
    )

    choices = _session_choices(sessions, today=today)

    assert [choice.group for choice in choices] == [
        "Today",
        "Yesterday",
        "Previous 7 days",
        "Previous 30 days",
        "Previous 30 days",
        "June 2026",
        "Archived",
        "Archived",
    ]
    assert [choice.value for choice in choices[-2:]] == ["ses_2", "ses_30"]


def test_session_choices_mark_only_the_selected_session(tmp_path: Path) -> None:
    application = _application(tmp_path)
    first = application.runtime.sessions.create(session_id="ses_first", title="First").session
    second = application.runtime.sessions.create(session_id="ses_second", title="Second").session
    sessions = application.runtime.sessions.catalog().entries

    choices = {
        choice.value: choice
        for choice in _session_choices(
            sessions,
            selected_session_id=second.session_id,
        )
    }

    assert choices[first.session_id].marker == ""
    assert choices[second.session_id].marker == DEFAULT_TUI_THEME.selected_choice_marker


def test_session_choices_use_semantic_health_tones(tmp_path: Path) -> None:
    application = _application(tmp_path)
    template = application.runtime.sessions.create(title="Template").session
    entry = application.runtime.sessions.catalog().entries[0]
    sessions = (
        entry.model_copy(update={"session_id": template.session_id, "health": "ok"}),
        entry.model_copy(update={"session_id": "ses_warning", "health": "recoverable"}),
        entry.model_copy(update={"session_id": "ses_failure", "health": "corrupt"}),
        entry.model_copy(
            update={
                "session_id": "ses_index_error",
                "session_kind": None,
                "health": None,
                "catalog_error": "session_kind is missing",
            }
        ),
    )

    projected = _session_choices(sessions)
    choices = {choice.value: choice for choice in projected}

    assert choices[template.session_id].accent is None
    assert choices["ses_warning"].accent == DEFAULT_TUI_THEME.warning
    assert choices["ses_failure"].accent == DEFAULT_TUI_THEME.failure
    assert choices["ses_index_error"].accent == DEFAULT_TUI_THEME.failure
    assert choices["ses_index_error"].trailing == "index error"
    assert choices["ses_index_error"].group == "Index issues"
    assert projected[-1].value == "ses_index_error"


def test_choice_description_can_use_failure_semantics() -> None:
    option = ChoiceScreen._options(
        (
            Choice(
                "close",
                "Close",
                description="Could not load Session",
                description_accent=DEFAULT_TUI_THEME.failure,
            ),
        ),
        group_accent="#ffffff",
    )[0]
    prompt = option.prompt

    assert isinstance(prompt, Text)
    description_style = prompt.get_style_at_offset(
        Console(),
        prompt.plain.index("Could not load Session"),
    )
    assert description_style.color is not None
    assert description_style.color.get_truecolor().hex == DEFAULT_TUI_THEME.failure


def test_choice_screen_projects_group_headers_as_disabled_options() -> None:
    options = ChoiceScreen._options(
        (
            Choice("today-1", "First", group="Today"),
            Choice("today-2", "Second", group="Today"),
            Choice("yesterday", "Third", group="Yesterday"),
        ),
        group_accent="#ffffff",
    )

    assert [option.id for option in options] == [
        None,
        "today-1",
        "today-2",
        None,
        None,
        "yesterday",
    ]
    assert options[0].disabled is True
    assert options[0].prompt.plain == "Today"
    assert options[3].disabled is True
    assert options[3].prompt.plain == " "
    assert options[4].disabled is True
    assert options[4].prompt.plain == "Yesterday"


def test_choice_screen_projects_group_status_on_the_group_header() -> None:
    options = ChoiceScreen._options(
        (
            Choice(
                "endpoint",
                "Endpoint",
                group="Disabled provider",
                group_trailing="unavailable",
                disabled=True,
            ),
        ),
        group_accent="#ffffff",
    )

    console = Console(width=40, record=True)
    console.print(options[0].prompt)
    rendered = console.export_text().splitlines()[0]
    assert rendered.startswith("Disabled provider")
    assert rendered.endswith("unavailable")
    assert ChoiceScreen._is_group_header(options[0]) is True
    assert ChoiceScreen._is_group_header(options[1]) is False


def test_group_shortcuts_are_disabled_for_ungrouped_choice_screens() -> None:
    screen = ChoiceScreen(
        title="MODELS",
        placeholder="Filter models…",
        choices=(Choice("model", "Model"),),
    )

    assert screen.check_action("previous_group", ()) is False
    assert screen.check_action("next_group", ()) is False


@pytest.mark.asyncio
async def test_editable_choice_filter_owns_text_input_outside_inline_edit(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async def save_value(_choice_id: str, _value: str) -> tuple[Choice, ...]:
        return ()

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        screen = EditableChoiceScreen(
            title="EDITABLE ITEMS",
            placeholder="Filter items…",
            choices=(Choice("item", "Item", group="ITEMS"),),
            on_save=save_value,
        )
        app.push_screen(screen)
        await pilot.pause()

        filter_input = screen.query_one("#choice-filter", Input)
        assert filter_input.has_focus is True

        await pilot.press(*tuple("abcd"), "home", "delete", "end", "backspace")
        assert filter_input.value == "bc"

        await pilot.press("ctrl+u")
        assert filter_input.value == ""

        await pilot.press("enter")
        await pilot.pause()
        assert screen._editing_value == "item"

        await pilot.press("escape")
        await pilot.pause()
        assert screen._editing_value is None
        assert filter_input.has_focus is True

        await pilot.press(*tuple("Item"))
        assert filter_input.value == "Item"

        await pilot.press("escape")
        await pilot.pause()


def test_choice_focus_position_clamps_to_last_enabled_option() -> None:
    options = OptionList(
        *ChoiceScreen._options(
            (
                Choice("first", "First", group="Current"),
                Choice("disabled", "Disabled", group="Current", disabled=True),
                Choice("last", "Last", group="Archived"),
            )
        )
    )

    highlighted = ChoiceScreen._highlight_index(options, preferred_position=99)

    assert highlighted is not None
    assert options.get_option_at_index(highlighted).id == "last"


@pytest.mark.asyncio
async def test_grouped_choice_filter_hides_empty_groups_and_selects_first_choice(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    results: list[str | None] = []

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.push_screen(
            ChoiceScreen(
                title="SESSIONS",
                placeholder="Filter sessions…",
                choices=(
                    Choice("today", "Today session", group="Today"),
                    Choice("yesterday", "Yesterday session", group="Archive"),
                ),
            ),
            callback=results.append,
        )
        await pilot.pause()

        options = app.screen.query_one(OptionList)
        assert options.option_count == 5
        assert options.highlighted == 1
        await pilot.press("right")
        assert options.highlighted == 4
        await pilot.press("left")
        assert options.highlighted == 1
        await pilot.press("left")
        assert options.highlighted == 4
        await pilot.press("right")
        assert options.highlighted == 1
        await pilot.press("down")
        assert options.highlighted == 4
        await pilot.press("up")
        assert options.highlighted == 1

        app.screen.query_one("#choice-filter", Input).value = "Archive"
        await pilot.pause()

        assert options.option_count == 2
        assert options.get_option_at_index(0).prompt.plain == "Archive"
        assert options.highlighted == 1

        await pilot.press("enter", "enter")
        await pilot.pause()
        assert results == ["yesterday"]


@pytest.mark.asyncio
async def test_refreshing_choice_screen_can_retain_focus_position_across_groups(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async def move_selected(_selected: str) -> tuple[Choice, ...]:
        return (
            Choice("first", "First", group="Current"),
            Choice("third", "Third", group="Current"),
            Choice("second", "Second", group="Archived"),
        )

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.push_screen(
            ChoiceScreen(
                title="SESSIONS",
                placeholder="Filter sessions…",
                choices=(
                    Choice("first", "First", group="Current"),
                    Choice("second", "Second", group="Current"),
                    Choice("third", "Third", group="Archived"),
                ),
                initial_value="second",
                on_select=move_selected,
                retain_focus_position_on_select=True,
            )
        )
        await pilot.pause()

        options = app.screen.query_one(OptionList)
        await pilot.press("enter")
        await pilot.pause()

        assert app.screen.query_one(OptionList) is options
        assert options.highlighted is not None
        assert options.get_option_at_index(options.highlighted).id == "third"


@pytest.mark.asyncio
async def test_retained_focus_clears_when_refresh_filter_has_no_choices(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async def archive_selected(_selected: str) -> tuple[Choice, ...]:
        return (Choice("only", "Only", group="Archived"),)

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.push_screen(
            ChoiceScreen(
                title="SESSIONS",
                placeholder="Filter sessions…",
                choices=(Choice("only", "Only", group="Current"),),
                on_select=archive_selected,
                retain_focus_position_on_select=True,
            )
        )
        await pilot.pause()

        app.screen.query_one(Input).value = "Current"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        options = app.screen.query_one(OptionList)
        assert options.option_count == 0
        assert options.highlighted is None


@pytest.mark.asyncio
async def test_choice_page_refreshes_in_place_after_a_child_operation(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    label = "Before"

    async def refresh_choices() -> tuple[Choice, ...]:
        return (Choice("item", label),)

    async def update_item(_selected: str) -> tuple[Choice, ...] | None:
        nonlocal label
        label = "After"
        return None

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.push_screen(
            ChoiceScreen(
                title="ITEMS",
                placeholder="Filter items…",
                choices=(Choice("item", label),),
                on_select=update_item,
                on_refresh=refresh_choices,
            )
        )
        await pilot.pause()
        options = app.screen.query_one(OptionList)

        await pilot.press("enter")
        await pilot.pause()

        assert app.screen.query_one(OptionList) is options
        assert app.screen._choices == (Choice("item", "After"),)


@pytest.mark.asyncio
async def test_choice_page_refreshes_parent_before_in_place_back_navigation(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    label = "Before"

    async def refresh_parent() -> tuple[Choice, ...]:
        return (Choice("item", label),)

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        screen = ChoiceScreen(
            title="ITEMS",
            placeholder="Filter items…",
            choices=(Choice("item", label),),
            on_refresh=refresh_parent,
        )
        app.push_screen(screen)
        await pilot.pause()
        options = screen.query_one(OptionList)
        screen.push_page(
            title="ITEM",
            placeholder="Choose an action…",
            choices=(Choice("edit", "Edit"),),
        )
        label = "After"

        await pilot.press("escape")
        await pilot.pause()

        assert app.screen is screen
        assert screen.query_one(OptionList) is options
        assert screen._title == "ITEMS"
        assert screen._choices == (Choice("item", "After"),)
        assert screen._page_stack == []


class BlockingStreamProvider:
    provider_id = "blocking"

    def __init__(self) -> None:
        self.first_chunk_sent = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(
        self,
        *,
        model: ModelMetadata,
        system_prompt: str = "",
        messages: list[ModelMessage],
        tools: list[dict[str, object]] | None = None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        del model, system_prompt, messages, tools
        yield ProviderStreamEvent(type="text_delta", text="first ")
        self.first_chunk_sent.set()
        await self.release.wait()
        yield ProviderStreamEvent(type="text_delta", text="second")
        yield ProviderStreamEvent(type="done")

    async def check_connection(self, model: ModelMetadata) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            ok=True,
            model_id=model.model_id,
            message="ok",
        )


class RetryThenBlockProvider:
    provider_id = "retry-then-block"

    def __init__(self) -> None:
        self.retry_observed = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(
        self,
        *,
        model: ModelMetadata,
        system_prompt: str = "",
        messages: list[ModelMessage],
        tools: list[dict[str, object]] | None = None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        del model, system_prompt, messages, tools
        yield ProviderStreamEvent(
            type="retry_scheduled",
            retry=ProviderRetryNotice(
                attempt=3,
                max_attempts=5,
                delay_seconds=0,
                reason="temporary transport failure",
            ),
        )
        self.retry_observed.set()
        await self.release.wait()
        yield ProviderStreamEvent(type="text_delta", text="Recovered.")
        yield ProviderStreamEvent(type="done")


class BlockingReasoningProvider:
    provider_id = "blocking-reasoning"

    def __init__(self) -> None:
        self.first_chunk_sent = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(
        self,
        *,
        model: ModelMetadata,
        system_prompt: str = "",
        messages: list[ModelMessage],
        tools: list[dict[str, object]] | None = None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        del model, system_prompt, messages, tools
        yield ProviderStreamEvent(type="reasoning_delta", reasoning_content="Inspecting ")
        self.first_chunk_sent.set()
        await self.release.wait()
        yield ProviderStreamEvent(type="reasoning_delta", reasoning_content="the event path.")
        yield ProviderStreamEvent(type="text_delta", text="Done.")
        yield ProviderStreamEvent(type="done")

    async def check_connection(self, model: ModelMetadata) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            ok=True,
            model_id=model.model_id,
            message="ok",
        )


class ReasoningBurstThenBlockProvider:
    provider_id = "reasoning-burst"

    def __init__(self, *, reasoning_chunks: int = 270) -> None:
        self.reasoning_chunks = reasoning_chunks
        self.visible_chunk_sent = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(
        self,
        *,
        model: ModelMetadata,
        system_prompt: str = "",
        messages: list[ModelMessage],
        tools: list[dict[str, object]] | None = None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        del model, system_prompt, messages, tools
        for _ in range(self.reasoning_chunks):
            yield ProviderStreamEvent(type="reasoning_delta", reasoning_content="r")
        yield ProviderStreamEvent(type="text_delta", text="visible while running")
        self.visible_chunk_sent.set()
        await self.release.wait()
        yield ProviderStreamEvent(type="done")

    async def check_connection(self, model: ModelMetadata) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            ok=True,
            model_id=model.model_id,
            message="ok",
        )


class LiveMcpSession:
    def __init__(self) -> None:
        self.initialize_gate = asyncio.Event()
        self.tools = [self._tool("search")]

    async def initialize(self) -> MCPServerInfo:
        await self.initialize_gate.wait()
        return MCPServerInfo(
            name="Live MCP",
            version="1.0",
            protocol_version="2025-11-25",
            capabilities=MCPServerCapabilities(tools=True, tools_list_changed=True),
        )

    async def list_tools(
        self,
        cursor: str | None = None,
    ) -> tuple[list[MCPRemoteTool], str | None]:
        del cursor
        return list(self.tools), None

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, object],
    ) -> MCPRemoteToolResult:
        del name, arguments
        return MCPRemoteToolResult(content="ok")

    async def ping(self) -> None:
        return None

    @staticmethod
    def _tool(name: str) -> MCPRemoteTool:
        return MCPRemoteTool(
            name=name,
            title=name.title(),
            description=f"{name.title()} through MCP.",
            input_schema={"type": "object"},
            annotations={"readOnlyHint": True},
        )


class LiveMcpFactory:
    def __init__(self, session: LiveMcpSession) -> None:
        self.session = session
        self.tools_changed = lambda: None
        self.connection_lost = lambda _error: None

    @asynccontextmanager
    async def open(
        self,
        config,
        *,
        resolve_credential,
        diagnostic,
        tools_changed,
        connection_lost,
    ):
        del config, resolve_credential, diagnostic
        self.tools_changed = tools_changed
        self.connection_lost = connection_lost
        yield self.session


class ToolCapturingProvider:
    provider_id = "tool-capturing"

    def __init__(self) -> None:
        self.tool_batches: list[list[dict[str, object]]] = []

    async def stream(
        self,
        *,
        model: ModelMetadata,
        system_prompt: str = "",
        messages: list[ModelMessage],
        tools: list[dict[str, object]] | None = None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        del model, system_prompt, messages
        self.tool_batches.append(list(tools or []))
        yield ProviderStreamEvent(type="text_delta", text="Tools inspected.")
        yield ProviderStreamEvent(type="done")

    async def check_connection(self, model: ModelMetadata) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            ok=True,
            model_id=model.model_id,
            message="ok",
        )


class QueuedSubagentApprovalProvider:
    """Keep a child active long enough to queue input, then request approval."""

    provider_id = "queued-subagent-approval"

    def __init__(self) -> None:
        self.child_started = asyncio.Event()
        self.release_child = asyncio.Event()
        self.queued_input_consumed = asyncio.Event()
        self.root_round = 0
        self.child_round = 0

    async def stream(
        self,
        *,
        model: ModelMetadata,
        system_prompt: str = "",
        messages: list[ModelMessage],
        tools: list[dict[str, object]] | None = None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        del model, system_prompt, messages
        names = [str(item["name"]) for item in (tools or [])]
        if "SpawnAgent" not in names:
            if self.child_round == 0:
                self.child_round += 1
                self.child_started.set()
                await self.release_child.wait()
                yield ProviderStreamEvent.from_tool_call(
                    {
                        "id": "child_approval_call",
                        "name": "Danger",
                        "arguments": {},
                    }
                )
                yield ProviderStreamEvent(type="done")
                return
            yield ProviderStreamEvent(type="text_delta", text="child completed")
            yield ProviderStreamEvent(type="done")
            return

        if self.root_round == 0:
            self.root_round += 1
            yield ProviderStreamEvent.from_tool_call(
                {
                    "id": "spawn_child_call",
                    "name": "SpawnAgent",
                    "arguments": {
                        "agent_id": "general",
                        "task": "Run the Danger test tool and report completion.",
                        "description": "TUI child approval test",
                        "execution_mode": "sync",
                    },
                }
            )
            yield ProviderStreamEvent(type="done")
            return
        if self.root_round == 1:
            self.root_round += 1
            yield ProviderStreamEvent(type="text_delta", text="parent resumed")
            yield ProviderStreamEvent(type="done")
            return
        self.queued_input_consumed.set()
        yield ProviderStreamEvent(type="text_delta", text="queued input consumed")
        yield ProviderStreamEvent(type="done")

    async def check_connection(self, model: ModelMetadata) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            ok=True,
            model_id=model.model_id,
            message="ok",
        )


class MultiChildStreamingProvider:
    """Run two async child Sessions concurrently while the root completes."""

    provider_id = "multi-child-streaming"

    def __init__(self) -> None:
        self.root_round = 0
        self.root_prefix = "\n\n".join(f"Root history paragraph {index}" for index in range(80))
        self.child_prefixes = {
            task: "\n\n".join(f"{label} child paragraph {index}" for index in range(30))
            for task, label in (
                ("first child task", "first"),
                ("second child task", "second"),
            )
        }
        self.root_stream_started = asyncio.Event()
        self.release_root = asyncio.Event()
        self.first_chunks = {
            "first child task": asyncio.Event(),
            "second child task": asyncio.Event(),
        }
        self.releases = {
            "first child task": asyncio.Event(),
            "second child task": asyncio.Event(),
        }

    async def stream(
        self,
        *,
        model: ModelMetadata,
        system_prompt: str = "",
        messages: list[ModelMessage],
        tools: list[dict[str, object]] | None = None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        del model, system_prompt
        names = [str(item["name"]) for item in (tools or [])]
        if "SpawnAgent" in names:
            if self.root_round == 0:
                self.root_round += 1
                for index, task in enumerate(self.first_chunks, start=1):
                    yield ProviderStreamEvent.from_tool_call(
                        {
                            "id": f"spawn_child_{index}",
                            "name": "SpawnAgent",
                            "arguments": {
                                "agent_id": "general",
                                "task": task,
                                "description": f"Observe child {index}",
                                "execution_mode": "async",
                            },
                        },
                        index=index - 1,
                    )
                yield ProviderStreamEvent(type="done")
                return
            yield ProviderStreamEvent(type="text_delta", text=self.root_prefix)
            self.root_stream_started.set()
            await self.release_root.wait()
            yield ProviderStreamEvent(type="text_delta", text=" + root suffix")
            yield ProviderStreamEvent(type="done")
            return

        task = next(
            name
            for name in self.first_chunks
            if any(name in message.text_content for message in messages)
        )
        label = "first" if task.startswith("first") else "second"
        yield ProviderStreamEvent(type="text_delta", text=self.child_prefixes[task])
        self.first_chunks[task].set()
        await self.releases[task].wait()
        yield ProviderStreamEvent(type="text_delta", text=f" + {label} child suffix")
        yield ProviderStreamEvent(type="done")

    async def check_connection(self, model: ModelMetadata) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            ok=True,
            model_id=model.model_id,
            message="ok",
        )


class SyncChildViewProvider:
    """Keep one synchronous child reasoning while its parent view is reloaded."""

    provider_id = "sync-child-view"

    def __init__(self) -> None:
        self.root_round = 0
        self.child_reasoning_started = asyncio.Event()
        self.release_child = asyncio.Event()

    async def stream(
        self,
        *,
        model: ModelMetadata,
        system_prompt: str = "",
        messages: list[ModelMessage],
        tools: list[dict[str, object]] | None = None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        del model, system_prompt, messages
        names = [str(item["name"]) for item in (tools or [])]
        if "SpawnAgent" not in names:
            yield ProviderStreamEvent(type="reasoning_delta", reasoning_content="Inspecting")
            self.child_reasoning_started.set()
            await self.release_child.wait()
            yield ProviderStreamEvent(type="text_delta", text="child complete")
            yield ProviderStreamEvent(type="done")
            return

        if self.root_round == 0:
            self.root_round += 1
            yield ProviderStreamEvent.from_tool_call(
                {
                    "id": "sync_view_spawn",
                    "name": "SpawnAgent",
                    "arguments": {
                        "agent_id": "general",
                        "task": "Inspect while both Session views are exercised.",
                        "description": "Synchronous view test",
                        "execution_mode": "sync",
                    },
                }
            )
            yield ProviderStreamEvent(type="done")
            return
        yield ProviderStreamEvent(type="text_delta", text="parent resumed")
        yield ProviderStreamEvent(type="done")

    async def check_connection(self, model: ModelMetadata) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            ok=True,
            model_id=model.model_id,
            message="ok",
        )


class TuiPlanConfirmationProvider:
    provider_id = "tui-plan-confirmation"

    def __init__(
        self,
        options: list[dict[str, str]] | None = None,
        *,
        plan: str = "# Release plan\n\n1. Inspect.\n2. Ship safely.\n3. Verify.",
    ) -> None:
        self.options = options or []
        self.plan = plan
        self.stream_count = 0
        self.plan_path: str | None = None

    async def stream(
        self,
        *,
        model: ModelMetadata,
        system_prompt: str = "",
        messages: list[ModelMessage],
        tools: list[dict[str, object]] | None = None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        del model, system_prompt, tools
        if self.stream_count == 0:
            scaffold = next(
                message.text_content
                for message in reversed(messages)
                if message.role == "system" and "Plan file:" in message.text_content
            )
            match = re.search(r"Plan file: (.+)", scaffold)
            assert match is not None
            self.plan_path = match.group(1)
            call = ToolCallRequest(
                id="call_write_plan",
                name="Write",
                arguments={
                    "path": self.plan_path,
                    "content": self.plan,
                },
            )
        elif self.stream_count == 1:
            call = ToolCallRequest(
                id="call_exit_plan",
                name="ExitPlanMode",
                arguments={"options": self.options} if self.options else {},
            )
        else:
            self.stream_count += 1
            yield ProviderStreamEvent(type="text_delta", text="Decision received.")
            yield ProviderStreamEvent(type="done")
            return
        self.stream_count += 1
        yield ProviderStreamEvent.from_tool_call(call)
        yield ProviderStreamEvent(type="done")

    async def check_connection(self, model: ModelMetadata) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            ok=True,
            model_id=model.model_id,
            message="ok",
        )


def _application(
    tmp_path: Path,
    text: str = "**Hello** from OURO",
    *,
    skills: tuple[SkillMetadata, ...] = (),
) -> LocalApplication:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(
                event_batches=[
                    [
                        ProviderStreamEvent(type="text_delta", text=text),
                        ProviderStreamEvent(type="done"),
                    ]
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=ToolRegistry(),
        skills=skills,
        default_workspace=tmp_path,
        environment=RuntimeEnvironment.local(tmp_path, store.sessions_dir),
    )
    return LocalApplication.from_runtime(runtime)


def _write_skill_plugin(source: Path) -> None:
    manifest_dir = source / ".ouro-plugin"
    skill_dir = source / "skills" / "calendar"
    manifest_dir.mkdir(parents=True)
    skill_dir.mkdir(parents=True)
    (manifest_dir / "plugin.json").write_text(
        """{
  "schema_version": 3,
  "plugin_id": "dev.ouro.tui_skill",
  "name": "TUI Skill",
  "version": "1.0.0",
  "namespace": "tui_skill",
  "engines": {"ouro": ">=0.1.0,<0.2.0", "plugin_api": "^2.0.0"},
  "runtime": {"kind": "declarative"},
  "contributes": {"skills": [{"id": "calendar", "path": "./skills/calendar"}]},
  "permissions": [],
  "dependencies": {"plugins": []},
  "metadata": {}
} """.strip(),
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").write_text(
        "---\nname: calendar\ndescription: Manage calendars.\n---\n\nUse calendars.\n",
        encoding="utf-8",
    )


def _application_with_tool_sources(tmp_path: Path) -> LocalApplication:
    registry = ToolRegistry()
    tool_sources: tuple[tuple[str, ToolSource, str | None], ...] = (
        ("BuiltinTest", "builtin", None),
        ("McpTest", "mcp", None),
        ("OfficialPluginTest", "official_plugin", "dev.ouro.official"),
        ("ThirdPartyPluginTest", "third_party_plugin", "dev.ouro.third-party"),
        ("AiGeneratedPluginTest", "ai_generated_plugin", "dev.ouro.generated"),
    )
    for name, source, plugin_id in tool_sources:
        registry.register(
            Tool(
                definition=ToolDefinition(
                    name=name,
                    display_name=name,
                    description=f"{source} test tool",
                    source=source,
                    permission_mode="approval",
                    metadata={"plugin_id": plugin_id} if plugin_id is not None else {},
                ),
                handler=lambda invocation: ToolResult(
                    tool_call_id=invocation.tool_call_id,
                    tool_name=invocation.tool_name,
                ),
            )
        )
    registry.register(
        Tool(
            definition=ToolDefinition(
                name="ContractControlledTest",
                description="Contract-controlled test tool",
                source="third_party_plugin",
                permission_mode="auto",
                permission_control="tool",
                metadata={"plugin_id": "dev.ouro.third-party"},
            ),
            handler=lambda invocation: ToolResult(
                tool_call_id=invocation.tool_call_id,
                tool_name=invocation.tool_name,
            ),
        )
    )
    store = LocalSessionStore(tmp_path)
    store.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=registry,
        default_workspace=tmp_path,
        environment=RuntimeEnvironment.local(tmp_path, store.sessions_dir),
    )
    return LocalApplication.from_runtime(runtime)


def _endpoint_named_application(tmp_path: Path) -> LocalApplication:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(),
            ModelMetadata(
                model_id="provider-model-id",
                provider_id="supplier",
                endpoint_id="chat-endpoint",
                display_name="Friendly model",
            ),
        ),
        tools=ToolRegistry(),
        default_workspace=tmp_path,
        environment=RuntimeEnvironment.local(tmp_path, store.sessions_dir),
    )
    return LocalApplication.from_runtime(runtime)


def _thinking_application(tmp_path: Path) -> LocalApplication:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(),
            ModelMetadata(
                model_id="thinking-model",
                provider_id="faux",
                reasoning=ReasoningCapability(
                    activation="toggle",
                    supported_efforts=("low", "high"),
                    default=ReasoningRequest(mode="enabled", effort="low"),
                    output="summary",
                ),
            ),
        ),
        tools=ToolRegistry(),
        default_workspace=tmp_path,
        environment=RuntimeEnvironment.local(tmp_path, store.sessions_dir),
    )
    return LocalApplication.from_runtime(runtime)


def _approval_application(tmp_path: Path, target: Path) -> LocalApplication:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    tools = ToolRegistry()
    for tool in builtin_tools():
        tools.register(tool)
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(
                event_batches=[
                    [
                        ProviderStreamEvent(type="text_delta", text="Preparing."),
                        ProviderStreamEvent.from_tool_call(
                            {
                                "id": "call_write",
                                "name": "Write",
                                "arguments": {"path": str(target), "content": "written"},
                            }
                        ),
                        ProviderStreamEvent(type="done"),
                    ],
                    [
                        ProviderStreamEvent(type="text_delta", text="Done."),
                        ProviderStreamEvent(type="done"),
                    ],
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=tools,
        default_workspace=tmp_path,
        environment=RuntimeEnvironment.local(tmp_path, store.sessions_dir),
    )
    return LocalApplication.from_runtime(runtime)


def _multi_approval_application(tmp_path: Path, targets: tuple[Path, Path]) -> LocalApplication:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    tools = ToolRegistry()
    for tool in builtin_tools():
        tools.register(tool)
    first, second = targets
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(
                event_batches=[
                    [
                        ProviderStreamEvent.from_tool_call(
                            {
                                "id": "call_write_1",
                                "name": "Write",
                                "arguments": {"path": str(first), "content": "first"},
                            },
                            index=0,
                        ),
                        ProviderStreamEvent.from_tool_call(
                            {
                                "id": "call_write_2",
                                "name": "Write",
                                "arguments": {"path": str(second), "content": "second"},
                            },
                            index=1,
                        ),
                        ProviderStreamEvent(type="done"),
                    ],
                    [
                        ProviderStreamEvent(type="text_delta", text="Both done."),
                        ProviderStreamEvent(type="done"),
                    ],
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=tools,
        default_workspace=tmp_path,
        environment=RuntimeEnvironment.local(tmp_path, store.sessions_dir),
    )
    return LocalApplication.from_runtime(runtime)


def _blocking_application(
    tmp_path: Path,
    provider: (
        BlockingStreamProvider
        | RetryThenBlockProvider
        | BlockingReasoningProvider
        | ReasoningBurstThenBlockProvider
    ),
) -> LocalApplication:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="blocking", provider_id=provider.provider_id),
        ),
        tools=ToolRegistry(),
        default_workspace=tmp_path,
        environment=RuntimeEnvironment.local(tmp_path, store.sessions_dir),
    )
    return LocalApplication.from_runtime(runtime)


def _queued_subagent_approval_application(
    tmp_path: Path,
    provider: QueuedSubagentApprovalProvider,
) -> LocalApplication:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    tools = ToolRegistry()
    tools.register(
        Tool(
            definition=ToolDefinition(
                name="Danger",
                description="A test tool that requires approval.",
                permission_mode="approval",
                effect="state_changing",
            ),
            handler=lambda invocation: ToolResult(
                tool_call_id=invocation.tool_call_id,
                tool_name=invocation.tool_name,
                content="danger completed",
            ),
        )
    )
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="faux", provider_id=provider.provider_id),
        ),
        tools=tools,
        default_workspace=tmp_path,
        environment=RuntimeEnvironment.local(tmp_path, store.sessions_dir),
    )
    return LocalApplication.from_runtime(runtime)


def _multi_child_streaming_application(
    tmp_path: Path,
    provider: MultiChildStreamingProvider,
) -> LocalApplication:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="faux", provider_id=provider.provider_id),
        ),
        tools=ToolRegistry(),
        default_workspace=tmp_path,
        environment=RuntimeEnvironment.local(tmp_path, store.sessions_dir),
    )
    return LocalApplication.from_runtime(runtime)


def _persist_direct_child_session(application: LocalApplication) -> tuple[str, str]:
    """Persist one canonical root/Delegation/child aggregate for picker tests."""

    store = application.runtime.store
    definition = AgentDefinition(
        agent_id="general",
        name="General",
        description="General subagent.",
        tool_selection=ToolSelection(mode="selected", names=()),
    )
    root_run_id = "run_picker_root"
    child_run_id = "run_picker_child"
    root = SessionRecord(
        session_id="ses_picker_root",
        title="Picker root",
        last_run_id=root_run_id,
    )
    child = SessionRecord(
        session_id="ses_picker_child",
        title="Picker child",
        session_kind="subagent",
        root_session_id=root.session_id,
        parent_session_id=root.session_id,
        delegation_id="dlg_picker_child",
        last_run_id=child_run_id,
    )
    store.create_session(root)
    store.save_run(
        RunRecord(
            run_id=root_run_id,
            session_id=root.session_id,
            status="completed",
            agent_catalog=AgentCatalogSnapshot(
                entries=(AgentCatalogEntry(definition=definition),)
            ),
        )
    )
    store.create_session(child)
    store.save_run(
        RunRecord(
            run_id=child_run_id,
            session_id=child.session_id,
            status="completed",
            parent_run_id=root_run_id,
            initiator_id="ouro.subagents/dlg_picker_child",
            metadata={
                "delegation_id": child.delegation_id,
                "root_session_id": root.session_id,
                "agent_id": definition.agent_id,
                "agent_definition_contract_hash": definition.contract_hash,
            },
        )
    )
    store.create_delegation(
        DelegationRecord(
            delegation_id=child.delegation_id or "",
            idempotency_key="picker/direct-child",
            root_session_id=root.session_id,
            parent_session_id=root.session_id,
            parent_run_id=root_run_id,
            child_session_id=child.session_id,
            current_child_run_id=child_run_id,
            agent_id=definition.agent_id,
            task_description="Inspect direct child navigation.",
            prompt_digest="sha256:picker-direct-child",
            spawn_state="submitted",
            definition=definition,
        )
    )
    store.append_message(
        MessageRecord.text(
            session_id=child.session_id,
            run_id=child_run_id,
            role="assistant",
            text="Direct child history",
            sequence=1,
        )
    )
    application.runtime.sessions.open(root.session_id)
    application.runtime.sessions.open(child.session_id)
    return root.session_id, child.session_id


def _sync_child_view_application(
    tmp_path: Path,
    provider: SyncChildViewProvider,
) -> LocalApplication:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="faux", provider_id=provider.provider_id),
        ),
        tools=ToolRegistry(),
        default_workspace=tmp_path,
        environment=RuntimeEnvironment.local(tmp_path, store.sessions_dir),
    )
    return LocalApplication.from_runtime(runtime)


def _question_application(
    tmp_path: Path,
    *,
    tool_name: str = "AskUser",
) -> LocalApplication:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    tools = ToolRegistry()
    if tool_name == "AskUser":
        for tool in builtin_tools():
            tools.register(tool)
        arguments = {
            "questions": [
                {
                    "id": "file",
                    "header": "File",
                    "question": "Which file should OURO use?",
                    "options": [
                        {
                            "label": "notes.md (Recommended)",
                            "description": "Use the notes file.",
                        },
                        {
                            "label": "README.md",
                            "description": "Use the readme file.",
                        },
                    ],
                }
            ]
        }
    else:
        tools.register(
            Tool(
                definition=ToolDefinition(
                    name=tool_name,
                    description="Ask a plugin-defined question.",
                    kind="user_input",
                    effect="read_only",
                    permission_mode="auto",
                ),
                handler=lambda _invocation: ToolInputRequest(
                    data={
                        "question": "How should the plugin continue?",
                        "options": [
                            {
                                "label": "Safely",
                                "description": "Use guarded defaults.",
                            },
                            {
                                "label": "Quickly",
                                "description": "Prefer lower latency.",
                            },
                        ],
                    }
                ),
            )
        )
        arguments = {}
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(
                event_batches=[
                    [
                        ProviderStreamEvent.from_tool_call(
                            {
                                "id": "question_1",
                                "name": tool_name,
                                "arguments": arguments,
                            }
                        ),
                        ProviderStreamEvent(type="done"),
                    ],
                    [
                        ProviderStreamEvent(type="text_delta", text="Answer received."),
                        ProviderStreamEvent(type="done"),
                    ],
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=tools,
        default_workspace=tmp_path,
        environment=RuntimeEnvironment.local(tmp_path, store.sessions_dir),
    )
    return LocalApplication.from_runtime(runtime)


def _plan_confirmation_application(
    tmp_path: Path,
    *,
    options: list[dict[str, str]] | None = None,
    plan: str = "# Release plan\n\n1. Inspect.\n2. Ship safely.\n3. Verify.",
) -> LocalApplication:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    tools = ToolRegistry()
    for tool in builtin_tools():
        tools.register(tool)
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            TuiPlanConfirmationProvider(options, plan=plan),
            ModelMetadata(model_id="faux", provider_id="tui-plan-confirmation"),
        ),
        tools=tools,
        default_workspace=tmp_path,
        environment=RuntimeEnvironment.local(tmp_path, store.sessions_dir),
    )
    return LocalApplication.from_runtime(runtime)


@pytest.mark.asyncio
async def test_tui_hides_welcome_header_while_startup_is_still_in_progress(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    startup_entered = asyncio.Event()
    release_startup = asyncio.Event()
    start = app._controller.start

    async def delayed_start() -> TuiBootstrap:
        startup_entered.set()
        await release_startup.wait()
        return await start()

    app._controller.start = delayed_start  # type: ignore[method-assign]

    async with app.run_test(size=(100, 32)) as pilot:
        try:
            await asyncio.wait_for(startup_entered.wait(), timeout=1)
            await pilot.pause()
            header = app.query_one(BrandHeader)
            composer = app.query_one(Composer)
            status_line = app.query_one(StatusLine)
            assert not header.display
            assert not composer.display
            assert status_line.display
            assert status_line.region.bottom == app.screen.region.bottom
            visible_input_children = [
                child for child in app.query_one(WelcomePanel).children if child.display
            ]
            assert visible_input_children == [status_line]
            assert f"v{__version__}" in _welcome_plain_text(app.query_one(WelcomePanel))
        finally:
            release_startup.set()


@pytest.mark.asyncio
async def test_streaming_assistant_renders_completed_source_lines_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    rendered_source: list[str] = []
    real_render = AssistantTailLines._render_source_line

    def counting_render(self: AssistantTailLines, line: str):
        rendered_source.append(line)
        return real_render(self, line)

    monkeypatch.setattr(AssistantTailLines, "_render_source_line", counting_render)
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await conversation.append_assistant("first")
        await conversation.append_assistant("\n")
        for _ in range(20):
            await pilot.pause(0.05)
            if "first" in rendered_source:
                break

        live_turn = conversation.query_one(AssistantTurn)
        completed_renders = rendered_source.count("first")
        assert completed_renders >= 1

        await conversation.append_assistant("second")
        await conversation.append_assistant(" grows")
        await pilot.pause()

        assert live_turn.tail.text == "first\nsecond grows"
        assert rendered_source.count("first") == completed_renders
        assert "first" in live_turn.tail.rendered_text
        assert "second grows" in live_turn.tail.rendered_text

        rendered_source.clear()
        fragments = ("x" * 1024,) * 32
        for fragment in fragments:
            await conversation.append_assistant(fragment)
        await pilot.pause()

        width = live_turn.tail.content_region.width
        assert width > 0
        assert rendered_source
        assert max(len(line) for line in rendered_source) <= width
        assert sum(len(line) for line in rendered_source) <= len("".join(fragments)) + (
            len(fragments) * width * 2
        )


@pytest.mark.asyncio
async def test_first_assistant_chunk_is_applied_before_the_turn_is_mounted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        original_mount = conversation._transcript.mount
        mounted_sources: list[str] = []

        async def record_mount(*widgets, **kwargs):
            mounted_sources.extend(
                widget.source for widget in widgets if isinstance(widget, AssistantTurn)
            )
            return await original_mount(*widgets, **kwargs)

        monkeypatch.setattr(conversation._transcript, "mount", record_mount)

        await conversation.append_assistant("first ")

        assert mounted_sources == ["first "]
        assert conversation.query_one(AssistantTurn).source == "first "


@pytest.mark.asyncio
async def test_long_live_markdown_closes_without_blocking_the_ui_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await conversation.append_assistant("```python\n")
        await conversation.append_assistant("x" * 4_096)
        entered = threading.Event()
        release = threading.Event()
        parser_threads: list[int] = []
        block_starts = conversation_history._block_starts

        def blocking_block_starts(source: str) -> list[int]:
            parser_threads.append(threading.get_ident())
            entered.set()
            release.wait()
            return block_starts(source)

        monkeypatch.setattr(conversation_history, "_block_starts", blocking_block_starts)
        watchdog = threading.Timer(1, release.set)
        watchdog.start()
        event_loop_thread = threading.get_ident()
        finishing = asyncio.create_task(conversation.finish_assistant())

        try:
            parser_entered = await asyncio.to_thread(entered.wait, 2)
            event_loop_remained_responsive = not finishing.done()
        finally:
            release.set()
            watchdog.cancel()
        await finishing

        assert parser_entered is True
        assert event_loop_remained_responsive is True
        assert parser_threads != [event_loop_thread]
        assert not conversation.query(AssistantTurn)


@pytest.mark.asyncio
async def test_streaming_tail_skips_layout_when_visual_height_is_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await conversation.append_assistant("prefix")
        tail = conversation.query_one(AssistantTailLines)
        await _wait_until(
            lambda: tail._render_width > 0 and bool(tail.rendered_text),
            pilot,
            description="initial assistant tail layout",
        )
        refresh_layouts: list[bool] = []

        def record_refresh(*_regions, **kwargs):
            refresh_layouts.append(bool(kwargs.get("layout")))
            return tail

        monkeypatch.setattr(tail, "refresh", record_refresh)
        await conversation.append_assistant(" suffix")

        assert refresh_layouts == [False]
        assert tail.text == "prefix suffix"


@pytest.mark.asyncio
async def test_welcome_refreshes_live_system_status_each_time_it_opens(tmp_path: Path) -> None:
    application = _application(tmp_path)
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        live_skill = SkillMetadata(
            name="live-skill",
            description="Added after TUI startup.",
            location=str(tmp_path / "live-skill" / "SKILL.md"),
        )
        await application.refresh_skills((live_skill,))
        assert app._system_status.skill_names == ()

        await app._show_welcome()
        welcome = await _wait_for_welcome_render(app, pilot, contains="live-skill")

        assert app._system_status.skill_names == ("live-skill",)
        assert "1 available • live-skill" in welcome._system._skills.render().plain


@pytest.mark.asyncio
async def test_welcome_tips_point_to_keyboard_help_instead_of_sessions(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        welcome = await _wait_for_welcome_render(app, pilot, contains="Commands")
        tips = "\n".join(
            getattr(widget.render(), "plain", "") for widget in welcome._tips.query(Static)
        )

        assert "Ctrl+H\nHelp" in tips
        assert "Ctrl+S" not in tips
        assert "Sessions" not in tips


@pytest.mark.asyncio
async def test_welcome_layout_stays_centered_across_terminal_width_bounds(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(200, 32)) as pilot:
        welcome = await _wait_for_welcome_render(app, pilot, contains="Commands")
        composer = app.query_one(Composer)
        assert app.query_one(SubagentNavigationPanel).display is False

        assert welcome.region.width == 116
        assert welcome.region.x == app.screen.region.width - welcome.region.right
        assert composer.region.x == app.screen.region.width - composer.region.right

        await pilot.resize_terminal(64, 24)
        await pilot.pause()
        assert welcome.region.width == 48
        assert welcome.region.x == app.screen.region.width - welcome.region.right
        assert composer.region.x == app.screen.region.width - composer.region.right

        await pilot.resize_terminal(40, 16)
        await pilot.pause()
        assert welcome.region.width == app.screen.region.width
        assert composer.region.x == app.screen.region.width - composer.region.right
        assert welcome._top.region.height == welcome._bottom.region.height
        assert composer.region.y == welcome._top.region.bottom
        assert app.query_one(StatusLine).region.bottom <= app.screen.region.bottom

        await pilot.click("#composer")
        await pilot.press("slash")
        await pilot.pause()
        popup = app.query_one(CommandPopup)
        assert popup.region.y >= app.screen.region.y
        assert popup.region.bottom == composer.region.y
        assert popup.region.right <= app.screen.region.right


@pytest.mark.asyncio
async def test_session_input_surface_reserves_one_row_above_connected_menus(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        panel = app.query_one(SubagentNavigationPanel)
        assert panel.display is False

        composer.load_text("start session")
        await pilot.press("enter")
        await _wait_for_worker_group(app, pilot, group="run")

        assert panel.display is True
        assert panel.region.height == 1
        assert panel.render().plain.strip() == ""
        assert panel.region.bottom == composer.region.y
        assert panel.content_region.x == composer.region.x
        assert panel.content_region.right == composer.region.right
        composer_y = composer.region.y

        await pilot.press("slash")
        await pilot.pause()
        popup = app.query_one(CommandPopup)
        assert panel.region.bottom == popup.region.y
        assert popup.region.bottom == composer.region.y
        assert composer.region.y == composer_y

        await pilot.press("escape")
        await pilot.pause()
        assert panel.region.bottom == composer.region.y
        assert composer.region.y == composer_y


def test_plugin_skill_choice_is_visible_but_not_selectable() -> None:
    choice = _skill_choice(
        SkillView(
            name="calendar",
            description="Manage calendars.",
            location="C:/plugins/calendar/SKILL.md",
            source="plugin",
            origin="plugin",
            enabled=True,
            extensions={"ouro": {"plugin_id": "dev.ouro.calendar"}},
        )
    )

    assert choice.disabled is True
    assert choice.trailing == "plugin • enabled"


@pytest.mark.asyncio
async def test_tui_context_status_matches_latest_prepared_context_size(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        status_line = app.query_one(StatusLine)
        context_status = SessionContextStatus(
            context_id="ctx_1",
            session_id="session_1",
            run_id="run_1",
            turn_id="turn_1",
            model_round=1,
            provider_id="anthropic",
            model_id="claude-test",
            prepared_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            metrics=ContextMetrics(
                estimated_input_tokens=48_000,
                estimated_fixed_tokens=1_000,
                context_window_tokens=128_000,
                reserved_output_tokens=8_000,
                input_budget_tokens=120_000,
                estimated_remaining_tokens=72_000,
                estimated_utilization_ratio=0.4,
            ),
            # Provider usage can exclude cached input and therefore is not
            # the current prepared context size shown by /context.
            provider_reported_input_tokens=800,
        )
        status_line.set_context(context_status)

        context_detail = status_line.query_one("#context-detail", Static)
        context = context_detail.render()
        assert context.plain == "48.0K █████▒▒▒▒▒▒ 40%"
        percentage_offset = context.plain.index("40%")
        assert context.get_style_at_offset(percentage_offset).foreground == Color.parse("#6f7a87")

        at_warning_boundary = ContextMetrics(
            estimated_input_tokens=96_000,
            estimated_fixed_tokens=1_000,
            context_window_tokens=128_000,
            reserved_output_tokens=8_000,
            input_budget_tokens=120_000,
            estimated_remaining_tokens=24_000,
            estimated_utilization_ratio=0.8,
        )
        status_line.set_context(context_status.model_copy(update={"metrics": at_warning_boundary}))
        boundary = context_detail.render()
        assert boundary.plain == "96.0K █████████▒▒ 80%"
        assert boundary.get_style_at_offset(boundary.plain.index("80%")).foreground == Color.parse(
            "#6f7a87"
        )

        above_warning_boundary = ContextMetrics(
            estimated_input_tokens=97_200,
            estimated_fixed_tokens=1_000,
            context_window_tokens=128_000,
            reserved_output_tokens=8_000,
            input_budget_tokens=120_000,
            estimated_remaining_tokens=22_800,
            estimated_utilization_ratio=0.81,
        )
        status_line.set_context(
            context_status.model_copy(update={"metrics": above_warning_boundary})
        )
        warning = context_detail.render()
        assert warning.plain == "97.2K █████████▒▒ 81%"
        assert warning.get_style_at_offset(warning.plain.index("81%")).foreground == Color.parse(
            "#968264"
        )

        status_line.set_context(None)
        assert context_detail.render().plain == ""


@pytest.mark.asyncio
async def test_status_line_shows_model_retry_attempts_after_the_first_attempt(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        first_attempt = RuntimeActivitySnapshot(
            session_id="ses_retry",
            run_id="run_retry",
            status="running",
            activity="waiting_model",
        )
        app._sync_activity(first_attempt)
        indicator = app.query_one(ActivityIndicator)

        assert "Waiting for model •" in indicator.render().plain
        assert "/5" not in indicator.render().plain

        app._sync_activity(
            first_attempt.model_copy(update={"model_attempt": 2, "model_max_attempts": 5})
        )
        assert "Waiting for model 2/5" in indicator.render().plain

        app._sync_activity(
            first_attempt.model_copy(update={"model_attempt": 3, "model_max_attempts": 5})
        )
        assert "Waiting for model 3/5" in indicator.render().plain


@pytest.mark.asyncio
async def test_repeated_runtime_activity_skips_unchanged_presentations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        session_id = "session_hot_path"
        activity = RuntimeActivitySnapshot(
            session_id=session_id,
            run_id="run_hot_path",
            status="running",
            activity="streaming_text",
        )
        projection = TuiSessionProjection(session_id)
        projection.activity = activity
        app._projection = projection
        app._controller._session_id = session_id
        app._sync_activity(activity)
        indicator = app.query_one(ActivityIndicator)
        assert indicator._timer is not None
        indicator._timer.stop()
        indicator._timer = None

        def reject_redundant_render() -> None:
            raise AssertionError("unchanged runtime activity triggered presentation work")

        monkeypatch.setattr(
            indicator,
            "_render_indicator",
            reject_redundant_render,
        )
        monkeypatch.setattr(
            app.query_one(StatusLine),
            "_render_context",
            reject_redundant_render,
        )
        monkeypatch.setattr(
            app.query_one(SubagentNavigationPanel),
            "_refresh_content",
            reject_redundant_render,
        )
        monkeypatch.setattr(
            app.query_one(Composer),
            "_render_styled_placeholder",
            lambda _parts: reject_redundant_render(),
        )

        app._sync_activity(activity)


@pytest.mark.asyncio
async def test_session_animations_only_layout_when_geometry_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        indicator = app.query_one(ActivityIndicator)
        indicator_layouts: list[bool] = []

        def record_indicator_refresh(*_regions: object, **kwargs: object) -> ActivityIndicator:
            indicator_layouts.append(bool(kwargs.get("layout", False)))
            return indicator

        monkeypatch.setattr(indicator, "refresh", record_indicator_refresh)
        indicator._tick()
        assert indicator_layouts == [False]

        await conversation.append_reasoning("Inspecting the render path")
        reasoning = conversation._assistant
        assert reasoning is not None
        if reasoning.reasoning._timer is not None:
            reasoning.reasoning._timer.stop()
            reasoning.reasoning._timer = None
        label = reasoning.reasoning.label
        label_layouts: list[bool] = []

        def record_label_refresh(*_regions: object, **kwargs: object) -> tui_widgets.ReasoningLabel:
            label_layouts.append(bool(kwargs.get("layout", False)))
            return label

        monkeypatch.setattr(label, "refresh", record_label_refresh)
        label.set_content(
            conversation_history._reasoning_label_text("Thinking", 1.1, expanded=True)
        )
        label_layouts.clear()
        label.set_content(
            conversation_history._reasoning_label_text("Thinking", 1.2, expanded=True)
        )
        assert label_layouts == [False]

        label_layouts.clear()
        label.set_content(
            conversation_history._reasoning_label_text("Thinking", 10.0, expanded=True)
        )
        assert label_layouts == [True]

        await conversation.finish_assistant()
        tool = RuntimeToolCallActivity(
            tool_call_id="call_animation_layout",
            tool_name="Read",
            status="running",
            source_index=0,
        )
        await conversation.sync_tools((tool,))
        row = conversation.query_one(tui_widgets.ToolActivityRow)
        if row._timer is not None:
            row._timer.stop()
            row._timer = None
        tool_layouts: list[bool] = []

        def record_tool_refresh(*_regions: object, **kwargs: object) -> tui_widgets.ToolActivityRow:
            tool_layouts.append(bool(kwargs.get("layout", False)))
            return row

        monkeypatch.setattr(row, "refresh", record_tool_refresh)
        row._tick()
        assert tool_layouts == [False]


@pytest.mark.asyncio
async def test_context_detail_only_layouts_when_its_cell_width_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        status_line = app.query_one(StatusLine)
        status_line.set_context_metrics(None)
        detail = status_line.query_one("#context-detail", Static)
        layouts: list[bool] = []

        def record_refresh(*_regions: object, **kwargs: object) -> Static:
            layouts.append(bool(kwargs.get("layout", False)))
            return detail

        monkeypatch.setattr(detail, "refresh", record_refresh)
        metrics = ContextMetrics(
            estimated_input_tokens=48_000,
            estimated_fixed_tokens=1_000,
            context_window_tokens=128_000,
            reserved_output_tokens=8_000,
            input_budget_tokens=120_000,
            estimated_remaining_tokens=72_000,
            estimated_utilization_ratio=0.4,
        )
        status_line.set_context_metrics(metrics)
        status_line.set_context_metrics(
            metrics.model_copy(
                update={
                    "estimated_input_tokens": 49_200,
                    "estimated_remaining_tokens": 70_800,
                    "estimated_utilization_ratio": 0.41,
                }
            )
        )
        status_line.set_context_metrics(None)

        assert layouts == [True, False, True]


@pytest.mark.asyncio
async def test_status_line_updates_while_a_model_retry_is_waiting(tmp_path: Path) -> None:
    provider = RetryThenBlockProvider()
    app = OuroTuiApp(application=_blocking_application(tmp_path, provider))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.press(*"retry", "enter")
        try:
            await asyncio.wait_for(provider.retry_observed.wait(), timeout=2)
            await _wait_until(
                lambda: "Waiting for model 3/5" in app.query_one(ActivityIndicator).render().plain,
                pilot,
                description="model retry status",
            )

            assert "Waiting for model 3/5" in app.query_one(ActivityIndicator).render().plain
        finally:
            provider.release.set()
        await _wait_until(
            lambda: "Ready" in app.query_one(ActivityIndicator).render().plain,
            pilot,
            description="ready status after model retry",
        )
        assert "Ready" in app.query_one(ActivityIndicator).render().plain


@pytest.mark.asyncio
async def test_status_line_shows_only_the_model_endpoint_name(tmp_path: Path) -> None:
    application = _endpoint_named_application(tmp_path)
    session = application.runtime.create_session(title="Endpoint status")
    app = OuroTuiApp(
        application=application,
        resume_session_id=session.session_id,
    )

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        indicator = app.query_one(StatusLine).query_one(ActivityIndicator)

        assert "Ready • inherit • chat-endpoint" in indicator.render().plain
        assert "supplier/" not in indicator.render().plain
        assert "provider-model-id" not in indicator.render().plain
        assert "Friendly model" not in indicator.render().plain

        await app._select_model("supplier/chat-endpoint")

        assert "Ready • inherit • chat-endpoint" in indicator.render().plain


@pytest.mark.asyncio
async def test_status_line_projects_arbitrary_session_control_label_first(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        status_line = app.query_one(StatusLine)
        indicator = status_line.query_one(ActivityIndicator)

        status_line.set_session_controls(("Review Harness",))
        assert indicator.render().plain.startswith(" ▣ Review Harness • Ready")

        status_line.set_session_controls(())
        assert indicator.render().plain.startswith(" ▣ Ready")
        assert "Review Harness" not in indicator.render().plain


@pytest.mark.asyncio
async def test_model_switch_keeps_context_usage_and_updates_model_capacity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application(tmp_path)
    result = await application.runtime.run("create context")
    app = OuroTuiApp(
        application=application,
        resume_session_id=result.session.session_id,
    )

    async with app.run_test(size=(100, 32)) as pilot:
        await _wait_for_active_session(app, pilot, result.session.session_id)
        await _wait_until(
            lambda: (
                app._projection is not None
                and app._projection.session_id == result.session.session_id
            ),
            pilot,
            description="restored session projection",
        )
        projection = app._projection
        assert projection is not None
        before = projection.activity.effective_context_metrics
        assert before is not None
        selected = app._models[0].model_copy(
            update={"context_window": 256_000, "max_output_tokens": 16_000}
        )

        async def select_model(_selector: str) -> AvailableModel:
            return selected

        monkeypatch.setattr(app._controller, "select_model", select_model)
        await app._select_model(selected.key)

        after = projection.activity.effective_context_metrics
        assert after is not None
        assert after.estimated_input_tokens == before.estimated_input_tokens
        assert after.estimated_fixed_tokens == before.estimated_fixed_tokens
        assert after.estimator_id == before.estimator_id
        assert after.context_window_tokens == 256_000
        assert after.reserved_output_tokens == 16_000
        assert after.input_budget_tokens == 240_000
        status_line = app.query_one(StatusLine)
        assert status_line._context_metrics == after
        assert status_line.query_one("#context-detail", Static).render().plain

        app._sync_activity(projection.activity)
        assert status_line._context_metrics == after


@pytest.mark.asyncio
async def test_tui_changes_model_and_approval_while_the_session_is_running(tmp_path: Path) -> None:
    provider = BlockingStreamProvider()
    application = _blocking_application(tmp_path, provider)
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.click("#composer")
        await pilot.press(*"stream", "enter")
        await asyncio.wait_for(provider.first_chunk_sent.wait(), timeout=2)
        await _wait_until(
            lambda: app._projection is not None and app._projection.activity.status == "running",
            pilot,
            description="active streamed run",
        )

        try:
            await app._select_model("blocking/blocking")
            session_id = app._controller.session_id
            assert session_id is not None
            persisted = application.runtime.store.load_session_record(session_id)
            assert persisted.current_model == "blocking/blocking"

            app.action_approval_settings()
            await _wait_for_screen(app, ChoiceScreen, title="SESSION APPROVAL")
            await pilot.press("escape")

            await app._set_approval_mode("auto")
            persisted = application.runtime.store.load_session_record(session_id)
            assert persisted.approval_mode == "auto"
            assert "auto" in app.query_one(StatusLine).query_one(ActivityIndicator).render().plain
        finally:
            provider.release.set()
        await _wait_until(
            lambda: app._projection is not None and app._projection.activity.status == "completed",
            pilot,
            description="completed streamed run",
        )


@pytest.mark.asyncio
async def test_tui_reconciles_live_mcp_connection_and_tool_status(tmp_path: Path) -> None:
    application = _application(tmp_path)
    session = LiveMcpSession()
    factory = LiveMcpFactory(session)
    manager = MCPManager(
        registry=application.runtime.tools,
        client_factory=factory,
    )
    manager.configure(
        MCPServerConfig(
            server_id="live",
            name="Live MCP",
            command="unused-in-test",
            auto_start=True,
            auto_reconnect=False,
            timeout_seconds=1,
            health_check_interval_seconds=60,
        )
    )
    application.mcp = MCPRuntimeResource(manager)
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 32)) as pilot:
        welcome = await _wait_for_welcome_render(app, pilot, contains="connecting")
        mcp_label = welcome.query_one("#welcome-mcp-label", Static)
        connecting_label = mcp_label.render().plain
        assert connecting_label.endswith(" MCP")
        assert connecting_label[0] in DEFAULT_TUI_THEME.spinner_frames
        starting_status = welcome._system._mcp.render()
        assert "0/1 connected • 0 tool(s) • 1 connecting" in starting_status.plain
        assert starting_status.get_style_at_offset(
            starting_status.plain.index("connecting"),
        ).foreground == Color.parse(app.tui_theme.accent)

        session.initialize_gate.set()
        await _wait_until(
            lambda: "1/1 connected • 1 tool(s)" in welcome._system._mcp.render().plain,
            pilot,
            description="MCP connection status",
        )
        connected_status = welcome._system._mcp.render()
        assert mcp_label.render().plain == "MCP"
        assert "1/1 connected • 1 tool(s)" in connected_status.plain
        assert connected_status.get_style_at_offset(
            connected_status.plain.index("connected"),
        ).foreground == Color.parse(app.tui_theme.accent)
        assert "live__search" in application.runtime.tools.names()

        session.tools.append(LiveMcpSession._tool("fetch"))
        factory.tools_changed()
        await _wait_until(
            lambda: "1/1 connected • 2 tool(s)" in welcome._system._mcp.render().plain,
            pilot,
            description="refreshed MCP tools",
        )
        assert "1/1 connected • 2 tool(s)" in welcome._system._mcp.render().plain
        assert {"live__fetch", "live__search"}.issubset(application.runtime.tools.names())

        factory.connection_lost("connection lost")
        await _wait_until(
            lambda: "0/1 connected • 0 tool(s) • 1 failed" in welcome._system._mcp.render().plain,
            pilot,
            description="failed MCP connection status",
        )
        assert "0/1 connected • 0 tool(s) • 1 failed" in welcome._system._mcp.render().plain
        assert mcp_label.render().plain == "MCP"
        assert "live__search" not in application.runtime.tools.names()

    assert app._mcp_status_timer is None


@pytest.mark.asyncio
async def test_stream_updates_follow_only_while_conversation_is_at_bottom(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        for index in range(40):
            await conversation.add_notice(f"History {index}")
        await pilot.pause()

        assert conversation.is_vertical_scroll_end is True

        conversation.scroll_to(y=0, animate=False, immediate=True)
        await pilot.pause()
        held_position = conversation.scroll_y
        await conversation.add_notice("New streamed content while reviewing history")
        await pilot.pause()

        assert conversation.scroll_y == held_position
        assert conversation.is_vertical_scroll_end is False

        conversation.scroll_to(
            y=conversation.max_scroll_y,
            animate=False,
            immediate=True,
        )
        await pilot.pause()
        await conversation.add_notice("New streamed content after returning to bottom")
        await pilot.pause()

        assert conversation.is_vertical_scroll_end is True


@pytest.mark.asyncio
async def test_short_conversation_starts_at_top_while_following_latest_message(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await conversation.add_user("First question")
        await conversation.append_assistant("First answer")
        await pilot.pause()

        history = conversation.query_one(ConversationHistory)
        assert conversation.scroll_y == 0
        assert history.user_lines == (0,)
        assert history.region.y <= conversation.content_region.y + 2
        assert conversation.is_vertical_scroll_end is True


@pytest.mark.asyncio
async def test_reasoning_stream_renders_markup_syntax_as_plain_text(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        app.query_one(ConversationView).set_reasoning_expanded(True)
        session_id = "ses_reasoning_markup"
        app._projection = TuiSessionProjection(session_id)
        fragments = (" [", "data", '-theme="')
        events = (
            EventRecord(
                session_id=session_id,
                run_id="run_reasoning_markup",
                event_type="run_started",
                sequence=1,
                payload={"trigger": "user"},
            ),
            *(
                EventRecord(
                    session_id=session_id,
                    run_id="run_reasoning_markup",
                    event_type="model_reasoning_delta",
                    sequence=sequence,
                    payload={"reasoning_content": fragment},
                )
                for sequence, fragment in enumerate(fragments, start=2)
            ),
        )

        await app._apply_runtime_events(events)
        await pilot.pause()

        expected = "".join(fragments)
        turn = app.query_one(AssistantTurn)
        reasoning = turn.reasoning
        assert reasoning.text == expected
        # Strip rendering lands a frame after the mount, so wait for it
        # rather than asserting against an empty buffer.
        for _ in range(10):
            if reasoning._content.rendered_text:
                break
            await pilot.pause(0.05)
        # The live body renders through the same │-prefixed definition the
        # immutable history freezes with, so markup stays literal text.
        assert reasoning._content.rendered_text == f"│ {expected}"
        assert reasoning.label.render().plain.startswith(
            f"{DEFAULT_TUI_THEME.expanded_disclosure_marker} Thinking"
            f"{DEFAULT_TUI_THEME.inline_separator}"
        )
        assert reasoning.label.render().plain.endswith("s")
        assert "Thinking" in reasoning.label.render_line(0).text
        assert reasoning.label.region.x == turn.content_region.x
        assert reasoning._content.region.x == reasoning.label.region.x
        assert reasoning._content.region.y == reasoning.label.region.bottom + 1
        assert not turn.query(".assistant-label")


def _reasoning_delta_events(
    session_id: str,
    run_id: str,
    fragments: tuple[str, ...],
) -> tuple[EventRecord, ...]:
    return (
        EventRecord(
            session_id=session_id,
            run_id=run_id,
            event_type="run_started",
            sequence=1,
            payload={"trigger": "user"},
        ),
        *(
            EventRecord(
                session_id=session_id,
                run_id=run_id,
                event_type="model_reasoning_delta",
                sequence=sequence,
                payload={"reasoning_content": fragment},
            )
            for sequence, fragment in enumerate(fragments, start=2)
        ),
    )


@pytest.mark.asyncio
async def test_reasoning_stream_renders_each_completed_line_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        app.query_one(ConversationView).set_reasoning_expanded(True)
        session_id = "ses_reasoning_bounded_work"
        app._projection = TuiSessionProjection(session_id)
        fragments = tuple(f"thought {number}\n" for number in range(40))
        events = _reasoning_delta_events(session_id, "run_reasoning_bounded", fragments)

        rendered: list[str] = []
        original_render = ReasoningLines._render_source_line

        def counting_render(self: ReasoningLines, line: str) -> list[Strip]:
            rendered.append(line)
            return original_render(self, line)

        monkeypatch.setattr(ReasoningLines, "_render_source_line", counting_render)
        # One batch per fragment exercises the append path fragment by
        # fragment, the way runtime event batches arrive while thinking.
        for event in events:
            await app._apply_runtime_events((event,))
        await pilot.pause()

        expected_lines = [f"thought {number}" for number in range(40)]
        body = app.query_one(AssistantTurn).reasoning._content
        for _ in range(10):
            if body.rendered_text:
                break
            await pilot.pause(0.05)
        # A completed line renders exactly once no matter how much more
        # thinking follows; quadratic re-rendering would total N*(N+1)/2.
        assert rendered == expected_lines
        assert body.text == "".join(f"{line}\n" for line in expected_lines)
        assert body.rendered_text.splitlines() == [f"│ {line}" for line in expected_lines]


@pytest.mark.asyncio
async def test_reasoning_stream_lines_match_whole_block_rendering(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        app.query_one(ConversationView).set_reasoning_expanded(True)
        session_id = "ses_reasoning_parity"
        app._projection = TuiSessionProjection(session_id)
        long_line = " ".join(f"word{number}" for number in range(40))
        text = f'first thought\n\n{long_line}\nmarkup [data-theme=" stays literal\ntrailing partial'
        # Fragments split mid-word and mid-line so line completion and tail
        # growth are exercised separately from the source structure.
        fragments = tuple(text[index : index + 7] for index in range(0, len(text), 7))
        events = _reasoning_delta_events(session_id, "run_reasoning_parity", fragments)

        for event in events:
            await app._apply_runtime_events((event,))
        await pilot.pause()

        body = app.query_one(AssistantTurn).reasoning._content
        assert body.text == text
        for _ in range(10):
            if body.rendered_text:
                break
            await pilot.pause(0.05)
        # Incremental per-line rendering must equal one whole-block render at
        # the same width: that equality is what freezing later preserves.
        console = app.console
        options = console.options.update_width(body._render_width)
        segments = console.render(_ReasoningContent(text), options)
        whole_block = [
            strip.text.rstrip() for strip in Strip.from_lines(list(Segment.split_lines(segments)))
        ]
        assert body.rendered_text.splitlines() == whole_block
        assert body.rendered_text.splitlines()[0] == "│ first thought"
        assert any("word39" in line for line in body.rendered_text.splitlines())


@pytest.mark.asyncio
async def test_fork_command_uses_inline_targets_for_full_and_user_boundary(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 34), notifications=True) as pilot:
        await pilot.pause()
        await pilot.press("f", "o", "r", "k", "enter")
        await _wait_until(
            lambda: (
                app._controller.session_id is not None
                and app._projection is not None
                and app._projection.activity.status == "completed"
            ),
            pilot,
            description="source session",
        )
        source_session_id = app._controller.session_id
        assert source_session_id is not None
        source = application.runtime.store.load_session(source_session_id)

        await pilot.press("slash", *"fork", "enter")
        await pilot.pause()
        popup = app.query_one(CommandPopup)
        assert popup.argument_command is not None
        assert popup.argument_command.name == "/fork"
        assert [argument.value for argument in popup._arguments] == ["full", "#1"]
        assert [argument.display_label for argument in popup._arguments] == [
            "Full",
            "#1",
        ]
        popup_text = popup.query_one("#command-popup-content", Static).render().plain
        assert "Fork the complete current session" in popup_text
        assert "fork" in popup_text
        assert not isinstance(app.screen, ChoiceScreen)
        assert app._controller.session_id == source_session_id

        await pilot.press("enter")
        await _wait_until(
            lambda: app._controller.session_id not in {None, source_session_id},
            pilot,
            description="fully forked session",
        )
        full_session_id = app._controller.session_id
        assert full_session_id is not None
        full_branch = application.runtime.store.load_session(full_session_id)
        assert full_branch.session.parent_session_id == source_session_id
        assert full_branch.session.forked_from_message_id == source.messages[-1].message_id
        assert [message.text_content for message in full_branch.messages] == [
            message.text_content for message in source.messages
        ]

        await pilot.press("slash", *"fork", "enter", "down", "enter")
        await _wait_until(
            lambda: app._controller.session_id not in {None, full_session_id},
            pilot,
            description="user-boundary forked session",
        )
        boundary_session_id = app._controller.session_id
        assert boundary_session_id is not None
        boundary_branch = application.runtime.store.load_session(boundary_session_id)
        assert boundary_branch.session.parent_session_id == full_session_id
        assert boundary_branch.session.forked_from_message_id is None
        assert boundary_branch.messages == []
        assert app.query_one(Composer).text == "fork"
        assert app.query_one(Composer).has_focus is True
        assert not isinstance(app.screen, ChoiceScreen)
        assert not list(app.screen.query_one("#textual-toastrack").query("Toast"))


@pytest.mark.asyncio
async def test_fork_command_refuses_active_tui_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application(tmp_path)
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("f", "o", "r", "k", "enter")
        await _wait_until(
            lambda: (
                app._controller.session_id is not None
                and app._projection is not None
                and app._projection.activity.status == "completed"
            ),
            pilot,
            description="source session",
        )
        source_session_id = app._controller.session_id
        assert source_session_id is not None
        assert app._projection is not None
        app._projection.activity = app._projection.activity.model_copy(update={"status": "running"})

        def unexpected_branch(_message_id: str | None = None) -> None:
            raise AssertionError("active TUI session reached the branch service")

        monkeypatch.setattr(app._controller, "branch_session", unexpected_branch)
        app._fork_session("full")
        await pilot.pause()

        assert app._controller.session_id == source_session_id
        assert not isinstance(app.screen, ChoiceScreen)


@pytest.mark.asyncio
async def test_first_markdown_chunk_renders_while_provider_is_still_running(
    tmp_path: Path,
) -> None:
    provider = BlockingStreamProvider()
    app = OuroTuiApp(application=_blocking_application(tmp_path, provider))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.click("#composer")
        await pilot.press("s", "t", "r", "e", "a", "m", "enter")
        try:
            await asyncio.wait_for(provider.first_chunk_sent.wait(), timeout=2)
            await _wait_until(
                lambda: app._projection is not None and app._projection.assistant_text == "first ",
                pilot,
                description="first streamed chunk",
            )

            assert app._projection is not None
            assert app._projection.assistant_text == "first "
            assert app._projection.activity.status != "completed"
            assert app.query_one(AssistantTurn).source == "first "
        finally:
            provider.release.set()
        await _wait_until(
            lambda: app._projection is not None and app._projection.activity.status == "completed",
            pilot,
            description="completed streamed run",
        )

        assert app._projection is not None
        assert app._projection.assistant_text == "first second"
        assert app._projection.activity.status == "completed"


@pytest.mark.asyncio
async def test_visible_subscription_owns_events_without_global_sink_duplicates(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    session = application.runtime.sessions.create(title="Observed once").session
    app = OuroTuiApp(application=application, resume_session_id=session.session_id)

    async with app.run_test(size=(100, 32)) as pilot:
        await _wait_until(
            lambda: app._visible_session_id() == session.session_id,
            pilot,
            description="visible subscribed Session",
        )
        await _wait_until(
            lambda: not app._pending_runtime_events,
            pilot,
            description="settled event buffer",
        )
        assert app._observation_subscription is not None
        event = EventRecord(
            session_id=session.session_id,
            run_id="run_duplicate",
            event_type="model_text_delta",
            sequence=1,
            payload={"text": "duplicate sink delivery"},
        )

        app._publish_runtime_event(event)

        assert app._pending_runtime_events == []


@pytest.mark.asyncio
async def test_tui_runtime_event_staging_buffer_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ouro.tui.app._RUNTIME_EVENT_BUFFER_MAX", 2)
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        events = tuple(
            EventRecord(
                session_id="ses_buffer",
                run_id="run_buffer",
                event_type="model_text_delta",
                sequence=sequence,
                payload={"text": str(sequence)},
            )
            for sequence in range(1, 4)
        )

        assert app._queue_runtime_event(events[0]) is True
        assert app._queue_runtime_event(events[1]) is True
        assert app._queue_runtime_event(events[2]) is False
        assert app._pending_runtime_events == list(events[:2])
        app._pending_runtime_events.clear()


@pytest.mark.asyncio
async def test_tui_runtime_event_staging_applies_one_bounded_prefix_per_frame(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ouro.tui.app._RUNTIME_EVENT_APPLY_MAX", 2)
    app = OuroTuiApp(application=_application(tmp_path))
    applied: list[tuple[EventRecord, ...]] = []

    async def apply(events, *, allow_new_projection=False):
        del allow_new_projection
        applied.append(tuple(events))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        events = tuple(
            EventRecord(
                session_id="ses_prefix",
                run_id="run_prefix",
                event_type="model_text_delta",
                sequence=sequence,
                payload={"text": str(sequence)},
            )
            for sequence in range(1, 4)
        )
        monkeypatch.setattr(app, "_apply_runtime_events", apply)
        app._pending_runtime_events.extend(events)

        await app._flush_pending_runtime_events()

        assert applied == [events[:2]]
        assert app._pending_runtime_events == [events[2]]
        assert app._runtime_events_frame_scheduled is not None
        app._pending_runtime_events.clear()


@pytest.mark.asyncio
async def test_terminal_run_events_reuse_the_bounded_runtime_event_consumer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ouro.tui.app._RUNTIME_EVENT_APPLY_MAX", 2)
    app = OuroTuiApp(application=_application(tmp_path))
    session = SessionRecord(session_id="ses_terminal_prefix", title="Terminal prefix")
    run = RunRecord(
        session_id=session.session_id,
        run_id="run_terminal_prefix",
        status="completed",
    )
    events = [
        EventRecord(
            session_id=session.session_id,
            run_id=run.run_id,
            event_type="model_text_delta",
            sequence=sequence,
            payload={"text": str(sequence)},
        )
        for sequence in range(1, 6)
    ]
    prefixes: list[tuple[int, ...]] = []

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        app._projection = TuiSessionProjection(session.session_id)
        apply_events = app._apply_runtime_events

        async def recording_apply(events, *, allow_new_projection=False):
            prefixes.append(tuple(event.sequence for event in events))
            await apply_events(events, allow_new_projection=allow_new_projection)

        monkeypatch.setattr(app, "_apply_runtime_events", recording_apply)

        await app._apply_run_result(
            RunResult(
                session=session,
                run=run,
                final_text="12345",
                events=events,
            )
        )

        assert prefixes
        assert all(len(prefix) <= 2 for prefix in prefixes)
        assert tuple(sequence for prefix in prefixes for sequence in prefix) == (1, 2, 3, 4, 5)
        assert app._projection.last_sequence == 5


@pytest.mark.asyncio
async def test_terminal_run_events_skip_live_and_pending_event_prefixes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ouro.tui.app._RUNTIME_EVENT_APPLY_MAX", 2)
    app = OuroTuiApp(application=_application(tmp_path))
    session = SessionRecord(session_id="ses_terminal_dedup", title="Terminal dedup")
    run = RunRecord(
        session_id=session.session_id,
        run_id="run_terminal_dedup",
        status="completed",
    )
    events = [
        EventRecord(
            session_id=session.session_id,
            run_id=run.run_id,
            event_type="model_text_delta",
            sequence=sequence,
            payload={"text": str(sequence)},
        )
        for sequence in range(1, 6)
    ]
    prefixes: list[tuple[int, ...]] = []

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        projection = TuiSessionProjection(session.session_id)
        projection.apply(events[0])
        projection.apply(events[1])
        app._projection = projection
        app._pending_runtime_events.append(events[2])
        apply_events = app._apply_runtime_events

        async def recording_apply(events, *, allow_new_projection=False):
            prefixes.append(tuple(event.sequence for event in events))
            await apply_events(events, allow_new_projection=allow_new_projection)

        monkeypatch.setattr(app, "_apply_runtime_events", recording_apply)

        await app._apply_run_result(RunResult(session=session, run=run, events=events))

        assert tuple(sequence for prefix in prefixes for sequence in prefix) == (3, 4, 5)
        assert app._projection.last_sequence == 5


@pytest.mark.asyncio
async def test_runtime_event_handler_schedules_session_control_refresh_without_awaiting_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application(tmp_path)
    session_id = application.runtime.sessions.create(title="Control refresh").session.session_id
    app = OuroTuiApp(application=application)
    app._projection = TuiSessionProjection(session_id)
    app._controller._session_id = session_id
    entered = asyncio.Event()
    release = asyncio.Event()

    async def delayed_statuses() -> tuple[TuiSessionControlStatus, ...]:
        entered.set()
        await release.wait()
        return (TuiSessionControlStatus(controller_id="control", label="Control"),)

    monkeypatch.setattr(app._controller, "session_control_statuses", delayed_statuses)
    event = EventRecord(
        session_id=session_id,
        run_id="run_control_refresh",
        event_type="tool_call_completed",
        sequence=1,
        payload={"tool_call_id": "call_control_refresh"},
    )

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        applying = asyncio.create_task(app._apply_runtime_events((event,)))
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            await asyncio.sleep(0)
            assert applying.done() is True
        finally:
            release.set()
        await applying
        refresh_task = app._session_controls_refresh_task
        assert refresh_task is not None
        await refresh_task
        assert app.query_one(ActivityIndicator)._session_control_labels == ("Control",)


@pytest.mark.asyncio
async def test_session_control_refresh_coalesces_requests_and_discards_stale_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application(tmp_path)
    first = application.runtime.sessions.create(title="First controls").session
    second = application.runtime.sessions.create(title="Second controls").session
    app = OuroTuiApp(application=application)
    app._projection = TuiSessionProjection(first.session_id)
    app._controller._session_id = first.session_id
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def delayed_statuses() -> tuple[TuiSessionControlStatus, ...]:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
            return (TuiSessionControlStatus(controller_id="stale", label="Stale"),)
        return (TuiSessionControlStatus(controller_id="fresh", label="Fresh"),)

    monkeypatch.setattr(app._controller, "session_control_statuses", delayed_statuses)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        app._schedule_session_controls_refresh()
        await asyncio.wait_for(entered.wait(), timeout=1)
        app._schedule_session_controls_refresh()
        app._schedule_session_controls_refresh()
        task = app._session_controls_refresh_task
        assert task is not None
        app._controller._session_id = second.session_id
        app._projection = TuiSessionProjection(second.session_id)
        release.set()
        await task

        assert calls == 2
        assert app.query_one(ActivityIndicator)._session_control_labels == ("Fresh",)


@pytest.mark.asyncio
async def test_first_user_message_renders_when_leaving_welcome_page(tmp_path: Path) -> None:
    provider = BlockingStreamProvider()
    app = OuroTuiApp(application=_blocking_application(tmp_path, provider))

    async with app.run_test(size=(100, 32)) as pilot:
        await _wait_until(
            lambda: app.query_one(ConversationView).showing_welcome,
            pilot,
            description="welcome page",
        )

        await pilot.press(*"first message", "enter")
        try:
            await asyncio.wait_for(provider.first_chunk_sent.wait(), timeout=2)
            await _wait_until(
                lambda: app._projection is not None and app._projection.assistant_text == "first ",
                pilot,
                description="first streamed response chunk",
            )

            history = app.query_one(ConversationHistory)
            assert history.user_turns == ("first message",)
            assert "first message" in history.rendered_text
        finally:
            provider.release.set()
        await _wait_for_worker_group(app, pilot, group="run")

        assert app._projection is not None
        assert app._projection.activity.status == "completed"


@pytest.mark.asyncio
async def test_composer_placeholder_tracks_active_run(tmp_path: Path) -> None:
    provider = BlockingStreamProvider()
    app = OuroTuiApp(application=_blocking_application(tmp_path, provider))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        assert composer.placeholder == "Ask anything..."

        await pilot.click("#composer")
        await pilot.press(*"stream", "enter")
        try:
            await asyncio.wait_for(provider.first_chunk_sent.wait(), timeout=2)
            await _wait_until(
                lambda: (
                    isinstance(composer.placeholder, Content)
                    and composer.placeholder.plain == Composer.ACTIVE_RUN_PLACEHOLDER
                ),
                pilot,
                description="active-run composer placeholder",
            )
            active_placeholder = composer.placeholder
            assert isinstance(active_placeholder, Content)
            assert active_placeholder.plain == Composer.ACTIVE_RUN_PLACEHOLDER
            for shortcut in ("Enter", "Ctrl+Enter"):
                style = active_placeholder.get_style_at_offset(
                    active_placeholder.plain.index(shortcut)
                )
                assert style.bold is False
                assert style.foreground == Color.parse(TUI_THEMES["original"].accent_bright)

            composer.focus()
            composer.cursor_blink = True
            composer._cursor_visible = False
            cursor_off = composer.render_line(0)
            composer._cursor_visible = True
            cursor_on = composer.render_line(0)
            cursor_off_style = next(iter(cursor_off.crop(0, 1))).style
            cursor_on_style = next(iter(cursor_on.crop(0, 1))).style
            assert cursor_on_style != cursor_off_style
            assert next(iter(cursor_on.crop(1, 2))).style == next(
                iter(cursor_off.crop(1, 2))
            ).style
        finally:
            provider.release.set()
        await _wait_for_worker_group(app, pilot, group="run")
        await _wait_until(
            lambda: (
                app._projection is not None
                and app._projection.activity.status == "completed"
                and composer.placeholder == Composer.DEFAULT_PLACEHOLDER
                and not app._pending_runtime_events
                and not app._runtime_events_ready_posted
                and not app._runtime_events_frame_scheduled
            ),
            pilot,
            description="completed streamed run with settled TUI events",
        )

        assert app._projection is not None
        assert app._projection.activity.status == "completed"
        assert composer.placeholder == "Ask anything..."


@pytest.mark.asyncio
async def test_queued_input_rows_use_single_line_tokenized_previews(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    long_prompt = "first line\nsecond line " + ("queued detail " * 12) + "hidden suffix"
    inputs = (
        QueuedUserInputView(
            queue_id="queue_guide",
            prompt=long_prompt,
            mode="steer",
            queued_at=datetime.now(UTC),
        ),
        QueuedUserInputView(
            queue_id="queue_follow_up",
            prompt="follow next",
            mode="follow_up",
            queued_at=datetime.now(UTC),
        ),
    )

    async with app.run_test(size=(48, 24)) as pilot:
        await pilot.pause()
        panel = app.query_one(QueuedInputPanel)
        panel.set_inputs(inputs)
        await pilot.pause()

        content_widget = panel.query_one("#queued-input-content", Static)
        source = content_widget.content
        assert isinstance(source, Text)
        assert source.plain.count("\n") == len(inputs) - 1
        assert source.plain.startswith(
            f"1. Guide{DEFAULT_TUI_THEME.inline_separator}first line ↵ second line"
        )
        assert f"2. Follow-up{DEFAULT_TUI_THEME.inline_separator}follow next" in source.plain

        label_style = source.get_style_at_offset(Console(), source.plain.index("Guide"))
        separator_style = source.get_style_at_offset(
            Console(), source.plain.index(DEFAULT_TUI_THEME.inline_separator_mark)
        )
        message_style = source.get_style_at_offset(
            Console(), source.plain.index("first line")
        )
        assert not label_style.bold
        assert separator_style.color is not None
        assert separator_style.color.triplet is not None
        assert separator_style.color.triplet.hex == DEFAULT_TUI_THEME.separator_tone
        assert message_style.color is not None
        assert message_style.color.triplet is not None
        assert message_style.color.triplet.hex == DEFAULT_TUI_THEME.text_lowlight

        assert content_widget.styles.text_wrap == "nowrap"
        assert content_widget.styles.text_overflow == "ellipsis"
        assert content_widget.content_size.height == len(inputs)
        first_visible_line = content_widget.render_line(0).text.rstrip()
        assert first_visible_line.startswith(f"1. Guide{DEFAULT_TUI_THEME.inline_separator}")
        assert first_visible_line.endswith("…")
        assert "hidden suffix" not in first_visible_line


@pytest.mark.asyncio
async def test_follow_up_stays_in_queue_until_consumed_at_the_next_run(
    tmp_path: Path,
) -> None:
    provider = BlockingStreamProvider()
    application = _blocking_application(tmp_path, provider)
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.pause()
        await pilot.click("#composer")
        await pilot.press(*"first", "enter")
        await asyncio.wait_for(provider.first_chunk_sent.wait(), timeout=2)
        await _wait_until(
            lambda: app._projection is not None and app._projection.activity.status == "running",
            pilot,
            description="active run",
        )

        app.query_one(Composer).load_text("follow next")
        await pilot.press("enter")
        await _wait_until(
            lambda: app.query_one(QueuedInputPanel).display,
            pilot,
            description="queued follow-up",
        )

        queue_panel = app.query_one(QueuedInputPanel)
        conversation = app.query_one(ConversationView)
        assert queue_panel.display is True
        queue_text = queue_panel.query_one("#queued-input-content").render().plain
        assert "QUEUED INPUTS" not in queue_text
        assert "Follow-up" in queue_text
        assert f"Follow-up{DEFAULT_TUI_THEME.inline_separator}follow next" in queue_text
        assert "follow next" in queue_text
        history = conversation.query_one(ConversationHistory)
        assert history.user_turns == ("first",)

        provider.release.set()
        for _ in range(20):
            await pilot.pause(0.1)
            assistant_count = sum(entry.kind == "assistant" for entry in history.entries)
            if assistant_count == 2:
                break

        assert queue_panel.display is False
        assert history.user_turns == (
            "first",
            "follow next",
        )
        user_indexes = [
            index for index, entry in enumerate(history.entries) if entry.kind == "user"
        ]
        assistant_indexes = [
            index for index, entry in enumerate(history.entries) if entry.kind == "assistant"
        ]
        assert len(user_indexes) == len(assistant_indexes) == 2
        assert user_indexes[0] < assistant_indexes[0] < user_indexes[1] < assistant_indexes[1]

        snapshot = application.runtime.store.load_runtime_session(app._controller.session_id or "")
        queued_message = next(
            message for message in snapshot.messages if message.text_content == "follow next"
        )
        assert queued_message.metadata["queued_input_mode"] == "follow_up"
        assert len(snapshot.runs) == 2


@pytest.mark.asyncio
async def test_unqueue_command_selects_message_and_returns_it_to_composer(
    tmp_path: Path,
) -> None:
    provider = BlockingStreamProvider()
    application = _blocking_application(tmp_path, provider)
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.pause()
        await pilot.click("#composer")
        await pilot.press(*"first", "enter")
        await asyncio.wait_for(provider.first_chunk_sent.wait(), timeout=2)
        await _wait_until(
            lambda: app._projection is not None and app._projection.activity.status == "running",
            pilot,
            description="active run",
        )

        composer = app.query_one(Composer)
        composer.load_text("keep queued")
        await pilot.press("enter")
        composer.load_text("return to composer")
        await pilot.press("enter")
        await _wait_until(
            lambda: all(
                value
                in app.query_one(QueuedInputPanel)
                .query_one("#queued-input-content", Static)
                .render()
                .plain
                for value in ("keep queued", "return to composer")
            ),
            pilot,
            description="queued inputs",
        )

        await pilot.press("slash", *"unqueue", "enter")
        await pilot.pause()
        popup = app.query_one(CommandPopup)
        choices = popup.query_one("#command-popup-content", Static).render().plain
        assert popup.argument_command is not None
        assert popup.argument_command.name == "/unqueue"
        assert "1. keep queued" in choices
        assert "2. return to composer" in choices

        await pilot.press("down", "enter")
        await _wait_until(
            lambda: composer.text == "return to composer",
            pilot,
            description="restored queued input",
        )

        assert composer.text == "return to composer"
        queue_panel = app.query_one(QueuedInputPanel)
        queued_text = queue_panel.query_one("#queued-input-content", Static).render().plain
        assert queue_panel.display is True
        assert "keep queued" in queued_text
        assert "return to composer" not in queued_text

        provider.release.set()
        await _wait_until(
            lambda: app._projection is not None and app._projection.activity.status == "completed",
            pilot,
            description="run completion after unqueue",
        )

        snapshot = application.runtime.store.load_session(app._controller.session_id or "")
        assert "return to composer" not in [message.text_content for message in snapshot.messages]
        assert any(event.event_type == "queued_input_cancelled" for event in snapshot.events)


@pytest.mark.asyncio
async def test_queued_input_does_not_hide_or_block_subagent_approval(tmp_path: Path) -> None:
    provider = QueuedSubagentApprovalProvider()
    application = _queued_subagent_approval_application(tmp_path, provider)
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 38)) as pilot:
        await pilot.pause()
        await pilot.click("#composer")
        await pilot.press(*"start child", "enter")
        await asyncio.wait_for(provider.child_started.wait(), timeout=2)
        await pilot.pause()

        try:
            app.query_one(Composer).load_text("follow after child")
            await pilot.press("enter")
            await _wait_until(
                lambda: app.query_one(QueuedInputPanel).display,
                pilot,
                description="queued follow-up",
            )
            assert app.query_one(QueuedInputPanel).display is True
        finally:
            provider.release_child.set()
        await _wait_until(
            lambda: app._interaction is not None and app._interaction.is_subagent,
            pilot,
            description="subagent approval",
        )

        interaction = app._interaction
        assert interaction is not None
        assert interaction.kind == "approval"
        assert interaction.is_subagent is True
        assert interaction.tool_name == "Danger"
        approval_panel = app.query_one(InteractionBar)
        assert approval_panel.display is True
        subagent_panel = app.query_one(SubagentNavigationPanel)
        assert subagent_panel.display is True
        assert subagent_panel.region.bottom == approval_panel.region.y
        assert approval_panel.region.bottom == app.query_one(Composer).region.y
        assert (
            "Subagent General"
            in approval_panel.query_one("#interaction-summary", Static).render().plain
        )

        await pilot.press("enter")
        await asyncio.wait_for(provider.queued_input_consumed.wait(), timeout=5)
        await _wait_for_worker_group(app, pilot, group="run")
        history = app.query_one(ConversationHistory)
        await _wait_until(
            lambda: (
                app._projection is not None
                and app._projection.activity.status == "completed"
                and app._interaction is None
                and not app._pending_runtime_events
                and not app._runtime_events_ready_posted
                and not app._runtime_events_frame_scheduled
                and any(
                    entry.kind == "assistant" and "queued input consumed" in entry.text
                    for entry in history.entries
                )
            ),
            pilot,
            description="completed queued follow-up with settled TUI events",
        )

        assert app._interaction is None
        assert app.query_one(InteractionBar).display is False
        assert app.query_one(QueuedInputPanel).display is False
        rendered = "\n".join(entry.text for entry in history.entries if entry.kind == "assistant")
        assert "parent resumed" in rendered
        assert "queued input consumed" in rendered


@pytest.mark.asyncio
async def test_tui_switches_between_root_and_multiple_read_only_child_streams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = MultiChildStreamingProvider()
    application = _multi_child_streaming_application(tmp_path, provider)
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 38)) as pilot:
        await pilot.pause()
        await pilot.click("#composer")
        await pilot.press(*"start two children", "enter")
        await asyncio.wait_for(
            asyncio.gather(*(event.wait() for event in provider.first_chunks.values())),
            timeout=5,
        )
        await asyncio.wait_for(provider.root_stream_started.wait(), timeout=5)
        root_session_id = app._controller.session_id
        assert root_session_id is not None
        assert application.agents is not None
        tree = application.agents.agent_tree_snapshot(root_session_id)
        by_call = {node.parent_tool_call_id: node for node in tree.nodes}
        first = by_call["spawn_child_1"]
        second = by_call["spawn_child_2"]
        root_history = app.query_one(ConversationHistory)

        def child_entry(call_id: str):
            return next(
                (
                    entry
                    for entry in root_history.entries
                    if entry.tool is not None and entry.tool.tool_call_id == call_id
                ),
                None,
            )

        await _wait_until(
            lambda: all(
                (entry := child_entry(call_id)) is not None
                and entry.agent is not None
                and entry.agent.activity is not None
                and entry.agent.activity.activity == "streaming_text"
                for call_id in ("spawn_child_1", "spawn_child_2")
            ),
            pilot,
            description="root subagent previews",
        )
        for index, call_id in enumerate(("spawn_child_1", "spawn_child_2"), start=1):
            entry = child_entry(call_id)
            assert entry is not None
            assert entry.agent is not None
            assert entry.agent.task_description == f"Observe child {index}"
            assert entry.agent.agent_name in root_history.rendered_text
        assert "SpawnAgent" not in root_history.rendered_text
        assert "└ Responding • Observe child 1" in root_history.rendered_text
        assert "└ Responding • Observe child 2" in root_history.rendered_text
        subagent_panel = app.query_one(SubagentNavigationPanel)
        assert subagent_panel.display is True
        assert subagent_panel.current_session_id == root_session_id
        assert subagent_panel.visible_session_ids == (first.session_id, second.session_id)
        assert subagent_panel.render_content(80).plain.rstrip().endswith("▣ | ■■")
        assert str(subagent_panel.styles.text_align) == "right"
        assert subagent_panel.content_region.right == app.query_one(Composer).region.right
        await pilot.press("ctrl+h")
        await _wait_for_screen(app, HelpScreen, title="KEYBOARD HELP")
        app.post_message(events.Key("alt+right", None))
        await pilot.pause()
        assert app._visible_session_id() == root_session_id
        await pilot.press("escape")
        await pilot.click("#composer")
        await pilot.press("slash")
        await _wait_until(
            lambda: app.query_one(CommandPopup).display,
            pilot,
            description="command popup above composer",
        )
        command_popup = app.query_one(CommandPopup)
        assert subagent_panel.region.bottom == command_popup.region.y
        assert command_popup.region.bottom == app.query_one(Composer).region.y
        await pilot.press("escape")
        await _wait_until(
            lambda: not command_popup.display,
            pilot,
            description="dismissed command popup",
        )
        app.query_one(Composer).load_text("pending root draft")
        root_conversation = app.query_one(ConversationView)
        await pilot.pause()
        assert root_conversation.max_scroll_y > 0
        root_conversation.scroll_to(
            y=root_conversation.max_scroll_y - 2,
            animate=False,
            immediate=True,
        )
        await pilot.pause()
        held_root_scroll_y = root_conversation.scroll_y
        assert held_root_scroll_y > 3
        assert held_root_scroll_y < root_conversation.max_scroll_y

        app.post_message(
            ToolNavigationRequested(
                parent_run_id=first.parent_run_id,
                parent_tool_call_id="spawn_child_1",
            )
        )
        await _wait_until(
            lambda: app._visible_session_id() == first.session_id,
            pilot,
            description="first child view",
        )
        assert app._controller.session_id == root_session_id
        assert app._visible_read_only is True
        assert app.query_one(Composer).disabled is True
        assert app._projection is not None
        assert app._projection.assistant_text == provider.child_prefixes["first child task"]
        assert app.query_one(AssistantTurn).source == provider.child_prefixes["first child task"]
        child_header = app.query_one(BrandHeader).render()
        assert "read-only" not in child_header.plain
        assert app.query_one(BrandHeader)._session_detail == first.task_description
        child_status = app.query_one(ActivityIndicator).render().plain
        assert "Subagent" not in child_status
        assert "Responding" in child_status
        assert "read-only" not in child_status
        await app._refresh_session_controls()
        assert "Subagent" not in app.query_one(ActivityIndicator).render().plain
        child_placeholder = app.query_one(Composer).placeholder
        assert isinstance(child_placeholder, Content)
        assert child_placeholder.plain == (
            "Subagent view is read-only • Alt+↑ main • Alt+←/→ switch"
        )
        composer = app.query_one(Composer)
        assert composer.has_class("read-only-session")
        assert composer.styles.opacity == 1.0
        assert composer.text == ""
        assert app._controlled_composer_draft == "pending root draft"
        for shortcut in ("Alt+↑", "Alt+←/→"):
            shortcut_style = child_placeholder.get_style_at_offset(
                child_placeholder.plain.index(shortcut)
            )
            assert shortcut_style.bold is False
            assert shortcut_style.foreground == Color.parse(TUI_THEMES["original"].accent_bright)
        rendered_placeholder = composer.render_line(0)
        assert rendered_placeholder.text == child_placeholder.plain
        for shortcut in ("Alt+↑", "Alt+←/→"):
            offset = rendered_placeholder.text.index(shortcut)
            segment = next(iter(rendered_placeholder.crop(offset, offset + len(shortcut))))
            assert segment.style is not None and not segment.style.bold
            assert segment.style.color is not None
            assert (
                segment.style.color.get_truecolor().hex
                == TUI_THEMES["original"].accent_bright
            )
        assert any(
            entry.kind == "user" and entry.text == "first child task"
            for entry in app.query_one(ConversationHistory).entries
        )

        writes: list[str] = []

        async def reject_submit(prompt: str, **_kwargs):
            writes.append(f"submit:{prompt}")
            raise AssertionError("read-only child view submitted input")

        async def reject_abort():
            writes.append("abort")
            raise AssertionError("read-only child view aborted a Run")

        monkeypatch.setattr(app._controller, "submit", reject_submit)
        monkeypatch.setattr(app._controller, "abort", reject_abort)
        app.post_message(Composer.Submitted("must not submit", "follow_up"))
        app.action_abort()
        await pilot.pause()
        assert writes == []

        provider.release_root.set()
        await _wait_for_worker_group(app, pilot, group="run")
        assert app._visible_session_id() == first.session_id
        assert app._visible_read_only is True
        assert app.query_one(Composer).disabled is True

        provider.releases["first child task"].set()
        await _wait_until(
            lambda: app._projection is not None and app._projection.activity.status == "completed",
            pilot,
            description="first child stream completion",
        )
        await _wait_until(
            lambda: "Done" in app.query_one(ActivityIndicator).render().plain,
            pilot,
            description="first child task result",
        )
        assert "Subagent" not in app.query_one(ActivityIndicator).render().plain
        first_child_text = "\n".join(
            entry.text
            for entry in app.query_one(ConversationHistory).entries
            if entry.kind == "assistant"
        )
        assert "first child paragraph 0" in first_child_text
        assert " + first child suffix" in first_child_text

        child_conversation = app.query_one(ConversationView)
        assert child_conversation.max_scroll_y > 3
        assert held_root_scroll_y > child_conversation.max_scroll_y
        child_conversation.scroll_to(
            y=child_conversation.max_scroll_y - 2,
            animate=False,
            immediate=True,
        )
        await pilot.pause()
        held_child_scroll_y = child_conversation.scroll_y
        assert held_child_scroll_y < child_conversation.max_scroll_y

        displayed_root_frames: list[tuple[str, float]] = []
        original_display = app._display

        def capture_root_frames(screen, renderable) -> None:
            rendered_history = app.query_one(ConversationHistory).rendered_text
            displayed_root_frames.append(
                (rendered_history, app.query_one(ConversationView).scroll_y)
            )
            original_display(screen, renderable)

        monkeypatch.setattr(app, "_display", capture_root_frames)

        app.action_main_session()
        await _wait_for_worker_group(app, pilot, group="navigation")
        assert app._visible_session_id() == root_session_id
        assert app.query_one(ConversationView).scroll_y == held_root_scroll_y
        assert displayed_root_frames
        assert all(
            (
                "first child paragraph 29" in rendered
                and scroll_y == held_child_scroll_y
            )
            or (
                "Root history paragraph 79" in rendered
                and scroll_y == held_root_scroll_y
            )
            for rendered, scroll_y in displayed_root_frames
        )
        assert app._visible_read_only is False
        composer = app.query_one(Composer)
        assert composer.disabled is False
        assert not composer.has_class("read-only-session")
        assert composer.text == "pending root draft"
        assert app._controlled_composer_draft is None
        assert "Root history paragraph 0" in "\n".join(
            entry.text
            for entry in app.query_one(ConversationHistory).entries
            if entry.kind == "assistant"
        )
        assert " + root suffix" in "\n".join(
            entry.text
            for entry in app.query_one(ConversationHistory).entries
            if entry.kind == "assistant"
        )
        root_history = app.query_one(ConversationHistory)
        await _wait_until(
            lambda: (
                (entry := child_entry("spawn_child_1")) is not None
                and entry.agent is not None
                and entry.agent.status == "completed"
            ),
            pilot,
            description="completed first child preview",
        )
        assert "└ Done • Observe child 1" in root_history.rendered_text

        displayed_child_frames: list[tuple[str, float]] = []
        rendered_during_cached_switch: list[int] = []
        write_entry = ConversationHistory._write_entry

        def capture_cached_switch_render(
            history: ConversationHistory,
            *args,
            entry_index: int,
            **kwargs,
        ) -> None:
            rendered_during_cached_switch.append(entry_index)
            write_entry(history, *args, entry_index=entry_index, **kwargs)

        monkeypatch.setattr(
            ConversationHistory,
            "_write_entry",
            capture_cached_switch_render,
        )

        def capture_child_frames(screen, renderable) -> None:
            rendered_history = app.query_one(ConversationHistory).rendered_text
            displayed_child_frames.append(
                (rendered_history, app.query_one(ConversationView).scroll_y)
            )
            original_display(screen, renderable)

        monkeypatch.setattr(app, "_display", capture_child_frames)
        app.post_message(
            ToolNavigationRequested(
                parent_run_id=first.parent_run_id,
                parent_tool_call_id="spawn_child_1",
            )
        )
        await _wait_until(
            lambda: app._visible_session_id() == first.session_id,
            pilot,
            description="reopened first child view",
        )
        assert app.query_one(ConversationView).scroll_y == held_child_scroll_y
        assert rendered_during_cached_switch == [
            len(app.query_one(ConversationHistory).entries) - 1
        ]
        rendered_during_cached_switch.clear()
        assert displayed_child_frames
        assert all(
            (
                "Root history paragraph 79" in rendered
                and scroll_y == held_root_scroll_y
            )
            or (
                "first child paragraph 29" in rendered
                and scroll_y == held_child_scroll_y
            )
            for rendered, scroll_y in displayed_child_frames
        )
        monkeypatch.setattr(app, "_display", original_display)

        app.post_message(events.Key("alt+right", None))
        await _wait_until(
            lambda: app._visible_session_id() == second.session_id,
            pilot,
            description="direct sibling child view",
        )
        assert app._controller.session_id == root_session_id
        assert subagent_panel.current_session_id == second.session_id
        assert "Observe child 2" in subagent_panel.render_content(80).plain
        rendered_during_cached_switch.clear()

        app.post_message(events.Key("alt+up", None))
        await _wait_until(
            lambda: app._visible_session_id() == root_session_id,
            pilot,
            description="return from sibling child view",
        )
        assert rendered_during_cached_switch == []

        app.post_message(
            ToolNavigationRequested(
                parent_run_id=second.parent_run_id,
                parent_tool_call_id="spawn_child_2",
            )
        )
        await _wait_until(
            lambda: app._visible_session_id() == second.session_id,
            pilot,
            description="second child view",
        )
        assert app._controller.session_id == root_session_id
        assert app._projection is not None
        assert app._projection.assistant_text == provider.child_prefixes["second child task"]

        app.action_main_session()
        await _wait_until(
            lambda: app._visible_session_id() == root_session_id,
            pilot,
            description="leave second child while it is streaming",
        )
        provider.releases["second child task"].set()
        await _wait_until(
            lambda: any(
                node.delegation_id == second.delegation_id and node.status == "completed"
                for node in application.agents.agent_tree_snapshot(root_session_id).nodes
            ),
            pilot,
            description="invisible second child completion",
        )
        app.post_message(
            ToolNavigationRequested(
                parent_run_id=second.parent_run_id,
                parent_tool_call_id="spawn_child_2",
            )
        )
        await _wait_until(
            lambda: (
                app._visible_session_id() == second.session_id
                and app._projection is not None
                and app._projection.activity.status == "idle"
            ),
            pilot,
            description="reloaded completed second child",
        )
        second_child_text = "\n".join(
            entry.text
            for entry in app.query_one(ConversationHistory).entries
            if entry.kind == "assistant"
        )
        assert "second child paragraph 0" in second_child_text
        assert " + second child suffix" in second_child_text


@pytest.mark.asyncio
async def test_sync_child_view_restore_preserves_live_tool_and_reasoning_clocks(
    tmp_path: Path,
) -> None:
    provider = SyncChildViewProvider()
    application = _sync_child_view_application(tmp_path, provider)
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 38)) as pilot:
        await pilot.pause()
        await pilot.click("#composer")
        await pilot.press(*"start child", "enter")
        await asyncio.wait_for(provider.child_reasoning_started.wait(), timeout=5)
        await pilot.pause(0.25)

        root_session_id = app._controller.session_id
        assert root_session_id is not None
        assert application.agents is not None
        child = application.agents.agent_tree_snapshot(root_session_id).nodes[0]
        app.post_message(
            ToolNavigationRequested(
                parent_run_id=child.parent_run_id,
                parent_tool_call_id="sync_view_spawn",
            )
        )
        await _wait_until(
            lambda: app._visible_session_id() == child.session_id,
            pilot,
            description="synchronous child view",
        )
        first_reasoning = app.query_one(AssistantTurn).reasoning
        first_elapsed = first_reasoning._elapsed_seconds()
        assert first_elapsed >= 0.2

        app.action_main_session()
        await _wait_until(
            lambda: app._visible_session_id() == root_session_id,
            pilot,
            description="restored synchronous parent view",
        )
        root_conversation = app.query_one(ConversationView)
        await _wait_until(
            lambda: "sync_view_spawn" in root_conversation._tools,
            pilot,
            description="restored live SpawnAgent row",
        )
        root_tool = root_conversation._tools["sync_view_spawn"]
        assert root_tool.tool.status == "waiting_child"
        assert root_tool._timer is not None
        frame = root_tool._frame
        root_tool._tick()
        assert root_tool._frame != frame

        await pilot.pause(0.2)
        app.post_message(
            ToolNavigationRequested(
                parent_run_id=child.parent_run_id,
                parent_tool_call_id="sync_view_spawn",
            )
        )
        await _wait_until(
            lambda: app._visible_session_id() == child.session_id,
            pilot,
            description="reopened synchronous child view",
        )
        restored_elapsed = app.query_one(AssistantTurn).reasoning._elapsed_seconds()
        assert restored_elapsed >= first_elapsed + 0.15

        app.action_main_session()
        await _wait_until(
            lambda: app._visible_session_id() == root_session_id,
            pilot,
            description="final parent view",
        )
        provider.release_child.set()
        await _wait_for_worker_group(app, pilot, group="run")
        await _wait_until(
            lambda: app._projection is not None and app._projection.activity.status == "completed",
            pilot,
            description="synchronous parent completion",
        )
        completed = [
            entry.tool
            for entry in app.query_one(ConversationHistory).entries
            if entry.tool is not None and entry.tool.tool_call_id == "sync_view_spawn"
        ]
        assert len(completed) == 1
        assert completed[0].status == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "submit_key",
    ("ctrl+enter", "ctrl+j"),
    ids=("enhanced-terminal", "legacy-windows-terminal"),
)
async def test_guide_shortcuts_steer_the_active_run_at_the_next_model_turn(
    tmp_path: Path,
    submit_key: str,
) -> None:
    provider = BlockingStreamProvider()
    application = _blocking_application(tmp_path, provider)
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.pause()
        await pilot.click("#composer")
        await pilot.press(*"first", "enter")
        await asyncio.wait_for(provider.first_chunk_sent.wait(), timeout=2)
        await _wait_until(
            lambda: app._projection is not None and app._projection.activity.status == "running",
            pilot,
            description="active run",
        )

        queue_panel = app.query_one(QueuedInputPanel)
        try:
            app.query_one(Composer).load_text("guide now")
            await pilot.press(submit_key)
            await _wait_until(
                lambda: queue_panel.display,
                pilot,
                description="queued guidance",
            )

            queue_text = queue_panel.query_one("#queued-input-content").render().plain
            assert queue_panel.display is True
            assert "Guide" in queue_text
            assert f"Guide{DEFAULT_TUI_THEME.inline_separator}guide now" in queue_text
            assert "guide now" in queue_text
            assert "guide now" not in app.query_one(ConversationHistory).user_turns
        finally:
            provider.release.set()
        await _wait_until(
            lambda: app._projection is not None and app._projection.activity.status == "completed",
            pilot,
            description="guided run completion",
        )

        assert queue_panel.display is False
        snapshot = application.runtime.store.load_runtime_session(app._controller.session_id or "")
        guided_message = next(
            message for message in snapshot.messages if message.text_content == "guide now"
        )
        assert guided_message.metadata["queued_input_mode"] == "steer"
        assert len(snapshot.runs) == 1


@pytest.mark.asyncio
async def test_resumed_session_restores_its_durable_user_input_queue(
    tmp_path: Path,
) -> None:
    application = _approval_application(tmp_path, tmp_path / "pending.txt")
    await application.start()
    waiting = await application.runtime.run("Start approval")
    await application.runtime.run(
        "Continue after approval",
        session_id=waiting.session.session_id,
    )
    app = OuroTuiApp(
        application=application,
        approval_mode="approval",
        resume_session_id=waiting.session.session_id,
    )

    async with app.run_test(size=(100, 36)) as pilot:
        await _wait_until(
            lambda: app._interaction is not None and app.query_one(QueuedInputPanel).display,
            pilot,
            description="restored input queue",
        )

        queue_panel = app.query_one(QueuedInputPanel)
        queue_text = queue_panel.query_one("#queued-input-content").render().plain
        assert queue_panel.display is True
        assert "Follow-up" in queue_text
        assert "Continue after approval" in queue_text
        assert "Continue after approval" not in app.query_one(ConversationHistory).user_turns
        assert app._interaction is not None
        assert app._interaction.kind == "approval"
        assert app.query_one(InteractionBar).display is True


@pytest.mark.asyncio
async def test_assistant_history_restores_reasoning_separately(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    reasoning_text = (
        'Prior reasoning with [data-theme=" remains plain text across wrapping. '
        + "wrapped reasoning " * 8
    )
    message = MessageRecord(
        session_id="ses_history",
        role="assistant",
        content=[TextContent(text="Final answer")],
        reasoning_content=reasoning_text,
    )

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        conversation.set_reasoning_expanded(True)
        await conversation.load_messages((message,))
        await pilot.pause()

        history = app.query_one(ConversationHistory)
        assert len(history.entries) == 2
        thought, answer = history.entries
        assert thought.kind == "assistant"
        assert thought.reasoning == reasoning_text
        assert answer.kind == "assistant"
        assert answer.text == "Final answer"
        rendered_lines = history.rendered_text.splitlines()
        thought_line = next(
            index for index, line in enumerate(rendered_lines) if line.strip() == "- Thought"
        )
        answer_line = next(
            index for index, line in enumerate(rendered_lines) if line.strip() == "Final answer"
        )
        assert rendered_lines[thought_line + 1] == ""
        reasoning_lines = [
            line for line in rendered_lines[thought_line + 1 : answer_line] if line.strip()
        ]
        assert len(reasoning_lines) > 1
        thought_indent = len(rendered_lines[thought_line]) - len(
            rendered_lines[thought_line].lstrip()
        )
        assert all(
            line.startswith(" " * thought_indent + "\N{BOX DRAWINGS LIGHT VERTICAL} ")
            for line in reasoning_lines
        )
        assert not app.query(AssistantTurn)


@pytest.mark.asyncio
async def test_thought_display_command_applies_to_history_and_live_output_and_persists(
    tmp_path: Path,
) -> None:
    message = MessageRecord(
        session_id="ses_thought_display",
        role="assistant",
        content=[TextContent(text="Final answer")],
        reasoning_content="Historical private thought",
    )
    preferences = TuiPreferencesStore(tmp_path)
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await conversation.load_messages((message,))
        history = conversation.query_one(ConversationHistory)
        assert "+ Thought" in history.rendered_text
        assert "Historical private thought" not in history.rendered_text
        collapsed_lines = history.rendered_text.splitlines()
        collapsed_thought = next(
            index for index, line in enumerate(collapsed_lines) if line.strip() == "+ Thought"
        )
        collapsed_answer = next(
            index for index, line in enumerate(collapsed_lines) if line.strip() == "Final answer"
        )
        assert collapsed_lines[collapsed_thought + 1] == ""
        assert collapsed_answer == collapsed_thought + 2

        await pilot.press("slash", *"thoughts", "enter")
        await pilot.pause()
        popup = app.query_one(CommandPopup)
        assert popup.argument_command is not None
        assert popup.argument_command.name == "/thoughts"
        assert popup.selected_argument is not None
        assert popup.selected_argument.value == "expand"

        await pilot.press("enter")
        await pilot.pause()
        assert app.query_one(Composer).text == ""
        assert "- Thought" in history.rendered_text
        assert "Historical private thought" in history.rendered_text
        assert preferences.load().reasoning_expanded is True

        await conversation.append_reasoning("Live private thought")
        live_reasoning = app.query_one(AssistantTurn).reasoning
        assert live_reasoning.text == "Live private thought"
        assert live_reasoning.expanded is True

        await app._dispatch_command("/thoughts collapse")
        await pilot.pause()
        assert "Historical private thought" not in history.rendered_text
        assert live_reasoning.expanded is False
        assert live_reasoning.label.render().plain.startswith("+ Thinking")

        await conversation.append_assistant("Live final answer")
        await pilot.pause()
        live_turn = app.query_one(AssistantTurn)
        assert live_turn.tail.region.y == live_reasoning.label.region.bottom + 1

        await app._set_tui_theme("harbor")
        assert preferences.load() == TuiPreferences(
            theme="harbor",
            reasoning_expanded=False,
            recent_workspaces=(str(tmp_path.resolve()),),
        )

    restarted = OuroTuiApp(home=tmp_path, application=_application(tmp_path))
    async with restarted.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        conversation = restarted.query_one(ConversationView)
        await conversation.load_messages((message,))
        history = conversation.query_one(ConversationHistory)
        assert "Historical private thought" not in history.rendered_text

        await restarted._dispatch_command("/thoughts expand")
        await pilot.pause()
        assert "Historical private thought" in history.rendered_text
        assert preferences.load().reasoning_expanded is True


@pytest.mark.asyncio
async def test_history_restores_each_model_call_timing_with_one_run_summary(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    started_at = datetime(2026, 8, 11, tzinfo=UTC)
    run = RunRecord(
        session_id="ses_multi_model",
        run_id="run_multi_model",
        status="completed",
        started_at=started_at,
        ended_at=started_at + timedelta(seconds=3),
        model="provider-model",
    )
    messages = tuple(
        MessageRecord(
            session_id=run.session_id,
            run_id=run.run_id,
            role="assistant",
            content=[TextContent(text=f"Answer {index}")],
            reasoning_content=f"Thought {index}",
            model_response=ModelResponseRecord(
                provider_id="supplier",
                endpoint_id="chat-endpoint",
                model_id="provider-model",
                started_at=started_at + timedelta(seconds=index),
                reasoning_ended_at=started_at + timedelta(seconds=index, milliseconds=500),
                ended_at=started_at + timedelta(seconds=index, milliseconds=700),
            ),
        )
        for index in (0, 1)
    )

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        presentation = project_run_presentation(run, messages)
        await conversation.load_messages(messages, runs=(presentation,))
        await pilot.pause()

        history = conversation.query_one(ConversationHistory)
        thoughts = [
            entry for entry in history.entries if entry.kind == "assistant" and entry.reasoning
        ]
        summaries = [entry for entry in history.entries if entry.kind == "run"]
        assert [entry.reasoning_seconds for entry in thoughts] == [0.5, 0.5]
        assert len(summaries) == 1
        assert summaries[0].run == presentation
        assert presentation.model == "chat-endpoint"
        assert presentation.duration_seconds == 3
        assert history.rendered_text.count("+ Thought • 500ms") == 2
        assert "chat-endpoint" in history.rendered_text
        assert "3.0s" in history.rendered_text


@pytest.mark.asyncio
async def test_history_loads_failed_run_summary_with_failure_semantics(tmp_path: Path) -> None:
    started_at = datetime(2026, 8, 11, tzinfo=UTC)
    run = RunRecord(
        session_id="ses_failed_run",
        run_id="run_failed",
        status="failed",
        started_at=started_at,
        ended_at=started_at + timedelta(seconds=316.4),
        model="deepseek-v4-flash",
        error="provider returned an empty assistant message",
    )
    message = MessageRecord.text(
        session_id=run.session_id,
        run_id=run.run_id,
        role="assistant",
        text="Partial answer",
    )
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await conversation.load_messages(
            (message,),
            runs=(project_run_presentation(run, (message,)),),
        )
        await pilot.pause()

        history = conversation.query_one(ConversationHistory)
        summary = next(entry for entry in history.entries if entry.kind == "run")
        assert summary.run is not None
        assert summary.run.status == "failed"
        assert (
            f"{DEFAULT_TUI_THEME.brand_mark} deepseek-v4-flash"
            f"{DEFAULT_TUI_THEME.section_separator}5m 16.4s"
            f"{DEFAULT_TUI_THEME.section_separator}failed"
            f"{DEFAULT_TUI_THEME.section_separator}provider returned an empty assistant message"
        ) in history.rendered_text


@pytest.mark.asyncio
async def test_reloaded_history_renders_identically_to_streamed_output(
    tmp_path: Path,
) -> None:
    answer = (
        "Here is the plan:\n\n- step one\n- step two\n\n## Details\n\n"
        "```python\nx = 1\n```\n\n| col | val |\n| --- | --- |\n| a | 1 |\n\n"
        "> quoted\n\nSetext Title\n============\n\nFinal paragraph.\n"
    )
    fragments = answer.split("\n\n")
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await conversation.add_user("Question")
        for index, fragment in enumerate(fragments):
            separator = "\n\n" if index < len(fragments) - 1 else ""
            await conversation.append_assistant(fragment + separator)
            await pilot.pause()
        await conversation.finish_assistant()
        await pilot.pause()
        streamed = conversation.query_one(ConversationHistory).rendered_text
        assert "step one" in streamed
        assert "Setext Title" in streamed
        assert "Final paragraph." in streamed

        messages = (
            MessageRecord(
                session_id="ses_consistency",
                role="user",
                content=[TextContent(text="Question")],
            ),
            MessageRecord(
                session_id="ses_consistency",
                role="assistant",
                content=[TextContent(text=answer)],
                run_id="run_consistency",
            ),
        )
        await conversation.load_messages(messages)
        await pilot.pause()
        reloaded = conversation.query_one(ConversationHistory).rendered_text

        assert reloaded == streamed


@pytest.mark.asyncio
async def test_history_batch_mount_preserves_dialogue_order(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    messages = tuple(
        MessageRecord(
            session_id="ses_history_order",
            role=role,
            content=[TextContent(text=text)],
        )
        for role, text in (
            ("user", "First question"),
            ("assistant", "First answer"),
            ("user", "Second question"),
            ("assistant", "Second answer"),
        )
    )

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await conversation.load_messages(messages)
        await pilot.pause()

        history = conversation.query_one(ConversationHistory)
        transcript = [(entry.kind, entry.text) for entry in history.entries]
        assert transcript == [
            ("user", "First question"),
            ("assistant", "First answer"),
            ("user", "Second question"),
            ("assistant", "Second answer"),
        ]
        assert not conversation.query(AssistantTurn)


@pytest.mark.asyncio
async def test_history_restores_compaction_boundaries_in_chronological_order(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    session_id = "ses_compacted_history"
    messages = (
        MessageRecord.text(
            session_id=session_id,
            role="user",
            text="First question",
        ).model_copy(update={"created_at": datetime(2026, 1, 1, 10, 0, tzinfo=UTC)}),
        MessageRecord.text(
            session_id=session_id,
            role="assistant",
            text="First answer",
        ).model_copy(update={"created_at": datetime(2026, 1, 1, 10, 2, tzinfo=UTC)}),
        MessageRecord.text(
            session_id=session_id,
            role="user",
            text="Second question",
        ).model_copy(update={"created_at": datetime(2026, 1, 1, 10, 4, tzinfo=UTC)}),
    )
    compactions = (
        CompactionRecord(
            session_id=session_id,
            strategy_id="test",
            trigger="auto",
            summary="first summary",
            covered_message_ids=[messages[0].message_id],
            history_boundary_message_id=messages[0].message_id,
            source_digest="sha256:" + "1" * 64,
            estimated_tokens_before=800,
            estimated_tokens_after=300,
            duration_seconds=3_425.4,
            # The durable boundary, not wall-clock ordering, owns placement.
            created_at=datetime(2026, 1, 1, 11, 0, tzinfo=UTC),
        ),
        CompactionRecord(
            session_id=session_id,
            strategy_id="test",
            trigger="manual",
            summary="second summary",
            covered_message_ids=[messages[0].message_id, messages[1].message_id],
            history_boundary_message_id=messages[1].message_id,
            source_digest="sha256:" + "2" * 64,
            estimated_tokens_before=1_500,
            estimated_tokens_after=500,
            duration_seconds=5.4,
            created_at=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
        ),
    )

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await conversation.load_messages(messages, compactions)
        await pilot.pause()

        history = conversation.query_one(ConversationHistory)
        assert [(entry.kind, entry.compaction) for entry in history.entries] == [
            ("user", None),
            ("compaction", CompactionPresentation(1, 500, 3_425.4)),
            ("assistant", None),
            ("compaction", CompactionPresentation(2, 1_000, 5.4)),
            ("user", None),
        ]
        assert (
            f"{DEFAULT_TUI_THEME.brand_mark} Context compacted"
            f"{DEFAULT_TUI_THEME.section_separator}1 message"
            f"{DEFAULT_TUI_THEME.section_separator}500 tokens"
            f"{DEFAULT_TUI_THEME.section_separator}57m 05.4s"
        ) in history.rendered_text

        invalid = compactions[0].model_copy(update={"history_boundary_message_id": "msg_missing"})
        with pytest.raises(ValueError, match="references missing history boundary"):
            await conversation.load_messages(messages, (invalid,))


@pytest.mark.asyncio
async def test_compaction_completion_updates_context_and_transcript_without_next_request(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    metrics = ContextMetrics(
        estimated_input_tokens=250,
        estimated_fixed_tokens=100,
        context_window_tokens=1_000,
        reserved_output_tokens=100,
        input_budget_tokens=900,
        estimated_remaining_tokens=650,
        estimated_utilization_ratio=250 / 900,
    )

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        session_id = "ses_live_compaction"
        app._projection = TuiSessionProjection(session_id)
        completed = EventRecord(
            session_id=session_id,
            run_id="run_compaction",
            event_type="compaction_completed",
            sequence=1,
            payload={
                "compaction_id": "cmp_live",
                "covered_message_count": 4,
                "tokens_before": 800,
                "tokens_after": 250,
                "duration_seconds": 65.4,
                "context_metrics_after": metrics.model_dump(mode="json"),
            },
        )
        await app._apply_runtime_events((completed,))
        await pilot.pause()

        history = app.query_one(ConversationHistory)
        assert history.entries[-1].kind == "compaction"
        assert history.entries[-1].compaction == CompactionPresentation(
            covered_message_count=4,
            tokens_reduced=550,
            duration_seconds=65.4,
        )
        assert (
            f"{DEFAULT_TUI_THEME.brand_mark} Context compacted"
            f"{DEFAULT_TUI_THEME.section_separator}4 messages"
            f"{DEFAULT_TUI_THEME.section_separator}550 tokens"
            f"{DEFAULT_TUI_THEME.section_separator}1m 05.4s"
        ) in history.rendered_text
        assert app.query_one("#context-detail", Static).render().plain.startswith("250 ")


@pytest.mark.asyncio
async def test_live_host_event_updates_context_without_reloading(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    metrics = ContextMetrics(
        estimated_input_tokens=250,
        estimated_fixed_tokens=100,
        context_window_tokens=1_000,
        reserved_output_tokens=100,
        input_budget_tokens=900,
        estimated_remaining_tokens=650,
        estimated_utilization_ratio=250 / 900,
    )

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        session_id = "ses_live_context"
        app._projection = TuiSessionProjection(session_id)
        started = EventRecord(
            session_id=session_id,
            run_id="run_live_context",
            turn_id="turn_live_context",
            event_type="model_request_started",
            sequence=1,
            payload={
                "context_id": "ctx_live",
                "provider": "test",
                "endpoint": "primary",
                "model": "test-model",
                "round": 1,
                "turn_id": "turn_live_context",
                "context_metrics": metrics.model_dump(mode="json"),
            },
        )

        await app._apply_runtime_events((_event_record(project_runtime_event(started)),))
        await pilot.pause()

        assert app.query_one("#context-detail", Static).render().plain.startswith("250 ")


@pytest.mark.asyncio
async def test_persisted_history_uses_a_bounded_widget_tree(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    messages = tuple(
        MessageRecord(
            session_id="ses_large_history",
            role="user" if index % 2 == 0 else "assistant",
            content=[TextContent(text=f"Message {index}")],
            reasoning_content=(f"Reasoning {index}" if index % 2 else ""),
        )
        for index in range(400)
    )

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await conversation.load_messages(messages)
        await pilot.pause()

        history = conversation.query_one(ConversationHistory)
        # Assistant messages with reasoning split into a reasoning entry plus
        # one entry per markdown block: 200 user + 200 reasoning + 200 text.
        assert len(history.entries) == 600
        assert len(list(conversation.query("*"))) == 2


@pytest.mark.asyncio
async def test_history_projection_does_not_block_the_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    message = MessageRecord(
        session_id="ses_projection",
        role="assistant",
        content=[TextContent(text="Projected in a worker" * 4_096)],
    )

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        entered = threading.Event()
        release = threading.Event()
        projection_threads: list[int] = []
        project_transcript = tui_widgets._project_transcript_entries

        def blocking_projection(*args, **kwargs):
            projection_threads.append(threading.get_ident())
            entered.set()
            release.wait()
            return project_transcript(*args, **kwargs)

        monkeypatch.setattr(
            tui_widgets,
            "_project_transcript_entries",
            blocking_projection,
        )
        watchdog = threading.Timer(1, release.set)
        watchdog.start()
        event_loop_thread = threading.get_ident()
        loading = asyncio.create_task(
            app.query_one(ConversationView).load_messages((message,))
        )

        try:
            projection_entered = await asyncio.to_thread(entered.wait, 2)
            event_loop_remained_responsive = not loading.done()
        finally:
            release.set()
            watchdog.cancel()
        await loading

        assert projection_entered is True
        assert event_loop_remained_responsive is True
        assert projection_threads != [event_loop_thread]


@pytest.mark.asyncio
async def test_completed_live_turns_keep_the_widget_tree_bounded(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        for index in range(100):
            await conversation.add_user(f"Question {index}")
            await conversation.append_assistant(f"Answer {index}")
            await conversation.finish_assistant()
        await pilot.pause()

        history = conversation.query_one(ConversationHistory)
        assert len(history.entries) == 200
        assert len(list(conversation.query("*"))) == 2


@pytest.mark.asyncio
async def test_completed_tool_batch_archives_in_source_order(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    first = RuntimeToolCallActivity(
        tool_call_id="call_first",
        tool_name="First",
        status="running",
        source_index=0,
    )
    second = RuntimeToolCallActivity(
        tool_call_id="call_second",
        tool_name="Second",
        status="running",
        source_index=1,
    )

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await conversation.sync_tools((first, second))
        await conversation.sync_tools((first.model_copy(update={"status": "completed"}), second))

        history = conversation.query_one(ConversationHistory)
        assert not [entry for entry in history.entries if entry.kind == "tool"]
        assert len(conversation.query(".tool-row")) == 2

        await conversation.sync_tools(
            (
                first.model_copy(update={"status": "completed"}),
                second.model_copy(update={"status": "completed"}),
            )
        )
        tool_ids = [
            entry.tool.tool_call_id
            for entry in history.entries
            if entry.kind == "tool" and entry.tool is not None
        ]
        assert tool_ids == ["call_first", "call_second"]
        assert not conversation.query(".tool-row")


@pytest.mark.asyncio
async def test_streaming_tool_row_mounts_after_its_name_and_updates_in_place_on_start(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    session_id = "ses_streaming_tool"
    run_id = "run_streaming_tool"
    app._projection = TuiSessionProjection(session_id)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await app._apply_runtime_events(
            (
                EventRecord(
                    session_id=session_id,
                    run_id=run_id,
                    event_type="run_started",
                    sequence=1,
                ),
                EventRecord(
                    session_id=session_id,
                    run_id=run_id,
                    event_type="model_request_started",
                    sequence=2,
                    payload={"turn_id": "turn_streaming_tool"},
                ),
                EventRecord(
                    session_id=session_id,
                    run_id=run_id,
                    event_type="model_tool_call_delta",
                    sequence=3,
                    payload={
                        "turn_id": "turn_streaming_tool",
                        "index": 0,
                        "name_delta": "Write",
                    },
                ),
            )
        )
        assert not conversation.query(".tool-row")

        await app._apply_runtime_events(
            (
                EventRecord(
                    session_id=session_id,
                    run_id=run_id,
                    event_type="model_tool_call_delta",
                    sequence=4,
                    payload={
                        "turn_id": "turn_streaming_tool",
                        "index": 0,
                        "arguments_delta": '{"path":"out',
                    },
                ),
            )
        )
        row = conversation.query_one(tui_widgets.ToolActivityRow)
        assert row.tool.status == "streaming"
        assert row.tool.argument_stream_tail == '{"path":"out'
        assert f"Write {DEFAULT_TUI_THEME.inline_separator_mark}" in str(row.render())
        provisional_id = row.tool_call_id

        await app._apply_runtime_events(
            (
                EventRecord(
                    session_id=session_id,
                    run_id=run_id,
                    event_type="model_message_completed",
                    sequence=5,
                    payload={"tool_call_count": 1},
                ),
                EventRecord(
                    session_id=session_id,
                    run_id=run_id,
                    event_type="tool_batch_planned",
                    sequence=6,
                    payload={"batch_id": "batch_streaming_tool"},
                ),
                EventRecord(
                    session_id=session_id,
                    run_id=run_id,
                    event_type="tool_call_requested",
                    sequence=7,
                    payload={
                        "tool_call_id": "call_streaming_tool",
                        "tool_name": "Write",
                        "source_index": 0,
                        "arguments": {"path": "out.txt", "content": "hello"},
                    },
                ),
                EventRecord(
                    session_id=session_id,
                    run_id=run_id,
                    event_type="tool_call_started",
                    sequence=8,
                    payload={
                        "tool_call_id": "call_streaming_tool",
                        "tool_name": "Write",
                        "source_index": 0,
                    },
                ),
            )
        )

        assert conversation.query_one(tui_widgets.ToolActivityRow) is row
        assert len(conversation.query(".tool-row")) == 1
        assert provisional_id not in conversation._tools
        assert conversation._tools["call_streaming_tool"] is row
        assert row.tool.argument_stream_tail is None
        assert "out.txt" in str(row.render())
        assert '{"path":"out' not in str(row.render())


@pytest.mark.asyncio
async def test_tool_batch_mounts_publishes_history_and_unmounts_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    tools = tuple(
        RuntimeToolCallActivity(
            tool_call_id=f"call_{index}",
            tool_name=f"Tool{index}",
            status="running",
            source_index=index,
        )
        for index in range(4)
    )

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await conversation.add_user("Run one tool batch")
        await pilot.pause()
        mounted_batches: list[tuple[str, ...]] = []
        mount_all = conversation._transcript.mount_all

        async def record_mount_all(widgets, **kwargs):
            rows = tuple(widgets)
            mounted_batches.append(tuple(row.tool_call_id for row in rows))
            await mount_all(rows, **kwargs)

        monkeypatch.setattr(conversation._transcript, "mount_all", record_mount_all)
        await conversation.sync_tools(tools)

        history = conversation.query_one(ConversationHistory)
        size_publications = 0
        sync_virtual_size = history._sync_virtual_size

        def record_size_publication() -> None:
            nonlocal size_publications
            size_publications += 1
            sync_virtual_size()

        removed_batches: list[tuple[str, ...]] = []
        remove_children = conversation._transcript.remove_children

        async def record_remove_children(widgets):
            rows = tuple(widgets)
            removed_batches.append(tuple(row.tool_call_id for row in rows))
            await remove_children(rows)

        monkeypatch.setattr(history, "_sync_virtual_size", record_size_publication)
        monkeypatch.setattr(conversation._transcript, "remove_children", record_remove_children)
        await conversation.sync_tools(
            tuple(tool.model_copy(update={"status": "completed"}) for tool in tools)
        )

        expected_ids = tuple(tool.tool_call_id for tool in tools)
        assert mounted_batches == [expected_ids]
        assert size_publications == 1
        assert removed_batches == [expected_ids]
        assert not conversation.query(".tool-row")


@pytest.mark.asyncio
async def test_runtime_event_prefix_mounts_one_complete_tool_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    session_id = "ses_tool_batch_prefix"
    run_id = "run_tool_batch_prefix"
    app._projection = TuiSessionProjection(session_id)
    events = (
        EventRecord(
            session_id=session_id,
            run_id=run_id,
            event_type="run_started",
            sequence=1,
            payload={"trigger": "user"},
        ),
        EventRecord(
            session_id=session_id,
            run_id=run_id,
            event_type="model_request_started",
            sequence=2,
            payload={"turn_id": "turn_tool_batch_prefix"},
        ),
        EventRecord(
            session_id=session_id,
            run_id=run_id,
            event_type="model_text_delta",
            sequence=3,
            payload={"text": "Inspecting the batch."},
        ),
        EventRecord(
            session_id=session_id,
            run_id=run_id,
            event_type="tool_batch_planned",
            sequence=4,
            payload={"batch_id": "batch_prefix"},
        ),
        *(
            EventRecord(
                session_id=session_id,
                run_id=run_id,
                event_type="tool_call_requested",
                sequence=5 + index,
                payload={
                    "tool_call_id": f"call_prefix_{index}",
                    "tool_name": f"Tool{index}",
                    "source_index": index,
                    "arguments": {"index": index},
                },
            )
            for index in range(4)
        ),
    )

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        mounted_batches: list[tuple[str, ...]] = []
        mount_all = conversation._transcript.mount_all

        async def record_mount_all(widgets, **kwargs):
            rows = tuple(widgets)
            mounted_batches.append(tuple(row.tool_call_id for row in rows))
            await mount_all(rows, **kwargs)

        monkeypatch.setattr(conversation._transcript, "mount_all", record_mount_all)
        await app._apply_runtime_events(events)

        assert mounted_batches == [tuple(f"call_prefix_{index}" for index in range(4))]
        assert len(conversation.query(".tool-row")) == 4


@pytest.mark.asyncio
async def test_history_load_renders_tool_calls_between_messages(tmp_path: Path) -> None:
    """Loading a persisted session must render tool calls, not just text.

    A tool call spans two messages: an assistant message carrying the request
    (``tool_calls``) and a following ``tool_result`` message carrying the
    outcome reference. ``load_messages`` must pair them and render a
    ``ToolActivityRow`` in the right dialogue position.
    """
    app = OuroTuiApp(application=_application(tmp_path))
    tool_call_id = "tool_history_read"
    tool_started_at = datetime(2026, 8, 26, 10, 0, tzinfo=UTC)
    messages = (
        MessageRecord(
            session_id="ses_tools",
            role="user",
            content=[TextContent(text="Read the config")],
        ),
        MessageRecord(
            session_id="ses_tools",
            role="assistant",
            content=[],
            tool_calls=[
                ToolCallRequest(
                    id=tool_call_id,
                    name="read",
                    arguments={"path": "config.json"},
                )
            ],
        ),
        MessageRecord(
            session_id="ses_tools",
            role="tool_result",
            tool_result=ToolResultReference(
                tool_call_id=tool_call_id,
                tool_name="read",
                is_error=False,
            ),
            tool_execution=ToolExecutionTiming(
                started_at=tool_started_at,
                ended_at=tool_started_at + timedelta(seconds=3),
            ),
        ),
        MessageRecord(
            session_id="ses_tools",
            role="assistant",
            content=[TextContent(text="The config has 3 lines.")],
        ),
    )

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await conversation.load_messages(messages)
        await pilot.pause()

        history = conversation.query_one(ConversationHistory)
        tools = [entry.tool for entry in history.entries if entry.tool is not None]
        assert len(tools) == 1
        assert tools[0].tool_call_id == tool_call_id
        assert tools[0].status == "completed"
        assert tools[0].started_at == tool_started_at
        assert tools[0].ended_at == tool_started_at + timedelta(seconds=3)

        rendered = history.rendered_text
        assert "read" in rendered
        assert "config.json" in rendered
        assert "3.0s" in rendered

        # Dialogue order: user text, tool row, assistant text — interleaved.
        kinds = [entry.kind for entry in history.entries]
        assert kinds == ["user", "tool", "assistant"]


@pytest.mark.asyncio
async def test_history_load_keeps_the_active_tool_batch_in_live_rows(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    run_id = "run_active_tools"
    tool_call_id = "spawn_active"
    request = MessageRecord(
        session_id="ses_active_tools",
        run_id=run_id,
        role="assistant",
        content=[],
        tool_calls=[
            ToolCallRequest(
                id=tool_call_id,
                name="SpawnAgent",
                arguments={"task": "Inspect the active child."},
            )
        ],
    )
    active = RuntimeToolCallActivity(
        tool_call_id=tool_call_id,
        tool_name="SpawnAgent",
        status="waiting_child",
        source_index=0,
        arguments={"task": "Inspect the active child."},
        started_at=datetime.now(UTC) - timedelta(seconds=2),
    )

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await conversation.load_messages(
            (request,),
            mutable_tool_calls=((run_id, tool_call_id),),
        )
        conversation.begin_model_request(run_id=run_id)
        await conversation.sync_tools((active,))
        await pilot.pause()

        history = conversation.query_one(ConversationHistory)
        assert not [entry for entry in history.entries if entry.tool is not None]
        row = conversation._tools[tool_call_id]
        assert row.is_attached is True
        assert row.content_region.x == conversation.content_region.x + 2
        assert row.content_region.right == conversation.content_region.right - 2
        assert row._timer is not None
        frame = row._frame
        row._tick()
        assert row._frame != frame


def test_history_recovers_legacy_tool_timing_from_message_boundaries() -> None:
    started_at = datetime(2026, 8, 26, 10, 0, tzinfo=UTC)
    call_id = "legacy_call"
    request = MessageRecord(
        session_id="ses_legacy_tools",
        role="assistant",
        created_at=started_at,
        tool_calls=[ToolCallRequest(id=call_id, name="read")],
    )
    result = MessageRecord(
        session_id="ses_legacy_tools",
        role="tool_result",
        created_at=started_at + timedelta(seconds=4),
        tool_result=ToolResultReference(tool_call_id=call_id, tool_name="read"),
    )

    activity = _tool_activities_from_messages((request, result))[call_id]

    assert activity.started_at == started_at
    assert activity.ended_at == started_at + timedelta(seconds=4)


@pytest.mark.asyncio
async def test_history_load_projects_scaffold_visibility_from_message_record(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    messages = (
        MessageRecord.text(
            session_id="ses_scaffold",
            role="user",
            text="internal goal instruction",
            scaffold=True,
            semantic_tag="goal-create",
            sequence=1,
        ),
        MessageRecord.text(
            session_id="ses_scaffold",
            role="user",
            text="Ship the visible objective",
            scaffold=False,
            sequence=2,
        ),
        MessageRecord.text(
            session_id="ses_scaffold",
            role="assistant",
            text="internal assistant scaffold",
            scaffold=True,
            sequence=3,
        ),
        MessageRecord.text(
            session_id="ses_scaffold",
            role="assistant",
            text="Visible answer",
            scaffold=False,
            sequence=4,
        ),
    )

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await conversation.load_messages(messages)

        entries = conversation.query_one(ConversationHistory).entries
        assert [(entry.kind, entry.text) for entry in entries] == [
            ("user", "Ship the visible objective"),
            ("assistant", "Visible answer"),
        ]


@pytest.mark.asyncio
async def test_child_history_presents_only_its_own_delegated_user_inputs(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    delegated = MessageRecord(
        session_id="ses_child",
        role="user",
        content=[TextContent(text="Inspect the delegated target")],
        scaffold=True,
        metadata={
            "session_input_source": "delegation",
            "delegation_id": "dlg_child",
        },
    )
    unrelated = delegated.model_copy(
        update={
            "message_id": "msg_unrelated_delegation",
            "content": [TextContent(text="Unrelated delegated task")],
            "metadata": {
                "session_input_source": "delegation",
                "delegation_id": "dlg_other",
            },
        }
    )
    follow_up = delegated.model_copy(
        update={
            "message_id": "msg_delegated_follow_up",
            "content": [TextContent(text="Check one more file")],
        }
    )

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)

        await conversation.load_messages((delegated, unrelated))
        assert conversation.query_one(ConversationHistory).entries == ()

        await conversation.load_messages(
            (delegated, unrelated),
            visible_scaffold_delegation_id="dlg_child",
        )
        await conversation.add_user_message(unrelated)
        await conversation.add_user_message(follow_up)

        entries = conversation.query_one(ConversationHistory).entries
        assert [(entry.kind, entry.text) for entry in entries] == [
            ("user", "Inspect the delegated target"),
            ("user", "Check one more file"),
        ]


@pytest.mark.asyncio
async def test_plugin_message_event_renders_non_scaffold_user_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    visible = MessageRecord.text(
        session_id="ses_plugin_input",
        role="user",
        text="Ship through Goal",
        scaffold=False,
        sequence=1,
    )
    scaffold = MessageRecord.text(
        session_id="ses_plugin_input",
        role="user",
        text="internal goal instruction",
        scaffold=True,
        semantic_tag="goal-create",
        sequence=2,
    )
    by_id = {message.message_id: message for message in (visible, scaffold)}

    async def message(_session_id: str, message_id: str) -> MessageRecord:
        return by_id[message_id]

    async def queued_user_inputs(_session_id: str):
        return ()

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(app._controller, "message", message)
        monkeypatch.setattr(app._controller, "queued_user_inputs", queued_user_inputs)
        events = tuple(
            EventRecord(
                session_id="ses_plugin_input",
                event_type="plugin_message_added",
                payload={"message_id": item.message_id},
                sequence=index,
            )
            for index, item in enumerate((scaffold, visible), start=1)
        )

        await app._apply_runtime_events(events, allow_new_projection=True)

        entries = app.query_one(ConversationHistory).entries
        assert [(entry.kind, entry.text) for entry in entries] == [("user", "Ship through Goal")]


@pytest.mark.asyncio
async def test_history_load_marks_failed_tool_calls(tmp_path: Path) -> None:
    """A tool_result with is_error must surface as a failed tool row."""
    app = OuroTuiApp(application=_application(tmp_path))
    tool_call_id = "tool_history_fail"
    messages = (
        MessageRecord(
            session_id="ses_fail",
            role="assistant",
            content=[],
            tool_calls=[
                ToolCallRequest(
                    id=tool_call_id,
                    name="write",
                    arguments={"path": "/readonly/x"},
                )
            ],
        ),
        MessageRecord(
            session_id="ses_fail",
            role="tool_result",
            tool_result=ToolResultReference(
                tool_call_id=tool_call_id,
                tool_name="write",
                is_error=True,
            ),
        ),
    )

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await conversation.load_messages(messages)
        await pilot.pause()

        history = conversation.query_one(ConversationHistory)
        tools = [entry.tool for entry in history.entries if entry.tool is not None]
        assert len(tools) == 1
        assert tools[0].status == "failed"
        assert "failed" not in history.rendered_text


@pytest.mark.asyncio
async def test_navigate_command_in_catalog() -> None:
    """The /navigate command is registered in the command catalog."""
    assert any(cmd.name == "/navigate" for cmd in TUI_COMMANDS)


def _scrollable_messages() -> tuple[MessageRecord, ...]:
    """Build a transcript tall enough to scroll inside a small terminal."""
    return tuple(
        MessageRecord(
            session_id="ses_nav",
            role=role,
            content=[TextContent(text=text)],
        )
        for i in range(15)
        for role, text in (
            ("user", f"Question {i}"),
            ("assistant", f"Answer {i} with enough detail to occupy a line"),
        )
    )


@pytest.mark.asyncio
async def test_navigate_to_top_scrolls_to_first_message(tmp_path: Path) -> None:
    """Selecting Top jumps the conversation to the first message."""
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(80, 12)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await conversation.load_messages(_scrollable_messages())
        await pilot.pause()
        assert conversation.scroll_y > 0  # anchored at the bottom after load

        await app._dispatch_command("/navigate top")
        await pilot.pause()
        assert conversation.scroll_y == 0


@pytest.mark.asyncio
async def test_navigate_to_bottom_scrolls_to_latest(tmp_path: Path) -> None:
    """Selecting Bottom re-anchors the conversation to the latest message."""
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(80, 12)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await conversation.load_messages(_scrollable_messages())
        await pilot.pause()

        conversation.scroll_home(animate=False, immediate=True)
        await pilot.pause()
        assert conversation.scroll_y == 0

        await app._dispatch_command("/navigate bottom")
        await pilot.pause()
        assert conversation.scroll_y == conversation.max_scroll_y


@pytest.mark.asyncio
async def test_conversation_shortcuts_navigate_while_composer_is_focused(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(80, 12)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await conversation.load_messages(_scrollable_messages())
        composer = app.query_one(Composer)
        composer.load_text("draft")
        composer.focus()
        await pilot.pause()
        assert conversation.scroll_y == conversation.max_scroll_y

        await pilot.press("ctrl+up")
        await pilot.pause()
        assert conversation.scroll_y == 0

        await pilot.press("ctrl+down")
        await pilot.pause()
        assert conversation.scroll_y == conversation.max_scroll_y
        assert composer.text == "draft"


@pytest.mark.asyncio
async def test_conversation_shortcuts_navigate_adjacent_user_messages(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(80, 12)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await conversation.load_messages(_scrollable_messages())
        history = conversation.query_one(ConversationHistory)
        composer = app.query_one(Composer)
        composer.load_text("draft")
        composer.focus()

        conversation.scroll_home(animate=False, immediate=True)
        await pilot.pause()
        await pilot.press("ctrl+left")
        await pilot.pause()
        assert conversation.scroll_y == history.user_lines[0]

        await pilot.press("ctrl+right")
        await pilot.pause()
        assert conversation.scroll_y == history.user_lines[1]

        await pilot.press("ctrl+right")
        await pilot.pause()
        assert conversation.scroll_y == history.user_lines[2]

        await pilot.press("ctrl+left")
        await pilot.pause()
        assert conversation.scroll_y == history.user_lines[1]
        assert composer.text == "draft"

        await pilot.press("ctrl+down", "ctrl+right")
        await pilot.pause()
        assert conversation.scroll_y == conversation.max_scroll_y


@pytest.mark.asyncio
async def test_navigate_to_user_message_scrolls_to_target(tmp_path: Path) -> None:
    """Picking the first user message scrolls it to the top of the viewport."""
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(80, 12)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await conversation.load_messages(_scrollable_messages())
        await pilot.pause()
        assert conversation.scroll_y > 0  # at the bottom

        await app._dispatch_command("/navigate #1")
        await pilot.pause()

        history = conversation.query_one(ConversationHistory)
        assert history.user_lines[0] == 0
        assert conversation.scroll_y == history.user_lines[0]


@pytest.mark.asyncio
async def test_navigate_options_list_user_messages(tmp_path: Path) -> None:
    """The /navigate argument options include top, bottom, and every user message."""
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(80, 12)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await conversation.load_messages(_scrollable_messages())
        await pilot.pause()

        options = app._navigate_options()
        values = [opt.value for opt in options]
        assert values[:2] == ["top", "bottom"]
        # 15 user messages → #1 through #15
        assert values[2:] == [f"#{i}" for i in range(1, 16)]


@pytest.mark.asyncio
async def test_run_result_replays_events_when_live_sink_is_delayed(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path, "Recovered response"))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        app._controller._publish_event = lambda _event: None
        await pilot.click("#composer")
        await pilot.press("h", "e", "l", "l", "o", "enter")
        await _wait_until(
            lambda: app._projection is not None and app._projection.activity.status == "completed",
            pilot,
            description="replayed run result",
        )

        assert app._projection is not None
        assert app._projection.assistant_text == "Recovered response"
        assert app._projection.activity.status == "completed"


def test_active_stream_tail_waits_for_canonical_message_commit() -> None:
    session_id = "ses_restore_boundary"
    run_id = "run_restore_boundary"
    events = (
        EventRecord(
            session_id=session_id,
            run_id=run_id,
            turn_id="turn_committed",
            event_type="model_text_delta",
            sequence=1,
            payload={"text": "already stored"},
        ),
        EventRecord(
            session_id=session_id,
            run_id=run_id,
            turn_id="turn_committed",
            event_type="model_message_completed",
            sequence=2,
        ),
        EventRecord(
            session_id=session_id,
            run_id=run_id,
            turn_id="turn_pending",
            event_type="model_reasoning_delta",
            sequence=3,
            payload={"reasoning_content": "still thinking"},
        ),
        EventRecord(
            session_id=session_id,
            run_id=run_id,
            turn_id="turn_pending",
            event_type="model_text_delta",
            sequence=4,
            payload={"text": "not committed yet"},
        ),
        EventRecord(
            session_id=session_id,
            run_id=run_id,
            turn_id="turn_pending",
            event_type="model_message_completed",
            sequence=5,
        ),
    )
    committed = MessageRecord.text(
        session_id=session_id,
        run_id=run_id,
        turn_id="turn_committed",
        role="assistant",
        text="already stored",
        sequence=1,
    )

    assert _active_stream_tail(events, (committed,)) == (
        "still thinking",
        "not committed yet",
    )

    pending = MessageRecord.text(
        session_id=session_id,
        run_id=run_id,
        turn_id="turn_pending",
        role="assistant",
        text="not committed yet",
        sequence=2,
    )
    assert _active_stream_tail(events, (committed, pending)) == ("", "")


@pytest.mark.asyncio
async def test_composer_uses_shift_enter_for_newline(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.click("#composer")
        await pilot.press("a", "shift+enter", "b")

        assert app.query_one(Composer).text == "a\nb"


@pytest.mark.asyncio
async def test_session_picker_hides_route_identity_and_truncates_long_titles(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    long_title = "这是一个非常长的会话标题，用于验证选择菜单只保留可识别标题并在超出宽度时省略显示"
    application.runtime.sessions.create(
        session_id="ses_private_identity",
        title=long_title,
        title_source="user",
        current_model="private-provider/private-endpoint",
    )
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()

        options = app.screen.query_one(OptionList)
        group_header = options.get_option_at_index(0)
        assert group_header.disabled is True
        assert group_header.prompt.plain == "Today"
        assert options.highlighted == 1
        session_choice = next(
            choice for choice in app.screen._choices if choice.value == "ses_private_identity"
        )
        assert "ses_private_identity" not in session_choice.label
        assert "private-provider/private-endpoint" not in session_choice.label
        assert session_choice.label != long_title
        assert "…" in session_choice.label
        assert session_choice.description == ""
        assert session_choice.trailing == "idle"
        assert session_choice.marker == ""


@pytest.mark.asyncio
async def test_session_picker_marks_the_session_open_on_the_conversation_page(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    current = application.runtime.sessions.create(
        session_id="ses_current",
        title="Current session",
    ).session
    other = application.runtime.sessions.create(
        session_id="ses_other",
        title="Other session",
    ).session
    application.runtime.store.append_message(
        MessageRecord.text(
            session_id=current.session_id,
            role="user",
            text="Current conversation",
            sequence=1,
        )
    )
    app = OuroTuiApp(
        application=application,
        resume_session_id=current.session_id,
    )

    async with app.run_test(size=(100, 32)) as pilot:
        await _wait_for_active_session(app, pilot, current.session_id)
        await pilot.press("ctrl+s")
        await _wait_for_screen(app, ChoiceScreen, title="SESSIONS")

        choices = {choice.value: choice for choice in app.screen._choices}
        assert choices[current.session_id].marker == DEFAULT_TUI_THEME.selected_choice_marker
        assert choices[other.session_id].marker == ""


@pytest.mark.asyncio
async def test_session_picker_reads_only_the_catalog_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application(tmp_path)
    sessions = tuple(
        application.runtime.sessions.create(
            session_id=f"ses_index_only_{index}",
            title=f"Indexed {index}",
        ).session
        for index in range(3)
    )
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()

        def unexpected_canonical_read(*_args: object) -> None:
            raise AssertionError("Ctrl+S must not load canonical Session records")

        with monkeypatch.context() as catalog_guard:
            catalog_guard.setattr(
                application.runtime.store,
                "load_session_record",
                unexpected_canonical_read,
            )
            catalog_guard.setattr(
                application.runtime.store,
                "list_sessions",
                unexpected_canonical_read,
            )
            await pilot.press("ctrl+s")
            await _wait_for_screen(app, ChoiceScreen, title="SESSIONS")

        assert {choice.value for choice in app.screen._choices} == {
            session.session_id for session in sessions
        }


@pytest.mark.asyncio
async def test_session_picker_opens_canonical_session_when_catalog_entry_is_invalid(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    session = application.runtime.sessions.create(
        session_id="ses_invalid_index",
        title="Canonical session",
    ).session
    application.runtime.store.append_message(
        MessageRecord.text(
            session_id=session.session_id,
            role="user",
            text="Canonical history remains readable",
            sequence=1,
        )
    )
    index_path = application.runtime.store._catalog.index_path
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    indexed = next(item for item in payload["sessions"] if item["session_id"] == session.session_id)
    indexed.pop("session_kind")
    index_path.write_text(json.dumps(payload), encoding="utf-8")
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+s")
        await _wait_for_screen(app, ChoiceScreen, title="SESSIONS")
        choice = next(item for item in app.screen._choices if item.value == session.session_id)

        assert choice.accent == DEFAULT_TUI_THEME.failure
        assert choice.trailing == "index error"
        assert choice.group == "Index issues"

        await pilot.press("enter")
        await _wait_for_active_session(app, pilot, session.session_id)

        assert app.query_one(ConversationHistory).user_turns == (
            "Canonical history remains readable",
        )
        assert not isinstance(app.screen, ChoiceScreen)


@pytest.mark.asyncio
async def test_session_picker_closes_modal_before_rendering_selected_history(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    session = application.runtime.sessions.create(title="Readable session").session
    application.runtime.store.append_message(
        MessageRecord.text(
            session_id=session.session_id,
            role="user",
            text="Persisted user message",
            sequence=1,
        )
    )
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+s")
        await _wait_for_screen(app, ChoiceScreen, title="SESSIONS")
        session_menu = app.screen

        await asyncio.wait_for(pilot.press("enter"), timeout=2)
        await pilot.pause()

        assert session_menu not in app.screen_stack
        assert app._controller.session_id == session.session_id
        assert app.query_one(ConversationHistory).user_turns == ("Persisted user message",)


@pytest.mark.asyncio
async def test_session_picker_hides_child_and_agent_tree_keeps_it_navigable(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    root_session_id, child_session_id = _persist_direct_child_session(application)
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 32)) as pilot:
        await _wait_until(
            lambda: app._controller._started,
            pilot,
            description="TUI startup with persisted Agent tree",
        )
        await pilot.press("ctrl+s")
        await _wait_for_screen(app, ChoiceScreen, title="SESSIONS")
        assert [choice.value for choice in app.screen._choices] == [root_session_id]
        await pilot.press("enter")
        await _wait_until(
            lambda: app._visible_session_id() == root_session_id,
            pilot,
            description="root session surface",
        )

        app.post_message(events.Key("alt+right", None))
        await _wait_until(
            lambda: app._visible_session_id() == child_session_id,
            pilot,
            description="direct child session surface from Agent tree",
        )

        panel = app.query_one(SubagentNavigationPanel)
        composer = app.query_one(Composer)
        assert app._controller.session_id == root_session_id
        assert app._visible_read_only is True
        assert app._visible_agent is not None
        assert app._visible_agent.session_id == child_session_id
        assert composer.disabled is True
        assert composer.has_class("read-only-session")
        assert panel.display is True
        assert panel.current_session_id == child_session_id
        assert panel.visible_session_ids == (child_session_id,)

        app.post_message(events.Key("alt+up", None))
        await _wait_until(
            lambda: app._visible_session_id() == root_session_id,
            pilot,
            description="root surface after direct child entry",
        )

        assert app._visible_read_only is False
        assert composer.disabled is False
        assert not composer.has_class("read-only-session")
        assert panel.current_session_id == root_session_id

        rendered_panel = panel.render_content(panel.content_region.width).plain
        child_node_x = rendered_panel.rfind("■")
        await pilot.hover(panel, offset=(child_node_x, 0))
        await pilot.pause()
        hovered_panel = panel.render_content(panel.content_region.width).plain
        assert "General" in hovered_panel
        assert "Inspect direct child navigation." in hovered_panel
        assert panel.selected_session_id == root_session_id

        await pilot.click(panel, offset=(child_node_x, 0))
        await _wait_until(
            lambda: app._visible_session_id() == child_session_id,
            pilot,
            description="child surface after clicking its navigation node",
        )

        rendered_panel = panel.render_content(panel.content_region.width).plain
        root_node_x = rendered_panel.rfind("■ | ")
        await pilot.click(panel, offset=(root_node_x, 0))
        await _wait_until(
            lambda: app._visible_session_id() == root_session_id,
            pilot,
            description="root surface after clicking its navigation node",
        )


@pytest.mark.asyncio
async def test_fork_projects_animated_activity_until_the_branch_is_created(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application(tmp_path)
    session = application.runtime.sessions.create(
        session_id="ses_fork_activity",
        title="Fork activity",
    ).session
    application.runtime.store.append_message(
        MessageRecord.text(
            session_id=session.session_id,
            role="user",
            text="Fork this conversation",
            sequence=1,
        )
    )
    app = OuroTuiApp(
        application=application,
        resume_session_id=session.session_id,
    )

    async with app.run_test(size=(100, 34)) as pilot:
        await _wait_for_active_session(app, pilot, session.session_id)
        original_branch = app._controller.branch_session
        branch_started = asyncio.Event()
        release_branch = asyncio.Event()

        async def delayed_branch(message_id: str | None = None):
            branch_started.set()
            await release_branch.wait()
            return await original_branch(message_id)

        monkeypatch.setattr(app._controller, "branch_session", delayed_branch)
        app._fork_session("full")
        await asyncio.wait_for(branch_started.wait(), timeout=2)
        await pilot.pause()

        indicator = app.query_one(ActivityIndicator)
        assert indicator._activity == "forking"
        assert "Forking session" in indicator.render().plain
        assert indicator.render().plain.strip()[0] in DEFAULT_TUI_THEME.spinner_frames
        frame = indicator._frame
        indicator._tick()
        assert indicator._frame != frame

        await pilot.press("ctrl+s")
        await _wait_for_screen(app, ChoiceScreen, title="SESSIONS")
        assert indicator._activity == "forking"
        assert "Forking session" in indicator.render().plain
        menu_frame = indicator._frame
        await pilot.pause(0.2)
        assert indicator._frame != menu_frame

        release_branch.set()
        await _wait_for_worker_group(app, pilot, group="session-fork")
        assert app._controller.session_id != session.session_id
        assert not isinstance(app.screen, ChoiceScreen)
        assert "Ready" in indicator.render().plain

        branch_session_id = app._controller.session_id

        async def fail_branch(_message_id: str | None = None):
            raise PersistenceError("branch storage unavailable")

        monkeypatch.setattr(app._controller, "branch_session", fail_branch)
        app._fork_session("full")
        await _wait_for_worker_group(app, pilot, group="session-fork")

        assert app._controller.session_id == branch_session_id
        assert app._activity_override is None
        assert "Ready" in indicator.render().plain


@pytest.mark.asyncio
async def test_agent_node_navigation_finishes_current_load_before_latest_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application(tmp_path)
    root_session_id, child_session_id = _persist_direct_child_session(application)
    app = OuroTuiApp(
        application=application,
        resume_session_id=root_session_id,
    )

    async with app.run_test(size=(100, 32)) as pilot:
        await _wait_until(
            lambda: app._visible_session_id() == root_session_id,
            pilot,
            description="root surface before coalesced agent navigation",
        )
        original_open_child = app._open_child_session
        original_show_main = app._show_main_session
        release_child = asyncio.Event()
        child_started = asyncio.Event()
        child_finished = asyncio.Event()
        main_started = asyncio.Event()
        order: list[str] = []
        active_loads = 0
        peak_active_loads = 0

        async def delayed_open_child(agent: AgentTreeNode) -> None:
            nonlocal active_loads, peak_active_loads
            active_loads += 1
            peak_active_loads = max(peak_active_loads, active_loads)
            order.append(agent.session_id)
            child_started.set()
            try:
                await release_child.wait()
                await original_open_child(agent)
                child_finished.set()
            finally:
                active_loads -= 1

        async def observed_show_main() -> None:
            nonlocal active_loads, peak_active_loads
            active_loads += 1
            peak_active_loads = max(peak_active_loads, active_loads)
            order.append(root_session_id)
            main_started.set()
            try:
                await original_show_main()
            finally:
                active_loads -= 1

        monkeypatch.setattr(app, "_open_child_session", delayed_open_child)
        monkeypatch.setattr(app, "_show_main_session", observed_show_main)

        app.post_message(events.Key("alt+right", None))
        await asyncio.wait_for(child_started.wait(), timeout=2)
        app.post_message(events.Key("alt+up", None))
        await pilot.pause()

        assert main_started.is_set() is False
        release_child.set()
        await _wait_for_worker_group(app, pilot, group="navigation")

        assert child_finished.is_set() is True
        assert order == [child_session_id, root_session_id]
        assert peak_active_loads == 1
        assert app._visible_session_id() == root_session_id


@pytest.mark.asyncio
async def test_startup_resume_preserves_canonical_child_read_only_state(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    root_session_id, child_session_id = _persist_direct_child_session(application)
    app = OuroTuiApp(
        application=application,
        resume_session_id=child_session_id,
    )

    async with app.run_test(size=(100, 32)) as pilot:
        await _wait_until(
            lambda: app._visible_session_id() == child_session_id,
            pilot,
            description="startup child session surface",
        )

        composer = app.query_one(Composer)
        panel = app.query_one(SubagentNavigationPanel)
        assert app._controller.session_id == root_session_id
        assert app._visible_read_only is True
        assert composer.disabled is True
        assert composer.has_class("read-only-session")
        assert panel.display is True
        assert panel.current_session_id == child_session_id


@pytest.mark.asyncio
async def test_session_picker_restores_history_after_returning_to_welcome(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    session = application.runtime.sessions.create(title="Reusable session").session
    application.runtime.store.append_message(
        MessageRecord.text(
            session_id=session.session_id,
            role="user",
            text="Persisted user message",
            sequence=1,
        )
    )
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+s")
        await _wait_for_screen(app, ChoiceScreen, title="SESSIONS")
        await pilot.press("enter")
        await _wait_for_active_session(app, pilot, session.session_id)
        assert app.query_one(ConversationHistory).user_turns == ("Persisted user message",)

        await pilot.press("ctrl+n")
        await _wait_until(
            lambda: app.query_one(ConversationView).showing_welcome,
            pilot,
            description="new-session welcome page",
        )
        await pilot.press("ctrl+s")
        await _wait_for_screen(app, ChoiceScreen, title="SESSIONS")
        await pilot.press("enter")
        await _wait_for_active_session(app, pilot, session.session_id)

        history = app.query_one(ConversationHistory)
        assert history.user_turns == ("Persisted user message",)
        assert "Persisted user message" in history.rendered_text


@pytest.mark.asyncio
async def test_input_drafts_restore_per_surface_across_tui_restarts(tmp_path: Path) -> None:
    application = _application(tmp_path)
    first = application.runtime.sessions.create(session_id="ses_first_draft", title="First").session
    second = application.runtime.sessions.create(
        session_id="ses_second_draft",
        title="Second",
    ).session
    for session, text in ((first, "First message"), (second, "Second message")):
        application.runtime.store.append_message(
            MessageRecord.text(
                session_id=session.session_id,
                role="user",
                text=text,
                sequence=1,
            )
        )

    draft_store = TuiInputDraftStore(tmp_path)
    seeded = (
        TuiInputDrafts()
        .remember(None, "welcome seed")
        .remember(first.session_id, "first seed")
        .remember(second.session_id, "second seed")
    )
    draft_store.save(seeded)

    app = OuroTuiApp(application=application)
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        assert composer.text == "welcome seed"

        composer.load_text("welcome edited")
        await _wait_until(
            lambda: draft_store.load().draft_for(None) == "welcome edited",
            pilot,
            description="persisted welcome draft edit",
        )

        await app._open_session(first.session_id)
        await pilot.pause()
        assert composer.text == "first seed"
        composer.load_text("first edited")
        await _wait_until(
            lambda: draft_store.load().draft_for(first.session_id) == "first edited",
            pilot,
            description="persisted first Session draft edit",
        )

        await app._open_session(second.session_id)
        await pilot.pause()
        assert composer.text == "second seed"
        composer.load_text("second edited")
        await _wait_until(
            lambda: draft_store.load().draft_for(second.session_id) == "second edited",
            pilot,
            description="persisted second Session draft edit",
        )

        await app._open_session(first.session_id)
        await pilot.pause()
        assert composer.text == "first edited"
        await app._new_session()
        await pilot.pause()
        assert composer.text == "welcome edited"

    restarted = OuroTuiApp(application=_application(tmp_path))
    async with restarted.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        composer = restarted.query_one(Composer)
        assert composer.text == "welcome edited"

        await restarted._open_session(first.session_id)
        await pilot.pause()
        assert composer.text == "first edited"
        composer.clear()
        await _wait_until(
            lambda: draft_store.load().draft_for(first.session_id) == "",
            pilot,
            description="removed first Session draft after editing it empty",
        )

        await restarted._open_session(second.session_id)
        await pilot.pause()
        assert composer.text == "second edited"
        await pilot.press("enter")
        await _wait_for_worker_group(restarted, pilot, group="run")
        await _wait_until(
            lambda: draft_store.load().draft_for(second.session_id) == "",
            pilot,
            description="removed submitted Session draft",
        )

    resumed = OuroTuiApp(
        application=_application(tmp_path),
        resume_session_id=first.session_id,
    )
    async with resumed.run_test(size=(100, 32)) as pilot:
        await _wait_for_active_session(resumed, pilot, first.session_id)
        assert resumed.query_one(Composer).text == ""
        await resumed._new_session()
        await pilot.pause()
        assert resumed.query_one(Composer).text == "welcome edited"


@pytest.mark.asyncio
async def test_input_draft_persistence_debounces_to_the_latest_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tui_app_module,
        "_INPUT_DRAFT_SAVE_DEBOUNCE_SECONDS",
        0.1,
        raising=False,
    )
    app = OuroTuiApp(application=_application(tmp_path))
    saved: list[TuiInputDrafts] = []

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        if app._input_draft_save_task is not None:
            await app._input_draft_save_task
        monkeypatch.setattr(app._input_draft_store, "save", saved.append)

        app._remember_input_draft(None, "a")
        await asyncio.sleep(0.04)
        assert saved == []

        app._remember_input_draft(None, "ab")
        await asyncio.sleep(0.04)
        app._remember_input_draft(None, "abc")
        await asyncio.sleep(0.14)

        assert len(saved) == 1
        assert saved[0].draft_for(None) == "abc"


@pytest.mark.asyncio
async def test_tui_close_waits_for_pending_input_draft_save(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tui_app_module, "_INPUT_DRAFT_SAVE_DEBOUNCE_SECONDS", 0.05)
    app = OuroTuiApp(application=_application(tmp_path))
    saved: list[TuiInputDrafts] = []

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        if app._input_draft_save_task is not None:
            await app._input_draft_save_task
        monkeypatch.setattr(app._input_draft_store, "save", saved.append)
        app._remember_input_draft(None, "saved while closing")

    assert len(saved) == 1
    assert saved[0].draft_for(None) == "saved while closing"


@pytest.mark.asyncio
async def test_input_draft_save_failure_allows_a_later_revision_to_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tui_app_module, "_INPUT_DRAFT_SAVE_DEBOUNCE_SECONDS", 0.01)
    app = OuroTuiApp(application=_application(tmp_path))
    save_attempts = 0
    saved: list[TuiInputDrafts] = []

    def save(drafts: TuiInputDrafts) -> None:
        nonlocal save_attempts
        save_attempts += 1
        if save_attempts == 1:
            raise PersistenceError("draft storage unavailable")
        saved.append(drafts)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        if app._input_draft_save_task is not None:
            await app._input_draft_save_task
        monkeypatch.setattr(app._input_draft_store, "save", save)

        app._remember_input_draft(None, "first revision")
        await _wait_until(
            lambda: app._input_draft_save_task is None,
            pilot,
            description="failed draft save task to settle",
        )
        assert app._input_draft_error_notified is True

        app._remember_input_draft(None, "retry revision")
        await _wait_until(
            lambda: bool(saved),
            pilot,
            description="later draft revision to retry persistence",
        )

    assert save_attempts == 2
    assert saved[0].draft_for(None) == "retry revision"


@pytest.mark.asyncio
async def test_session_load_projects_loading_activity_until_history_is_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application(tmp_path)
    session = application.runtime.sessions.create(title="Long session").session
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        load_started = asyncio.Event()
        release_load = asyncio.Event()
        load_session = app._controller.load_session

        async def slow_load(session_id: str) -> LoadedSession:
            load_started.set()
            await release_load.wait()
            return await load_session(session_id)

        monkeypatch.setattr(app._controller, "load_session", slow_load)
        loading = asyncio.create_task(app._open_session(session.session_id))
        await asyncio.wait_for(load_started.wait(), timeout=1)
        await pilot.pause()

        indicator = app.query_one(StatusLine).query_one(ActivityIndicator)
        assert "Loading session" in indicator.render().plain

        release_load.set()
        await asyncio.wait_for(loading, timeout=2)
        await pilot.pause()
        assert "Ready" in indicator.render().plain


@pytest.mark.asyncio
async def test_session_picker_contains_load_failure_in_choice_dialog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application(tmp_path)
    session = application.runtime.sessions.create(title="Damaged session").session
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()

        async def fail_load(_session_id: str) -> None:
            raise PersistenceError("message journal is invalid")

        monkeypatch.setattr(app._controller, "load_session", fail_load)
        await pilot.press("ctrl+s")
        await _wait_for_screen(app, ChoiceScreen, title="SESSIONS")
        session_menu = app.screen

        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, ChoiceScreen)
        assert app.screen is not session_menu
        assert app.screen._title == "SESSION LOAD FAILED"
        error = app.screen._choices[0].description
        assert session.session_id in error
        assert "message journal is invalid" in error
        assert app.screen._choices[0].description_accent == DEFAULT_TUI_THEME.failure
        assert "Ready" in app.query_one(ActivityIndicator).render().plain

        await pilot.press("escape")
        await pilot.pause()
        assert session_menu not in app.screen_stack
        assert not isinstance(app.screen, ChoiceScreen)
        assert app.is_running is True


@pytest.mark.asyncio
async def test_startup_resume_failure_opens_choice_dialog_and_keeps_tui_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application(tmp_path)
    session = application.runtime.sessions.create(title="Damaged startup session").session
    app = OuroTuiApp(
        application=application,
        resume_session_id=session.session_id,
    )

    async def fail_load(_session_id: str) -> None:
        raise PersistenceError("session payload is incompatible")

    monkeypatch.setattr(app._controller, "load_session", fail_load)
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()

        assert isinstance(app.screen, ChoiceScreen)
        assert app.screen._title == "SESSION LOAD FAILED"
        error = app.screen._choices[0].description
        assert session.session_id in error
        assert "session payload is incompatible" in error

        await pilot.press("enter")
        await pilot.pause()
        assert not isinstance(app.screen, ChoiceScreen)
        assert app.query_one(Composer).disabled is False
        assert app.is_running is True


@pytest.mark.asyncio
async def test_session_picker_and_archive_menu_project_and_toggle_archived_group(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    active = application.runtime.sessions.create(
        session_id="ses_active",
        title="Active session",
    ).session
    archived = application.runtime.sessions.create(
        session_id="ses_archived",
        title="Archived session",
    ).session
    application.runtime.sessions.set_archived(archived.session_id, True)
    application.runtime.store.append_message(
        MessageRecord.text(
            session_id=active.session_id,
            role="user",
            text="Visible active session",
            sequence=1,
        )
    )
    app = OuroTuiApp(
        application=application,
        resume_session_id=active.session_id,
    )

    async with app.run_test(size=(100, 34), notifications=True) as pilot:
        await _wait_for_active_session(app, pilot, active.session_id)
        await pilot.press("ctrl+s")
        await _wait_for_screen(app, ChoiceScreen, title="SESSIONS")
        groups = {choice.value: choice.group for choice in app.screen._choices}
        assert groups[active.session_id] == "Today"
        assert groups[archived.session_id] == "Archived"
        await pilot.press("escape")

        await pilot.press("ctrl+p")
        await pilot.pause()
        app.screen.query_one("#choice-filter", Input).value = "Archive session"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        archive_screen = app.screen
        initial = {choice.value: choice for choice in archive_screen._choices}
        assert initial[active.session_id].marker == DEFAULT_TUI_THEME.selected_choice_marker
        assert initial[archived.session_id].marker == ""

        archive_screen.query_one("#choice-filter", Input).value = "Active session"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen is archive_screen
        choices = {choice.value: choice for choice in archive_screen._choices}
        assert choices[active.session_id].group == "Archived"
        assert choices[archived.session_id].group == "Archived"
        assert choices[active.session_id].marker == DEFAULT_TUI_THEME.selected_choice_marker

        archive_screen.query_one("#choice-filter", Input).value = "Archived session"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen is archive_screen
        choices = {choice.value: choice for choice in archive_screen._choices}
        assert choices[archived.session_id].group == "Today"
        catalog = {item.session_id: item for item in application.runtime.sessions.catalog().entries}
        assert catalog[active.session_id].archived is True
        assert catalog[archived.session_id].archived is False
        assert not list(app.screen.query_one("#textual-toastrack").query("Toast"))
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_rename_session_menu_edits_active_session_inline_and_stays_open(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    active = application.runtime.sessions.create(
        session_id="ses_rename_active",
        title="Original title",
    ).session
    archived = application.runtime.sessions.create(
        session_id="ses_rename_archived",
        title="Archived title",
    ).session
    application.runtime.sessions.set_archived(archived.session_id, True)
    application.runtime.store.append_message(
        MessageRecord.text(
            session_id=active.session_id,
            role="user",
            text="Visible rename session",
            sequence=1,
        )
    )
    app = OuroTuiApp(
        application=application,
        resume_session_id=active.session_id,
    )

    async with app.run_test(size=(100, 34)) as pilot:
        await _wait_for_active_session(app, pilot, active.session_id)
        await pilot.press("ctrl+p")
        await pilot.pause()
        action_screen = app.screen
        app.screen.query_one("#choice-filter", Input).value = "Rename session"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        rename_screen = app.screen
        assert isinstance(rename_screen, EditableChoiceScreen)
        assert rename_screen.query_one("#choice-hint", Static).render().plain == (
            EditableChoiceScreen.DEFAULT_HINT
        )
        assert action_screen.query_one("#choice-panel", Vertical).display is False
        assert action_screen.styles.background.a == 0
        assert rename_screen.styles.background.a == 0.45
        assert [choice.value for choice in rename_screen._choices] == [active.session_id]
        assert {choice.group for choice in rename_screen._choices} == {"Today"}
        assert rename_screen._choices[0].marker == DEFAULT_TUI_THEME.selected_choice_marker

        rename_screen.query_one("#choice-filter", Input).value = "Original title"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen is rename_screen
        assert rename_screen._editing_value == active.session_id
        assert rename_screen.query_one("#choice-filter", Input).disabled is True
        assert rename_screen.query_one("#choice-hint", Static).render().plain == (
            EditableChoiceScreen.EDIT_HINT
        )
        options = rename_screen.query_one(OptionList)
        assert options.highlighted is not None
        assert isinstance(options.get_option_at_index(options.highlighted).prompt, Text)

        await pilot.press(*(("backspace",) * len(active.title)), "enter")
        await pilot.pause()
        assert app.screen is rename_screen
        assert rename_screen._editing_value == active.session_id
        assert application.runtime.sessions.open(active.session_id).session.title == active.title

        await pilot.press(
            *tuple("Renamed"),
            "space",
            *tuple("title"),
            "enter",
        )
        await pilot.pause()
        assert app.screen is rename_screen
        assert rename_screen._editing_value is None
        assert rename_screen.query_one("#choice-filter", Input).disabled is False
        assert rename_screen.query_one("#choice-filter", Input).has_focus is True
        assert rename_screen.query_one("#choice-hint", Static).render().plain == (
            EditableChoiceScreen.DEFAULT_HINT
        )
        assert rename_screen.query_one("#choice-filter", Input).value == ""
        assert rename_screen._choices[0].edit_value == "Renamed title"
        assert rename_screen._choices[0].marker == DEFAULT_TUI_THEME.selected_choice_marker
        assert application.runtime.sessions.open(active.session_id).session.title == (
            "Renamed title"
        )
        assert app._active_title == "Renamed title"
        highlighted_style = options.get_visual_style(
            "option-list--option",
            "option-list--option-highlighted",
        ).rich_style
        assert highlighted_style.bold is False

        await pilot.press("enter", "home", "x", "escape")
        await pilot.pause()
        assert app.screen is rename_screen
        assert rename_screen._editing_value is None
        assert application.runtime.sessions.open(active.session_id).session.title == (
            "Renamed title"
        )
        highlighted_style = options.get_visual_style(
            "option-list--option",
            "option-list--option-highlighted",
        ).rich_style
        assert highlighted_style.bold is False
        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        assert app.screen is action_screen
        assert action_screen.query_one("#choice-panel", Vertical).display is True
        assert action_screen.styles.background.a == 0.45


@pytest.mark.asyncio
async def test_delete_session_menu_force_deletes_catalog_error_and_quits_cleanly(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    session = application.runtime.sessions.create(
        session_id="ses_delete_catalog_error",
        title="Readable canonical session",
    ).session
    index_path = application.runtime.store._catalog.index_path
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    indexed = next(item for item in payload["sessions"] if item["session_id"] == session.session_id)
    indexed.pop("session_kind")
    index_path.write_text(json.dumps(payload), encoding="utf-8")
    session_dir = application.runtime.store.sessions_dir / session.session_id
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        app.screen.query_one("#choice-filter", Input).value = "Delete session"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, MultiChoiceScreen, title="DELETE SESSIONS")

        choice = next(item for item in app.screen._choices if item.value == session.session_id)
        assert choice.trailing == "index error"

        await pilot.press("enter", "down", "enter")
        await _wait_until(
            lambda: not session_dir.exists(),
            pilot,
            description="catalog-error Session deletion",
        )

        assert application.runtime.sessions.catalog().entries == ()
        assert not list(app.query("Toast"))

        await pilot.press("ctrl+q")
        await pilot.pause()
        assert app.is_running is False


@pytest.mark.asyncio
async def test_delete_session_menu_keeps_current_marker_separate_from_multi_selection(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    current = application.runtime.sessions.create(
        session_id="ses_delete_current",
        title="Current archived session",
    ).session
    other = application.runtime.sessions.create(
        session_id="ses_delete_other",
        title="Other archived session",
    ).session
    application.runtime.sessions.set_archived(current.session_id, True)
    application.runtime.sessions.set_archived(other.session_id, True)
    application.runtime.store.append_message(
        MessageRecord.text(
            session_id=current.session_id,
            role="user",
            text="Visible archived session",
            sequence=1,
        )
    )
    app = OuroTuiApp(
        application=application,
        resume_session_id=current.session_id,
    )

    async with app.run_test(size=(100, 34)) as pilot:
        await _wait_for_active_session(app, pilot, current.session_id)
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        app.screen.query_one("#choice-filter", Input).value = "Delete session"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, MultiChoiceScreen, title="DELETE SESSIONS")

        delete_screen = app.screen
        choices = {choice.value: choice for choice in delete_screen._choices}
        assert choices[current.session_id].marker == DEFAULT_TUI_THEME.selected_choice_marker
        assert choices[other.session_id].marker == ""
        assert (
            choices[current.session_id].selection_marker
            == DEFAULT_TUI_THEME.unselected_choice_marker
        )
        assert (
            choices[other.session_id].selection_marker
            == DEFAULT_TUI_THEME.unselected_choice_marker
        )

        options = delete_screen.query_one("#choice-list", OptionList)
        options.highlighted = options.get_option_index(other.session_id)
        options.focus()
        await pilot.press("space")
        await pilot.pause()

        choices = {choice.value: choice for choice in delete_screen._choices}
        assert choices[current.session_id].marker == DEFAULT_TUI_THEME.selected_choice_marker
        assert (
            choices[current.session_id].selection_marker
            == DEFAULT_TUI_THEME.unselected_choice_marker
        )
        assert (
            choices[other.session_id].selection_marker
            == DEFAULT_TUI_THEME.selected_choice_marker
        )
        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_switching_sessions_clears_the_previous_approval_panel(tmp_path: Path) -> None:
    application = _application(tmp_path)
    destination = application.runtime.sessions.create(
        session_id="ses_destination",
        title="Destination",
    ).session
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        stale = PendingInteraction(
            kind="approval",
            run_id="run_previous",
            tool_call_id="call_previous",
            tool_name="Write",
            request_id="approval_previous",
            session_id="ses_previous",
        )
        app._interaction = stale
        app._sync_interaction(stale)
        assert app.query_one(InteractionBar).display is True

        await app._open_session(destination.session_id)
        await pilot.pause()

        assert app._projection is not None
        assert app._projection.session_id == destination.session_id
        assert app._active_title == "Destination"
        assert str(tmp_path)[:20] in app.query_one(BrandHeader).render().plain
        assert app._interaction is None
        assert app.query_one(InteractionBar).display is False


@pytest.mark.asyncio
async def test_endpoint_configuration_screen_is_provider_scoped_and_validates_draft(
    tmp_path: Path,
) -> None:
    configuration = ConfigurationFacade(tmp_path)
    configuration.add_provider(
        "custom",
        provider_type="openai-compatible",
        base_url="https://example.invalid/v1",
    )
    provider = configuration.provider("custom")
    app = OuroTuiApp(application=_application(tmp_path))
    results: list[TuiEndpointConfiguration | None] = []

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        screen = EndpointConfigurationScreen(provider)
        app.push_screen(screen, callback=results.append)
        await pilot.pause()

        assert isinstance(screen, EditableChoiceScreen)
        assert not list(screen.query("#model-provider"))
        assert not list(screen.query("Button"))
        assert not list(screen.query("Checkbox"))
        assert screen.query_one("#choice-filter", Input).placeholder == (
            "Filter endpoint settings…"
        )
        assert [choice.group for choice in screen._choices] == [
            "IDENTITY",
            "IDENTITY",
            "LIMITS",
            "LIMITS",
            "CAPABILITIES",
            "CAPABILITIES",
            "CAPABILITIES",
            "CAPABILITIES",
            "REASONING",
            "REASONING",
            "REQUEST",
            "ACTIONS",
        ]
        assert screen._capabilities["endpoint-tools"] is False

        options = screen.query_one("#choice-list", OptionList)
        for field_id, value in (
            ("endpoint-id", "new-endpoint"),
            ("endpoint-model-id", "provider-model"),
            ("endpoint-context-window", "100"),
            ("endpoint-max-output", "200"),
        ):
            options.highlighted = options.get_option_index(field_id)
            options.focus()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("ctrl+u", *value, "enter")
            await pilot.pause()

        assert results == []
        assert options.get_option("endpoint-save").disabled is True
        save_choice = next(choice for choice in screen._choices if choice.value == "endpoint-save")
        assert "cannot exceed" in save_choice.trailing

        options.highlighted = options.get_option_index("endpoint-max-output")
        options.focus()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("ctrl+u", *"50", "enter")
        await pilot.pause()

        options.highlighted = options.get_option_index("endpoint-vision")
        options.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert screen._capabilities["endpoint-vision"] is True
        assert screen._capabilities["endpoint-streaming"] is True

        screen._values["endpoint-reasoning-config"] = "[]"
        screen._choices = screen._endpoint_choices()
        screen._refresh_options(needle="", preferred_value="endpoint-reasoning-config")
        assert options.get_option("endpoint-save").disabled is True
        save_choice = next(choice for choice in screen._choices if choice.value == "endpoint-save")
        assert "JSON object" in save_choice.trailing

        options.highlighted = options.get_option_index("endpoint-reasoning-preset")
        options.focus()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="REASONING PRESET")
        preset_options = app.screen.query_one("#choice-list", OptionList)
        assert preset_options.get_option("anthropic-adaptive-effort").disabled is True
        incompatible = next(
            choice for choice in app.screen._choices if choice.value == "anthropic-adaptive-effort"
        )
        assert incompatible.trailing == "Protocol incompatible"
        preset_options.highlighted = preset_options.get_option_index("thinking-toggle")
        preset_options.focus()
        await pilot.press("enter")
        await _wait_for_screen(app, EndpointConfigurationScreen, title="ADD ENDPOINT")
        assert screen._reasoning_preset_name() == "thinking-toggle"
        assert screen._draft().reasoning == reasoning_preset("thinking-toggle")
        assert options.get_option("endpoint-save").disabled is False

        screen._values["endpoint-reasoning-config"] = (
            '{"capability":{"activation":"toggle","default":{"mode":"enabled"}},'
            '"profile":{"name":"vendor-thinking","variants":'
            '{"enabled":{"thinking":true},"disabled":{"thinking":false}}}}'
        )
        screen._choices = screen._endpoint_choices()
        screen._refresh_options(needle="", preferred_value="endpoint-reasoning-config")
        assert screen._reasoning_preset_name() is None
        assert options.get_option("endpoint-save").disabled is False

        screen._values["endpoint-request-options"] = '{"thinking":true}'
        screen._choices = screen._endpoint_choices()
        screen._refresh_options(needle="", preferred_value="endpoint-request-options")
        assert options.get_option("endpoint-save").disabled is True
        save_choice = next(choice for choice in screen._choices if choice.value == "endpoint-save")
        assert "reasoning-owned fields: thinking" in save_choice.trailing

        screen._values["endpoint-request-options"] = "[]"
        screen._choices = screen._endpoint_choices()
        screen._refresh_options(needle="", preferred_value="endpoint-request-options")
        assert options.get_option("endpoint-save").disabled is True
        save_choice = next(choice for choice in screen._choices if choice.value == "endpoint-save")
        assert "JSON object" in save_choice.trailing

        screen._values["endpoint-request-options"] = '{"seed":7}'
        screen._choices = screen._endpoint_choices()
        screen._refresh_options(needle="", preferred_value="endpoint-save")
        assert options.get_option("endpoint-save").disabled is False

        options.highlighted = options.get_option_index("endpoint-save")
        await pilot.press("enter")
        await pilot.pause()

        assert len(results) == 1
        draft = results[0]
        assert draft is not None
        assert draft.provider_id == "custom"
        assert draft.endpoint_id == "new-endpoint"
        assert draft.model_id == "provider-model"
        assert draft.context_window == 100
        assert draft.max_output_tokens == 50
        assert draft.supports_tools is False
        assert draft.supports_vision is True
        assert draft.supports_streaming is True
        assert draft.reasoning is not None
        assert draft.reasoning.profile is not None
        assert draft.reasoning.profile.name == "vendor-thinking"
        assert draft.request_options == {"seed": 7}


@pytest.mark.asyncio
async def test_session_settings_can_be_completed_with_keyboard_only(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_thinking_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.press("slash", *"approval", "enter", *"afk", "enter")
        await pilot.pause()
        assert await app._controller.session_approval_preset() == "afk"

        await pilot.press("slash", *"thinking", "enter", *"high", "enter")
        await pilot.pause()
        reasoning = await app._controller.session_reasoning()
        assert reasoning.requested.effort == "high"
        assert reasoning.effective.effort == "high"


@pytest.mark.asyncio
async def test_session_setting_shortcuts_cycle_and_wrap_available_values(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_thinking_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()

        for expected in (
            ReasoningRequest(mode="enabled"),
            ReasoningRequest(mode="disabled"),
            ReasoningRequest(mode="enabled", effort="low"),
            ReasoningRequest(mode="enabled", effort="high"),
            ReasoningRequest(),
        ):
            await pilot.press("alt+t")
            await pilot.pause()
            assert (await app._controller.session_reasoning()).requested == expected

        for expected in ("auto", "approval", "deny", "afk", "inherit"):
            await pilot.press("alt+a")
            await pilot.pause()
            assert await app._controller.session_approval_preset() == expected

        assert app.query_one(Composer).text == ""


@pytest.mark.asyncio
async def test_thinking_completion_and_modal_share_supported_choices(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_thinking_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.press("slash", *"thinking", "space")
        await pilot.pause()

        inline = (
            app.query_one(CommandPopup).query_one("#command-popup-content", Static).render().plain
        )
        for value in ("default", "enabled", "disabled", "low", "high"):
            assert value in inline
        for value in ("minimal", "medium", "xhigh", "max"):
            assert value not in inline

        app.query_one(Composer).clear()
        app.action_thinking_settings()
        await pilot.pause()
        modal = {choice.value for choice in app.screen._choices}
        for value in ("default", "enabled", "disabled", "low", "high"):
            assert value in modal
        for value in ("minimal", "medium", "xhigh", "max"):
            assert value not in modal


@pytest.mark.asyncio
async def test_thinking_modal_remains_available_during_an_active_run(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_thinking_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.press(*"start", "enter")
        await _wait_until(
            lambda: app._projection is not None and app._projection.activity.status == "completed",
            pilot,
            description="completed initial run",
        )
        assert app._projection is not None
        app._projection.activity = app._projection.activity.model_copy(update={"status": "running"})

        app.action_thinking_settings()
        await _wait_for_screen(app, ChoiceScreen, title="MODEL THINKING")
        app.screen.query_one("#choice-filter", Input).value = "high"
        await pilot.press("enter")
        await pilot.pause()

        reasoning = await app._controller.session_reasoning()
        assert reasoning.requested.effort == "high"


@pytest.mark.asyncio
async def test_status_line_shows_thinking_level_and_highlights_peak_effort(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_thinking_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        indicator = app.query_one(ActivityIndicator)

        # The endpoint default effort is low — not the highest supported — so
        # the level is shown without the peak highlight.
        assert indicator._reasoning == "low"
        assert indicator._reasoning_peak is False
        assert "low" in indicator.render().plain

        # Raising to high (the highest effort this endpoint supports) peaks the
        # indicator so the level is highlighted.
        await pilot.press("slash", *"thinking", "enter", *"high", "enter")
        await pilot.pause()
        assert indicator._reasoning == "high"
        assert indicator._reasoning_peak is True
        assert "high" in indicator.render().plain


@pytest.mark.asyncio
async def test_status_line_hides_thinking_level_when_reasoning_unsupported(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        indicator = app.query_one(ActivityIndicator)
        # The default faux endpoint declares no reasoning capability, so no
        # thinking level is projected onto the status line.
        assert indicator._reasoning is None


@pytest.mark.asyncio
async def test_status_line_shows_thinking_when_enabled_without_effort(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_thinking_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        indicator = app.query_one(ActivityIndicator)
        # The endpoint default resolves to enabled + low, shown as "low".
        assert indicator._reasoning == "low"

        # Switching to enabled without an effort shows the generic "thinking".
        await pilot.press("slash", *"thinking", "enter", *"enabled", "enter")
        await pilot.pause()
        assert indicator._reasoning == "thinking"
        assert indicator._reasoning_peak is False
        assert "thinking" in indicator.render().plain


@pytest.mark.asyncio
async def test_status_line_hides_thinking_when_disabled(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_thinking_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        indicator = app.query_one(ActivityIndicator)
        active_separator_count = indicator.render().plain.count(" • ")

        await pilot.press("slash", *"thinking", "enter", *"disabled", "enter")
        await pilot.pause()
        assert indicator._reasoning is None
        # Disabling reasoning removes the trailing separator so the status line
        # shows only the activity, approval mode, and model endpoint.
        assert indicator.render().plain.count(" • ") == active_separator_count - 1


@pytest.mark.asyncio
async def test_status_line_default_reflects_endpoint_default(tmp_path: Path) -> None:
    # An endpoint whose default is disabled shows nothing under a default intent.
    store = LocalSessionStore(tmp_path)
    store.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(),
            ModelMetadata(
                model_id="thinking-model",
                provider_id="faux",
                reasoning=ReasoningCapability(
                    activation="toggle",
                    supported_efforts=("low", "high"),
                    default=ReasoningRequest(mode="disabled"),
                    output="summary",
                ),
            ),
        ),
        tools=ToolRegistry(),
        default_workspace=tmp_path,
        environment=RuntimeEnvironment.local(tmp_path, store.sessions_dir),
    )
    app = OuroTuiApp(application=LocalApplication.from_runtime(runtime))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        indicator = app.query_one(ActivityIndicator)
        # The default intent resolves to the endpoint's disabled default, so the
        # thinking indicator stays hidden.
        assert indicator._reasoning is None


@pytest.mark.asyncio
async def test_actions_groups_granular_settings_by_function_without_descriptions(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()

        await pilot.press("ctrl+p")
        await pilot.pause()
        action_choices = app.screen._choices
        action_values = {choice.value for choice in action_choices}
        assert "/thinking" not in action_values
        assert "/compact" not in action_values
        assert [choice.label for choice in action_choices] == [
            "Theme",
            "Model thought display",
            "Input draft memory",
            "Queued input delivery",
            "Compaction policy",
            "New session",
            "Open session",
            "Rename session",
            "Archive session",
            "Delete session",
            "List providers",
            "Add provider",
            "Edit provider",
            "Remove provider",
            "List endpoints",
            "Add endpoint",
            "Edit endpoint",
            "Set default endpoint",
            "Remove endpoint",
            "List plugins",
            "Install plugin",
            "Remove plugin",
            "Tool permissions",
            "Execution & path access",
            "Dispatch policy",
            "List subagents",
            "Create subagent",
            "Edit subagent",
            "Delete subagent",
            "List MCP servers",
            "Add MCP server",
            "Connect MCP server",
            "Disconnect MCP server",
            "Edit MCP server",
            "Remove MCP server",
            "Skill sources",
            "List skills",
            "Install skill",
            "Validate skill",
            "Update skill",
            "Remove skill",
            "Keyboard help",
        ]
        assert [choice.group for choice in action_choices] == [
            *("Settings",) * 5,
            *("Session",) * 5,
            *("Providers",) * 9,
            *("Plugins",) * 3,
            *("Tools",) * 2,
            *("Subagents",) * 5,
            *("MCP",) * 6,
            *("Skills",) * 6,
            "Support",
        ]
        assert "Configuration" not in {choice.group for choice in action_choices}
        assert "Recent" not in {choice.group for choice in action_choices}
        assert all(choice.description == "" for choice in action_choices)
        assert {choice.label: choice.trailing for choice in action_choices if choice.trailing} == {
            "New session": "Ctrl+N",
            "Open session": "Ctrl+S",
            "List plugins": "Alt+P",
            "Keyboard help": "Ctrl+H",
        }

        app.screen.query_one("#choice-filter", Input).value = "Compaction policy"
        await pilot.pause()
        await pilot.press("enter", "enter")
        await pilot.pause(0.2)
        assert isinstance(app.screen, CompactionConfigurationScreen)
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_queued_input_delivery_uses_shared_settings_menu_and_persists_mode(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with app.run_test(size=(100, 34), notifications=True) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await pilot.pause()
        app.screen.query_one("#choice-filter", Input).value = "Queued input delivery"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="QUEUED INPUT DELIVERY")

        screen = app.screen
        assert [choice.value for choice in screen._choices] == [
            "one_by_one",
            "all_at_once",
        ]
        assert [choice.trailing for choice in screen._choices] == [
            "one unit per boundary",
            "all eligible units per boundary",
        ]
        assert screen._choices[0].marker == DEFAULT_TUI_THEME.selected_choice_marker
        assert screen._choices[1].marker == ""

        screen.query_one("#choice-filter", Input).value = "All at once"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_until(
            lambda: (
                ConfigurationFacade(tmp_path).queued_input_config().consumption_mode
                == "all_at_once"
            ),
            pilot,
            description="queued input delivery mode to persist",
        )

        assert app.screen is screen
        assert screen._choices[0].marker == ""
        assert screen._choices[1].marker == DEFAULT_TUI_THEME.selected_choice_marker


@pytest.mark.asyncio
async def test_shared_menu_applies_responsive_height_before_mount_layout(
    tmp_path: Path,
) -> None:
    class MountObservedChoiceScreen(ChoiceScreen):
        list_max_height_at_mount: int | None = None

        def on_mount(self) -> None:
            max_height = self.query_one("#choice-list", OptionList).styles.max_height
            assert max_height is not None
            self.list_max_height_at_mount = max_height.cells
            super().on_mount()

    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(48, 18)) as pilot:
        await pilot.pause()
        screen = MountObservedChoiceScreen(
            title="ITEMS",
            placeholder="Filter items…",
            choices=tuple(Choice(str(index), f"Item {index}") for index in range(30)),
        )
        app.push_screen(screen)
        await pilot.pause()

        assert screen.list_max_height_at_mount == 7
        assert screen.query_one("#choice-list", OptionList).styles.max_height.cells == 7

        await pilot.press("escape")
        await pilot.pause()


@pytest.mark.asyncio
async def test_shared_menu_height_tracks_terminal_with_bounded_layout(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 60)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")

        panel = app.screen.query_one("#choice-panel", Vertical)
        choices = app.screen.query_one("#choice-list", OptionList)
        hint = app.screen.query_one("#choice-hint", Static)
        assert app.screen.styles.overflow_y == "hidden"
        assert panel.region.height == 27
        assert panel.region.x == app.screen.region.width - panel.region.right
        assert panel.region.bottom <= app.screen.region.height
        assert choices.region.bottom <= panel.region.bottom
        assert hint.region.bottom <= panel.region.bottom

        app.screen.query_one("#choice-filter", Input).value = "Keyboard help"
        await pilot.pause()
        assert panel.region.height == 12
        app.screen.query_one("#choice-filter", Input).value = ""
        await pilot.pause()

        await pilot.resize_terminal(48, 18)
        await pilot.pause()
        assert 12 <= panel.region.height < 27
        assert panel.region.x == app.screen.region.width - panel.region.right
        assert choices.region.height >= 5
        assert panel.region.bottom <= app.screen.region.height
        assert hint.region.bottom <= panel.region.bottom

        await pilot.resize_terminal(48, 12)
        await pilot.pause()
        assert panel.region.height == 12
        assert panel.region.x == app.screen.region.width - panel.region.right
        assert choices.region.height == 5
        assert panel.region.bottom <= app.screen.region.height
        assert hint.region.bottom <= panel.region.bottom


@pytest.mark.asyncio
async def test_actions_opens_subagent_dispatch_policy(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await pilot.pause()

        app.screen.query_one("#choice-filter", Input).value = "Dispatch policy"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(
            app,
            AgentDispatchPolicyScreen,
            title="SUBAGENT DISPATCH POLICY",
        )
        screen = app.screen
        assert isinstance(screen, AgentDispatchPolicyScreen)
        screen._replace_choices(
            screen._policy_choices(),
            selected="agent-policy-save",
        )
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        assert not list(app.query("Toast"))
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_actions_lists_the_unified_subagent_catalog(tmp_path: Path) -> None:
    application = _application(tmp_path)
    assert application.agent_definitions is not None
    application.agent_definitions.create(
        agent_id="review-code",
        name="Code reviewer",
        description="Review a bounded code change.",
        system_prompt="Review the change.",
        tool_selection=ToolSelection(mode="all"),
        tools=application.runtime.tools,
        expected_revision=0,
    )
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        app.screen.query_one("#choice-filter", Input).value = "List subagents"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="SUBAGENTS")

        general = next(choice for choice in app.screen._choices if choice.value == "general")
        assert general.group == "BUILT-IN"
        assert general.trailing == "general • available"
        assert general.disabled is True
        custom = next(choice for choice in app.screen._choices if choice.value == "review-code")
        assert custom.group == "USER"
        assert custom.disabled is False

        options = app.screen.query_one("#choice-list", OptionList)
        options.highlighted = options.get_option_index("review-code")
        options.focus()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="SUBAGENT • Code reviewer")
        assert [choice.label for choice in app.screen._choices] == [
            "Edit subagent",
            "Delete subagent",
        ]
        assert not list(app.query("Toast"))

        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="SUBAGENTS")
        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_subagent_edit_and_delete_menus_show_disabled_builtin_group(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")

        for label, title in (
            ("Edit subagent", "EDIT SUBAGENT"),
            ("Delete subagent", "DELETE SUBAGENT"),
        ):
            app.screen.query_one("#choice-filter", Input).value = label
            await pilot.pause()
            await pilot.press("enter")
            await _wait_for_screen(app, ChoiceScreen, title=title)
            general = next(choice for choice in app.screen._choices if choice.value == "general")
            assert general.group == "BUILT-IN"
            assert general.disabled is True
            assert not list(app.query("Toast"))
            await pilot.press("escape")
            await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")

        await pilot.press("escape")


@pytest.mark.asyncio
async def test_subagent_crud_saves_without_success_notifications(tmp_path: Path) -> None:
    application = _application(tmp_path)
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        app.screen.query_one("#choice-filter", Input).value = "Create subagent"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(
            app,
            AgentDefinitionConfigurationScreen,
            title="CREATE SUBAGENT",
        )

        screen = app.screen
        assert isinstance(screen, AgentDefinitionConfigurationScreen)
        screen._values.update(
            {
                "agent-definition-id": "review-code",
                "agent-definition-name": "Code reviewer",
                "agent-definition-description": "Review a bounded code change.",
                "agent-definition-prompt": "Review the change.",
            }
        )
        screen._replace_choices(
            screen._definition_choices(),
            selected="agent-definition-save",
        )
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")

        assert application.agent_definitions is not None
        assert [
            item.agent_id for item in application.agent_definitions.config.snapshot().definitions
        ] == ["review-code"]
        assert not list(app.query("Toast"))

        app.screen.query_one("#choice-filter", Input).value = "Edit subagent"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="EDIT SUBAGENT")
        options = app.screen.query_one("#choice-list", OptionList)
        options.highlighted = options.get_option_index("review-code")
        options.focus()
        await pilot.press("enter")
        await _wait_for_screen(
            app,
            AgentDefinitionConfigurationScreen,
            title="EDIT SUBAGENT",
        )
        screen = app.screen
        assert isinstance(screen, AgentDefinitionConfigurationScreen)
        screen._values["agent-definition-name"] = "Senior code reviewer"
        screen._replace_choices(
            screen._definition_choices(),
            selected="agent-definition-save",
        )
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        assert (
            application.agent_definitions.config.snapshot().definitions[0].name
            == "Senior code reviewer"
        )
        assert not list(app.query("Toast"))

        app.screen.query_one("#choice-filter", Input).value = "Delete subagent"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="DELETE SUBAGENT")
        options = app.screen.query_one("#choice-list", OptionList)
        options.highlighted = options.get_option_index("review-code")
        options.focus()
        await pilot.press("enter")
        await _wait_for_screen(
            app,
            ConfirmationScreen,
            title="CONFIRM DELETE SUBAGENT",
        )
        await pilot.press("down", "enter")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        assert not application.agent_definitions.config.snapshot().definitions
        assert not list(app.query("Toast"))
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_actions_opens_provider_scoped_endpoint_configuration(tmp_path: Path) -> None:
    configuration = ConfigurationFacade(tmp_path)
    configuration.add_provider(
        "custom",
        provider_type="openai-compatible",
        base_url="https://example.invalid/v1",
    )
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await pilot.pause()

        app.screen.query_one("#choice-filter", Input).value = "Add endpoint"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen.query_one("#choice-title", Static).render().plain == (
            "ADD ENDPOINT • PROVIDER"
        )

        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, EndpointConfigurationScreen)
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_provider_enable_action_is_owned_by_provider_submenu(tmp_path: Path) -> None:
    configuration = ConfigurationFacade(tmp_path)
    configuration.add_provider(
        "secondary",
        provider_type="openai-compatible",
        base_url="https://secondary.example.invalid/v1",
        enabled=False,
    )
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await pilot.pause()
        app.screen.query_one("#choice-filter", Input).value = "List providers"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        app.screen.query_one("#choice-filter", Input).value = "secondary"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert app.screen.query_one("#choice-title", Static).render().plain == (
            "PROVIDER • secondary"
        )
        toggle = next(choice for choice in app.screen._choices if choice.value == "toggle")
        assert toggle.label == "Enable provider"

        options = app.screen.query_one("#choice-list", OptionList)
        options.highlighted = options.get_option_index("toggle")
        options.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert ConfigurationFacade(tmp_path).provider("secondary").enabled is True
        assert (
            next(choice for choice in app.screen._choices if choice.value == "toggle").label
            == "Disable provider"
        )

        await pilot.press("escape")
        await pilot.pause()
        assert next(
            choice for choice in app.screen._choices if choice.value == "secondary"
        ).trailing.endswith("• enabled")
        await pilot.press("enter")
        await pilot.pause()
        options = app.screen.query_one("#choice-list", OptionList)
        await pilot.click(options, offset=(4, 1))
        await pilot.pause()
        assert ConfigurationFacade(tmp_path).provider("secondary").enabled is False
        assert (
            next(choice for choice in app.screen._choices if choice.value == "toggle").label
            == "Enable provider"
        )

        await pilot.press("escape")
        await pilot.pause()
        assert next(
            choice for choice in app.screen._choices if choice.value == "secondary"
        ).trailing.endswith("• disabled")
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_provider_submenu_updates_from_latest_preset_in_place(tmp_path: Path) -> None:
    configuration = ConfigurationFacade(tmp_path)
    configuration.add_provider("primary", provider_type="openai")
    template_path = tmp_path / "config" / "provider-templates.json"
    root = json.loads(template_path.read_text(encoding="utf-8"))
    template = root["templates"]["openai"]
    template["revision"] = 3
    endpoint = next(item for item in template["endpoints"] if item["endpoint_id"] == "gpt-5.6")
    endpoint["context_window"] = 2_000_000
    template_path.write_text(json.dumps(root), encoding="utf-8")
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await pilot.pause()
        app.screen.query_one("#choice-filter", Input).value = "List providers"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        app.screen.query_one("#choice-filter", Input).value = "primary"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        update = next(choice for choice in app.screen._choices if choice.value == "update-template")
        assert update.label == "Update from preset"
        assert update.trailing == "openai r1"
        options = app.screen.query_one("#choice-list", OptionList)
        options.highlighted = options.get_option_index("update-template")
        options.focus()
        await pilot.press("enter")
        await pilot.pause()

        assert (
            ConfigurationFacade(tmp_path).endpoint("primary", "gpt-5.6").context_window == 2_000_000
        )
        refreshed = next(
            choice for choice in app.screen._choices if choice.value == "update-template"
        )
        assert refreshed.trailing == "openai r3"


@pytest.mark.asyncio
async def test_provider_catalog_refreshes_before_returning_from_submenu(
    tmp_path: Path,
) -> None:
    configuration = ConfigurationFacade(tmp_path)
    configuration.add_provider(
        "custom",
        provider_type="openai-compatible",
        base_url="https://example.invalid/v1",
    )
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        app.screen.query_one("#choice-filter", Input).value = "List providers"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="PROVIDERS")
        menu = app.screen
        options = menu.query_one(OptionList)
        options.highlighted = options.get_option_index("custom")
        options.focus()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="PROVIDER • custom")

        await app._controller.add_endpoint(
            TuiEndpointConfiguration(
                provider_id="custom",
                endpoint_id="new-model",
                model_id="provider-model-id",
                context_window=128_000,
                max_output_tokens=8192,
            )
        )
        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="PROVIDERS")

        assert app.screen is menu
        assert menu.query_one(OptionList) is options
        refreshed = next(choice for choice in menu._choices if choice.value == "custom")
        assert "• 1 •" in refreshed.trailing
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_default_provider_submenu_keeps_toggle_visible_but_disabled(
    tmp_path: Path,
) -> None:
    configuration = ConfigurationFacade(tmp_path)
    configuration.add_provider(
        "primary",
        provider_type="openai-compatible",
        base_url="https://primary.example.invalid/v1",
    )
    configuration.save_endpoint("primary", "chat", model="primary-model")
    configuration.use_provider("primary")
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await pilot.pause()
        app.screen.query_one("#choice-filter", Input).value = "List providers"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        app.screen.query_one("#choice-filter", Input).value = "primary"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        toggle = next(choice for choice in app.screen._choices if choice.value == "toggle")
        assert toggle.label == "Disable provider"
        assert toggle.disabled is True
        option = app.screen.query_one("#choice-list", OptionList).get_option("toggle")
        assert option.disabled is True


@pytest.mark.parametrize(
    ("action_label", "title"),
    (
        ("List endpoints", "ENDPOINTS"),
        ("Edit endpoint", "EDIT ENDPOINT"),
        ("Set default endpoint", "SET DEFAULT ENDPOINT"),
        ("Remove endpoint", "REMOVE ENDPOINT"),
    ),
)
@pytest.mark.asyncio
async def test_endpoint_actions_flatten_routes_grouped_by_provider(
    tmp_path: Path,
    action_label: str,
    title: str,
) -> None:
    configuration = ConfigurationFacade(tmp_path)
    for provider_id in ("primary", "secondary"):
        configuration.add_provider(
            provider_id,
            provider_type="openai-compatible",
            base_url=f"https://{provider_id}.example.invalid/v1",
            enabled=provider_id == "primary",
        )
        configuration.save_endpoint(
            provider_id,
            "shared",
            model=f"{provider_id}-model",
        )
    configuration.use_provider("primary")
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await pilot.pause()
        app.screen.query_one("#choice-filter", Input).value = action_label
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert app.screen.query_one("#choice-title", Static).render().plain == title
        assert [choice.value for choice in app.screen._choices] == [
            "primary/shared",
            "secondary/shared",
        ]
        assert [choice.label for choice in app.screen._choices] == ["shared", "shared"]
        assert [choice.marker for choice in app.screen._choices] == [
            DEFAULT_TUI_THEME.selected_choice_marker,
            "",
        ]
        assert [choice.group for choice in app.screen._choices] == [
            "primary",
            "secondary",
        ]
        assert all(choice.description == "" for choice in app.screen._choices)
        assert all(choice.trailing == "" for choice in app.screen._choices)
        assert [choice.group_trailing for choice in app.screen._choices] == [
            "",
            "unavailable",
        ]
        assert [choice.disabled for choice in app.screen._choices] == [False, True]
        options = app.screen.query_one(OptionList)
        endpoint_options = {
            str(option.id): option
            for index in range(options.option_count)
            if (option := options.get_option_at_index(index)).id is not None
        }
        assert endpoint_options["primary/shared"].disabled is False
        assert endpoint_options["secondary/shared"].disabled is True
        app.screen.query_one("#choice-filter", Input).value = "secondary"
        await pilot.pause()
        assert app.screen.query_one(OptionList).highlighted is None
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ChoiceScreen)
        await pilot.press("escape")


@pytest.mark.parametrize(
    ("action_label", "target_label", "confirm_title", "message"),
    (
        (
            "Remove provider",
            "custom",
            "CONFIRM REMOVE PROVIDER",
            "Remove custom and all of its endpoints from configuration?",
        ),
        (
            "Remove endpoint",
            "chat",
            "CONFIRM REMOVE ENDPOINT",
            "Remove custom/chat from configuration?",
        ),
    ),
)
@pytest.mark.asyncio
async def test_provider_deletions_use_shared_menu_confirmation(
    tmp_path: Path,
    action_label: str,
    target_label: str,
    confirm_title: str,
    message: str,
) -> None:
    configuration = ConfigurationFacade(tmp_path)
    configuration.add_provider(
        "custom",
        provider_type="openai-compatible",
        base_url="https://custom.example.invalid/v1",
    )
    configuration.save_endpoint("custom", "chat", model="custom-model")
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await pilot.pause()
        app.screen.query_one("#choice-filter", Input).value = action_label
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        app.screen.query_one("#choice-filter", Input).value = target_label
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, ConfirmationScreen)
        assert app.screen.query_one("#choice-title", Static).render().plain == confirm_title
        assert [choice.label for choice in app.screen._choices] == ["Cancel", "Remove"]
        assert app.screen._choices[1].description == message
        options = app.screen.query_one(OptionList)
        assert options.highlighted == 0
        assert options.has_focus

        await pilot.press("enter")
        await pilot.pause()
        assert not isinstance(app.screen, ConfirmationScreen)
        assert ConfigurationFacade(tmp_path).provider("custom") is not None


@pytest.mark.asyncio
async def test_setting_default_endpoint_also_switches_default_provider(tmp_path: Path) -> None:
    configuration = ConfigurationFacade(tmp_path)
    for provider_id in ("primary", "secondary"):
        configuration.add_provider(
            provider_id,
            provider_type="openai-compatible",
            base_url=f"https://{provider_id}.example.invalid/v1",
        )
        configuration.save_endpoint(
            provider_id,
            f"{provider_id}-chat",
            model=f"{provider_id}-model",
        )
    configuration.use_provider("primary")
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 34), notifications=True) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await pilot.pause()
        app.screen.query_one("#choice-filter", Input).value = "Set default endpoint"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        app.screen.query_one("#choice-filter", Input).value = "secondary-chat"
        await pilot.pause()
        default_endpoint_screen = app.screen
        await pilot.press("enter")
        await pilot.pause()

        assert app.screen is default_endpoint_screen
        assert isinstance(app.screen, ChoiceScreen)
        assert app.screen.query_one("#choice-filter", Input).value == "secondary-chat"
        choices = {choice.value: choice for choice in app.screen._choices}
        assert choices["primary/primary-chat"].label == "primary-chat"
        assert choices["primary/primary-chat"].marker == ""
        assert choices["secondary/secondary-chat"].label == "secondary-chat"
        assert (
            choices["secondary/secondary-chat"].marker
            == DEFAULT_TUI_THEME.selected_choice_marker
        )
        assert all(choice.trailing == "" for choice in choices.values())
        catalog = ConfigurationFacade(tmp_path).provider_catalog()
        assert catalog.default_provider_id == "secondary"
        assert ConfigurationFacade(tmp_path).provider("secondary").default_endpoint == (
            "secondary-chat"
        )
        assert not list(app.screen.query_one("#textual-toastrack").query("Toast"))
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_add_provider_flow_integrates_presets_and_compatible_profiles(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await pilot.pause()
        app.screen.query_one("#choice-filter", Input).value = "Add provider"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert {"OURO presets", "Compatible"} == {choice.group for choice in app.screen._choices}
        assert {"Google Gemini", "OpenAI-compatible", "Anthropic-compatible"} <= {
            choice.label for choice in app.screen._choices
        }

        app.screen.query_one("#choice-filter", Input).value = "Google Gemini"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ProviderConfigurationScreen)
        assert not list(app.screen.query("#provider-default"))

        app.screen._values["provider-id"] = "gemini-work"
        app.screen._choices = app.screen._provider_choices()
        app.screen._refresh_options(needle="", preferred_value="provider-save")
        app.screen.query_one("#choice-list", OptionList).focus()
        await pilot.press("enter")
        await pilot.pause()
        provider = ConfigurationFacade(tmp_path).provider("gemini-work")
        assert provider.provider_type == "gemini"
        assert provider.endpoint_count == 3

        await pilot.press("ctrl+p")
        await pilot.pause()
        app.screen.query_one("#choice-filter", Input).value = "Add provider"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        app.screen.query_one("#choice-filter", Input).value = "Anthropic-compatible"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, ProviderConfigurationScreen)
        assert app.screen._values["provider-base-url"] == ""
        assert app.screen._values["provider-api-path"] == "/v1/messages"
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_provider_configuration_uses_shared_grouped_inline_menu(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = ProviderSetupProfile(
        provider_type="openai-compatible",
        default_provider_id="gateway",
        display_name="Gateway",
        description="Test gateway",
        base_url="https://gateway.example.invalid/v1",
        api_path="/chat/completions",
        credential_env="GATEWAY_API_KEY",
        endpoint_count=0,
        preset=False,
    )
    app = OuroTuiApp(application=_application(tmp_path))
    results: list[TuiProviderConfiguration | None] = []

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        screen = ProviderConfigurationScreen(profile)
        app.push_screen(screen, callback=results.append)
        await pilot.pause()

        assert isinstance(screen, EditableChoiceScreen)
        assert not list(screen.query("Button"))
        assert not list(screen.query("Checkbox"))
        assert screen.query_one("#choice-filter", Input).placeholder == (
            "Filter provider settings…"
        )
        options = screen.query_one("#choice-list", OptionList)
        assert options.styles.scrollbar_size_vertical == 0
        assert [choice.group for choice in screen._choices] == [
            "CREDENTIALS",
            "CREDENTIALS",
            "ADVANCED",
            "ADVANCED",
            "ADVANCED",
            "ADVANCED",
            "REQUEST",
            "ACTIONS",
        ]
        assert screen._values["provider-api-key-env"] == "GATEWAY_API_KEY"
        assert screen._values["provider-base-url"] == "https://gateway.example.invalid/v1"
        assert screen._values["provider-api-path"] == "/chat/completions"
        assert screen._values["provider-timeout"] == "60.0"
        assert screen._values["provider-options"] == "{}"

        options.highlighted = options.get_option_index("provider-api-key")
        options.focus()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press(*"pre", "left")
        monkeypatch.setattr(
            "ouro.tui.screens.read_system_clipboard",
            lambda: "pasted\non two lines",
        )
        await pilot.click(options, offset=(8, 1), button=3)
        await pilot.pause()
        assert screen._edit_text == "prpasted on two linese"
        assert "pasted" not in str(options.get_option("provider-api-key").prompt)
        await pilot.press("enter")
        await pilot.pause()
        assert screen._values["provider-api-key"] == "prpasted on two linese"
        assert screen._values["provider-api-key-env"] == ""

        screen._values["provider-base-url"] = ""
        screen._choices = screen._provider_choices()
        screen._refresh_options(needle="", preferred_value="provider-base-url")
        assert options.get_option("provider-save").disabled is True

        screen._values["provider-base-url"] = "https://gateway.invalid/v1"
        screen._values["provider-options"] = "[]"
        screen._choices = screen._provider_choices()
        screen._refresh_options(needle="", preferred_value="provider-options")
        assert options.get_option("provider-save").disabled is True

        screen._values["provider-options"] = '{"tool_choice":"none","top_p":0.8}'
        screen._choices = screen._provider_choices()
        screen._refresh_options(needle="", preferred_value="provider-save")
        assert options.get_option("provider-save").disabled is False
        await pilot.press("enter")
        await pilot.pause()

        assert len(results) == 1
        draft = results[0]
        assert draft is not None
        assert draft.api_key == "prpasted on two linese"
        assert draft.api_key_env is None
        assert draft.enabled is True
        assert draft.provider_options == {"tool_choice": "none", "top_p": 0.8}


def test_provider_editors_preserve_redacted_request_options_when_unchanged(
    tmp_path: Path,
) -> None:
    configuration = ConfigurationFacade(tmp_path)
    configuration.add_provider(
        "gateway",
        provider_type="openai-compatible",
        base_url="https://gateway.example.invalid/v1",
        provider_options={"api_key": "request-secret", "top_p": 0.8},
    )
    configuration.save_endpoint(
        "gateway",
        "chat",
        model="gateway-model",
        request_options={"nested": {"password": "endpoint-secret"}, "seed": 7},
    )
    provider = configuration.provider("gateway")
    endpoint = configuration.endpoint("gateway", "chat")
    profile = ProviderSetupProfile(
        provider_type="openai-compatible",
        default_provider_id="gateway",
        display_name="Gateway",
        description="Test gateway",
        base_url="https://gateway.example.invalid/v1",
        api_path="/chat/completions",
        credential_env=None,
        endpoint_count=1,
        preset=False,
    )

    provider_draft = ProviderConfigurationScreen(profile, provider)._draft()
    endpoint_draft = EndpointConfigurationScreen(provider, endpoint)._draft()

    assert provider.provider_options == {"api_key": "***", "top_p": 0.8}
    assert endpoint.request_options == {"nested": {"password": "***"}, "seed": 7}
    assert provider_draft.provider_options is None
    assert endpoint_draft.reasoning is None
    assert endpoint_draft.request_options is None


@pytest.mark.asyncio
async def test_provider_configuration_preserves_existing_enabled_state(
    tmp_path: Path,
) -> None:
    configuration = ConfigurationFacade(tmp_path)
    configuration.add_provider(
        "disabled-gateway",
        provider_type="openai-compatible",
        base_url="https://gateway.example.invalid/v1",
        enabled=False,
    )
    profile = ProviderSetupProfile(
        provider_type="openai-compatible",
        default_provider_id="openai-compatible",
        display_name="OpenAI-compatible",
        description="Test gateway",
        base_url="",
        api_path="/chat/completions",
        credential_env=None,
        endpoint_count=0,
        preset=False,
    )
    app = OuroTuiApp(application=_application(tmp_path))
    results: list[TuiProviderConfiguration | None] = []

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.push_screen(
            ProviderConfigurationScreen(
                profile,
                existing=configuration.provider("disabled-gateway"),
            ),
            callback=results.append,
        )
        await pilot.pause()

        actions = app.screen.query_one("#choice-list", OptionList)
        action_ids = [
            str(option.id)
            for index in range(actions.option_count)
            if (option := actions.get_option_at_index(index)).id is not None
            and str(option.id).startswith("provider-save")
        ]
        assert action_ids == ["provider-save"]
        actions.highlighted = actions.get_option_index("provider-save")
        actions.focus()
        await pilot.press("enter")
        await pilot.pause()

        assert len(results) == 1
        draft = results[0]
        assert draft is not None
        assert draft.enabled is False


@pytest.mark.asyncio
async def test_actions_persists_three_distinct_recent_choices_across_restart(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        for label, screen_type in (
            ("Theme", ChoiceScreen),
            ("Compaction policy", CompactionConfigurationScreen),
            ("Add MCP server", MCPConfigurationScreen),
        ):
            await pilot.press("ctrl+p")
            await pilot.pause()
            assert "Recent" not in {choice.group for choice in app.screen._choices}
            app.screen.query_one("#choice-filter", Input).value = label
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause(0.2)
            assert isinstance(app.screen, screen_type)
            await pilot.press("escape")
            await pilot.pause()
            if isinstance(app.screen, ChoiceScreen) and app.screen._title == "ACTIONS":
                await pilot.press("escape")
                await pilot.pause()

        preferences = TuiPreferencesStore(tmp_path).load()
        assert len(preferences.recent_actions) == 3

        await pilot.press("ctrl+p")
        await pilot.pause()
        assert [choice.label for choice in app.screen._choices[:3]] == [
            "Add MCP server",
            "Compaction policy",
            "Theme",
        ]
        assert all(choice.group == "Recent" for choice in app.screen._choices[:3])

    restarted = OuroTuiApp(home=tmp_path, application=_application(tmp_path))
    async with restarted.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert [choice.label for choice in restarted.screen._choices[:3]] == [
            "Add MCP server",
            "Compaction policy",
            "Theme",
        ]
        assert all(choice.group == "Recent" for choice in restarted.screen._choices[:3])


@pytest.mark.asyncio
async def test_plugin_actions_reuse_grouped_menu_and_refresh_lifecycle_status(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with app.run_test(size=(100, 34), notifications=True) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await pilot.pause()
        app.screen.query_one("#choice-filter", Input).value = "List plugins"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="PLUGINS")

        plugin_screen = app.screen
        assert list(dict.fromkeys(choice.group for choice in plugin_screen._choices)) == [
            "Built-in"
        ]
        toolkit = next(
            choice for choice in plugin_screen._choices if choice.value == "dev.ouro.toolkit"
        )
        assert toolkit.label == "Toolkit"
        assert toolkit.trailing == "v1.0.0 • active"
        assert toolkit.marker == ""

        plugin_screen.query_one("#choice-filter", Input).value = "Toolkit"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="PLUGIN • Toolkit")
        assert app.screen is plugin_screen
        assert [choice.label for choice in plugin_screen._choices] == [
            "Disable",
            "Check integrity and readiness",
            "Restart",
        ]
        assert (
            next(choice for choice in plugin_screen._choices if choice.value == "restart").disabled
            is False
        )

        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="PLUGIN • Toolkit")
        assert app.screen is plugin_screen
        lifecycle = {choice.value: choice for choice in plugin_screen._choices}
        assert lifecycle["enable"].trailing == "disabled"
        assert lifecycle["restart"].disabled is True
        assert "Toolkit" not in app._system_status.plugin_active_names

        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="PLUGINS")
        refreshed = next(
            choice for choice in plugin_screen._choices if choice.value == "dev.ouro.toolkit"
        )
        assert refreshed.trailing == "v1.0.0 • disabled"
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="PLUGIN • Toolkit")
        options = plugin_screen.query_one("#choice-list", OptionList)
        await pilot.click(options, offset=(4, 1))
        await _wait_for_screen(app, ChoiceScreen, title="PLUGIN • Toolkit")
        assert (
            next(choice for choice in plugin_screen._choices if choice.value == "disable").trailing
            == "active"
        )
        assert (
            next(choice for choice in plugin_screen._choices if choice.value == "restart").disabled
            is False
        )
        assert (
            plugin_screen.query_one("#choice-list", OptionList).get_option("restart").disabled
            is False
        )

        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="PLUGINS")
        refreshed = next(
            choice for choice in plugin_screen._choices if choice.value == "dev.ouro.toolkit"
        )
        assert refreshed.marker == ""
        assert refreshed.trailing == "v1.0.0 • active"


@pytest.mark.asyncio
async def test_plugin_install_failure_uses_error_toast_without_changing_conversation(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with app.run_test(size=(100, 34), notifications=True) as pilot:
        await pilot.pause()
        history = app.query_one(ConversationHistory)
        entries_before = history.entries

        await pilot.press("ctrl+p")
        await pilot.pause()
        app.screen.query_one("#choice-filter", Input).value = "Install plugin"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, PluginInstallScreen, title="INSTALL PLUGIN")

        install_screen = app.screen
        options = install_screen.query_one("#choice-list", OptionList)
        options.highlighted = options.get_option_index("plugin-source")
        options.focus()
        await pilot.press("enter", *"missing-plugin-source", "enter")
        await pilot.pause()
        options.highlighted = options.get_option_index("plugin-install")
        options.focus()
        await pilot.press("enter")
        await pilot.pause()

        toast = app.screen.query_one("#textual-toastrack").query_one("Toast")
        assert toast.has_class("-error")
        assert "plugin source does not exist" in toast.render().plain
        assert history.entries == entries_before
        assert "plugin source does not exist" not in history.rendered_text


@pytest.mark.asyncio
async def test_alt_navigation_shortcuts_preempt_all_focused_text_inputs(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        composer = app.query_one("#composer", TextArea)
        composer.text = "draft"
        composer.focus()
        app.post_message(events.Key("alt+x", "x"))
        await pilot.pause()
        assert composer.text == "draft"

        app.post_message(events.Key("alt+p", "p"))
        await _wait_for_screen(app, ChoiceScreen, title="PLUGINS")

        assert {choice.value for choice in app.screen._choices} >= {
            "dev.ouro.agent_context",
            "dev.ouro.toolkit",
        }
        assert composer.text == "draft"

        plugin_screen = app.screen
        plugin_filter = plugin_screen.query_one("#choice-filter", Input)
        plugin_filter.value = "tool"
        plugin_filter.focus()
        app.post_message(events.Key("alt+x", "x"))
        await pilot.pause()
        assert app.screen is plugin_screen
        assert plugin_filter.value == "tool"

        app.post_message(events.Key("alt+p", "p"))
        await pilot.pause()

        assert app.screen is plugin_screen
        assert plugin_filter.value == "tool"
        assert sum(isinstance(screen, ChoiceScreen) for screen in app.screen_stack) == 1


@pytest.mark.asyncio
async def test_alt_menu_shortcuts_are_idempotent_and_do_not_stack_screens(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("alt+p")
        await _wait_for_screen(app, ChoiceScreen, title="PLUGINS")
        menu = app.screen

        for _ in range(3):
            await pilot.press("alt+p")
            await _wait_for_screen(app, ChoiceScreen, title="PLUGINS")

        assert app.screen is menu
        assert len(menu._page_stack) == 0
        assert sum(isinstance(screen, ChoiceScreen) for screen in app.screen_stack) == 1


@pytest.mark.asyncio
async def test_plugin_shortcut_navigates_beneath_actions_without_stacking(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        menu = app.screen

        for key, title in (
            ("alt+p", "PLUGINS"),
            ("alt+p", "PLUGINS"),
        ):
            await pilot.press(key)
            await _wait_for_screen(app, ChoiceScreen, title=title)
            assert app.screen is menu
            assert len(menu._page_stack) == 1
            assert sum(isinstance(screen, ChoiceScreen) for screen in app.screen_stack) == 1

        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        assert app.screen is menu


@pytest.mark.asyncio
async def test_actions_and_session_shortcuts_switch_on_the_active_menu_surface(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    application.runtime.sessions.create(session_id="ses_shortcut", title="Shortcut session")
    app = OuroTuiApp(home=tmp_path, application=application)

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        menu = app.screen

        for key, title, page_depth in (
            ("ctrl+s", "SESSIONS", 1),
            ("ctrl+p", "ACTIONS", 0),
            ("ctrl+p", "ACTIONS", 0),
        ):
            await pilot.press(key)
            await _wait_for_screen(app, ChoiceScreen, title=title)
            assert app.screen is menu
            assert len(menu._page_stack) == page_depth
            assert sum(isinstance(screen, ChoiceScreen) for screen in app.screen_stack) == 1

        await pilot.press("escape")


@pytest.mark.asyncio
async def test_question_mark_remains_composer_input_after_help_shortcut_migration(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        composer = app.query_one("#composer", TextArea)
        composer.focus()
        app.post_message(events.Key("question_mark", "?"))
        await pilot.pause()

        assert composer.text == "?"
        assert not isinstance(app.screen, HelpScreen)


@pytest.mark.asyncio
async def test_root_help_groups_runtime_plugin_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(
            app._controller,
            "plugin_commands",
            lambda: (
                PluginCommandView(
                    command_id="dev.ouro.demo:hello",
                    plugin_id="dev.ouro.demo",
                    name="hello",
                    description="Say hello",
                    usage="<name>",
                ),
            ),
        )

        await pilot.press("ctrl+h")
        await _wait_for_screen(app, HelpScreen, title="KEYBOARD HELP")

        plugins = tuple(
            choice for choice in app.screen._choices if choice.group == "PLUGIN SLASH COMMANDS"
        )
        assert [(choice.label, choice.trailing) for choice in plugins] == [
            ("/hello <name>", "Say hello")
        ]


@pytest.mark.asyncio
async def test_action_menu_executes_new_session_and_help_shortcuts_in_context(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        menu = app.screen

        await pilot.press("ctrl+h")
        await _wait_for_screen(app, ChoiceScreen, title="KEYBOARD HELP")
        assert app.screen is menu
        assert sum(isinstance(screen, ChoiceScreen) for screen in app.screen_stack) == 1
        assert menu.query_one("#choice-panel", Vertical).display is True
        assert menu.query_one("#choice-hint", Static).render().plain == (HelpScreen.BACK_HINT)
        assert {choice.group for choice in menu._choices} == {
            "SHORTCUTS",
            "BUILT-IN SLASH COMMANDS",
            "PLUGIN SLASH COMMANDS",
        }
        shortcuts = {
            choice.label: choice.trailing for choice in menu._choices if choice.group == "SHORTCUTS"
        }
        assert shortcuts["Alt+T"] == "cycle model thinking"
        assert shortcuts["Alt+A"] == "cycle session approval"
        assert shortcuts["Alt+← / Alt+→"] == "switch agent node"
        assert shortcuts["Alt+↑ / Alt+↓"] == "main / latest agent"
        plugin_commands = tuple(
            choice for choice in menu._choices if choice.group == "PLUGIN SLASH COMMANDS"
        )
        goal = next(choice for choice in plugin_commands if choice.label.startswith("/goal "))
        plan = next(choice for choice in plugin_commands if choice.label == "/plan [on|off|status]")
        assert goal.trailing == "Create and control the latest Goal for this conversation."
        assert plan.trailing == "Enter plan mode for the current Session."

        await pilot.press("ctrl+h")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        assert app.screen is menu
        assert menu.query_one("#choice-hint", Static).render().plain == (ChoiceScreen.DEFAULT_HINT)

        await pilot.press("ctrl+n")
        await pilot.pause()
        assert not isinstance(app.screen, ChoiceScreen)
        assert len(app.screen_stack) == 1
        assert app._active_title == "New session"


@pytest.mark.asyncio
async def test_navigation_menu_routes_ctrl_c_to_abort_without_closing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))
    aborted = False

    def abort() -> None:
        nonlocal aborted
        aborted = True

    monkeypatch.setattr(app, "_has_active_run", lambda: True)
    monkeypatch.setattr(app, "_abort_run", abort)

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        menu = app.screen

        await pilot.press("ctrl+c")
        await pilot.pause()

        assert aborted is True
        assert app.screen is menu


@pytest.mark.asyncio
async def test_alt_menu_shortcuts_do_not_replace_transient_choice_children(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with app.run_test(size=(100, 42)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        actions = app.screen
        actions.query_one("#choice-filter", Input).value = "Compaction policy"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, CompactionConfigurationScreen, title="CONTEXT COMPACTION")
        editor = app.screen
        options = editor.query_one("#choice-list", OptionList)
        options.highlighted = options.get_option_index("compaction-model")
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="COMPACTION MODEL")
        transient = app.screen

        await pilot.press("alt+p")
        await pilot.pause()

        assert app.screen is transient
        assert transient.navigation_root_id is None
        assert len(app.screen_stack) == 4
        await pilot.press("escape")
        await _wait_for_screen(app, CompactionConfigurationScreen, title="CONTEXT COMPACTION")
        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_mcp_actions_open_operation_specific_server_selection(tmp_path: Path) -> None:
    ConfigurationFacade(tmp_path).save_mcp_server(
        "local",
        MCPServerConfigUpdate(command="python", auto_start=False),
    )
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        for action_label, chooser_title in (
            ("Connect MCP server", "CONNECT MCP SERVER"),
            ("Edit MCP server", "EDIT MCP SERVER"),
            ("Remove MCP server", "REMOVE MCP SERVER"),
        ):
            await pilot.press("ctrl+p")
            await pilot.pause()
            app.screen.query_one("#choice-filter", Input).value = action_label
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause(0.2)

            assert isinstance(app.screen, ChoiceScreen)
            assert app.screen.query_one("#choice-title", Static).render().plain == chooser_title
            assert [choice.value for choice in app.screen._choices] == ["local"]
            assert app.screen._choices[0].group == "STDIO"
            assert app.screen._choices[0].trailing == "enabled • configured"
            assert app.screen._choices[0].description == ""

            await pilot.press("escape")
            await pilot.pause()


@pytest.mark.asyncio
async def test_mcp_connection_menus_show_all_servers_and_disable_unavailable_actions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configuration = ConfigurationFacade(tmp_path)
    for server_id in ("connected", "stopped"):
        configuration.save_mcp_server(
            server_id,
            MCPServerConfigUpdate(command="unused-in-test", auto_start=False),
        )
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    def runtime_servers() -> tuple[McpServerStatus, ...]:
        return (
            McpServerStatus(
                server_id="connected",
                name="connected",
                transport="stdio",
                status="connected",
            ),
            McpServerStatus(
                server_id="stopped",
                name="stopped",
                transport="stdio",
                status="stopped",
            ),
        )

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(app._controller, "mcp_servers", runtime_servers)
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        actions = app.screen

        actions.query_one("#choice-filter", Input).value = "Connect MCP server"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="CONNECT MCP SERVER")
        connect_choices = {choice.value: choice for choice in app.screen._choices}
        assert set(connect_choices) == {"connected", "stopped"}
        assert connect_choices["connected"].disabled is True
        assert connect_choices["stopped"].disabled is False

        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        actions.query_one("#choice-filter", Input).value = "Disconnect MCP server"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="DISCONNECT MCP SERVER")
        disconnect_choices = {choice.value: choice for choice in app.screen._choices}
        assert set(disconnect_choices) == {"connected", "stopped"}
        assert disconnect_choices["connected"].disabled is False
        assert disconnect_choices["stopped"].disabled is True

        await pilot.press("escape", "escape")


@pytest.mark.asyncio
async def test_mcp_connect_runs_in_background_and_contains_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ConfigurationFacade(tmp_path).save_mcp_server(
        "local",
        MCPServerConfigUpdate(command="unused-in-test", auto_start=False),
    )
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))
    operation_started = asyncio.Event()
    release_operation = asyncio.Event()
    runtime_status = "configured"
    start_calls = 0

    def runtime_servers() -> tuple[McpServerStatus, ...]:
        return (
            McpServerStatus(
                server_id="local",
                name="local",
                transport="stdio",
                status=runtime_status,
            ),
        )

    async def failing_start(_server_id: str) -> McpServerStatus:
        nonlocal runtime_status, start_calls
        start_calls += 1
        runtime_status = "starting"
        operation_started.set()
        await release_operation.wait()
        runtime_status = "failed"
        raise RuntimeError("controlled MCP connection failure")

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(app._controller, "mcp_servers", runtime_servers)
        monkeypatch.setattr(app._controller, "start_mcp_server", failing_start)
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        app.screen.query_one("#choice-filter", Input).value = "Connect MCP server"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="CONNECT MCP SERVER")
        menu = app.screen
        options = menu.query_one(OptionList)
        options.focus()

        await pilot.press("enter")
        await asyncio.wait_for(operation_started.wait(), timeout=1)

        assert app.screen is menu
        assert next(choice for choice in menu._choices if choice.value == "local").loading
        menu.query_one("#choice-filter", Input).value = "local"
        await pilot.pause()
        assert menu.query_one("#choice-filter", Input).value == "local"

        release_operation.set()
        deadline = asyncio.get_running_loop().time() + 2
        while next(choice for choice in menu._choices if choice.value == "local").loading:
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("MCP failure did not clear the menu loading state")
            await pilot.pause(0.05)

        failed = next(choice for choice in menu._choices if choice.value == "local")
        assert failed.trailing == "enabled • failed"
        assert app.screen is menu

        # A runtime refresh keeps the full server catalog visible while making
        # an already connected server non-selectable.
        runtime_status = "connected"
        await menu.refresh_choices()
        connected = next(choice for choice in menu._choices if choice.value == "local")
        assert connected.disabled is True
        assert connected.trailing == "enabled • connected"
        options.highlighted = options.get_option_index("local")
        await pilot.press("enter")
        await pilot.pause()
        assert start_calls == 1


@pytest.mark.asyncio
async def test_mcp_disconnect_runs_in_background_and_refreshes_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ConfigurationFacade(tmp_path).save_mcp_server(
        "local",
        MCPServerConfigUpdate(command="unused-in-test", auto_start=False),
    )
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))
    operation_started = asyncio.Event()
    release_operation = asyncio.Event()
    runtime_status = "connected"

    def runtime_servers() -> tuple[McpServerStatus, ...]:
        return (
            McpServerStatus(
                server_id="local",
                name="local",
                transport="stdio",
                status=runtime_status,
            ),
        )

    async def controlled_stop(_server_id: str) -> McpServerStatus:
        nonlocal runtime_status
        runtime_status = "stopping"
        operation_started.set()
        await release_operation.wait()
        runtime_status = "stopped"
        return runtime_servers()[0]

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(app._controller, "mcp_servers", runtime_servers)
        monkeypatch.setattr(app._controller, "stop_mcp_server", controlled_stop)
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        app.screen.query_one("#choice-filter", Input).value = "Disconnect MCP server"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="DISCONNECT MCP SERVER")
        menu = app.screen
        options = menu.query_one(OptionList)
        options.focus()

        await pilot.press("enter")
        await asyncio.wait_for(operation_started.wait(), timeout=1)
        assert next(choice for choice in menu._choices if choice.value == "local").loading
        menu.query_one("#choice-filter", Input).value = "local"
        await pilot.pause()
        assert menu.query_one("#choice-filter", Input).value == "local"

        release_operation.set()
        deadline = asyncio.get_running_loop().time() + 2
        while next(choice for choice in menu._choices if choice.value == "local").loading:
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("MCP stop did not clear the menu loading state")
            await pilot.pause(0.05)

        stopped = next(choice for choice in menu._choices if choice.value == "local")
        assert stopped.trailing == "enabled • stopped"
        assert stopped.disabled is True
        assert app.screen is menu


@pytest.mark.asyncio
async def test_mcp_disconnect_then_connect_survives_closed_action_menus(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ConfigurationFacade(tmp_path).save_mcp_server(
        "local",
        MCPServerConfigUpdate(command="unused-in-test", auto_start=False),
    )
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))
    runtime_status = "connected"
    stop_started = asyncio.Event()
    stop_release = asyncio.Event()
    stop_finished = asyncio.Event()
    start_started = asyncio.Event()
    start_release = asyncio.Event()
    start_finished = asyncio.Event()

    def runtime_servers() -> tuple[McpServerStatus, ...]:
        return (
            McpServerStatus(
                server_id="local",
                name="local",
                transport="stdio",
                status=runtime_status,
                tool_count=1 if runtime_status == "connected" else 0,
            ),
        )

    async def controlled_stop(_server_id: str) -> McpServerStatus:
        nonlocal runtime_status
        runtime_status = "stopping"
        stop_started.set()
        await stop_release.wait()
        runtime_status = "stopped"
        stop_finished.set()
        return runtime_servers()[0]

    async def controlled_start(_server_id: str) -> McpServerStatus:
        nonlocal runtime_status
        runtime_status = "starting"
        start_started.set()
        await start_release.wait()
        runtime_status = "connected"
        start_finished.set()
        return runtime_servers()[0]

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(app._controller, "mcp_servers", runtime_servers)
        monkeypatch.setattr(app._controller, "stop_mcp_server", controlled_stop)
        monkeypatch.setattr(app._controller, "start_mcp_server", controlled_start)

        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        app.screen.query_one("#choice-filter", Input).value = "Disconnect MCP server"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="DISCONNECT MCP SERVER")
        disconnected_menu = app.screen
        await pilot.press("enter")
        await asyncio.wait_for(stop_started.wait(), timeout=1)
        await pilot.press("escape", "escape")
        await pilot.pause()
        assert disconnected_menu.is_attached is False

        stop_release.set()
        await asyncio.wait_for(stop_finished.wait(), timeout=1)
        await pilot.pause()
        assert app.is_running is True

        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        app.screen.query_one("#choice-filter", Input).value = "Connect MCP server"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="CONNECT MCP SERVER")
        connected_menu = app.screen
        await pilot.press("enter")
        await asyncio.wait_for(start_started.wait(), timeout=1)
        await pilot.press("escape", "escape")
        await pilot.pause()
        assert connected_menu.is_attached is False

        start_release.set()
        await asyncio.wait_for(start_finished.wait(), timeout=1)
        await pilot.pause()

        assert app.is_running is True
        assert not isinstance(app.screen, ChoiceScreen)
        assert app._system_status.mcp_connected_count == 1


@pytest.mark.asyncio
async def test_tool_permissions_group_all_registered_sources(tmp_path: Path) -> None:
    ConfigurationFacade(tmp_path).set_tool_permission("UnavailableTest", "deny")
    app = OuroTuiApp(home=tmp_path, application=_application_with_tool_sources(tmp_path))

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await pilot.pause()
        app.screen.query_one("#choice-filter", Input).value = "Tool permissions"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="TOOL PERMISSIONS")

        groups = list(dict.fromkeys(choice.group for choice in app.screen._choices))
        assert groups[:3] == [
            "Built-in",
            "MCP",
            "dev.ouro.official",
        ]
        assert groups[-3:] == [
            "dev.ouro.third-party",
            "dev.ouro.generated",
            "Unavailable",
        ]
        choices = {choice.value: choice for choice in app.screen._choices}
        assert choices["BuiltinTest"].group == "Built-in"
        assert choices["McpTest"].group == "MCP"
        assert choices["OfficialPluginTest"].group == "dev.ouro.official"
        assert choices["ThirdPartyPluginTest"].group == "dev.ouro.third-party"
        assert choices["AiGeneratedPluginTest"].group == "dev.ouro.generated"
        assert not any(choice.label == "Plugin default" for choice in choices.values())
        assert choices["UnavailableTest"].group == "Unavailable"
        assert choices["BuiltinTest"].trailing == "default • approval"
        assert choices["ContractControlledTest"].trailing == "contract • auto"
        assert choices["ContractControlledTest"].disabled is True
        assert choices["ThirdPartyPluginTest"].disabled is False
        assert choices["UnavailableTest"].trailing == "deny • unavailable"
        assert all(choice.description == "" for choice in choices.values())


@pytest.mark.asyncio
async def test_tool_permission_selection_returns_to_refreshed_tool_menu(tmp_path: Path) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application_with_tool_sources(tmp_path))

    async with app.run_test(size=(100, 34), notifications=True) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await pilot.pause()
        app.screen.query_one("#choice-filter", Input).value = "Tool permissions"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="TOOL PERMISSIONS")

        tool_screen = app.screen
        tool_screen.query_one("#choice-filter", Input).value = "BuiltinTest"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(
            app,
            ChoiceScreen,
            title="TOOL PERMISSION • BuiltinTest",
        )
        assert app.screen is tool_screen
        assert sum(isinstance(screen, ChoiceScreen) for screen in app.screen_stack) == 1
        current = next(choice for choice in app.screen._choices if choice.value == "default")
        assert current.label == "default"
        assert current.marker == DEFAULT_TUI_THEME.selected_choice_marker
        assert current.trailing == "approval"
        assert all(choice.trailing != "current" for choice in app.screen._choices)
        app.screen.query_one("#choice-filter", Input).value = "auto"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="TOOL PERMISSIONS")

        assert app.screen is tool_screen
        assert app.screen.query_one("#choice-title", Static).render().plain == ("TOOL PERMISSIONS")
        assert (
            next(choice for choice in app.screen._choices if choice.value == "BuiltinTest").trailing
            == "auto"
        )
        assert not list(app.screen.query("Toast"))
        assert ConfigurationFacade(tmp_path).permission_config().tools["BuiltinTest"] == "auto"

        await pilot.press("enter")
        await _wait_for_screen(
            app,
            ChoiceScreen,
            title="TOOL PERMISSION • BuiltinTest",
        )
        assert app.screen is tool_screen
        assert sum(isinstance(screen, ChoiceScreen) for screen in app.screen_stack) == 1
        assert "default" in {choice.value for choice in app.screen._choices}
        assert "inherit" not in {choice.value for choice in app.screen._choices}
        app.screen.query_one("#choice-filter", Input).value = "default"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="TOOL PERMISSIONS")
        assert app.screen is tool_screen
        assert (
            next(choice for choice in app.screen._choices if choice.value == "BuiltinTest").trailing
            == "default • approval"
        )
        assert not list(app.screen.query("Toast"))
        assert "BuiltinTest" not in ConfigurationFacade(tmp_path).permission_config().tools


@pytest.mark.parametrize(
    ("enabled", "runtime_status", "selectable_runtime_actions"),
    (
        (False, "disabled", set()),
        (True, None, set()),
        (True, "configured", {"start"}),
        (True, "failed", {"start"}),
        (True, "stopped", {"start"}),
        (True, "starting", {"stop"}),
        (True, "reconnecting", {"stop"}),
        (True, "connected", {"refresh", "restart", "stop"}),
        (True, "stopping", set()),
    ),
)
def test_mcp_management_menu_keeps_stable_actions_and_disables_unavailable_ones(
    tmp_path: Path,
    *,
    enabled: bool,
    runtime_status: str | None,
    selectable_runtime_actions: set[str],
) -> None:
    catalog = ConfigurationFacade(tmp_path).save_mcp_server(
        "local",
        MCPServerConfigUpdate(command="python", enabled=enabled, auto_start=False),
    )
    config = next(server for server in catalog.servers if server.server_id == "local")
    status = (
        McpServerStatus(
            server_id="local",
            name="local",
            transport="stdio",
            status=runtime_status,
        )
        if runtime_status is not None
        else None
    )

    choices = _mcp_management_choices(config, status)

    assert [choice.value for choice in choices] == [
        "edit",
        "toggle",
        "check",
        "start",
        "refresh",
        "restart",
        "stop",
        "remove",
    ]
    assert {choice.value for choice in choices if not choice.disabled} == {
        "edit",
        "toggle",
        "check",
        "remove",
        *selectable_runtime_actions,
    }
    assert next(choice for choice in choices if choice.value == "toggle").label == (
        "Disable server" if enabled else "Enable server"
    )


@pytest.mark.asyncio
async def test_configuration_choice_submenus_reuse_action_menu_without_flash(
    tmp_path: Path,
) -> None:
    ConfigurationFacade(tmp_path).save_mcp_server(
        "local",
        MCPServerConfigUpdate(command="python", auto_start=False),
    )
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        action_screen = app.screen

        action_screen.query_one("#choice-filter", Input).value = "List MCP servers"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="MCP SERVERS")
        assert app.screen is action_screen
        assert sum(isinstance(screen, ChoiceScreen) for screen in app.screen_stack) == 1

        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="MCP • local")
        assert app.screen is action_screen
        assert [choice.value for choice in app.screen._choices] == [
            "edit",
            "toggle",
            "check",
            "start",
            "refresh",
            "restart",
            "stop",
            "remove",
        ]
        toggle = next(choice for choice in app.screen._choices if choice.value == "toggle")
        assert toggle.label == "Disable server"

        options = app.screen.query_one("#choice-list", OptionList)
        options.highlighted = options.get_option_index("toggle")
        options.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert ConfigurationFacade(tmp_path).mcp_server("local").enabled is False
        assert (
            next(choice for choice in app.screen._choices if choice.value == "toggle").label
            == "Enable server"
        )
        start = next(choice for choice in app.screen._choices if choice.value == "start")
        assert start.label == "Start runtime"
        assert start.disabled is True
        assert options.get_option("start").disabled is True
        assert all(
            next(choice for choice in app.screen._choices if choice.value == action).disabled
            for action in ("refresh", "restart", "stop")
        )
        assert all(options.get_option(action).disabled for action in ("refresh", "restart", "stop"))

        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="MCP SERVERS")
        assert (
            next(choice for choice in app.screen._choices if choice.value == "local").trailing
            == "disabled • disabled"
        )
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="MCP • local")
        options = app.screen.query_one("#choice-list", OptionList)
        await pilot.click(options, offset=(4, 1))
        await pilot.pause()
        assert ConfigurationFacade(tmp_path).mcp_server("local").enabled is True
        assert (
            next(choice for choice in app.screen._choices if choice.value == "toggle").label
            == "Disable server"
        )
        assert (
            next(choice for choice in app.screen._choices if choice.value == "start").disabled
            is False
        )

        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="MCP SERVERS")
        assert app.screen is action_screen
        assert next(
            choice for choice in app.screen._choices if choice.value == "local"
        ).trailing.startswith("enabled •")
        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        assert app.screen is action_screen


@pytest.mark.asyncio
async def test_mcp_runtime_actions_refresh_availability_without_rebuilding_menu(
    tmp_path: Path,
) -> None:
    configuration = ConfigurationFacade(tmp_path)
    configuration.save_mcp_server(
        "local",
        MCPServerConfigUpdate(
            command="unused-in-test",
            auto_start=False,
            auto_reconnect=False,
            timeout_seconds=1,
            health_check_interval_seconds=60,
        ),
    )
    application = _application(tmp_path)
    session = LiveMcpSession()
    manager = MCPManager(
        registry=application.runtime.tools,
        client_factory=LiveMcpFactory(session),
    )
    config = configuration.mcp.get("local")
    assert config is not None
    manager.configure(config)
    application.mcp = MCPRuntimeResource(manager)
    app = OuroTuiApp(home=tmp_path, application=application)

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        action_screen = app.screen
        action_screen.query_one("#choice-filter", Input).value = "List MCP servers"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="MCP SERVERS")
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="MCP • local")

        options = action_screen.query_one("#choice-list", OptionList)
        assert options.get_option("start").disabled is False
        options.highlighted = options.get_option_index("start")
        options.focus()
        await pilot.press("enter")

        assert action_screen is app.screen
        assert (
            next(choice for choice in action_screen._choices if choice.value == "start").loading
            is True
        )
        action_screen.query_one("#choice-filter", Input).value = "runtime"
        await pilot.pause(0.15)
        assert action_screen.query_one("#choice-filter", Input).value == "runtime"

        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="MCP SERVERS")
        assert (
            next(choice for choice in action_screen._choices if choice.value == "local").loading
            is True
        )

        session.initialize_gate.set()
        deadline = asyncio.get_running_loop().time() + 2
        while next(choice for choice in action_screen._choices if choice.value == "local").loading:
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("MCP start did not refresh the parent menu")
            await pilot.pause(0.05)
        assert (
            next(choice for choice in action_screen._choices if choice.value == "local").trailing
            == "enabled • connected"
        )

        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="MCP • local")
        options = action_screen.query_one("#choice-list", OptionList)

        assert action_screen is app.screen
        assert options.get_option("start").disabled is True
        assert options.get_option("refresh").disabled is False
        assert options.get_option("restart").disabled is False
        assert options.get_option("stop").disabled is False

        options.highlighted = options.get_option_index("stop")
        await pilot.press("enter")
        await pilot.pause()

        assert [choice.value for choice in action_screen._choices] == [
            "edit",
            "toggle",
            "check",
            "start",
            "refresh",
            "restart",
            "stop",
            "remove",
        ]
        assert options.get_option("start").disabled is False
        assert options.get_option("refresh").disabled is True
        assert options.get_option("restart").disabled is True
        assert options.get_option("stop").disabled is True

        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="MCP SERVERS")
        assert (
            next(choice for choice in action_screen._choices if choice.value == "local").trailing
            == "enabled • stopped"
        )
        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        assert any(choice.value.endswith(":mcp.list") for choice in action_screen._choices)


@pytest.mark.asyncio
async def test_configuration_editor_restores_retained_action_menu(tmp_path: Path) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        action_screen = app.screen
        action_screen.query_one("#choice-filter", Input).value = "Add MCP server"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, EditableChoiceScreen, title="ADD MCP SERVER")

        assert action_screen in app.screen_stack
        assert action_screen.query_one("#choice-panel", Vertical).display is False
        assert action_screen.styles.background.a == 0
        assert app.screen is not action_screen
        assert app.screen.styles.background.a == 0.45
        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        assert app.screen is action_screen
        assert action_screen.query_one("#choice-panel", Vertical).display is True


@pytest.mark.asyncio
async def test_action_choice_submenus_reuse_the_action_screen_without_flash(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    application.runtime.sessions.create(
        session_id="ses_navigation",
        title="Navigation session",
    )
    app = OuroTuiApp(home=tmp_path, application=application)

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        for action_label, title in (
            ("Theme", "THEME"),
            ("Open session", "SESSIONS"),
            ("Archive session", "ARCHIVE SESSIONS"),
            ("Keyboard help", "KEYBOARD HELP"),
        ):
            await pilot.press("ctrl+p")
            await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
            action_screen = app.screen
            action_screen.query_one("#choice-filter", Input).value = action_label
            await pilot.pause()
            await pilot.press("enter")
            await _wait_for_screen(app, ChoiceScreen, title=title)

            assert app.screen is action_screen
            assert len(app.screen_stack) == 2
            assert action_screen.styles.background.a == 0.45

            await pilot.press("escape")
            await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
            assert app.screen is action_screen
            await pilot.press("escape")


@pytest.mark.asyncio
async def test_action_list_and_help_pages_render_once_on_the_shared_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configuration = ConfigurationFacade(tmp_path)
    configuration.add_provider(
        "custom",
        provider_type="openai-compatible",
        base_url="https://example.invalid/v1",
    )
    skill_dir = tmp_path / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo skill.\n---\n",
        encoding="utf-8",
    )
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        menu = app.screen
        options = menu.query_one(OptionList)

        for action_label, title in (
            ("List providers", "PROVIDERS"),
            ("List skills", "SKILLS"),
            ("Keyboard help", "KEYBOARD HELP"),
        ):
            menu.query_one("#choice-filter", Input).value = action_label
            await pilot.pause()
            rendered_titles: list[str] = []
            refresh_options = menu._refresh_options

            def track_refresh(
                *,
                needle: str,
                preferred_value: str | None,
                preferred_position: int | None = None,
                _rendered_titles: list[str] = rendered_titles,
                _refresh_options: Callable[..., None] = refresh_options,
            ) -> None:
                _rendered_titles.append(menu.page_title)
                _refresh_options(
                    needle=needle,
                    preferred_value=preferred_value,
                    preferred_position=preferred_position,
                )

            monkeypatch.setattr(menu, "_refresh_options", track_refresh)
            await pilot.press("enter")
            await _wait_for_screen(app, ChoiceScreen, title=title)
            monkeypatch.setattr(menu, "_refresh_options", refresh_options)

            assert app.screen is menu
            assert menu.query_one(OptionList) is options
            assert rendered_titles == [title]
            assert sum(isinstance(screen, ChoiceScreen) for screen in app.screen_stack) == 1

            await pilot.press("escape")
            await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")

        assert [choice.label for choice in menu._choices if choice.group == "Recent"] == [
            "Keyboard help",
            "List skills",
            "List providers",
        ]
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_configuration_editors_replace_visible_parent_at_every_depth(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with app.run_test(size=(100, 42)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        action_screen = app.screen
        action_screen.query_one("#choice-filter", Input).value = "Compaction policy"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, CompactionConfigurationScreen, title="CONTEXT COMPACTION")

        compaction_screen = app.screen
        assert action_screen.query_one("#choice-panel", Vertical).display is False
        assert action_screen.styles.background.a == 0
        assert compaction_screen.styles.background.a == 0.45

        compaction_screen.query_one(
            "#choice-list", OptionList
        ).highlighted = compaction_screen.query_one("#choice-list", OptionList).get_option_index(
            "compaction-model"
        )
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="COMPACTION MODEL")

        assert compaction_screen.query_one("#choice-panel", Vertical).display is False
        assert compaction_screen.styles.background.a == 0
        assert app.screen.styles.background.a == 0.45

        await pilot.press("escape")
        await _wait_for_screen(app, CompactionConfigurationScreen, title="CONTEXT COMPACTION")
        assert app.screen is compaction_screen
        assert compaction_screen.query_one("#choice-panel", Vertical).display is True
        assert compaction_screen.styles.background.a == 0.45

        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        assert app.screen is action_screen
        assert action_screen.query_one("#choice-panel", Vertical).display is True
        assert action_screen.styles.background.a == 0.45


@pytest.mark.asyncio
async def test_skill_actions_open_operation_specific_skill_selection(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo skill.\n---\n\nUse the demo workflow.\n",
        encoding="utf-8",
    )
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await pilot.pause()
        app.screen.query_one("#choice-filter", Input).value = "Validate skill"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause(0.2)

        assert isinstance(app.screen, ChoiceScreen)
        assert app.screen.query_one("#choice-title", Static).render().plain == "VALIDATE SKILL"
        assert [choice.value for choice in app.screen._choices] == ["demo"]
        assert app.screen._choices[0].description == ""
        assert app.screen._choices[0].trailing == "ouro • user • enabled"


@pytest.mark.asyncio
async def test_skill_directory_sources_use_shared_menu_and_persist_toggles(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo skill.\n---\n",
        encoding="utf-8",
    )
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with app.run_test(size=(100, 34)) as pilot:
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
            "DISCOVERY",
            *("USER DIRECTORIES",) * 5,
            *("WORKSPACE DIRECTORIES",) * 5,
        ]
        assert [choice.label for choice in action_screen._choices[1:]] == [
            "agents • user",
            "claude • user",
            "copilot • user",
            "codex • user",
            "ouro • user",
            "agents • workspace",
            "claude • workspace",
            "github • workspace",
            "codex • workspace",
            "ouro • workspace",
        ]
        assert all(choice.trailing == "enabled" for choice in action_screen._choices)
        assert "plugin" not in {choice.value for choice in action_screen._choices}

        action_screen.query_one("#choice-filter", Input).value = "ouro • user"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        policy = ConfigurationFacade(tmp_path).skill_policy()
        assert policy.disabled_sources == ["ouro.user"]
        assert app._controller.runtime_skill_names() == ()
        assert (
            next(
                choice for choice in action_screen._choices if choice.value == "ouro.user"
            ).trailing
            == "disabled"
        )

        action_screen.query_one("#choice-filter", Input).value = "Skill directory discovery"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        policy = ConfigurationFacade(tmp_path).skill_policy()
        assert policy.enabled is False
        assert policy.disabled_sources == ["ouro.user"]
        assert (
            next(
                choice for choice in action_screen._choices if choice.value == "discovery"
            ).trailing
            == "disabled"
        )


@pytest.mark.asyncio
async def test_skill_menu_loads_live_plugin_skill_as_disabled_choice(tmp_path: Path) -> None:
    source = tmp_path / "skill-plugin"
    _write_skill_plugin(source)
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        installed = await app._controller.install_plugin(
            TuiPluginInstallConfiguration(source=str(source))
        )
        assert (await app._controller.enable_plugin(installed.plugin_id)).status == "active"

        await pilot.press("ctrl+p")
        await pilot.pause()
        app.screen.query_one("#choice-filter", Input).value = "List skills"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, ChoiceScreen)
        assert app.screen.query_one("#choice-title", Static).render().plain == "SKILLS"
        assert [choice.value for choice in app.screen._choices] == ["calendar"]
        assert app.screen._choices[0].disabled is True
        assert app.screen._choices[0].trailing == "plugin • enabled"


@pytest.mark.asyncio
async def test_skill_disable_action_is_owned_by_skill_submenu(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo skill.\n---\n\nUse the demo workflow.\n",
        encoding="utf-8",
    )
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await pilot.pause()
        app.screen.query_one("#choice-filter", Input).value = "List skills"
        await pilot.pause()
        await pilot.press("enter", "enter")
        await pilot.pause()

        assert app.screen.query_one("#choice-title", Static).render().plain == "SKILL • demo"
        assert app.screen._choices[0].label == "Disable"

        options = app.screen.query_one("#choice-list", OptionList)
        options.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert app._controller.runtime_skill_names() == ()
        assert app.screen._choices[0].label == "Enable"

        await pilot.press("escape")
        await pilot.pause()
        assert (
            next(choice for choice in app.screen._choices if choice.value == "demo").trailing
            == "ouro • user • disabled"
        )
        await pilot.press("enter")
        await pilot.pause()
        options = app.screen.query_one("#choice-list", OptionList)
        await pilot.click(options, offset=(4, 0))
        await pilot.pause()
        assert app._controller.runtime_skill_names() == ("demo",)
        assert app.screen._choices[0].label == "Disable"

        await pilot.press("escape")
        await pilot.pause()
        assert (
            next(choice for choice in app.screen._choices if choice.value == "demo").trailing
            == "ouro • user • enabled"
        )
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_mcp_configuration_screen_validates_json_and_returns_typed_draft(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    results = []

    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        screen = MCPConfigurationScreen()
        app.push_screen(screen, callback=results.append)
        await pilot.pause()
        assert isinstance(screen, EditableChoiceScreen)
        assert not list(screen.query("Button"))
        assert not list(screen.query("Checkbox"))
        assert not list(screen.query("Select"))
        assert {choice.group for choice in screen._choices} == {
            "IDENTITY",
            "CONNECTION",
            "PROCESS",
            "CREDENTIALS",
            "TOOLS",
            "RUNTIME",
            "ACTIONS",
        }
        options = screen.query_one("#choice-list", OptionList)
        options.highlighted = options.get_option_index("mcp-server-id")
        options.focus()
        await pilot.press("enter")
        await pilot.pause()
        prompt = options.get_option("mcp-server-id").prompt
        assert isinstance(prompt, Text)
        assert "Enter a unique server ID" in prompt.plain
        placeholder_offset = prompt.plain.index("Enter a unique server ID")
        assert prompt.get_style_at_offset(Console(), placeholder_offset).bold is False
        await pilot.press("escape")
        await pilot.pause()

        options.highlighted = options.get_option_index("mcp-server-id")
        options.focus()
        await pilot.press("enter", *"local")
        await pilot.pause()
        prompt = options.get_option("mcp-server-id").prompt
        assert isinstance(prompt, Text)
        assert prompt.get_style_at_offset(Console(), prompt.plain.index("local")).bold is False
        await pilot.press("escape")
        await pilot.pause()

        screen._values.update(
            {
                "mcp-server-id": "local",
                "mcp-name": "Local server",
                "mcp-target": "python",
                "mcp-args": "not-json",
            }
        )
        screen._refresh("mcp-save")
        save = next(choice for choice in screen._choices if choice.value == "mcp-save")
        assert save.disabled is True
        assert "valid JSON" in save.trailing

        screen._values["mcp-args"] = '["-m", "server"]'
        screen._values["mcp-tool-effects"] = '{"read":"read_only"}'
        screen._refresh("mcp-save")
        await pilot.press("enter")
        await pilot.pause()

        assert len(results) == 1
        draft = results[0]
        assert draft is not None
        assert draft.server_id == "local"
        assert draft.transport == "stdio"
        assert draft.target == "python"
        assert draft.args == ["-m", "server"]
        assert draft.tool_effect_overrides == {"read": "read_only"}


@pytest.mark.asyncio
async def test_tool_execution_uses_shared_grouped_inline_menu(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    results = []

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        current = await app._controller.tool_execution_configuration()
        screen = ToolExecutionConfigurationScreen(current)
        app.push_screen(screen, callback=results.append)
        await pilot.pause()

        assert isinstance(screen, EditableChoiceScreen)
        assert not list(screen.query("Button"))
        assert not list(screen.query("Checkbox"))
        assert not list(screen.query("Select"))
        assert [choice.group for choice in screen._choices] == [
            "EXECUTION",
            "EXECUTION",
            "EXECUTION",
            "PATH ACCESS",
            "ACTIONS",
        ]

        options = screen.query_one("#choice-list", OptionList)
        save_calls: list[tuple[str, str]] = []
        save_field = screen._on_save

        async def track_save(field_id: str, value: str) -> tuple[Choice, ...] | None:
            save_calls.append((field_id, value))
            return await save_field(field_id, value)

        screen._on_save = track_save
        await pilot.click(options, offset=(4, 2))
        await pilot.pause()
        assert screen._editing_value == "tool-max-concurrency"
        assert screen._edit_cursor_timer is not None
        assert screen._edit_cursor_visible is True

        screen._toggle_edit_cursor()
        assert screen._edit_cursor_visible is False
        screen.action_edit_home()
        assert screen._edit_cursor_visible is True

        await pilot.press("enter")
        await pilot.pause()
        assert screen._editing_value is None
        assert screen._edit_cursor_timer is None
        assert save_calls == []

        screen._refresh("tool-execution-mode")
        await pilot.press("enter")
        await pilot.pause()
        assert screen._values["tool-execution-mode"] != current.mode

        screen._refresh("tool-workspace-only")
        await pilot.press("enter")
        await pilot.pause()
        assert screen._workspace_only is not current.workspace_only

        screen._refresh("tool-config-save")
        await pilot.press("enter")
        await pilot.pause()
        assert len(results) == 1
        assert results[0].mode != current.mode
        assert results[0].workspace_only is not current.workspace_only


@pytest.mark.asyncio
async def test_shared_inline_editor_keeps_cursor_in_horizontal_viewport(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    long_value = "begin-" + "思考" * 80 + "-end"

    async def save_value(_field_id: str, _value: str) -> tuple[Choice, ...]:
        return ()

    async with app.run_test(size=(64, 24)) as pilot:
        await pilot.pause()
        screen = EditableChoiceScreen(
            title="INLINE EDIT",
            placeholder="Filter values…",
            choices=(
                Choice(
                    "long-value",
                    "Long value",
                    group="VALUE",
                    edit_value=long_value,
                ),
            ),
            on_save=save_value,
        )
        app.push_screen(screen)
        await pilot.pause()

        options = screen.query_one("#choice-list", OptionList)
        options.highlighted = options.get_option_index("long-value")
        options.focus()
        await pilot.press("enter")
        await pilot.pause()

        def rendered_prompt() -> Text:
            screen._edit_cursor_visible = True
            screen._render_editor()
            prompt = options.get_option("long-value").prompt
            assert isinstance(prompt, Text)
            assert any("reverse" in str(span.style) for span in prompt.spans)
            editor_width = (
                options.scrollable_content_region.width
                - options.get_component_styles("option-list--option").padding.width
            )
            assert prompt.cell_len <= editor_width
            return prompt

        prompt = rendered_prompt()
        assert prompt.plain.startswith("…")
        assert prompt.plain.endswith(" ")

        screen.action_edit_home()
        prompt = rendered_prompt()
        assert prompt.plain.startswith("begin-")
        assert prompt.plain.endswith("…")

        screen._edit_cursor = len(long_value) // 2
        screen._reset_edit_cursor()
        prompt = rendered_prompt()
        assert prompt.plain.startswith("…")
        assert prompt.plain.endswith("…")

        screen.action_edit_end()
        await pilot.resize_terminal(42, 18)
        await pilot.pause()
        prompt = rendered_prompt()
        assert prompt.plain.startswith("…")
        assert prompt.plain.endswith(" ")


@pytest.mark.asyncio
async def test_subagent_policy_uses_shared_grouped_inline_menu(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    results = []

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        current = await app._controller.agent_runtime_configuration()
        catalog = await app._controller.agent_definition_catalog()
        models = await app._controller.models()
        screen = AgentDispatchPolicyScreen(
            current,
            definitions=catalog.definitions,
            models=models,
        )
        app.push_screen(screen, callback=results.append)
        await pilot.pause()

        assert isinstance(screen, EditableChoiceScreen)
        assert not list(screen.query("Button"))
        assert not list(screen.query("Checkbox"))
        assert not list(screen.query("Select"))
        assert {choice.group for choice in screen._choices} == {
            "ALLOWLISTS",
            "EXECUTION",
            "CAPABILITIES",
            "ACTIONS",
        }
        assert (
            next(
                choice for choice in screen._choices if choice.value == "agent-policy-agents"
            ).edit_value
            is None
        )

        options = screen.query_one("#choice-list", OptionList)
        options.highlighted = options.get_option_index("agent-policy-agents")
        options.focus()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="ALLOWED SUBAGENTS")
        general = next(choice for choice in app.screen._choices if choice.value == "general")
        assert general.marker == DEFAULT_TUI_THEME.selected_choice_marker
        await pilot.press("enter")
        await pilot.pause()
        general = next(choice for choice in app.screen._choices if choice.value == "general")
        assert general.marker == ""
        await pilot.press("enter", "escape")
        await _wait_for_screen(
            app,
            AgentDispatchPolicyScreen,
            title="SUBAGENT DISPATCH POLICY",
        )

        options = screen.query_one("#choice-list", OptionList)
        options.highlighted = options.get_option_index("agent-policy-models")
        options.focus()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="ALLOWED SUBAGENT MODELS")
        assert (
            next(choice for choice in app.screen._choices if choice.value == models[0].key).marker
            == DEFAULT_TUI_THEME.selected_choice_marker
        )
        await pilot.press("escape")
        await _wait_for_screen(
            app,
            AgentDispatchPolicyScreen,
            title="SUBAGENT DISPATCH POLICY",
        )

        await screen._select_choice("agent-policy-write")
        screen._replace_choices(
            screen._policy_choices(),
            selected="agent-policy-save",
        )
        await pilot.press("enter")
        await pilot.pause()

        assert len(results) == 1
        assert results[0].allowed_agent_id_patterns == ("general",)
        assert results[0].allowed_model_patterns == (models[0].key,)
        assert results[0].allow_write is False


@pytest.mark.asyncio
async def test_subagent_definition_editor_does_not_limit_selected_tool_count(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    tools = tuple(
        AgentToolOptionView(
            name=f"tool_{index}",
            description=f"Tool {index}",
            effect="read_only",
            owner_id="ouro.host",
            execution_contract_hash=f"sha256:{index}",
        )
        for index in range(40)
    )

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        models = await app._controller.models()
        screen = AgentDefinitionConfigurationScreen(tools, models)
        app.push_screen(screen)
        await pilot.pause()

        assert isinstance(screen, EditableChoiceScreen)
        assert not list(screen.query("Button"))
        assert not list(screen.query("Checkbox"))
        assert not list(screen.query("Select"))
        save = next(choice for choice in screen._choices if choice.value == "agent-definition-save")
        assert save.trailing == "Agent ID is required"
        model_choice = next(
            choice for choice in screen._choices if choice.value == "agent-definition-model"
        )
        assert model_choice.trailing == "Inherit session model"

        options = screen.query_one("#choice-list", OptionList)
        options.highlighted = options.get_option_index("agent-definition-model")
        options.focus()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="SUBAGENT MODEL")
        assert (
            next(
                choice for choice in app.screen._choices if choice.value == "__ouro_runtime_model__"
            ).marker
            == DEFAULT_TUI_THEME.selected_choice_marker
        )
        model_options = app.screen.query_one("#choice-list", OptionList)
        model_options.highlighted = model_options.get_option_index(models[0].key)
        model_options.focus()
        await pilot.press("enter")
        await _wait_for_screen(
            app,
            AgentDefinitionConfigurationScreen,
            title="CREATE SUBAGENT",
        )
        screen._values.update(
            {
                "agent-definition-id": "Invalid ID",
                "agent-definition-name": "Tool reviewer",
                "agent-definition-description": "Review tool contracts.",
                "agent-definition-prompt": "Review every selected tool contract.",
            }
        )
        error = screen._validation_error()
        assert "lowercase stable identifier" in error
        assert "pydantic.dev" not in error
        assert "validation error" not in error
        screen._values["agent-definition-id"] = "tool-reviewer"
        screen._tool_mode = "selected"
        screen._selected_tools = set(screen._tools)

        draft = screen._draft()
        tool_choices = tuple(
            choice
            for choice in screen._definition_choices()
            if choice.value.startswith("agent-definition-tool:")
        )

        assert draft.model == models[0].key
        assert draft.tool_selection.mode == "selected"
        assert len(draft.tool_selection.names) == 40
        assert all(choice.trailing == "read only" for choice in tool_choices)
        assert all("selected" not in choice.trailing for choice in tool_choices)
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_skill_install_uses_shared_grouped_inline_menu(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    results = []

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        screen = SkillInstallScreen()
        app.push_screen(screen, callback=results.append)
        await pilot.pause()

        assert isinstance(screen, EditableChoiceScreen)
        assert not list(screen.query("Button"))
        assert not list(screen.query("Checkbox"))
        assert not list(screen.query("Select"))
        assert [choice.group for choice in screen._choices] == [
            "SOURCE",
            "DESTINATION",
            "SELECTION",
            "SELECTION",
            "VERSION",
            "OPTIONS",
            "ACTIONS",
        ]
        install = next(choice for choice in screen._choices if choice.value == "skill-install")
        assert install.disabled is True
        assert install.trailing == "Source is required"

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
async def test_compaction_uses_shared_grouped_inline_menu_and_returns_typed_draft(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    results = []

    async with app.run_test(size=(100, 42)) as pilot:
        await pilot.pause()
        runtime_models = await app._controller.models()
        unsupported_model = runtime_models[0]
        capable_model = unsupported_model.model_copy(
            update={
                "key": "faux/thinking",
                "provider_id": "faux",
                "endpoint_id": "thinking",
                "display_name": "Thinking model",
                "reasoning": ReasoningCapability(
                    activation="toggle",
                    supported_efforts=("low", "high"),
                    default=ReasoningRequest(mode="enabled", effort="low"),
                    output="summary",
                ),
            }
        )
        models = (unsupported_model, capable_model)
        screen = CompactionConfigurationScreen(
            await app._controller.compaction_configuration(),
            models=models,
            strategy_ids=app._controller.compaction_strategies(),
        )
        app.push_screen(screen, callback=results.append)
        await pilot.pause()
        assert isinstance(screen, EditableChoiceScreen)
        assert not list(screen.query("Button"))
        assert not list(screen.query("Checkbox"))
        assert not list(screen.query("Select"))
        assert [choice.group for choice in screen._choices] == [
            "STRATEGY",
            "STRATEGY",
            "STRATEGY",
            "THRESHOLDS",
            "THRESHOLDS",
            "MODEL",
            "MODEL",
            "MODEL",
            "MODEL",
            "ACTIONS",
        ]

        choices = {choice.value: choice for choice in screen._choices}
        assert choices["compaction-strategy"].disabled is True
        assert choices["compaction-fallback"].disabled is True
        assert choices["compaction-temperature"].disabled is True
        assert choices["compaction-max-output"].disabled is True
        assert choices["compaction-reasoning"].disabled is True

        screen._refresh("compaction-model")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ChoiceScreen)
        assert app.screen is not screen
        assert app.screen.query_one("#choice-filter", Input).placeholder == "Filter models…"
        model_choices = app.screen._choices
        assert model_choices[0].group == "SESSION"
        assert {choice.group for choice in model_choices[1:]} == {
            unsupported_model.provider_id,
            "faux",
        }
        app.screen.query_one("#choice-filter", Input).value = "Thinking model"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen is screen
        assert screen._values["compaction-model"] == capable_model.key
        choices = {choice.value: choice for choice in screen._choices}
        assert choices["compaction-temperature"].disabled is False
        assert choices["compaction-max-output"].disabled is False
        assert choices["compaction-reasoning"].disabled is False

        screen._values["compaction-trigger"] = "0.4"
        screen._values["compaction-target"] = "0.5"
        screen._refresh("compaction-save")
        save = next(choice for choice in screen._choices if choice.value == "compaction-save")
        assert save.disabled is True
        assert "lower than" in save.trailing

        screen._values["compaction-target"] = "0.2"
        screen._refresh("compaction-save")
        await pilot.press("enter")
        await pilot.pause()
        assert len(results) == 1
        assert results[0].trigger_ratio == 0.4
        assert results[0].target_ratio == 0.2
        assert results[0].model_key == capable_model.key


@pytest.mark.asyncio
async def test_plugin_commands_complete_and_execute_through_controller_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    executed: list[tuple[str, str]] = []

    async def execute(selector: str, argument: str) -> TuiPluginCommandExecution:
        executed.append((selector, argument))
        return TuiPluginCommandExecution(
            execution=PluginCommandExecutionView(command_id="dev.ouro.demo:hello", output="hello")
        )

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(
            app._controller,
            "plugin_commands",
            lambda: (
                PluginCommandView(
                    command_id="dev.ouro.demo:hello",
                    plugin_id="dev.ouro.demo",
                    name="hello",
                    description="Say hello",
                ),
            ),
        )
        monkeypatch.setattr(app._controller, "execute_plugin_command", execute)

        await pilot.press("slash")
        await pilot.pause()
        popup = app.query_one(CommandPopup)
        popup_text = popup.query_one("#command-popup-content", Static).render().plain
        assert "/hello" in popup_text
        app.query_one(Composer).clear()

        await pilot.press("slash", *"hello world", "enter")
        await pilot.pause()

        assert executed == [("hello", "world")]
        notices = "\n".join(
            entry.text
            for entry in app.query_one(ConversationHistory).entries
            if entry.kind == "notice"
        )
        assert "hello" in notices


@pytest.mark.asyncio
async def test_slash_popup_loads_active_runtime_plugin_commands(tmp_path: Path) -> None:
    application = _application(tmp_path)
    await application.start()
    assert application.plugins is not None
    enabled = await application.plugins.enable("dev.ouro.goal")
    assert enabled.status == "active"
    goal = next(item for item in application.plugins.list_commands() if item.name == "goal")
    assert [item.value for item in goal.argument_options] == [
        "status",
        "pause",
        "resume",
        "cancel",
        "replace",
    ]
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.press("slash")
        await pilot.pause()

        popup = app.query_one(CommandPopup)
        content = popup.query_one("#command-popup-content", Static).render().plain
        assert "/goal" in content


@pytest.mark.asyncio
async def test_abort_and_quit_commands_have_independent_lifecycles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    aborted = False

    async def fake_abort() -> None:
        nonlocal aborted
        aborted = True

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(app._controller, "abort", fake_abort)
        app._projection = TuiSessionProjection("session_1")
        app._projection.apply(
            EventRecord(
                session_id="session_1",
                run_id="run_1",
                event_type="run_started",
                sequence=1,
            )
        )

        await app._dispatch_command("/abort")
        await pilot.pause()

        assert aborted is True
        assert app.is_running is True

        await app._dispatch_command("/quit")
        await pilot.pause()
        assert app.is_running is False


@pytest.mark.asyncio
async def test_slash_opens_filters_navigates_and_executes_command_popup(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.click("#composer")
        await pilot.press("slash")
        await pilot.pause()

        popup = app.query_one(CommandPopup)
        assert popup.display is True
        popup_content = popup.query_one("#command-popup-content", Static)
        assert popup_content.content_region.width == popup.scrollable_content_region.width
        assert "abort" in app.export_screenshot()
        popup_text = popup_content.render().plain
        assert "/config" not in popup_text
        assert "/tools" not in popup_text
        assert "/mcp" not in popup_text
        assert "/skills" not in popup_text
        assert popup.selected_command is not None
        assert popup.selected_command.name == "/abort"

        await pilot.press("down")
        await pilot.pause()
        assert popup.selected_command is not None
        assert popup.selected_command.name == "/actions"

        await pilot.press("down")
        await pilot.pause()
        assert popup.selected_command is not None
        assert popup.selected_command.name == "/approval"
        assert popup.scroll_y == 0

        for _ in range(20):
            if popup.selected_command is not None and popup.selected_command.name == "/skill":
                break
            await pilot.press("down")
        await pilot.pause()
        assert popup.selected_command is not None
        assert popup.selected_command.name == "/skill"

        await pilot.press("escape")
        assert popup.display is False
        assert app.query_one(Composer).text == "/"

        app.query_one(Composer).clear()
        await pilot.press("slash", "h")
        await pilot.pause()
        assert popup.display is True
        assert popup.selected_command is not None
        assert popup.selected_command.name == "/help"

        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)


@pytest.mark.asyncio
async def test_command_and_approval_menus_fit_resized_terminal(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text("start session")
        await pilot.press("enter")
        await _wait_for_worker_group(app, pilot, group="run")

        await pilot.resize_terminal(80, 16)
        await pilot.click("#composer")
        await pilot.press("slash")
        await pilot.pause()
        popup = app.query_one(CommandPopup)
        assert popup.region.height == 9
        assert popup.region.bottom == composer.region.y
        assert app.query_one(StatusLine).region.bottom == app.screen.region.bottom

        await pilot.resize_terminal(40, 12)
        await pilot.pause()
        assert popup.region.height == 5
        assert popup.region.right <= app.screen.region.right
        assert popup.region.bottom == composer.region.y
        assert app.query_one(StatusLine).region.bottom == app.screen.region.bottom

        composer.load_text("/approval ")
        composer.move_cursor((0, len(composer.text)))
        await pilot.pause()
        assert popup.argument_command is not None
        assert popup.argument_command.name == "/approval"
        assert popup.region.bottom == composer.region.y
        assert popup.region.right <= app.screen.region.right

        for _ in range(len(popup._arguments) - 1):
            await pilot.press("down")
        await pilot.pause()
        assert popup.selected_argument == popup._arguments[-1]
        assert popup.scroll_y > 0

        await pilot.resize_terminal(100, 32)
        composer.load_text("/")
        composer.move_cursor((0, 1))
        await pilot.pause()
        assert popup.region.height == 14
        assert popup.region.bottom == composer.region.y


@pytest.mark.asyncio
async def test_workspace_command_selects_paths_only_before_session_and_keeps_clear_available(
    tmp_path: Path,
) -> None:
    historical = tmp_path / "historical"
    freeform = tmp_path / "custom workspace"
    rejected = tmp_path / "rejected"
    for workspace in (historical, freeform, rejected):
        workspace.mkdir()
    preferences = TuiPreferencesStore(tmp_path)
    preferences.save(TuiPreferences(recent_workspaces=(str(historical.resolve()),)))
    application = _application(tmp_path)
    app = OuroTuiApp(home=tmp_path, application=application)

    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        await pilot.click("#composer")
        await pilot.press("slash", *"workspace", "enter")
        await pilot.pause()

        popup = app.query_one(CommandPopup)
        assert len(app.screen_stack) == 1
        assert popup.argument_command is not None
        assert popup.argument_command.name == "/workspace"
        assert [argument.value for argument in popup._arguments] == [
            str(tmp_path.resolve()),
            str(historical.resolve()),
            "clear",
        ]
        assert [argument.display_label for argument in popup._arguments] == [
            str(tmp_path.resolve()),
            str(historical.resolve()),
            "Clear",
        ]
        assert all(not argument.description for argument in popup._arguments)
        assert popup._arguments[-1].accent == DEFAULT_TUI_THEME.danger
        workspace_menu = popup.query_one("#command-popup-content", Static).render()
        assert isinstance(workspace_menu, Content)
        assert "Current workspace" not in workspace_menu.plain
        assert "Recent workspace" not in workspace_menu.plain
        clear_style = workspace_menu.get_style_at_offset(workspace_menu.plain.index("Clear"))
        assert clear_style.foreground == Color.parse(DEFAULT_TUI_THEME.danger)

        await pilot.press("down", "enter")
        await pilot.pause()
        assert app._active_workspace == str(historical.resolve())
        assert preferences.load().recent_workspaces[:2] == (
            str(historical.resolve()),
            str(tmp_path.resolve()),
        )

        composer = app.query_one(Composer)
        composer.load_text(f"/workspace {freeform}")
        composer.move_cursor((0, len(composer.document.lines[0])))
        await pilot.pause()
        assert popup.argument_command is not None
        assert popup.argument_command.name == "/workspace"
        assert popup.selected_argument is None

        await pilot.press("enter")
        await pilot.pause()
        assert app._active_workspace == str(freeform.resolve())
        assert preferences.load().recent_workspaces[:3] == (
            str(freeform.resolve()),
            str(historical.resolve()),
            str(tmp_path.resolve()),
        )

        composer.load_text("create the session")
        await pilot.press("enter")
        await _wait_until(
            lambda: app._controller.session_id is not None,
            pilot,
            description="workspace session creation",
        )
        session_id = app._controller.session_id
        assert session_id is not None
        session = application.runtime.store.load_session_record(session_id)
        assert session.workspace == str(freeform.resolve())
        assert "/workspace" in {command.name for command in app._command_catalog()}

        composer.load_text("/workspace ")
        composer.move_cursor((0, len(composer.document.lines[0])))
        await pilot.pause()
        assert [argument.value for argument in popup._arguments] == ["clear"]
        assert popup.argument_command is not None
        assert popup.argument_command.input_hint is None

        await app._dispatch_command(f"/workspace {rejected}")
        await pilot.pause()
        unchanged = application.runtime.store.load_session_record(session_id)
        assert unchanged.workspace == str(freeform.resolve())


@pytest.mark.asyncio
async def test_workspace_history_records_startup_directory_and_keeps_twenty(
    tmp_path: Path,
) -> None:
    preferences = TuiPreferencesStore(tmp_path)
    previous = tuple(f"C:/workspace/{index}" for index in range(20))
    preferences.save(TuiPreferences(recent_workspaces=previous))
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        stored = preferences.load().recent_workspaces
        assert stored == (str(tmp_path.resolve()), *previous[:19])


@pytest.mark.asyncio
async def test_workspace_clear_argument_forgets_saved_history_without_changing_workspace(
    tmp_path: Path,
) -> None:
    historical = tmp_path / "historical"
    historical.mkdir()
    preferences = TuiPreferencesStore(tmp_path)
    preferences.save(TuiPreferences(recent_workspaces=(str(historical.resolve()),)))
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with app.run_test(size=(100, 32), notifications=True) as pilot:
        await pilot.pause()
        active_workspace = app._active_workspace
        composer = app.query_one(Composer)
        composer.load_text("/workspace clear")
        composer.move_cursor((0, len(composer.document.lines[0])))
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert preferences.load().recent_workspaces == ()
        assert app._active_workspace == active_workspace
        assert "Recent workspaces cleared" in {
            toast.render().plain
            for toast in app.screen.query_one("#textual-toastrack").query("Toast")
        }


@pytest.mark.asyncio
async def test_slash_prefix_filters_new_command_text_and_restores_existing_draft(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        popup = app.query_one(CommandPopup)
        draft = "Compare these two implementations"
        composer.load_text(draft)
        composer.move_cursor((0, 0))
        await pilot.pause()

        await pilot.press("slash")
        await pilot.pause()

        assert composer.text == f"/{draft}"
        assert popup.display is True
        assert len(popup._commands) == len(app._command_catalog())

        await pilot.press(*"model")
        await pilot.pause()

        assert popup.selected_command is not None
        assert popup.selected_command.name == "/model"
        assert composer.text == f"/model{draft}"

        await pilot.press("enter")
        await pilot.pause()

        assert popup.argument_command is not None
        assert popup.argument_command.name == "/model"
        assert composer.text == f"/model {draft}"

        await pilot.press("enter")
        await pilot.pause()

        assert popup.display is False
        assert composer.text == draft

        composer.move_cursor((0, 0))
        await pilot.press("slash", *"not-a-command")
        await pilot.pause()
        assert popup.display is False

        await pilot.press("escape")
        await pilot.pause()
        assert composer.text == draft


@pytest.mark.asyncio
async def test_command_popup_hides_when_no_commands_match(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.press("slash", *"not-a-command")
        await pilot.pause()

        popup = app.query_one(CommandPopup)
        assert popup.display is False
        assert popup.selected_command is None
        assert "No matching commands" not in app.export_screenshot()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ("actions", "menu"))
async def test_actions_command_opens_action_menu(tmp_path: Path, command: str) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.press("slash", *command, "enter")
        await pilot.pause()

        assert isinstance(app.screen, ChoiceScreen)
        assert app.screen.query_one("#choice-title", Static).render().plain == "ACTIONS"


@pytest.mark.asyncio
async def test_model_command_selects_from_inline_argument_menu(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 32), notifications=True) as pilot:
        await pilot.pause()
        await pilot.press("slash", *"model", "enter")
        await pilot.pause()

        composer = app.query_one(Composer)
        popup = app.query_one(CommandPopup)
        assert not isinstance(app.screen, ChoiceScreen)
        assert composer.text == "/model "
        assert popup.argument_command is not None
        assert popup.argument_command.name == "/model"
        assert popup.selected_argument is not None
        assert popup.selected_argument.value == "faux/faux"

        await pilot.press("enter")
        await pilot.pause()
        assert composer.text == ""
        assert app._active_model == "faux"
        assert not list(app.screen.query_one("#textual-toastrack").query("Toast"))


@pytest.mark.asyncio
async def test_model_command_refreshes_endpoints_configured_after_tui_start(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=LocalApplication.build(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        await app._controller.add_provider(
            TuiProviderConfiguration(
                provider_id="hot-provider",
                provider_type="openai-compatible",
                base_url="https://models.example.invalid/v1",
                api_path="/chat/completions",
                api_key=None,
                api_key_env=None,
                timeout=60.0,
            )
        )
        await app._controller.add_endpoint(
            TuiEndpointConfiguration(
                provider_id="hot-provider",
                endpoint_id="hot-model",
                model_id="provider-model-id",
                context_window=128_000,
                max_output_tokens=8192,
            )
        )

        assert "hot-provider/hot-model" not in {model.key for model in app._models}

        await pilot.press("slash", *"model", "space")
        await pilot.pause()

        popup = app.query_one(CommandPopup)
        assert popup.argument_command is not None
        assert popup.argument_command.name == "/model"
        assert popup.selected_argument is not None
        assert popup.selected_argument.value == "hot-provider/hot-model"
        assert app._system_status.provider_ids == ("hot-provider",)
        assert app._system_status.model_count == 1

        await pilot.press("enter")
        await pilot.pause()
        assert app.query_one(Composer).text == ""
        assert app._active_model == "hot-model"


@pytest.mark.asyncio
async def test_model_command_shows_endpoint_and_provider_without_changing_route_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    selected: list[str] = []

    async def select_model(selector: str) -> AvailableModel:
        selected.append(selector)
        return app._models[0]

    async def models() -> tuple[AvailableModel, ...]:
        return app._models

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        app._models = (
            AvailableModel(
                key="supplier/model-endpoint",
                provider_id="supplier",
                endpoint_id="model-endpoint",
                model_id="provider-model-id",
            ),
        )
        monkeypatch.setattr(app._controller, "models", models)
        monkeypatch.setattr(app._controller, "select_model", select_model)
        await pilot.press("slash", *"model", "space", *"model")
        await pilot.pause()

        composer = app.query_one(Composer)
        popup = app.query_one(CommandPopup)
        argument = popup.selected_argument
        assert argument is not None
        assert argument.value == "supplier/model-endpoint"
        assert argument.display_label == "model-endpoint"
        assert argument.description == "supplier"
        assert composer.suggestion == ""

        content = popup.query_one("#command-popup-content", Static)
        rendered = content.render().plain.splitlines()[0]
        assert rendered.startswith("› model-endpoint  supplier")
        assert rendered.rindex("supplier") < content.content_region.width - len("supplier")

        await pilot.press("enter")
        await pilot.pause()
        assert composer.text == ""
        assert selected == ["supplier/model-endpoint"]


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

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.press("slash", *"skill", "enter")
        await pilot.pause()

        popup = app.query_one(CommandPopup)
        assert app.query_one(Composer).text == "/skill "
        assert popup.argument_command is not None
        assert popup.argument_command.name == "/skill"
        assert popup.selected_argument is not None
        assert popup.selected_argument.value == "reviewer"

        await pilot.press("enter")
        await pilot.pause()
        composer = app.query_one(Composer)
        assert composer.text == "/skill reviewer "
        assert composer.suggestion == "<task>"

        await pilot.press(*"inspect", "space", *"app.py", "enter")
        await _wait_for_worker_group(app, pilot, group="run")
        await _wait_until(
            lambda: app._projection is not None and app._projection.activity.status == "completed",
            pilot,
            description="skill-activated run",
        )

        assert app.query_one(ConversationHistory).user_turns == ("/skill reviewer inspect app.py",)
        assert app._controller.session_id is not None
        snapshot = application.runtime.store.load_runtime_session(app._controller.session_id)
        message = next(item for item in snapshot.messages if item.skill_activations)
        assert message.skill_activations[0].name == "reviewer"
        assert message.text_content == "inspect app.py"


@pytest.mark.asyncio
async def test_skill_selection_prompts_for_instructions_without_metadata_hint(
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

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.press("slash", *"skill", "enter")
        await pilot.pause()

        await pilot.press("enter")
        await pilot.pause()

        composer = app.query_one(Composer)
        assert composer.text == "/skill imagegen "
        assert composer.suggestion == "<instruction>"
        assert app.query_one(CommandPopup).display is True


@pytest.mark.asyncio
async def test_command_selection_executes_complete_argument(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    executed: list[tuple[str, str]] = []

    async def execute(selector: str, argument: str) -> TuiPluginCommandExecution:
        executed.append((selector, argument))
        return TuiPluginCommandExecution(
            execution=PluginCommandExecutionView(command_id="dev.ouro.plan:plan")
        )

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(
            app._controller,
            "plugin_commands",
            lambda: (
                PluginCommandView(
                    command_id="dev.ouro.plan:plan",
                    plugin_id="dev.ouro.plan",
                    name="plan",
                    description="Enter Plan mode",
                    usage="[on|off|status]",
                    argument_options=(
                        PluginCommandArgumentView(value="on", description="Enter Plan mode"),
                        PluginCommandArgumentView(value="off", description="Leave Plan mode"),
                        PluginCommandArgumentView(value="status", description="Show mode status"),
                    ),
                ),
            ),
        )
        monkeypatch.setattr(app._controller, "execute_plugin_command", execute)

        await pilot.press("slash", "p", "l", "a", "n", "enter")
        await pilot.pause()

        composer = app.query_one(Composer)
        popup = app.query_one(CommandPopup)
        assert composer.text == "/plan "
        assert composer.suggestion == ""
        assert popup.argument_command is not None
        assert popup.argument_command.name == "/plan"
        assert popup.selected_argument is not None
        assert popup.selected_argument.value == "on"
        assert executed == []

        await pilot.press("down", "enter")
        await pilot.pause()
        assert composer.text == ""
        assert executed == [("plan", "off")]


@pytest.mark.asyncio
async def test_parameterized_command_opens_argument_menu_and_executes_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    executed: list[tuple[str, str]] = []

    async def execute(selector: str, argument: str) -> TuiPluginCommandExecution:
        executed.append((selector, argument))
        return TuiPluginCommandExecution(
            execution=PluginCommandExecutionView(command_id="dev.ouro.goal:goal")
        )

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(
            app._controller,
            "plugin_commands",
            lambda: (
                PluginCommandView(
                    command_id="dev.ouro.goal:goal",
                    plugin_id="dev.ouro.goal",
                    name="goal",
                    description="Control a Goal",
                    usage=(
                        "[--token-budget N] <objective> | status | pause | "
                        "resume [--token-budget N] | cancel | "
                        "replace [--token-budget N] <objective>"
                    ),
                    argument_options=(
                        PluginCommandArgumentView(
                            value="status", description="Show the current Goal"
                        ),
                        PluginCommandArgumentView(
                            value="pause", description="Pause the active Goal"
                        ),
                        PluginCommandArgumentView(
                            value="resume", description="Resume a paused Goal"
                        ),
                        PluginCommandArgumentView(
                            value="cancel", description="Cancel the current Goal"
                        ),
                        PluginCommandArgumentView(
                            value="replace",
                            description="Replace the current Goal",
                            usage="[--token-budget N] <objective>",
                        ),
                    ),
                ),
            ),
        )
        monkeypatch.setattr(app._controller, "execute_plugin_command", execute)

        await pilot.press("slash", "g", "o", "a", "l", "space")
        await pilot.pause()

        composer = app.query_one(Composer)
        popup = app.query_one(CommandPopup)
        assert composer.text == "/goal "
        assert composer.suggestion == "--token-budget N <objective>"
        assert popup.argument_command is not None

        await pilot.press("left")
        await pilot.pause()
        assert composer.cursor_location == (0, len("/goal"))
        assert composer.suggestion == ""

        await pilot.press("right")
        await pilot.pause()
        assert composer.text == "/goal "
        assert composer.suggestion == "--token-budget N <objective>"

        await pilot.press("right")
        await pilot.pause()
        assert composer.text == "/goal --token-budget N <objective>"
        assert "[" not in composer.text
        assert "]" not in composer.text

        composer.load_command("/goal ")
        await pilot.pause()
        await pilot.press("p", "a", "u")
        await pilot.pause()
        assert popup.selected_argument is not None
        assert popup.selected_argument.value == "pause"
        assert composer.suggestion == "se"
        content = popup.query_one("#command-popup-content", Static).render().plain
        assert "pause" in content
        assert "status" not in content

        await pilot.press("enter")
        await pilot.pause()
        assert composer.text == ""
        assert executed == [("goal", "pause")]


@pytest.mark.asyncio
async def test_deleting_command_argument_updates_suggestion_in_same_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    hint = "--token-budget N <objective>"

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(
            app,
            "_command_catalog",
            lambda: (
                TuiCommand(
                    "/goal",
                    "Goal",
                    "Control a Goal",
                    "<objective> | replace [--token-budget N] <objective>",
                    argument_options=(
                        TuiCommandArgument(
                            "replace",
                            "Replace the current Goal",
                            "[--token-budget N] <objective>",
                        ),
                    ),
                ),
            ),
        )
        composer = app.query_one(Composer)
        composer.load_command("/goal replace")
        await pilot.pause()
        assert composer.suggestion == f" {hint}"

        transitions: list[tuple[str, str]] = []
        app.watch(
            composer,
            "suggestion",
            lambda old, new: transitions.append((old, new)),
            init=False,
        )

        previous = f" {hint}"
        expected_suggestions = (
            f"e {hint}",
            f"ce {hint}",
            f"ace {hint}",
            f"lace {hint}",
            f"place {hint}",
            f"eplace {hint}",
            "<objective>",
        )
        for expected in expected_suggestions:
            transitions.clear()
            composer.action_delete_left()
            assert composer.suggestion == expected
            await pilot.pause()
            assert composer.suggestion == expected
            assert transitions == [(previous, expected)]
            previous = expected

        transitions.clear()
        composer.action_delete_left()
        assert composer.suggestion == ""
        await pilot.pause()
        assert composer.text == "/goal"
        assert composer.suggestion == ""
        assert transitions == [(previous, "")]

        transitions.clear()
        composer.action_delete_left()
        assert composer.suggestion == ""
        await pilot.pause()
        assert composer.text == "/goa"
        assert composer.suggestion == ""
        assert transitions == []


@pytest.mark.asyncio
async def test_plan_command_without_dialogue_keeps_welcome_and_updates_status_line(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    await application.start()
    assert application.plugins is not None
    enabled = await application.plugins.enable("dev.ouro.plan")
    assert enabled.status == "active"
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.press("slash", "p", "l", "a", "n", "space", "o", "n", "enter")
        await _wait_for_worker_group(app, pilot, group="plugin-command")
        await _wait_until(
            lambda: "Plan" in app.query_one(ActivityIndicator).render().plain,
            pilot,
            description="active plan-mode status",
        )

        conversation = app.query_one(ConversationView)
        indicator = app.query_one(ActivityIndicator)
        assert app._projection is None
        assert conversation.showing_welcome is True
        assert conversation.query_one(ConversationHistory).entries == ()
        assert app._has_active_run() is False
        assert app.query_one(Composer).disabled is False
        assert indicator.render().plain.startswith(" ▣ Plan • Ready")

        await pilot.press("slash", "p", "l", "a", "n", "space", "o", "f", "f", "enter")
        await _wait_for_worker_group(app, pilot, group="plugin-command")

        assert app._projection is None
        assert conversation.showing_welcome is True
        assert conversation.query_one(ConversationHistory).entries == ()
        assert indicator.render().plain.startswith(" ▣ Ready")
        assert "Plan" not in indicator.render().plain


@pytest.mark.asyncio
async def test_plan_status_keeps_its_conversation_notice(tmp_path: Path) -> None:
    application = _application(tmp_path)
    await application.start()
    assert application.plugins is not None
    assert (await application.plugins.enable("dev.ouro.plan")).status == "active"
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.press("slash", *"plan status", "enter")
        await _wait_for_worker_group(app, pilot, group="plugin-command")

        notices = tuple(
            entry.text
            for entry in app.query_one(ConversationHistory).entries
            if entry.kind == "notice"
        )
        assert notices == ("Plan mode phase: inactive.",)
        assert app.query_one(ConversationView).showing_welcome is False


@pytest.mark.asyncio
async def test_loaded_session_projects_active_harness_into_status_line(tmp_path: Path) -> None:
    application = _application(tmp_path)
    await application.start()
    assert application.plugins is not None
    assert (await application.plugins.enable("dev.ouro.plan")).status == "active"
    session = application.runtime.create_session(title="Active harness")
    await application.plugins.execute_command(
        "dev.ouro.plan:plan",
        "on",
        session_id=session.session_id,
        workspace=str(tmp_path),
    )
    app = OuroTuiApp(application=application, resume_session_id=session.session_id)

    async with app.run_test(size=(100, 32)) as pilot:
        await _wait_for_active_session(app, pilot, session.session_id)

        indicator = app.query_one(ActivityIndicator)
        await _wait_until(
            lambda: "Plan" in indicator.render().plain,
            pilot,
            description="restored harness status",
        )
        assert indicator.render().plain.startswith(" ▣ Plan • Ready")

        await pilot.press("slash", *"plan off", "enter")
        await _wait_for_worker_group(app, pilot, group="plugin-command")

        assert "Plan" not in indicator.render().plain
        notices = tuple(
            entry.text
            for entry in app.query_one(ConversationHistory).entries
            if entry.kind == "notice"
        )
        assert not any("Plan mode deactivated" in notice for notice in notices)


@pytest.mark.asyncio
async def test_approval_panel_selects_with_arrows_and_resumes_run(tmp_path: Path) -> None:
    target = tmp_path / "approved.txt"
    app = OuroTuiApp(
        application=_approval_application(tmp_path, target),
        approval_mode="approval",
    )

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.click("#composer")
        await pilot.press("w", "r", "i", "t", "e", "enter")
        await _wait_until(
            lambda: app._projection is not None
            and app._projection.pending_interaction is not None,
            pilot,
            description="tool approval request",
        )

        assert app._projection is not None
        assert app._projection.pending_interaction is not None
        assert app._projection.pending_interaction.kind == "approval"
        panel = app.query_one(InteractionBar)
        composer = app.query_one(Composer)
        assert app.query_one(SubagentNavigationPanel).region.bottom == panel.region.y
        assert panel.region.bottom == composer.region.y
        assert panel.has_focus is True
        assert panel.selected_approved is True
        assert composer.disabled is True
        title = panel.query_one("#interaction-title", Static).render().plain
        controls = panel.query_one("#interaction-controls", Static).render().plain
        assert title == "APPROVAL"
        assert "1. Allow" not in controls
        assert "2. Deny" not in controls
        assert "Y/N direct" not in controls
        assert "↑/↓ move • Enter choose" in controls
        assert "←/→" not in controls

        await pilot.press("n")
        await pilot.pause()
        assert app._projection.pending_interaction is not None
        assert panel.selected_approved is True

        await pilot.press("left", "right")
        await pilot.pause()
        assert panel.selected_approved is True

        await pilot.press("down")
        assert panel.selected_approved is False

        await pilot.press("up")
        assert panel.selected_approved is True

        await pilot.press("enter")
        await _wait_until(
            lambda: app._projection is not None and app._projection.activity.status == "completed",
            pilot,
            description="approved run completion",
        )

        assert target.read_text(encoding="utf-8") == "written"
        assert app._projection.pending_interaction is None
        assert app._projection.activity.status == "completed"
        assert composer.disabled is False


@pytest.mark.asyncio
async def test_approval_panel_keeps_selected_action_visible_in_short_terminal(
    tmp_path: Path,
) -> None:
    target = tmp_path / "approved.txt"
    app = OuroTuiApp(
        application=_approval_application(tmp_path, target),
        approval_mode="approval",
    )

    async with app.run_test(size=(40, 12)) as pilot:
        await pilot.pause()
        await pilot.click("#composer")
        await pilot.press(*"write", "enter")
        await _wait_until(
            lambda: app._projection is not None and app._projection.pending_interaction is not None,
            pilot,
            description="approval in short terminal",
        )

        panel = app.query_one(InteractionBar)
        composer = app.query_one(Composer)
        assert panel.region.height == 5
        assert panel.region.right <= app.screen.region.right
        assert panel.region.bottom == composer.region.y
        assert app.query_one(StatusLine).region.bottom == app.screen.region.bottom

        await pilot.press("down")
        await pilot.pause()
        assert panel.selected_approved is False
        assert panel.scroll_y > 0
        assert "Deny" in app.export_screenshot()


@pytest.mark.asyncio
async def test_parallel_approvals_advance_one_at_a_time_without_stalling(tmp_path: Path) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    app = OuroTuiApp(
        application=_multi_approval_application(tmp_path, (first, second)),
        approval_mode="approval",
    )

    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.pause()
        await pilot.click("#composer")
        await pilot.press("r", "u", "n", "enter")
        await _wait_until(
            lambda: (
                app._projection is not None
                and len(app._projection.pending_interactions) == 2
                and app.query_one(InteractionBar).has_focus
            ),
            pilot,
            description="parallel approval requests",
        )

        assert app._projection is not None
        assert [
            interaction.tool_call_id for interaction in app._projection.pending_interactions
        ] == ["call_write_1", "call_write_2"]

        await pilot.press("enter")
        await _wait_until(
            lambda: (
                app._projection is not None
                and app._projection.pending_interaction is not None
                and app._projection.pending_interaction.tool_call_id == "call_write_2"
            ),
            pilot,
            description="second approval request",
        )

        assert app._projection.pending_interaction is not None
        assert app._projection.pending_interaction.tool_call_id == "call_write_2"
        assert app.query_one(InteractionBar).has_focus is True
        assert first.exists() is False
        assert second.exists() is False

        await pilot.press("enter")
        await _wait_until(
            lambda: app._projection is not None and app._projection.activity.status == "completed",
            pilot,
            description="parallel approval completion",
        )

        assert first.read_text(encoding="utf-8") == "first"
        assert second.read_text(encoding="utf-8") == "second"
        assert app._projection.pending_interaction is None
        assert app._projection.activity.status == "completed"


@pytest.mark.asyncio
async def test_approval_panel_denies_selected_tool(tmp_path: Path) -> None:
    target = tmp_path / "denied.txt"
    app = OuroTuiApp(
        application=_approval_application(tmp_path, target),
        approval_mode="approval",
    )

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.click("#composer")
        await pilot.press("w", "r", "i", "t", "e", "enter")
        await _wait_until(
            lambda: app._projection is not None and app._projection.pending_interaction is not None,
            pilot,
            description="tool approval request",
        )

        panel = app.query_one(InteractionBar)
        await pilot.press("down", "enter")
        await _wait_until(
            lambda: app._projection is not None and app._projection.pending_interaction is None,
            pilot,
            description="denied approval resolution",
        )

        assert target.exists() is False
        assert app._projection is not None
        assert app._projection.pending_interaction is None
        assert panel.display is False


@pytest.mark.asyncio
async def test_builtin_ask_user_renders_form_and_submits_structured_answer(
    tmp_path: Path,
) -> None:
    application = _question_application(tmp_path)
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 38)) as pilot:
        await pilot.pause()
        await pilot.click("#composer")
        await pilot.press("a", "s", "k", "enter")
        await _wait_until(
            lambda: app.query_one(AskUserPanel).display,
            pilot,
            description="question form",
        )

        panel = app.query_one(AskUserPanel)
        assert panel.display is True
        assert panel.has_focus is True
        rendered_question = panel.render().plain
        assert rendered_question.splitlines()[0] == "FILE  1/1"
        assert "Which file should OURO use?" in rendered_question
        assert "↑/↓ move • Enter choose" in rendered_question
        assert "←/→" not in rendered_question

        await pilot.press("left", "right")
        await pilot.pause()
        assert panel.render().plain == rendered_question

        await pilot.press("2")
        await _wait_until(
            lambda: (
                app._projection is not None
                and app._projection.pending_interaction is None
                and app._projection.assistant_text == "Answer received."
            ),
            pilot,
            description="structured question answer",
        )
        await _wait_for_worker_group(app, pilot, group="interaction")

        assert app._projection is not None
        assert app._projection.pending_interaction is None
        assert app._projection.assistant_text == "Answer received."
        assert panel.display is False
        events = application.runtime.sessions.event_page(
            app._projection.session_id,
            cursor=0,
            limit=200,
        ).events
        resolved = next(event for event in events if event.event_type == "ask_user_resolved")
        assert resolved.payload["answer"] == {"answers": {"file": ["README.md"]}}


@pytest.mark.asyncio
async def test_ask_user_other_accepts_freeform_even_when_answer_starts_with_slash(
    tmp_path: Path,
) -> None:
    application = _question_application(tmp_path)
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 38)) as pilot:
        await pilot.pause()
        await pilot.click("#composer")
        await pilot.press("a", "s", "k", "enter")
        await _wait_until(
            lambda: app.query_one(AskUserPanel).display,
            pilot,
            description="question form",
        )

        await pilot.press("down", "down", "enter")
        await pilot.pause()
        panel = app.query_one(AskUserPanel)
        assert panel.awaiting_freeform is True
        assert app.query_one(Composer).has_focus is True

        await pilot.press("slash", "t", "m", "p", "enter")
        await _wait_until(
            lambda: app._projection is not None and app._projection.pending_interaction is None,
            pilot,
            description="freeform question answer",
        )
        await _wait_for_worker_group(app, pilot, group="interaction")

        assert app._projection is not None
        assert app._projection.pending_interaction is None
        events = application.runtime.sessions.event_page(
            app._projection.session_id,
            cursor=0,
            limit=200,
        ).events
        resolved = next(event for event in events if event.event_type == "ask_user_resolved")
        assert resolved.payload["answer"] == {"answers": {"file": ["/tmp"]}}


@pytest.mark.asyncio
async def test_plugin_question_uses_common_form_and_returns_scalar_answer(
    tmp_path: Path,
) -> None:
    application = _question_application(tmp_path, tool_name="PluginQuestion")
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 38)) as pilot:
        await pilot.pause()
        await pilot.click("#composer")
        await pilot.press("a", "s", "k", "enter")
        await _wait_until(
            lambda: app.query_one(AskUserPanel).display,
            pilot,
            description="plugin question form",
        )

        panel = app.query_one(AskUserPanel)
        assert panel.display is True
        assert "How should the plugin continue?" in str(panel.render())

        await pilot.press("1")
        await _wait_until(
            lambda: app._projection is not None and app._projection.pending_interaction is None,
            pilot,
            description="plugin question answer",
        )
        await _wait_for_worker_group(app, pilot, group="interaction")

        assert app._projection is not None
        assert app._projection.pending_interaction is None
        events = application.runtime.sessions.event_page(
            app._projection.session_id,
            cursor=0,
            limit=200,
        ).events
        resolved = next(event for event in events if event.event_type == "ask_user_resolved")
        assert resolved.payload["answer"] == "Safely"


@pytest.mark.asyncio
async def test_plan_mode_write_approval_keeps_focus_on_approval_panel(tmp_path: Path) -> None:
    application = _plan_confirmation_application(tmp_path)
    await application.start()
    assert application.plugins is not None
    assert (await application.plugins.enable("dev.ouro.plan")).status == "active"
    app = OuroTuiApp(application=application, approval_mode="approval")

    async with app.run_test(size=(100, 42)) as pilot:
        await pilot.pause()
        await pilot.press("slash", *"plan", "space", "o", "n", "enter")
        await pilot.pause()
        await pilot.click("#composer")
        await pilot.press("p", "l", "a", "n", "enter")

        panel = app.query_one(InteractionBar)
        await _wait_until(
            lambda: panel.display and panel.has_focus,
            pilot,
            description="plan file write approval",
        )
        composer = app.query_one(Composer)
        assert composer.disabled is True
        assert panel.has_focus is True

        await pilot.click("#composer")
        await pilot.pause()
        assert composer.has_focus is False
        assert panel.has_focus is True

        await pilot.press("tab")
        await pilot.pause()

        assert composer.has_focus is False
        assert panel.has_focus is True


@pytest.mark.asyncio
async def test_plan_confirmation_arrows_page_document_without_moving_options(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    request = PlanConfirmationRequest(
        plan_path=str(tmp_path / "plan.md"),
        plan="# Long plan\n\n" + "\n".join(f"{index}. Verify step {index}." for index in range(30)),
        plan_digest="digest",
    )

    async with app.run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        panel = app.query_one(PlanConfirmationPanel)
        panel.show_request(request)
        await pilot.pause()

        document = panel.query_one("#plan-confirmation-document")
        assert document.max_scroll_y > 0
        assert panel.selected_action == "accept"

        await pilot.press("right")
        await pilot.pause()
        assert document.scroll_y > 0
        assert panel.selected_action == "accept"

        await pilot.press("left")
        await pilot.pause()
        assert document.scroll_y == 0
        assert panel.selected_action == "accept"

        await pilot.press("pagedown")
        await pilot.pause()
        assert document.scroll_y > 0
        await pilot.press("pageup")
        await pilot.pause()
        assert document.scroll_y == 0

        await pilot.press("down")
        assert panel.selected_action == "revise"


@pytest.mark.asyncio
async def test_plan_confirmation_renders_only_plan_and_collects_revision_feedback(
    tmp_path: Path,
) -> None:
    application = _plan_confirmation_application(tmp_path)
    await application.start()
    assert application.plugins is not None
    assert (await application.plugins.enable("dev.ouro.plan")).status == "active"
    app = OuroTuiApp(application=application, approval_mode="auto")

    async with app.run_test(size=(100, 42)) as pilot:
        await pilot.pause()
        await pilot.press("slash", *"plan", "space", "o", "n", "enter")
        await pilot.pause()
        await pilot.click("#composer")
        await pilot.press("p", "l", "a", "n", "enter")

        panel = app.query_one(PlanConfirmationPanel)
        await _wait_until(
            lambda: panel.display and panel.has_focus,
            pilot,
            description="plan confirmation",
        )
        ask_user = app.query_one(AskUserPanel)
        plan_body = panel.query_one("#plan-confirmation-body", Static).render().plain
        controls = panel.query_one("#plan-confirmation-controls", Static).render().plain

        assert panel.display is True
        assert panel.has_focus is True
        assert ask_user.display is False
        assert plan_body == "# Release plan\n\n1. Inspect.\n2. Ship safely.\n3. Verify."
        assert "C:/private/session/plan.md" not in plan_body
        assert "plan_digest" not in plan_body
        assert '"actions"' not in plan_body
        assert "Accept" in controls
        assert "Revise" in controls
        assert "Reject" in controls
        assert "←/→ or PgUp/PgDn plan • ↑/↓ move • Enter choose" in controls
        assert "1. Accept" not in controls
        assert "2. Revise" not in controls
        assert "3. Reject" not in controls
        assert "direct" not in controls
        assert panel.query_one("#plan-confirmation-title", Static).render().plain == "PLAN APPROVAL"
        assert panel.selected_action == "accept"

        await pilot.press("down", "enter")
        await pilot.pause()

        composer = app.query_one(Composer)
        assert panel.awaiting_feedback is True
        assert composer.has_focus is True
        assert composer.placeholder == Composer.PLAN_FEEDBACK_PLACEHOLDER

        await pilot.press(*"Add rollback checks.", "enter")
        await _wait_until(
            lambda: app._projection is not None and app._projection.pending_interaction is None,
            pilot,
            description="plan revision answer",
        )
        await _wait_for_worker_group(app, pilot, group="interaction")

        assert app._projection is not None
        assert app._projection.pending_interaction is None
        assert panel.display is False
        events = application.runtime.sessions.event_page(
            app._projection.session_id,
            cursor=0,
            limit=200,
        ).events
        requested = next(event for event in events if event.event_type == "ask_user_requested")
        resolved = next(event for event in events if event.event_type == "ask_user_resolved")
        assert requested.payload["canonical_tool_id"] == "dev.ouro.plan/tools/exit"
        assert resolved.payload["answer"] == {
            "action": "revise",
            "feedback": "Add rollback checks.",
        }


@pytest.mark.asyncio
async def test_plan_confirmation_accepts_a_keyboard_selected_implementation_option(
    tmp_path: Path,
) -> None:
    application = _plan_confirmation_application(
        tmp_path,
        options=[
            {"label": "Safe rollout", "description": "Keep rollback available."},
            {"label": "Direct rollout", "description": "Complete the change at once."},
        ],
    )
    await application.start()
    assert application.plugins is not None
    assert (await application.plugins.enable("dev.ouro.plan")).status == "active"
    app = OuroTuiApp(application=application, approval_mode="auto")

    async with app.run_test(size=(100, 42)) as pilot:
        await pilot.pause()
        await pilot.press("slash", *"plan", "space", "o", "n", "enter")
        await pilot.pause()
        await pilot.click("#composer")
        await pilot.press("p", "l", "a", "n", "enter")

        panel = app.query_one(PlanConfirmationPanel)
        await _wait_until(
            lambda: panel.display,
            pilot,
            description="plan confirmation",
        )
        await pilot.press("enter")
        await pilot.pause()

        controls = panel.query_one("#plan-confirmation-controls", Static).render().plain
        assert "Choose an implementation approach" in controls
        assert "Safe rollout" in controls
        assert "Direct rollout" in controls

        await pilot.press("2")
        await pilot.pause()
        assert app._projection is not None
        assert app._projection.pending_interaction is not None

        await pilot.press("down", "enter")
        await _wait_until(
            lambda: app._projection is not None and app._projection.pending_interaction is None,
            pilot,
            description="selected plan implementation",
        )
        await _wait_for_worker_group(app, pilot, group="interaction")

        assert app._projection is not None
        await _wait_until(
            lambda: "Plan" not in app.query_one(ActivityIndicator).render().plain,
            pilot,
            description="released plan-mode status",
        )
        events = application.runtime.sessions.event_page(
            app._projection.session_id,
            cursor=0,
            limit=200,
        ).events
        resolved = next(event for event in events if event.event_type == "ask_user_resolved")
        assert resolved.payload["answer"] == {
            "action": "accept",
            "option": "Direct rollout",
        }


@pytest.mark.asyncio
async def test_noop_abort_does_not_notify_when_ready_status_is_visible(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 32), notifications=True) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+c")
        await pilot.pause()

        assert "Ready" in app.query_one(ActivityIndicator).render().plain
        assert not list(app.screen.query_one("#textual-toastrack").query("Toast"))


@pytest.mark.asyncio
async def test_preference_writes_do_not_block_the_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        entered = threading.Event()
        release = threading.Event()
        writer_threads: list[int] = []
        save_preferences = app._preferences_store.save

        def blocking_save(preferences: TuiPreferences) -> None:
            writer_threads.append(threading.get_ident())
            entered.set()
            release.wait()
            save_preferences(preferences)

        monkeypatch.setattr(app._preferences_store, "save", blocking_save)
        watchdog = threading.Timer(1, release.set)
        watchdog.start()
        event_loop_thread = threading.get_ident()
        saving = asyncio.create_task(app._set_tui_theme("harbor"))

        try:
            writer_entered = await asyncio.to_thread(entered.wait, 2)
            event_loop_remained_responsive = not saving.done()
        finally:
            release.set()
            watchdog.cancel()
        saved = await saving

        assert writer_entered is True
        assert event_loop_remained_responsive is True
        assert writer_threads != [event_loop_thread]
        assert saved is True
        assert app.tui_theme == TUI_THEMES["harbor"]


@pytest.mark.asyncio
async def test_action_menu_switches_theme_across_css_and_rich_and_persists(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with app.run_test(size=(100, 32), notifications=True) as pilot:
        await pilot.pause()
        status_line = app.query_one(StatusLine)
        status_line.set_context(
            SessionContextStatus(
                context_id="ctx_theme",
                session_id="session_theme",
                run_id="run_theme",
                turn_id="turn_theme",
                model_round=1,
                provider_id="deepseek",
                model_id="deepseek-chat",
                prepared_at=datetime.now(UTC),
                metrics=ContextMetrics(
                    estimated_input_tokens=24_000,
                    estimated_fixed_tokens=1_000,
                    context_window_tokens=128_000,
                    reserved_output_tokens=8_000,
                    input_budget_tokens=120_000,
                    estimated_remaining_tokens=96_000,
                    estimated_utilization_ratio=0.2,
                ),
            )
        )
        await pilot.press("ctrl+p")
        await pilot.pause()

        actions = app.screen.query_one(OptionList)
        assert "Theme" in str(actions.get_option_at_index(1).prompt)
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, ChoiceScreen)
        theme_screen = app.screen
        assert app.screen.query_one("#choice-title", Static).render().plain == "THEME"
        theme_options = app.screen.query_one(OptionList)
        assert theme_options.option_count == len(TUI_THEMES)
        assert all(choice.description == "" for choice in app.screen._choices)
        assert "original" in {choice.value for choice in app.screen._choices}
        assert TUI_THEMES["original"].accent == "#e8e8e8"
        assert TUI_THEMES["original"].accent_bright == "#ffffff"

        await pilot.press("down")
        await pilot.pause()

        harbor = TUI_THEMES["harbor"]
        assert app.tui_theme == TUI_THEMES["original"]
        preview_preferences = TuiPreferencesStore(tmp_path).load()
        assert preview_preferences.theme == "original"
        assert len(preview_preferences.recent_actions) == 1
        preview_title = theme_screen.query_one("#choice-title", Static)
        assert preview_title.styles.color == Color.parse(harbor.accent)
        assert preview_title.styles.border_left[1] == Color.parse(harbor.accent)
        assert theme_screen.query_one("#choice-filter", Input).styles.border_left[1] == (
            Color.parse(harbor.accent)
        )
        preview_highlight = theme_options.get_component_rich_style(
            "option-list--option-highlighted"
        )
        assert preview_highlight.color is not None
        assert Color.from_rich_color(preview_highlight.color) == Color.parse(harbor.accent)
        preview = app.query_one(BrandHeader).render()
        assert preview.get_style_at_offset(preview.plain.index("OURO")).foreground == Color.parse(
            TUI_THEMES["original"].accent
        )
        assert app.query_one(Composer).styles.border_left[1] == Color.parse(
            TUI_THEMES["original"].accent
        )

        await pilot.press("down", "enter")
        await _wait_until(
            lambda: not app._theme_switch_active,
            pilot,
            description="theme switch",
        )

        sage = TUI_THEMES["sage"]
        assert app.screen is theme_screen
        assert isinstance(app.screen, ChoiceScreen)
        assert (
            next(choice for choice in app.screen._choices if choice.value == "sage").marker
            == DEFAULT_TUI_THEME.selected_choice_marker
        )
        assert not any(choice.loading for choice in app.screen._choices)
        assert app.tui_theme == sage
        header = app.query_one(BrandHeader).render()
        assert header.get_style_at_offset(header.plain.index("OURO")).foreground == Color.parse(
            sage.accent
        )
        assert app.query_one(Composer).styles.border_left[1] == Color.parse(sage.accent)
        context = status_line.query_one("#context-detail", Static).render()
        assert context.get_style_at_offset(context.plain.index("█")).foreground == Color.parse(
            sage.accent
        )

        await pilot.press("down")
        await pilot.pause()
        assert app.tui_theme == sage
        assert TuiPreferencesStore(tmp_path).load().theme == "sage"

        await pilot.press("escape")
        await pilot.pause()
        assert app.tui_theme == sage
        assert app.screen.query_one("#choice-title", Static).render().plain == "ACTIONS"
        theme_action = next(
            choice for choice in app.screen._choices if choice.label == "Theme"
        )
        assert theme_action.accent == sage.accent
        theme_prompt = app.screen.query_one(OptionList).get_option(theme_action.value).prompt
        assert theme_prompt.spans[0].style == sage.accent
        assert not list(app.screen.query_one("#textual-toastrack").query("Toast"))

    stored = (tmp_path / "config" / "tui.json").read_text(encoding="utf-8")
    assert '"theme": "sage"' in stored

    restarted = OuroTuiApp(home=tmp_path, application=_application(tmp_path))
    assert restarted.tui_theme == TUI_THEMES["sage"]


@pytest.mark.asyncio
async def test_theme_switch_stays_interactive_coalesces_and_outlives_picker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await pilot.pause()
        app.screen.query_one("#choice-filter", Input).value = "Theme"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        theme_screen = app.screen
        assert isinstance(theme_screen, ChoiceScreen)
        entered = threading.Event()
        release = threading.Event()
        saved_themes: list[str] = []
        save_preferences = app._preferences_store.save

        def blocking_save(preferences: TuiPreferences) -> None:
            saved_themes.append(preferences.theme)
            if len(saved_themes) == 1:
                entered.set()
                release.wait()
            save_preferences(preferences)

        monkeypatch.setattr(app._preferences_store, "save", blocking_save)
        watchdog = threading.Timer(2, release.set)
        watchdog.start()
        try:
            await pilot.press("down", "enter")
            assert await asyncio.to_thread(entered.wait, 1)
            choices = {choice.value: choice for choice in theme_screen._choices}
            assert choices["harbor"].loading is True
            assert choices["sage"].loading is False
            assert app.tui_theme == TUI_THEMES["original"]

            await pilot.press("down", "enter")
            await pilot.pause()
            choices = {choice.value: choice for choice in theme_screen._choices}
            assert choices["harbor"].loading is False
            assert choices["sage"].loading is True
            assert app.tui_theme == TUI_THEMES["original"]

            reasoning_update = asyncio.create_task(app._set_reasoning_expanded("expand"))
            await pilot.pause()
            assert reasoning_update.done() is False

            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is theme_screen
            assert theme_screen.page_title == "ACTIONS"
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is not theme_screen
            assert app._theme_switch_active is True
        finally:
            release.set()
            watchdog.cancel()

        await _wait_until(
            lambda: not app._theme_switch_active,
            pilot,
            description="coalesced background theme switch",
        )
        assert await reasoning_update is True
        assert saved_themes[0] == "harbor"
        assert saved_themes[-1] == "sage"
        assert app.tui_theme == TUI_THEMES["sage"]
        stored_preferences = TuiPreferencesStore(tmp_path).load()
        assert stored_preferences.theme == "sage"
        assert stored_preferences.reasoning_expanded is True


@pytest.mark.asyncio
async def test_theme_loading_ends_after_history_reaches_the_new_theme(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))
    message = MessageRecord(
        session_id="ses_theme_loading",
        role="assistant",
        content=[TextContent(text="```python\nvalue = 1\n```\n")],
    )

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        await app.query_one(ConversationView).load_messages((message,))
        await pilot.press("ctrl+p")
        await pilot.pause()
        app.screen.query_one("#choice-filter", Input).value = "Theme"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        theme_screen = app.screen
        assert isinstance(theme_screen, ChoiceScreen)
        entered = threading.Event()
        release = threading.Event()
        render_entries = conversation_history._render_entry_requests

        def blocking_render(*args, **kwargs):
            entered.set()
            release.wait()
            return render_entries(*args, **kwargs)

        monkeypatch.setattr(
            conversation_history,
            "_render_entry_requests",
            blocking_render,
        )
        watchdog = threading.Timer(2, release.set)
        watchdog.start()
        try:
            await pilot.press("down", "enter")
            assert await asyncio.to_thread(entered.wait, 1)
            await pilot.pause()
            assert app.tui_theme == TUI_THEMES["harbor"]
            assert app._theme_switch_active is True
            assert next(
                choice for choice in theme_screen._choices if choice.value == "harbor"
            ).loading is True
        finally:
            release.set()
            watchdog.cancel()

        await _wait_until(
            lambda: not app._theme_switch_active,
            pilot,
            description="theme history render",
        )
        assert not any(choice.loading for choice in theme_screen._choices)


@pytest.mark.asyncio
async def test_theme_persistence_failure_keeps_current_theme_and_clears_loading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with app.run_test(size=(100, 32), notifications=True) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await pilot.pause()
        app.screen.query_one("#choice-filter", Input).value = "Theme"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        theme_screen = app.screen
        assert isinstance(theme_screen, ChoiceScreen)

        def fail_save(_preferences: TuiPreferences) -> None:
            raise PersistenceError("theme storage unavailable")

        monkeypatch.setattr(app._preferences_store, "save", fail_save)
        await pilot.press("down", "enter")
        await _wait_until(
            lambda: not app._theme_switch_active,
            pilot,
            description="failed theme switch",
        )
        await pilot.pause()

        assert app.tui_theme == TUI_THEMES["original"]
        assert TuiPreferencesStore(tmp_path).load().theme == "original"
        assert not any(choice.loading for choice in theme_screen._choices)
        assert list(theme_screen.query_one("#textual-toastrack").query("Toast"))


@pytest.mark.asyncio
async def test_host_injected_theme_controls_css_rich_glyphs_and_static_spinner(
    tmp_path: Path,
) -> None:
    theme = replace(
        DEFAULT_TUI_THEME,
        theme_id="graphite",
        label="Graphite",
        description="Host injected test theme",
        accent="#57a9c2",
        panel="#15212a",
        text_primary="#dce5ea",
        brand_mark="◆",
        section_separator_mark="·",
        inline_separator_mark="·",
        selected_choice_marker="■",
        unselected_choice_marker="□",
        spinner_frames=("◐",),
        spinner_interval_seconds=0.2,
    )
    app = OuroTuiApp(
        application=_application(tmp_path),
        theme=theme,
    )

    assert app.tui_theme is theme
    assert app.get_css_variables()["ouro-panel"] == theme.panel
    markdown_code_color = app.console.get_style("markdown.code").color
    assert markdown_code_color is not None
    assert markdown_code_color.triplet.hex == theme.accent

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        header = app.query_one(BrandHeader).render()
        assert header.plain.startswith(f" {theme.brand_mark} OURO ")
        assert header.get_style_at_offset(header.plain.index("OURO")).foreground == (
            Color.parse(theme.accent)
        )
        composer = app.query_one(Composer)
        assert composer.styles.background == Color.parse(theme.panel)

        indicator = app.query_one(ActivityIndicator)
        indicator.set_activity("waiting_model")
        await pilot.pause()
        assert indicator.render().plain.strip().startswith(theme.spinner_frames[0])
        assert indicator._timer is None


@pytest.mark.parametrize(
    ("placeholder_setter", "highlight"),
    (
        ("set_active_run_placeholder", "Ctrl+Enter"),
        ("set_subagent_placeholder", "Alt+↑"),
    ),
)
@pytest.mark.asyncio
async def test_theme_switch_refreshes_styled_composer_placeholder(
    tmp_path: Path,
    placeholder_setter: str,
    highlight: str,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        getattr(composer, placeholder_setter)()
        placeholder = composer.placeholder
        assert isinstance(placeholder, Content)
        offset = placeholder.plain.index(highlight)
        assert placeholder.get_style_at_offset(offset).foreground == Color.parse(
            TUI_THEMES["original"].accent_bright
        )

        app._apply_tui_theme(TUI_THEMES["harbor"])

        themed_placeholder = composer.placeholder
        assert isinstance(themed_placeholder, Content)
        assert themed_placeholder.get_style_at_offset(offset).foreground == Color.parse(
            TUI_THEMES["harbor"].accent_bright
        )
        rendered = composer.render_line(0)
        rendered_offset = rendered.text.index(highlight)
        segment = next(iter(rendered.crop(rendered_offset, rendered_offset + len(highlight))))
        assert segment.style is not None and segment.style.color is not None
        assert segment.style.color.get_truecolor().hex == TUI_THEMES["harbor"].accent_bright


@pytest.mark.asyncio
async def test_action_menu_updates_thought_display_without_closing_picker(
    tmp_path: Path,
) -> None:
    preferences = TuiPreferencesStore(tmp_path)
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with app.run_test(size=(100, 32), notifications=True) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await pilot.pause()
        app.screen.query_one("#choice-filter", Input).value = "Model thought display"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        thought_screen = app.screen
        assert isinstance(thought_screen, ChoiceScreen)
        assert thought_screen.query_one("#choice-title", Static).render().plain == (
            "MODEL THOUGHT DISPLAY"
        )
        choices = {choice.value: choice for choice in thought_screen._choices}
        assert choices["collapse"].marker == DEFAULT_TUI_THEME.selected_choice_marker
        assert choices["expand"].marker == ""

        thought_screen.query_one("#choice-filter", Input).value = "Expand"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen is thought_screen
        choices = {choice.value: choice for choice in thought_screen._choices}
        assert choices["expand"].marker == DEFAULT_TUI_THEME.selected_choice_marker
        assert choices["collapse"].marker == ""
        assert preferences.load().reasoning_expanded is True
        assert not list(app.screen.query_one("#textual-toastrack").query("Toast"))

        await pilot.press("escape")


@pytest.mark.asyncio
async def test_action_menu_toggles_input_draft_memory_and_clears_disabled_state(
    tmp_path: Path,
) -> None:
    preferences = TuiPreferencesStore(tmp_path)
    drafts = TuiInputDraftStore(tmp_path)
    drafts.save(TuiInputDrafts().remember(None, "remember this draft"))
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        assert composer.text == "remember this draft"

        await pilot.press("ctrl+p")
        await pilot.pause()
        app.screen.query_one("#choice-filter", Input).value = "Input draft memory"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        memory_screen = app.screen
        assert isinstance(memory_screen, ChoiceScreen)
        assert memory_screen.query_one("#choice-title", Static).render().plain == (
            "INPUT DRAFT MEMORY"
        )
        choices = {choice.value: choice for choice in memory_screen._choices}
        assert choices["enable"].marker == DEFAULT_TUI_THEME.selected_choice_marker
        assert choices["disable"].marker == ""

        memory_screen.query_one("#choice-filter", Input).value = "Do not remember"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_until(
            lambda: drafts.load() == TuiInputDrafts(),
            pilot,
            description="disabled draft memory to clear durable drafts",
        )
        assert app.screen is memory_screen
        assert preferences.load().input_draft_memory_enabled is False
        assert composer.text == "remember this draft"

        memory_screen.query_one("#choice-filter", Input).value = "Remember input"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_until(
            lambda: drafts.load().draft_for(None) == "remember this draft",
            pilot,
            description="re-enabled draft memory to remember the visible draft",
        )
        choices = {choice.value: choice for choice in memory_screen._choices}
        assert choices["enable"].marker == DEFAULT_TUI_THEME.selected_choice_marker
        assert choices["disable"].marker == ""
        assert preferences.load().input_draft_memory_enabled is True


@pytest.mark.asyncio
async def test_theme_preview_is_local_and_cleared_when_picker_is_cancelled(
    tmp_path: Path,
) -> None:
    preferences = TuiPreferencesStore(tmp_path)
    preferences.save(TuiPreferences(theme="iris"))
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p", "enter")
        await pilot.pause()
        theme_screen = app.screen
        assert isinstance(theme_screen, ChoiceScreen)
        assert theme_screen.query_one(OptionList).highlighted == 3

        await pilot.press("down")
        await pilot.pause()

        assert app.tui_theme == TUI_THEMES["iris"]
        assert theme_screen.query_one("#choice-title", Static).styles.color == Color.parse(
            TUI_THEMES["rosewood"].accent
        )
        assert app.query_one(Composer).styles.border_left[1] == Color.parse(
            TUI_THEMES["iris"].accent
        )

        await pilot.press("escape")
        await pilot.pause()

        assert app.tui_theme == TUI_THEMES["iris"]
        assert theme_screen.query_one("#choice-title", Static).styles.color == Color.parse(
            TUI_THEMES["iris"].accent
        )
        assert app.query_one(Composer).styles.border_left[1] == Color.parse(
            TUI_THEMES["iris"].accent
        )
        theme_action = next(
            choice for choice in app.screen._choices if choice.label == "Theme"
        )
        assert theme_action.accent == TUI_THEMES["iris"].accent
        assert preferences.load() == TuiPreferences(
            theme="iris",
            recent_actions=("__ouro_theme__",),
            recent_workspaces=(str(tmp_path.resolve()),),
        )
