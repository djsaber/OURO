from ouro.cli.interactive_io import InteractiveOutput
from ouro.core.events import EventRecord


def _event(
    sequence: int,
    event_type: str,
    payload: dict[str, object],
) -> EventRecord:
    return EventRecord(
        session_id="ses_1",
        run_id="run_1",
        turn_id="turn_1",
        event_type=event_type,
        sequence=sequence,
        payload={"turn_id": "turn_1", **payload},
    )


def test_interactive_output_renders_tool_call_fragments_before_model_completion() -> None:
    outputs: list[str] = []
    output = InteractiveOutput(outputs.append)

    output.handle_event(
        _event(
            1,
            "model_tool_call_delta",
            {
                "index": 0,
                "id_delta": "call_1",
                "name_delta": "Read",
                "arguments_delta": '{"path"',
            },
        )
    )

    assert "Read" in outputs[-1]
    assert '{"path"' in outputs[-1]

    output.handle_event(
        _event(
            2,
            "model_tool_call_delta",
            {
                "index": 0,
                "id_delta": "",
                "name_delta": "",
                "arguments_delta": ':"README.md"}',
            },
        )
    )
    assert '"README.md"' in outputs[-1]
    output.handle_event(_event(3, "model_message_completed", {"tool_call_count": 1}))

    rendered = "".join(outputs)
    assert "Read" in rendered
    assert '{"path": "README.md"}' in rendered


def test_interactive_output_finalizes_interleaved_tool_calls_in_source_order() -> None:
    outputs: list[str] = []
    output = InteractiveOutput(outputs.append)
    deltas = [
        {"index": 0, "id_delta": "call_1", "name_delta": "Read", "arguments_delta": "{"},
        {
            "index": 1,
            "id_delta": "call_2",
            "name_delta": "Write",
            "arguments_delta": '{"path":"out.txt"}',
        },
        {"index": 0, "id_delta": "", "name_delta": "", "arguments_delta": "}"},
    ]
    for sequence, payload in enumerate(deltas, start=1):
        output.handle_event(_event(sequence, "model_tool_call_delta", payload))

    output.handle_event(_event(4, "model_message_completed", {"tool_call_count": 2}))

    final_lines = outputs[-2:]
    assert "Read" in final_lines[0]
    assert "Write" in final_lines[1]


def test_interactive_output_bounds_and_sanitizes_live_argument_preview() -> None:
    outputs: list[str] = []
    output = InteractiveOutput(outputs.append)

    output.handle_event(
        _event(
            1,
            "model_tool_call_delta",
            {
                "index": 0,
                "id_delta": "call_1",
                "name_delta": "Write",
                "arguments_delta": "x\n" + "y" * 300,
            },
        )
    )

    assert "\n" not in outputs[-1]
    assert "\\n" in outputs[-1]
    assert "…" in outputs[-1]
    assert len(outputs[-1]) < 320
