from __future__ import annotations

import asyncio

from ouro.cli.terminal_status import SPINNER_THEMES, TerminalStatusLine


def test_spinner_semantics_have_distinct_extensible_frame_sets() -> None:
    assert set(SPINNER_THEMES) == {
        "mcp_connection",
        "compaction",
        "tool_input",
        "tool_execution",
    }
    assert len({theme.frames for theme in SPINNER_THEMES.values()}) == 4
    assert all(theme.frames and theme.interval_seconds > 0 for theme in SPINNER_THEMES.values())


async def test_nonanimated_status_does_not_emit_on_every_message_update() -> None:
    outputs: list[str] = []
    status = TerminalStatusLine(outputs.append, animated=False)

    await status.start("tool_input", "Receiving tool input")
    status.update("Receiving tool input · Read")
    status.update("Receiving tool input · Read more arguments")

    assert len(outputs) == 1
    assert outputs[0].endswith("Receiving tool input\n")
    await status.clear()


async def test_animated_status_replaces_one_terminal_line() -> None:
    outputs: list[str] = []
    status = TerminalStatusLine(outputs.append, animated=True)

    await status.start("tool_execution", "Running tool · Read")
    for _ in range(20):
        if len(outputs) >= 2:
            break
        await asyncio.sleep(0.01)
    await status.clear()

    frames = [item for item in outputs if "Running tool · Read" in item]
    assert len(frames) >= 2
    assert all(item.startswith("\r\x1b[2K") for item in frames)
    assert outputs[-1] == "\r\x1b[2K"


async def test_status_finish_clears_animation_and_emits_stable_result() -> None:
    outputs: list[str] = []
    status = TerminalStatusLine(outputs.append, animated=False)

    await status.start("mcp_connection", "Connecting MCP")
    await status.finish("MCP connected", tone="success")

    assert status.active is False
    assert outputs[-1] == "✓ MCP connected\n"
