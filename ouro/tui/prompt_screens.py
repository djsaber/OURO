"""Personal prompt forms built from the shared grouped and multiline editors."""

from __future__ import annotations

from pydantic import ValidationError

from ouro.config.prompts import PromptConfig, PromptDraft, PromptEntry, PromptKind
from ouro.tui.inline_editor import MultilineEditableChoiceScreen
from ouro.tui.screens import Choice


def prompt_label(entry: PromptEntry) -> str:
    return entry.description.strip() or " ".join(entry.text.split())[:80]


def prompt_choices(config: PromptConfig) -> tuple[Choice, ...]:
    choices: list[Choice] = []
    for kind, entries, group in (
        ("replacement", config.replacements, "REPLACEMENT · AT MOST ONE ENABLED"),
        ("addition", config.additions, "ADDITIONS · APPLIED IN LIST ORDER"),
    ):
        group_trailing = (
            "System default active"
            if kind == "replacement" and config.system_prompt is None
            else ""
        )
        for index, entry in enumerate(entries, 1):
            choices.append(
                Choice(
                    f"{kind}:{entry.id}",
                    prompt_label(entry),
                    trailing=f"{index} · {'Enabled' if entry.enabled else 'Disabled'}",
                    group=group,
                    group_trailing=group_trailing,
                )
            )
        choices.append(
            Choice(f"add:{kind}", f"Add {kind} prompt", group=group, group_trailing=group_trailing)
        )
    return tuple(choices)


class PromptEntryScreen(MultilineEditableChoiceScreen):
    def __init__(self, kind: PromptKind, entry: PromptEntry | None = None) -> None:
        self._kind = kind
        self._values = {
            "description": entry.description if entry else "",
            "text": entry.text if entry else "",
        }
        self._enabled = entry.enabled if entry else False
        super().__init__(
            title=f"{'EDIT' if entry else 'ADD'} {kind.upper()} PROMPT",
            placeholder="Filter prompt fields…",
            choices=self._field_choices(),
            on_save=self._save_field,
        )

    def _draft(self) -> PromptDraft:
        return PromptDraft(**self._values, enabled=self._enabled)

    async def _save_field(self, field: str, value: str) -> tuple[Choice, ...] | None:
        self._values[field] = value
        return self._field_choices()

    async def _select_choice(self, selected: str) -> None:
        if self._editing_value is None:
            if selected == "enabled":
                self._enabled = not self._enabled
                self._replace_choices(self._field_choices(), selected=selected)
                return
            if selected == "save":
                try:
                    draft = self._draft()
                except ValidationError:
                    return
                self.dismiss(draft)  # type: ignore[arg-type]
                return
        await super()._select_choice(selected)

    def _field_choices(self) -> tuple[Choice, ...]:
        error = ""
        try:
            self._draft()
        except ValidationError as exc:
            issue = exc.errors(include_url=False)[0]
            error = f"{issue['loc'][0]}: {issue['msg']}"
        return (
            Choice(
                "description",
                "Description (optional)",
                trailing=self._values["description"] or "Not set",
                edit_value=self._values["description"],
                group="PROMPT",
            ),
            Choice(
                "text",
                "Content",
                trailing=f"{len(self._values['text'])} characters",
                edit_value=self._values["text"],
                edit_lines=4,
                group="PROMPT",
            ),
            Choice(
                "enabled",
                "Enabled",
                trailing="Yes" if self._enabled else "No",
                description="Enabling switches off the other replacement."
                if self._kind == "replacement"
                else "",
                group="ACTIVATION",
            ),
            Choice(
                "save",
                "Save",
                trailing=error or "Applies to the next model request",
                disabled=bool(error),
                group="ACTIONS",
            ),
        )
