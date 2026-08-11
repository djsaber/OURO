from __future__ import annotations

from dataclasses import dataclass

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Static

from ouro.application.configuration_models import (
    ProviderEndpointView,
    ProviderTemplateView,
    ProviderView,
)
from ouro.config import ProviderType
from ouro.tui.controller import TuiEndpointConfiguration, TuiProviderConfiguration


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


class ProviderConfigurationScreen(ModalScreen[TuiProviderConfiguration | None]):
    """Edit supplier identity, connection, credentials, and availability."""

    BINDINGS = [
        Binding("escape", "cancel", "Close", show=False),
        Binding("ctrl+c", "cancel", "Close", show=False, priority=True),
        Binding("ctrl+s", "save", "Save", show=False, priority=True),
    ]

    def __init__(
        self,
        profile: ProviderSetupProfile,
        existing: ProviderView | None = None,
    ) -> None:
        super().__init__()
        self._profile = profile
        self._existing = existing

    def compose(self) -> ComposeResult:
        profile = self._profile
        existing = self._existing
        base_url = existing.base_url if existing is not None else profile.base_url
        api_path = existing.api_path if existing is not None else profile.api_path
        with VerticalScroll(classes="configuration-panel provider-configuration-panel"):
            yield Static(
                "EDIT PROVIDER" if existing is not None else "ADD PROVIDER",
                classes="configuration-title",
            )
            yield Static(
                _provider_summary(profile),
                classes="configuration-summary",
            )
            yield Static("IDENTITY", classes="configuration-section")
            yield Label("Provider ID", classes="configuration-label")
            yield Input(
                value=(
                    existing.provider_id
                    if existing is not None
                    else profile.default_provider_id
                ),
                placeholder="e.g. primary",
                disabled=existing is not None,
                id="provider-id",
            )
            yield Static("CONNECTION", classes="configuration-section")
            yield Label("Base URL", classes="configuration-label")
            yield Input(
                value=base_url or "",
                placeholder="https://api.example.com/v1",
                id="provider-base-url",
            )
            yield Label("API path", classes="configuration-label")
            yield Input(
                value=api_path or "",
                placeholder=profile.api_path,
                id="provider-api-path",
            )
            yield Static("CREDENTIALS", classes="configuration-section")
            yield Label("API key", classes="configuration-label")
            yield Input(
                placeholder=(
                    "Blank preserves the configured credential"
                    if existing is not None
                    else "Optional; prefer an environment variable"
                ),
                password=True,
                id="provider-api-key",
            )
            yield Label("API key environment variable", classes="configuration-label")
            yield Input(
                placeholder=_credential_placeholder(profile, existing),
                id="provider-api-key-env",
            )
            yield Static("BEHAVIOR", classes="configuration-section")
            yield Label("Request timeout (seconds)", classes="configuration-label")
            yield Input(
                value=str(existing.timeout if existing is not None else 60.0),
                id="provider-timeout",
            )
            with Horizontal(classes="configuration-checks"):
                yield Checkbox(
                    (
                        "Enabled · default provider"
                        if existing is not None and existing.default
                        else "Enabled"
                    ),
                    value=existing.enabled if existing is not None else True,
                    disabled=existing is not None and existing.default,
                    id="provider-enabled",
                )
            yield Static(
                _provider_note(profile, existing),
                classes="configuration-note",
            )
            yield Static("", classes="configuration-error")
            yield Static(
                "Tab move · Space toggle · Ctrl+S save · Esc cancel",
                classes="configuration-hint",
            )
            with Horizontal(classes="configuration-actions"):
                yield Button("Cancel", id="provider-config-cancel")
                yield Button("Save", variant="primary", id="provider-config-save")

    def on_mount(self) -> None:
        selector = "#provider-base-url" if self._existing is not None else "#provider-id"
        self.query_one(selector, Input).focus()

    @on(Button.Pressed, "#provider-config-cancel")
    def cancel_from_button(self) -> None:
        self.action_cancel()

    @on(Button.Pressed, "#provider-config-save")
    def save_from_button(self) -> None:
        self._submit()

    @on(Input.Submitted)
    def advance_from_input(self) -> None:
        self.focus_next()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_save(self) -> None:
        self._submit()

    def _submit(self) -> None:
        try:
            provider_id = self.query_one("#provider-id", Input).value.strip()
            base_url = self.query_one("#provider-base-url", Input).value.strip()
            api_path = self.query_one("#provider-api-path", Input).value.strip()
            api_key = self.query_one("#provider-api-key", Input).value.strip()
            api_key_env = self.query_one("#provider-api-key-env", Input).value.strip()
            if not provider_id:
                raise ValueError("Provider ID is required")
            if not base_url:
                raise ValueError("Base URL is required")
            if api_key and api_key_env:
                raise ValueError("Use either an API key or an environment variable")
            timeout = _positive_float(
                self.query_one("#provider-timeout", Input).value,
                "Request timeout",
            )
        except ValueError as exc:
            self.query_one(".configuration-error", Static).update(str(exc))
            return
        self.dismiss(
            TuiProviderConfiguration(
                provider_id=provider_id,
                provider_type=self._profile.provider_type,
                base_url=base_url,
                api_path=api_path or None,
                api_key=api_key or None,
                api_key_env=api_key_env or None,
                timeout=timeout,
                enabled=self.query_one("#provider-enabled", Checkbox).value,
                make_default=False,
            )
        )


class EndpointConfigurationScreen(ModalScreen[TuiEndpointConfiguration | None]):
    """Edit only the callable endpoint owned by one selected provider."""

    BINDINGS = [
        Binding("escape", "cancel", "Close", show=False),
        Binding("ctrl+c", "cancel", "Close", show=False, priority=True),
        Binding("ctrl+s", "save", "Save", show=False, priority=True),
    ]

    def __init__(
        self,
        provider: ProviderView,
        existing: ProviderEndpointView | None = None,
    ) -> None:
        super().__init__()
        self._provider = provider
        self._existing = existing

    def compose(self) -> ComposeResult:
        existing = self._existing
        with VerticalScroll(classes="configuration-panel endpoint-configuration-panel"):
            yield Static(
                "EDIT ENDPOINT" if existing is not None else "ADD ENDPOINT",
                classes="configuration-title",
            )
            yield Static(
                f"{self._provider.provider_id} · {self._provider.provider_type}",
                classes="configuration-summary",
            )
            yield Label("Endpoint ID", classes="configuration-label")
            yield Input(
                value=existing.endpoint_id if existing is not None else "",
                placeholder="e.g. fast",
                disabled=existing is not None,
                id="endpoint-id",
            )
            yield Label("Provider model ID", classes="configuration-label")
            yield Input(
                value=existing.model_id if existing is not None else "",
                placeholder="Model name sent to the provider",
                id="endpoint-model-id",
            )
            with Horizontal(classes="configuration-number-row"):
                with Vertical(classes="configuration-number-field"):
                    yield Label("Context window", classes="configuration-label")
                    yield Input(
                        value=str(existing.context_window if existing is not None else 128_000),
                        id="endpoint-context-window",
                    )
                with Vertical(classes="configuration-number-field"):
                    yield Label("Max output tokens", classes="configuration-label")
                    yield Input(
                        value=str(
                            existing.max_output_tokens if existing is not None else 8192
                        ),
                        id="endpoint-max-output",
                    )
            with Horizontal(classes="configuration-checks endpoint-capabilities"):
                yield Checkbox(
                    "Tools",
                    value=existing.supports_tools if existing is not None else True,
                    id="endpoint-tools",
                )
                yield Checkbox(
                    "Vision",
                    value=existing.supports_vision if existing is not None else False,
                    id="endpoint-vision",
                )
                yield Checkbox(
                    "Streaming",
                    value=existing.supports_streaming if existing is not None else True,
                    id="endpoint-streaming",
                )
                yield Checkbox(
                    "Structured output",
                    value=(
                        existing.supports_structured_output if existing is not None else False
                    ),
                    id="endpoint-structured-output",
                )
            yield Static(
                "Connection and credentials are inherited from the provider.",
                classes="configuration-note",
            )
            yield Static("", classes="configuration-error")
            yield Static(
                "Tab move · Space toggle · Ctrl+S save · Esc cancel",
                classes="configuration-hint",
            )
            with Horizontal(classes="configuration-actions"):
                yield Button("Cancel", id="endpoint-config-cancel")
                yield Button("Save", variant="primary", id="endpoint-config-save")

    def on_mount(self) -> None:
        selector = "#endpoint-model-id" if self._existing is not None else "#endpoint-id"
        self.query_one(selector, Input).focus()

    @on(Button.Pressed, "#endpoint-config-cancel")
    def cancel_from_button(self) -> None:
        self.action_cancel()

    @on(Button.Pressed, "#endpoint-config-save")
    def save_from_button(self) -> None:
        self._submit()

    @on(Input.Submitted)
    def submit_from_input(self, event: Input.Submitted) -> None:
        if event.input.id == "endpoint-max-output":
            self._submit()
        else:
            self.focus_next()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_save(self) -> None:
        self._submit()

    def _submit(self) -> None:
        try:
            endpoint_id = self.query_one("#endpoint-id", Input).value.strip()
            model_id = self.query_one("#endpoint-model-id", Input).value.strip()
            if not endpoint_id:
                raise ValueError("Endpoint ID is required")
            if not model_id:
                raise ValueError("Provider model ID is required")
            context_window = _positive_int(
                self.query_one("#endpoint-context-window", Input).value,
                "Context window",
            )
            max_output_tokens = _positive_int(
                self.query_one("#endpoint-max-output", Input).value,
                "Max output tokens",
            )
            if max_output_tokens > context_window:
                raise ValueError("Max output tokens cannot exceed the context window")
        except ValueError as exc:
            self.query_one(".configuration-error", Static).update(str(exc))
            return
        self.dismiss(
            TuiEndpointConfiguration(
                provider_id=self._provider.provider_id,
                endpoint_id=endpoint_id,
                model_id=model_id,
                context_window=context_window,
                max_output_tokens=max_output_tokens,
                supports_tools=self.query_one("#endpoint-tools", Checkbox).value,
                supports_vision=self.query_one("#endpoint-vision", Checkbox).value,
                supports_streaming=self.query_one("#endpoint-streaming", Checkbox).value,
                supports_structured_output=self.query_one(
                    "#endpoint-structured-output", Checkbox
                ).value,
            )
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


def _credential_placeholder(
    profile: ProviderSetupProfile,
    existing: ProviderView | None,
) -> str:
    if existing is not None and existing.credential.reference != "none":
        return f"Blank preserves {existing.credential.reference}"
    if profile.credential_env is not None:
        return f"Blank uses {profile.credential_env}"
    return "Optional environment variable name"


def _provider_summary(profile: ProviderSetupProfile) -> str:
    kind = (
        f"OURO preset · {profile.endpoint_count} endpoint(s)"
        if profile.preset
        else "Compatible provider · add endpoints after saving"
    )
    return f"{profile.display_name} · {kind}"


def _provider_note(
    profile: ProviderSetupProfile,
    existing: ProviderView | None,
) -> str:
    if existing is not None:
        return "Blank API key preserves the configured credential. Endpoints are unchanged."
    if profile.preset:
        return "The reviewed endpoint catalog is installed with this provider."
    return "Save the provider first, then add one or more callable endpoints."


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
