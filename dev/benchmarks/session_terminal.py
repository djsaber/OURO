"""Offline interactive harness for manual terminal validation; never contacts a model service.

Start this command in two terminals with the same --home and --backend. Create Sessions
in one window and open them from the session picker in the other. All output is synthetic.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from ouro.application.runtime import RuntimeService
from ouro.application.runtime_environment import RuntimeEnvironment
from ouro.core.adapter import RuntimeAdapter
from ouro.core.messages import MessageRecord
from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.core.state import SessionRecord
from ouro.local_application import LocalApplication
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import Tool, ToolDefinition, ToolRegistry, ToolResult
from ouro.tui.app import OuroTuiApp


class TerminalProvider:
    provider_id = "faux"

    def __init__(self, deltas: int, interval_ms: float):
        self.deltas = deltas
        self.interval = interval_ms / 1000

    async def stream(self, *, messages, request_context, **kwargs):
        if request_context.values["request.purpose"] != "chat":
            yield ProviderStreamEvent(type="text_delta", text="Synthetic context summary.")
            yield ProviderStreamEvent(type="done")
            return
        run_id = request_context.values["run.id"]
        tool_completed = messages[-1].role == "tool_result"
        if tool_completed:
            yield ProviderStreamEvent(type="text_delta", text="Synthetic tool finished.\n\n")
        else:
            yield ProviderStreamEvent(
                type="reasoning_delta", reasoning_content="Synthetic reasoning. "
            )
        for index in range(self.deltas):
            await asyncio.sleep(self.interval)
            yield ProviderStreamEvent(
                type="text_delta", text=f"**{run_id[-8:]} / {index + 1}** Synthetic evidence.\n\n"
            )
        if not tool_completed:
            yield ProviderStreamEvent.from_tool_call(
                {"id": f"evidence_{run_id}", "name": "Evidence", "arguments": {}}
            )
        yield ProviderStreamEvent(type="done")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--backend", choices=("files", "sqlite"), default="files")
    parser.add_argument("--deltas", type=int, default=400)
    parser.add_argument("--interval-ms", type=float, default=25)
    parser.add_argument("--seed-history", type=int, default=0)
    args = parser.parse_args()
    if args.deltas < 1 or args.interval_ms < 0 or args.seed_history < 0:
        parser.error("--deltas must be positive; --interval-ms and --seed-history nonnegative")
    home = args.home.resolve()
    store = LocalSessionStore(home) if args.backend == "files" else SQLiteSessionStore(home / "db")
    store.initialize()
    if args.seed_history:
        session = SessionRecord(
            title=f"Synthetic {args.seed_history} messages", current_model="faux/faux"
        )
        store.create_session_snapshot(
            session,
            [
                MessageRecord.text(
                    session_id=session.session_id,
                    sequence=index + 1,
                    role="user" if index % 2 == 0 else "assistant",
                    text=f"History {index + 1}. " + "Synthetic **evidence**. " * 8,
                )
                for index in range(args.seed_history)
            ],
        )
        if error := store.refresh_catalog(session.session_id):
            parser.error(f"Could not publish seeded session: {error}")
    registry = ToolRegistry()
    registry.register(
        Tool(
            definition=ToolDefinition(
                name="Evidence", description="Synthetic data", effect="read_only"
            ),
            handler=lambda invocation, context: ToolResult(
                tool_call_id=invocation.tool_call_id,
                tool_name=invocation.tool_name,
                content="synthetic evidence\n" * 2048,
            ),
        )
    )
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            TerminalProvider(args.deltas, args.interval_ms),
            ModelMetadata(model_id="faux", provider_id="faux", context_window=2_000_000),
        ),
        tools=registry,
        default_workspace=home,
        environment=RuntimeEnvironment.local(home),
    )
    OuroTuiApp(application=LocalApplication.from_runtime(runtime)).run()


if __name__ == "__main__":
    main()
