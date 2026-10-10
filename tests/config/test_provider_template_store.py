from __future__ import annotations

import json
from pathlib import Path

import pytest

import ouro.config.provider_template_store as store_module
from ouro.config.provider_template_store import (
    SCHEMA_VERSION,
    ProviderTemplateStore,
    template_fingerprint,
)
from ouro.config.provider_templates import ProviderTemplate, provider_templates
from ouro.core.errors import ConfigError

_INLINE_BY_ID = {template.template_id: template for template in provider_templates()}


def _file_path(tmp_path: Path) -> Path:
    return tmp_path / "config" / "provider-templates.json"


def _read_json(tmp_path: Path) -> dict:
    return json.loads(_file_path(tmp_path).read_text(encoding="utf-8"))


def _write_json(tmp_path: Path, root: dict) -> None:
    _file_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    _file_path(tmp_path).write_text(
        json.dumps(root, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _revise(template: ProviderTemplate, **updates: object) -> ProviderTemplate:
    return template.model_copy(update={"revision": template.revision + 1, **updates})


def _fake_inline(monkeypatch, templates: tuple[ProviderTemplate, ...]) -> None:
    monkeypatch.setattr(store_module, "inline_provider_templates", lambda: templates)


def _custom_entry() -> dict:
    return {
        "template_id": "mygw",
        "display_name": "My Gateway",
        "description": "Custom gateway template.",
        "provider_type": "custom",
        "protocol": "openai-chat-completions",
        "credential_env": "MYGW_API_KEY",
        "base_url": "https://gw.example.test/v1",
        "api_path": "/chat/completions",
        "default_endpoint": "default",
        "endpoints": [{"endpoint_id": "default", "model_id": "my-model"}],
        "documentation_url": "https://gw.example.test/docs",
        "verified_on": "2026-08-18",
        "revision": 1,
        "meta": {"origin": "custom"},
    }


def test_catalog_materializes_missing_file_from_inline_catalog(tmp_path) -> None:
    store = ProviderTemplateStore(tmp_path)

    snapshot = store.catalog()

    assert {entry.template.template_id for entry in snapshot.entries} == set(_INLINE_BY_ID)
    assert any("created" in notice for notice in snapshot.notices)
    root = _read_json(tmp_path)
    assert root["schema_version"] == SCHEMA_VERSION
    for template_id, template in _INLINE_BY_ID.items():
        entry = root["templates"][template_id]
        meta = entry["meta"]
        assert meta["origin"] == "inline"
        assert meta["baseline_revision"] == template.revision
        assert meta["deprecated"] is False
        assert meta["enabled"] is True
        business = {key: value for key, value in entry.items() if key != "meta"}
        assert business == template.model_dump(mode="json")
        assert meta["baseline_fingerprint"] == template_fingerprint(business)


def test_catalog_is_idempotent_when_already_synchronized(tmp_path) -> None:
    store = ProviderTemplateStore(tmp_path)
    store.catalog()
    first_text = _file_path(tmp_path).read_text(encoding="utf-8")

    snapshot = store.catalog()

    assert snapshot.notices == ()
    assert _file_path(tmp_path).read_text(encoding="utf-8") == first_text


def test_inline_catalog_revisions_start_monotonic() -> None:
    assert all(template.revision >= 1 for template in provider_templates())


def test_fingerprint_normalizes_key_order() -> None:
    assert template_fingerprint({"b": 2, "a": {"y": 1, "x": 2}}) == template_fingerprint(
        {"a": {"x": 2, "y": 1}, "b": 2}
    )


def test_formatting_only_edits_still_upgrade_to_new_inline_revision(tmp_path, monkeypatch) -> None:
    store = ProviderTemplateStore(tmp_path)
    store.catalog()
    reformatted = json.dumps(_read_json(tmp_path), ensure_ascii=False, indent=4)
    _file_path(tmp_path).write_text(reformatted, encoding="utf-8")
    bumped = _revise(_INLINE_BY_ID["openai"], display_name="OpenAI v2")
    _fake_inline(
        monkeypatch,
        tuple(
            bumped if template.template_id == "openai" else template
            for template in provider_templates()
        ),
    )

    snapshot = store.catalog()

    openai = next(entry for entry in snapshot.entries if entry.template.template_id == "openai")
    assert openai.template.display_name == "OpenAI v2"
    assert any("updated to inline revision" in notice for notice in snapshot.notices)
    entry = _read_json(tmp_path)["templates"]["openai"]
    assert entry["display_name"] == "OpenAI v2"
    assert entry["meta"]["baseline_revision"] == bumped.revision


def test_qwen_revision_upgrade_replaces_incomplete_cached_template(tmp_path, monkeypatch) -> None:
    latest = _INLINE_BY_ID["qwen"]
    incomplete = latest.model_copy(
        update={
            "revision": 2,
            "endpoints": tuple(
                endpoint for endpoint in latest.endpoints if endpoint.endpoint_id != "qwen3.7-plus"
            ),
        }
    )
    _fake_inline(
        monkeypatch,
        tuple(
            incomplete if template.template_id == "qwen" else template
            for template in provider_templates()
        ),
    )
    store = ProviderTemplateStore(tmp_path)
    initial = store.catalog()
    initial_qwen = next(entry for entry in initial.entries if entry.template.template_id == "qwen")
    assert {endpoint.endpoint_id for endpoint in initial_qwen.template.endpoints} == {
        "qwen3.8-max",
        "qwen3.8-flash",
    }

    _fake_inline(monkeypatch, provider_templates())
    upgraded = store.catalog()

    qwen = next(entry for entry in upgraded.entries if entry.template.template_id == "qwen")
    assert {endpoint.endpoint_id for endpoint in qwen.template.endpoints} == {
        "qwen3.8-max",
        "qwen3.8-flash",
        "qwen3.7-plus",
    }
    assert any("updated to inline revision" in notice for notice in upgraded.notices)
    entry = _read_json(tmp_path)["templates"]["qwen"]
    assert entry["meta"]["baseline_revision"] == latest.revision


def test_user_modified_template_is_preserved_with_revision_advanced(tmp_path, monkeypatch) -> None:
    store = ProviderTemplateStore(tmp_path)
    store.catalog()
    root = _read_json(tmp_path)
    materialized_fingerprint = root["templates"]["openai"]["meta"]["baseline_fingerprint"]
    root["templates"]["openai"]["display_name"] = "My OpenAI fork"
    _write_json(tmp_path, root)
    bumped = _revise(_INLINE_BY_ID["openai"], display_name="OpenAI v2")
    _fake_inline(
        monkeypatch,
        tuple(
            bumped if template.template_id == "openai" else template
            for template in provider_templates()
        ),
    )

    snapshot = store.catalog()

    openai = next(entry for entry in snapshot.entries if entry.template.template_id == "openai")
    assert openai.template.display_name == "My OpenAI fork"
    assert any("keeps the user version" in notice for notice in snapshot.notices)
    meta = _read_json(tmp_path)["templates"]["openai"]["meta"]
    assert meta["baseline_revision"] == bumped.revision
    assert meta["baseline_fingerprint"] == materialized_fingerprint

    again = store.catalog()
    assert not any("keeps the user version" in notice for notice in again.notices)


def test_overwrite_replaces_business_fields_but_keeps_user_metadata(tmp_path, monkeypatch) -> None:
    store = ProviderTemplateStore(tmp_path)
    store.catalog()
    root = _read_json(tmp_path)
    root["templates"]["openai"]["meta"]["enabled"] = False
    _write_json(tmp_path, root)
    bumped = _revise(_INLINE_BY_ID["openai"], display_name="OpenAI v2")
    _fake_inline(
        monkeypatch,
        tuple(
            bumped if template.template_id == "openai" else template
            for template in provider_templates()
        ),
    )

    snapshot = store.catalog()

    openai = next(entry for entry in snapshot.entries if entry.template.template_id == "openai")
    assert openai.template.display_name == "OpenAI v2"
    assert openai.enabled is False
    entry = _read_json(tmp_path)["templates"]["openai"]
    assert entry["meta"]["enabled"] is False


def test_custom_template_wins_id_conflict_against_inline(tmp_path, monkeypatch) -> None:
    store = ProviderTemplateStore(tmp_path)
    store.catalog()
    root = _read_json(tmp_path)
    root["templates"]["glm"]["meta"]["origin"] = "custom"
    root["templates"]["glm"]["display_name"] = "My GLM"
    _write_json(tmp_path, root)
    bumped = _revise(_INLINE_BY_ID["glm"], display_name="GLM v2")
    _fake_inline(
        monkeypatch,
        tuple(
            bumped if template.template_id == "glm" else template
            for template in provider_templates()
        ),
    )

    snapshot = store.catalog()

    glm = next(entry for entry in snapshot.entries if entry.template.template_id == "glm")
    assert glm.origin == "custom"
    assert glm.template.display_name == "My GLM"
    assert any("user-defined template wins" in notice for notice in snapshot.notices)


def test_template_left_inline_is_deprecated_and_flag_cleared_on_return(
    tmp_path, monkeypatch
) -> None:
    store = ProviderTemplateStore(tmp_path)
    store.catalog()
    without_mistral = tuple(
        template for template in provider_templates() if template.template_id != "mistral"
    )
    _fake_inline(monkeypatch, without_mistral)

    deprecated = store.catalog()

    mistral = next(entry for entry in deprecated.entries if entry.template.template_id == "mistral")
    assert mistral.deprecated is True
    assert any("no longer in the inline" in notice for notice in deprecated.notices)
    assert _read_json(tmp_path)["templates"]["mistral"]["meta"]["deprecated"] is True

    monkeypatch.undo()
    restored = store.catalog()

    mistral = next(entry for entry in restored.entries if entry.template.template_id == "mistral")
    assert mistral.deprecated is False
    assert any("available again" in notice for notice in restored.notices)

    settled = store.catalog()
    assert settled.notices == ()


def test_broken_template_entry_is_skipped_and_preserved_in_file(tmp_path) -> None:
    store = ProviderTemplateStore(tmp_path)
    store.catalog()
    root = _read_json(tmp_path)
    root["templates"]["glm"]["endpoints"] = []
    _write_json(tmp_path, root)

    snapshot = store.catalog()

    ids = {entry.template.template_id for entry in snapshot.entries}
    assert "glm" not in ids
    assert ids == set(_INLINE_BY_ID) - {"glm"}
    assert any(
        "'glm' skipped" in notice and "validation failed" in notice for notice in snapshot.notices
    )
    assert _read_json(tmp_path)["templates"]["glm"]["endpoints"] == []


def test_syntax_error_falls_back_to_inline_without_touching_file(tmp_path) -> None:
    _file_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    _file_path(tmp_path).write_text("{ not json", encoding="utf-8")
    store = ProviderTemplateStore(tmp_path)

    snapshot = store.catalog()

    assert {entry.template.template_id for entry in snapshot.entries} == set(_INLINE_BY_ID)
    assert any("failed to load" in notice for notice in snapshot.notices)
    assert _file_path(tmp_path).read_text(encoding="utf-8") == "{ not json"


def test_newer_structure_version_is_rejected_without_writing(tmp_path) -> None:
    store = ProviderTemplateStore(tmp_path)
    store.catalog()
    root = _read_json(tmp_path)
    root["schema_version"] = SCHEMA_VERSION + 98
    root["future_root"] = {"kept": True}
    root["templates"]["openai"]["future_field"] = 7
    _write_json(tmp_path, root)
    written_text = _file_path(tmp_path).read_text(encoding="utf-8")

    snapshot = store.catalog()

    assert {entry.template.template_id for entry in snapshot.entries} == set(_INLINE_BY_ID)
    assert any("unsupported provider template schema" in notice for notice in snapshot.notices)
    assert _file_path(tmp_path).read_text(encoding="utf-8") == written_text


def test_unknown_fields_and_root_keys_survive_synchronized_rewrites(tmp_path, monkeypatch) -> None:
    store = ProviderTemplateStore(tmp_path)
    store.catalog()
    root = _read_json(tmp_path)
    root["future_root"] = {"kept": True}
    root["templates"]["mistral"]["future_field"] = 7
    root["templates"]["mygw"] = _custom_entry()
    _write_json(tmp_path, root)
    bumped = _revise(_INLINE_BY_ID["qwen"], display_name="Qwen v2")
    _fake_inline(
        monkeypatch,
        tuple(
            bumped if template.template_id == "qwen" else template
            for template in provider_templates()
        ),
    )

    snapshot = store.catalog()

    ids = {entry.template.template_id for entry in snapshot.entries}
    assert ids == set(_INLINE_BY_ID) | {"mygw"}
    rewritten = _read_json(tmp_path)
    assert rewritten["future_root"] == {"kept": True}
    assert rewritten["templates"]["mistral"]["future_field"] == 7
    assert rewritten["templates"]["mygw"]["meta"]["origin"] == "custom"
    assert rewritten["templates"]["qwen"]["display_name"] == "Qwen v2"


def test_empty_templates_object_refills_from_inline(tmp_path) -> None:
    _write_json(tmp_path, {"schema_version": SCHEMA_VERSION})
    store = ProviderTemplateStore(tmp_path)

    snapshot = store.catalog()

    assert {entry.template.template_id for entry in snapshot.entries} == set(_INLINE_BY_ID)
    assert any("added from the inline catalog" in notice for notice in snapshot.notices)
    assert set(_read_json(tmp_path)["templates"]) == set(_INLINE_BY_ID)


@pytest.mark.parametrize("version", [0, 3])
def test_old_structure_version_is_rejected_without_writing(tmp_path, version) -> None:
    store = ProviderTemplateStore(tmp_path)
    store.catalog()
    root = _read_json(tmp_path)
    root["schema_version"] = version
    _write_json(tmp_path, root)
    written_text = _file_path(tmp_path).read_text(encoding="utf-8")

    snapshot = store.catalog()

    assert {entry.template.template_id for entry in snapshot.entries} == set(_INLINE_BY_ID)
    assert any("unsupported provider template schema" in notice for notice in snapshot.notices)
    assert _file_path(tmp_path).read_text(encoding="utf-8") == written_text


def test_get_is_case_insensitive_and_reports_available_templates(tmp_path) -> None:
    store = ProviderTemplateStore(tmp_path)

    assert store.get("OpenAI").template_id == "openai"

    with pytest.raises(ConfigError, match="provider template not found: nope"):
        store.get("nope")


def test_restore_replaces_user_overlay_with_inline_content(tmp_path, monkeypatch) -> None:
    store = ProviderTemplateStore(tmp_path)
    store.catalog()
    root = _read_json(tmp_path)
    root["templates"]["openai"]["display_name"] = "My OpenAI fork"
    _write_json(tmp_path, root)
    bumped = _revise(_INLINE_BY_ID["openai"], display_name="OpenAI v2")
    _fake_inline(
        monkeypatch,
        tuple(
            bumped if template.template_id == "openai" else template
            for template in provider_templates()
        ),
    )
    store.catalog()

    restored = store.restore("OpenAI")

    assert restored.display_name == "OpenAI v2"
    entry = _read_json(tmp_path)["templates"]["openai"]
    assert entry["display_name"] == "OpenAI v2"
    assert entry["meta"]["baseline_revision"] == bumped.revision
    settled = store.catalog()
    assert not any("openai" in notice for notice in settled.notices)


def test_restore_rejects_templates_missing_from_inline_catalog(tmp_path) -> None:
    store = ProviderTemplateStore(tmp_path)
    store.catalog()
    root = _read_json(tmp_path)
    root["templates"]["mygw"] = _custom_entry()
    _write_json(tmp_path, root)

    with pytest.raises(ConfigError, match="not found in the inline catalog: mygw"):
        store.restore("mygw")


def test_plaintext_credential_in_template_produces_notice(tmp_path) -> None:
    store = ProviderTemplateStore(tmp_path)
    store.catalog()
    root = _read_json(tmp_path)
    root["templates"]["glm"]["endpoints"][0]["request_options"] = {"api_key": "sk-live-secret"}
    _write_json(tmp_path, root)

    snapshot = store.catalog()

    assert any("possible plaintext credential" in notice for notice in snapshot.notices)
    glm = next(entry for entry in snapshot.entries if entry.template.template_id == "glm")
    assert glm.template.endpoints[0].request_options["api_key"] == "sk-live-secret"


@pytest.mark.parametrize("missing", ["baseline_revision", "baseline_fingerprint", "meta"])
def test_catalog_does_not_invent_missing_content_baseline(tmp_path, monkeypatch, missing):
    store = ProviderTemplateStore(tmp_path)
    store.catalog()
    root = _read_json(tmp_path)
    entry = root["templates"]["openai"]
    if missing == "meta":
        entry.pop("meta")
    else:
        entry["meta"].pop(missing)
    _write_json(tmp_path, root)
    before = _file_path(tmp_path).read_bytes()
    bumped = _revise(_INLINE_BY_ID["openai"], display_name="Updated upstream")
    _fake_inline(
        monkeypatch, tuple(bumped if t.template_id == "openai" else t for t in provider_templates())
    )

    store.catalog()

    assert _file_path(tmp_path).read_bytes() == before


@pytest.mark.parametrize("version", [None, 0, 3, SCHEMA_VERSION + 1])
def test_restore_does_not_relabel_an_unsupported_template_file(tmp_path, version):
    store = ProviderTemplateStore(tmp_path)
    store.catalog()
    root = _read_json(tmp_path)
    root["schema_version"] = version
    _write_json(tmp_path, root)
    before = _file_path(tmp_path).read_bytes()
    with pytest.raises(ConfigError, match="unsupported provider template schema"):
        store.restore("openai")
    assert _file_path(tmp_path).read_bytes() == before
