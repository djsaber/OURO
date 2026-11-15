from __future__ import annotations

import pytest

from ouro.application.inputs.requests import UserInputRequest
from ouro.application.runtime.service import RuntimeService
from ouro.core.adapter import RuntimeAdapter
from ouro.core.errors import PromptRejectedError
from ouro.core.prompt_policies import (
    PromptPolicyDecision,
    PromptPolicyRegistration,
    PromptPolicyRegistry,
)
from ouro.core.provider import ModelMetadata
from ouro.providers import FauxProvider
from ouro.sessions.backends.files.store import LocalSessionStore
from ouro.sessions.backends.sqlite.store import SQLiteSessionStore
from ouro.tools import ToolRegistry


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize("entry", ["accept", "run"])
@pytest.mark.parametrize("failure", ["deny", "commit"])
async def test_failed_input_preserves_session_settings(
    tmp_path, monkeypatch, backend, entry, failure
):
    store = (
        LocalSessionStore(tmp_path)
        if backend == "files"
        else SQLiteSessionStore(tmp_path / "sessions.db")
    )
    store.initialize()
    policies = PromptPolicyRegistry()
    if failure == "deny":
        policies.register(
            PromptPolicyRegistration(
                "ouro.host",
                "guard",
                lambda call: PromptPolicyDecision(action="deny", reason="deny"),
            )
        )
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(FauxProvider(), ModelMetadata(model_id="test", provider_id="faux")),
        tools=ToolRegistry(),
        prompt_policies=policies,
    )
    try:
        session = runtime.create_session()
        commit = store.commit_session

        def reject_input(request):
            if request.inputs:
                raise OSError("input commit failed")
            return commit(request)

        if failure == "commit":
            monkeypatch.setattr(store, "commit_session", reject_input)
        with pytest.raises(PromptRejectedError if failure == "deny" else OSError):
            if entry == "accept":
                await runtime.accept_user_input(
                    UserInputRequest(
                        prompt="input",
                        session_id=session.session_id,
                        approval_mode="auto",
                        interaction_mode="unattended",
                    )
                )
            else:
                await runtime.run(
                    "input",
                    session_id=session.session_id,
                    approval_mode="auto",
                    interaction_mode="unattended",
                )
        after = store.load_session_record(session.session_id)
        assert after.approval_mode == session.approval_mode
        assert after.interaction_mode == session.interaction_mode
        assert not after.pending_input_ids
        assert after.message_count == 0
    finally:
        await runtime.close()


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize("entry", ["accept", "run"])
async def test_successful_input_publishes_settings_with_its_identity(tmp_path, backend, entry):
    store = (
        LocalSessionStore(tmp_path)
        if backend == "files"
        else SQLiteSessionStore(tmp_path / "sessions.db")
    )
    store.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(FauxProvider(), ModelMetadata(model_id="test", provider_id="faux")),
        tools=ToolRegistry(),
    )
    try:
        session = runtime.create_session()
        request = UserInputRequest(
            prompt="input",
            session_id=session.session_id,
            approval_mode="auto",
            interaction_mode="unattended",
        )
        if entry == "accept":
            receipt = await runtime.accept_user_input(request)
            assert (await runtime.accept_user_input(request)) == receipt
        else:
            await runtime.run(
                request.prompt,
                session_id=session.session_id,
                input_id=request.input_id,
                approval_mode=request.approval_mode,
                interaction_mode=request.interaction_mode,
            )
        after = store.load_session_record(session.session_id)
        assert (after.approval_mode, after.interaction_mode) == ("auto", "unattended")
        assert after.updated_at > session.updated_at
        assert after.last_activity_at > session.last_activity_at
        assert store.load_input(session.session_id, request.input_id) is not None
        assert store.load_input_state(session.session_id, request.input_id).status == (
            "pending" if entry == "accept" else "consumed"
        )
    finally:
        await runtime.close()
