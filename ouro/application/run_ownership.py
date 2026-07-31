from __future__ import annotations

from ouro.core.cancellation import AbortSignal
from ouro.core.events import EventRecord
from ouro.core.results import RunResult
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tools import ToolResult
from ouro.sessions import SessionLock, SessionStore


class RunExecutionOwnership:
    """Own process-local locks and cancellation signals for durable Runs."""

    def __init__(self, store: SessionStore) -> None:
        self.store = store
        self._abort_signals: dict[str, AbortSignal] = {}

    def acquire(self, session_id: str, run_id: str) -> SessionLock | None:
        return self.store.acquire_session_lock(session_id, run_id)

    @staticmethod
    def release(lock: SessionLock | None) -> None:
        if lock is not None:
            lock.release()

    def is_locked(self, session_id: str) -> bool:
        return self.store.is_session_locked(session_id)

    def activate_signal(self, session_id: str, run_id: str) -> AbortSignal:
        signal = AbortSignal(lambda: self.abort_state(session_id, run_id) is not None)
        self._abort_signals[run_id] = signal
        return signal

    def release_signal(self, run_id: str, signal: AbortSignal) -> None:
        if self._abort_signals.get(run_id) is signal:
            self._abort_signals.pop(run_id, None)

    def signal_for(self, run_id: str) -> AbortSignal | None:
        return self._abort_signals.get(run_id)

    def is_aborted(self, session_id: str, run_id: str) -> bool:
        return self.abort_state(session_id, run_id) is not None

    def abort_state(
        self,
        session_id: str,
        run_id: str,
    ) -> tuple[SessionRecord, RunRecord] | None:
        try:
            run = self.store.load_run(session_id, run_id)
        except Exception:  # noqa: BLE001 - abort polling must not break active runs.
            return None
        if run.status != "aborted" and not run.abort_requested:
            return None
        try:
            return self.store.load_session_record(session_id), run
        except Exception:  # noqa: BLE001 - abort polling must not break active runs.
            return None

    def aborted_result(
        self,
        *,
        session_id: str,
        run_id: str,
        final_text: str,
        emitted: list[EventRecord],
        tool_results: list[ToolResult],
    ) -> RunResult | None:
        abort_state = self.abort_state(session_id, run_id)
        if abort_state is None:
            return None
        session, run = abort_state
        return RunResult(
            session=session,
            run=run,
            final_text=final_text,
            events=emitted,
            tool_results=tool_results,
        )
