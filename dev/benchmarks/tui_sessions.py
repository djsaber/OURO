"""Offline, headless whole-TUI timing; uses disposable synthetic sessions only."""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from textual.events import Key

from ouro.application.inputs.requests import UserInputRequest
from ouro.application.runtime.environment import RuntimeEnvironment
from ouro.application.runtime.service import RuntimeService
from ouro.core.adapter import RuntimeAdapter
from ouro.core.messages import MessageRecord
from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.core.state import SessionRecord
from ouro.local_application import LocalApplication
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import ToolRegistry
from ouro.tui.app import OuroTuiApp
from ouro.tui.composer.input import Composer
from ouro.tui.conversation.history import ConversationHistory
from ouro.tui.conversation.turn import AssistantTurn


class HeldProvider:
    provider_id = "faux"

    def __init__(self, reasoning_chars: int) -> None:
        self.reasoning = ("Synthetic reasoning 中文 **literal**. " * reasoning_chars)[
            :reasoning_chars
        ]
        self.ready = asyncio.Event()
        self.advance = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(self, *, request_context, **kwargs):
        if request_context.values["request.purpose"] != "chat":
            yield ProviderStreamEvent(type="text_delta", text="Synthetic summary.")
            yield ProviderStreamEvent(type="done")
            return
        yield ProviderStreamEvent(type="reasoning_delta", reasoning_content=self.reasoning)
        yield ProviderStreamEvent(type="text_delta", text="Before attachment")
        self.ready.set()
        await self.advance.wait()
        yield ProviderStreamEvent(type="text_delta", text="; caught up after attachment")
        await self.release.wait()
        yield ProviderStreamEvent(type="done")


async def until(predicate) -> None:
    async with asyncio.timeout(60):
        while not predicate():
            await asyncio.sleep(0.005)


async def sample(
    home: Path, *, messages: int, reasoning_chars: int, backend: str = "files"
) -> dict:
    store = (
        LocalSessionStore(home) if backend == "files" else SQLiteSessionStore(home / "sessions.db")
    )
    store.initialize()
    session = SessionRecord(title="Synthetic long history", current_model="faux/faux")
    store.create_session_snapshot(
        session,
        [
            MessageRecord.text(
                session_id=session.session_id,
                sequence=index + 1,
                role="user" if index % 2 == 0 else "assistant",
                text=f"History {index + 1}. " + "Synthetic **evidence**. " * 16,
            )
            for index in range(messages)
        ],
    )
    store.refresh_catalog(session.session_id)
    provider = HeldProvider(reasoning_chars)
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="faux", provider_id="faux", context_window=20_000_000),
        ),
        tools=ToolRegistry(),
        default_workspace=home,
        environment=RuntimeEnvironment.local(home),
    )
    application = LocalApplication.from_runtime(runtime)
    app = OuroTuiApp(application=application)
    result = {"messages": messages, "reasoning_chars": reasoning_chars, "samples": []}
    preparations = []
    prepare = app._prepare_loaded_session

    def measured_preparation(loaded, *args, **kwargs):
        start = perf_counter()
        prepared = prepare(loaded, *args, **kwargs)
        preparations.append(
            {
                "source_messages": len(loaded.messages),
                "snapshot_events": len(loaded.events),
                "projection_ms": (perf_counter() - start) * 1000,
            }
        )
        return prepared

    app._prepare_loaded_session = measured_preparation
    async with app.run_test(size=(100, 32)) as pilot:
        for worker in tuple(app.workers):
            if worker.name == "tui-start":
                await worker.wait()
        await pilot.pause()
        submission = await application.supervisor.submit_user_input(
            UserInputRequest(session_id=session.session_id, prompt="Continue synthetic session")
        )
        try:
            await asyncio.wait_for(provider.ready.wait(), 60)
            await asyncio.sleep(0.15)  # Let the existing draft publication interval elapse.
            history = app.query_one(ConversationHistory)
            turn = app.query_one(AssistantTurn)
            restores = []
            restore = history._restore_state

            def measured_restore(state, entries):
                restored = restore(state, entries)
                restores.append(
                    {
                        "cached_entries": len(state.entries) if state else 0,
                        "cached_width": state.render_width if state else 0,
                        "current_width": history.scrollable_content_region.width,
                        "shared_entries": restored,
                    }
                )
                return restored

            history._restore_state = measured_restore
            for scenario in ("home_to_running", "cached_to_running"):
                lags = []
                stopped = asyncio.Event()

                async def heartbeat(stopped=stopped, lags=lags):
                    while not stopped.is_set():
                        tick = perf_counter()
                        await asyncio.sleep(0.01)
                        lags.append(max(0.0, perf_counter() - tick - 0.01) * 1000)

                pulse = asyncio.create_task(heartbeat())
                start = perf_counter()
                await app._open_session(session.session_id)
                installed_ms = (perf_counter() - start) * 1000
                composer = app.query_one(Composer)
                previous = composer.text
                key_start = perf_counter()
                composer.post_message(Key("x", "x"))
                await until(lambda composer=composer, previous=previous: composer.text != previous)
                key_ms = (perf_counter() - key_start) * 1000
                interactive_ms = (perf_counter() - start) * 1000
                await until(lambda: history.render_complete and bool(history.lines))
                await app.screen.wait_for_refresh()
                history_frame_ms = (perf_counter() - start) * 1000
                await until(
                    lambda: (
                        "Before attachment" in turn.tail.rendered_text
                        and not turn.reasoning._content._deferred_render
                        and not turn.reasoning._content._render_active
                        and bool(turn.reasoning._content.rendered_text)
                    )
                )
                await app.screen.wait_for_refresh()
                caught_up_ms = (perf_counter() - start) * 1000
                stopped.set()
                await pulse
                result["samples"].append(
                    {
                        "scenario": scenario,
                        "installed_ms": installed_ms,
                        "history_frame_ms": history_frame_ms,
                        "draft_frame_ms": caught_up_ms,
                        "key_ms": key_ms,
                        "interactive_ms": interactive_ms,
                        "max_loop_lag_ms": max(lags, default=0),
                        "entries": len(history.entries),
                        "lines": len(history.lines),
                        **preparations[-1],
                        "cache_restore": restores[-1],
                    }
                )
                if scenario == "home_to_running":
                    for _ in range(2):
                        if not await app._prepend_history_window(app._view_revision):
                            break
                        await history.wait_until_rendered()
                    await app._new_session()
                    await pilot.pause()
            start = perf_counter()
            provider.advance.set()
            await until(lambda: "caught up after attachment" in turn.tail.rendered_text)
            await app.screen.wait_for_refresh()
            result["live_delta_frame_ms"] = (perf_counter() - start) * 1000
        finally:
            provider.advance.set()
            provider.release.set()
            await submission.wait()
    await application.close()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--messages", type=int, nargs="+", default=[1000, 10000])
    parser.add_argument("--reasoning-chars", type=int, default=48070)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--workdir", type=Path)
    args = parser.parse_args()
    if args.workdir:
        args.workdir.mkdir(parents=True, exist_ok=True)
    print(json.dumps({"python": platform.python_version(), "platform": platform.platform()}))
    for count in args.messages:
        for repeat in range(args.repeat):
            with TemporaryDirectory(prefix="tui-", dir=args.workdir) as directory:
                measured = asyncio.run(
                    sample(Path(directory), messages=count, reasoning_chars=args.reasoning_chars)
                )
            print(json.dumps({"repeat": repeat, **measured}), flush=True)


if __name__ == "__main__":
    main()
