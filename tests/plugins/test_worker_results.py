from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from ouro.core.harness import HarnessState
from ouro.core.harness_calls import HarnessCallSnapshot
from ouro.plugins import worker as worker_module
from ouro.plugins.harness_calls import PluginStateView
from ouro.plugins.manifest import PluginManifest
from ouro.plugins.worker import WorkerPluginInstance, WorkerProtocolError
from ouro.sdk.plugins import v5 as sdk

CALLS = {
    "tool": sdk.ToolCall("call", "session", "run", "echo", {}),
    "tool_resume": sdk.ToolResumeCall(
        "call", "session", "run", "echo", {}, {}, sdk.AnswerResolution(value="yes")
    ),
    "observer": sdk.ContextPreparedObservation("session", "run", 0, 0, 0),
    "context": sdk.ContextCall("session", "run", 0, 0),
    "command": sdk.CommandCall("command", session_id="session"),
    "session_controller": sdk.SessionControllerCall(
        "session", "primary", "activation", "mode", "activate"
    ),
    "compaction": sdk.CompactionCall("session", "run", "", ()),
    "continuation": sdk.RunOutcome("session", "run", "completed", "user"),
    "tool_policy": sdk.ToolPolicyCall("session", "run", "call", "echo", "echo", "owner"),
    "prompt_policy": sdk.PromptPolicyCall("session", "run", "hello"),
    "turn_policy": sdk.TurnCompletionCall("session", "run", "turn", "done"),
}
DEFAULTS = {
    "tool": sdk.ToolOutput(),
    "tool_resume": sdk.ToolOutput(),
    "observer": None,
    "context": sdk.ContextOutput(),
    "command": sdk.CommandOutput(),
    "session_controller": sdk.SessionControllerOutput(),
    "compaction": sdk.CompactionOutput(summary=""),
    "continuation": sdk.ContinuationDecision(),
    "tool_policy": sdk.ToolPolicyDecision(),
    "prompt_policy": sdk.PromptPolicyDecision(),
    "turn_policy": sdk.TurnPolicyDecision(),
}


@pytest.mark.parametrize("kind", ["tool", "tool_resume"])
async def test_worker_decodes_content_parts_and_rejects_unknown_types(worker, kind):
    invoke, _ = worker
    result = await invoke(
        kind,
        {
            "content": [
                {"type": "text", "text": "before"},
                {"type": "image", "uri": "data:image/png;base64,AAAA", "media_type": "image/png"},
                {"type": "text", "text": "after"},
            ]
        },
    )
    assert result.content == (
        sdk.ToolTextContent("before"),
        sdk.ToolImageContent("data:image/png;base64,AAAA", "image/png"),
        sdk.ToolTextContent("after"),
    )
    for part in ({"type": "unknown"}, {"type": "image", "uri": 3}, {"type": "image", "uri": ""}):
        with pytest.raises(WorkerProtocolError, match="content"):
            await invoke(kind, {"content": [part]})


@pytest.fixture
def worker(monkeypatch, tmp_path):
    package = Path(__file__).parents[2] / "examples/plugins/worker-tool"
    manifest = PluginManifest.model_validate_json(
        (package / ".ouro-plugin/plugin.json").read_text(encoding="utf-8")
    )
    diagnostics = []
    instance = WorkerPluginInstance(
        manifest,
        package_root=package,
        package_digest="test",
        permissions=frozenset(),
        configuration={},
        data_directory=tmp_path,
        credentials={},
        diagnostic=lambda *entry: diagnostics.append(entry),
    )

    async def invoke(kind, payload, view=None):
        async def respond(method, params, *, timeout=None):
            expected = {"tool_resume": "tool.resume", "observer": "observer.observe"}.get(
                kind, f"{kind}.call"
            )
            assert method == expected
            assert params["contribution_id"] == "example"
            return dict(payload) if isinstance(payload, dict) else payload

        monkeypatch.setattr(instance.client, "request", respond)
        handler = getattr(instance, f"_{kind}_handler")("example")
        return await handler(replace(CALLS[kind], session_state=view))

    return invoke, diagnostics


@pytest.fixture
def view():
    value = PluginStateView(
        HarnessCallSnapshot(
            session_id="session", harness_version="before", history_epoch=0, state=HarnessState()
        ),
        "example.owner",
        writable=True,
    )
    yield value
    value.close()


@pytest.mark.parametrize("kind", CALLS)
async def test_worker_defaults_and_unchanged_state(worker, view, kind):
    invoke, diagnostics = worker
    assert await invoke(kind, {}, view) == DEFAULTS[kind]
    assert view.finish() is None
    assert diagnostics == []


@pytest.mark.parametrize("kind", ["tool", "tool_resume"])
async def test_worker_input_request_and_output_remain_distinct(worker, kind):
    invoke, _ = worker
    assert await invoke(kind, {"input_request": {"choices": ["yes", "no"]}}) == (
        sdk.ToolInputRequest(data={"choices": ["yes", "no"]})
    )
    assert await invoke(
        kind,
        {"content": "done", "structured": {"value": 3}, "resource_refs": ["r"], "is_error": True},
    ) == sdk.ToolOutput(
        content="done", structured={"value": 3}, resource_refs=("r",), is_error=True
    )


@pytest.mark.parametrize("kind", ["context", "command", "tool", "tool_resume", "continuation"])
async def test_worker_reuses_decoded_fragments_inputs_and_transitions(worker, monkeypatch, kind):
    invoke, _ = worker
    decoded = {name: [] for name in ("context_fragment", "session_inputs", "control_transitions")}

    def observe(name):
        original = getattr(worker_module, f"_worker_{name}")

        def decode(raw):
            value = original(raw)
            decoded[name].append(value)
            return value

        monkeypatch.setattr(worker_module, f"_worker_{name}", decode)

    for name in decoded:
        observe(name)
    if kind == "context":
        output = await invoke(kind, {"fragments": [{"text": "context"}]})
        assert len(decoded["context_fragment"]) == 1
        assert output.fragments[0] is decoded["context_fragment"][0]
    else:
        payload = {"session_control_transitions": [{"action": "acquire", "controller_id": "mode"}]}
        if kind == "command":
            payload.update(
                session_inputs=[{"prompt": "one"}],
                session_input_batches=[{"inputs": [{"prompt": "two"}], "coalesce_key": "batch"}],
            )
        output = await invoke(kind, payload)
        assert len(decoded["control_transitions"]) == 1
        assert output.session_control_transitions is decoded["control_transitions"][0]
        if kind == "command":
            assert len(decoded["session_inputs"]) == 2
            assert output.session_inputs is decoded["session_inputs"][0]
            assert output.session_input_batches[0].inputs is decoded["session_inputs"][1]


@pytest.mark.parametrize(
    ("kind", "payload", "error"),
    [
        ("tool", {"content": 1}, "content"),
        ("tool_resume", {"input_request": {}, "content": "mixed"}, "together"),
        ("tool", {"resource_refs": [1]}, "resource_refs"),
        ("tool", {"is_error": 1}, "is_error"),
        ("tool", {"session_control_transitions": [{}]}, "transition"),
        ("observer", {"output": "unexpected"}, "empty"),
        ("context", {"fragments": {}}, "fragments"),
        ("context", {"fragments": [{"text": "ok"}, {"text": "bad", "role": "invalid"}]}, "role"),
        (
            "context",
            {"fragments": [{"text": "bad", "semantic_tag": "invalid tag"}]},
            "semantic_tag",
        ),
        ("context", {"fragments": [{"text": "bad", "scaffold": 1}]}, "scaffold"),
        ("command", {"data": []}, "data"),
        ("command", {"run_action": "invalid"}, "run_action"),
        ("command", {"session_inputs": [{"prompt": ""}]}, "prompt"),
        ("command", {"session_input_batches": [1]}, "batch"),
        ("session_controller", {"usage_scope": 1}, "usage_scope"),
        ("compaction", {"usage": {"tokens": True}}, "numeric"),
        ("compaction", {"summary": None}, "summary"),
        ("continuation", {"action": "invalid"}, "action"),
        ("continuation", {"mode": "invalid"}, "mode"),
        ("continuation", {"role": "invalid"}, "role"),
        ("continuation", {"semantic_tag": "invalid tag"}, "semantic_tag"),
        ("continuation", {"scaffold": 1}, "scaffold"),
        ("tool_policy", {"action": "allow"}, "action"),
        ("prompt_policy", {"reason": 1}, "reason"),
        ("turn_policy", {"feedback": []}, "feedback"),
    ],
)
async def test_invalid_worker_output_is_reported_before_staging_state(
    worker, view, kind, payload, error
):
    invoke, diagnostics = worker
    with pytest.raises(WorkerProtocolError, match=error):
        await invoke(
            kind, {**payload, "session_state": {"state_schema_version": 1, "payload": "bad"}}, view
        )
    assert view.finish() is None
    assert len(diagnostics) == 1
    assert diagnostics[0][0] == "worker_invalid_result"


@pytest.mark.parametrize("kind", CALLS)
async def test_nonobject_worker_response_is_reported(worker, kind):
    invoke, diagnostics = worker
    with pytest.raises(WorkerProtocolError, match="must be an object"):
        await invoke(kind, [])
    assert diagnostics[0][0] == "worker_invalid_result"


@pytest.mark.parametrize("action", ["stop", "continue"])
async def test_worker_decodes_command_run_action(worker, action):
    invoke, _ = worker
    assert (await invoke("command", {"run_action": action})).run_action == action


@pytest.mark.parametrize(
    "payload",
    [
        {"session_input_batches": [{"inputs": []}]},
        {
            "session_input_batches": [
                {"inputs": [{"prompt": "a"}, {"prompt": "b", "mode": "steer"}]}
            ]
        },
    ],
)
async def test_invalid_batch_keeps_sdk_value_error_and_does_not_stage_state(worker, view, payload):
    invoke, _ = worker
    with pytest.raises(ValueError, match="batch"):
        await invoke(
            "command",
            {**payload, "session_state": {"state_schema_version": 1, "payload": "bad"}},
            view,
        )
    assert view.finish() is None


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ([], WorkerProtocolError),
        ({"state_schema_version": 0, "payload": {}}, ValueError),
        ({"state_schema_version": 1, "payload": {}, "owner_id": "foreign"}, TypeError),
    ],
)
async def test_invalid_state_proposal_cannot_leave_partial_state(worker, view, change, error):
    invoke, _ = worker
    with pytest.raises(error):
        await invoke("command", {"output": "valid", "session_state": change}, view)
    assert view.finish() is None


async def test_valid_output_and_state_replacement_are_returned_together(worker, view):
    invoke, diagnostics = worker
    output = await invoke(
        "command",
        {"output": "done", "session_state": {"state_schema_version": 1, "payload": {"count": 1}}},
        view,
    )
    assert output == sdk.CommandOutput(output="done")
    change = view.finish()
    assert change.session_id == "session"
    assert change.owner_id == "example.owner"
    assert change.state.payload == {"count": 1}
    assert diagnostics == []


async def test_worker_delete_is_a_staged_proposal(worker, view):
    invoke, _ = worker
    view.replace(state_schema_version=1, payload="temporary")
    assert await invoke("command", {"session_state": None}, view) == sdk.CommandOutput()
    assert view.read() is None
    assert view.finish() is None
