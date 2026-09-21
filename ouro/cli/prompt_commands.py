"""CLI adapter for persistent personal prompt configuration."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ouro.application.configuration import ConfigurationFacade
from ouro.application.configuration_models import SystemPromptConfigView
from ouro.cli.errors import error_exit_code
from ouro.config.prompts import PromptDraft
from ouro.core.errors import ConfigError, OuroError


def run_prompt_command(args: argparse.Namespace) -> int:
    try:
        view = _execute(args)
    except (OuroError, OSError, ValueError) as exc:
        if args.json:
            print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        else:
            print(str(exc), file=sys.stderr)
        return error_exit_code(exc) if isinstance(exc, OuroError) else 2
    if args.json:
        print(json.dumps({"status": "ok", **view.model_dump(mode="json")}, ensure_ascii=False))
    else:
        print(f"Config: {view.config_path} (revision {view.config.revision})")
        for kind in ("replacement", "addition"):
            entries = view.config.replacements if kind == "replacement" else view.config.additions
            print(f"{kind.title()} prompts:")
            for position, entry in enumerate(entries, 1):
                state = "enabled" if entry.enabled else "disabled"
                label = entry.description.strip() or " ".join(entry.text.split())[:80]
                print(f"  {position}. [{state}] {label}")
            if not entries:
                print("  (none)")
        if view.config.system_prompt is None:
            print("Using the default base system prompt.")
        if view.change is not None:
            print("Saved. Applies to the next model request; the current response is unchanged.")
    return 0


def _execute(args: argparse.Namespace) -> SystemPromptConfigView:
    facade = ConfigurationFacade(args.home)
    view = facade.system_prompt()
    action = args.prompt_command or "list"
    if action == "list":
        return view
    revision = args.expected_revision
    if revision is None:
        revision = view.config.revision
    current = None
    if action != "add":
        entries = view.config.entries(args.kind)
        if not 1 <= args.number <= len(entries):
            raise ConfigError(f"prompt number must be between 1 and {len(entries)}")
        current = entries[args.number - 1]
    if action in {"add", "edit"}:
        values = current.model_dump(exclude={"id"}) if current is not None else {}
        if args.description is not None:
            values["description"] = args.description
        if args.text is not None:
            values["text"] = args.text
        elif args.file is not None:
            values["text"] = Path(args.file).read_text(encoding="utf-8-sig")
        if args.enabled is not None:
            values["enabled"] = args.enabled
        return facade.save_prompt(
            args.kind,
            PromptDraft.model_validate(values),
            entry_id=current.id if current is not None else None,
            expected_revision=revision,
        )
    assert current is not None
    if action == "remove":
        return facade.remove_prompt(args.kind, current.id, expected_revision=revision)
    if action in {"enable", "disable"}:
        return facade.set_prompt_enabled(
            args.kind, current.id, action == "enable", expected_revision=revision
        )
    if action == "move":
        return facade.move_prompt(args.kind, current.id, args.position, expected_revision=revision)
    raise AssertionError(f"unknown prompt command: {action}")
