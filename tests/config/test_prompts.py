import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from ouro.config.file import JsonConfigFile
from ouro.config.prompts import PromptConfig, PromptConfigManager, PromptDraft, PromptEntry
from ouro.core.errors import ConfigError
from ouro.prompts import DEFAULT_SYSTEM_PROMPT


def test_prompt_selection_order_and_independent_persistence(tmp_path):
    JsonConfigFile(tmp_path).write({"unrelated": {"keep": True}})
    manager = PromptConfigManager(tmp_path)
    assert manager.snapshot().config.resolve_system_prompt() == DEFAULT_SYSTEM_PROMPT
    ids = {}
    for revision, (kind, entry) in enumerate(
        (
            ("replacement", PromptEntry(id="one", text="First base", enabled=True)),
            ("replacement", PromptEntry(id="two", text="Second base", enabled=True)),
            (
                "addition",
                PromptEntry(id="a", description="Never send this label", text="A", enabled=True),
            ),
            ("addition", PromptEntry(id="off", text="Hidden")),
            ("addition", PromptEntry(id="b", text="B", enabled=True)),
        )
    ):
        saved = manager.save_entry(
            kind,
            PromptDraft.model_validate(entry.model_dump(exclude={"id"})),
            expected_revision=revision,
        )
        ids[entry.id] = saved.config.entries(kind)[-1].id
    config = PromptConfigManager(tmp_path).snapshot().config
    assert [entry.enabled for entry in config.replacements] == [False, True]
    assert config.system_prompt == "Second base"
    assert config.additional_system_prompt == "A\n\nB"
    assert config.resolve_system_prompt("CLI override") == "CLI override"
    assert config.resolve_system_prompt("") == ""
    moved = manager.move_entry("addition", ids["b"], 1, expected_revision=5).config
    assert moved.additional_system_prompt == "B\n\nA"
    disabled = manager.set_enabled("replacement", ids["two"], False, expected_revision=6).config
    assert disabled.resolve_system_prompt() == DEFAULT_SYSTEM_PROMPT
    updated = manager.save_entry(
        "addition",
        PromptDraft(text="Edited", enabled=True),
        entry_id=ids["a"],
        expected_revision=7,
    ).config
    assert [entry.id for entry in updated.additions] == [ids["b"], ids["a"], ids["off"]]
    removed = manager.remove_entry("addition", ids["b"], expected_revision=8).config
    assert removed.additional_system_prompt == "Edited"
    assert JsonConfigFile(tmp_path).read() == {"unrelated": {"keep": True}}
    assert (tmp_path / "config" / "prompts.json").exists()


@pytest.mark.parametrize(
    "raw",
    [
        {
            "replacements": [
                {"id": "a", "text": "a", "enabled": True},
                {"id": "b", "text": "b", "enabled": True},
            ]
        },
        {"additions": [{"id": "a", "text": "a"}, {"id": "a", "text": "b"}]},
        {"additions": [{"id": "a", "text": " "}]},
        {"additions": [{"id": "a"}]},
        {"additions": [{"id": "a", "text": "ok", "enabled": "false"}]},
        {"system": "old format"},
        {"unknown": True},
        {"revision": -1},
    ],
)
def test_invalid_prompt_file_is_rejected_without_modification(tmp_path, raw):
    file = JsonConfigFile(tmp_path, name="prompts.json")
    file.write({"prompts": raw})
    before = file.path.read_bytes()
    with pytest.raises(ConfigError):
        PromptConfigManager(tmp_path).snapshot()
    assert file.path.read_bytes() == before


@pytest.mark.parametrize("raw", [{"prompts": {"system": "old"}}, {"system_prompt": "old"}])
def test_shared_prompt_configuration_is_explicitly_rejected(tmp_path, raw):
    path = tmp_path / "config" / "config.json"
    path.parent.mkdir()
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ConfigError, match="prompts.json"):
        PromptConfigManager(tmp_path).snapshot()


def test_prompt_content_is_required_and_description_is_optional():
    assert PromptEntry(id="basic", text="  line one\nline two  ").description == ""
    for text in ("", " \n\t"):
        with pytest.raises(ValidationError):
            PromptEntry(id="basic", text=text)
    assert PromptConfig().additional_system_prompt == ""


def test_framework_generates_stable_ids_and_drafts_do_not_accept_them(tmp_path):
    manager = PromptConfigManager(tmp_path)
    draft = PromptDraft(text="Same content")
    first = manager.save_entry("addition", draft, expected_revision=0).config.additions[0]
    second = manager.save_entry("addition", draft, expected_revision=1).config.additions[1]
    assert first.id != second.id
    assert first.id.startswith("prompt_")
    updated = manager.save_entry(
        "addition", PromptDraft(text="Edited"), entry_id=first.id, expected_revision=2
    ).config
    assert updated.additions[0].id == first.id
    assert PromptConfigManager(tmp_path).snapshot().config == updated
    with pytest.raises(ValidationError):
        PromptDraft.model_validate({"id": "user-supplied", "text": "A"})
    with pytest.raises(ValidationError):
        PromptEntry.model_validate({"text": "Missing persisted identity"})


def test_stale_or_invalid_mutations_preserve_prompt_file(tmp_path):
    manager = PromptConfigManager(tmp_path)
    entry = PromptDraft(text="A", enabled=True)
    saved = manager.save_entry("replacement", entry, expected_revision=0)
    entry_id = saved.config.replacements[0].id
    before = manager.config_path.read_bytes()
    for operation in (
        lambda: manager.save_entry("replacement", entry, entry_id="missing", expected_revision=1),
        lambda: manager.remove_entry("replacement", "missing", expected_revision=1),
        lambda: manager.move_entry("replacement", entry_id, 0, expected_revision=1),
        lambda: manager.set_enabled("replacement", entry_id, False, expected_revision=0),
    ):
        with pytest.raises(ConfigError):
            operation()
        assert manager.config_path.read_bytes() == before


def test_concurrent_prompt_writers_reject_stale_revision(tmp_path):
    def save(identity):
        manager = PromptConfigManager(tmp_path)
        try:
            manager.save_entry(
                "replacement",
                PromptDraft(text=identity, enabled=True),
                expected_revision=0,
            )
        except ConfigError as exc:
            assert "revision conflict" in str(exc)
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(save, ("a", "b")))
    assert sorted(results) == [False, True]
    config = PromptConfigManager(tmp_path).snapshot().config
    assert config.revision == 1
    assert len(config.replacements) == 1
