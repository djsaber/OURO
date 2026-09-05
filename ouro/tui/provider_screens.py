from __future__ import annotations

import json
from dataclasses import dataclass

from ouro.application.configuration_models import (
    ProviderEndpointView,
    ProviderTemplateView,
    ProviderView,
)
from ouro.config import (
    REASONING_PRESETS,
    EndpointReasoningConfig,
    ProviderType,
    reasoning_preset,
)
from ouro.config.provider_models import validate_reasoning_protocol_compatibility
from ouro.tui.controller import TuiEndpointConfiguration, TuiProviderConfiguration
from ouro.tui.screens import Choice, ChoiceScreen, EditableChoiceScreen
from ouro.tui.theme import DEFAULT_TUI_THEME, TuiTheme, theme_for

_REASONING_PRESET_LABELS = {
    "none": "None",
    "openai-effort": "OpenAI effort",
    "reasoning-content-effort": "Thinking level (reasoning_content)",
    "thinking-toggle": "Thinking switch (thinking)",
    "enable-thinking-toggle": "Thinking switch (enable_thinking)",
    "anthropic-adaptive-effort": "Anthropic adaptive effort",
}


@dataclass(frozen=True)
class ProviderSetupProfile:
    """User-facing provider identity with connection defaults."""

    provider_type: ProviderType
    default_provider_id: str
    display_name: str
    description: str
    base_url: str
    api_path: str
    credential_env: str | None
    endpoint_count: int
    preset: bool

    @classmethod
    def from_template(cls, template: ProviderTemplateView) -> ProviderSetupProfile:
        return cls(
            provider_type=template.provider_type,
            default_provider_id=template.provider_id,
            display_name=template.display_name,
            description=template.description,
            base_url=template.base_url,
            api_path=template.api_path,
            credential_env=template.credential_env,
            endpoint_count=template.endpoint_count,
            preset=True,
        )


COMPATIBLE_PROVIDER_PROFILES = (
    ProviderSetupProfile(
        provider_type="openai-compatible",
        default_provider_id="openai-compatible",
        display_name="OpenAI-compatible",
        description="Connect a service that implements OpenAI Chat Completions.",
        base_url="",
        api_path="/chat/completions",
        credential_env=None,
        endpoint_count=0,
        preset=False,
    ),
    ProviderSetupProfile(
        provider_type="anthropic-compatible",
        default_provider_id="anthropic-compatible",
        display_name="Anthropic-compatible",
        description="Connect a service that implements Anthropic Messages.",
        base_url="",
        api_path="/v1/messages",
        credential_env=None,
        endpoint_count=0,
        preset=False,
    ),
)


class ProviderConfigurationScreen(EditableChoiceScreen):
    """Edit provider settings through the shared grouped inline-edit menu."""

    def __init__(
        self,
        profile: ProviderSetupProfile,
        existing: ProviderView | None = None,
        *,
        theme: TuiTheme = DEFAULT_TUI_THEME,
    ) -> None:
        self._initial_tui_theme = theme
        self._profile = profile
        self._existing = existing
        provider_options = _json_text(existing.provider_options if existing is not None else {})
        self._values = {
            "provider-api-key": "",
            "provider-api-key-env": _credential_environment(profile, existing),
            "provider-id": (
                existing.provider_id if existing is not None else profile.default_provider_id
            ),
            "provider-base-url": (existing.base_url if existing is not None else profile.base_url)
            or "",
            "provider-api-path": (existing.api_path if existing is not None else profile.api_path)
            or "",
            "provider-timeout": str(existing.timeout if existing is not None else 60.0),
            "provider-options": provider_options,
        }
        super().__init__(
            title="EDIT PROVIDER" if existing is not None else "ADD PROVIDER",
            placeholder="Filter provider settings…",
            choices=self._provider_choices(),
            on_save=self._save_field,
        )

    async def _select_choice(self, selected: str) -> None:
        if selected == "provider-save" and self._editing_value is None:
            self._submit()
            return
        await super()._select_choice(selected)

    async def _save_field(
        self,
        field_id: str,
        value: str,
    ) -> tuple[Choice, ...] | None:
        normalized = value.strip()
        self._values[field_id] = normalized
        if normalized and field_id == "provider-api-key":
            self._values["provider-api-key-env"] = ""
        elif normalized and field_id == "provider-api-key-env":
            self._values["provider-api-key"] = ""
        return self._provider_choices()

    def _editor_display_text(self, value: str) -> str:
        if self._editing_value == "provider-api-key":
            return theme_for(self).mask_character * len(value)
        return value

    def _submit(self) -> None:
        try:
            draft = self._draft()
        except ValueError:
            return
        self.dismiss(draft)  # type: ignore[arg-type]

    def _draft(self) -> TuiProviderConfiguration:
        provider_id = self._values["provider-id"]
        base_url = self._values["provider-base-url"]
        api_path = self._values["provider-api-path"]
        api_key = self._values["provider-api-key"]
        api_key_env = self._values["provider-api-key-env"]
        if not provider_id:
            raise ValueError("Provider ID is required")
        if not base_url:
            raise ValueError("Base URL is required")
        if api_key and api_key_env:
            raise ValueError("Use either an API key or an environment variable")
        timeout = _positive_float(
            self._values["provider-timeout"],
            "Request timeout",
        )
        provider_options = _json_object(
            self._values["provider-options"],
            "Request defaults",
        )
        return TuiProviderConfiguration(
            provider_id=provider_id,
            provider_type=self._profile.provider_type,
            base_url=base_url,
            api_path=api_path or None,
            api_key=api_key or None,
            api_key_env=api_key_env or None,
            timeout=timeout,
            provider_options=(
                None
                if self._existing is not None
                and provider_options == self._existing.provider_options
                else provider_options
            ),
            enabled=self._existing.enabled if self._existing is not None else True,
            make_default=False,
        )

    def _validation_error(self) -> str:
        try:
            self._draft()
        except ValueError as exc:
            return str(exc)
        return ""

    def _provider_choices(self) -> tuple[Choice, ...]:
        theme = theme_for(self)
        api_key = self._values["provider-api-key"]
        existing_credential = (
            self._existing is not None and self._existing.credential.reference != "none"
        )
        api_key_display = (
            theme.mask_character * 8
            if api_key
            else theme.inline_separator.join(("Configured", "preserved"))
            if existing_credential and not self._values["provider-api-key-env"]
            else "Not set"
        )
        error = self._validation_error()
        return (
            Choice(
                "provider-api-key",
                "API key",
                trailing=api_key_display,
                group="CREDENTIALS",
                edit_value=api_key,
            ),
            Choice(
                "provider-api-key-env",
                "Environment variable",
                trailing=self._values["provider-api-key-env"] or "Not set",
                group="CREDENTIALS",
                edit_value=self._values["provider-api-key-env"],
            ),
            Choice(
                "provider-id",
                "Provider ID",
                trailing=self._values["provider-id"] or "Required",
                group="ADVANCED",
                disabled=self._existing is not None,
                edit_value=self._values["provider-id"],
            ),
            Choice(
                "provider-base-url",
                "Base URL",
                trailing=self._values["provider-base-url"] or "Required",
                group="ADVANCED",
                edit_value=self._values["provider-base-url"],
            ),
            Choice(
                "provider-api-path",
                "API path",
                trailing=self._values["provider-api-path"] or "Default",
                group="ADVANCED",
                edit_value=self._values["provider-api-path"],
            ),
            Choice(
                "provider-timeout",
                "Request timeout",
                trailing=f"{self._values['provider-timeout']} seconds",
                group="ADVANCED",
                edit_value=self._values["provider-timeout"],
            ),
            Choice(
                "provider-options",
                "Request defaults",
                trailing=_json_summary(self._values["provider-options"]),
                group="REQUEST",
                edit_value=self._values["provider-options"],
                edit_placeholder="Enter a JSON object",
            ),
            Choice(
                "provider-save",
                "Save",
                trailing=error,
                group="ACTIONS",
                disabled=bool(error),
            ),
        )


class EndpointConfigurationScreen(EditableChoiceScreen):
    """Edit an endpoint through the shared grouped inline-edit menu."""

    _CAPABILITY_FIELDS = frozenset(
        {
            "endpoint-tools",
            "endpoint-vision",
            "endpoint-streaming",
            "endpoint-structured-output",
        }
    )

    def __init__(
        self,
        provider: ProviderView,
        existing: ProviderEndpointView | None = None,
        *,
        theme: TuiTheme = DEFAULT_TUI_THEME,
    ) -> None:
        self._initial_tui_theme = theme
        self._provider = provider
        self._existing = existing
        request_options = _json_text(existing.request_options if existing is not None else {})
        reasoning = existing.reasoning if existing is not None else EndpointReasoningConfig()
        self._values = {
            "endpoint-id": existing.endpoint_id if existing is not None else "",
            "endpoint-model-id": existing.model_id if existing is not None else "",
            "endpoint-context-window": str(
                existing.context_window if existing is not None else 128_000
            ),
            "endpoint-max-output": str(
                existing.max_output_tokens if existing is not None else 8192
            ),
            "endpoint-reasoning-config": _reasoning_text(reasoning),
            "endpoint-request-options": request_options,
        }
        self._capabilities = {
            "endpoint-tools": existing.supports_tools if existing is not None else False,
            "endpoint-vision": existing.supports_vision if existing is not None else False,
            "endpoint-streaming": (existing.supports_streaming if existing is not None else True),
            "endpoint-structured-output": (
                existing.supports_structured_output if existing is not None else False
            ),
        }
        super().__init__(
            title="EDIT ENDPOINT" if existing is not None else "ADD ENDPOINT",
            placeholder="Filter endpoint settings…",
            choices=self._endpoint_choices(),
            on_save=self._save_field,
        )

    async def _select_choice(self, selected: str) -> None:
        if self._editing_value is None and selected in self._CAPABILITY_FIELDS:
            self._capabilities[selected] = not self._capabilities[selected]
            self._replace_choices(self._endpoint_choices(), selected=selected)
            return
        if self._editing_value is None and selected == "endpoint-reasoning-preset":
            await self._select_reasoning_preset()
            return
        if selected == "endpoint-save" and self._editing_value is None:
            self._submit()
            return
        await super()._select_choice(selected)

    async def _select_reasoning_preset(self) -> None:
        selected = await self.open_child(
            ChoiceScreen(
                title="THINKING PRESET",
                placeholder="Search thinking presets…",
                choices=self._reasoning_preset_choices(),
                initial_value=self._reasoning_preset_name(),
            )
        )
        if selected not in REASONING_PRESETS:
            return
        self._values["endpoint-reasoning-config"] = _reasoning_text(reasoning_preset(selected))
        self._replace_choices(
            self._endpoint_choices(),
            selected="endpoint-reasoning-preset",
        )

    def _reasoning_preset_name(self) -> str | None:
        try:
            current = _reasoning_config(self._values["endpoint-reasoning-config"])
        except ValueError:
            return None
        return next(
            (name for name in REASONING_PRESETS if reasoning_preset(name) == current),
            None,
        )

    def _reasoning_preset_choices(self) -> tuple[Choice, ...]:
        theme = theme_for(self)
        current_name = self._reasoning_preset_name()
        choices: list[Choice] = []
        if current_name is None:
            choices.append(
                Choice(
                    "custom-reasoning-config",
                    "Custom settings",
                    trailing=_reasoning_summary(
                        self._values["endpoint-reasoning-config"], theme=theme
                    ),
                    group="CURRENT",
                    disabled=True,
                    marker=theme.selected_choice_marker,
                )
            )
        for name in REASONING_PRESETS:
            reasoning = reasoning_preset(name)
            try:
                validate_reasoning_protocol_compatibility(
                    self._provider.protocol,
                    reasoning,
                )
            except ValueError:
                incompatible = True
            else:
                incompatible = False
            choices.append(
                Choice(
                    name,
                    _REASONING_PRESET_LABELS[name],
                    trailing=(
                        "Not supported by this provider"
                        if incompatible
                        else _reasoning_capability_summary(reasoning, theme=theme)
                    ),
                    group="PRESETS",
                    disabled=incompatible,
                    marker=(theme.selected_choice_marker if name == current_name else ""),
                )
            )
        return tuple(choices)

    async def _save_field(
        self,
        field_id: str,
        value: str,
    ) -> tuple[Choice, ...] | None:
        self._values[field_id] = value.strip()
        return self._endpoint_choices()

    def _submit(self) -> None:
        try:
            draft = self._draft()
        except ValueError:
            return
        self.dismiss(draft)  # type: ignore[arg-type]

    def _draft(self) -> TuiEndpointConfiguration:
        endpoint_id = self._values["endpoint-id"]
        model_id = self._values["endpoint-model-id"]
        if not endpoint_id:
            raise ValueError("Endpoint ID is required")
        if not model_id:
            raise ValueError("Provider model ID is required")
        context_window = _positive_int(
            self._values["endpoint-context-window"],
            "Context window",
        )
        max_output_tokens = _positive_int(
            self._values["endpoint-max-output"],
            "Max output tokens",
        )
        if max_output_tokens > context_window:
            raise ValueError("Max output tokens cannot exceed the context window")
        request_options = _json_object(
            self._values["endpoint-request-options"],
            "Request options",
        )
        reasoning = _reasoning_config(self._values["endpoint-reasoning-config"])
        try:
            validate_reasoning_protocol_compatibility(
                self._provider.protocol,
                reasoning,
            )
            reasoning.validate_request_options(
                {**self._provider.provider_options, **request_options}
            )
        except ValueError as exc:
            raise ValueError(f"Reasoning config is incompatible: {exc}") from exc
        return TuiEndpointConfiguration(
            provider_id=self._provider.provider_id,
            endpoint_id=endpoint_id,
            model_id=model_id,
            context_window=context_window,
            max_output_tokens=max_output_tokens,
            supports_tools=self._capabilities["endpoint-tools"],
            supports_vision=self._capabilities["endpoint-vision"],
            supports_streaming=self._capabilities["endpoint-streaming"],
            supports_structured_output=self._capabilities["endpoint-structured-output"],
            reasoning=(
                None
                if self._existing is not None and reasoning == self._existing.reasoning
                else reasoning
            ),
            request_options=(
                None
                if self._existing is not None and request_options == self._existing.request_options
                else request_options
            ),
        )

    def _validation_error(self) -> str:
        try:
            self._draft()
        except ValueError as exc:
            return str(exc)
        return ""

    def _endpoint_choices(self) -> tuple[Choice, ...]:
        theme = theme_for(self)
        error = self._validation_error()

        def state(field: str) -> str:
            return "Enabled" if self._capabilities[field] else "Disabled"

        return (
            Choice(
                "endpoint-id",
                "Endpoint ID",
                trailing=self._values["endpoint-id"] or "Required",
                group="DETAILS",
                disabled=self._existing is not None,
                edit_value=self._values["endpoint-id"],
            ),
            Choice(
                "endpoint-model-id",
                "Model ID",
                trailing=self._values["endpoint-model-id"] or "Required",
                group="DETAILS",
                edit_value=self._values["endpoint-model-id"],
            ),
            Choice(
                "endpoint-context-window",
                "Context window",
                trailing=f"{self._values['endpoint-context-window']} tokens",
                group="LIMITS",
                edit_value=self._values["endpoint-context-window"],
            ),
            Choice(
                "endpoint-max-output",
                "Max output tokens",
                trailing=f"{self._values['endpoint-max-output']} tokens",
                group="LIMITS",
                edit_value=self._values["endpoint-max-output"],
            ),
            Choice(
                "endpoint-tools",
                "Tools",
                trailing=state("endpoint-tools"),
                group="CAPABILITIES",
            ),
            Choice(
                "endpoint-vision",
                "Vision",
                trailing=state("endpoint-vision"),
                group="CAPABILITIES",
            ),
            Choice(
                "endpoint-streaming",
                "Streaming",
                trailing=state("endpoint-streaming"),
                group="CAPABILITIES",
            ),
            Choice(
                "endpoint-structured-output",
                "Structured output",
                trailing=state("endpoint-structured-output"),
                group="CAPABILITIES",
            ),
            Choice(
                "endpoint-reasoning-preset",
                "Thinking preset",
                trailing=(
                    _REASONING_PRESET_LABELS.get(
                        self._reasoning_preset_name() or "",
                        "Custom",
                    )
                ),
                group="THINKING",
            ),
            Choice(
                "endpoint-reasoning-config",
                "Thinking settings (JSON)",
                trailing=_reasoning_summary(self._values["endpoint-reasoning-config"], theme=theme),
                group="THINKING",
                edit_value=self._values["endpoint-reasoning-config"],
                edit_placeholder="Enter a reasoning JSON object",
            ),
            Choice(
                "endpoint-request-options",
                "Request options",
                trailing=_json_summary(self._values["endpoint-request-options"]),
                group="REQUEST",
                edit_value=self._values["endpoint-request-options"],
                edit_placeholder="Enter a JSON object",
            ),
            Choice(
                "endpoint-save",
                "Save",
                trailing=error,
                group="ACTIONS",
                disabled=bool(error),
            ),
        )


def profile_for_provider(
    provider: ProviderView,
    templates: tuple[ProviderTemplateView, ...],
) -> ProviderSetupProfile:
    template = next(
        (item for item in templates if item.provider_type == provider.provider_type),
        None,
    )
    if template is not None:
        return ProviderSetupProfile.from_template(template)
    compatible = next(
        (
            item
            for item in COMPATIBLE_PROVIDER_PROFILES
            if item.provider_type == provider.provider_type
        ),
        None,
    )
    if compatible is None:
        raise ValueError(f"Unsupported configurable provider type: {provider.provider_type}")
    return compatible


def _credential_environment(
    profile: ProviderSetupProfile,
    existing: ProviderView | None,
) -> str:
    if existing is not None:
        reference = existing.credential.reference
        if reference.startswith("env:"):
            return reference.removeprefix("env:")
        if reference != "none":
            return ""
    return profile.credential_env or ""


def _json_text(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_object(raw: str, label: str) -> dict[str, object]:
    normalized = raw.strip()
    if not normalized:
        return {}
    try:
        parsed = json.loads(normalized)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} must be a JSON object")
    return dict(parsed)


def _json_summary(raw: str) -> str:
    try:
        value = _json_object(raw, "Request options")
    except ValueError:
        return "Invalid JSON"
    return "None" if not value else f"{len(value)} fields"


def _reasoning_text(value: EndpointReasoningConfig) -> str:
    return _json_text(dict(value.model_dump(mode="json", exclude_none=True)))


def _reasoning_config(raw: str) -> EndpointReasoningConfig:
    value = _json_object(raw, "Thinking settings (JSON)")
    try:
        return EndpointReasoningConfig.model_validate(value)
    except ValueError as exc:
        raise ValueError(f"Reasoning config is invalid: {exc}") from exc


def _reasoning_summary(
    raw: str,
    *,
    theme: TuiTheme = DEFAULT_TUI_THEME,
) -> str:
    try:
        return _reasoning_capability_summary(_reasoning_config(raw), theme=theme)
    except ValueError:
        return "Invalid settings"


def _reasoning_capability_summary(
    value: EndpointReasoningConfig,
    *,
    theme: TuiTheme = DEFAULT_TUI_THEME,
) -> str:
    capability = value.capability
    if capability.activation == "unsupported":
        return "Unsupported"
    activation = "Always on" if capability.activation == "always_on" else "Toggle"
    effort_count = len(capability.supported_efforts)
    return (
        activation
        if not effort_count
        else theme.inline_separator.join((activation, f"{effort_count} efforts"))
    )


def _positive_int(raw: str, label: str) -> int:
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{label} must be an integer") from exc
    if value < 1:
        raise ValueError(f"{label} must be positive")
    return value


def _positive_float(raw: str, label: str) -> float:
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{label} must be a number") from exc
    if value <= 0:
        raise ValueError(f"{label} must be positive")
    return value
