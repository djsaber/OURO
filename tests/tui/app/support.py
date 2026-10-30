"""Shared application factories and scenario-specific wait helpers."""

from __future__ import annotations

import asyncio
from pathlib import Path

from textual.widgets import Static

from ouro.application.runtime.environment import RuntimeEnvironment
from ouro.application.runtime.service import RuntimeService
from ouro.core.adapter import RuntimeAdapter
from ouro.core.agents import (
    AgentCatalogEntry,
    AgentCatalogSnapshot,
    AgentDefinition,
    DelegationRecord,
    ToolSelection,
)
from ouro.core.messages import MessageRecord
from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.core.reasoning import ReasoningCapability, ReasoningRequest
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tools import ToolSource
from ouro.local_application import LocalApplication
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore
from ouro.skills import SkillMetadata
from ouro.tools import (
    Tool,
    ToolDefinition,
    ToolInputRequest,
    ToolRegistry,
    ToolResult,
    builtin_tools,
)
from ouro.tui.app import OuroTuiApp
from ouro.tui.composer.input import Composer
from ouro.tui.composer.surface import WelcomePanel
from ouro.tui.interactions.bar import InteractionBar
from tests.tui.app.providers import (
    BlockingReasoningProvider,
    BlockingStreamProvider,
    MultiChildStreamingProvider,
    QueuedSubagentApprovalProvider,
    ReasoningBurstThenBlockProvider,
    RetryThenBlockProvider,
    TuiPlanConfirmationProvider,
)
from tests.tui.helpers import wait_until as _wait_until


async def _wait_for_active_session(
    app: OuroTuiApp,
    pilot,
    session_id: str,
    *,
    timeout: float = 5.0,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while (
        app._controller.session_id != session_id
        or app._projection is None
        or app._projection.session_id != session_id
        or app.query_one(Composer).disabled
    ):
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"Timed out waiting for active Session {session_id}")
        await asyncio.sleep(0.01)
    await pilot._wait_for_screen()


async def _wait_for_tool_approval(
    app: OuroTuiApp,
    pilot,
    *,
    tool_call_id: str,
    pending_count: int | None = None,
) -> InteractionBar:
    panel = app.query_one(InteractionBar)

    def ready() -> bool:
        projection = app._projection
        pending = app._interaction
        return (
            pending is not None
            and pending.kind == "approval"
            and pending.tool_call_id == tool_call_id
            and (
                pending_count is None
                or (
                    projection is not None and len(projection.pending_interactions) == pending_count
                )
            )
            and panel._interaction == pending
            and panel.display
            and not panel.disabled
            and panel.has_focus
        )

    await _wait_until(ready, pilot, description=f"interactive approval for {tool_call_id}")
    # The projection can advance before the panel binds the new request, and
    # binding can precede layout. The controller owns checkpoint admission.
    await app.screen.wait_for_refresh()
    return panel


async def _wait_for_welcome_render(
    app: OuroTuiApp,
    pilot,
    *,
    contains: str,
    timeout: float = 15.0,
) -> WelcomePanel:
    def ready() -> bool:
        matches = list(app.query(WelcomePanel))
        welcome = matches[0] if matches else None
        return (
            welcome is not None
            and welcome.has_class("welcome-page")
            and welcome.page.region.width > 0
            and contains in _welcome_plain_text(welcome)
        )

    await _wait_until(ready, pilot, description=f"welcome content: {contains}", timeout=timeout)
    return app.query_one(WelcomePanel)


def _welcome_plain_text(welcome: WelcomePanel) -> str:
    return "\n".join(getattr(widget.render(), "plain", "") for widget in welcome.query(Static))


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
        environment=RuntimeEnvironment.local(tmp_path),
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
  "engines": {"ouro": ">=0.4.0", "plugin_api": "^5.0.0"},
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
                handler=lambda invocation, context: ToolResult(
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
            handler=lambda invocation, context: ToolResult(
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
        environment=RuntimeEnvironment.local(tmp_path),
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
        environment=RuntimeEnvironment.local(tmp_path),
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
                ),
            ),
        ),
        tools=ToolRegistry(),
        default_workspace=tmp_path,
        environment=RuntimeEnvironment.local(tmp_path),
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
        environment=RuntimeEnvironment.local(tmp_path),
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
        environment=RuntimeEnvironment.local(tmp_path),
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
        environment=RuntimeEnvironment.local(tmp_path),
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
            handler=lambda invocation, context: ToolResult(
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
        environment=RuntimeEnvironment.local(tmp_path),
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
        environment=RuntimeEnvironment.local(tmp_path),
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
            agent_catalog=AgentCatalogSnapshot(entries=(AgentCatalogEntry(definition=definition),)),
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
                handler=lambda _invocation, context: ToolInputRequest(
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
        environment=RuntimeEnvironment.local(tmp_path),
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
        environment=RuntimeEnvironment.local(tmp_path),
    )
    return LocalApplication.from_runtime(runtime)
