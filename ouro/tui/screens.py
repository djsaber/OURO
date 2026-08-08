from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, OptionList, Select, Static
from textual.widgets.option_list import Option

from ouro.tui.commands import slash_command_help
from ouro.tui.controller import TuiModelConfiguration
from ouro.tui.theme import theme_for


@dataclass(frozen=True)
class Choice:
    value: str
    label: str
    description: str = ""
    accent: str | None = None


class ChoiceScreen(ModalScreen[str | None]):
    BINDINGS = [
        Binding("up", "previous_choice", "Previous", show=False, priority=True),
        Binding("down", "next_choice", "Next", show=False, priority=True),
        Binding("enter", "accept_choice", "Select", show=False, priority=True),
        Binding("escape", "cancel", "Close", show=False),
        Binding("ctrl+c", "cancel", "Close", show=False, priority=True),
    ]

    def __init__(self, *, title: str, placeholder: str, choices: tuple[Choice, ...]) -> None:
        super().__init__()
        self._title = title
        self._placeholder = placeholder
        self._choices = choices

    def compose(self) -> ComposeResult:
        with Vertical(id="choice-panel"):
            yield Static(self._title, id="choice-title")
            yield Input(placeholder=self._placeholder, id="choice-filter")
            yield OptionList(*self._options(self._choices), id="choice-list")
            yield Static("↑/↓ move · Enter select · Esc cancel", id="choice-hint")

    def on_mount(self) -> None:
        self.query_one("#choice-filter", Input).focus()
        options = self.query_one("#choice-list", OptionList)
        if options.option_count:
            options.highlighted = 0

    def on_input_changed(self, event: Input.Changed) -> None:
        needle = event.value.casefold().strip()
        choices = tuple(
            choice
            for choice in self._choices
            if not needle
            or needle in choice.label.casefold()
            or needle in choice.description.casefold()
        )
        options = self.query_one("#choice-list", OptionList)
        options.clear_options()
        options.add_options(self._options(choices))
        options.highlighted = 0 if choices else None

    def on_input_submitted(self, _event: Input.Submitted) -> None:
        options = self.query_one("#choice-list", OptionList)
        if options.option_count and options.highlighted is None:
            options.highlighted = 0
        options.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id is not None:
            self.dismiss(str(event.option.id))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_previous_choice(self) -> None:
        self.query_one("#choice-list", OptionList).action_cursor_up()

    def action_next_choice(self) -> None:
        self.query_one("#choice-list", OptionList).action_cursor_down()

    def action_accept_choice(self) -> None:
        self.query_one("#choice-list", OptionList).action_select()

    @staticmethod
    def _options(choices: tuple[Choice, ...]) -> list[Option]:
        options: list[Option] = []
        for choice in choices:
            # Build the label from per-segment styles so the description does
            # not inherit the label's bold: Rich applies a Text's base style to
            # every appended segment, so bold must live on the label segment.
            label = Text()
            label.append(choice.label, style=f"bold {choice.accent}" if choice.accent else "bold")
            if choice.description:
                label.append(f"\n{choice.description}", style="#7d8794")
            options.append(Option(label, id=choice.value))
        return options


class ModelConfigurationScreen(ModalScreen[TuiModelConfiguration | None]):
    """Collect a new endpoint configuration without owning persistence rules."""

    BINDINGS = [
        Binding("escape", "cancel", "Close", show=False),
        Binding("ctrl+c", "cancel", "Close", show=False, priority=True),
        Binding("ctrl+s", "save", "Save", show=False, priority=True),
    ]

    def __init__(self, providers: tuple[Choice, ...]) -> None:
        super().__init__()
        self._providers = providers

    def compose(self) -> ComposeResult:
        options = tuple((provider.label, provider.value) for provider in self._providers)
        initial = self._providers[0].value if self._providers else Select.NULL
        with VerticalScroll(id="model-config-panel"):
            yield Static("CONFIGURE MODEL", id="model-config-title")
            yield Label("Provider", classes="model-config-label")
            yield Select[str](
                options,
                value=initial,
                allow_blank=False,
                compact=True,
                id="model-provider",
            )
            yield Label("Endpoint ID", classes="model-config-label")
            yield Input(placeholder="e.g. gpt-5.6-mini", id="model-endpoint")
            yield Label("Provider model ID", classes="model-config-label")
            yield Input(placeholder="Model name sent to the provider", id="model-id")
            with Horizontal(classes="model-config-fields"):
                with Vertical(classes="model-config-number-field"):
                    yield Label("Context window", classes="model-config-number-label")
                    yield Input(value="128000", id="context-window")
                with Vertical(classes="model-config-number-field"):
                    yield Label("Max output tokens", classes="model-config-number-label")
                    yield Input(value="8192", id="max-output-tokens")
            with Horizontal(classes="model-config-capabilities"):
                yield Checkbox("Tools", value=True, id="supports-tools")
                yield Checkbox("Vision", id="supports-vision")
            yield Static("", id="model-config-error")
            yield Static(
                "Tab move · Space toggle · Ctrl+S save · Esc cancel",
                id="model-config-hint",
            )
            with Horizontal(classes="model-config-actions"):
                yield Button("Cancel", id="model-config-cancel")
                yield Button("Save & select", variant="primary", id="model-config-save")

    def on_mount(self) -> None:
        if self._providers:
            self.query_one("#model-provider", Select).focus()

    @on(Button.Pressed, "#model-config-cancel")
    def cancel_from_button(self) -> None:
        self.action_cancel()

    @on(Button.Pressed, "#model-config-save")
    def save_from_button(self) -> None:
        self._submit()

    @on(Input.Submitted)
    def submit_from_input(self, event: Input.Submitted) -> None:
        if event.input.id == "max-output-tokens":
            self._submit()
        else:
            self.focus_next()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_save(self) -> None:
        self._submit()

    def _submit(self) -> None:
        provider = self.query_one("#model-provider", Select).value
        endpoint_id = self.query_one("#model-endpoint", Input).value.strip()
        model_id = self.query_one("#model-id", Input).value.strip()
        try:
            if not isinstance(provider, str) or not provider:
                raise ValueError("Select a provider")
            if not endpoint_id:
                raise ValueError("Endpoint ID is required")
            if not model_id:
                raise ValueError("Provider model ID is required")
            context_window = self._positive_int("#context-window", "Context window")
            max_output_tokens = self._positive_int("#max-output-tokens", "Max output tokens")
            if max_output_tokens > context_window:
                raise ValueError("Max output tokens cannot exceed the context window")
        except ValueError as exc:
            self.query_one("#model-config-error", Static).update(str(exc))
            return
        self.dismiss(
            TuiModelConfiguration(
                provider_id=provider,
                endpoint_id=endpoint_id,
                model_id=model_id,
                context_window=context_window,
                max_output_tokens=max_output_tokens,
                supports_tools=self.query_one("#supports-tools", Checkbox).value,
                supports_vision=self.query_one("#supports-vision", Checkbox).value,
            )
        )

    def _positive_int(self, selector: str, label: str) -> int:
        raw = self.query_one(selector, Input).value.strip()
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError(f"{label} must be an integer") from exc
        if value < 1:
            raise ValueError(f"{label} must be positive")
        return value


class HelpScreen(ModalScreen[None]):
    BINDINGS = [
        Binding(
            "escape,question_mark,ctrl+c",
            "close",
            "Close",
            show=False,
            priority=True,
        ),
    ]

    def compose(self) -> ComposeResult:
        # Build the panel from a Text so padding tracks visible width, not
        # markup length — keeping the shortcut columns aligned in the flat panel.
        text = Text()
        accent = theme_for(self).accent
        text.append("OURO shortcuts\n\n", style=f"bold {accent}")
        pairs = (
            ("Enter", "send", "Shift+Enter", "new line"),
            ("Ctrl+N", "new session", "Ctrl+S", "session picker"),
            ("Ctrl+M", "model picker", "Ctrl+P", "action palette"),
            ("Ctrl+C", "abort run", "Ctrl+Q", "quit"),
        )
        for left_key, left_desc, right_key, right_desc in pairs:
            text.append(left_key, style="bold")
            text.append(f" {left_desc}")
            text.append(" " * max(0, 24 - len(left_key) - len(left_desc) - 1))
            text.append(right_key, style="bold")
            text.append(f" {right_desc}\n")
        text.append("?", style="bold")
        text.append(" help\n\n")
        text.append("Slash commands\n\n", style=f"bold {accent}")
        text.append(slash_command_help(), style="#aeb5bf")
        text.append(
            "\n\nPlugin commands also appear in slash completion; unique names "
            "use the short form, ambiguous ones use /plugin.id:command.",
            style="#8f9aa8",
        )
        yield Static(text, id="help-panel")

    def action_close(self) -> None:
        self.dismiss(None)
