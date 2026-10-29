"""Measure TUI history/expansion using a read-only current-schema observation JSON."""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import platform
from collections import Counter
from pathlib import Path
from time import perf_counter

from textual.app import App
from textual.events import Key
from textual.widgets import Input

from ouro.application.contracts.sessions import SessionObservationSnapshot
from ouro.tui.app import OuroTuiApp
from ouro.tui.controller import _observation_detail
from ouro.tui.conversation.view import ConversationView
from ouro.tui.theme import DEFAULT_TUI_THEME
from ouro.tui.view_models import LoadedSession


class HistoryApp(App):
    CSS = """
    ConversationView { height: 1fr; scrollbar-size-vertical: 0; scrollbar-gutter: auto; }
    #conversation-transcript { height: auto; min-height: 100%; layout: vertical; }
    ConversationHistory { height: auto; min-height: 0; overflow: hidden; scrollbar-size: 0 0; }
    .assistant-turn { height: auto; }
    .reasoning-view { height: auto; }
    .reasoning-lines { height: auto; padding: 1 1 0 0; }
    .collapsed { display: none; }
    Input { height: 3; }
    """
    tui_theme = DEFAULT_TUI_THEME

    def compose(self):
        yield ConversationView()
        yield Input()


async def sample(prepared, *, dump: Path | None = None) -> dict:
    result = {}
    frames = {}
    app = HistoryApp()
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        history = conversation._history
        lags = []
        stopped = asyncio.Event()

        async def heartbeat():
            while not stopped.is_set():
                tick = perf_counter()
                await asyncio.sleep(0.001)
                lags.append(max(0.0, perf_counter() - tick - 0.001) * 1000)

        async def measure(name, action):
            lags.clear()
            start = perf_counter()
            await action()
            await history.wait_until_rendered()
            prepared_ms = (perf_counter() - start) * 1000
            await app.screen.wait_for_refresh()
            result[name] = {
                "prepared_ms": prepared_ms,
                "frame_ms": (perf_counter() - start) * 1000,
                "max_loop_lag_ms": max(lags, default=0),
                "pieces": len(history._pieces),
                "lines": len(history.lines),
            }
            if dump:
                frames[name] = [
                    [(segment.text, str(segment.style)) for segment in row] for row in history.lines
                ]

        pulse = asyncio.create_task(heartbeat())
        try:
            await measure("cold", lambda: conversation.load_transcript(prepared))
            state = conversation.take_history_state()
            await measure(
                "cached",
                lambda: conversation.load_transcript(prepared, reusable_history=state),
            )
            traces = Counter(history._trace_run_by_entry.values())
            if traces:
                largest = traces.most_common(1)[0][0]
                summary = history._trace_summary_indexes[largest]
                result["trace_entries"] = traces[largest]
                await measure("trace_first_expand", lambda: history.toggle_trace(summary))
                await history.toggle_trace(summary)
                await measure("trace_reexpand", lambda: history.toggle_trace(summary))

            thoughts = [entry for entry in prepared[0] if entry.reasoning]
            if thoughts:
                longest = max(thoughts, key=lambda entry: len(entry.reasoning))
                result["reasoning_chars"] = len(longest.reasoning)
                await conversation.set_reasoning_expanded(False)
                await conversation.load_transcript(((longest,), frozenset()))
                await history.wait_until_rendered()
                await measure("reasoning_first_expand", lambda: history.toggle_reasoning(0))
                await history.toggle_reasoning(0)
                await measure("reasoning_reexpand", lambda: history.toggle_reasoning(0))
                await conversation.show_welcome()
                await conversation.set_reasoning_expanded(True)
                await conversation.append_reasoning(longest.reasoning)
                await pilot.pause()
                await measure("reasoning_freeze", conversation.finish_assistant)

            field = app.query_one(Input)
            field.focus()
            start = perf_counter()
            field.post_message(Key("x", "x"))
            while field.value != "x":
                await asyncio.sleep(0.001)
            result["key_ms"] = (perf_counter() - start) * 1000
        finally:
            stopped.set()
            await pulse
    if dump:
        dump.write_text(json.dumps(frames, ensure_ascii=False), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--strips-output", type=Path)
    args = parser.parse_args()
    snapshot = SessionObservationSnapshot.model_validate_json(args.snapshot.read_bytes())
    loaded = LoadedSession(
        detail=_observation_detail(snapshot),
        messages=snapshot.messages,
        events=snapshot.events,
        runs=snapshot.runs,
        active_tool_calls=snapshot.active_tool_calls,
        pending_interactions=snapshot.pending_interactions,
        current_request=snapshot.current_request,
        context_status=snapshot.context_status,
        effective_context_metrics=snapshot.effective_context_metrics,
        compactions=snapshot.compactions,
        queued_inputs=snapshot.queued_inputs,
        history_before_cursor=snapshot.history_before_cursor,
    )
    start = perf_counter()
    prepared = OuroTuiApp._prepare_loaded_session(loaded, ())[1]
    print(
        json.dumps(
            {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "messages": len(loaded.messages),
                "entries": len(prepared[0]),
                "projection_ms": (perf_counter() - start) * 1000,
            }
        ),
        flush=True,
    )
    for repeat in range(args.repeat):
        gc.collect()  # Retired Textual Apps must not contaminate the next independent sample.
        result = asyncio.run(sample(prepared, dump=args.strips_output if repeat == 0 else None))
        print(json.dumps({"repeat": repeat, **result}), flush=True)


if __name__ == "__main__":
    main()
