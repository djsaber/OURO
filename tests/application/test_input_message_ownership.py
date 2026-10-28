from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest

from ouro.application.inputs.requests import UserInputRequest
from ouro.application.inputs.skills import SkillInvocationRequest
from ouro.core.input_content import InputFileReference
from ouro.sessions.portable import PortableSession, SessionBundle
from tests.tui.test_history_commands import application_at


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize("body", [False, True])
async def test_input_owns_body_and_attachments_across_history_branch_and_export(
    tmp_path, backend, body
):
    home = tmp_path / "home"
    skill = home / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    instructions = "Frozen skill instruction with unique ownership."
    skill.write_text(
        f"---\nname: demo\ndescription: Review data\n---\n{instructions}\n", encoding="utf-8"
    )
    source = tmp_path / "evidence.txt"
    observed = "Previously observed file evidence."
    source.write_text(observed, encoding="utf-8")
    prompt = "Accepted prompt with unique ownership." if body else ""
    attachment = InputFileReference(name=source.name, path=str(source), text_offset=len(prompt))
    request = UserInputRequest(
        prompt=prompt,
        workspace=str(tmp_path),
        attachments=(attachment,),
        skill_invocations=(SkillInvocationRequest(name="demo", arguments=prompt),) if body else (),
    )
    app = application_at(home, backend=backend)
    try:
        submission = await app.supervisor.submit_user_input(request)
        result = await submission.wait()
        assert result.run.status == "completed", result.run.error
        session_id = result.session.session_id
        store = app.runtime.store
        state = store.load_input_state(session_id, request.input_id)
        assert state is not None and state.status == "consumed"
        messages = [store.load_message(session_id, identity) for identity in state.message_ids]
        assert len(messages) == (2 if body else 1)
        assert all(message.input_id == request.input_id for message in messages)
        assert observed in messages[0].text_content
        if body:
            assert messages[1].text_content == prompt
            assert instructions in messages[1].skill_activations[0].instructions
            assert messages[1].input_attachments == (attachment,)
        retry = await app.supervisor.submit_user_input(request)
        assert retry.receipt.message_id == messages[-1].message_id
        snapshot = store.load_session(session_id)
        consumed = [
            event
            for event in snapshot.events
            if event.payload.get("queued_input_id") == request.input_id
        ]
        assert consumed and all(
            event.payload["message_id"] == messages[-1].message_id for event in consumed
        )

        if backend == "files":
            directory = store.sessions_dir / session_id
            payloads = (directory / "messages.jsonl").read_text("utf-8").splitlines()
            assert not (directory / "operations").exists()
        else:
            with closing(sqlite3.connect(store.database)) as connection:
                payloads = [row[0] for row in connection.execute("SELECT payload FROM messages")]
                assert (
                    connection.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='table' AND name='session_commits'"
                    ).fetchone()
                    is None
                )
        facts = [
            json.loads(payload) for payload in payloads if json.loads(payload).get("input_ref")
        ]
        assert len(facts) == len(messages)
        assert all(not fact.get("input_attachments") for fact in facts)
        if body:
            assert not next(fact for fact in facts if fact["input_ref"]["part"] == "body").get(
                "content"
            )

        portable = PortableSession.from_snapshot(snapshot)
        bundle = SessionBundle(sessions=[portable])
        encoded = bundle.model_dump_json()
        assert sum(
            item.model_dump_json().count(instructions) for item in portable.snapshot.inputs
        ) == (1 if body else 0)
        assert all(
            instructions not in item.model_dump_json() for item in portable.snapshot.messages
        )
        # The latest input owns the actual transformed skill envelope sent to the Provider.
        assert portable.snapshot.latest_manifest is not None
        assert portable.snapshot.latest_manifest.model_dump_json().count(instructions) == (
            1 if body else 0
        )
        assert encoded.count(instructions) == (2 if body else 0)
        assert all(
            fact.input_attachments == () for fact in portable.snapshot.messages if fact.input_ref
        )
        assert (
            SessionBundle.model_validate_json(encoded).sessions[0].snapshot.materialize()
            == snapshot
        )

        # Query, branch and undo restore accepted units and observed file data;
        # they cannot execute the skill resolver or read the changed source.
        source.unlink()
        skill.unlink()
        assert [
            store.load_message(session_id, identity) for identity in state.message_ids
        ] == messages
        branch = app.runtime.sessions.branch(session_id, messages[-1].message_id)
        branch_store = store.load_session(branch.session.session_id)
        assert branch_store.messages[0].content == messages[0].content
        branch_state = branch_store.input_states[0]
        assert branch_state.message_ids == tuple(
            message.message_id for message in branch_store.messages
        )
        undone = app.runtime.sessions.undo(session_id)
        assert undone.text_content == prompt
        assert undone.attachments == (attachment,)
        assert store.load_input_state(session_id, request.input_id).status == "withdrawn"
    finally:
        await app.close()
