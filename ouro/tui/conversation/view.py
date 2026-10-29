"""Conversation surface, transcript selection and scroll coordination."""

from __future__ import annotations

import asyncio
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Iterable
from datetime import datetime

from textual import events, work
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.geometry import Size
from textual.layout import DockArrangeResult
from textual.message import Message

from ouro.application.contracts.agents import AgentTreeNode
from ouro.application.contracts.execution import RuntimeToolCallActivity
from ouro.core.compaction import CompactionRecord
from ouro.core.messages import MessageRecord, TextContent
from ouro.tui.conversation.history import ConversationHistory
from ouro.tui.conversation.history_models import ConversationHistoryState, TranscriptEntry
from ouro.tui.conversation.reasoning import ReasoningLabel
from ouro.tui.conversation.tools import ToolActivityRow, _tool_activity_text, is_terminal_tool
from ouro.tui.conversation.transcript import (
    _compaction_presentation,
    _input_transcript_entry,
    project_transcript_entries,
)
from ouro.tui.conversation.turn import AssistantTurn
from ouro.tui.selection import RangeSelectionOnly
from ouro.tui.state import RunPresentation
from ouro.tui.view_models import TuiSystemStatus

_TRANSCRIPT_BACKGROUND_ENTRY_THRESHOLD = 64


_TRANSCRIPT_BACKGROUND_TEXT_THRESHOLD = 8 * 1024


type PreparedTranscript = tuple[tuple[TranscriptEntry, ...], frozenset[str]]


def _requires_background_transcript_projection(
    messages: tuple[MessageRecord, ...],
    compactions: tuple[CompactionRecord, ...],
    runs: tuple[RunPresentation, ...],
) -> bool:
    """Keep cheap loads synchronous; move only frame-sized work off the UI loop."""

    entry_count = len(messages) + len(compactions) + len(runs)
    text_size = 0
    for message in messages:
        entry_count += len(message.tool_calls)
        text_size += len(message.reasoning_content)
        text_size += sum(
            len(block.text) for block in message.content if isinstance(block, TextContent)
        )
        if (
            entry_count >= _TRANSCRIPT_BACKGROUND_ENTRY_THRESHOLD
            or text_size >= _TRANSCRIPT_BACKGROUND_TEXT_THRESHOLD
        ):
            return True
    entry_count += sum(len(run.requests) for run in runs)
    return entry_count >= _TRANSCRIPT_BACKGROUND_ENTRY_THRESHOLD


class _ConversationTranscript(RangeSelectionOnly, Vertical):
    """Own transcript layout without turning triple-click into select-all."""


class ConversationView(RangeSelectionOnly, VerticalScroll):
    class HistoryTopReached(Message):
        """Upward navigation reached the start of the loaded history."""

    def __init__(
        self,
        *,
        reasoning_expanded: bool = True,
        welcome_state_changed: Callable[[bool], None] | None = None,
        system_status_changed: Callable[[TuiSystemStatus], None] | None = None,
        show_scaffold_messages: bool = False,
        collapse_traces: bool = True,
    ) -> None:
        super().__init__(id="conversation")
        self._follow_tail = True
        self._reasoning_expanded = reasoning_expanded
        self.show_scaffold_messages = show_scaffold_messages
        self._welcome_state_changed = welcome_state_changed
        self._system_status_changed = system_status_changed
        self._history = ConversationHistory(
            _tool_activity_text,
            reasoning_expanded=reasoning_expanded,
            collapse_traces=collapse_traces,
        )
        self._live_turn = AssistantTurn(reasoning_expanded=reasoning_expanded)
        self._live_turn.display = False
        self._transcript = _ConversationTranscript(
            self._history,
            self._live_turn,
            id="conversation-transcript",
        )
        self._system_status = TuiSystemStatus()
        self._showing_welcome = False
        self._restored_scroll_y: float | None = None
        self._assistant: AssistantTurn | None = None
        self._tools: dict[str, ToolActivityRow] = {}
        self._current_run_id: str | None = None
        self._presented_run_ids: set[str] = set()
        self._presented_compaction_ids: set[str] = set()
        self._visible_scaffold_delegation_id: str | None = None

    def compose(self) -> ComposeResult:
        yield self._transcript

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        if not event.ctrl and not event.shift and self.scroll_y <= 0:
            self.release_anchor()
            self.post_message(self.HistoryTopReached())
            event.stop()

    def action_page_up(self) -> None:
        if self.scroll_y <= 0:
            self.release_anchor()
            self.post_message(self.HistoryTopReached())
        else:
            self.scroll_page_up(animate=False)

    def action_page_down(self) -> None:
        self.scroll_page_down(animate=False)

    def action_scroll_up(self) -> None:
        if self.scroll_y <= 0:
            self.release_anchor()
            self.post_message(self.HistoryTopReached())
        else:
            super().action_scroll_up()

    async def on_conversation_history_prefix_rendered(
        self, message: ConversationHistory.PrefixRendered
    ) -> None:
        if not self._follow_tail:
            await self.stop_animation("scroll_y", complete=False)
            self._restore_scroll_y(self.scroll_y + message.line_delta)

    def prepend_transcript(self, prepared: PreparedTranscript) -> None:
        entries, run_ids = prepared
        self._presented_run_ids.update(run_ids)
        self._presented_compaction_ids.update(
            entry.compaction.compaction_id
            for entry in entries
            if entry.compaction and entry.compaction.compaction_id is not None
        )
        self._history.prepend(entries)

    def arrange(self, size: Size, optimal: bool = False) -> DockArrangeResult:
        """Resolve the scroll position from the viewport used by this layout pass.

        Dynamic siblings change the conversation's height. Scroll correction must use
        this pass's ``size`` so child placement and scrolling reach the screen as one
        coherent frame. A later resize callback is already one frame too late.
        """

        arrangement = super().arrange(size, optimal)
        if not optimal:
            max_scroll_y = max(0, arrangement.total_region.bottom - size.height)
            scroll_y: float
            if self._follow_tail:
                scroll_y = max_scroll_y
            elif self._restored_scroll_y is not None:
                scroll_y = min(self._restored_scroll_y, max_scroll_y)
                if self._history.render_complete:
                    # Reflow may measure several viewport sizes in one frame.
                    # Keep the original offset until the final layout is published.
                    self.call_after_refresh(self._finish_scroll_restore, self._restored_scroll_y)
            else:
                scroll_y = min(self.scroll_y, max_scroll_y)
            self.set_scroll(None, scroll_y)
            self.scroll_target_y = scroll_y
        return arrangement

    def _finish_scroll_restore(self, scroll_y: float) -> None:
        if self._restored_scroll_y == scroll_y and self._history.render_complete:
            self._restored_scroll_y = None

    def anchor(self, anchor: bool = True) -> None:
        self._follow_tail = anchor
        self._restored_scroll_y = None
        if anchor:
            self.scroll_end(immediate=True, animate=False)

    def release_anchor(self) -> None:
        self._follow_tail = False
        self._restored_scroll_y = None
        self.scroll_target_y = self.scroll_y

    def held_scroll_y(self) -> float | None:
        """Return a restorable offset only while the user is reviewing history."""

        if self._follow_tail:
            return None
        return self._restored_scroll_y if self._restored_scroll_y is not None else self.scroll_y

    def _restore_scroll_y(self, scroll_y: float) -> None:
        """Resolve a saved offset against the destination transcript's first layout."""

        self._follow_tail = False
        self._restored_scroll_y = max(0.0, scroll_y)
        # Cached strips can repaint before layout runs. Publish their saved offset
        # now; arrange() will clamp it against the destination's actual geometry.
        self.set_scroll(None, self._restored_scroll_y)
        self.refresh(layout=True)

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        # Layout rounds animated offsets to cells; detect the visible edge once.
        if (
            not self._follow_tail
            and self._restored_scroll_y is None
            and round(old_value) > 0
            and round(new_value) == 0
        ):
            self.post_message(self.HistoryTopReached())
        if (
            not self._follow_tail
            and self._restored_scroll_y is None
            and new_value >= self.max_scroll_y
        ):
            self._follow_tail = True

    def on_mount(self) -> None:
        self.anchor()

    async def show_welcome(self) -> None:
        await self._clear_live_transcript()
        self._history.replace(())
        self.set_welcome_mode(True)
        self._assistant = None
        self._tools.clear()
        self._current_run_id = None
        self._presented_run_ids.clear()
        self._presented_compaction_ids.clear()
        self.anchor()

    def set_system_status(self, status: TuiSystemStatus) -> None:
        self._system_status = status
        if self._system_status_changed is not None:
            self._system_status_changed(status)

    @property
    def showing_welcome(self) -> bool:
        return self._showing_welcome

    def set_welcome_mode(self, showing: bool) -> None:
        if self._showing_welcome == showing:
            return
        self._showing_welcome = showing
        if self._welcome_state_changed is not None:
            self._welcome_state_changed(showing)

    async def set_reasoning_expanded(self, expanded: bool) -> None:
        revision = self._history.content_revision
        self._reasoning_expanded = expanded
        if self._assistant is not None:
            self._assistant.set_reasoning_expanded(expanded)
        delta = await self._history.set_reasoning_expanded(
            expanded, above_line=round(self.scroll_y)
        )
        with self.app.batch_update():
            if delta and revision == self._history.content_revision and not self._follow_tail:
                self.scroll_to(y=max(0.0, self.scroll_y + delta), animate=False, immediate=True)

    def on_reasoning_label_toggle_requested(
        self,
        message: ReasoningLabel.ToggleRequested,
    ) -> None:
        turn = self._assistant
        if turn is not None:
            with self.app.batch_update():
                expanding = not turn.reasoning_expanded
                if expanding:
                    self.release_anchor()
                turn.set_reasoning_expanded(expanding)
        message.stop()

    def on_conversation_history_toggle_reasoning_requested(
        self,
        message: ConversationHistory.ToggleReasoningRequested,
    ) -> None:
        self._toggle_reasoning(message.entry_index)
        message.stop()

    @work(exclusive=True, group="history-disclosure")
    async def _toggle_reasoning(self, entry_index: int) -> None:
        if not self._history.is_reasoning_expanded(entry_index):
            self.release_anchor()
        await self._history.toggle_reasoning(entry_index)

    def on_conversation_history_toggle_trace_requested(
        self,
        message: ConversationHistory.ToggleTraceRequested,
    ) -> None:
        self._toggle_trace(message.entry_index)
        message.stop()

    @work(exclusive=True, group="history-disclosure")
    async def _toggle_trace(self, entry_index: int) -> None:
        scroll_y = self.scroll_y
        revision = self._history.content_revision
        delta = await self._history.toggle_trace(entry_index)
        if revision == self._history.content_revision and self.scroll_y == scroll_y:
            with self.app.batch_update():
                self._restore_scroll_y(scroll_y + delta)

    async def set_collapse_traces(self, collapsed: bool) -> None:
        revision = self._history.content_revision
        delta = await self._history.set_collapse_traces(collapsed, above_line=round(self.scroll_y))
        with self.app.batch_update():
            if revision == self._history.content_revision and not self._follow_tail:
                self._restore_scroll_y(self.scroll_y + delta)

    def take_history_state(self) -> ConversationHistoryState | None:
        """Detach the persisted transcript; live Run widgets are reconstructed."""

        return self._history.take_state()

    async def load_messages(
        self,
        messages: Iterable[MessageRecord],
        compactions: Iterable[CompactionRecord] = (),
        runs: Iterable[RunPresentation] = (),
        *,
        mutable_tool_calls: Iterable[tuple[str | None, str | None, str]] = (),
        visible_scaffold_delegation_id: str | None = None,
        held_scroll_y: float | None = None,
        reusable_history: ConversationHistoryState | None = None,
    ) -> None:
        # All session kinds share the same presentation policy, including the
        # user-facing attachment and delegation inputs shown by default.
        materialized = tuple(messages)
        compaction_records = tuple(compactions)
        run_presentations = tuple(runs)
        mutable_tool_call_keys = frozenset(mutable_tool_calls)
        if _requires_background_transcript_projection(
            materialized,
            compaction_records,
            run_presentations,
        ):
            transcript, presented_run_ids = await asyncio.to_thread(
                project_transcript_entries,
                materialized,
                compaction_records,
                run_presentations,
                mutable_tool_call_keys=mutable_tool_call_keys,
                visible_scaffold_delegation_id=visible_scaffold_delegation_id,
                show_scaffold_messages=self.show_scaffold_messages,
            )
        else:
            transcript, presented_run_ids = project_transcript_entries(
                materialized,
                compaction_records,
                run_presentations,
                mutable_tool_call_keys=mutable_tool_call_keys,
                visible_scaffold_delegation_id=visible_scaffold_delegation_id,
                show_scaffold_messages=self.show_scaffold_messages,
            )
        await self.load_transcript(
            (transcript, presented_run_ids),
            visible_scaffold_delegation_id=visible_scaffold_delegation_id,
            held_scroll_y=held_scroll_y,
            reusable_history=reusable_history,
        )

    async def load_transcript(
        self,
        prepared: PreparedTranscript,
        *,
        visible_scaffold_delegation_id: str | None = None,
        held_scroll_y: float | None = None,
        reusable_history: ConversationHistoryState | None = None,
    ) -> None:
        """Commit prepared content without waiting for history layout."""
        self.anchor()
        await self._clear_live_transcript()
        self._assistant = None
        self._tools.clear()
        self._current_run_id = None
        self._presented_run_ids.clear()
        self._presented_compaction_ids.clear()
        self._visible_scaffold_delegation_id = visible_scaffold_delegation_id

        transcript, presented_run_ids = prepared
        self._presented_run_ids.update(presented_run_ids)
        self._presented_compaction_ids.update(
            entry.compaction.compaction_id
            for entry in transcript
            if entry.compaction and entry.compaction.compaction_id is not None
        )

        if not transcript:
            # An empty body may belong to an accepted input awaiting execution.
            # History replacement does not choose which page the user is visiting.
            self._history.replace(())
            self.anchor()
            return

        self.set_welcome_mode(False)
        await self._history.load(transcript, reusable_state=reusable_history, wait_for_render=False)
        if held_scroll_y is None:
            self.anchor()
        else:
            self._restore_scroll_y(held_scroll_y)

    async def _clear_live_transcript(self) -> None:
        self._assistant = None
        self._live_turn.reset(reasoning_expanded=self._reasoning_expanded)
        live_children = tuple(
            child
            for child in self._transcript.children
            if child is not self._history and child is not self._live_turn
        )
        if live_children:
            # DOM teardown owns the message tasks; cancelling a renderer must
            # not propagate into those tasks while they unmount their children.
            await asyncio.shield(self._transcript.remove_children(live_children))

    def user_turn_summaries(self) -> tuple[str, ...]:
        return self._history.user_turns

    @property
    def presented_run_ids(self) -> frozenset[str]:
        return frozenset(self._presented_run_ids)

    def scroll_to_user_turn(self, index: int) -> None:
        historical_lines = self._history.user_lines
        if 0 <= index < len(historical_lines):
            self.release_anchor()
            self.scroll_to(y=historical_lines[index], animate=False)

    def scroll_to_previous_user_turn(self) -> bool:
        historical_lines = self._history.user_lines
        index = bisect_left(historical_lines, self.scroll_y) - 1
        if index < 0:
            return False
        self.scroll_to_user_turn(index)
        return True

    def scroll_to_next_user_turn(self) -> None:
        historical_lines = self._history.user_lines
        index = bisect_right(historical_lines, self.scroll_y)
        if index < len(historical_lines):
            self.scroll_to_user_turn(index)
        else:
            self.scroll_end(animate=False)

    async def _dismiss_welcome(self) -> None:
        """Transition from the empty welcome layout to the conversation layout."""
        if self._showing_welcome:
            self.set_welcome_mode(False)

    async def add_user(self, text: str, *, created_at: datetime | None = None) -> None:
        await self._dismiss_welcome()
        # A new user turn closes the preceding model round. Persist the live
        # assistant before appending the next chronological history boundary.
        await self.finish_assistant()
        self._history.append(
            TranscriptEntry(
                kind="user",
                text=text,
                created_at=created_at or datetime.now().astimezone(),
            )
        )

    async def add_user_message(self, message: MessageRecord) -> None:
        """Append user or scaffold input using the persisted-history projection."""
        entry = _input_transcript_entry(
            message,
            visible_scaffold_delegation_id=self._visible_scaffold_delegation_id,
            show_scaffold_messages=self.show_scaffold_messages,
        )
        if entry is not None:
            await self._dismiss_welcome()
            await self.finish_assistant()
            self._history.append(entry)

    async def append_assistant(self, text: str) -> None:
        await self._dismiss_welcome()
        turn = self._ensure_turn()
        blocks = await turn.feed(text)
        turn.display = True
        await self._freeze_blocks(turn, blocks)
        turn.commit_frozen_blocks()

    async def append_reasoning(
        self,
        text: str,
        *,
        started_at: datetime | None = None,
    ) -> None:
        await self._dismiss_welcome()
        if self._assistant is not None and self._assistant.has_text:
            # Reasoning after answer text starts a new model round; the previous
            # round must freeze before the new one renders below it.
            await self.finish_assistant()
        turn = self._ensure_turn()
        await turn.append_reasoning(text, started_at=started_at)
        turn.display = True

    def _ensure_turn(self) -> AssistantTurn:
        if self._assistant is None:
            self._live_turn.reset(
                run_id=self._current_run_id,
                after_tool=self._history_ends_with_tool,
                reasoning_expanded=self._reasoning_expanded,
            )
            self._assistant = self._live_turn
        return self._assistant

    async def _freeze_blocks(
        self, turn: AssistantTurn, blocks: Iterable[str], *, final: bool = False
    ) -> None:
        """Transfer live content only when its replacement is ready to display."""
        materialized = tuple(block for block in blocks if block.strip())
        if not materialized and not final:
            return
        if turn.reasoning_pending:
            reasoning = turn.reasoning.text
            if reasoning:
                self._history.append(
                    TranscriptEntry(
                        kind="assistant",
                        reasoning=reasoning,
                        reasoning_seconds=turn.reasoning.duration_seconds,
                        run_id=turn.run_id,
                    ),
                    reasoning_expanded=turn.reasoning_expanded,
                    reasoning_layout=await turn.reasoning.completed_layout(),
                )
        self._history.append_many(
            TranscriptEntry(kind="assistant", text=block, run_id=turn.run_id)
            for block in materialized
        )
        await self._history.wait_until_rendered()
        if turn.reasoning_pending:
            turn.take_reasoning()

    async def finish_assistant(self) -> None:
        """Freeze the live round into the immutable history."""
        turn = self._assistant
        if turn is None:
            return
        await self._freeze_blocks(turn, await turn.close(), final=True)
        self._assistant = None
        turn.reset(reasoning_expanded=self._reasoning_expanded)

    @property
    def _history_ends_with_tool(self) -> bool:
        return self._history.last_entry_kind == "tool"

    def finish_reasoning(self, duration_seconds: float | None = None) -> None:
        if self._assistant is not None:
            self._assistant.finish_reasoning(duration_seconds)

    def begin_model_request(self, *, run_id: str | None) -> None:
        self._current_run_id = run_id

    async def add_run_summary(self, run: RunPresentation) -> None:
        if run.duration_seconds is None or run.run_id in self._presented_run_ids:
            return
        await self._dismiss_welcome()
        if self._assistant is not None and self._assistant.run_id == run.run_id:
            await self.finish_assistant()
        self._history.append(TranscriptEntry(kind="run", run_id=run.run_id, run=run))
        self._presented_run_ids.add(run.run_id)

    async def sync_tools(self, tools: tuple[RuntimeToolCallActivity, ...]) -> None:
        tools = tuple(tool for tool in tools if tool.tool_name)
        if not tools:
            return
        await self._dismiss_welcome()
        historical_tool_ids = self._history.update_tools(
            tools,
            run_id=self._current_run_id,
        )
        self._adopt_streaming_tool_rows(tools)
        has_new_tool = any(
            tool.tool_call_id not in historical_tool_ids and tool.tool_call_id not in self._tools
            for tool in tools
        )
        if has_new_tool:
            # A tool call is a chronological boundary between model rounds.
            # Freeze the preceding assistant segment before rendering the tool.
            await self.finish_assistant()
        new_rows: list[ToolActivityRow] = []
        for tool in tools:
            if tool.tool_call_id in historical_tool_ids:
                continue
            row = self._tools.get(tool.tool_call_id)
            if row is None:
                row = ToolActivityRow(tool, run_id=self._current_run_id)
                self._tools[tool.tool_call_id] = row
                new_rows.append(row)
            else:
                row.apply_tool(tool)
        if new_rows:
            await self._transcript.mount_all(new_rows, before=self._live_turn)

        # Keep a tool batch live until every row is terminal. Archiving rows one
        # by one would move a later-completing row ahead of an earlier active row
        # because the immutable history always precedes the live widget region.
        if self._tools and all(is_terminal_tool(row.tool) for row in self._tools.values()):
            terminal_rows = tuple(self._tools.values())
            self._history.append_many(
                TranscriptEntry(
                    kind="tool",
                    run_id=row.run_id,
                    tool=row.tool,
                    agent=row.agent,
                )
                for row in terminal_rows
            )
            await self._history.wait_until_rendered()
            self._tools.clear()
            await asyncio.shield(self._transcript.remove_children(terminal_rows))

    async def apply_tool_result(self, message: MessageRecord) -> None:
        result = message.tool_result
        if result is None:
            return
        batch_id = message.outcome_ref.identity.batch_id if message.outcome_ref else None
        row = self._tools.get(result.tool_call_id)
        tool = (
            row.tool
            if row is not None
            else self._history.tool_activity(
                message.run_id,
                batch_id,
                result.tool_call_id,
            )
        )
        if tool is not None:
            timing = message.tool_execution
            await self.sync_tools(
                (
                    tool.model_copy(
                        update={
                            "status": "failed" if result.is_error else "completed",
                            "started_at": timing.started_at if timing else tool.started_at,
                            "ended_at": timing.ended_at if timing else message.created_at,
                        }
                    ),
                )
            )

    def _adopt_streaming_tool_rows(
        self,
        tools: tuple[RuntimeToolCallActivity, ...],
    ) -> None:
        """Re-key provisional stream rows when canonical tool ids arrive."""

        provisional_by_index = {
            row.tool.source_index: tool_call_id
            for tool_call_id, row in self._tools.items()
            if row.tool.status == "streaming"
        }
        for tool in tools:
            if tool.tool_call_id in self._tools:
                continue
            provisional_id = provisional_by_index.get(tool.source_index)
            if provisional_id is None:
                continue
            row = self._tools.pop(provisional_id)
            row.apply_tool(tool)
            self._tools[tool.tool_call_id] = row

    def sync_agents(self, agents: Iterable[AgentTreeNode]) -> None:
        materialized = tuple(agents)
        if not materialized:
            return
        self._history.update_agents(materialized)
        agents_by_call = {
            (agent.parent_run_id, agent.parent_batch_id, agent.parent_tool_call_id): agent
            for agent in materialized
            if agent.parent_tool_call_id is not None
        }
        for row in self._tools.values():
            if row.run_id is None:
                continue
            agent = agents_by_call.get((row.run_id, row.tool.batch_id, row.tool_call_id))
            if agent is not None:
                row.apply_agent(agent)

    async def add_notice(self, message: str, *, error: bool = False) -> None:
        await self._dismiss_welcome()
        # Notices are immutable transcript boundaries as well. Freezing any
        # partial answer keeps presentation order identical to event order.
        await self.finish_assistant()
        self._history.append(TranscriptEntry(kind="notice", text=message, error=error))

    async def add_command_output(self, output: str) -> None:
        """Present short feedback as a notice and multiline content in a message frame."""
        if len(output.splitlines()) <= 1:
            await self.add_notice(output)
            return
        await self._dismiss_welcome()
        await self.finish_assistant()
        self._history.append(TranscriptEntry(kind="command", text=output))

    async def add_compaction(
        self,
        *,
        compaction_id: str | None = None,
        covered_message_count: int,
        tokens_before: int | None,
        tokens_after: int | None,
        duration_seconds: float | None,
    ) -> None:
        if compaction_id is not None:
            if compaction_id in self._presented_compaction_ids:
                return
            self._presented_compaction_ids.add(compaction_id)
        await self._dismiss_welcome()
        await self.finish_assistant()
        self._history.append(
            TranscriptEntry(
                kind="compaction",
                compaction=_compaction_presentation(
                    compaction_id=compaction_id,
                    covered_message_count=covered_message_count,
                    tokens_before=tokens_before,
                    tokens_after=tokens_after,
                    duration_seconds=duration_seconds,
                ),
            )
        )
