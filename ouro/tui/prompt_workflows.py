"""Keyboard-first management of personal prompt entries."""

from __future__ import annotations

from typing import cast

from ouro.config.prompts import PromptDraft, PromptKind
from ouro.tui.data_transfer import DataTransferWorkflows
from ouro.tui.prompt_screens import PromptEntryScreen, prompt_choices, prompt_label
from ouro.tui.screens import Choice, ChoiceScreen, ConfirmationScreen
from ouro.tui.theme import theme_for


class PromptWorkflows(DataTransferWorkflows):
    async def _configure_prompts(self, *, menu: ChoiceScreen | None = None) -> None:
        try:
            current = await self._controller.prompt_configuration()
        except Exception as exc:  # noqa: BLE001 - report configuration errors at the UI boundary.
            self.notify(str(exc), severity="error", markup=False)
            return

        async def refresh() -> tuple[Choice, ...]:
            nonlocal current
            try:
                current = await self._controller.prompt_configuration()
            except Exception as exc:  # noqa: BLE001 - keep navigation usable after read failures.
                self.notify(str(exc), severity="error", markup=False)
            return prompt_choices(current.config)

        async def select(value: str) -> tuple[Choice, ...] | None:
            try:
                prefix, identity = value.split(":", 1)
                if prefix == "add":
                    kind = cast(PromptKind, identity)
                    draft = await screen.open_child(PromptEntryScreen(kind))
                    if draft is not None:
                        await self._controller.save_prompt(
                            kind,
                            cast(PromptDraft, draft),
                            expected_revision=current.config.revision,
                        )
                        self.notify("Prompt saved (applies to the next model request)")
                        return await refresh()
                else:
                    await manage(cast(PromptKind, prefix), identity)
            except Exception as exc:  # noqa: BLE001 - retain the menu on a rejected edit.
                self.notify(str(exc), severity="error", markup=False)
                return await refresh()
            return None

        async def manage(kind: PromptKind, entry_id: str) -> None:
            entries = current.config.entries(kind)
            entry = next(item for item in entries if item.id == entry_id)
            position = entries.index(entry) + 1
            revision = current.config.revision

            async def action(value: str) -> tuple[Choice, ...] | None:
                try:
                    if value == "edit":
                        draft = await screen.open_child(PromptEntryScreen(kind, entry))
                        if draft is None:
                            return None
                        await self._controller.save_prompt(
                            kind,
                            cast(PromptDraft, draft),
                            entry_id=entry.id,
                            expected_revision=revision,
                        )
                    elif value == "toggle":
                        await self._controller.set_prompt_enabled(
                            kind, entry_id, not entry.enabled, expected_revision=revision
                        )
                    elif value in {"up", "down"}:
                        await self._controller.move_prompt(
                            kind,
                            entry_id,
                            position + (-1 if value == "up" else 1),
                            expected_revision=revision,
                        )
                    elif value == "delete":
                        confirmed = await screen.open_child(
                            ConfirmationScreen(
                                title="DELETE PROMPT",
                                message=f"Delete {prompt_label(entry)}?",
                                confirm_label="Delete",
                            )
                        )
                        if not ConfirmationScreen.confirmed(confirmed):
                            return None
                        await self._controller.remove_prompt(
                            kind, entry_id, expected_revision=revision
                        )
                    else:
                        return None
                    self.notify("Prompt configuration saved (applies to the next model request)")
                except Exception as exc:  # noqa: BLE001 - revision and I/O errors stay visible.
                    self.notify(str(exc), severity="error", markup=False)
                screen.pop_page(choices=await refresh(), initial_value=f"{kind}:{entry_id}")
                return None

            screen.push_page(
                title=f"PROMPT · {prompt_label(entry)}",
                placeholder="Choose an action…",
                choices=(
                    Choice("edit", "Edit"),
                    Choice(
                        "toggle",
                        "Disable" if entry.enabled else "Enable",
                        description="Enabling overrides OURO's default system prompt."
                        if kind == "replacement" and not entry.enabled
                        else "",
                        description_accent=(
                            theme_for(self).danger
                            if kind == "replacement" and not entry.enabled
                            else None
                        ),
                    ),
                    Choice("up", "Move up", disabled=position == 1),
                    Choice("down", "Move down", disabled=position == len(entries)),
                    Choice("delete", "Delete", destructive=True),
                ),
                on_select=action,
            )

        if menu is not None:
            screen = menu
            screen.push_page(
                title="PERSONAL PROMPTS",
                placeholder="Filter prompts…",
                choices=prompt_choices(current.config),
                on_select=select,
                on_refresh=refresh,
            )
        else:
            screen = ChoiceScreen(
                title="PERSONAL PROMPTS",
                placeholder="Filter prompts…",
                choices=prompt_choices(current.config),
                on_select=select,
                on_refresh=refresh,
            )
            await self.push_screen_wait(screen)
