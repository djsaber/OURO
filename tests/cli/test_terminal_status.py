from __future__ import annotations

import asyncio

from ouro.cli.terminal_status import (
    SPINNER_THEMES,
    TerminalStatusLine,
    TerminalStatusRegion,
)


def test_spinner_semantics_have_distinct_extensible_frame_sets() -> None:
    assert set(SPINNER_THEMES) == {
        "mcp_connection",
        "model_wait",
        "compaction",
        "tool_input",
        "tool_execution",
    }
    assert len({theme.frames for theme in SPINNER_THEMES.values()}) == 5
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
    assert outputs[-1] == "● MCP connected\n"


async def test_timed_status_updates_elapsed_seconds_with_animation() -> None:
    outputs: list[str] = []
    now = 10.0
    status = TerminalStatusLine(
        outputs.append,
        animated=True,
        clock=lambda: now,
    )

    await status.start_timed("model_wait", "Waiting · model response")
    now = 12.4
    for _ in range(20):
        if any("2.4s" in item for item in outputs):
            break
        await asyncio.sleep(0.01)
    await status.clear()

    assert any("Waiting · model response · 2.4s" in item for item in outputs)


async def test_nonanimated_region_emits_each_status_once_when_committed() -> None:
    outputs: list[str] = []
    region = TerminalStatusRegion(outputs.append, animated=False)

    await region.upsert("call_1", "tool_input", "Read · generating parameters")
    await region.upsert("call_1", "tool_execution", "Read · running · (path)")
    await region.finish("call_1", "Read · completed · (path)", tone="success")
    await region.commit()

    assert outputs == ["● Read · completed · (path)\n"]


async def test_animated_region_updates_multiple_rows_in_place() -> None:
    outputs: list[str] = []
    region = TerminalStatusRegion(outputs.append, animated=True)

    await region.upsert("call_1", "tool_input", "Read · generating parameters")
    await region.upsert("call_2", "tool_input", "Write · generating parameters")
    await region.upsert("call_1", "tool_execution", "Read · running · (path)")
    await region.finish("call_1", "Read · completed · (path)", tone="success")
    await region.finish("call_2", "Write · failed · (path)", tone="error")
    await region.commit()

    rendered = "".join(outputs)
    assert "\x1b[1A" in rendered
    assert "Read · completed · (path)" in rendered
    assert "Write · failed · (path)" in rendered
    assert rendered.count("\n") == 2
