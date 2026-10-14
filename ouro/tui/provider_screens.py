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
    ProviderProtocol,
    ProviderType,
    reasoning_preset,
)
from ouro.config.provider_headers import parse_provider_headers
from ouro.config.provider_models import (
    default_api_path,
    resolve_provider_connection,
    validate_media_protocol_compatibility,
    validate_reasoning_protocol_compatibility,
)
from ouro.tui.controller import TuiEndpointConfiguration, TuiProviderConfiguration
from ouro.tui.credential_editor import (
    CredentialEdit,
    CredentialEditableChoiceScreen,
    credential_trailing,
)
from ouro.tui.screens import Choice, ChoiceScreen, EditableChoiceScreen
from ouro.tui.theme import DEFAULT_TUI_THEME, TuiTheme, theme_for

_REASONING_PRESET_LABELS = {
    "none": "None",
    "openai-chat-completions-effort": "OpenAI Chat Completions effort",
    "openai-responses-effort": "OpenAI Responses effort",
    "openai-responses-effort-summary": "OpenAI Responses effort + request summary",
    "reasoning-content-effort": "Thinking level (reasoning_content)",
    "thinking-toggle": "Thinking switch (thinking)",
    "enable-thinking-toggle": "Thinking switch (enable_thinking)",
    "anthropic-messages-adaptive-effort": "Anthropic Messages adaptive effort",
}

_PROTOCOL_LABELS: dict[ProviderProtocol, str] = {
    "openai-chat-completions": "OpenAI Chat Completions",
    "openai-responses": "OpenAI Responses",
    "anthropic-messages": "Anthropic Messages",
}


@dataclass(frozen=True)
class ProviderSetupProfile:
    """User-facing provider identity with connection defaults."""

    provider_type: ProviderType
    protocol: ProviderProtocol
    display_name: str
    description: str
    base_url: str
    api_path: str
    credential_env: str | None
    endpoint_count: int
    preset: bool
    auth_driver: str = "static"
    notice: str | None = None

    @classmethod
    def from_template(cls, template: ProviderTemplateView) -> ProviderSetupProfile:
        return cls(
            provider_type=template.provider_type,
            protocol=template.protocol,
            display_name=template.display_name,
            description=template.description,
            base_url=template.base_url,
            api_path=template.api_path,
            credential_env=template.credential_env,
            endpoint_count=template.endpoint_count,
            preset=True,
            auth_driver=template.auth_driver,
            notice=template.notice,
        )


CUSTOM_PROVIDER_PROFILES = tuple(
    ProviderSetupProfile(
        provider_type="custom",
        protocol=protocol,
        display_name=display_name,
        description=f"Connect a service that implements {display_name}.",
        base_url="",
        api_path=default_api_path(protocol) or "",
        credential_env=None,
        endpoint_count=0,
        preset=False,
    )
    for protocol, display_name in _PROTOCOL_LABELS.items()
)


class ProviderConfigurationScreen(CredentialEditableChoiceScreen):
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
        self._auth_driver = existing.auth_driver if existing is not None else profile.auth_driver
        self._protocol = (
            existing.protocol
            if existing is not None
            else resolve_provider_connection(
                profile.provider_type, profile.protocol, self._auth_driver
            ).protocol
        )
        provider_options = _json_text(existing.provider_options if existing is not None else {})
        self._values = {
            "provider-api-key": "",
            "provider-api-key-env": _credential_environment(existing),
            "provider-display-name": (
                existing.display_name if existing is not None else profile.display_name
            ),
            "provider-base-url": (existing.base_url if existing is not None else profile.base_url)
            or "",
            "provider-api-path": (existing.api_path if existing is not None else profile.api_path)
            or "",
            "provider-timeout": str(existing.timeout if existing is not None else 60.0),
            "provider-options": provider_options,
            "provider-headers": "",
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

    def _credential_value(self, field_id: str) -> CredentialEdit | None:
        if field_id != "provider-credential":
            return None
        env = self._values["provider-api-key-env"]
        return (
            CredentialEdit("env", f"env:{env}")
            if env
            else CredentialEdit("inline", self._values["provider-api-key"])
        )

    async def _save_credential(self, field_id: str, edit: CredentialEdit) -> tuple[Choice, ...]:
        value = edit.value
        if edit.source == "env":
            value = value.strip().removeprefix("env:").strip()
        if value.strip():
            self._values["provider-api-key"] = value if edit.source == "inline" else ""
            self._values["provider-api-key-env"] = value if edit.source == "env" else ""
        return self._provider_choices()

    def _has_saved_key(self) -> bool:
        return self._existing is not None and self._existing.credential.reference.startswith(
            "inline:"
        )

    async def _save_field(
        self,
        field_id: str,
        value: str,
    ) -> tuple[Choice, ...] | None:
        normalized = value.strip()
        self._values[field_id] = normalized
        return self._provider_choices()

    def _submit(self) -> None:
        try:
            draft = self._draft()
        except ValueError:
            return
        self.dismiss(draft)  # type: ignore[arg-type]

    def _draft(self) -> TuiProviderConfiguration:
        display_name = self._values["provider-display-name"]
        base_url = self._values["provider-base-url"]
        api_path = self._values["provider-api-path"]
        api_key = self._values["provider-api-key"]
        api_key_env = self._values["provider-api-key-env"]
        if not display_name:
            raise ValueError("Display name is required")
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
        headers_text = self._values["provider-headers"]
        headers = _json_object(headers_text, "Request headers") if headers_text else None
        if headers is not None:
            parse_provider_headers(headers)
        return TuiProviderConfiguration(
            display_name=display_name,
            provider_type=self._profile.provider_type,
            protocol=self._protocol,
            base_url=base_url,
            api_path=api_path or None,
            api_key=api_key or None,
            api_key_env=api_key_env or None,
            timeout=timeout,
            headers=headers,
            provider_options=(
                None
                if self._existing is not None
                and provider_options == self._existing.provider_options
                else provider_options
            ),
            enabled=self._existing.enabled if self._existing is not None else True,
            make_default=False,
            auth_driver=self._auth_driver,
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
        edit = self._credential_value("provider-credential")
        assert edit is not None
        error = self._validation_error()
        credential_choices = (
            (
                Choice(
                    "provider-credential",
                    "API key",
                    trailing=credential_trailing(
                        edit,
                        configured=bool(api_key or self._values["provider-api-key-env"])
                        or self._has_saved_key(),
                        theme=theme,
                    ),
                    group="CREDENTIALS",
                    edit_value=edit.value,
                    edit_placeholder="blank keeps current credential",
                ),
            )
            if self._auth_driver == "static"
            else (
                Choice(
                    "provider-authentication",
                    "Managed sign-in",
                    trailing=self._auth_driver,
                    description="Save this provider, then open Authentication to sign in.",
                    group="CREDENTIALS",
                    disabled=True,
                ),
            )
        )
        return (
            *credential_choices,
            *_service_notice_choices(
                self._existing.notice if self._existing is not None else self._profile.notice
            ),
            Choice(
                "provider-display-name",
                "Display name",
                trailing=self._values["provider-display-name"] or "Required",
                group="DETAILS",
                edit_value=self._values["provider-display-name"],
            ),
            *(
                (
                    Choice(
                        "provider-protocol",
                        "Protocol",
                        trailing=_PROTOCOL_LABELS.get(self._protocol, self._protocol),
                        group="ADVANCED",
                        disabled=True,
                    ),
                )
                if self._existing is not None or self._profile.preset
                else ()
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
                "provider-headers",
                "Request headers",
                trailing=(
                    "Replace"
                    if self._values["provider-headers"]
                    else f"Keep ({len(self._existing.headers) if self._existing else 0} configured)"
                ),
                description="Blank keeps existing headers; {} clears; a JSON object replaces all.",
                group="REQUEST",
                edit_value=self._values["provider-headers"],
                edit_placeholder="Header declarations as JSON; blank keeps existing",
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
            "endpoint-image-input",
            "endpoint-audio-input",
            "endpoint-video-input",
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
            "endpoint-display-name": (
                existing.display_name if existing is not None and existing.display_name else ""
            ),
            "endpoint-model-id": existing.model_id if existing is not None else "",
            "endpoint-context-window": str(
                existing.context_window if existing is not None else 1_000_000
            ),
            "endpoint-max-output": str(
                existing.max_output_tokens if existing is not None else 32_768
            ),
            "endpoint-reasoning-config": _reasoning_text(reasoning),
            "endpoint-request-options": request_options,
        }
        self._capabilities = {
            "endpoint-tools": existing.supports_tools if existing is not None else True,
            "endpoint-image-input": existing.supports_image_input
            if existing is not None
            else False,
            "endpoint-audio-input": existing.supports_audio_input
            if existing is not None
            else False,
            "endpoint-video-input": existing.supports_video_input
            if existing is not None
            else False,
            "endpoint-streaming": (existing.supports_streaming if existing is not None else True),
            "endpoint-structured-output": (
                existing.supports_structured_output if existing is not None else True
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
        display_name = self._values["endpoint-display-name"]
        model_id = self._values["endpoint-model-id"]
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
        validate_media_protocol_compatibility(
            self._provider.protocol,
            supports_image_input=self._capabilities["endpoint-image-input"],
            supports_audio_input=self._capabilities["endpoint-audio-input"],
            supports_video_input=self._capabilities["endpoint-video-input"],
        )
        return TuiEndpointConfiguration(
            display_name=display_name or None,
            model_id=model_id,
            context_window=context_window,
            max_output_tokens=max_output_tokens,
            supports_tools=self._capabilities["endpoint-tools"],
            supports_image_input=self._capabilities["endpoint-image-input"],
            supports_audio_input=self._capabilities["endpoint-audio-input"],
            supports_video_input=self._capabilities["endpoint-video-input"],
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
            *_service_notice_choices(self._provider.notice),
            Choice(
                "endpoint-display-name",
                "Display name",
                trailing=self._values["endpoint-display-name"] or "Optional",
                group="DETAILS",
                edit_value=self._values["endpoint-display-name"],
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
                "endpoint-image-input",
                "Image input",
                trailing=state("endpoint-image-input"),
                description="Accept images as model input.",
                group="CAPABILITIES",
            ),
            Choice(
                "endpoint-audio-input",
                "Audio input",
                trailing=state("endpoint-audio-input"),
                description="Accept audio as model input.",
                group="CAPABILITIES",
            ),
            Choice(
                "endpoint-video-input",
                "Video input",
                trailing=state("endpoint-video-input"),
                description="Accept video as model input.",
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
        (
            item
            for item in templates
            if item.provider_type == provider.provider_type and item.protocol == provider.protocol
        ),
        None,
    )
    if template is not None:
        return ProviderSetupProfile.from_template(template)
    profile = next(
        (
            item
            for item in CUSTOM_PROVIDER_PROFILES
            if item.provider_type == provider.provider_type and item.protocol == provider.protocol
        ),
        None,
    )
    if profile is None:
        raise ValueError(
            f"Unsupported provider connection: {provider.provider_type}/{provider.protocol}"
        )
    return profile


def _credential_environment(existing: ProviderView | None) -> str:
    if existing is not None and existing.auth_driver == "static":
        reference = existing.credential.reference
        if reference.startswith("env:"):
            return reference.removeprefix("env:")
    return ""


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


def _service_notice_choices(notice: str | None) -> tuple[Choice, ...]:
    if notice is None:
        return ()
    return (
        Choice(
            "service-notice", "Service limits", description=notice, group="DETAILS", disabled=True
        ),
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
