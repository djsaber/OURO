"""Configuration screens for compaction."""

from __future__ import annotations

from ouro.application.contracts.configuration import CompactionConfigView
from ouro.application.contracts.models import AvailableModel
from ouro.core.model_selection import DEFAULT_MODEL
from ouro.core.reasoning import ReasoningRequest
from ouro.tui.features.configuration.values import (
    _NO_FALLBACK,
    _REASONING_REQUESTS,
    _RUNTIME_MODEL,
    _enabled_label,
    _optional_bounded_float,
    _optional_positive_int,
    _optional_text,
    _ratio,
)
from ouro.tui.features.providers.model_menus import (
    UNAVAILABLE_MODEL_LABEL,
    model_choices,
    model_selection_label,
)
from ouro.tui.menus.choice import ChoiceScreen
from ouro.tui.menus.editable import EditableChoiceScreen
from ouro.tui.menus.labels import THINKING_LABELS
from ouro.tui.menus.models import Choice
from ouro.tui.theme import DEFAULT_TUI_THEME, TuiTheme, theme_for
from ouro.tui.view_models import TuiCompactionConfiguration


class CompactionConfigurationScreen(EditableChoiceScreen):
    """Edit compaction policy through the shared grouped inline menu."""

    def __init__(
        self,
        config: CompactionConfigView,
        *,
        models: tuple[AvailableModel, ...],
        strategy_ids: tuple[str, ...],
        theme: TuiTheme = DEFAULT_TUI_THEME,
        system_default_model: AvailableModel | None = None,
    ) -> None:
        self._initial_tui_theme = theme
        self._models = models
        self._system_default_model = system_default_model
        self._strategy_ids = tuple(
            dict.fromkeys(
                (
                    config.strategy_id,
                    *((config.fallback_strategy_id,) if config.fallback_strategy_id else ()),
                    *(strategy_ids or (config.strategy_id,)),
                )
            )
        )
        policy = config.policy
        configured_model = (
            DEFAULT_MODEL
            if config.model.use_default_model
            else f"{config.model.provider_id}/{config.model.endpoint_id}"
            if config.model.has_dedicated_endpoint
            else _RUNTIME_MODEL
        )
        self._model_labels = {
            _RUNTIME_MODEL: "Conversation model",
            DEFAULT_MODEL: "Default model",
            **{
                model.key: model_selection_label(model.key, models, theme=theme) for model in models
            },
        }
        if configured_model not in self._model_labels:
            self._model_labels[configured_model] = UNAVAILABLE_MODEL_LABEL
        reasoning = _reasoning_value(config.model.reasoning)
        configured_capability = self._model(configured_model)
        reasoning_options = _compaction_reasoning_options(configured_capability)
        if reasoning not in reasoning_options:
            reasoning = "default"
        self._values = {
            "compaction-strategy": config.strategy_id,
            "compaction-fallback": config.fallback_strategy_id or _NO_FALLBACK,
            "compaction-trigger": str(policy["trigger_ratio"]),
            "compaction-target": str(policy["target_ratio"]),
            "compaction-model": configured_model,
            "compaction-temperature": _optional_text(config.model.temperature),
            "compaction-max-output": _optional_text(config.model.max_output_tokens),
            "compaction-reasoning": reasoning,
        }
        self._auto_enabled = bool(policy["auto_enabled"])
        super().__init__(
            title="CONTEXT COMPACTION",
            placeholder="Search compaction settings…",
            choices=self._compaction_choices(),
            on_save=self._save_field,
        )

    async def _select_choice(self, selected: str) -> None:
        if self._editing_value is not None:
            await super()._select_choice(selected)
            return
        if selected == "compaction-auto":
            self._auto_enabled = not self._auto_enabled
            self._refresh(selected)
            return
        if selected == "compaction-model":
            model = await self.open_child(
                ChoiceScreen(
                    title="COMPACTION MODEL",
                    placeholder="Filter models…",
                    choices=self._model_choices(),
                    initial_value=self._values["compaction-model"],
                )
            )
            self._apply_model_selection(model)
            return
        cycles = {
            "compaction-strategy": self._strategy_ids,
            "compaction-fallback": (_NO_FALLBACK, *self._strategy_ids),
            "compaction-reasoning": _compaction_reasoning_options(
                self._model(self._values["compaction-model"])
            ),
        }
        if selected in cycles:
            values = cycles[selected]
            current = self._values[selected]
            self._values[selected] = values[(values.index(current) + 1) % len(values)]
            self._refresh(selected)
            return
        if selected == "compaction-save":
            self._submit()
            return
        await super()._select_choice(selected)

    async def _save_field(self, field_id: str, value: str) -> tuple[Choice, ...] | None:
        self._values[field_id] = value.strip()
        return self._compaction_choices()

    def _refresh(self, selected: str) -> None:
        self._replace_choices(self._compaction_choices(), selected=selected)

    def _normalize_model_options(self) -> None:
        model_key = self._values["compaction-model"]
        reasoning_options = _compaction_reasoning_options(self._model(model_key))
        if self._values["compaction-reasoning"] not in reasoning_options:
            self._values["compaction-reasoning"] = "default"

    def _apply_model_selection(self, selected: str | None) -> None:
        if selected is None:
            return
        self._values["compaction-model"] = selected
        self._normalize_model_options()
        self._refresh("compaction-model")

    def _model_choices(self) -> tuple[Choice, ...]:
        theme = theme_for(self)
        selected = self._values["compaction-model"]
        choices = [
            Choice(
                _RUNTIME_MODEL,
                "Conversation model",
                marker=(theme.selected_choice_marker if selected == _RUNTIME_MODEL else ""),
                group="MODEL SOURCE",
            ),
            Choice(
                DEFAULT_MODEL,
                "Default model",
                marker=theme.selected_choice_marker if selected == DEFAULT_MODEL else "",
                group="MODEL SOURCE",
                disabled=self._system_default_model is None,
            ),
        ]
        choices.extend(model_choices(self._models, selected=selected, theme=theme))
        if selected not in {_RUNTIME_MODEL, DEFAULT_MODEL} and self._model(selected) is None:
            choices.append(
                Choice(
                    selected,
                    self._model_labels[selected],
                    marker=theme.selected_choice_marker,
                    group="UNAVAILABLE",
                    disabled=True,
                )
            )
        return tuple(choices)

    def _submit(self) -> None:
        try:
            draft = self._draft()
        except ValueError:
            return
        self.dismiss(draft)  # type: ignore[arg-type]

    def _draft(self) -> TuiCompactionConfiguration:
        strategy = self._values["compaction-strategy"]
        fallback = self._values["compaction-fallback"]
        model = self._values["compaction-model"]
        if model == DEFAULT_MODEL and self._system_default_model is None:
            raise ValueError("System default endpoint is not configured")
        trigger = _ratio(self._values["compaction-trigger"], "Start at usage (0–1)")
        target = _ratio(self._values["compaction-target"], "Reduce to usage (0–1)")
        if target >= trigger:
            raise ValueError("Target ratio must be lower than trigger ratio")
        if fallback == strategy:
            raise ValueError("Fallback strategy must differ from the primary strategy")
        max_output_tokens = _optional_positive_int(
            self._values["compaction-max-output"], "Max output tokens"
        )
        return TuiCompactionConfiguration(
            strategy_id=strategy,
            fallback_strategy_id=None if fallback == _NO_FALLBACK else fallback,
            auto_enabled=self._auto_enabled,
            trigger_ratio=trigger,
            target_ratio=target,
            model_key=None if model == _RUNTIME_MODEL else model,
            temperature=(
                _optional_bounded_float(
                    self._values["compaction-temperature"],
                    "Temperature",
                    minimum=0.0,
                    maximum=2.0,
                )
            ),
            max_output_tokens=max_output_tokens,
            reasoning=_reasoning_request(self._values["compaction-reasoning"]),
        )

    def _validation_error(self) -> str:
        try:
            self._draft()
        except ValueError as exc:
            return str(exc)
        return ""

    def _compaction_choices(self) -> tuple[Choice, ...]:
        model_key = self._values["compaction-model"]
        configured = self._model(model_key)
        reasoning_disabled = (
            configured is not None and configured.reasoning.activation == "unsupported"
        )
        error = self._validation_error()
        fallback = self._values["compaction-fallback"]
        return (
            Choice(
                "compaction-strategy",
                "Compaction method",
                trailing=(
                    "Summarize older messages"
                    if self._values["compaction-strategy"] == "builtin.llm_summary"
                    else self._values["compaction-strategy"]
                ),
                group="STRATEGY",
                disabled=True,
            ),
            Choice(
                "compaction-fallback",
                "Backup method",
                trailing=(
                    "None"
                    if fallback == _NO_FALLBACK
                    else "Summarize older messages"
                    if fallback == "builtin.llm_summary"
                    else fallback
                ),
                group="STRATEGY",
                disabled=True,
            ),
            Choice(
                "compaction-auto",
                "Automatic compaction",
                trailing=_enabled_label(self._auto_enabled),
                group="STRATEGY",
            ),
            Choice(
                "compaction-trigger",
                "Start at usage (0–1)",
                trailing=self._values["compaction-trigger"] or "Required",
                group="THRESHOLDS",
                edit_value=self._values["compaction-trigger"],
                edit_placeholder="Enter a ratio from 0 to 1",
            ),
            Choice(
                "compaction-target",
                "Reduce to usage (0–1)",
                trailing=self._values["compaction-target"] or "Required",
                group="THRESHOLDS",
                edit_value=self._values["compaction-target"],
                edit_placeholder="Enter a ratio below the trigger",
            ),
            Choice(
                "compaction-model",
                "Compaction model",
                trailing=self._model_labels[model_key],
                group="MODEL",
            ),
            Choice(
                "compaction-temperature",
                "Temperature",
                trailing=self._values["compaction-temperature"] or "Endpoint default",
                group="MODEL",
                edit_value=self._values["compaction-temperature"],
                edit_placeholder="Leave blank to use the endpoint default",
            ),
            Choice(
                "compaction-max-output",
                "Max output tokens",
                trailing=self._values["compaction-max-output"] or "Optional",
                group="MODEL",
                edit_value=self._values["compaction-max-output"],
            ),
            Choice(
                "compaction-reasoning",
                "Thinking",
                trailing=(
                    "Unsupported"
                    if reasoning_disabled
                    else THINKING_LABELS[self._values["compaction-reasoning"]]
                ),
                group="MODEL",
                disabled=reasoning_disabled,
            ),
            Choice(
                "compaction-save",
                "Save",
                trailing=error,
                group="ACTIONS",
                disabled=bool(error),
            ),
        )

    def _model(self, key: str) -> AvailableModel | None:
        if key == DEFAULT_MODEL:
            return self._system_default_model
        return next((model for model in self._models if model.key == key), None)


def _reasoning_value(request: ReasoningRequest | None) -> str:
    if request is None:
        return "default"
    return request.effort or request.mode


def _reasoning_request(value: str) -> ReasoningRequest | None:
    try:
        return _REASONING_REQUESTS[value]
    except KeyError as exc:
        raise ValueError(f"Unsupported thinking option: {value}") from exc


def _compaction_reasoning_options(model: AvailableModel | None) -> tuple[str, ...]:
    if model is None:
        return tuple(THINKING_LABELS)
    if model.reasoning.activation == "unsupported":
        return ("default",)
    values = ["default", "enabled"]
    if model.reasoning.activation == "toggle":
        values.append("disabled")
    values.extend(model.reasoning.supported_efforts)
    return tuple(values)
