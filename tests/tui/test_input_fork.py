from __future__ import annotations

from html import escape

import pytest

from ouro.application.skill_activations import SkillInvocationRequest
from ouro.core.input_content import (
    InputAttachment,
    InputContent,
    InputFileReference,
    InputSkillReference,
)
from ouro.tui.app import OuroTuiApp
from ouro.tui.widgets import Composer
from tests.application.test_skill_activations import CapturingProvider
from tests.tui.helpers import run_ready_app
from tests.tui.test_history_commands import application_at


@pytest.mark.parametrize("backend", ("files", "sqlite"))
@pytest.mark.parametrize("kind", ("mixed", "upload_only", "reference_only", "skill_only"))
async def test_fork_and_undo_restore_complete_input_without_reupload(
    tmp_path, monkeypatch, backend, kind
):
    skill = tmp_path / "skills" / "reviewer" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: reviewer\ndescription: Review.\n---\nReview with this workflow.\n",
        encoding="utf-8",
    )
    provider = CapturingProvider()
    application = application_at(tmp_path, provider, backend=backend)
    runtime = application.runtime
    session = runtime.create_session(title="fork inputs", workspace=tmp_path)
    await runtime.run("Earlier question", session_id=session.session_id)
    retained = runtime.store.load_runtime_session(session.session_id).messages

    source = tmp_path / "上传 文件.txt"
    source.write_text("Uploaded evidence.", encoding="utf-8")
    reference = tmp_path / "引用 文件.txt"
    reference.write_text("Referenced evidence.", encoding="utf-8")
    files = []
    if kind in {"mixed", "upload_only"}:
        resource = runtime.resources.import_resource(session.session_id, source).resource
        files.append(InputAttachment(name=source.name, resource_id=resource.resource_id))
    if kind in {"mixed", "reference_only"}:
        files.append(
            InputFileReference(name=reference.name, path=str(reference), workspace=str(tmp_path))
        )
    original = InputContent(
        text="Inspect this" if kind == "mixed" else "",
        attachments=tuple(files),
        skills=(InputSkillReference(name="reviewer", order=1),)
        if kind in {"mixed", "skill_only"}
        else (),
    )
    original = InputContent.from_elements(original.text, original.elements)
    await runtime.run(
        original.text,
        session_id=session.session_id,
        attachments=original.attachments,
        skill_invocations=tuple(
            SkillInvocationRequest(arguments=original.text, **item.model_dump())
            for item in original.skills
        ),
    )
    before = runtime.store.load_runtime_session(session.session_id).messages
    source.unlink()  # Fork and resubmit must use the owned upload, not the original file.

    def unexpected_upload(*args, **kwargs):
        pytest.fail("Restoring a fork or undo must not upload the attachment again")

    monkeypatch.setattr(runtime.resources, "import_resource", unexpected_upload)
    app = OuroTuiApp(application=application, resume_session_id=session.session_id)
    async with run_ready_app(app) as pilot:
        points = await app._controller.branch_points()
        assert len(points) == 2
        assert [item.value for item in app._fork_arguments()] == ["full", "#1", "#2"]
        await app._fork_session("#2").wait()
        await pilot.pause()
        branch_id = app._controller.session_id
        assert branch_id != session.session_id
        composer = app.query_one(Composer)
        assert composer.input_content == original
        await app._open_session(session.session_id)
        await app._open_session(branch_id)
        assert composer.input_content == original
        copied = runtime.store.load_runtime_session(branch_id).messages
        assert [m.text_content for m in copied] == [m.text_content for m in retained]
        assert runtime.store.load_runtime_session(session.session_id).messages == before
        resources = runtime.resources.list_resources(branch_id).resources
        assert len(resources) == (1 if kind in {"mixed", "upload_only"} else 0)
        upload_paths = [
            runtime.resources.resource_path(branch_id, r.resource_id) for r in resources
        ]
        runtime.sessions.trash(session.session_id)

        for attempt in range(3):
            await pilot.press("enter")
            await app.workers.wait_for_complete([w for w in app.workers if w.group == "run"])
            await pilot.pause()
            sent = "\n".join(m.text_content for m in provider.calls[-1])
            for path in upload_paths:
                assert (
                    path.is_absolute() and path.read_text(encoding="utf-8") == "Uploaded evidence."
                )
                assert path.as_posix() in sent
                assert sent.count("Uploaded evidence.") == 1
            if kind in {"mixed", "reference_only"}:
                assert reference.as_posix() in sent
                assert sent.count("Referenced evidence.") == 1
            if original.skills:
                assert escape(str(skill), quote=True) in sent
                assert sent.count("Review with this workflow.") == 1
            assert runtime.resources.list_resources(branch_id).resources == resources
            assert sorted(runtime.resources.directory_for(branch_id).glob("*")) == sorted(
                upload_paths
            )
            if attempt < 2:
                await app._edit_session_history("/undo").wait()
                await pilot.pause()
                assert composer.input_content == original
                assert len(runtime.store.load_runtime_session(branch_id).messages) == len(retained)
