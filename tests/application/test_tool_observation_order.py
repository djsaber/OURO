from __future__ import annotations

from dataclasses import replace

import pytest

from ouro.application.runs.tool_batches import load_batch_call
from ouro.core.state import SessionRecord
from ouro.plugins.catalog.snapshot import ExtensionCatalogBase, build_extension_catalog
from ouro.plugins.inprocess.contributions import adapt_sdk_contributions
from ouro.plugins.session_controllers import PluginSessionControllerRegistry
from ouro.sdk.plugins import v5 as sdk
from tests.application.tool_runtime import runtime
from tests.application.tool_runtime import store as store
from tests.run_facts import run_tool_results

OWNER = "example.observation"
CONTRACT = "c" * 64


class InterruptedRun(BaseException):
    """Interrupt the owner without turning an injected storage fault into a tool error."""


@pytest.mark.parametrize(
    "boundary", ["success", "controller_error", "before_outcome", "before_stage", "publication"]
)
@pytest.mark.parametrize("entry", ["direct", "answer", "unattended"])
async def test_observation_precedes_control_and_remains_distinct_from_feedback(
    store, tmp_path, monkeypatch, boundary, entry
):
    effects = tmp_path / "external-effects.txt"
    identities = []
    attempt = 1 if entry == "direct" else 2

    def note(text):
        with effects.open("a", encoding="utf-8") as stream:
            stream.write(text + "\n")

    def tool_callback(call):
        note("tool")
        call.session_state.replace(state_schema_version=1, payload={"stage": "proposed"})
        return sdk.ToolOutput(
            content="external operation succeeded",
            structured={"receipt": "original"},
            session_control_transitions=[
                sdk.SessionControlTransition(action="acquire", controller_id="mode")
            ],
        )

    def observed(session_id):
        session = store.load_session_record(session_id)
        run = store.load_run(session_id, session.active_run_id)
        identity = run.active_tool_batch.identity(run.run_id, "call_state")
        outcome = store.load_tool_outcome(session_id, identity, attempt)
        assert outcome is not None
        assert outcome.value.content == "external operation succeeded"
        assert outcome.value.structured == {"receipt": "original"}
        assert not outcome.value.is_error
        assert not store.load_harness(session_id).state.states
        if not identities:
            identities.append(identity)
        return outcome

    def activate(call):
        # This callback runs in another thread: it can acquire the native lock
        # because observation persistence has finished before entering the plugin.
        with store.session_mutation(call.session_id):
            observed(call.session_id)
        note("controller")
        assert call.session_state.read().payload == {"stage": "proposed"}
        call.session_state.replace(state_schema_version=1, payload={"stage": "active"})
        if boundary == "controller_error":
            raise RuntimeError("controller refused activation")
        return sdk.SessionControllerOutput(
            output="framework control feedback", data={"active": True}
        )

    contributions = adapt_sdk_contributions(
        sdk.ContributionSet(
            tools=[
                sdk.ToolContribution(
                    id="observe",
                    exposed_name="Observe",
                    description="Observe before applying control.",
                    permission_mode="auto",
                    effect="state_changing",
                    handler=(
                        tool_callback
                        if entry == "direct"
                        else lambda _: sdk.ToolInputRequest(data={"question": "Continue?"})
                    ),
                    resume_handler=tool_callback if entry != "direct" else None,
                    supports_unattended=entry != "direct",
                )
            ],
            session_controllers=[
                sdk.SessionControllerContribution(
                    id="mode",
                    description="Session mode",
                    claims=(sdk.SessionControlClaim("example.mode", "exclusive"),),
                    activate=activate,
                    deactivate=lambda call: sdk.SessionControllerOutput(),
                )
            ],
        ),
        owner_id=OWNER,
    )
    tool = contributions.tools[0]
    tool = tool.model_copy(
        update={
            "definition": tool.definition.model_copy(
                update={
                    "metadata": {
                        **tool.definition.metadata,
                        "plugin_id": OWNER,
                        "plugin_class": "user",
                        "release_id": CONTRACT,
                        "package_digest": CONTRACT,
                        "contract_hash": CONTRACT,
                        "contribution_id": "observe",
                        "generation_id": "generation",
                        "execution_contract_hash": CONTRACT,
                    }
                }
            )
        }
    )
    controllers = PluginSessionControllerRegistry()
    controllers.replace_owner(
        "test",
        (
            replace(
                contributions.session_controllers[0],
                plugin_id=OWNER,
                release_id="release",
                generation_id="generation",
                execution_contract_hash=CONTRACT,
            ),
        ),
    )
    app = runtime(store, tool, {})
    catalog = build_extension_catalog(
        ExtensionCatalogBase.capture(
            tools=app.tools,
            observers=app.observers,
            commands=None,
            compaction=None,
            continuations=None,
            tool_policies=None,
            session_controllers=controllers,
        ),
        (),
    )

    async def attach_catalog(run):
        return await app._bind_extension_snapshot(run, catalog.acquire())

    async def ensure_catalog(run):
        if app._extension_snapshot_for_run(run.run_id) is None:
            await attach_catalog(run)

    monkeypatch.setattr(app, "_attach_extension_snapshot", attach_catalog)
    monkeypatch.setattr(app, "_ensure_extension_snapshot", ensure_catalog)
    session = SessionRecord(
        interaction_mode="unattended" if entry == "unattended" else "interactive"
    )
    store.create_session(session)
    native_commit = store.commit_session
    native_stage = app.session_controls.stage

    if boundary == "before_outcome":

        def interrupted_observation(request):
            if request.tool_facts.outcomes:
                identities.append(request.tool_facts.outcomes[0].identity)
                raise InterruptedRun
            return native_commit(request)

        monkeypatch.setattr(store, "commit_session", interrupted_observation)
    elif boundary == "before_stage":

        async def interrupted_stage(*args, **kwargs):
            observed(session.session_id)
            raise InterruptedRun

        monkeypatch.setattr(app.session_controls, "stage", interrupted_stage)
    elif boundary == "publication":

        def interrupted_publication(request):
            if any(step.message and step.message.role == "tool_result" for step in request.steps):
                raise InterruptedRun
            return native_commit(request)

        monkeypatch.setattr(store, "commit_session", interrupted_publication)

    async def execute():
        result = await app.run("observe", session_id=session.session_id)
        if entry == "answer":
            assert result.run.status == "waiting_user"
            result = await app.answer_user(
                session_id=session.session_id, run_id=result.run.run_id, answer="yes"
            )
        return result

    try:
        if boundary in {"before_outcome", "before_stage", "publication"}:
            with pytest.raises(InterruptedRun):
                await execute()
            monkeypatch.setattr(store, "commit_session", native_commit)
            monkeypatch.setattr(app.session_controls, "stage", native_stage)
            assert not store.load_harness(session.session_id).state.states
            recovered = await app.reconcile_execution_session(session.session_id)
            await app.run_recovery.reconcile(recovered)
        else:
            result = await execute()
            assert result.run.status == "completed", result.run.error
            assert identities, [item.structured for item in run_tool_results(store, result)]
        identity = identities[0]
        outcome = store.load_tool_outcome(session.session_id, identity, attempt)
        if attempt == 2:
            assert store.load_tool_outcome(session.session_id, identity, 1) is None
        if boundary == "before_outcome":
            assert outcome is None
            assert store.load_tool_call_start(session.session_id, identity, attempt) is not None
            state = store.load_tool_call_state(session.session_id, identity)
            assert state.status == "unknown_outcome"
            assert state.outcome_attempt is None
            message = store.load_message(session.session_id, state.result_message_id)
            assert message.outcome_ref.outcome_attempt is None
            assert "outcome is unknown" in message.text_content
            assert effects.read_text(encoding="utf-8").splitlines() == ["tool"]
            await app.run_recovery.reconcile(store.load_runtime_session(session.session_id))
            assert effects.read_text(encoding="utf-8").splitlines() == ["tool"]
            return
        assert outcome.value.content == "external operation succeeded"
        assert outcome.value.structured == {"receipt": "original"}
        assert not outcome.value.is_error
        messages = store.load_session(session.session_id).messages
        assert any(item.role == "tool_result" for item in messages), store.load_run(
            session.session_id, identity.run_id
        )
        message = next(item for item in messages if item.role == "tool_result")
        assert message.outcome_ref.outcome_attempt == attempt
        assert message.tool_feedback is not None
        assert message.as_fact().content == []
        assert "external operation succeeded" in message.text_content
        state = store.load_tool_call_state(session.session_id, identity)
        assert state.proposals is None
        assert state.feedback is None
        assert state.result_message_id == message.message_id
        materialized = load_batch_call(store, session.session_id, identity)
        assert materialized.result.text_content == message.text_content
        assert materialized.result.structured["receipt"] == "original"
        harness = store.load_harness(session.session_id).state
        if boundary == "success":
            assert "framework control feedback" in message.text_content
            assert not message.tool_result.is_error
            assert harness.states[0].payload == {"stage": "active"}
            assert len(harness.controls.activations) == 1
        else:
            assert message.tool_result.is_error
            assert not harness.states
            assert not harness.controls.activations
            if boundary == "controller_error":
                assert "controller refused activation" in message.text_content
            else:
                assert "not published before interruption" in message.text_content
                assert "framework control feedback" not in message.text_content
        assert effects.read_text(encoding="utf-8").splitlines() == (
            ["tool"] if boundary == "before_stage" else ["tool", "controller"]
        )
    finally:
        await app.close()
