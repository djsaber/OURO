from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from rich.console import Console
from rich.text import Text
from textual import events
from textual.color import Color
from textual.containers import Vertical, VerticalScroll
from textual.geometry import Region
from textual.widgets import Input, OptionList, Static, TextArea
from textual.widgets.markdown import MarkdownFence

from ouro import __version__
from ouro.application.configuration import ConfigurationFacade
from ouro.application.configuration_models import SkillView
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
from ouro.application.supervisor_models import RuntimeToolCallActivity
from ouro.core.adapter import RuntimeAdapter
from ouro.core.compaction import CompactionRecord, compaction_source_digest
from ouro.core.context import ContextMetrics
from ouro.core.events import EventRecord
from ouro.core.messages import (
    MessageRecord,
    ModelMessage,
    ModelResponseRecord,
    TextContent,
    ToolResultReference,
)
from ouro.core.provider import (
    ModelMetadata,
    ProviderCheckResult,
    ProviderStreamEvent,
)
from ouro.core.reasoning import ReasoningCapability, ReasoningRequest
from ouro.core.state import RunRecord
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
from ouro.tui.app import OuroTuiApp, _session_choices
from ouro.tui.commands import TUI_COMMANDS, TuiCommand
from ouro.tui.configuration_screens import (
    CompactionConfigurationScreen,
    MCPConfigurationScreen,
    PluginInstallScreen,
    SkillInstallScreen,
    ToolExecutionConfigurationScreen,
)
from ouro.tui.configuration_workflows import _mcp_management_choices, _skill_choice
from ouro.tui.controller import (
    McpServerStatus,
    TuiBootstrap,
    TuiController,
    TuiEndpointConfiguration,
    TuiPluginCommandExecution,
    TuiPluginInstallConfiguration,
    TuiProviderConfiguration,
    TuiSystemStatus,
)
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
from ouro.tui.state import PendingInteraction, TuiSessionProjection, project_run_presentation
from ouro.tui.theme import SELECTED_CHOICE_MARKER, TUI_THEMES
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


def test_choice_screen_aligns_trailing_metadata_to_the_right() -> None:
    option = ChoiceScreen._options((Choice("msg", "A user request " * 8, trailing="18:00"),))[0]
    console = Console(width=40, record=True, force_terminal=False, color_system=None)

    console.print(option.prompt)

    rendered = console.export_text().splitlines()[0]
    assert rendered.startswith("A user request")
    assert rendered.endswith("  18:00")
    assert len(rendered) == 40
    time_start = rendered.rindex("18:00")
    rendered_message = rendered[:time_start].rstrip()
    assert len(rendered_message) <= 40 * 2 // 3


def test_choice_screen_renders_marker_with_theme_accent() -> None:
    option = ChoiceScreen._options(
        (Choice("selected", "Selected", marker=SELECTED_CHOICE_MARKER),),
        group_accent="#123456",
    )[0]

    assert option.prompt.plain == f"{SELECTED_CHOICE_MARKER} Selected"
    assert option.prompt.spans[0].style == "bold #123456"


def test_regular_choice_metadata_keeps_rows_single_line() -> None:
    options = ChoiceScreen._options(
        (
            Choice("auto", "auto", trailing="current"),
            Choice("approval", "approval", trailing="requires confirmation"),
        )
    )

    console = Console(width=60, record=True, force_terminal=False, color_system=None)
    for option in options:
        console.print(option.prompt)

    assert len(console.export_text().splitlines()) == len(options)


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
        await pilot.pause(0.5)
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
        await pilot.pause(0.5)
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
        await pilot.pause(0.5)
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
@pytest.mark.parametrize(
    "navigation",
    (("down",) * 20, ("right",)),
    ids=("cross-group-with-down", "jump-group-with-right"),
)
async def test_grouped_choice_navigation_keeps_new_highlight_visible_immediately(
    tmp_path: Path,
    navigation: tuple[str, ...],
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(80, 16)) as pilot:
        await pilot.pause(0.5)
        app.push_screen(
            ChoiceScreen(
                title="SESSIONS",
                placeholder="Filter sessions…",
                choices=(
                    *tuple(
                        Choice(f"current-{index}", f"Current {index}", group="Current")
                        for index in range(20)
                    ),
                    Choice("target", "Target session", group="Next group"),
                ),
            )
        )
        await pilot.pause()

        options = app.screen.query_one(OptionList)
        await pilot.press(*navigation)
        await pilot.pause()

        visible_text = "\n".join(
            options.render_line(line).text
            for line in range(options.scrollable_content_region.height)
        )
        assert options.get_option_at_index(options.highlighted or 0).id == "target"
        assert "Next group" in visible_text
        assert "Target session" in visible_text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("navigation", "target", "group"),
    (
        (("up",) * 19, "later-0", "Later"),
        (("left",), "current-0", "Current"),
    ),
    ids=("move-up-within-group", "jump-to-previous-group"),
)
async def test_upward_grouped_navigation_keeps_header_and_highlight_visible(
    tmp_path: Path,
    navigation: tuple[str, ...],
    target: str,
    group: str,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(80, 16)) as pilot:
        await pilot.pause(0.5)
        app.push_screen(
            ChoiceScreen(
                title="SESSIONS",
                placeholder="Filter sessions…",
                choices=(
                    *tuple(
                        Choice(f"current-{index}", f"Current {index}", group="Current")
                        for index in range(20)
                    ),
                    *tuple(
                        Choice(f"later-{index}", f"Later {index}", group="Later")
                        for index in range(20)
                    ),
                ),
                initial_value="later-19",
            )
        )
        await pilot.pause()

        options = app.screen.query_one(OptionList)
        await pilot.press(*navigation)
        await pilot.pause()

        visible_text = "\n".join(
            options.render_line(line).text
            for line in range(options.scrollable_content_region.height)
        )
        assert options.get_option_at_index(options.highlighted or 0).id == target
        assert group in visible_text
        assert target.replace("-", " ").title() in visible_text


@pytest.mark.asyncio
async def test_group_header_remains_visible_after_wrapping_to_first_choice(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(80, 16)) as pilot:
        await pilot.pause(0.5)
        app.push_screen(
            ChoiceScreen(
                title="SESSIONS",
                placeholder="Filter sessions…",
                choices=tuple(
                    Choice(f"session-{index}", f"Session {index}", group="Today")
                    for index in range(20)
                ),
            )
        )
        await pilot.pause()

        options = app.screen.query_one(OptionList)
        assert options.highlighted == 1
        await pilot.press("up")
        await pilot.pause()
        assert options.highlighted == 20
        assert options.scroll_y > 0

        await pilot.press("down")
        await pilot.pause()
        assert options.highlighted == 1
        assert options.scroll_y == 0


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
                if message.role == "system_synthetic" and "Plan file:" in message.text_content
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
        BlockingStreamProvider | BlockingReasoningProvider | ReasoningBurstThenBlockProvider
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
async def test_tui_uses_compact_open_sided_chrome(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        welcome = await _wait_for_welcome_render(app, pilot, contains="██████")
        conversation = app.query_one(ConversationView)
        transcript = conversation.query_one("#conversation-transcript")
        header = app.query_one(BrandHeader)

        # Startup enters the empty state: the centered welcome frame projects
        # the workspace status while the conversation surface remains hidden.
        assert not header.display
        assert app.query_one(Composer).display
        assert "██████" in welcome._logo.render().plain
        assert welcome._system._workspace.render().plain == str(tmp_path)
        welcome.set_system_status(
            TuiSystemStatus(
                workspace=str(tmp_path),
                provider_ids=("deepseek", "openai"),
                model_count=3,
                skill_count=7,
                skill_names=("browser", "documents", "github", "imagegen"),
                plugin_active_count=2,
                plugin_total_count=3,
                plugin_issue_count=1,
                plugin_active_names=("Goals", "Guardian"),
                mcp_connected_count=3,
                mcp_total_count=3,
                mcp_tool_count=8,
                usage_request_count=4,
                usage_input_tokens=12_300,
                usage_output_tokens=1_200,
                usage_estimated_cost=0.0421,
                usage_priced_request_count=4,
            )
        )
        await pilot.pause()

        welcome = app.query_one(WelcomePanel)
        status_text = _welcome_plain_text(welcome)
        assert "SYSTEM" in status_text
        assert f"v{__version__}" in status_text
        assert str(tmp_path) in status_text
        assert status_text.index("TIPS") < status_text.index("SYSTEM")
        assert "Tell me what you want to accomplish." not in status_text
        assert "Enter" not in status_text
        assert "Ctrl+Enter" not in status_text
        assert "Shift+Enter" not in status_text
        tip_keys = [widget.render().plain for widget in welcome.query(".welcome-tip-key")]
        tip_descriptions = [
            widget.render().plain for widget in welcome.query(".welcome-tip-description")
        ]
        tips = dict(zip(tip_keys, tip_descriptions, strict=True))
        assert {
            "/": "Commands",
            "Ctrl+P": "Actions",
            "Ctrl+S": "Sessions",
        }.items() <= tips.items()
        tips_title = welcome._tips.query_one(".welcome-title", Static)
        assert tips_title.render().plain == "• TIPS"
        assert tips_title.styles.color == Color.parse(app.tui_theme.accent)
        assert "Health" not in status_text
        assert "deepseek, openai • 3 model(s)" in status_text
        assert "7 available • browser, documents, github, +1" in status_text
        assert "2/3 active • Goals, Guardian • 1 need attention" in status_text
        assert "3/3 connected • 8 tool(s)" in status_text
        assert "4 request(s) • $0.0421" in status_text
        assert "12.3K in / 1.2K out" in status_text
        ctrl_p = next(
            widget.render()
            for widget in welcome.query(".welcome-tip-key")
            if widget.render().plain == "Ctrl+P"
        )
        assert not ctrl_p.get_style_at_offset(0).bold
        workspace = welcome._system._workspace.render()
        assert all("bold" not in str(span.style) for span in workspace._spans)
        assert "MCP" not in header.render().plain
        assert header.styles.border_left[0] == ""
        assert welcome.styles.border_left[0] == ""
        assert app.screen.styles.background.hex == "#0C0C0C"
        assert app.screen.region.x == 0
        assert app.screen.region.y == 0
        assert app.screen.region.width == 100
        assert app.screen.region.height == 32
        assert not header.display
        main = app.query_one("#main")
        assert main.region.x == 0
        assert main.region.right == app.screen.region.right
        assert main.region.bottom == app.screen.region.bottom
        assert conversation.styles.scrollbar_size_vertical == 0
        assert transcript.layout.name == "stream"
        # Hidden scrollbar must not collapse the layout: virtual width should
        # follow the content area, and the welcome panel keeps its CSS width.
        assert conversation.virtual_size.width >= conversation.content_region.width
        composer = app.query_one(Composer)
        status_line = app.query_one(StatusLine)
        assert 64 <= composer.region.width <= 112
        assert composer.region.x == (app.screen.region.width - composer.region.width) // 2
        assert abs((composer.region.y + composer.region.height / 2) - 16) <= 2
        assert status_line.region.x == composer.region.x
        assert status_line.region.right == composer.region.right
        assert status_line.region.y == composer.region.bottom
        assert abs(
            (welcome._logo.region.x + welcome._logo.region.width / 2)
            - app.screen.region.width / 2
        ) <= 0.5
        assert welcome._logo.region.bottom + 2 == composer.region.y
        assert welcome._usage.region.x == composer.region.x
        assert welcome._tips.region.right == composer.region.right
        assert welcome._lower.region.y - status_line.region.bottom == 2
        assert welcome._system.region.x == composer.region.x
        assert welcome._system.region.right == composer.region.right
        assert welcome._system.region.y - welcome._lower.region.bottom == 3
        assert welcome._system._version.region.x == welcome._system._workspace.region.x
        assert conversation.scroll_y >= 0

        # Once dialogue content arrives the welcome frame yields to the
        # conversation — visual layering keeps the LOGO only at startup and
        # when a new session begins, never alongside messages.
        await conversation.add_user("Hello")
        await conversation.append_assistant("```python\nprint('Hi')\n```")
        await pilot.pause()
        code_fence = conversation.query_one(MarkdownFence)
        assert code_fence.styles.background == composer.styles.background
        await conversation.add_notice("Status changed")
        assert not welcome.has_class("welcome-page")
        assert header.display

        popup = app.query_one(CommandPopup)
        assert popup.styles.overflow_y == "hidden"
        assert popup.show_vertical_scrollbar is False
        assert composer.styles.scrollbar_size_vertical == 0
        assert composer.show_vertical_scrollbar is False
        assert (
            composer.get_component_rich_style("text-area--cursor-line").bgcolor
            == composer.rich_style.bgcolor
        )
        assert composer.region.height == 3
        assert composer.content_region.height == 1
        assert app.query_one(QueuedInputPanel).styles.scrollbar_size_vertical == 1
        history = conversation.query_one(ConversationHistory)
        assert history.styles.border_left[0] == ""
        user_lines = history.rendered_text.splitlines()[:4]
        assert [line.index("│") for line in user_lines[1:4]] == [2, 2, 2]
        assert user_lines[2].index("Hello") == 5
        code_segments = [
            segment for line in history.lines for segment in line if "print" in segment.text
        ]
        assert code_segments
        assert all(
            segment.style.bgcolor == Color.parse("#171717").rich_color for segment in code_segments
        )
        assert "Status changed" in history.rendered_text
        assert not conversation.query(AssistantTurn)

        assert composer.styles.border_left[0] == "solid"
        assert composer.styles.border_right[0] == "solid"
        assert composer.styles.border_top[0] == ""
        assert composer.styles.border_bottom[0] == ""

        dynamic_panels = (
            popup,
            app.query_one(InteractionBar),
            app.query_one(AskUserPanel),
            app.query_one(PlanConfirmationPanel),
            app.query_one(QueuedInputPanel),
        )
        for panel in dynamic_panels:
            assert panel.styles.overlay != "screen"
            assert panel.styles.background == composer.styles.background
            assert panel.styles.border_left[0] == "solid"
            assert panel.styles.border_right[0] == "solid"
            assert panel.styles.border_top[0] == ""
            assert panel.styles.border_bottom[0] == ""

        status_line = app.query_one(StatusLine)
        assert status_line.region.y == composer.region.bottom
        assert status_line.region.bottom == app.query_one("#main").region.bottom
        assert status_line.query_one("#context-detail", Static).render().plain == ""
        assert len(status_line.query("#status-detail")) == 0
        ready = status_line.query_one(ActivityIndicator).render()
        assert "Ready • inherit • faux" in ready.plain
        assert ready.get_style_at_offset(ready.plain.index("Ready")).foreground == Color.parse(
            app.tui_theme.accent
        )
        status_line.set_context(
            SessionContextStatus(
                context_id="ctx_1",
                session_id="session_1",
                run_id="run_1",
                turn_id="turn_1",
                model_round=1,
                provider_id="deepseek",
                model_id="deepseek-chat",
                prepared_at=datetime.now(UTC),
                metrics=ContextMetrics(
                    estimated_input_tokens=3_000,
                    estimated_fixed_tokens=100,
                    context_window_tokens=128_000,
                    reserved_output_tokens=8_000,
                    input_budget_tokens=120_000,
                    estimated_remaining_tokens=117_000,
                    estimated_utilization_ratio=0.025,
                ),
            )
        )
        context_detail = status_line.query_one("#context-detail", Static)
        context = context_detail.render()
        assert context.plain == "3.0K █▒▒▒▒▒▒▒▒▒▒ 2%"
        assert context.get_style_at_offset(context.plain.index("█")).foreground == Color.parse(
            app.tui_theme.accent
        )

        for index in range(40):
            await conversation.add_notice(f"Overflow {index}")
        await pilot.pause()
        assert conversation.region.right == app.screen.region.right
        assert conversation.vertical_scrollbar.region.width == 0


@pytest.mark.parametrize(
    ("terminal_width", "expected_composer_width"),
    ((80, 64), (100, 68), (200, 112)),
)
@pytest.mark.asyncio
async def test_welcome_composer_width_clamps_and_stays_centered(
    tmp_path: Path,
    terminal_width: int,
    expected_composer_width: int,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(terminal_width, 40)) as pilot:
        await _wait_for_welcome_render(app, pilot, contains="██████")
        composer = app.query_one(Composer)

        assert composer.region.width == expected_composer_width
        assert composer.region.x == (terminal_width - expected_composer_width) // 2


@pytest.mark.asyncio
async def test_welcome_refreshes_live_system_status_each_time_it_opens(tmp_path: Path) -> None:
    application = _application(tmp_path)
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause(0.5)
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


def test_plugin_skill_choice_is_visible_but_not_selectable() -> None:
    choice = _skill_choice(
        SkillView(
            name="calendar",
            description="Manage calendars.",
            location="C:/plugins/calendar/SKILL.md",
            source="plugin",
            enabled=True,
            extensions={"ouro": {"plugin_id": "dev.ouro.calendar"}},
        )
    )

    assert choice.disabled is True
    assert choice.trailing == "plugin · enabled"


@pytest.mark.asyncio
async def test_tui_context_status_matches_latest_prepared_context_size(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause(0.5)
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
        assert context.plain == "48.0K █████░░░░░░ 40%"
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
        assert boundary.plain == "96.0K █████████░░ 80%"
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
        assert warning.plain == "97.2K █████████░░ 81%"
        assert warning.get_style_at_offset(warning.plain.index("81%")).foreground == Color.parse(
            "#968264"
        )

        status_line.set_context(None)
        assert context_detail.render().plain == ""


@pytest.mark.asyncio
async def test_completed_session_restores_context_status_without_event_replay(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    await application.start()
    result = await application.runtime.run("remember this context")
    expected = application.runtime.sessions.context_status(result.session.session_id)
    assert expected is not None
    app = OuroTuiApp(
        application=application,
        resume_session_id=result.session.session_id,
    )

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause(0.7)

        assert app._projection is not None
        assert app._projection.activity.context_status == expected
        context = app.query_one("#context-detail", Static).render().plain
        assert context


@pytest.mark.asyncio
async def test_status_line_projects_approval_presets_with_muted_risk_color(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 32), notifications=True) as pilot:
        await pilot.pause(0.5)
        indicator = app.query_one(StatusLine).query_one(ActivityIndicator)

        await app._set_approval_mode("auto")
        automatic = indicator.render()
        assert "Ready • auto • faux" in automatic.plain
        assert automatic.get_style_at_offset(automatic.plain.index("auto")).foreground == (
            Color.parse("#b97878")
        )

        await app._set_approval_mode("afk")
        unattended = indicator.render()
        assert "Ready • afk • faux" in unattended.plain
        assert unattended.get_style_at_offset(unattended.plain.index("afk")).foreground == (
            Color.parse("#b97878")
        )

        await app._set_approval_mode("approval")
        protected = indicator.render()
        assert "Ready • approval • faux" in protected.plain
        assert protected.get_style_at_offset(protected.plain.index("approval")).foreground == (
            Color.parse("#647180")
        )

        await app._new_session()
        assert "Ready • inherit • faux" in indicator.render().plain
        assert not list(app.screen.query_one("#textual-toastrack").query("Toast"))


@pytest.mark.asyncio
async def test_status_line_shows_only_the_model_endpoint_name(tmp_path: Path) -> None:
    application = _endpoint_named_application(tmp_path)
    session = application.runtime.create_session(title="Endpoint status")
    app = OuroTuiApp(
        application=application,
        resume_session_id=session.session_id,
    )

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause(0.5)
        indicator = app.query_one(StatusLine).query_one(ActivityIndicator)

        assert "Ready • inherit • chat-endpoint" in indicator.render().plain
        assert "supplier/" not in indicator.render().plain
        assert "provider-model-id" not in indicator.render().plain
        assert "Friendly model" not in indicator.render().plain

        await app._select_model("supplier/chat-endpoint")

        assert "Ready • inherit • chat-endpoint" in indicator.render().plain


@pytest.mark.asyncio
async def test_tui_changes_model_and_approval_while_the_session_is_running(tmp_path: Path) -> None:
    provider = BlockingStreamProvider()
    application = _blocking_application(tmp_path, provider)
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause(0.5)
        await pilot.click("#composer")
        await pilot.press(*"stream", "enter")
        await asyncio.wait_for(provider.first_chunk_sent.wait(), timeout=2)
        await pilot.pause(0.2)

        try:
            await app._select_model("blocking/blocking")
            session_id = app._controller.session_id
            assert session_id is not None
            persisted = application.runtime.store.load_session_record(session_id)
            assert persisted.current_model == "blocking/blocking"

            app.action_approval_settings()
            await pilot.pause(0.2)
            assert isinstance(app.screen, ChoiceScreen)
            await pilot.press("escape")

            await app._set_approval_mode("auto")
            persisted = application.runtime.store.load_session_record(session_id)
            assert persisted.approval_mode == "auto"
            assert "auto" in app.query_one(StatusLine).query_one(ActivityIndicator).render().plain
        finally:
            provider.release.set()
        await pilot.pause(0.5)


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
        starting_status = welcome._system._mcp.render()
        assert "0/1 connected • 0 tool(s) • 1 connecting" in starting_status.plain
        assert starting_status.get_style_at_offset(
            starting_status.plain.index("connecting"),
        ).foreground == Color.parse(app.tui_theme.accent)

        session.initialize_gate.set()
        await pilot.pause(0.6)
        connected_status = welcome._system._mcp.render()
        assert "1/1 connected • 1 tool(s)" in connected_status.plain
        assert connected_status.get_style_at_offset(
            connected_status.plain.index("connected"),
        ).foreground == Color.parse(app.tui_theme.accent)
        assert "live__search" in application.runtime.tools.names()

        session.tools.append(LiveMcpSession._tool("fetch"))
        factory.tools_changed()
        await pilot.pause(0.6)
        assert "1/1 connected • 2 tool(s)" in welcome._system._mcp.render().plain
        assert {"live__fetch", "live__search"}.issubset(application.runtime.tools.names())

        factory.connection_lost("connection lost")
        await pilot.pause(0.6)
        assert "0/1 connected • 0 tool(s) • 1 failed" in welcome._system._mcp.render().plain
        assert "live__search" not in application.runtime.tools.names()

    assert app._mcp_status_timer is None


@pytest.mark.asyncio
async def test_tui_model_request_includes_mcp_tools_connected_after_startup(
    tmp_path: Path,
) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    provider = ToolCapturingProvider()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="tool-capturing", provider_id=provider.provider_id),
        ),
        tools=ToolRegistry(),
        default_workspace=tmp_path,
        environment=RuntimeEnvironment.local(tmp_path, store.sessions_dir),
    )
    application = LocalApplication.from_runtime(runtime)
    session = LiveMcpSession()
    manager = MCPManager(
        registry=runtime.tools,
        client_factory=LiveMcpFactory(session),
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
    controller = TuiController(
        lambda _event: None,
        lambda _status: None,
        application=application,
    )

    await controller.start()
    try:
        session.initialize_gate.set()
        connected = await asyncio.wait_for(manager.start("live"), timeout=1)
        assert connected.status == "connected"

        await controller.submit("inspect tools")

        assert "live__search" in {str(tool["name"]) for tool in provider.tool_batches[-1]}

        await manager.stop("live")
        await controller.submit("inspect tools again")

        assert "live__search" not in {str(tool["name"]) for tool in provider.tool_batches[-1]}
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_mcp_toggle_changes_subsequent_dialogue_runs_in_same_controller(
    tmp_path: Path,
) -> None:
    configuration = ConfigurationFacade(tmp_path)
    configuration.save_mcp_server(
        "live",
        MCPServerConfigUpdate(
            command="unused-in-test",
            auto_start=True,
            auto_reconnect=False,
            timeout_seconds=1,
            health_check_interval_seconds=60,
        ),
    )
    store = LocalSessionStore(tmp_path)
    store.initialize()
    provider = ToolCapturingProvider()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="tool-capturing", provider_id=provider.provider_id),
        ),
        tools=ToolRegistry(),
        default_workspace=tmp_path,
        environment=RuntimeEnvironment.local(tmp_path, store.sessions_dir),
    )
    application = LocalApplication.from_runtime(runtime)
    session = LiveMcpSession()
    manager = MCPManager(
        registry=runtime.tools,
        client_factory=LiveMcpFactory(session),
    )
    configured = configuration.mcp.get("live")
    assert configured is not None
    manager.configure(configured)
    application.mcp = MCPRuntimeResource(manager)
    controller = TuiController(
        lambda _event: None,
        lambda _status: None,
        application=application,
        configuration=configuration,
    )

    session.initialize_gate.set()
    await controller.start()
    try:
        await manager.start("live")
        await controller.submit("first turn")
        assert "live__search" in {str(tool["name"]) for tool in provider.tool_batches[-1]}

        await controller.set_mcp_enabled("live", enabled=False)
        await controller.submit("second turn")
        assert "live__search" not in {str(tool["name"]) for tool in provider.tool_batches[-1]}

        await controller.set_mcp_enabled("live", enabled=True)
        await manager.start("live")
        await controller.submit("third turn")
        assert "live__search" in {str(tool["name"]) for tool in provider.tool_batches[-1]}
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_app_streams_first_run_without_waiting_for_run_result(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause(0.5)
        composer = app.query_one(Composer)
        assert composer.disabled is False

        await pilot.click("#composer")
        await pilot.press("h", "e", "l", "l", "o", "enter")
        await pilot.pause(0.8)

        assert app._projection is not None
        assert app._projection.assistant_text == "**Hello** from OURO"
        assert app._projection.activity.status == "completed"
        assert app._controller.session_id is not None
        context = app.query_one(StatusLine).query_one("#context-detail", Static).render().plain
        assert "Context" not in context
        assert "~" not in context


@pytest.mark.asyncio
async def test_stream_updates_follow_only_while_conversation_is_at_bottom(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause(0.5)
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
        await pilot.pause(0.5)
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
async def test_user_message_frame_spans_every_content_line(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(40, 20)) as pilot:
        await pilot.pause(0.5)
        conversation = app.query_one(ConversationView)
        await conversation.add_user("First line\nSecond line")
        await pilot.pause()

        history = conversation.query_one(ConversationHistory)
        framed_lines = [line.text for line in history.lines if "│" in line.text]
        assert len(framed_lines) == 4
        assert all(line.index("│") == 2 for line in framed_lines)
        assert framed_lines[1].index("First line") == 5
        assert framed_lines[2].index("Second line") == 5


@pytest.mark.asyncio
async def test_runtime_event_burst_is_coalesced_before_markdown_rendering(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause(0.5)
        conversation = app.query_one(ConversationView)
        rendered_batches: list[str] = []
        append_assistant = conversation.append_assistant

        async def record_rendered_batch(text: str) -> None:
            rendered_batches.append(text)
            await append_assistant(text)

        conversation.append_assistant = record_rendered_batch  # type: ignore[method-assign]
        session_id = "ses_render_burst"
        app._projection = TuiSessionProjection(session_id)
        events = (
            EventRecord(
                session_id=session_id,
                run_id="run_render_burst",
                event_type="run_started",
                sequence=1,
                payload={"trigger": "user"},
            ),
            *(
                EventRecord(
                    session_id=session_id,
                    run_id="run_render_burst",
                    event_type="model_text_delta",
                    sequence=sequence,
                    payload={"text": f"chunk-{sequence}\n"},
                )
                for sequence in range(2, 202)
            ),
        )

        for event in events:
            app._publish_runtime_event(event)
        await pilot.pause(0.2)

        expected = "".join(f"chunk-{sequence}\n" for sequence in range(2, 202))
        assert rendered_batches == [expected]
        assert app._projection.assistant_text == expected


@pytest.mark.asyncio
async def test_reasoning_stream_renders_markup_syntax_as_plain_text(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause(0.5)
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
        assert reasoning._content.render().plain == expected
        assert reasoning.label.render().plain.startswith("THINKING · ")
        assert reasoning.label.render().plain.endswith("s")
        assert reasoning.label.region.x == turn.content_region.x
        assert reasoning._content.region.x > reasoning.label.region.x
        assert reasoning._content.region.y == reasoning.label.region.bottom + 1
        assert not turn.query(".assistant-label")


@pytest.mark.asyncio
async def test_compact_command_runs_the_canonical_manual_compaction_pipeline(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 32), notifications=True) as pilot:
        await pilot.pause(0.5)
        await pilot.press("h", "e", "l", "l", "o", "enter")
        await pilot.pause(0.8)
        await pilot.press("slash", *"compact", "enter")
        await pilot.pause(0.8)

        session_id = app._controller.session_id
        assert session_id is not None
        recent_run = application.runtime.sessions.open(session_id).recent_run
        assert recent_run is not None
        assert recent_run.kind == "compaction"
        assert recent_run.status == "completed"
        toast = app.screen.query_one("#textual-toastrack").query_one("Toast")
        assert "No complete older turn is eligible" in toast.render().plain


@pytest.mark.asyncio
async def test_fork_command_selects_boundary_with_keyboard_and_opens_branch(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 34), notifications=True) as pilot:
        await pilot.pause(0.5)
        await pilot.press("f", "o", "r", "k", "enter")
        await pilot.pause(0.8)
        source_session_id = app._controller.session_id
        assert source_session_id is not None

        await pilot.press("slash", *"fork", "enter", "enter")
        await pilot.pause()
        assert isinstance(app.screen, ChoiceScreen)
        await pilot.press("down", "up", "enter")
        await pilot.pause(0.8)

        branch_session_id = app._controller.session_id
        assert branch_session_id is not None
        assert branch_session_id != source_session_id
        branch = application.runtime.store.load_session(branch_session_id)
        assert branch.session.parent_session_id == source_session_id
        assert branch.session.forked_from_message_id is None
        assert branch.messages == []
        assert app.query_one(Composer).text == "fork"
        assert app.query_one(Composer).has_focus is True
        assert not list(app.screen.query_one("#textual-toastrack").query("Toast"))


@pytest.mark.asyncio
async def test_fork_menu_is_chronological_and_restores_inherited_compaction(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    source = application.runtime.sessions.create(title="Compacted fork source").session
    messages = [
        MessageRecord.text(
            session_id=source.session_id,
            role=role,
            text=text,
            sequence=index,
        ).model_copy(update={"created_at": created_at})
        for index, (role, text, created_at) in enumerate(
            (
                ("user", "First request", datetime(2026, 1, 1, 10, 0, tzinfo=UTC)),
                ("assistant", "First answer", datetime(2026, 1, 1, 10, 1, tzinfo=UTC)),
                ("user", "Second request", datetime(2026, 1, 1, 10, 2, tzinfo=UTC)),
                ("assistant", "Second answer", datetime(2026, 1, 1, 10, 3, tzinfo=UTC)),
            ),
            start=1,
        )
    ]
    for message in messages:
        application.runtime.store.append_message(message)
    application.runtime.store.save_session(
        application.runtime.store.load_session_record(source.session_id).model_copy(
            update={
                "message_count": len(messages),
                "last_message_id": messages[-1].message_id,
            }
        )
    )
    metrics = ContextMetrics(
        estimated_input_tokens=120,
        estimated_fixed_tokens=20,
    )
    compaction = CompactionRecord(
        session_id=source.session_id,
        strategy_id="test",
        trigger="manual",
        summary="summary",
        covered_message_ids=[messages[0].message_id],
        history_boundary_message_id=messages[1].message_id,
        source_digest=compaction_source_digest(messages[:1]),
        context_metrics_after=metrics,
    )
    application.runtime.store.append_compaction(compaction)
    application.runtime.store.activate_compaction(source.session_id, compaction.compaction_id)
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause(0.5)
        await app._open_session(source.session_id)
        app._fork_session()
        await _wait_for_screen(app, ChoiceScreen, title="FORK SESSION")

        assert isinstance(app.screen, ChoiceScreen)
        choices = app.screen._choices
        assert [choice.label for choice in choices] == ["First request", "Second request"]
        assert [choice.trailing for choice in choices] == [
            messages[0].created_at.astimezone().strftime("%H:%M"),
            messages[2].created_at.astimezone().strftime("%H:%M"),
        ]
        assert all(choice.description == "" for choice in choices)
        assert all(choice.group == "" for choice in choices)
        assert all("msg_" not in choice.label for choice in choices)

        await pilot.press("down", "enter")
        await pilot.pause(0.5)

        branch_session_id = app._controller.session_id
        assert branch_session_id is not None and branch_session_id != source.session_id
        branch = application.runtime.store.load_session(branch_session_id)
        assert [message.text_content for message in branch.messages] == [
            "First request",
            "First answer",
        ]
        assert branch.session.forked_from_message_id == messages[1].message_id
        assert len(branch.compactions) == 1
        assert branch.session.active_compaction_id == branch.compactions[0].compaction_id
        assert app.query_one(Composer).text == "Second request"
        assert app.query_one(StatusLine)._context_metrics == metrics
        history = app.query_one(ConversationHistory)
        assert [(entry.kind, entry.text) for entry in history.entries] == [
            ("user", "First request"),
            ("assistant", "First answer"),
            ("notice", "Context compacted · 1 message summarized"),
        ]


@pytest.mark.asyncio
async def test_fork_command_refuses_active_tui_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application(tmp_path)
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause(0.5)
        await pilot.press("f", "o", "r", "k", "enter")
        await pilot.pause(0.8)
        source_session_id = app._controller.session_id
        assert source_session_id is not None
        assert app._projection is not None
        app._projection.activity = app._projection.activity.model_copy(update={"status": "running"})

        def unexpected_branch(_message_id: str) -> None:
            raise AssertionError("active TUI session reached the branch service")

        monkeypatch.setattr(app._controller, "branch_session", unexpected_branch)
        app._fork_session("msg_not_reached")
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
        await pilot.pause(0.5)
        await pilot.click("#composer")
        await pilot.press("s", "t", "r", "e", "a", "m", "enter")
        try:
            await asyncio.wait_for(provider.first_chunk_sent.wait(), timeout=2)
            await pilot.pause(0.2)

            assert app._projection is not None
            assert app._projection.assistant_text == "first "
            assert app._projection.activity.status != "completed"
            assert app.query_one(AssistantTurn).markdown.source == "first "
        finally:
            provider.release.set()
        await pilot.pause(0.5)

        assert app._projection is not None
        assert app._projection.assistant_text == "first second"
        assert app._projection.activity.status == "completed"


@pytest.mark.asyncio
async def test_composer_placeholder_tracks_active_run(tmp_path: Path) -> None:
    provider = BlockingStreamProvider()
    app = OuroTuiApp(application=_blocking_application(tmp_path, provider))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause(0.5)
        composer = app.query_one(Composer)
        assert composer.placeholder == "Ask anything..."

        await pilot.click("#composer")
        await pilot.press(*"stream", "enter")
        try:
            await asyncio.wait_for(provider.first_chunk_sent.wait(), timeout=2)
            await pilot.pause(0.2)
            assert composer.placeholder == (
                "Enter queues a follow-up · Ctrl+Enter guides the active run…"
            )
        finally:
            provider.release.set()
        await pilot.pause(0.5)

        assert app._projection is not None
        assert app._projection.activity.status == "completed"
        assert composer.placeholder == "Ask anything..."


@pytest.mark.asyncio
async def test_follow_up_stays_in_queue_until_consumed_at_the_next_run(
    tmp_path: Path,
) -> None:
    provider = BlockingStreamProvider()
    application = _blocking_application(tmp_path, provider)
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.pause(0.5)
        await pilot.click("#composer")
        await pilot.press(*"first", "enter")
        await asyncio.wait_for(provider.first_chunk_sent.wait(), timeout=2)
        await pilot.pause(0.2)

        app.query_one(Composer).load_text("follow next")
        await pilot.press("enter")
        await pilot.pause(0.3)

        queue_panel = app.query_one(QueuedInputPanel)
        conversation = app.query_one(ConversationView)
        assert queue_panel.display is True
        queue_text = queue_panel.query_one("#queued-input-content").render().plain
        assert "QUEUED INPUTS" not in queue_text
        assert "FOLLOW" in queue_text
        assert "follow next" in queue_text
        composer = app.query_one(Composer)
        status_line = app.query_one(StatusLine)
        assert queue_panel.region.y >= composer.region.bottom
        assert status_line.region.y == queue_panel.region.bottom
        assert status_line.region.bottom == app.query_one("#main").region.bottom
        assert queue_panel.region.x == composer.region.x
        assert queue_panel.region.right == composer.region.right
        assert queue_panel.styles.background == composer.styles.background
        assert queue_panel.styles.border_left[0] == "solid"
        assert queue_panel.styles.border_right[0] == "solid"
        queue_content = queue_panel.query_one("#queued-input-content", Static)
        assert queue_content.region.y - queue_panel.region.y == 1
        assert queue_panel.region.bottom - queue_content.region.bottom == 1
        history = conversation.query_one(ConversationHistory)
        assert history.user_turns == ("first",)

        provider.release.set()
        await pilot.pause(1)

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
        await pilot.pause(0.5)
        await pilot.click("#composer")
        await pilot.press(*"first", "enter")
        await asyncio.wait_for(provider.first_chunk_sent.wait(), timeout=2)
        await pilot.pause(0.2)

        composer = app.query_one(Composer)
        composer.load_text("keep queued")
        await pilot.press("enter")
        composer.load_text("return to composer")
        await pilot.press("enter")
        await pilot.pause(0.3)

        await pilot.press("slash", *"unqueue", "enter")
        await pilot.pause()
        popup = app.query_one(CommandPopup)
        choices = popup.query_one("#command-popup-content", Static).render().plain
        assert popup.argument_command is not None
        assert popup.argument_command.name == "/unqueue"
        assert "1. keep queued" in choices
        assert "2. return to composer" in choices

        await pilot.press("down", "enter")
        await pilot.pause(0.3)

        assert composer.text == "return to composer"
        queue_panel = app.query_one(QueuedInputPanel)
        queued_text = queue_panel.query_one("#queued-input-content", Static).render().plain
        assert queue_panel.display is True
        assert "keep queued" in queued_text
        assert "return to composer" not in queued_text

        provider.release.set()
        await pilot.pause(1)

        snapshot = application.runtime.store.load_session(app._controller.session_id or "")
        assert "return to composer" not in [message.text_content for message in snapshot.messages]
        assert any(event.event_type == "queued_input_cancelled" for event in snapshot.events)


@pytest.mark.asyncio
async def test_queued_input_does_not_hide_or_block_subagent_approval(tmp_path: Path) -> None:
    provider = QueuedSubagentApprovalProvider()
    application = _queued_subagent_approval_application(tmp_path, provider)
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 38)) as pilot:
        await pilot.pause(0.5)
        await pilot.click("#composer")
        await pilot.press(*"start child", "enter")
        await asyncio.wait_for(provider.child_started.wait(), timeout=2)
        await pilot.pause(0.2)

        app.query_one(Composer).load_text("follow after child")
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert app.query_one(QueuedInputPanel).display is True

        provider.release_child.set()
        await pilot.pause(1)

        interaction = app._interaction
        assert interaction is not None
        assert interaction.kind == "approval"
        assert interaction.is_subagent is True
        assert interaction.tool_name == "Danger"
        approval_panel = app.query_one(InteractionBar)
        assert approval_panel.display is True
        assert "Subagent General" in approval_panel.render().plain

        await pilot.press("enter")
        await asyncio.wait_for(provider.queued_input_consumed.wait(), timeout=5)
        await pilot.pause(0.5)

        assert app._interaction is None
        assert app.query_one(InteractionBar).display is False
        assert app.query_one(QueuedInputPanel).display is False
        history = app.query_one(ConversationHistory)
        rendered = "\n".join(entry.text for entry in history.entries if entry.kind == "assistant")
        assert "parent resumed" in rendered
        assert "queued input consumed" in rendered


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
        await pilot.pause(0.5)
        await pilot.click("#composer")
        await pilot.press(*"first", "enter")
        await asyncio.wait_for(provider.first_chunk_sent.wait(), timeout=2)
        await pilot.pause(0.2)

        queue_panel = app.query_one(QueuedInputPanel)
        try:
            app.query_one(Composer).load_text("guide now")
            await pilot.press(submit_key)
            await pilot.pause(0.3)

            queue_text = queue_panel.query_one("#queued-input-content").render().plain
            assert queue_panel.display is True
            assert "GUIDE" in queue_text
            assert "guide now" in queue_text
            assert "guide now" not in app.query_one(ConversationHistory).user_turns
        finally:
            provider.release.set()
        await pilot.pause(1)

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
        await pilot.pause(0.7)

        queue_panel = app.query_one(QueuedInputPanel)
        queue_text = queue_panel.query_one("#queued-input-content").render().plain
        assert queue_panel.display is True
        assert "FOLLOW" in queue_text
        assert "Continue after approval" in queue_text
        assert "Continue after approval" not in app.query_one(ConversationHistory).user_turns
        assert app._interaction is not None
        assert app._interaction.kind == "approval"
        assert app.query_one(InteractionBar).display is True


@pytest.mark.asyncio
async def test_reasoning_chunk_renders_before_provider_continues(tmp_path: Path) -> None:
    provider = BlockingReasoningProvider()
    application = _blocking_application(tmp_path, provider)
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause(0.5)
        app.query_one(ConversationView).set_reasoning_expanded(True)
        await pilot.click("#composer")
        await pilot.press("t", "h", "i", "n", "k", "enter")
        try:
            await asyncio.wait_for(provider.first_chunk_sent.wait(), timeout=2)
            await pilot.pause(0.2)

            assert app._projection is not None
            assert app._projection.reasoning_text == "Inspecting "
            assert app._projection.assistant_text == ""
            assert app._projection.activity.activity == "streaming_reasoning"
            turn = app.query_one(AssistantTurn)
            assert turn.reasoning.text == "Inspecting "
            assert turn.reasoning.label.render().plain.startswith("THINKING · ")
            assert turn.reasoning.label.render().plain.endswith("s")
            assert turn.markdown.source == ""
        finally:
            provider.release.set()
        await pilot.pause(0.5)

        history = app.query_one(ConversationHistory)
        answer = next(entry for entry in reversed(history.entries) if entry.kind == "assistant")
        assert answer.reasoning == "Inspecting the event path."
        assert answer.text == "Done."
        assert "│ Inspecting the event path." in history.rendered_text
        assert answer.reasoning_seconds is not None and answer.reasoning_seconds > 0
        summary = next(entry for entry in reversed(history.entries) if entry.kind == "run")
        assert summary.run is not None
        assert summary.run.model == "blocking"
        assert summary.run.duration_seconds is not None
        assert summary.run.duration_seconds >= answer.reasoning_seconds
        assert "THOUGHT · " in history.rendered_text
        assert "blocking" in history.rendered_text
        assert app._projection is not None
        assert app._projection.activity.status == "completed"

        session_id = app._controller.session_id
        assert session_id is not None
        await app._open_session(session_id)
        await pilot.pause()

        restored = app.query_one(ConversationHistory)
        restored_answer = next(
            entry for entry in reversed(restored.entries) if entry.kind == "assistant"
        )
        restored_summary = next(
            entry for entry in reversed(restored.entries) if entry.kind == "run"
        )
        assert restored_answer.reasoning_seconds == pytest.approx(
            answer.reasoning_seconds,
            abs=0.02,
        )
        assert restored_summary.run is not None
        assert restored_summary.run.run_id == summary.run.run_id
        assert restored_summary.run.model == summary.run.model
        assert restored_summary.run.duration_seconds == summary.run.duration_seconds
        assert "THOUGHT · " in restored.rendered_text
        assert not app.query(AssistantTurn)


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
        await pilot.pause(0.5)
        conversation = app.query_one(ConversationView)
        conversation.set_reasoning_expanded(True)
        await conversation.load_messages((message,))
        await pilot.pause()

        history = app.query_one(ConversationHistory)
        assert len(history.entries) == 1
        entry = history.entries[0]
        assert entry.kind == "assistant"
        assert entry.reasoning == reasoning_text
        assert entry.text == "Final answer"
        rendered_lines = history.rendered_text.splitlines()
        thought_line = next(
            index for index, line in enumerate(rendered_lines) if line.strip() == "THOUGHT"
        )
        answer_line = next(
            index for index, line in enumerate(rendered_lines) if line.strip() == "Final answer"
        )
        assert rendered_lines[thought_line + 1] == ""
        reasoning_lines = [
            line for line in rendered_lines[thought_line + 1 : answer_line] if line.strip()
        ]
        assert len(reasoning_lines) > 1
        assert all(
            line.lstrip().startswith("\N{BOX DRAWINGS LIGHT VERTICAL} ") for line in reasoning_lines
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
        await pilot.pause(0.5)
        conversation = app.query_one(ConversationView)
        await conversation.load_messages((message,))
        history = conversation.query_one(ConversationHistory)
        assert "THOUGHT" in history.rendered_text
        assert "Historical private thought" not in history.rendered_text
        collapsed_lines = history.rendered_text.splitlines()
        collapsed_thought = next(
            index for index, line in enumerate(collapsed_lines) if line.strip() == "THOUGHT"
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
        assert "Historical private thought" in history.rendered_text
        assert preferences.load().reasoning_expanded is True

        await conversation.append_reasoning("Live private thought")
        live_reasoning = app.query_one(AssistantTurn).reasoning
        assert live_reasoning.text == "Live private thought"
        assert live_reasoning._content.display is True

        await app._dispatch_command("/thoughts collapse")
        await pilot.pause()
        assert "Historical private thought" not in history.rendered_text
        assert live_reasoning._content.display is False

        await conversation.append_assistant("Live final answer")
        await pilot.pause()
        live_turn = app.query_one(AssistantTurn)
        assert live_turn.markdown.region.y == live_reasoning.label.region.bottom + 1

        await app._set_tui_theme("harbor")
        assert preferences.load() == TuiPreferences(
            theme="harbor",
            reasoning_expanded=False,
        )

    restarted = OuroTuiApp(home=tmp_path, application=_application(tmp_path))
    async with restarted.run_test(size=(100, 32)) as pilot:
        await pilot.pause(0.5)
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
        await pilot.pause(0.5)
        conversation = app.query_one(ConversationView)
        presentation = project_run_presentation(run, messages)
        await conversation.load_messages(messages, runs=(presentation,))
        await pilot.pause()

        history = conversation.query_one(ConversationHistory)
        thoughts = [entry for entry in history.entries if entry.kind == "assistant"]
        summaries = [entry for entry in history.entries if entry.kind == "run"]
        assert [entry.reasoning_seconds for entry in thoughts] == [0.5, 0.5]
        assert len(summaries) == 1
        assert summaries[0].run == presentation
        assert presentation.model == "chat-endpoint"
        assert presentation.duration_seconds == 3
        assert history.rendered_text.count("THOUGHT · 500ms") == 2
        assert "chat-endpoint" in history.rendered_text
        assert "3.0s" in history.rendered_text


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
        await pilot.pause(0.5)
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
            created_at=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
        ),
    )

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause(0.5)
        conversation = app.query_one(ConversationView)
        await conversation.load_messages(messages, compactions)
        await pilot.pause()

        history = conversation.query_one(ConversationHistory)
        assert [(entry.kind, entry.text) for entry in history.entries] == [
            ("user", "First question"),
            ("notice", "Context compacted · 1 message summarized"),
            ("assistant", "First answer"),
            ("notice", "Context compacted · 2 messages summarized"),
            ("user", "Second question"),
        ]

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
        await pilot.pause(0.5)
        session_id = "ses_live_compaction"
        app._projection = TuiSessionProjection(session_id)
        await app._apply_runtime_events(
            (
                EventRecord(
                    session_id=session_id,
                    run_id="run_compaction",
                    event_type="compaction_completed",
                    sequence=1,
                    payload={
                        "compaction_id": "cmp_live",
                        "covered_message_count": 4,
                        "context_metrics_after": metrics.model_dump(mode="json"),
                    },
                ),
            )
        )
        await pilot.pause()

        history = app.query_one(ConversationHistory)
        assert history.entries[-1].text == "Context compacted · 4 messages summarized"
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
        await pilot.pause(0.5)
        conversation = app.query_one(ConversationView)
        await conversation.load_messages(messages)
        await pilot.pause()

        history = conversation.query_one(ConversationHistory)
        assert len(history.entries) == len(messages)
        assert len(list(conversation.query("*"))) == 2


@pytest.mark.asyncio
async def test_completed_live_turns_keep_the_widget_tree_bounded(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause(0.5)
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
        await pilot.pause(0.5)
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
async def test_history_load_renders_tool_calls_between_messages(tmp_path: Path) -> None:
    """Loading a persisted session must render tool calls, not just text.

    A tool call spans two messages: an assistant message carrying the request
    (``tool_calls``) and a following ``tool_result`` message carrying the
    outcome reference. ``load_messages`` must pair them and render a
    ``ToolActivityRow`` in the right dialogue position.
    """
    app = OuroTuiApp(application=_application(tmp_path))
    tool_call_id = "tool_history_read"
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
        ),
        MessageRecord(
            session_id="ses_tools",
            role="assistant",
            content=[TextContent(text="The config has 3 lines.")],
        ),
    )

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause(0.5)
        conversation = app.query_one(ConversationView)
        await conversation.load_messages(messages)
        await pilot.pause()

        history = conversation.query_one(ConversationHistory)
        tools = [entry.tool for entry in history.entries if entry.tool is not None]
        assert len(tools) == 1
        assert tools[0].tool_call_id == tool_call_id
        assert tools[0].status == "completed"

        rendered = history.rendered_text
        assert "read" in rendered
        assert "config.json" in rendered

        # Dialogue order: user text, tool row, assistant text — interleaved.
        kinds = [entry.kind for entry in history.entries]
        assert kinds == ["user", "tool", "assistant"]


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
        await pilot.pause(0.5)
        conversation = app.query_one(ConversationView)
        await conversation.load_messages(messages)
        await pilot.pause()

        history = conversation.query_one(ConversationHistory)
        tools = [entry.tool for entry in history.entries if entry.tool is not None]
        assert len(tools) == 1
        assert tools[0].status == "failed"
        assert "failed" in history.rendered_text


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
        await pilot.pause(0.5)
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
        await pilot.pause(0.5)
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
        await pilot.pause(0.5)
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
        await pilot.pause(0.5)
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
        await pilot.pause(0.5)
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
        await pilot.pause(0.5)
        conversation = app.query_one(ConversationView)
        await conversation.load_messages(_scrollable_messages())
        await pilot.pause()

        options = app._navigate_options()
        values = [opt.value for opt in options]
        assert values[:2] == ["top", "bottom"]
        # 15 user messages → #1 through #15
        assert values[2:] == [f"#{i}" for i in range(1, 16)]


@pytest.mark.asyncio
async def test_reasoning_burst_cannot_starve_live_text_rendering(tmp_path: Path) -> None:
    provider = ReasoningBurstThenBlockProvider()
    application = _blocking_application(tmp_path, provider)
    app = OuroTuiApp(application=application)
    projection_text = ""
    activity_status = ""
    markdown_source = ""
    sink_failed = True

    async def exercise(pilot) -> None:
        nonlocal projection_text, activity_status, markdown_source, sink_failed
        await pilot.pause(0.5)
        await pilot.click("#composer")
        await pilot.press("b", "u", "r", "s", "t", "enter")
        try:
            await asyncio.wait_for(provider.visible_chunk_sent.wait(), timeout=15)
            await pilot.pause(0.5)

            assert app._projection is not None
            projection_text = app._projection.assistant_text
            activity_status = app._projection.activity.status
            turns = app.query(AssistantTurn)
            markdown_source = turns.last().markdown.source if turns else ""
            events = application.runtime.sessions.event_page(
                app._projection.session_id,
                cursor=0,
                limit=1000,
            ).events
            sink_failed = any(event.event_type == "event_sink_failed" for event in events)
        finally:
            provider.release.set()
            await pilot.pause(1)
        app.exit()

    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    try:
        await asyncio.wait_for(
            app.run_async(
                headless=True,
                size=(100, 32),
                auto_pilot=exercise,
            ),
            timeout=20,
        )
    finally:
        loop.set_task_factory(previous_factory)

    assert projection_text == "visible while running"
    assert activity_status != "completed"
    assert markdown_source == "visible while running"
    assert sink_failed is False


@pytest.mark.asyncio
async def test_run_result_replays_events_when_live_sink_is_delayed(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path, "Recovered response"))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause(0.5)
        app._controller._publish_event = lambda _event: None
        await pilot.click("#composer")
        await pilot.press("h", "e", "l", "l", "o", "enter")
        await pilot.pause(0.8)

        assert app._projection is not None
        assert app._projection.assistant_text == "Recovered response"
        assert app._projection.activity.status == "completed"


@pytest.mark.asyncio
async def test_composer_uses_shift_enter_for_newline(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause(0.5)
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
        await pilot.pause(0.5)
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
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 34), notifications=True) as pilot:
        await pilot.pause(0.5)
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

        archive_screen.query_one("#choice-filter", Input).value = "Active session"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen is archive_screen
        choices = {choice.value: choice for choice in archive_screen._choices}
        assert choices[active.session_id].group == "Archived"
        assert choices[archived.session_id].group == "Archived"

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
async def test_delete_session_menu_confirms_and_batch_deletes_without_closing(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    sessions = tuple(
        application.runtime.sessions.create(
            session_id=f"ses_delete_{index}",
            title=f"Delete {index}",
        ).session
        for index in range(3)
    )
    for session in sessions:
        application.runtime.sessions.set_archived(session.session_id, True)
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 34), notifications=True) as pilot:
        await pilot.pause(0.5)
        await pilot.press("ctrl+p")
        await pilot.pause()
        app.screen.query_one("#choice-filter", Input).value = "Delete session"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        delete_screen = app.screen
        assert isinstance(delete_screen, MultiChoiceScreen)
        assert {choice.group for choice in delete_screen._choices} == {"Archived"}

        for title in ("Delete 0", "Delete 1"):
            delete_screen.query_one("#choice-filter", Input).value = title
            await pilot.pause()
            await pilot.press("space")
            await pilot.pause()
        choices = {choice.label: choice for choice in delete_screen._choices}
        assert choices["Delete 0"].marker == SELECTED_CHOICE_MARKER
        assert choices["Delete 1"].marker == SELECTED_CHOICE_MARKER
        assert choices["Delete 2"].marker == ""

        delete_screen.query_one("#choice-filter", Input).value = ""
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen is delete_screen
        assert delete_screen.query_one("#choice-title", Static).render().plain == (
            "CONFIRM DELETE SESSIONS"
        )
        assert [choice.label for choice in delete_screen._choices] == [
            "Cancel",
            "Delete 2 selected",
        ]
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen is delete_screen
        assert len(delete_screen._choices) == 3
        assert delete_screen.query_one("#choice-title", Static).render().plain == (
            "DELETE SESSIONS"
        )

        await pilot.press("enter")
        await pilot.pause()
        assert app.screen is delete_screen
        await pilot.press("down", "enter")
        await pilot.pause()
        assert app.screen is delete_screen
        assert [choice.label for choice in delete_screen._choices] == ["Delete 2"]
        assert delete_screen._choices[0].group == "Archived"
        assert delete_screen._choices[0].marker == ""
        assert {item.session_id for item in application.runtime.sessions.catalog().entries} == {
            sessions[2].session_id
        }
        assert not list(app.screen.query_one("#textual-toastrack").query("Toast"))
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_delete_session_toggle_preserves_list_scroll_position(tmp_path: Path) -> None:
    async def confirm_selected(_selected: tuple[str, ...]) -> tuple[Choice, ...] | None:
        return None

    choices = tuple(
        Choice(
            f"ses_delete_scroll_{index:02d}",
            f"Delete scroll {index:02d}",
            group="Archived",
        )
        for index in range(30)
    )
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause(0.5)
        app.push_screen(
            MultiChoiceScreen(
                title="DELETE SESSIONS",
                placeholder="Filter archived sessions…",
                choices=choices,
                on_confirm=confirm_selected,
                selected_marker=SELECTED_CHOICE_MARKER,
            )
        )
        await pilot.pause()

        delete_screen = app.screen
        assert isinstance(delete_screen, MultiChoiceScreen)
        options = delete_screen.query_one("#choice-list", OptionList)
        target = choices[15].value
        target_index = options.get_option_index(target)
        options.highlighted = target_index
        options.focus()
        options.scroll_to(y=target_index - 2, animate=False, immediate=True)
        await pilot.pause()
        scroll_before = options.scroll_y
        assert scroll_before > 0

        await pilot.press("space")
        await pilot.pause()

        assert options.highlighted == target_index
        assert options.scroll_y == scroll_before
        assert next(
            choice for choice in delete_screen._choices if choice.value == target
        ).marker == SELECTED_CHOICE_MARKER

        await pilot.press("space")
        await pilot.pause()

        assert options.highlighted == target_index
        assert options.scroll_y == scroll_before
        assert next(
            choice for choice in delete_screen._choices if choice.value == target
        ).marker == ""


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
        assert action_screen.query_one("#choice-panel", Vertical).display is False
        assert action_screen.styles.background.a == 0
        assert rename_screen.styles.background.a == 0.45
        assert [choice.value for choice in rename_screen._choices] == [active.session_id]
        assert {choice.group for choice in rename_screen._choices} == {"Today"}

        rename_screen.query_one("#choice-filter", Input).value = "Original title"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen is rename_screen
        assert rename_screen._editing_value == active.session_id
        assert rename_screen.query_one("#choice-filter", Input).disabled is True
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
        assert rename_screen.query_one("#choice-filter", Input).value == ""
        assert rename_screen._choices[0].edit_value == "Renamed title"
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
async def test_switching_sessions_clears_the_previous_approval_panel(tmp_path: Path) -> None:
    application = _application(tmp_path)
    destination = application.runtime.sessions.create(
        session_id="ses_destination",
        title="Destination",
    ).session
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause(0.5)
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
        assert app.query_one(BrandHeader).render().plain == " ▣ OURO Destination"
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
        await pilot.pause(0.5)
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
            "ACTIONS",
        ]

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
        assert draft.supports_vision is True
        assert draft.supports_streaming is True


@pytest.mark.asyncio
async def test_session_settings_can_be_completed_with_keyboard_only(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_thinking_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause(0.5)
        await pilot.press("slash", *"approval", "enter", *"afk", "enter")
        await pilot.pause()
        assert await app._controller.session_approval_preset() == "afk"

        await pilot.press("slash", *"thinking", "enter", *"high", "enter")
        await pilot.pause()
        reasoning = await app._controller.session_reasoning()
        assert reasoning.requested.effort == "high"
        assert reasoning.effective.effort == "high"


@pytest.mark.asyncio
async def test_thinking_completion_and_modal_share_supported_choices(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_thinking_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause(0.5)
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
async def test_status_line_shows_thinking_level_and_highlights_peak_effort(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_thinking_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause(0.5)
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
        await pilot.pause(0.5)
        indicator = app.query_one(ActivityIndicator)
        # The default faux endpoint declares no reasoning capability, so no
        # thinking level is projected onto the status line.
        assert indicator._reasoning is None


@pytest.mark.asyncio
async def test_actions_groups_granular_settings_by_function_without_descriptions(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause(0.5)

        await pilot.press("ctrl+p")
        await pilot.pause()
        action_choices = app.screen._choices
        action_values = {choice.value for choice in action_choices}
        assert "/thinking" not in action_values
        assert "/compact" not in action_values
        assert [choice.label for choice in action_choices] == [
            "Theme color",
            "Model thought display",
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
            "List MCP servers",
            "Add MCP server",
            "Connect MCP server",
            "Disconnect MCP server",
            "Edit MCP server",
            "Remove MCP server",
            "List skills",
            "Install skill",
            "Validate skill",
            "Update skill",
            "Remove skill",
            "Compaction policy",
            "Abort run",
            "Keyboard help",
        ]
        assert [choice.group for choice in action_choices] == [
            *("Appearance",) * 2,
            *("Session",) * 5,
            *("Providers",) * 9,
            *("Plugins",) * 3,
            *("Tools",) * 2,
            *("MCP",) * 6,
            *("Skills",) * 5,
            "Compaction",
            "Runtime",
            "Support",
        ]
        assert "Configuration" not in {choice.group for choice in action_choices}
        assert "Recent" not in {choice.group for choice in action_choices}
        assert all(choice.description == "" for choice in action_choices)
        assert {choice.label: choice.trailing for choice in action_choices if choice.trailing} == {
            "New session": "Ctrl+N",
            "Open session": "Ctrl+S",
            "List plugins": "Alt+P",
            "Abort run": "Ctrl+C",
            "Keyboard help": "Ctrl+H",
        }

        app.screen.query_one("#choice-filter", Input).value = "Compaction policy"
        await pilot.pause()
        await pilot.press("enter", "enter")
        await pilot.pause(0.2)
        assert isinstance(app.screen, CompactionConfigurationScreen)
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
        await pilot.pause(0.5)
        await pilot.press("ctrl+p")
        await pilot.pause()

        app.screen.query_one("#choice-filter", Input).value = "Add endpoint"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen.query_one("#choice-title", Static).render().plain == (
            "ADD ENDPOINT · PROVIDER"
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
        await pilot.pause(0.5)
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
            "PROVIDER · secondary"
        )
        toggle = next(choice for choice in app.screen._choices if choice.value == "toggle")
        assert toggle.label == "Enable provider"

        options = app.screen.query_one("#choice-list", OptionList)
        options.highlighted = options.get_option_index("toggle")
        options.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert ConfigurationFacade(tmp_path).provider("secondary").enabled is True
        assert next(
            choice for choice in app.screen._choices if choice.value == "toggle"
        ).label == "Disable provider"

        await pilot.press("escape")
        await pilot.pause()
        assert next(
            choice for choice in app.screen._choices if choice.value == "secondary"
        ).trailing.endswith("· enabled")
        await pilot.press("enter")
        await pilot.pause()
        options = app.screen.query_one("#choice-list", OptionList)
        await pilot.click(options, offset=(4, 1))
        await pilot.pause()
        assert ConfigurationFacade(tmp_path).provider("secondary").enabled is False
        assert next(
            choice for choice in app.screen._choices if choice.value == "toggle"
        ).label == "Enable provider"

        await pilot.press("escape")
        await pilot.pause()
        assert next(
            choice for choice in app.screen._choices if choice.value == "secondary"
        ).trailing.endswith("· disabled")
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
        await pilot.pause(0.5)
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
        await pilot.pause(0.5)
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
            SELECTED_CHOICE_MARKER,
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
        await pilot.pause(0.5)
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
        await pilot.pause(0.5)
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
        assert choices["secondary/secondary-chat"].marker == SELECTED_CHOICE_MARKER
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
        await pilot.pause(0.5)
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
        await pilot.pause(0.5)
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
            "ACTIONS",
        ]
        assert screen._values["provider-api-key-env"] == "GATEWAY_API_KEY"
        assert screen._values["provider-base-url"] == "https://gateway.example.invalid/v1"
        assert screen._values["provider-api-path"] == "/chat/completions"
        assert screen._values["provider-timeout"] == "60.0"

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
        await pilot.pause(0.5)
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
        await pilot.pause(0.5)
        for label, screen_type in (
            ("Theme color", ChoiceScreen),
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
            "Theme color",
        ]
        assert all(choice.group == "Recent" for choice in app.screen._choices[:3])

    restarted = OuroTuiApp(home=tmp_path, application=_application(tmp_path))
    async with restarted.run_test(size=(100, 34)) as pilot:
        await pilot.pause(0.5)
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert [choice.label for choice in restarted.screen._choices[:3]] == [
            "Add MCP server",
            "Compaction policy",
            "Theme color",
        ]
        assert all(choice.group == "Recent" for choice in restarted.screen._choices[:3])


@pytest.mark.asyncio
async def test_plugin_actions_reuse_grouped_menu_and_refresh_lifecycle_status(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with app.run_test(size=(100, 34), notifications=True) as pilot:
        await pilot.pause(0.5)
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
        assert toolkit.trailing == "v1.0.0 · active"
        assert toolkit.marker == ""

        plugin_screen.query_one("#choice-filter", Input).value = "Toolkit"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="PLUGIN · Toolkit")
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
        await _wait_for_screen(app, ChoiceScreen, title="PLUGIN · Toolkit")
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
        assert refreshed.trailing == "v1.0.0 · disabled"
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="PLUGIN · Toolkit")
        options = plugin_screen.query_one("#choice-list", OptionList)
        await pilot.click(options, offset=(4, 1))
        await _wait_for_screen(app, ChoiceScreen, title="PLUGIN · Toolkit")
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
        assert refreshed.trailing == "v1.0.0 · active"


@pytest.mark.asyncio
async def test_plugin_install_failure_uses_error_toast_without_changing_conversation(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with app.run_test(size=(100, 34), notifications=True) as pilot:
        await pilot.pause(0.5)
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
        await pilot.pause(0.5)

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
        await pilot.pause(0.5)
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
        await pilot.pause(0.5)
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
        await pilot.pause(0.5)
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
        await pilot.pause(0.5)
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
        await pilot.pause(0.5)
        composer = app.query_one("#composer", TextArea)
        composer.focus()
        app.post_message(events.Key("question_mark", "?"))
        await pilot.pause()

        assert composer.text == "?"
        assert not isinstance(app.screen, HelpScreen)


@pytest.mark.asyncio
async def test_action_menu_executes_new_session_and_help_shortcuts_in_context(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause(0.5)
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        menu = app.screen

        await pilot.press("ctrl+h")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        assert menu in app.screen_stack
        assert menu.query_one("#choice-panel", Vertical).display is False

        await pilot.press("ctrl+h")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        assert app.screen is menu

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
        await pilot.pause(0.5)
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
        await pilot.pause(0.5)
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
        await pilot.pause(0.5)
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
            assert app.screen._choices[0].trailing == "enabled · configured"
            assert app.screen._choices[0].description == ""

            await pilot.press("escape")
            await pilot.pause()


@pytest.mark.asyncio
async def test_tool_permissions_group_all_registered_sources(tmp_path: Path) -> None:
    ConfigurationFacade(tmp_path).set_tool_permission("UnavailableTest", "deny")
    app = OuroTuiApp(home=tmp_path, application=_application_with_tool_sources(tmp_path))

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause(0.5)
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
        assert choices["BuiltinTest"].trailing == "default · approval"
        assert choices["ContractControlledTest"].trailing == "contract · auto"
        assert choices["ContractControlledTest"].disabled is True
        assert choices["ThirdPartyPluginTest"].disabled is False
        assert choices["UnavailableTest"].trailing == "deny · unavailable"
        assert all(choice.description == "" for choice in choices.values())


@pytest.mark.asyncio
async def test_tool_permission_selection_returns_to_refreshed_tool_menu(tmp_path: Path) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application_with_tool_sources(tmp_path))

    async with app.run_test(size=(100, 34), notifications=True) as pilot:
        await pilot.pause(0.5)
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
            title="TOOL PERMISSION · BuiltinTest",
        )
        assert app.screen is tool_screen
        assert sum(isinstance(screen, ChoiceScreen) for screen in app.screen_stack) == 1
        current = next(choice for choice in app.screen._choices if choice.value == "default")
        assert current.label == "default"
        assert current.marker == SELECTED_CHOICE_MARKER
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
            title="TOOL PERMISSION · BuiltinTest",
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
            == "default · approval"
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
        await pilot.pause(0.5)
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
        await _wait_for_screen(app, ChoiceScreen, title="MCP · local")
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
        assert next(
            choice for choice in app.screen._choices if choice.value == "toggle"
        ).label == "Enable server"
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
        assert next(
            choice for choice in app.screen._choices if choice.value == "local"
        ).trailing == "disabled · disabled"
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="MCP · local")
        options = app.screen.query_one("#choice-list", OptionList)
        await pilot.click(options, offset=(4, 1))
        await pilot.pause()
        assert ConfigurationFacade(tmp_path).mcp_server("local").enabled is True
        assert next(
            choice for choice in app.screen._choices if choice.value == "toggle"
        ).label == "Disable server"
        assert next(
            choice for choice in app.screen._choices if choice.value == "start"
        ).disabled is False

        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="MCP SERVERS")
        assert app.screen is action_screen
        assert next(
            choice for choice in app.screen._choices if choice.value == "local"
        ).trailing.startswith("enabled ·")
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
        await pilot.pause(0.5)
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        action_screen = app.screen
        action_screen.query_one("#choice-filter", Input).value = "List MCP servers"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="MCP SERVERS")
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="MCP · local")

        options = action_screen.query_one("#choice-list", OptionList)
        assert options.get_option("start").disabled is False
        session.initialize_gate.set()
        options.highlighted = options.get_option_index("start")
        options.focus()
        await pilot.press("enter")
        await pilot.pause()

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
        assert next(
            choice for choice in action_screen._choices if choice.value == "local"
        ).trailing == "enabled · stopped"


@pytest.mark.asyncio
async def test_configuration_editor_restores_retained_action_menu(tmp_path: Path) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause(0.5)
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
        await pilot.pause(0.5)
        for action_label, title in (
            ("Theme color", "THEME COLOR"),
            ("Open session", "SESSIONS"),
            ("Archive session", "ARCHIVE SESSIONS"),
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
async def test_configuration_editors_replace_visible_parent_at_every_depth(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with app.run_test(size=(100, 42)) as pilot:
        await pilot.pause(0.5)
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
        await pilot.pause(0.5)
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
        assert app.screen._choices[0].trailing == "user · enabled"


@pytest.mark.asyncio
async def test_skill_menu_loads_live_plugin_skill_as_disabled_choice(tmp_path: Path) -> None:
    source = tmp_path / "skill-plugin"
    _write_skill_plugin(source)
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause(0.5)
        installed = await app._controller.install_plugin(
            TuiPluginInstallConfiguration(source=str(source))
        )
        await app._controller.authorize_plugin(installed.plugin_id)
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
        assert app.screen._choices[0].trailing == "plugin · enabled"


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
        await pilot.pause(0.5)
        await pilot.press("ctrl+p")
        await pilot.pause()
        app.screen.query_one("#choice-filter", Input).value = "List skills"
        await pilot.pause()
        await pilot.press("enter", "enter")
        await pilot.pause()

        assert app.screen.query_one("#choice-title", Static).render().plain == "SKILL · demo"
        assert app.screen._choices[0].label == "Disable"

        options = app.screen.query_one("#choice-list", OptionList)
        options.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert app._controller.runtime_skill_names() == ()
        assert app.screen._choices[0].label == "Enable"

        await pilot.press("escape")
        await pilot.pause()
        assert next(
            choice for choice in app.screen._choices if choice.value == "demo"
        ).trailing == "user · disabled"
        await pilot.press("enter")
        await pilot.pause()
        options = app.screen.query_one("#choice-list", OptionList)
        await pilot.click(options, offset=(4, 0))
        await pilot.pause()
        assert app._controller.runtime_skill_names() == ("demo",)
        assert app.screen._choices[0].label == "Disable"

        await pilot.press("escape")
        await pilot.pause()
        assert next(
            choice for choice in app.screen._choices if choice.value == "demo"
        ).trailing == "user · enabled"
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_mcp_configuration_screen_validates_json_and_returns_typed_draft(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    results = []

    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause(0.5)
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
        await pilot.pause(0.5)
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
async def test_skill_install_uses_shared_grouped_inline_menu(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    results = []

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause(0.5)
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
        await pilot.pause(0.5)
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
        await pilot.pause(0.5)
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
    enabled = await application.plugins.enable("dev.ouro.goal_mode")
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
        await pilot.pause(0.5)
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
        await pilot.pause(0.5)
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

        await app._dispatch_command("/quite")
        await pilot.pause()
        assert app.is_running is False


@pytest.mark.asyncio
async def test_welcome_command_popup_aligns_without_pushing_logo_up(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 40)) as pilot:
        welcome = await _wait_for_welcome_render(app, pilot, contains="██████")
        composer = app.query_one(Composer)
        popup = app.query_one(CommandPopup)
        status_line = app.query_one(StatusLine)
        resting_regions = (
            welcome._logo.region,
            composer.region,
            status_line.region,
            welcome._lower.region,
            welcome._system.region,
        )

        await pilot.press("slash")
        await pilot.pause()

        assert popup.display is True
        assert popup.styles.overlay == "screen"
        assert popup.region.x == composer.region.x
        assert popup.region.right == composer.region.right
        assert popup.region.bottom == composer.region.y
        assert (
            welcome._logo.region,
            composer.region,
            status_line.region,
            welcome._lower.region,
            welcome._system.region,
        ) == resting_regions

        composer.clear()
        await pilot.pause()

        assert popup.display is False
        assert (
            welcome._logo.region,
            composer.region,
            status_line.region,
            welcome._lower.region,
            welcome._system.region,
        ) == resting_regions


@pytest.mark.asyncio
async def test_command_popup_reflows_session_viewport_atomically(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause(0.5)
        conversation = app.query_one(ConversationView)
        await conversation.load_messages(_scrollable_messages())
        await pilot.pause()

        popup = app.query_one(CommandPopup)
        composer = app.query_one(Composer)
        transcript = conversation.query_one("#conversation-transcript")

        def visual_state() -> tuple[Region, int, Region, Region]:
            return (
                conversation.region,
                round(conversation.scroll_y),
                transcript.region,
                transcript.children[-1].region,
            )

        resting_state = visual_state()
        frames: list[tuple[Region, int, Region, Region]] = []

        def assert_atomic_transition(
            before: tuple[Region, int, Region, Region],
            after: tuple[Region, int, Region, Region],
        ) -> None:
            assert before != after
            assert frames
            assert frames[-1] == after
            first_final_frame = frames.index(after)
            assert all(frame == before for frame in frames[:first_final_frame])
            assert all(frame == after for frame in frames[first_final_frame:])

        post_display_hook = app.post_display_hook

        def capture_frame() -> None:
            frames.append(visual_state())

        app.post_display_hook = capture_frame
        try:
            await pilot.click("#composer")
            frames.clear()
            await pilot.press("slash")
            await pilot.pause()

            command_state = visual_state()
            assert popup.display is True
            assert popup.styles.overlay != "screen"
            assert command_state[0].bottom == popup.region.y
            assert popup.region.bottom == composer.region.y
            assert_atomic_transition(resting_state, command_state)
            viewport_delta = resting_state[0].height - command_state[0].height
            assert command_state[3].bottom == resting_state[3].bottom - viewport_delta

            frames.clear()
            await pilot.press("h")
            await pilot.pause()

            filtered_state = visual_state()
            assert popup.display is True
            assert popup.selected_command is not None
            assert popup.selected_command.name == "/help"
            assert filtered_state[0].bottom == popup.region.y
            assert_atomic_transition(command_state, filtered_state)

            frames.clear()
            composer.clear()
            await pilot.pause()

            assert popup.display is False
            assert visual_state() == resting_state
            assert_atomic_transition(filtered_state, resting_state)

            await pilot.press("ctrl+up")
            await pilot.pause()
            assert conversation.scroll_y == 0

            await pilot.press("ctrl+down")
            await pilot.pause()
            assert conversation.scroll_y == conversation.max_scroll_y
        finally:
            app.post_display_hook = post_display_hook


@pytest.mark.asyncio
@pytest.mark.parametrize("panel_kind", ("queue", "approval"))
async def test_dynamic_flow_panels_reflow_anchored_conversation_atomically(
    tmp_path: Path,
    panel_kind: str,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause(0.5)
        conversation = app.query_one(ConversationView)
        await conversation.load_messages(_scrollable_messages())
        await pilot.pause()
        transcript = conversation.query_one("#conversation-transcript")

        def visual_state() -> tuple[Region, int, Region, Region]:
            return (
                conversation.region,
                round(conversation.scroll_y),
                transcript.region,
                transcript.children[-1].region,
            )

        resting_state = visual_state()
        frames: list[tuple[Region, int, Region, Region]] = []
        post_display_hook = app.post_display_hook
        app.post_display_hook = lambda: frames.append(visual_state())
        try:
            if panel_kind == "queue":
                panel = app.query_one(QueuedInputPanel)
                panel.set_inputs(
                    (
                        QueuedUserInputView(
                            queue_id="queued_atomic_layout",
                            prompt="Continue after the current run",
                            mode="follow_up",
                            queued_at=datetime.now(UTC),
                        ),
                    )
                )
            else:
                panel = app.query_one(InteractionBar)
                app._sync_interaction(
                    PendingInteraction(
                        kind="approval",
                        run_id="run_atomic_layout",
                        tool_call_id="call_atomic_layout",
                        tool_name="Write",
                        request_id="approval_atomic_layout",
                    )
                )
            await pilot.pause()

            panel_state = visual_state()
            assert panel.display is True
            assert panel_state[0].height < resting_state[0].height
            first_panel_frame = frames.index(panel_state)
            assert all(frame == resting_state for frame in frames[:first_panel_frame])
            assert all(frame == panel_state for frame in frames[first_panel_frame:])
            viewport_delta = resting_state[0].height - panel_state[0].height
            assert panel_state[3].bottom == resting_state[3].bottom - viewport_delta

            frames.clear()
            if panel_kind == "queue":
                panel.set_inputs(())
            else:
                app._sync_interaction(None)
            await pilot.pause()

            assert visual_state() == resting_state
            first_resting_frame = frames.index(resting_state)
            assert all(frame == panel_state for frame in frames[:first_resting_frame])
            assert all(frame == resting_state for frame in frames[first_resting_frame:])
        finally:
            app.post_display_hook = post_display_hook


@pytest.mark.asyncio
async def test_slash_opens_filters_navigates_and_executes_command_popup(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause(0.5)
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
async def test_command_popup_truncates_long_rows_on_first_open(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(80, 32)) as pilot:
        await _wait_for_welcome_render(app, pilot, contains="██████")
        popup = app.query_one(CommandPopup)
        popup.show_commands(
            (
                TuiCommand("/short", "Short command", "Short command"),
                TuiCommand(
                    "/long",
                    "A command title that is intentionally much wider than the popup",
                    "Long command",
                ),
            )
        )
        await pilot.pause()

        rendered = popup.query_one("#command-popup-content", Static).render().plain
        rendered_lines = rendered.splitlines()
        assert len(rendered_lines) == 2
        assert rendered_lines[1].endswith("…")


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ("actions", "menu"))
async def test_actions_command_opens_action_menu(tmp_path: Path, command: str) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause(0.5)
        await pilot.press("slash", *command, "enter")
        await pilot.pause()

        assert isinstance(app.screen, ChoiceScreen)
        assert app.screen.query_one("#choice-title", Static).render().plain == "ACTIONS"


@pytest.mark.asyncio
async def test_model_command_selects_from_inline_argument_menu(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 32), notifications=True) as pilot:
        await pilot.pause(0.5)
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
        await pilot.pause(0.5)
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
        await pilot.pause(0.5)
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
        await pilot.pause(0.5)
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
        await pilot.pause(0.8)

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
        await pilot.pause(0.5)
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
            execution=PluginCommandExecutionView(command_id="dev.ouro.plan_mode:plan")
        )

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause(0.5)
        monkeypatch.setattr(
            app._controller,
            "plugin_commands",
            lambda: (
                PluginCommandView(
                    command_id="dev.ouro.plan_mode:plan",
                    plugin_id="dev.ouro.plan_mode",
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
        assert composer.suggestion == "[on|off|status]"
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
            execution=PluginCommandExecutionView(command_id="dev.ouro.goal_mode:goal")
        )

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause(0.5)
        monkeypatch.setattr(
            app._controller,
            "plugin_commands",
            lambda: (
                PluginCommandView(
                    command_id="dev.ouro.goal_mode:goal",
                    plugin_id="dev.ouro.goal_mode",
                    name="goal",
                    description="Control a Goal",
                    usage="[status|pause|resume|cancel|replace] | <request>",
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
        assert composer.suggestion == "[status|pause|resume|cancel|replace] | <request>"
        assert popup.argument_command is not None

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
async def test_plan_command_without_run_restores_idle_tui_state(tmp_path: Path) -> None:
    application = _application(tmp_path)
    await application.start()
    assert application.plugins is not None
    enabled = await application.plugins.enable("dev.ouro.plan_mode")
    assert enabled.status == "active"
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause(0.5)
        await pilot.press("slash", "p", "l", "a", "n", "space", "o", "n", "enter")
        await pilot.pause(0.8)

        assert app._projection is not None
        assert app._projection.activity.status == "idle"
        assert app._projection.activity.activity == "idle"
        assert app._has_active_run() is False
        assert app.query_one(Composer).disabled is False
        indicator = app.query_one(StatusLine).query_one(ActivityIndicator)
        assert "Ready" in indicator.render().plain
        assert "Queued" not in indicator.render().plain


@pytest.mark.asyncio
async def test_approval_panel_selects_with_arrows_and_resumes_run(tmp_path: Path) -> None:
    target = tmp_path / "approved.txt"
    app = OuroTuiApp(
        application=_approval_application(tmp_path, target),
        approval_mode="approval",
    )

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause(0.5)
        await pilot.click("#composer")
        await pilot.press("w", "r", "i", "t", "e", "enter")
        await pilot.pause(0.8)

        assert app._projection is not None
        assert app._projection.pending_interaction is not None
        assert app._projection.pending_interaction.kind == "approval"
        panel = app.query_one(InteractionBar)
        composer = app.query_one(Composer)
        assert panel.has_focus is True
        assert panel.selected_approved is True
        assert composer.disabled is True

        await pilot.press("down")
        assert panel.selected_approved is False

        await pilot.press("up")
        assert panel.selected_approved is True

        await pilot.press("enter")
        await pilot.pause(0.8)

        assert target.read_text(encoding="utf-8") == "written"
        assert app._projection.pending_interaction is None
        assert app._projection.activity.status == "completed"
        assert composer.disabled is False


@pytest.mark.asyncio
async def test_parallel_approvals_advance_one_at_a_time_without_stalling(tmp_path: Path) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    app = OuroTuiApp(
        application=_multi_approval_application(tmp_path, (first, second)),
        approval_mode="approval",
    )

    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.pause(0.5)
        await pilot.click("#composer")
        await pilot.press("r", "u", "n", "enter")
        await pilot.pause(0.8)

        assert app._projection is not None
        assert [
            interaction.tool_call_id for interaction in app._projection.pending_interactions
        ] == ["call_write_1", "call_write_2"]

        await pilot.press("enter")
        await pilot.pause(0.6)

        assert app._projection.pending_interaction is not None
        assert app._projection.pending_interaction.tool_call_id == "call_write_2"
        assert app.query_one(InteractionBar).has_focus is True
        assert first.exists() is False
        assert second.exists() is False

        await pilot.press("enter")
        await pilot.pause(0.8)

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
        await pilot.pause(0.5)
        await pilot.click("#composer")
        await pilot.press("w", "r", "i", "t", "e", "enter")
        await pilot.pause(0.8)

        panel = app.query_one(InteractionBar)
        await pilot.press("down", "enter")
        await pilot.pause(0.8)

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
        await pilot.pause(0.5)
        await pilot.click("#composer")
        await pilot.press("a", "s", "k", "enter")
        await pilot.pause(0.7)

        panel = app.query_one(AskUserPanel)
        assert panel.display is True
        assert panel.has_focus is True
        assert "Which file should OURO use?" in str(panel.render())

        await pilot.press("2")
        await pilot.pause(0.8)

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
        await pilot.pause(0.5)
        await pilot.click("#composer")
        await pilot.press("a", "s", "k", "enter")
        await pilot.pause(0.7)

        await pilot.press("down", "down", "enter")
        await pilot.pause()
        panel = app.query_one(AskUserPanel)
        assert panel.awaiting_freeform is True
        assert app.query_one(Composer).has_focus is True

        await pilot.press("slash", "t", "m", "p", "enter")
        await pilot.pause(0.8)

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
        await pilot.pause(0.5)
        await pilot.click("#composer")
        await pilot.press("a", "s", "k", "enter")
        await pilot.pause(0.7)

        panel = app.query_one(AskUserPanel)
        assert panel.display is True
        assert "How should the plugin continue?" in str(panel.render())

        await pilot.press("1")
        await pilot.pause(0.8)

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
async def test_plan_confirmation_renders_only_plan_and_collects_revision_feedback(
    tmp_path: Path,
) -> None:
    application = _plan_confirmation_application(tmp_path)
    await application.start()
    assert application.plugins is not None
    assert (await application.plugins.enable("dev.ouro.plan_mode")).status == "active"
    app = OuroTuiApp(application=application, approval_mode="auto")

    async with app.run_test(size=(100, 42)) as pilot:
        await pilot.pause(0.5)
        await pilot.press("slash", *"plan", "space", "o", "n", "enter")
        await pilot.pause(0.5)
        await pilot.click("#composer")
        await pilot.press("p", "l", "a", "n", "enter")

        panel = app.query_one(PlanConfirmationPanel)
        for _ in range(30):
            if panel.display:
                break
            await pilot.pause(0.1)
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
        assert "↑/↓ or ←/→ move · Enter choose · 1-3 direct" in controls
        assert panel.selected_action == "accept"

        await pilot.press("down", "enter")
        await pilot.pause()

        composer = app.query_one(Composer)
        assert panel.awaiting_feedback is True
        assert composer.has_focus is True
        assert composer.placeholder == Composer.PLAN_FEEDBACK_PLACEHOLDER

        await pilot.press(*"Add rollback checks.", "enter")
        await pilot.pause(0.8)

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
        assert requested.payload["canonical_tool_id"] == "dev.ouro.plan_mode/tools/exit"
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
    assert (await application.plugins.enable("dev.ouro.plan_mode")).status == "active"
    app = OuroTuiApp(application=application, approval_mode="auto")

    async with app.run_test(size=(100, 42)) as pilot:
        await pilot.pause(0.5)
        await pilot.press("slash", *"plan", "space", "o", "n", "enter")
        await pilot.pause(0.5)
        await pilot.click("#composer")
        await pilot.press("p", "l", "a", "n", "enter")

        panel = app.query_one(PlanConfirmationPanel)
        for _ in range(30):
            if panel.display:
                break
            await pilot.pause(0.1)
        await pilot.press("enter")
        await pilot.pause()

        controls = panel.query_one("#plan-confirmation-controls", Static).render().plain
        assert "Choose an implementation approach" in controls
        assert "Safe rollout" in controls
        assert "Direct rollout" in controls

        await pilot.press("2")
        await pilot.pause(0.8)

        assert app._projection is not None
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
async def test_long_plan_keeps_confirmation_controls_visible_and_scrolls_independently(
    tmp_path: Path,
) -> None:
    long_plan = "# Long plan\n\n" + "\n".join(
        f"{index}. Verify implementation step {index}." for index in range(1, 50)
    )
    application = _plan_confirmation_application(tmp_path, plan=long_plan)
    await application.start()
    assert application.plugins is not None
    assert (await application.plugins.enable("dev.ouro.plan_mode")).status == "active"
    app = OuroTuiApp(application=application, approval_mode="auto")

    async with app.run_test(size=(100, 42)) as pilot:
        await pilot.pause(0.5)
        await pilot.press("slash", *"plan", "space", "o", "n", "enter")
        await pilot.pause(0.5)
        await pilot.click("#composer")
        await pilot.press("p", "l", "a", "n", "enter")

        panel = app.query_one(PlanConfirmationPanel)
        for _ in range(30):
            if panel.display:
                break
            await pilot.pause(0.1)
        document = panel.query_one("#plan-confirmation-document", VerticalScroll)
        controls = panel.query_one("#plan-confirmation-controls", Static)

        assert document.max_scroll_y > 0
        assert document.styles.scrollbar_size_vertical == 0
        assert controls.region.y >= document.region.bottom
        assert controls.region.bottom <= panel.content_region.bottom
        assert "Accept" in controls.render().plain
        assert "Revise" in controls.render().plain
        assert "Reject" in controls.render().plain

        await pilot.press("pagedown")
        await pilot.pause()
        plan_scroll_y = document.scroll_y
        assert plan_scroll_y > 0

        await pilot.press("down")
        await pilot.pause()
        assert panel.selected_action == "revise"
        assert document.scroll_y == plan_scroll_y


@pytest.mark.asyncio
async def test_action_and_help_menus_share_flat_chrome_with_composer(tmp_path: Path) -> None:
    """Ctrl+P and /help menus use the same flat chrome as the composer.

    Every menu (Ctrl+P actions, Ctrl+S/M/pickers via ChoiceScreen, and /help)
    must share one visual idiom with the input box and command menu: a
    low-contrast panel with a muted right edge and no tall accent border.
    The choice menu carries the theme accent as a segmented left rail —
    the title segment is themed, the body segments are lowlight — so the
    header reads as emphasis and the body as structure. This guards the
    unified style against regression to the old bordered look.
    """
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause(0.5)
        composer = app.query_one(Composer)

        # Ctrl+P opens the shared ChoiceScreen action palette.
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert isinstance(app.screen, ChoiceScreen)
        panel = app.screen.query_one("#choice-panel")

        assert panel.styles.background == composer.styles.background
        # The panel itself has no left rail — it is segmented onto the children.
        assert panel.styles.border_left[0] == ""
        assert panel.styles.border_right[0] == "solid"
        assert panel.styles.border_top[0] == ""
        assert panel.styles.border_bottom[0] == ""
        # Title segment carries the theme accent; body segments carry the
        # lowlight, giving the "bright header + quiet body" contrast.
        assert app.screen.query_one("#choice-title").styles.border_left[0] == "solid"
        assert app.screen.query_one("#choice-list").styles.border_left[0] == "solid"
        assert app.screen.query_one("#choice-hint").styles.border_left[0] == "solid"
        # No inherited tall accent border on the stack children.
        assert app.screen.query_one("#choice-title").styles.border_top[0] == ""
        assert app.screen.query_one("#choice-list").styles.border_bottom[0] == ""
        # The interaction hint renders below the menu, not clipped to zero
        # lines by padding eating the fixed height.
        choice_hint = app.screen.query_one("#choice-hint")
        assert choice_hint.region.height >= 1
        assert "Enter" in choice_hint.render().plain

        await pilot.press("escape")
        await pilot.pause()

        # /help opens the HelpScreen with the same flat chrome.
        await pilot.press("slash", *"help", "enter")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        help_panel = app.screen.query_one("#help-panel")
        assert help_panel.styles.background == composer.styles.background
        assert help_panel.styles.border_left[0] == "solid"
        assert help_panel.styles.border_right[0] == "solid"
        assert help_panel.styles.border_top[0] == ""
        assert help_panel.styles.border_bottom[0] == ""
        # The help text still surfaces the shortcuts and slash commands.
        rendered = help_panel.render().plain
        assert "OURO shortcuts" in rendered
        assert "Ctrl+Enter / Ctrl+J" in rendered
        assert "↑ / ↓" in rendered
        assert "Alt+M" not in rendered
        assert "Alt+P" in rendered
        assert "plugins" in rendered
        assert "Ctrl+M" not in rendered
        assert "Ctrl+P" in rendered
        assert "Ctrl+H" in rendered
        assert "Ctrl+↑" in rendered
        assert "Ctrl+↓" in rendered
        assert "Ctrl+←" in rendered
        assert "Ctrl+→" in rendered
        assert "Built-in slash commands" in rendered
        assert "/actions" in rendered
        assert "/skill <skill> [task]" in rendered
        assert "/thoughts [expand|collapse]" in rendered
        assert "/unqueue <queued-message>" in rendered
        assert "/help" in rendered
        assert "/config" not in rendered
        assert "/tools" not in rendered
        assert "/mcp" not in rendered
        assert "/skills" not in rendered


@pytest.mark.asyncio
async def test_notifications_use_themed_chrome_at_top_right(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 34), notifications=True) as pilot:
        await pilot.pause(0.5)
        app.notify("Approval: auto", timeout=30)
        await pilot.pause()

        rack = app.screen.query_one("#textual-toastrack")
        toast = rack.query_one("Toast")
        composer = app.query_one(Composer)

        assert rack.styles.dock == "top"
        assert toast.styles.background == composer.styles.background
        assert toast.styles.border_left == (
            "solid",
            Color.parse(app.tui_theme.accent),
        )
        assert toast.styles.border_right == ("solid", Color.parse("#34373d"))
        assert toast.styles.border_top[0] == ""
        assert toast.styles.border_bottom[0] == ""
        top_inset = toast.region.y - app.screen.region.y
        right_inset = app.screen.region.right - toast.region.right
        assert top_inset == right_inset == 1
        assert toast.region.x >= app.screen.region.width // 2


@pytest.mark.asyncio
async def test_noop_abort_does_not_notify_when_ready_status_is_visible(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 32), notifications=True) as pilot:
        await pilot.pause(0.5)
        await pilot.press("ctrl+c")
        await pilot.pause()

        assert "Ready" in app.query_one(ActivityIndicator).render().plain
        assert not list(app.screen.query_one("#textual-toastrack").query("Toast"))


@pytest.mark.asyncio
async def test_action_menu_switches_theme_across_css_and_rich_and_persists(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with app.run_test(size=(100, 32), notifications=True) as pilot:
        await pilot.pause(0.5)
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
        assert "Theme color" in str(actions.get_option_at_index(1).prompt)
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, ChoiceScreen)
        theme_screen = app.screen
        assert app.screen.query_one("#choice-title", Static).render().plain == "THEME COLOR"
        theme_options = app.screen.query_one(OptionList)
        assert theme_options.option_count == len(TUI_THEMES)
        assert all(choice.description == "" for choice in app.screen._choices)
        assert "original" in {choice.value for choice in app.screen._choices}
        assert TUI_THEMES["original"].accent == "#e8e8e8"
        assert TUI_THEMES["original"].accent_bright == "#ffffff"

        await pilot.press("down")
        await pilot.pause()

        harbor = TUI_THEMES["harbor"]
        assert app.tui_theme == harbor
        preview_preferences = TuiPreferencesStore(tmp_path).load()
        assert preview_preferences.theme == "original"
        assert len(preview_preferences.recent_actions) == 1
        preview = app.query_one(BrandHeader).render()
        assert preview.get_style_at_offset(preview.plain.index("OURO")).foreground == Color.parse(
            harbor.accent
        )
        assert app.query_one(Composer).styles.border_left[1] == Color.parse(harbor.accent)
        context = status_line.query_one("#context-detail", Static).render()
        assert context.get_style_at_offset(context.plain.index("█")).foreground == Color.parse(
            harbor.accent
        )

        await pilot.press("down", "enter")
        await pilot.pause(0.2)

        sage = TUI_THEMES["sage"]
        assert app.screen is theme_screen
        assert isinstance(app.screen, ChoiceScreen)
        assert (
            next(choice for choice in app.screen._choices if choice.value == "sage").marker
            == SELECTED_CHOICE_MARKER
        )
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
        assert app.tui_theme == TUI_THEMES["iris"]
        assert TuiPreferencesStore(tmp_path).load().theme == "sage"

        await pilot.press("escape")
        await pilot.pause()
        assert app.tui_theme == sage
        assert not list(app.screen.query_one("#textual-toastrack").query("Toast"))

    stored = (tmp_path / "config" / "tui.json").read_text(encoding="utf-8")
    assert '"theme": "sage"' in stored

    restarted = OuroTuiApp(home=tmp_path, application=_application(tmp_path))
    assert restarted.tui_theme == TUI_THEMES["sage"]


@pytest.mark.asyncio
async def test_action_menu_updates_thought_display_without_closing_picker(
    tmp_path: Path,
) -> None:
    preferences = TuiPreferencesStore(tmp_path)
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with app.run_test(size=(100, 32), notifications=True) as pilot:
        await pilot.pause(0.5)
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
        assert choices["collapse"].marker == SELECTED_CHOICE_MARKER
        assert choices["expand"].marker == ""

        thought_screen.query_one("#choice-filter", Input).value = "Expand"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen is thought_screen
        choices = {choice.value: choice for choice in thought_screen._choices}
        assert choices["expand"].marker == SELECTED_CHOICE_MARKER
        assert choices["collapse"].marker == ""
        assert preferences.load().reasoning_expanded is True
        assert not list(app.screen.query_one("#textual-toastrack").query("Toast"))

        await pilot.press("escape")


@pytest.mark.asyncio
async def test_theme_preview_is_reverted_when_picker_is_cancelled(tmp_path: Path) -> None:
    preferences = TuiPreferencesStore(tmp_path)
    preferences.save(TuiPreferences(theme="iris"))
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause(0.5)
        await pilot.press("ctrl+p", "enter")
        await pilot.pause()
        assert app.screen.query_one(OptionList).highlighted == 3

        await pilot.press("down")
        await pilot.pause()

        assert app.tui_theme == TUI_THEMES["rosewood"]

        await pilot.press("escape")
        await pilot.pause(0.2)

        assert app.tui_theme == TUI_THEMES["iris"]
        assert app.query_one(Composer).styles.border_left[1] == Color.parse(
            TUI_THEMES["iris"].accent
        )
        assert preferences.load() == TuiPreferences(
            theme="iris",
            recent_actions=("__ouro_theme__",),
        )
