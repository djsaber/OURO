from __future__ import annotations

import json
from pathlib import Path

import pytest

from ouro.core.errors import ConfigError
from ouro.core.input_content import InputAttachment, InputFileReference, InputSkillReference
from ouro.tui.composer.drafts import MAX_SESSION_DRAFTS, TuiInputDrafts, TuiInputDraftStore


def test_input_drafts_round_trip_and_keep_a_bounded_session_mru(tmp_path: Path) -> None:
    drafts = TuiInputDrafts().remember(None, "welcome draft")
    for index in range(MAX_SESSION_DRAFTS + 2):
        drafts = drafts.remember(f"ses_{index}", f"draft {index}")

    assert drafts.draft_for(None) == "welcome draft"
    assert tuple(draft.session_id for draft in drafts.sessions) == tuple(
        f"ses_{index}" for index in range(MAX_SESSION_DRAFTS + 1, 1, -1)
    )
    assert drafts.draft_for("ses_0") == ""

    updated = drafts.remember("ses_5", "updated draft")
    assert updated.sessions[0].session_id == "ses_5"
    assert updated.draft_for("ses_5") == "updated draft"
    assert updated.remember("ses_5", "updated draft") is updated

    cleared = updated.remember("ses_5", "").remember(None, "")
    assert cleared.draft_for("ses_5") == ""
    assert cleared.draft_for(None) == ""

    forgotten = cleared.forget_sessions(("ses_6", "ses_7"))
    assert forgotten.draft_for("ses_6") == ""
    assert forgotten.draft_for("ses_7") == ""

    store = TuiInputDraftStore(tmp_path)
    store.save(forgotten)

    assert store.path == tmp_path / "config" / "tui-input-drafts.json"
    assert store.load() == forgotten


def test_editing_and_reloading_a_draft_preserves_its_attachment_selection(tmp_path):
    store = TuiInputDraftStore(tmp_path)
    attachments = (InputAttachment(resource_id="res_1", name="notes.txt"),)
    skills = (InputSkillReference(name="demo"),)
    draft = TuiInputDrafts().remember("ses_1", "original", attachments=attachments, skills=skills)
    store.save(draft.remember("ses_1", "edited", attachments=attachments, skills=skills))
    loaded = store.load()
    assert loaded.resources_for("ses_1") == ("res_1",)
    assert loaded.draft_for("ses_1") == "edited"
    assert loaded.content_for("ses_1").skills == skills
    assert loaded.resources_for("ses_2") == ()
    assert loaded.remember("ses_1", "", attachments=attachments).resources_for("ses_1") == (
        "res_1",
    )
    assert loaded.remember("ses_1", "").resources_for("ses_1") == ()


def test_old_draft_format_is_diagnosed_without_rewriting_it(tmp_path):
    store = TuiInputDraftStore(tmp_path)
    store.path.parent.mkdir(parents=True)
    original = '{"schema_version": 1, "welcome": "old draft"}'
    store.path.write_text(original, encoding="utf-8")
    with pytest.raises(ConfigError, match="schema_version"):
        store.load()
    assert store.path.read_text(encoding="utf-8") == original


def test_welcome_reference_survives_restart_with_its_workspace_binding(tmp_path):
    reference = InputFileReference(
        name="SECURITY.md",
        path=str(tmp_path / "SECURITY.md"),
        workspace=str(tmp_path),
    )
    drafts = TuiInputDrafts().remember(None, "", attachments=(reference,))
    store = TuiInputDraftStore(tmp_path / "home")
    store.save(drafts)
    restored = store.load().content_for(None)
    assert restored.attachments == (reference,)
    assert restored.attachments[0].label == "@SECURITY.md"
    assert not restored.is_empty and restored.resource_refs == ()


@pytest.mark.parametrize(
    "raw",
    (
        {"schema_version": 4, "sessions": [{"session_id": "", "text": "draft"}]},
        {"schema_version": 4, "sessions": [{"session_id": "ses_1", "text": ""}]},
        {
            "schema_version": 4,
            "sessions": [
                {"session_id": "ses_1", "text": "first"},
                {"session_id": "ses_1", "text": "second"},
            ],
        },
        {
            "schema_version": 4,
            "sessions": [
                {"session_id": f"ses_{index}", "text": "draft"}
                for index in range(MAX_SESSION_DRAFTS + 1)
            ],
        },
    ),
)
def test_input_drafts_reject_invalid_persisted_state(
    tmp_path: Path,
    raw: dict[str, object],
) -> None:
    store = TuiInputDraftStore(tmp_path)
    store.path.parent.mkdir(parents=True)
    store.path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigError, match="invalid TUI input drafts"):
        store.load()
