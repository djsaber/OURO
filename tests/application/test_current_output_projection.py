from __future__ import annotations

import gc
import weakref

import pytest

from ouro.application.observations.content import SessionContentProjection
from ouro.core.events import EventRecord
from ouro.core.response_drafts import ResponseStreamPosition


def event(sequence, kind, *, context_id=None, cursor=None, **payload):
    return EventRecord(
        session_id="ses_output",
        run_id="run_output",
        sequence=sequence,
        event_type=kind,
        payload=payload,
        stream=ResponseStreamPosition(context_id=context_id, cursor=cursor)
        if cursor is not None
        else None,
    )


@pytest.mark.parametrize("requests", [4, 128])
def test_projection_releases_old_requests_and_completed_fragments(requests):
    class Fragment(str):
        pass

    projection = SessionContentProjection("ses_output")
    fragments = []
    for index in range(requests):
        context = f"ctx_{index}"
        started = event(index * 2 + 1, "model_request_started")
        started.payload["context_id"] = context
        projection.apply(started)
        assert projection.assistant_text == projection.reasoning_text == ""
        for cursor, (kind, field) in enumerate(
            (("model_reasoning_delta", "reasoning_content"), ("model_text_delta", "text")), 1
        ):
            fragment = Fragment(f"{index}-{field}:" + "x" * 8192)
            fragments.append(weakref.ref(fragment))
            projection.apply(
                event(index * 2 + 1, kind, context_id=context, cursor=cursor, **{field: fragment})
            )
        del fragment
        text, reasoning = projection.assistant_text, projection.reasoning_text
        committed = event(
            index * 2 + 2, "model_response_committed", text=text, reasoning_content=reasoning
        )
        committed.payload["context_id"] = context
        update = projection.apply(committed)
        assert update is not None and update.assistant_delta == update.reasoning_delta == ""
        assert projection.assistant_text == text
        assert projection.reasoning_text == reasoning
    gc.collect()
    assert all(reference() is None for reference in fragments)
    assert projection.assistant_text.startswith(f"{requests - 1}-text:")
    assert len(projection.assistant_text) < 8300


def test_committed_catchup_replaces_previous_request_without_repeating_live_prefix():
    projection = SessionContentProjection("ses_output")
    previous = event(
        1, "model_response_committed", text="previous response", reasoning_content="old reasoning"
    )
    previous.payload["context_id"] = "ctx_old"
    projection.apply(previous)
    # Joining mid-request can receive a current Draft before its started event.
    projection.apply(event(2, "model_text_delta", context_id="ctx_current", cursor=1, text="new "))
    assert projection.assistant_text == "new "
    assert projection.reasoning_text == ""
    committed = event(
        3, "model_response_committed", text="new response", reasoning_content="reasoning"
    )
    committed.payload["context_id"] = "ctx_current"
    update = projection.apply(committed)
    assert update is not None and update.assistant_delta == "response"
    assert update.reasoning_delta == "reasoning"
    assert projection.assistant_text == "new response"
    assert projection.reasoning_text == "reasoning"
    assert projection.apply(committed) is None
    assert (
        projection.apply(
            event(3, "model_text_delta", context_id="ctx_current", cursor=2, text="late")
        )
        is None
    )
