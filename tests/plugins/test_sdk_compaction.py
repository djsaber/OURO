from __future__ import annotations

import asyncio
import threading
import time

import pytest

from ouro.application.runtime import RuntimeService
from ouro.config.compaction import CompactionConfig
from ouro.core.adapter import RuntimeAdapter
from ouro.core.cancellation import AbortSignal
from ouro.core.compaction import CompactionAbortedError, CompactionPolicy, CompactionRequest
from ouro.core.messages import MessageRecord
from ouro.core.provider import ModelMetadata, ProviderCheckResult, ProviderStreamEvent
from ouro.core.state import SessionRecord
from ouro.plugins.api import adapt_sdk_contributions
from ouro.sdk.plugins import v1 as sdk
from ouro.sessions import LocalSessionStore
from ouro.tools import ToolRegistry


class PassiveProvider:
    provider_id = "passive"

    async def stream(self, *, model, system_prompt="", messages, tools=None):
        del model, system_prompt, messages, tools
        yield ProviderStreamEvent(type="done")

    async def check_connection(self, model: ModelMetadata) -> ProviderCheckResult:
        return ProviderCheckResult(provider_id=self.provider_id, model_id=model.model_id, ok=True)


def _strategy(handler):
    contributions = adapt_sdk_contributions(
        sdk.ContributionSet(
            compaction_strategies=[
                sdk.CompactionContribution(
                    id="plugin.summary",
                    version="1",
                    handler=handler,
                )
            ]
        )
    )
    return contributions.compaction_strategies[0]


def _request() -> CompactionRequest:
    return CompactionRequest(
        session_id="ses_sdk_compaction",
        run_id="run_sdk_compaction",
        strategy_id="plugin.summary",
        trigger="manual",
        messages=[
            MessageRecord.text(
                session_id="ses_sdk_compaction",
                role="user",
                text="old context",
            )
        ],
        input_token_budget=2_000,
        summary_output_tokens=200,
    )


@pytest.mark.asyncio
async def test_sdk_compaction_exposes_cooperative_cancellation_to_sync_handler() -> None:
    started = threading.Event()
    stopped = threading.Event()

    def handler(call: sdk.CompactionCall, context: sdk.CancellationContext):
        del call
        started.set()
        try:
            while True:
                context.raise_if_aborted()
                time.sleep(0.01)
        finally:
            stopped.set()

    strategy = _strategy(handler)
    signal = AbortSignal()
    task = asyncio.create_task(strategy.compact(_request(), signal=signal))
    assert await asyncio.to_thread(started.wait, 2)

    signal.abort("Stop SDK compaction")

    with pytest.raises(CompactionAbortedError, match="Stop SDK compaction"):
        await asyncio.wait_for(task, timeout=2)
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_runtime_abort_cancels_async_sdk_compaction_and_owns_cleanup(tmp_path) -> None:
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def handler(call: sdk.CompactionCall) -> sdk.CompactionOutput:
        del call
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    strategy = _strategy(handler)
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="SDK compaction cancellation")
    store.create_session(session)
    store.append_messages(
        [
            MessageRecord.text(
                session_id=session.session_id,
                role=role,
                text=f"old {role} {turn} " + content,
                sequence=index,
            )
            for index, (turn, role, content) in enumerate(
                (
                    (turn, role, content)
                    for turn in range(3)
                    for role, content in (
                        ("user", "x" * 1_000),
                        ("assistant", "y" * 1_000),
                    )
                ),
                start=1,
            )
        ]
    )
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            PassiveProvider(),
            ModelMetadata(
                model_id="passive",
                provider_id="passive",
                context_window=3_000,
                max_output_tokens=500,
            ),
        ),
        tools=ToolRegistry(),
        system_prompt="",
        compaction_config=CompactionConfig(
            strategy_id=strategy.strategy_id,
            policy=CompactionPolicy(trigger_ratio=0.3, target_ratio=0.2),
        ),
    )
    runtime.compaction_strategies.register(strategy)

    compact_task = asyncio.create_task(runtime.compact(session_id=session.session_id))
    await asyncio.wait_for(started.wait(), timeout=2)
    active_run = store.load_runs(session.session_id)[-1]
    abort_task = asyncio.create_task(
        runtime.abort_run(
            session_id=session.session_id,
            run_id=active_run.run_id,
            reason="Stop async SDK compaction",
        )
    )

    compact_result, abort_result = await asyncio.gather(compact_task, abort_task)

    assert compact_result.run.status == "aborted"
    assert abort_result.run.status == "aborted"
    assert stopped.is_set()
    assert store.load_session_record(session.session_id).active_compaction_id is None
