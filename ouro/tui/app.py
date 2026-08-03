from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from pydantic import JsonValue
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import TextArea

from ouro.application.models import AvailableModel
from ouro.application.session_models import SessionCatalogEntry
from ouro.application.supervisor_models import RuntimeActivitySnapshot
from ouro.core.approvals import SessionApprovalMode
from ouro.core.events import EventRecord
from ouro.core.interactions import SessionInteractionMode
from ouro.core.messages import MessageRecord
from ouro.core.results import RunResult
from ouro.deployment import SessionBackend
from ouro.local_application import LocalApplication
from ouro.tui.commands import matching_commands, resolve_command
from ouro.tui.controller import (
    LoadedSession,
    TuiBootstrap,
    TuiController,
    TuiLifecycleStatus,
)
from ouro.tui.question_forms import project_question_form
from ouro.tui.screens import Choice, ChoiceScreen, HelpScreen
from ouro.tui.state import PendingInteraction, TuiSessionProjection
from ouro.tui.widgets import (
    AskUserPanel,
    BrandHeader,
    CommandPopup,
    Composer,
    ConversationView,
    InteractionBar,
    StatusLine,
)


class RuntimeEventsReady(Message):
    """Signal that the UI-owned Runtime event buffer has a prefix to render."""


class LifecycleStatusReceived(Message):
    def __init__(self, status: TuiLifecycleStatus) -> None:
        super().__init__()
        self.status = status


class OuroTuiApp(App[int]):
    TITLE = "OURO"
    SUB_TITLE = "Agent"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen {
        background: transparent;
        color: #d6d9df;
        layout: vertical;
    }

    #brand-header {
        height: 1;
        dock: top;
        background: transparent;
        color: #d6d9df;
    }

    #main {
        height: 1fr;
        background: transparent;
    }

    #conversation {
        height: 1fr;
        padding: 1 2 0 2;
        background: transparent;
        scrollbar-color: #505967;
        scrollbar-color-hover: #6a7482;
        scrollbar-background: transparent;
        scrollbar-size-vertical: 1;
    }

    .welcome {
        width: 72;
        max-width: 100%;
        margin: 2 0;
        padding: 1 2;
        color: #aeb5bf;
        background: transparent;
        border-left: thick #d9a441;
    }

    .turn {
        width: 100%;
        height: auto;
        margin-bottom: 1;
        background: transparent;
    }

    .user-turn {
        padding: 0 1;
    }

    .assistant-turn {
        padding: 0 1;
    }

    .turn-label {
        height: 1;
        color: #d9a441;
        text-style: bold;
    }

    .assistant-markdown {
        height: auto;
        background: transparent;
        padding: 0;
    }

    .reasoning-view {
        height: auto;
        margin: 0 0 1 1;
        padding: 0 1;
        color: #8f9aa8;
        border-left: solid #505967;
        background: transparent;
    }

    .reasoning-label {
        height: 1;
        color: #788492;
        text-style: bold;
    }

    .reasoning-markdown {
        height: auto;
        padding: 0;
        color: #8f9aa8;
        text-style: dim italic;
        background: transparent;
    }

    Markdown, MarkdownBlock, MarkdownFence, MarkdownTable {
        background: transparent;
    }

    .tool-row {
        height: 1;
        margin: 0 0 1 2;
        color: #aeb5bf;
        background: transparent;
    }

    .notice {
        height: auto;
        margin: 0 1 1 1;
        padding: 0 1;
        color: #d9a441;
        background: transparent;
    }

    .error-notice {
        color: #e08a8a;
    }

    #interaction-bar {
        height: auto;
        max-height: 10;
        margin: 0 1;
        padding: 1 2;
        color: #d6d9df;
        background: transparent;
        border-top: solid #7c6536;
        border-right: none;
        border-bottom: solid #7c6536;
        border-left: none;
    }

    #interaction-bar:focus {
        border-top: solid #b68a3b;
        border-bottom: solid #b68a3b;
    }

    #ask-user-panel {
        height: auto;
        max-height: 18;
        margin: 0 1;
        padding: 1 2;
        color: #d6d9df;
        background: transparent;
        border-top: solid #7c6536;
        border-right: none;
        border-bottom: solid #7c6536;
        border-left: none;
    }

    #ask-user-panel:focus {
        border-top: solid #b68a3b;
        border-bottom: solid #b68a3b;
    }

    #command-popup {
        height: auto;
        max-height: 15;
        margin: 0 1;
        padding: 1 2;
        color: #d6d9df;
        background: #17191d;
        border: tall #7c6536;
    }

    #composer {
        height: auto;
        min-height: 3;
        max-height: 9;
        margin: 0 1;
        padding: 0 1;
        color: #e5e7eb;
        background: transparent;
        border-top: solid #505967;
        border-right: none;
        border-bottom: solid #505967;
        border-left: none;
        scrollbar-background: transparent;
        scrollbar-color: #505967;
        scrollbar-size-vertical: 1;
    }

    #composer:focus {
        border-top: solid #b68a3b;
        border-bottom: solid #b68a3b;
    }

    StatusLine {
        height: 1;
        padding: 0 2;
        color: #6f7a87;
        background: transparent;
    }

    #key-hints { width: 1fr; }
    #status-detail { width: auto; text-align: right; }

    ChoiceScreen, HelpScreen {
        align: center middle;
        background: #000000 45%;
    }

    #choice-title {
        width: 72;
        max-width: 90%;
        height: 3;
        padding: 1 2 0 2;
        color: #d9a441;
        text-style: bold;
        background: #17191d;
        border-top: tall #7c6536;
    }

    #choice-filter {
        width: 72;
        max-width: 90%;
        background: #17191d;
        border: none;
        padding: 0 2;
    }

    #choice-list {
        width: 72;
        max-width: 90%;
        height: 16;
        background: #17191d;
        border-bottom: tall #7c6536;
        padding: 0 1 1 1;
    }

    #help-panel {
        width: 72;
        max-width: 90%;
        height: auto;
        padding: 2 3;
        background: #17191d;
        border: tall #7c6536;
    }
    """

    BINDINGS = [
        Binding("ctrl+n", "new_session", "New session", show=False),
        Binding("ctrl+s", "sessions", "Sessions", show=False),
        Binding("ctrl+m", "models", "Model", show=False),
        Binding("ctrl+p", "actions", "Actions", show=False),
        Binding("ctrl+c", "abort_or_quit", "Abort / quit", show=False, priority=True),
        Binding("question_mark", "help", "Help", show=False),
    ]

    def __init__(
        self,
        *,
        home: str | Path | None = None,
        system_prompt: str | None = None,
        session_backend: SessionBackend | None = None,
        approval_mode: SessionApprovalMode = "inherit",
        interaction_mode: SessionInteractionMode = "interactive",
        resume_session_id: str | None = None,
        application: LocalApplication | None = None,
        close_application: bool = True,
    ) -> None:
        super().__init__()
        self._controller = TuiController(
            self._publish_runtime_event,
            self._publish_lifecycle_status,
            home=home,
            system_prompt=system_prompt,
            session_backend=session_backend,
            approval_mode=approval_mode,
            interaction_mode=interaction_mode,
            application=application,
            close_application=close_application,
        )
        self._resume_session_id = resume_session_id
        self._projection: TuiSessionProjection | None = None
        self._models: tuple[AvailableModel, ...] = ()
        self._sessions: tuple[SessionCatalogEntry, ...] = ()
        self._active_model = "starting"
        self._active_title = "New session"
        self._host_closing = False
        self._waiting_for_session_event = False
        self._dismissed_command_text: str | None = None
        self._pending_runtime_events: list[EventRecord] = []
        self._runtime_events_ready_posted = False

    def compose(self) -> ComposeResult:
        yield BrandHeader()
        with Vertical(id="main"):
            yield ConversationView()
            yield CommandPopup()
            yield AskUserPanel()
            yield InteractionBar()
            yield Composer()
            yield StatusLine()

    def on_mount(self) -> None:
        self.query_one(BrandHeader).set_activity("starting")
        self.query_one(Composer).disabled = True
        self.run_worker(self._start(), name="tui-start", exclusive=True)

    async def on_unmount(self) -> None:
        self._host_closing = True
        await self._controller.close()

    async def _start(self) -> None:
        conversation = self.query_one(ConversationView)
        try:
            bootstrap = await self._controller.start()
        except Exception as exc:  # noqa: BLE001 - surface boundary failures in the Host.
            self.query_one(BrandHeader).set_activity("failed")
            await conversation.add_notice(f"OURO could not start: {exc}", error=True)
            self.query_one(StatusLine).set_detail("startup failed")
            return
        self._apply_bootstrap(bootstrap)
        if self._resume_session_id is not None:
            try:
                await self._open_session(self._resume_session_id)
            except Exception as exc:  # noqa: BLE001 - invalid persisted selection is recoverable.
                await conversation.add_notice(f"Could not open session: {exc}", error=True)
                await conversation.show_welcome()
        else:
            await conversation.show_welcome()
        self._apply_system_status(bootstrap)
        composer = self.query_one(Composer)
        composer.disabled = False
        composer.focus()

    def _apply_bootstrap(self, bootstrap: TuiBootstrap) -> None:
        self._models = bootstrap.models
        self._sessions = bootstrap.sessions
        self._active_model = (
            bootstrap.current_model.display_name or bootstrap.current_model.model_id
        )
        header = self.query_one(BrandHeader)
        header.set_context(model=self._active_model)
        header.set_activity("idle")
        diagnostics = bootstrap.startup_report.diagnostics
        if diagnostics:
            self.query_one(StatusLine).set_detail(f"{len(diagnostics)} startup diagnostic(s)")

    def _publish_runtime_event(self, event: EventRecord) -> None:
        if self._host_closing:
            return
        self._pending_runtime_events.append(event)
        if not self._runtime_events_ready_posted:
            self._runtime_events_ready_posted = self.post_message(RuntimeEventsReady())

    def _publish_lifecycle_status(self, status: TuiLifecycleStatus) -> None:
        if not self._host_closing:
            self.post_message(LifecycleStatusReceived(status))

    def on_lifecycle_status_received(self, message: LifecycleStatusReceived) -> None:
        self.query_one(BrandHeader).set_activity(message.status.activity)
        self.query_one(StatusLine).set_detail(message.status.detail)

    def _apply_system_status(self, bootstrap: TuiBootstrap) -> None:
        self.query_one(ConversationView).set_system_status(bootstrap.system_status)

    async def on_runtime_events_ready(self, _message: RuntimeEventsReady) -> None:
        self._runtime_events_ready_posted = False
        await self._flush_pending_runtime_events()

    async def _flush_pending_runtime_events(self) -> None:
        if not self._pending_runtime_events:
            return
        events = tuple(self._pending_runtime_events)
        self._pending_runtime_events.clear()
        await self._apply_runtime_events(events)

    async def _apply_runtime_events(
        self,
        events: Sequence[EventRecord],
        *,
        allow_new_projection: bool = False,
    ) -> None:
        if not events:
            return
        projection = self._projection
        if projection is None:
            if not self._waiting_for_session_event and not allow_new_projection:
                return
            projection = TuiSessionProjection(events[0].session_id)
            self._projection = projection
            composer = self.query_one(Composer)
            composer.disabled = False
            composer.focus()

        diagnostics: list[str] = []
        latest_update = None
        interaction_changed = False
        assistant_visible = bool(projection.assistant_text)
        conversation = self.query_one(ConversationView)
        for event in events:
            if event.event_type == "run_started":
                assistant_visible = False
            previous_tools = projection.activity.tool_calls
            update = projection.apply(event)
            if update is None:
                continue
            latest_update = update
            if update.reasoning_delta:
                await conversation.append_reasoning(update.reasoning_delta)
            if update.assistant_delta:
                await conversation.append_assistant(update.assistant_delta)
                assistant_visible = True
            if update.round_break and assistant_visible:
                await conversation.append_assistant("\n\n")
            if update.activity.tool_calls != previous_tools:
                await conversation.sync_tools(update.activity.tool_calls)
            interaction_changed = interaction_changed or update.interaction_changed
            if update.diagnostic:
                diagnostics.append(update.diagnostic)

        if latest_update is None:
            return
        if latest_update.activity.activity != "streaming_reasoning":
            conversation.finish_reasoning()
        self._sync_activity(latest_update.activity)
        if interaction_changed:
            self._sync_interaction(projection.pending_interaction)
        for diagnostic in diagnostics:
            await conversation.add_notice(diagnostic, error=True)

    async def on_composer_submitted(self, message: Composer.Submitted) -> None:
        interaction = self._projection.pending_interaction if self._projection else None
        if interaction is not None and interaction.kind == "approval":
            self.query_one(InteractionBar).focus()
            return
        if interaction is not None and interaction.kind == "question":
            panel = self.query_one(AskUserPanel)
            if panel.display and panel.awaiting_freeform:
                panel.accept_freeform(message.text)
            return
        await self.query_one(ConversationView).add_user(message.text)
        self.query_one(BrandHeader).set_activity("queued")
        self.query_one(StatusLine).set_detail(
            "Submitting…" if self._projection is None else "Queueing follow-up…"
        )
        if self._projection is None:
            self.query_one(Composer).disabled = True
            self._waiting_for_session_event = True
        self._run_prompt(message.text)

    @work(group="run")
    async def _run_prompt(self, prompt: str) -> None:
        try:
            result = await self._controller.submit(prompt)
            await self._apply_run_result(result)
        except Exception as exc:  # noqa: BLE001 - providers and tools fail across this boundary.
            self.query_one(BrandHeader).set_activity("failed")
            self.query_one(StatusLine).set_detail("Run failed")
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
        finally:
            self._waiting_for_session_event = False
            composer = self.query_one(Composer)
            interaction = self._projection.pending_interaction if self._projection else None
            composer.disabled = interaction is not None and interaction.kind == "approval"
            if interaction is not None and interaction.kind == "approval":
                self.query_one(InteractionBar).focus()
            elif interaction is not None and interaction.kind == "question":
                panel = self.query_one(AskUserPanel)
                if panel.awaiting_freeform:
                    composer.focus()
                else:
                    panel.focus()
            else:
                composer.focus()

    async def _apply_run_result(self, result: RunResult) -> None:
        await self._flush_pending_runtime_events()
        await self._apply_runtime_events(result.events, allow_new_projection=True)
        if self._projection is None or self._projection.session_id != result.session.session_id:
            self._projection = TuiSessionProjection(result.session.session_id)
        if result.final_text and not self._projection.assistant_text:
            self._projection.assistant_text = result.final_text
            await self.query_one(ConversationView).append_assistant(result.final_text)
        self._active_title = result.session.title
        self.query_one(BrandHeader).set_context(session_title=self._active_title)

    def on_text_area_changed(self, message: TextArea.Changed) -> None:
        if message.text_area is not self.query_one(Composer):
            return
        value = message.text_area.text
        popup = self.query_one(CommandPopup)
        ask_user = self.query_one(AskUserPanel)
        if ask_user.display and ask_user.awaiting_freeform:
            popup.hide()
            return
        if value == self._dismissed_command_text:
            return
        self._dismissed_command_text = None
        if (
            value.startswith("/")
            and "\n" not in value
            and not any(character.isspace() for character in value)
        ):
            popup.show_commands(matching_commands(value))
        else:
            popup.hide()

    async def on_composer_command_submitted(self, message: Composer.CommandSubmitted) -> None:
        popup = self.query_one(CommandPopup)
        composer = self.query_one(Composer)
        interaction = self._projection.pending_interaction if self._projection else None
        ask_user = self.query_one(AskUserPanel)
        if (
            interaction is not None
            and interaction.kind == "question"
            and ask_user.display
            and ask_user.awaiting_freeform
        ):
            composer.clear()
            popup.hide()
            self._dismissed_command_text = None
            ask_user.accept_freeform(message.text)
            return
        selected = popup.selected_command
        value = selected.name if selected is not None else message.text
        composer.clear()
        popup.hide()
        self._dismissed_command_text = None
        await self._dispatch_command(value)
        if self.is_running:
            composer.focus()

    def on_composer_command_navigated(self, message: Composer.CommandNavigated) -> None:
        self.query_one(CommandPopup).navigate(message.offset)

    def on_composer_command_dismissed(self, _message: Composer.CommandDismissed) -> None:
        composer = self.query_one(Composer)
        self._dismissed_command_text = composer.text
        self.query_one(CommandPopup).hide()

    @work(exclusive=True, group="interaction")
    async def _resolve_approval(
        self,
        interaction: PendingInteraction,
        approved: bool,
    ) -> None:
        approval_id = interaction.request_id
        if approval_id is None:
            await self.query_one(ConversationView).add_notice(
                "Approval request has no identifier",
                error=True,
            )
            self.query_one(InteractionBar).resume()
            return
        try:
            result = await self._controller.resolve_approval(
                run_id=interaction.run_id,
                approval_id=approval_id,
                approved=approved,
            )
            await self._apply_run_result(result)
        except Exception as exc:  # noqa: BLE001 - application validation is user-visible.
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            self.query_one(InteractionBar).resume()

    @work(exclusive=True, group="interaction")
    async def _answer_question(self, interaction: PendingInteraction, answer: JsonValue) -> None:
        self.query_one(Composer).disabled = True
        try:
            result = await self._controller.answer(run_id=interaction.run_id, answer=answer)
            await self._apply_run_result(result)
        except Exception as exc:  # noqa: BLE001 - application validation is user-visible.
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            self.query_one(AskUserPanel).resume()
        finally:
            composer = self.query_one(Composer)
            composer.disabled = False
            current = self._projection.pending_interaction if self._projection else None
            panel = self.query_one(AskUserPanel)
            if current is not None and current.kind == "question" and panel.display:
                if panel.awaiting_freeform:
                    composer.focus()
                else:
                    panel.focus()
            else:
                composer.focus()

    def on_interaction_bar_decision(self, message: InteractionBar.Decision) -> None:
        self._resolve_approval(message.interaction, message.approved)

    def on_ask_user_panel_completed(self, message: AskUserPanel.Completed) -> None:
        interaction = self._projection.pending_interaction if self._projection else None
        if interaction is not None and interaction.kind == "question":
            self._answer_question(interaction, message.answer)

    def on_ask_user_panel_freeform_requested(
        self,
        _message: AskUserPanel.FreeformRequested,
    ) -> None:
        composer = self.query_one(Composer)
        composer.placeholder = "Type another answer…"
        composer.focus()

    def _sync_activity(self, activity: RuntimeActivitySnapshot) -> None:
        self.query_one(BrandHeader).set_activity(activity.activity)
        detail = f"{len(activity.tool_calls)} tool(s)" if activity.tool_calls else ""
        self.query_one(StatusLine).set_detail(detail)

    def _sync_interaction(self, interaction: PendingInteraction | None) -> None:
        interaction_bar = self.query_one(InteractionBar)
        ask_user = self.query_one(AskUserPanel)
        composer = self.query_one(Composer)
        if interaction is not None and interaction.kind == "question":
            interaction_bar.show_interaction(None)
            ask_user.show_request(project_question_form(interaction.tool_name, interaction.request))
            composer.disabled = False
            composer.placeholder = "Choose an option above…"
            return
        ask_user.hide_panel()
        interaction_bar.show_interaction(interaction)
        composer.placeholder = "Message OURO…"
        if interaction is not None and interaction.kind == "approval":
            composer.disabled = True
            interaction_bar.focus()
        else:
            composer.disabled = False
            composer.focus()

    async def _dispatch_command(self, value: str) -> None:
        command, argument = resolve_command(value)
        if command is None:
            command_name = value.partition(" ")[0]
            await self.query_one(ConversationView).add_notice(
                f"Unknown command: {command_name}. Use /help to see available commands."
            )
            return
        if command.name == "/quit":
            self.exit(0)
        elif command.name == "/new":
            await self._new_session()
        elif command.name == "/sessions":
            self.action_sessions()
        elif command.name == "/model":
            if argument:
                await self._select_model(argument)
            else:
                self.action_models()
        elif command.name == "/clear":
            await self.query_one(ConversationView).show_welcome()
        elif command.name == "/abort":
            self.action_abort_or_quit()
        elif command.name == "/help":
            self.action_help()

    async def _new_session(self) -> None:
        if self._has_active_run():
            self.notify("Abort the active run before starting a new session", severity="warning")
            return
        self._controller.new_session()
        self._projection = None
        self._active_title = "New session"
        self.query_one(BrandHeader).set_context(session_title=self._active_title)
        self.query_one(BrandHeader).set_activity("idle")
        self._sync_interaction(None)
        await self.query_one(ConversationView).show_welcome()

    def action_new_session(self) -> None:
        self.run_worker(self._new_session(), exclusive=True, group="navigation")

    def action_sessions(self) -> None:
        if self._has_active_run():
            self.notify("Abort the active run before switching sessions", severity="warning")
            return
        self._choose_session()

    @work(exclusive=True, group="navigation")
    async def _choose_session(self) -> None:
        try:
            self._sessions = self._controller.sessions()
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        if not self._sessions:
            self.notify("No saved sessions yet")
            return
        choices = tuple(
            Choice(
                value=item.session_id,
                label=("★ " if item.pinned else "") + item.title,
                description=(
                    f"{item.status} · {item.current_model or 'default model'} · {item.session_id}"
                ),
            )
            for item in self._sessions
        )
        selected = await self.push_screen_wait(
            ChoiceScreen(title="SESSIONS", placeholder="Filter sessions…", choices=choices)
        )
        if selected is not None:
            await self._open_session(selected)

    async def _open_session(self, session_id: str) -> None:
        loaded = self._controller.load_session(session_id)
        await self.query_one(ConversationView).load_messages(loaded.messages)
        self._restore_projection(loaded)
        session = loaded.detail.session
        self._active_title = session.title
        self._active_model = session.current_model or self._active_model
        self.query_one(BrandHeader).set_context(
            session_title=self._active_title,
            model=self._active_model,
        )

    def _restore_projection(self, loaded: LoadedSession) -> None:
        projection = TuiSessionProjection(loaded.detail.session.session_id)
        projection.restore(loaded.events)
        self._projection = projection
        self._sync_activity(projection.activity)
        self._sync_interaction(projection.pending_interaction)
        self.run_worker(
            self.query_one(ConversationView).sync_tools(projection.activity.tool_calls),
            group="restore-tools",
            exclusive=True,
        )

    def action_models(self) -> None:
        self._choose_model()

    @work(exclusive=True, group="navigation")
    async def _choose_model(self) -> None:
        self._models = self._controller.models()
        choices = tuple(
            Choice(
                value=model.key,
                label=model.display_name or model.model_id,
                description=f"{model.provider_id} · {model.endpoint_id} · {model.key}",
            )
            for model in self._models
        )
        selected = await self.push_screen_wait(
            ChoiceScreen(title="MODELS", placeholder="Filter models…", choices=choices)
        )
        if selected is not None:
            await self._select_model(selected)

    async def _select_model(self, selector: str) -> None:
        try:
            model = self._controller.select_model(selector)
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        self._active_model = model.display_name or model.model_id
        self.query_one(BrandHeader).set_context(model=self._active_model)
        self.notify(f"Model: {self._active_model}")

    def action_actions(self) -> None:
        self._choose_action()

    @work(exclusive=True, group="navigation")
    async def _choose_action(self) -> None:
        action_commands = tuple(
            command
            for name in ("/new", "/sessions", "/model", "/abort", "/help")
            if (command := resolve_command(name)[0]) is not None
        )
        choices = tuple(
            Choice(command.name, command.title, command.description) for command in action_commands
        )
        selected = await self.push_screen_wait(
            ChoiceScreen(title="ACTIONS", placeholder="Type an action…", choices=choices)
        )
        if selected is not None:
            await self._dispatch_command(selected)

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_abort_or_quit(self) -> None:
        projection = self._projection
        if projection is not None and projection.activity.status in {
            "queued",
            "running",
            "waiting_user",
            "waiting_approval",
            "waiting_child",
        }:
            self._abort_run()
            return
        self.exit(0)

    def _has_active_run(self) -> bool:
        return self._projection is not None and self._projection.activity.status in {
            "queued",
            "running",
            "waiting_user",
            "waiting_approval",
            "waiting_child",
        }

    @work(exclusive=True, group="abort")
    async def _abort_run(self) -> None:
        try:
            await self._controller.abort()
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)


def visible_transcript(messages: tuple[MessageRecord, ...]) -> tuple[MessageRecord, ...]:
    """Expose the transcript rule for focused unit tests."""

    return tuple(
        message
        for message in messages
        if message.role in {"user", "assistant"} and bool(message.text_content)
    )
