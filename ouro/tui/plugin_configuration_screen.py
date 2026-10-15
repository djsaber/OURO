from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterator
from typing import cast

from pydantic import JsonValue, SecretStr

from ouro.application.plugin_configuration_models import (
    ConfigurationIssue,
    InlinePluginCredential,
    PluginConfigurationCandidate,
    PluginConfigurationPreview,
    PluginConfigurationResult,
    PluginConfigurationView,
)
from ouro.tui.credential_editor import (
    CredentialEdit,
    CredentialEditableChoiceScreen,
    credential_trailing,
)
from ouro.tui.plugin_configuration_schema import (
    configuration_fields,
    json_text,
    schema_placeholder,
)
from ouro.tui.screens import Choice, ChoiceScreen, ConfirmationScreen
from ouro.tui.theme import DEFAULT_TUI_THEME, TuiTheme, theme_for


class PluginConfigurationScreen(CredentialEditableChoiceScreen):
    """Edit a full application draft with shared menus, inputs and explicit application."""

    def __init__(
        self,
        view: PluginConfigurationView,
        *,
        preview: Callable[[PluginConfigurationCandidate], Awaitable[PluginConfigurationPreview]],
        apply: Callable[[PluginConfigurationCandidate], Awaitable[PluginConfigurationResult]],
        reload: Callable[[], Awaitable[PluginConfigurationView]],
        theme: TuiTheme = DEFAULT_TUI_THEME,
    ) -> None:
        self._initial_tui_theme = theme
        self._view = view
        self._draft = view.candidate()
        self._fields = {
            f"field:{index}": field
            for index, field in enumerate(configuration_fields(view.schema_document or {}))
        }
        self._preview = preview
        self._apply = apply
        self._reload = reload
        self._issues = view.issues
        self._failure = ""
        self._busy = False
        super().__init__(
            title=theme_for(self).inline_separator.join(("EDIT PLUGIN", view.name)),
            placeholder="Filter plugin configuration…",
            choices=self._configuration_choices(),
            on_save=self._save_field,
        )

    async def _select_choice(self, selected: str) -> None:
        if self._busy:
            return
        field = self._fields.get(selected)
        if self._editing_value is not None:
            # Entering an empty string is distinct from leaving an unset/null field untouched.
            if (
                field is not None
                and field.kind == "string"
                and self._edit_text == ""
                and self._draft.values.get(field.key) is None
            ):
                await self._save_edit(self._save_field(selected, ""), selected=selected)
                return
            await super()._select_choice(selected)
            return
        if selected == "save":
            await self._submit()
            return
        if selected == "preview":
            await self._check()
            self._refresh(selected)
            return
        if selected == "reload":
            confirmed = await self.open_child(
                ConfirmationScreen(
                    title="RELOAD CONFIGURATION",
                    message="Discard this draft and load the saved configuration?",
                )
            )
            if ConfirmationScreen.confirmed(confirmed):
                try:
                    self._view = await self._reload()
                    self._draft = self._view.candidate()
                    self._issues, self._failure = self._view.issues, ""
                except Exception as exc:
                    self._failure = str(exc)
                self._refresh(selected)
            return
        if selected in {"unset", "null"}:
            choices = tuple(
                Choice(key, field.title)
                for key, field in self._fields.items()
                if selected == "unset" or (field.nullable)
            )
            key = await self.open_child(
                ChoiceScreen(
                    title="UNSET FIELD" if selected == "unset" else "SET NULL",
                    placeholder="Choose a field…",
                    choices=choices,
                )
            )
            if key in self._fields:
                name = self._fields[key].key
                if selected == "unset":
                    self._draft.values.pop(name, None)
                else:
                    self._draft.values[name] = None
                await self._check()
                self._refresh(selected)
            return
        if field is not None and field.kind == "select":
            choice = await self.open_child(
                ChoiceScreen(
                    title=field.title,
                    placeholder=field.description or "Choose a value…",
                    choices=(
                        Choice("unset", "Unset", trailing="Use default"),
                        *(
                            Choice(str(index), json_text(value))
                            for index, value in enumerate(field.options)
                        ),
                    ),
                )
            )
            if choice == "unset":
                self._draft.values.pop(field.key, None)
            elif choice is not None:
                self._draft.values[field.key] = json.loads(json_text(field.options[int(choice)]))
            else:
                return
            await self._check()
            self._refresh(selected)
            return
        await super()._select_choice(selected)

    def _credential_value(self, field_id: str) -> CredentialEdit | None:
        if not field_id.startswith("credential:"):
            return None
        binding = self._draft.credential_refs.get(field_id.removeprefix("credential:"))
        if isinstance(binding, str):
            return CredentialEdit("env", binding)
        return CredentialEdit("inline", binding.value.get_secret_value() if binding else "")

    async def _save_credential(self, field_id: str, edit: CredentialEdit) -> tuple[Choice, ...]:
        slot = field_id.removeprefix("credential:")
        value = edit.value if edit.source == "inline" else edit.value.strip()
        if value:
            self._draft.credential_refs[slot] = (
                InlinePluginCredential(value=SecretStr(value)) if edit.source == "inline" else value
            )
        else:
            self._draft.credential_refs.pop(slot, None)
        await self._check()
        return self._configuration_choices()

    async def _save_field(self, field_id: str, value: str) -> tuple[Choice, ...] | None:
        try:
            if field_id == "values-json":
                parsed = _parse_json(value)
                if not isinstance(parsed, dict):
                    raise ValueError("Settings must be a JSON object.")
                self._draft.values = parsed
            else:
                field = self._fields[field_id]
                parsed = value if field.kind == "string" else _parse_json(value)
                if field.kind in {"integer", "number"} and (
                    isinstance(parsed, bool) or not isinstance(parsed, int | float)
                ):
                    raise ValueError("Enter a JSON number.")
                self._draft.values[field.key] = parsed
        except ValueError as exc:
            self.notify(str(exc), severity="error", markup=False)
            return None
        await self._check()
        return self._configuration_choices()

    async def _check(self) -> PluginConfigurationPreview | None:
        self._failure = ""
        try:
            result = await self._preview(self._draft)
            self._issues = result.issues
            return result
        except Exception as exc:
            self._failure = str(exc)
            return None

    async def _submit(self) -> None:
        self._busy = True
        try:
            result = await self._check()
            if result is not None and result.valid:
                applied = await self._apply(result.candidate)
                self.dismiss(applied)  # type: ignore[arg-type]
                return
        except Exception as exc:
            self._failure = str(exc)
        finally:
            self._busy = False
        self._refresh("save")

    def _refresh(self, selected: str) -> None:
        self._replace_choices(self._configuration_choices(), selected=selected)

    def _configuration_choices(self) -> tuple[Choice, ...]:
        choices = []
        theme = theme_for(self)
        document = self._view.schema_document or {}
        for field_id, field in self._fields.items():
            value = self._draft.values.get(field.key)
            present = field.key in self._draft.values
            description = field.description
            constraints = [
                f"{key}: {json_text(field.schema[key])}"
                for key in (
                    "minimum",
                    "maximum",
                    "exclusiveMinimum",
                    "exclusiveMaximum",
                    "multipleOf",
                    "minLength",
                    "maxLength",
                    "pattern",
                    "format",
                )
                if key in field.schema
            ]
            if constraints:
                description = " · ".join(filter(None, (description, ", ".join(constraints))))
            placeholder = schema_placeholder(field.schema, document)
            if field.kind == "json":
                placeholder = f"Example: {placeholder}"
            placeholder = theme.inline_separator.join(filter(None, (placeholder, description)))
            choices.append(
                Choice(
                    field_id,
                    field.title,
                    trailing=json_text(value) if present else "",
                    trailing_badge="required" if field.required and not present else "",
                    group="SETTINGS",
                    edit_value=None
                    if field.kind == "select"
                    else (
                        value
                        if field.kind == "string" and isinstance(value, str)
                        else json_text(value)
                        if present and field.kind != "string"
                        else ""
                    ),
                    edit_placeholder=placeholder,
                )
            )
        if self._view.schema_document is not None:
            choices.append(
                Choice(
                    "values-json",
                    "Settings (JSON)",
                    trailing=json_text(self._draft.values) if self._draft.values else "",
                    group="SETTINGS",
                    edit_value=json_text(self._draft.values),
                    edit_placeholder=f"Example: {schema_placeholder(document, document)}",
                )
            )
        for slot in self._view.credential_slots:
            binding = self._draft.credential_refs.get(slot.slot)
            field_id = f"credential:{slot.slot}"
            edit = self._credential_value(field_id)
            assert edit is not None
            choices.append(
                Choice(
                    field_id,
                    slot.title,
                    trailing=credential_trailing(edit, configured=bool(binding), theme=theme),
                    trailing_badge="required" if slot.required and not binding else "",
                    group="CREDENTIALS",
                    edit_value=edit.value,
                    edit_placeholder=theme.inline_separator.join(
                        filter(None, ("blank to unbind", slot.description))
                    ),
                )
            )
        for key, issue in _issue_rows(self._issues):
            choices.append(
                Choice(
                    f"issue:{key}",
                    issue.path or "Configuration",
                    description=issue.message,
                    trailing=issue.code,
                    group="VALIDATION",
                    disabled=True,
                )
            )
        if self._failure:
            choices.append(
                Choice(
                    "failure",
                    "Apply failed",
                    trailing=" ".join(self._failure.splitlines()),
                    group="VALIDATION",
                    disabled=True,
                )
            )
        choices.extend(
            (
                Choice(
                    "unset",
                    "Unset field",
                    trailing="Use default",
                    group="ACTIONS",
                    disabled=not self._fields,
                ),
                Choice(
                    "null",
                    "Set null",
                    trailing="Nullable fields",
                    group="ACTIONS",
                    disabled=not any(field.nullable for field in self._fields.values()),
                ),
                Choice("preview", "Validate", group="ACTIONS"),
                Choice(
                    "reload",
                    "Reload",
                    trailing="Discard draft",
                    group="ACTIONS",
                ),
                Choice(
                    "save",
                    "Apply",
                    group="ACTIONS",
                    trailing="Current host"
                    if self._view.selected and self._view.desired_enabled
                    else "Save only",
                ),
            )
        )
        return tuple(choices)


def _issue_rows(
    issues: tuple[ConfigurationIssue, ...], prefix: str = ""
) -> Iterator[tuple[str, ConfigurationIssue]]:
    for index, issue in enumerate(issues):
        key = f"{prefix}{index}"
        yield key, issue
        yield from _issue_rows(issue.children, f"{key}.")


def _parse_json(value: str) -> JsonValue:
    def reject_constant(_value: str) -> None:
        raise ValueError("JSON numbers must be finite.")

    try:
        parsed = json.loads(value, parse_constant=reject_constant)
        json.dumps(parsed, allow_nan=False)
        return cast(JsonValue, parsed)
    except json.JSONDecodeError:
        raise ValueError("Enter valid JSON.") from None
