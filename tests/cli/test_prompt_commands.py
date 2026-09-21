import json

from ouro.cli.main import main
from ouro.config.prompts import PromptConfigManager


def test_cli_prompt_management_preserves_content_and_uses_only_enabled_entries(tmp_path, capsys):
    source = tmp_path / "instructions.md"
    source.write_text("Line one\nLine two", encoding="utf-8-sig")

    def command(*args):
        result = main(["config", "prompt", *args, "--home", str(tmp_path), "--json"])
        return result, json.loads(capsys.readouterr().out)

    code, payload = command("add", "replacement", "--text", "Base", "--enabled")
    assert code == 0 and payload["system_prompt"] == "Base"
    assert payload["change"]["effective_from"] == "next_request"
    assert command("add", "addition", "--file", str(source), "--enabled")[0] == 0
    assert command("add", "addition", "--text", "Last", "--enabled")[0] == 0
    code, payload = command("edit", "addition", "1", "--description", "UI only")
    assert code == 0
    assert payload["effective_system_prompt"] == "Base\n\nLine one\nLine two\n\nLast"
    assert "UI only" not in payload["effective_system_prompt"]
    code, payload = command("move", "addition", "2", "1")
    assert code == 0 and payload["additional_system_prompt"] == "Last\n\nLine one\nLine two"
    assert command("disable", "addition", "1")[0] == 0
    assert command("remove", "addition", "1")[0] == 0
    code, payload = command("list")
    assert code == 0 and len(payload["config"]["additions"]) == 1
    assert PromptConfigManager(tmp_path).snapshot().config.additions[0].description == "UI only"
    assert command("disable", "replacement", "1")[0] == 0
    code, payload = command("enable", "replacement", "1")
    assert code == 0 and payload["configured"]


def test_cli_invalid_or_stale_prompt_edits_do_not_write(tmp_path, capsys):
    prefix = ["config", "prompt", "--home", str(tmp_path), "--json"]
    assert main([*prefix, "add", "addition", "--text", "A"]) == 0
    capsys.readouterr()
    file = tmp_path / "config" / "prompts.json"
    before = file.read_bytes()
    for args in (
        ["edit", "addition", "2", "--text", "Missing"],
        ["edit", "addition", "1", "--text", " "],
        ["edit", "addition", "1", "--text", "New", "--expected-revision", "0"],
        ["edit", "addition", "1", "--file", str(tmp_path / "missing")],
        ["move", "addition", "1", "2"],
    ):
        assert main([*prefix, *args]) != 0
        assert json.loads(capsys.readouterr().out)["status"] == "error"
        assert file.read_bytes() == before


def test_cli_human_list_uses_numbers_and_annotations_instead_of_internal_ids(tmp_path, capsys):
    prefix = ["config", "prompt", "--home", str(tmp_path)]
    assert main([*prefix, "add", "addition", "--text", "First line\nSecond line"]) == 0
    output = capsys.readouterr().out
    entry = PromptConfigManager(tmp_path).snapshot().config.additions[0]
    assert "1. [disabled] First line Second line" in output
    assert entry.id not in output
    assert main([*prefix, "edit", "addition", "1", "--description", "My annotation"]) == 0
    output = capsys.readouterr().out
    assert "1. [disabled] My annotation" in output
    assert PromptConfigManager(tmp_path).snapshot().config.additions[0].id == entry.id
