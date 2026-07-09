from __future__ import annotations

from typing import Any

from ouro.application.events import RuntimeEventRecorder
from ouro.application.followups import FollowUpQueue
from ouro.application.session import SessionService
from ouro.core.adapter import RuntimeAdapter
from ouro.core.base import new_id, utc_now
from ouro.core.cancellation import AbortSignal
from ouro.core.context import ContextBuilder
from ouro.core.events import EventRecord
from ouro.core.hooks import HookContext, HookRegistry
from ouro.core.inner_loop import InnerAgentLoop, InnerLoopPorts
from ouro.core.messages import MessageRecord
from ouro.core.results import RunResult
from ouro.core.state import RunRecord, SessionRecord
from ouro.persistence import SessionSnapshot, SessionStore
from ouro.providers import Usage
from ouro.tools import ToolExecutionOutcome, ToolExecutor, ToolInvocation, ToolRegistry, ToolResult

_ACTIVE_RUN_STATUSES = {"queued", "running", "waiting_user", "waiting_approval"}


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
    ) -> None:
        self.store = store
        self.sessions = SessionService(store)
        self.follow_ups = FollowUpQueue(store)
        self.adapter = adapter
        self.tools = tools
        self.tool_executor = ToolExecutor(tools)
        self.hooks = hooks or HookRegistry()
        self.event_recorder = RuntimeEventRecorder(store=store, hooks=self.hooks)
        self.context_builder = context_builder or ContextBuilder()
        self.max_model_rounds = max_model_rounds
        self._abort_signals: dict[str, AbortSignal] = {}

    async def run(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        title: str | None = None,
        _drain_followups: bool = True,
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
            active_run = _maybe_active_run(snapshot.runs)
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
            active_run = _maybe_active_run(snapshot.runs)
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

        sequence = await self.event_recorder.emit(
            emitted,
            sequence,
            session_id=session.session_id,
            run_id=run.run_id,
            event_type="run_started",
            payload={"trigger": "user"},
        )

        user_message = MessageRecord.text(
            session_id=session.session_id,
            run_id=run.run_id,
            role="user",
            text=prompt,
            sequence=len(existing_messages) + 1,
        )
        self.store.append_message(user_message)
        self._checkpoint_appended_message(session=session, run=run, message=user_message)
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
            ports=InnerLoopPorts(
                emit=self.event_recorder.emit,
                record_hook_failures=self.event_recorder.record_hook_failures,
                handle_tool_call=self._handle_tool_call,
                record_usage=self._record_provider_usage,
                checkpoint=self._checkpoint_run,
                aborted_result=self._aborted_result,
                save_waiting_state=self._save_waiting_state,
                complete_run=self._complete_run,
                fail_run=self._fail_run,
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
        run = _find_run(snapshot.runs, run_id) if run_id else _find_active_run(snapshot.runs)
        if run.status not in _ACTIVE_RUN_STATUSES:
            return RunResult(session=snapshot.session, run=run)

        sequence = len(snapshot.events)
        emitted: list[EventRecord] = []
        metadata = _clear_pending_metadata(run.metadata)
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
            if _maybe_active_run(snapshot.runs) is not None:
                return
            queued = self.follow_ups.pop(snapshot)
            if queued is None:
                return
            result = await self.run(
                queued.prompt,
                session_id=session_id,
                title=queued.title,
                _drain_followups=False,
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

    def _latest_session_metadata(
        self,
        session_id: str,
        fallback: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            return dict(self.store.load_session(session_id, recover=False).session.metadata)
        except Exception:  # noqa: BLE001 - metadata preservation must not break run finalization.
            return dict(fallback)

    def _latest_run_record(
        self,
        session_id: str,
        run_id: str,
        fallback: RunRecord,
    ) -> RunRecord:
        try:
            snapshot = self.store.load_session(session_id, recover=False)
            return _find_run(snapshot.runs, run_id)
        except Exception:  # noqa: BLE001 - checkpointing must not break active runs.
            return fallback

    def _record_provider_usage(self, run: RunRecord, usage: Usage) -> RunRecord:
        latest = self._latest_run_record(run.session_id, run.run_id, run)
        if latest.status == "aborted" or latest.abort_requested:
            return latest
        run = latest.model_copy(update={"usage": _merge_usage(latest.usage, usage)})
        self.store.save_run(run)
        return run

    def _checkpoint_run(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        messages: list[MessageRecord],
        partial_assistant_text: str,
        event_sequence: int,
    ) -> tuple[SessionRecord, RunRecord]:
        try:
            snapshot = self.store.load_session(session.session_id, recover=False)
            latest_run = _find_run(snapshot.runs, run.run_id)
        except Exception:  # noqa: BLE001 - checkpoints are best-effort during active runs.
            snapshot = None
            latest_run = run

        if latest_run.status == "aborted" or latest_run.abort_requested:
            return (snapshot.session if snapshot is not None else session), latest_run

        persisted_messages = snapshot.messages if snapshot is not None else []
        message_count = max(len(messages), len(persisted_messages), session.message_count)
        last_message_id = session.last_message_id
        if len(persisted_messages) >= len(messages) and persisted_messages:
            last_message_id = persisted_messages[-1].message_id
        elif messages:
            last_message_id = messages[-1].message_id

        metadata = dict(latest_run.metadata)
        metadata.update(run.metadata)
        raw_checkpoint = metadata.get("checkpoint")
        checkpoint = dict(raw_checkpoint) if isinstance(raw_checkpoint, dict) else {}
        checkpoint.update(
            {
                "run_id": run.run_id,
                "message_count": message_count,
                "last_message_id": last_message_id,
                "event_sequence": event_sequence,
                "updated_at": utc_now(),
            }
        )
        if partial_assistant_text:
            checkpoint["partial_assistant_text"] = partial_assistant_text
            checkpoint["partial_assistant_message_id"] = checkpoint.get(
                "partial_assistant_message_id",
                new_id("msg"),
            )
            checkpoint["partial_assistant_sequence"] = message_count + 1
        else:
            checkpoint.pop("partial_assistant_text", None)
            checkpoint.pop("partial_assistant_message_id", None)
            checkpoint.pop("partial_assistant_sequence", None)
        metadata["checkpoint"] = checkpoint

        run = run.model_copy(update={"metadata": metadata})
        session_base = snapshot.session if snapshot is not None else session
        session = session_base.model_copy(
            update={
                "status": session.status,
                "updated_at": utc_now(),
                "last_run_id": run.run_id,
                "last_message_id": last_message_id,
                "message_count": message_count,
                "metadata": dict(session_base.metadata),
            }
        )
        self.store.save_run(run)
        self.store.save_session(session)
        return session, run

    def _save_waiting_state(self, *, session: SessionRecord, run: RunRecord) -> None:
        try:
            snapshot = self.store.load_session(session.session_id, recover=False)
            last_message_id = (
                snapshot.messages[-1].message_id if snapshot.messages else session.last_message_id
            )
            session = snapshot.session.model_copy(
                update={
                    "status": session.status,
                    "updated_at": utc_now(),
                    "last_run_id": run.run_id,
                    "last_message_id": last_message_id,
                    "message_count": len(snapshot.messages),
                    "metadata": dict(snapshot.session.metadata),
                }
            )
        except Exception:  # noqa: BLE001 - waiting state still needs a best-effort save.
            pass
        self.store.save_run(run)
        self.store.save_session(session)

    async def _complete_run(
        self,
        emitted: list[EventRecord],
        sequence: int,
        *,
        session: SessionRecord,
        run: RunRecord,
        messages: list[MessageRecord],
        final_text: str,
        tool_results: list[ToolResult],
    ) -> RunResult:
        if final_text:
            assistant_message = MessageRecord.text(
                session_id=session.session_id,
                run_id=run.run_id,
                role="assistant",
                text=final_text,
                sequence=len(messages) + 1,
            )
            self.store.append_message(assistant_message)
            self._checkpoint_appended_message(session=session, run=run, message=assistant_message)
            messages.append(assistant_message)

        last_message_id = messages[-1].message_id if messages else session.last_message_id
        metadata = self._latest_session_metadata(session.session_id, session.metadata)
        run = run.model_copy(
            update={
                "status": "completed",
                "ended_at": utc_now(),
                "pending_approval_id": None,
                "metadata": _clear_pending_metadata(run.metadata),
            }
        )
        session = session.model_copy(
            update={
                "status": "idle",
                "updated_at": utc_now(),
                "last_message_id": last_message_id,
                "message_count": len(messages),
                "metadata": metadata,
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
            event_type="run_completed",
            payload={"final_text_length": len(final_text)},
        )
        del sequence
        return RunResult(
            session=session,
            run=run,
            final_text=final_text,
            events=emitted,
            tool_results=tool_results,
        )

    async def _fail_run(
        self,
        emitted: list[EventRecord],
        sequence: int,
        *,
        session: SessionRecord,
        run: RunRecord,
        error: str,
        final_text: str,
        tool_results: list[ToolResult],
    ) -> RunResult:
        if final_text:
            snapshot = self.store.load_session(session.session_id, recover=False)
            assistant_message = MessageRecord.text(
                session_id=session.session_id,
                run_id=run.run_id,
                role="assistant",
                text=final_text,
                sequence=len(snapshot.messages) + 1,
            ).model_copy(
                update={
                    "metadata": {
                        "incomplete": True,
                        "failure_reason": error,
                    }
                }
            )
            self.store.append_message(assistant_message)
            self._checkpoint_appended_message(session=session, run=run, message=assistant_message)

        snapshot = self.store.load_session(session.session_id, recover=False)
        last_message_id = (
            snapshot.messages[-1].message_id if snapshot.messages else session.last_message_id
        )
        run = run.model_copy(
            update={
                "status": "failed",
                "ended_at": utc_now(),
                "error": error,
            }
        )
        session = session.model_copy(
            update={
                "status": "failed",
                "updated_at": utc_now(),
                "last_message_id": last_message_id,
                "message_count": len(snapshot.messages),
                "metadata": dict(snapshot.session.metadata),
            }
        )
        self.store.save_run(run)
        self.store.save_session(session)
        sequence = await self.event_recorder.emit(
            emitted,
            sequence,
            session_id=session.session_id,
            run_id=run.run_id,
            event_type="run_failed",
            payload={"error": error},
        )
        del sequence
        return RunResult(
            session=session,
            run=run,
            final_text=final_text,
            events=emitted,
            tool_results=tool_results,
        )

    async def approve_tool(
        self,
        *,
        session_id: str,
        run_id: str,
        approved: bool,
        reason: str = "User rejected",
        arguments_override: dict[str, object] | None = None,
    ) -> RunResult:
        snapshot = self.store.load_session(session_id)
        session = snapshot.session
        run = _find_run(snapshot.runs, run_id)
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

            tool_results.append(result)
            sequence = await self._after_tool_call(emitted, sequence, session, run, result)
            message = self._append_tool_result_message(
                session=session,
                run=run,
                result=result,
                sequence=len(snapshot.messages) + 1,
            )
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
                    "metadata": _clear_pending_metadata(run.metadata),
                }
            )
            session = session.model_copy(update={"status": "running", "updated_at": utc_now()})
            self.store.save_run(run)
            self.store.save_session(session)
            abort_signal = self._abort_signals.get(run.run_id) or self._activate_abort_signal(
                session.session_id,
                run.run_id,
            )
            try:
                loop_result = await self._continue_model_loop(
                    session=session,
                    run=run,
                    messages=[*snapshot.messages, message],
                    sequence=sequence,
                    emitted=emitted,
                    tool_results=tool_results,
                    abort_signal=abort_signal,
                )
            finally:
                self._release_abort_signal(run.run_id, abort_signal)
            if loop_result.run.status == "completed":
                await self._drain_follow_up_queue(loop_result.session.session_id)
            return loop_result
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
        run = _find_run(snapshot.runs, run_id)
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
            sequence = await self._after_tool_call(emitted, sequence, session, run, result)
            message = self._append_tool_result_message(
                session=session,
                run=run,
                result=result,
                sequence=len(snapshot.messages) + 1,
            )
            run = run.model_copy(
                update={
                    "status": "running",
                    "pending_approval_id": None,
                    "metadata": _clear_pending_metadata(run.metadata),
                }
            )
            session = session.model_copy(update={"status": "running", "updated_at": utc_now()})
            self.store.save_run(run)
            self.store.save_session(session)
            abort_signal = self._activate_abort_signal(session.session_id, run.run_id)
            try:
                resumed = await self._continue_model_loop(
                    session=session,
                    run=run,
                    messages=[*snapshot.messages, message],
                    sequence=sequence,
                    emitted=emitted,
                    tool_results=[result],
                    abort_signal=abort_signal,
                )
            finally:
                self._release_abort_signal(run.run_id, abort_signal)
            if resumed.run.status == "completed":
                await self._drain_follow_up_queue(resumed.session.session_id)
            return resumed
        finally:
            self._release_session_run_lock(run_lock)

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
        abort_signal: AbortSignal,
    ) -> tuple[ToolExecutionOutcome, int, list[MessageRecord]]:
        assistant_message = self._append_assistant_tool_call_message(
            session=session,
            run=run,
            tool_call=tool_call,
            text=assistant_text,
            sequence=message_sequence,
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
            message = self._append_tool_result_message(
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

    def _checkpoint_appended_message(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        message: MessageRecord,
    ) -> None:
        try:
            snapshot = self.store.load_session(session.session_id, recover=False)
            latest_run = _find_run(snapshot.runs, run.run_id)
        except Exception:  # noqa: BLE001 - message append already succeeded; checkpoint best-effort.
            snapshot = None
            latest_run = run
        if latest_run.status == "aborted" or latest_run.abort_requested:
            return
        session_base = snapshot.session if snapshot is not None else session
        persisted_count = len(snapshot.messages) if snapshot is not None else message.sequence
        session = session_base.model_copy(
            update={
                "updated_at": utc_now(),
                "last_run_id": run.run_id,
                "last_message_id": message.message_id,
                "message_count": max(session_base.message_count, persisted_count),
                "metadata": dict(session_base.metadata),
            }
        )
        self.store.save_session(session)

    def _append_assistant_tool_call_message(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        tool_call: dict[str, Any],
        text: str,
        sequence: int,
    ) -> MessageRecord:
        message = MessageRecord.text(
            session_id=session.session_id,
            run_id=run.run_id,
            role="assistant",
            text=text,
            sequence=sequence,
        ).model_copy(
            update={
                "metadata": {
                    "tool_calls": [
                        {
                            "id": str(tool_call.get("id") or ""),
                            "name": str(tool_call.get("name") or ""),
                            "arguments": dict(tool_call.get("arguments") or {}),
                        }
                    ]
                }
            }
        )
        self.store.append_message(message)
        self._checkpoint_appended_message(session=session, run=run, message=message)
        return message

    def _append_tool_result_message(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        result: ToolResult,
        sequence: int,
    ) -> MessageRecord:
        message = MessageRecord.text(
            session_id=session.session_id,
            run_id=run.run_id,
            role="tool_result",
            text=result.content,
            sequence=sequence,
        ).model_copy(
            update={
                "resource_refs": result.resource_refs,
                "metadata": {
                    "tool_call_id": result.tool_call_id,
                    "tool_name": result.tool_name,
                    "is_error": result.is_error,
                },
            }
        )
        self.store.append_message(message)
        self._checkpoint_appended_message(session=session, run=run, message=message)
        return message

    async def _complete_resumed_run(
        self,
        emitted: list[EventRecord],
        sequence: int,
        *,
        session: SessionRecord,
        run: RunRecord,
        last_message_id: str,
        message_count: int,
    ) -> tuple[RunRecord, SessionRecord, int]:
        run = run.model_copy(
            update={
                "status": "completed",
                "ended_at": utc_now(),
                "pending_approval_id": None,
                "metadata": _clear_pending_metadata(run.metadata),
            }
        )
        session = session.model_copy(
            update={
                "status": "idle",
                "updated_at": utc_now(),
                "last_message_id": last_message_id,
                "message_count": message_count,
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
            event_type="run_completed",
            payload={"resumed": True},
        )
        return run, session, sequence

    def _abort_snapshot(self, session_id: str, run_id: str):
        try:
            snapshot = self.store.load_session(session_id, recover=False)
            run = _find_run(snapshot.runs, run_id)
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
            run=_find_run(abort_snapshot.runs, run_id),
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


def _title_from_prompt(prompt: str) -> str:
    stripped = " ".join(prompt.split())
    return stripped[:60] if stripped else "New session"


def _find_run(runs: list[RunRecord], run_id: str) -> RunRecord:
    for run in runs:
        if run.run_id == run_id:
            return run
    raise KeyError(f"unknown run: {run_id}")


def _find_active_run(runs: list[RunRecord]) -> RunRecord:
    for run in reversed(runs):
        if run.status in _ACTIVE_RUN_STATUSES:
            return run
    raise ValueError("no active run to abort")


def _maybe_active_run(runs: list[RunRecord]) -> RunRecord | None:
    try:
        return _find_active_run(runs)
    except ValueError:
        return None


def _pending_invocation(run: RunRecord) -> ToolInvocation:
    raw = run.metadata.get("pending_invocation")
    if not isinstance(raw, dict):
        raise ValueError("run has no pending invocation")
    return ToolInvocation.model_validate(raw)


def _clear_pending_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(metadata)
    for key in ("pending_invocation", "pending_approval", "pending_question"):
        cleaned.pop(key, None)
    return cleaned


def _merge_usage(current: dict[str, int | float], usage: Usage) -> dict[str, int | float]:
    merged = dict(current)
    fields = usage.model_dump()
    for key, value in fields.items():
        if value is None:
            continue
        existing = merged.get(key, 0)
        if isinstance(existing, int | float):
            merged[key] = existing + value
        else:
            merged[key] = value
    return merged
