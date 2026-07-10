from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ouro.application.events import RuntimeEventRecorder, RuntimeEventSink
from ouro.application.followups import FollowUpQueue
from ouro.application.runs import (
    ACTIVE_RUN_STATUSES,
    RunLifecycle,
    clear_pending_metadata,
    find_active_run,
    find_run,
    maybe_active_run,
)
from ouro.core.adapter import RuntimeAdapter
from ouro.core.base import utc_now
from ouro.core.cancellation import AbortSignal
from ouro.core.context import ContextBuilder
from ouro.core.events import EventRecord
from ouro.core.hooks import HookContext, HookRegistry
from ouro.core.inner_loop import InnerAgentLoop, InnerLoopPorts
from ouro.core.messages import MessageRecord
from ouro.core.results import RunResult
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tools import PermissionMode
from ouro.prompts import DEFAULT_SYSTEM_PROMPT
from ouro.sessions import SessionService, SessionSnapshot, SessionStore
from ouro.tools import ToolExecutionOutcome, ToolExecutor, ToolInvocation, ToolRegistry, ToolResult


class RuntimeService:
    def __init__(
        self,
        *,
        store: SessionStore,
        adapter: RuntimeAdapter,
        tools: ToolRegistry,
        hooks: HookRegistry | None = None,
        context_builder: ContextBuilder | None = None,
        max_model_rounds: int = 8,
        system_prompt: str | None = None,
        event_sink: RuntimeEventSink | None = None,
        permission_overrides: Mapping[str, PermissionMode] | None = None,
    ) -> None:
        self.store = store
        self.sessions = SessionService(store)
        self.follow_ups = FollowUpQueue(store)
        self.adapter = adapter
        self.tools = tools
        self.tool_executor = ToolExecutor(tools, permission_overrides=permission_overrides)
        self.hooks = hooks or HookRegistry()
        self.event_recorder = RuntimeEventRecorder(
            store=store,
            hooks=self.hooks,
            event_sink=event_sink,
        )
        self.run_lifecycle = RunLifecycle(store=store, events=self.event_recorder)
        self.context_builder = context_builder or ContextBuilder()
        self.max_model_rounds = max_model_rounds
        self.system_prompt = DEFAULT_SYSTEM_PROMPT if system_prompt is None else system_prompt
        self._abort_signals: dict[str, AbortSignal] = {}

    def add_event_sink(self, event_sink: RuntimeEventSink) -> None:
        self.event_recorder.add_event_sink(event_sink)

    async def run(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        title: str | None = None,
        _drain_followups: bool = True,
        _queued_follow_up_id: str | None = None,
    ) -> RunResult:
        if session_id is None:
            session = self.sessions.create(title=title or _title_from_prompt(prompt))
            existing_messages: list[MessageRecord] = []
            existing_events: list[EventRecord] = []
        else:
            snapshot = self.store.load_session(
                session_id,
                recover=not self._is_session_run_locked(session_id),
            )
            session = snapshot.session
            existing_messages = snapshot.messages
            existing_events = snapshot.events
            active_run = maybe_active_run(snapshot.runs)
            if active_run is not None:
                queued_session, queued_events = await self._queue_follow_up(
                    snapshot=snapshot,
                    active_run=active_run,
                    prompt=prompt,
                    title=title,
                )
                return RunResult(session=queued_session, run=active_run, events=queued_events)
        sequence = len(existing_events)
        emitted: list[EventRecord] = []

        run = RunRecord(
            session_id=session.session_id,
            status="running",
            model=self.adapter.model.model_id,
        )
        run_lock = self._acquire_session_run_lock(session.session_id, run.run_id)
        if run_lock is None:
            snapshot = self.store.load_session(session.session_id, recover=False)
            active_run = maybe_active_run(snapshot.runs)
            if active_run is not None:
                queued_session, queued_events = await self._queue_follow_up(
                    snapshot=snapshot,
                    active_run=active_run,
                    prompt=prompt,
                    title=title,
                )
                return RunResult(session=queued_session, run=active_run, events=queued_events)
            raise RuntimeError(f"session is already locked: {session.session_id}")
        session = session.model_copy(
            update={"status": "running", "last_run_id": run.run_id, "updated_at": utc_now()}
        )
        self.store.save_session(session)
        self.store.save_run(run)
        self.store.rebuild_index()

        sequence = await self.event_recorder.emit(
            emitted,
            sequence,
            session_id=session.session_id,
            run_id=run.run_id,
            event_type="run_started",
            payload={"trigger": "user"},
        )

        user_message = self.run_lifecycle.append_user_message(
            session=session,
            run=run,
            prompt=prompt,
            sequence=len(existing_messages) + 1,
            metadata={"queued_follow_up_id": _queued_follow_up_id}
            if _queued_follow_up_id
            else None,
        )
        if _queued_follow_up_id:
            latest = self.store.load_session(session.session_id, recover=False)
            self.follow_ups.acknowledge(latest, _queued_follow_up_id)
        sequence = await self.event_recorder.emit(
            emitted,
            sequence,
            session_id=session.session_id,
            run_id=run.run_id,
            event_type="user_message_added",
            payload={"message_id": user_message.message_id},
        )

        abort_signal = self._activate_abort_signal(session.session_id, run.run_id)
        try:
            result = await self._continue_model_loop(
                session=session,
                run=run,
                messages=[*existing_messages, user_message],
                sequence=sequence,
                emitted=emitted,
                abort_signal=abort_signal,
            )
        finally:
            self._release_abort_signal(run.run_id, abort_signal)
            self._release_session_run_lock(run_lock)
        self._finish_run_hooks(result.run)
        if _drain_followups and result.run.status == "completed":
            await self._drain_follow_up_queue(result.session.session_id)
        return result

    async def _continue_model_loop(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        messages: list[MessageRecord],
        sequence: int,
        emitted: list[EventRecord],
        final_parts: list[str] | None = None,
        tool_results: list[ToolResult] | None = None,
        abort_signal: AbortSignal,
    ) -> RunResult:
        loop = InnerAgentLoop(
            adapter=self.adapter,
            tools=self.tools,
            hooks=self.hooks,
            context_builder=self.context_builder,
            max_model_rounds=self.max_model_rounds,
            system_prompt=self.system_prompt,
            ports=InnerLoopPorts(
                emit=self.event_recorder.emit,
                record_hook_failures=self.event_recorder.record_hook_failures,
                handle_tool_call=self._handle_tool_call,
                record_usage=self.run_lifecycle.record_usage,
                checkpoint=self.run_lifecycle.checkpoint,
                aborted_result=self._aborted_result,
                save_waiting_state=self.run_lifecycle.save_waiting_state,
                complete_run=self.run_lifecycle.complete,
                fail_run=self.run_lifecycle.fail,
            ),
        )
        return await loop.run(
            session=session,
            run=run,
            messages=messages,
            sequence=sequence,
            emitted=emitted,
            final_parts=final_parts,
            tool_results=tool_results,
            abort_signal=abort_signal,
        )

    async def abort_run(
        self,
        *,
        session_id: str,
        run_id: str | None = None,
        reason: str = "User aborted",
    ) -> RunResult:
        snapshot = self.store.load_session(session_id, recover=False)
        run = find_run(snapshot.runs, run_id) if run_id else find_active_run(snapshot.runs)
        if run.status not in ACTIVE_RUN_STATUSES:
            return RunResult(session=snapshot.session, run=run)

        sequence = len(snapshot.events)
        emitted: list[EventRecord] = []
        metadata = clear_pending_metadata(run.metadata)
        metadata["abort_reason"] = reason
        run = run.model_copy(
            update={
                "status": "aborted",
                "ended_at": utc_now(),
                "pending_approval_id": None,
                "abort_requested": True,
                "error": reason,
                "metadata": metadata,
            }
        )
        session = snapshot.session.model_copy(
            update={
                "status": "idle",
                "updated_at": utc_now(),
                "last_run_id": run.run_id,
            }
        )
        self.store.save_run(run)
        self.store.save_session(session)
        self.store.rebuild_index()
        sequence = await self.event_recorder.emit(
            emitted,
            sequence,
            session_id=session.session_id,
            run_id=run.run_id,
            event_type="run_aborted",
            payload={"reason": reason},
        )
        del sequence
        signal = self._abort_signals.get(run.run_id)
        if signal is not None:
            signal.abort(reason)
        self._finish_run_hooks(run)
        return RunResult(session=session, run=run, events=emitted)

    async def _queue_follow_up(
        self,
        *,
        snapshot: SessionSnapshot,
        active_run: RunRecord,
        prompt: str,
        title: str | None,
    ) -> tuple[SessionRecord, list[EventRecord]]:
        session, queue_size = self.follow_ups.enqueue(
            snapshot=snapshot,
            prompt=prompt,
            title=title,
        )
        emitted: list[EventRecord] = []
        sequence = await self.event_recorder.emit(
            emitted,
            len(snapshot.events),
            session_id=session.session_id,
            run_id=active_run.run_id,
            event_type="follow_up_input_queued",
            payload={"queue_size": queue_size},
        )
        del sequence
        return session, emitted

    async def _drain_follow_up_queue(self, session_id: str) -> None:
        while True:
            snapshot = self.store.load_session(
                session_id,
                recover=not self._is_session_run_locked(session_id),
            )
            if maybe_active_run(snapshot.runs) is not None:
                return
            queued = self.follow_ups.peek(snapshot)
            if queued is None:
                return
            already_persisted = any(
                message.metadata.get("queued_follow_up_id") == queued.queue_id
                for message in snapshot.messages
            )
            if already_persisted:
                self.follow_ups.acknowledge(snapshot, queued.queue_id)
                continue
            result = await self.run(
                queued.prompt,
                session_id=session_id,
                title=queued.title,
                _drain_followups=False,
                _queued_follow_up_id=queued.queue_id,
            )
            if result.run.status != "completed":
                return

    def _acquire_session_run_lock(self, session_id: str, run_id: str):
        return self.store.acquire_session_lock(session_id, run_id)

    def _release_session_run_lock(self, lock: object | None) -> None:
        release = getattr(lock, "release", None)
        if callable(release):
            release()

    def _is_session_run_locked(self, session_id: str) -> bool:
        return self.store.is_session_locked(session_id)

    async def approve_tool(
        self,
        *,
        session_id: str,
        run_id: str | None = None,
        approved: bool,
        reason: str = "User rejected",
        arguments_override: dict[str, object] | None = None,
    ) -> RunResult:
        snapshot = self.store.load_session(session_id)
        session = snapshot.session
        run = find_run(snapshot.runs, run_id) if run_id else find_active_run(snapshot.runs)
        if run.status != "waiting_approval":
            raise ValueError(f"run is not waiting for approval: {run.status}")
        invocation = _pending_invocation(run)
        run_lock = self._acquire_session_run_lock(session.session_id, run.run_id)
        if run_lock is None:
            raise RuntimeError(f"run is already active: {run.run_id}")
        try:
            sequence = len(snapshot.events)
            emitted: list[EventRecord] = []
            tool_results: list[ToolResult] = []
            abort_signal: AbortSignal | None = None

            sequence = await self.event_recorder.emit(
                emitted,
                sequence,
                session_id=session.session_id,
                run_id=run.run_id,
                event_type="approval_resolved",
                payload={
                    "approval_id": run.pending_approval_id,
                    "approved": approved,
                    "reason": None if approved else reason,
                },
            )
            if approved:
                sequence = await self.event_recorder.emit(
                    emitted,
                    sequence,
                    session_id=session.session_id,
                    run_id=run.run_id,
                    event_type="tool_call_started",
                    payload={"tool_name": invocation.tool_name},
                )
                abort_signal = self._activate_abort_signal(session.session_id, run.run_id)
                try:
                    outcome = await self.tool_executor.execute(
                        invocation,
                        approved=True,
                        arguments_override=arguments_override,
                        signal=abort_signal,
                    )
                    abort_result = self._aborted_result(
                        session_id=session.session_id,
                        run_id=run.run_id,
                        final_text="",
                        emitted=emitted,
                        tool_results=tool_results,
                    )
                    if abort_result is not None:
                        self._release_abort_signal(run.run_id, abort_signal)
                        return abort_result
                    if outcome.result is None:
                        raise ValueError("approved tool execution did not produce a result")
                    result = outcome.result
                except Exception:
                    self._release_abort_signal(run.run_id, abort_signal)
                    raise
            else:
                result = self.tool_executor.rejected_result(invocation, reason)

            return await self._resume_with_tool_result(
                snapshot=snapshot,
                session=session,
                run=run,
                emitted=emitted,
                sequence=sequence,
                result=result,
                emit_tool_completed=True,
                abort_signal=abort_signal,
            )
        finally:
            self._release_session_run_lock(run_lock)

    async def answer_user(
        self,
        *,
        session_id: str,
        run_id: str,
        answer: str,
    ) -> RunResult:
        snapshot = self.store.load_session(session_id)
        session = snapshot.session
        run = find_run(snapshot.runs, run_id)
        if run.status != "waiting_user":
            raise ValueError(f"run is not waiting for user input: {run.status}")
        invocation = _pending_invocation(run)
        run_lock = self._acquire_session_run_lock(session.session_id, run.run_id)
        if run_lock is None:
            raise RuntimeError(f"run is already active: {run.run_id}")
        try:
            sequence = len(snapshot.events)
            emitted: list[EventRecord] = []
            result = ToolResult(
                tool_call_id=invocation.tool_call_id,
                tool_name=invocation.tool_name,
                content=answer,
                structured={"answer": answer},
                ended_at=utc_now(),
            )
            sequence = await self.event_recorder.emit(
                emitted,
                sequence,
                session_id=session.session_id,
                run_id=run.run_id,
                event_type="ask_user_resolved",
                payload={"answer": answer},
            )
            return await self._resume_with_tool_result(
                snapshot=snapshot,
                session=session,
                run=run,
                emitted=emitted,
                sequence=sequence,
                result=result,
            )
        finally:
            self._release_session_run_lock(run_lock)

    async def _resume_with_tool_result(
        self,
        *,
        snapshot: SessionSnapshot,
        session: SessionRecord,
        run: RunRecord,
        emitted: list[EventRecord],
        sequence: int,
        result: ToolResult,
        emit_tool_completed: bool = False,
        abort_signal: AbortSignal | None = None,
    ) -> RunResult:
        active_signal = abort_signal
        try:
            sequence = await self._after_tool_call(emitted, sequence, session, run, result)
            message = self.run_lifecycle.append_tool_result_message(
                session=session,
                run=run,
                result=result,
                sequence=len(snapshot.messages) + 1,
            )
            if emit_tool_completed:
                sequence = await self.event_recorder.emit(
                    emitted,
                    sequence,
                    session_id=session.session_id,
                    run_id=run.run_id,
                    event_type="tool_call_completed",
                    payload={"tool_name": result.tool_name, "is_error": result.is_error},
                )
            run = run.model_copy(
                update={
                    "status": "running",
                    "pending_approval_id": None,
                    "metadata": clear_pending_metadata(run.metadata),
                }
            )
            session = session.model_copy(update={"status": "running", "updated_at": utc_now()})
            self.store.save_run(run)
            self.store.save_session(session)
            self.store.rebuild_index()
            active_signal = active_signal or self._activate_abort_signal(
                session.session_id,
                run.run_id,
            )
            resumed = await self._continue_model_loop(
                session=session,
                run=run,
                messages=[*snapshot.messages, message],
                sequence=sequence,
                emitted=emitted,
                tool_results=[result],
                abort_signal=active_signal,
            )
        finally:
            if active_signal is not None:
                self._release_abort_signal(run.run_id, active_signal)
        self._finish_run_hooks(resumed.run)
        if resumed.run.status == "completed":
            await self._drain_follow_up_queue(resumed.session.session_id)
        return resumed

    async def _handle_tool_call(
        self,
        emitted: list[EventRecord],
        sequence: int,
        session: SessionRecord,
        run: RunRecord,
        tool_call: dict[str, Any],
        *,
        message_sequence: int,
        assistant_text: str,
        assistant_reasoning: str,
        abort_signal: AbortSignal,
    ) -> tuple[ToolExecutionOutcome, int, list[MessageRecord]]:
        assistant_message = self.run_lifecycle.append_assistant_tool_call_message(
            session=session,
            run=run,
            tool_call=tool_call,
            text=assistant_text,
            sequence=message_sequence,
            reasoning_content=assistant_reasoning,
        )
        invocation = ToolInvocation(
            session_id=session.session_id,
            run_id=run.run_id,
            tool_call_id=str(tool_call.get("id") or ""),
            tool_name=str(tool_call["name"]),
            arguments=dict(tool_call.get("arguments") or {}),
        )
        sequence = await self.event_recorder.emit(
            emitted,
            sequence,
            session_id=session.session_id,
            run_id=run.run_id,
            event_type="tool_call_requested",
            payload={"tool_name": invocation.tool_name, "arguments": invocation.arguments},
        )
        before_result = await self.hooks.dispatch(
            HookContext(
                hook_name="before_tool_call",
                session_id=session.session_id,
                run_id=run.run_id,
                data={"tool_name": invocation.tool_name, "arguments": invocation.arguments},
            )
        )
        sequence = await self.event_recorder.record_hook_failures(
            emitted,
            sequence,
            before_result.failures,
        )
        outcome = await self.tool_executor.execute(invocation, signal=abort_signal)
        if self._abort_snapshot(session.session_id, run.run_id) is not None:
            return outcome, sequence, [assistant_message]
        if outcome.status == "approval_required":
            sequence = await self.event_recorder.emit(
                emitted,
                sequence,
                session_id=session.session_id,
                run_id=run.run_id,
                event_type="approval_requested",
                payload={
                    "approval_id": outcome.approval.approval_id if outcome.approval else None,
                    "tool_name": invocation.tool_name,
                },
            )
            return outcome, sequence, [assistant_message]
        if outcome.status == "waiting_user":
            sequence = await self.event_recorder.emit(
                emitted,
                sequence,
                session_id=session.session_id,
                run_id=run.run_id,
                event_type="ask_user_requested",
                payload={"question": outcome.question},
            )
            return outcome, sequence, [assistant_message]
        messages = [assistant_message]
        if outcome.result is not None:
            sequence = await self._after_tool_call(emitted, sequence, session, run, outcome.result)
            message = self.run_lifecycle.append_tool_result_message(
                session=session,
                run=run,
                result=outcome.result,
                sequence=message_sequence + 1,
            )
            messages.append(message)
            sequence = await self.event_recorder.emit(
                emitted,
                sequence,
                session_id=session.session_id,
                run_id=run.run_id,
                event_type="tool_call_completed",
                payload={
                    "tool_name": outcome.result.tool_name,
                    "is_error": outcome.result.is_error,
                },
            )
        return outcome, sequence, messages

    async def _after_tool_call(
        self,
        emitted: list[EventRecord],
        sequence: int,
        session: SessionRecord,
        run: RunRecord,
        result: ToolResult,
    ) -> int:
        hook_result = await self.hooks.dispatch(
            HookContext(
                hook_name="after_tool_call",
                session_id=session.session_id,
                run_id=run.run_id,
                data={"result": result.model_dump(mode="json")},
            )
        )
        return await self.event_recorder.record_hook_failures(
            emitted,
            sequence,
            hook_result.failures,
        )

    def _abort_snapshot(self, session_id: str, run_id: str):
        try:
            snapshot = self.store.load_session(session_id, recover=False)
            run = find_run(snapshot.runs, run_id)
        except Exception:  # noqa: BLE001 - abort polling must not break active runs.
            return None
        if run.status == "aborted" or run.abort_requested:
            return snapshot
        return None

    def _aborted_result(
        self,
        *,
        session_id: str,
        run_id: str,
        final_text: str,
        emitted: list[EventRecord],
        tool_results: list[ToolResult],
    ) -> RunResult | None:
        abort_snapshot = self._abort_snapshot(session_id, run_id)
        if abort_snapshot is None:
            return None
        return RunResult(
            session=abort_snapshot.session,
            run=find_run(abort_snapshot.runs, run_id),
            final_text=final_text,
            events=emitted,
            tool_results=tool_results,
        )

    def _activate_abort_signal(self, session_id: str, run_id: str) -> AbortSignal:
        signal = AbortSignal(lambda: self._abort_snapshot(session_id, run_id) is not None)
        self._abort_signals[run_id] = signal
        return signal

    def _release_abort_signal(self, run_id: str, signal: AbortSignal) -> None:
        if self._abort_signals.get(run_id) is signal:
            self._abort_signals.pop(run_id, None)

    def _finish_run_hooks(self, run: RunRecord) -> None:
        if run.status not in ACTIVE_RUN_STATUSES:
            self.hooks.finish_run(run.run_id)


def _title_from_prompt(prompt: str) -> str:
    stripped = " ".join(prompt.split())
    return stripped[:60] if stripped else "New session"


def _pending_invocation(run: RunRecord) -> ToolInvocation:
    raw = run.metadata.get("pending_invocation")
    if not isinstance(raw, dict):
        raise ValueError("run has no pending invocation")
    return ToolInvocation.model_validate(raw)
