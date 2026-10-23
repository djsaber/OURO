"""Register providers command arguments."""

from __future__ import annotations

import argparse

from ouro.cli.parsers.arguments import _json_object, _output_limit
from ouro.config.provider_models import CONFIGURABLE_PROVIDER_TYPES
from ouro.config.reasoning import (
    REASONING_EFFORT_PRESETS,
    EndpointReasoningConfig,
    reasoning_effort_preset,
)


def _add_provider_parser(subparsers: argparse._SubParsersAction) -> None:
    provider_parser = subparsers.add_parser(
        "provider",
        help="Manage providers and model endpoints",
        description=(
            "Manage Provider identities, credentials, connection defaults, and model "
            "Endpoints.\nIntegrated types install reviewed Endpoint templates; custom "
            "types start empty."
        ),
        epilog="""Typical workflow:
  ouro provider templates
  ouro provider add "OpenAI" --type openai --api-key-env OPENAI_API_KEY --default
  ouro provider endpoint list PROVIDER_ID
  ouro provider check PROVIDER_ID

For a private or gateway model, add a custom Provider with an explicit --protocol,
then create its model route with 'ouro provider endpoint add'.""",
    )
    provider_subparsers = provider_parser.add_subparsers(
        dest="provider_command",
        required=True,
    )
    _add_provider_auth_parser(provider_subparsers)

    provider_list_parser = provider_subparsers.add_parser("list", help="List providers")
    provider_list_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    provider_list_parser.add_argument("--json", action="store_true", help="Print JSON")

    provider_show_parser = provider_subparsers.add_parser("show", help="Show a provider")
    provider_show_parser.add_argument("provider_id", nargs="?", help="Provider/supplier id")
    provider_show_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    provider_show_parser.add_argument("--json", action="store_true", help="Print JSON")

    provider_templates_parser = provider_subparsers.add_parser(
        "templates",
        help="List or show built-in provider templates",
        description=(
            "List reviewed built-in Provider templates, or show the complete Endpoint "
            "catalog for one template."
        ),
    )
    provider_templates_parser.add_argument("template_id", nargs="?", help="Template id")
    provider_templates_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    provider_templates_parser.add_argument("--json", action="store_true", help="Print JSON")

    provider_restore_template_parser = provider_subparsers.add_parser(
        "restore-template",
        help="Restore a provider template to the inline catalog version",
        description=(
            "Drop local modifications for one provider template in"
            " provider-templates.json and re-apply the reviewed inline content."
        ),
    )
    provider_restore_template_parser.add_argument("template_id", help="Template id")
    provider_restore_template_parser.add_argument(
        "--home", metavar="HOME", help="Override OURO_HOME"
    )
    provider_restore_template_parser.add_argument("--json", action="store_true", help="Print JSON")

    provider_add_parser = provider_subparsers.add_parser(
        "add",
        help="Add a provider; integrated types include preset endpoints",
        description=(
            "Create a new Provider. Integrated types install their reviewed Endpoint "
            "catalog.\nCustom types create an empty Provider for custom Endpoints."
        ),
        epilog="""Examples:
  ouro provider add "OpenAI" --type openai --api-key-env OPENAI_API_KEY --default
  ouro provider add "Private Gateway" --type custom --protocol openai-chat-completions \\
    --base-url https://models.example.com/v1 --api-key-env PRIVATE_API_KEY
  ouro provider add "Private Responses" --type custom --protocol openai-responses \\
    --base-url https://models.example.com/v1 --api-key-env PRIVATE_API_KEY

OURO generates the Provider id. Use 'provider update' for an existing Provider or
'provider replace' when its type and complete template-owned configuration must change.""",
    )
    provider_add_parser.add_argument("display_name", help="Provider display name")
    _add_provider_identity_args(provider_add_parser)
    _add_provider_mutation_args(
        provider_add_parser,
        enabled_default=True,
    )

    provider_update_parser = provider_subparsers.add_parser(
        "update",
        help="Update a provider and safely merge its latest preset",
        description=(
            "Update connection, credential, retry, or enabled state without changing the "
            "Provider type.\nFor preset providers, unchanged template-owned values follow "
            "the latest preset while customized, locally removed, and user-created "
            "Endpoints are preserved."
        ),
    )
    provider_update_parser.add_argument("provider_id", help="Provider/supplier id")
    provider_update_parser.add_argument("--name", dest="display_name", help="Display name")
    _add_provider_mutation_args(
        provider_update_parser,
        enabled_default=None,
    )

    provider_replace_parser = provider_subparsers.add_parser(
        "replace",
        help="Replace a provider; integrated types include preset endpoints",
        description=(
            "Replace an existing Provider with the selected type and its complete initial "
            "configuration.\nUse this when changing Provider type or intentionally resetting "
            "template-owned Endpoints."
        ),
    )
    provider_replace_parser.add_argument("provider_id", help="Provider/supplier id")
    provider_replace_parser.add_argument("--name", dest="display_name", help="Display name")
    _add_provider_identity_args(provider_replace_parser)
    _add_provider_mutation_args(
        provider_replace_parser,
        enabled_default=True,
    )

    provider_use_parser = provider_subparsers.add_parser("use", help="Set default provider")
    provider_use_parser.add_argument("provider_id", help="Provider/supplier id")
    provider_use_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    provider_use_parser.add_argument("--json", action="store_true", help="Print JSON")

    provider_remove_parser = provider_subparsers.add_parser("remove", help="Remove a provider")
    provider_remove_parser.add_argument("provider_id", help="Provider/supplier id")
    provider_remove_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    provider_remove_parser.add_argument("--json", action="store_true", help="Print JSON")

    provider_check_parser = provider_subparsers.add_parser(
        "check",
        help="Check provider connection",
        description=(
            "Send a minimal request through a configured Endpoint.\n"
            "Runtime flags override configuration for this check only and are not persisted."
        ),
    )
    provider_check_parser.add_argument("provider_id", nargs="?", help="Provider/supplier id")
    provider_check_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    provider_check_parser.add_argument("--json", action="store_true", help="Print JSON")
    _add_provider_args(
        provider_check_parser,
        prefix="provider_check_",
        include_provider=False,
    )

    endpoint_parser = provider_subparsers.add_parser(
        "endpoint",
        help="Manage model endpoints for a provider",
        description=(
            "Manage stable model routes and their capabilities.\nReasoning support belongs "
            "to the Endpoint because models behind the same Provider can differ."
        ),
        epilog="""Examples:
  ouro provider endpoint list PROVIDER_ID
  ouro provider endpoint show PROVIDER_ID ENDPOINT_ID
  ouro provider endpoint add PROVIDER_ID --model private-model --name "Private Model"

Use 'ouro provider endpoint add --help' for custom capability and reasoning contracts.""",
    )
    endpoint_subparsers = endpoint_parser.add_subparsers(
        dest="endpoint_command",
        required=True,
    )

    endpoint_list_parser = endpoint_subparsers.add_parser("list", help="List endpoints")
    endpoint_list_parser.add_argument("provider_id", help="Provider/supplier id")
    endpoint_list_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    endpoint_list_parser.add_argument("--json", action="store_true", help="Print JSON")

    endpoint_show_parser = endpoint_subparsers.add_parser("show", help="Show an endpoint")
    endpoint_show_parser.add_argument("provider_id", help="Provider/supplier id")
    endpoint_show_parser.add_argument("endpoint_id", nargs="?", help="Endpoint id")
    endpoint_show_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    endpoint_show_parser.add_argument("--json", action="store_true", help="Print JSON")

    endpoint_add_parser = endpoint_subparsers.add_parser(
        "add",
        help="Add an endpoint",
        description=(
            "Create one model Endpoint with an OURO-generated id.\nReasoning preset and "
            "full JSON options are mutually exclusive."
        ),
        epilog=f"""Examples:
  ouro provider endpoint add PROVIDER_ID --model private-model --default
  ouro provider endpoint add PROVIDER_ID --model reasoning-model \\
    --reasoning-preset openai-chat-completions-effort --default
  ouro provider endpoint add PROVIDER_ID --model custom-claude \\
    --reasoning-preset anthropic-messages-adaptive-effort
  ouro provider endpoint add PROVIDER_ID --model custom-model \\
    --reasoning-preset openai-responses-effort

Reasoning effort presets:
  {", ".join(REASONING_EFFORT_PRESETS)}

Presets are explicit declarations, not capability detection. Use --reasoning-config when a
model supports different native fields or only a subset of efforts. Its JSON shape is:
  {{"capability": {{...}}, "profile": {{"name": "...", "effort": {{...}}, "summary": {{...}}}}}}

Use openai-responses-effort for openai-responses (reasoning.effort).
Use openai-chat-completions-effort for Chat Completions (reasoning_effort).
Responses default inherits the provider setting;
enabled without an effort selects medium, and disabled sends none.
Summary requests are independent of effort presets. In --reasoning-config, declare
capability.supported_summaries=["auto"], capability.default.summary="auto", and a matching
profile.summary mapping when supported. Responses standard mappings use reasoning.summary;
Messages standard mappings use thinking.display. Custom mappings can encode service extensions.
Declare only modes supported by the endpoint. Set default.summary="provider_default"
to omit the summary parameter and leave summary generation to the provider.

Reasoning-owned fields must not also appear in --request-options. Invalid or incomplete
contracts are rejected before configuration is written.""",
    )
    endpoint_add_parser.add_argument("provider_id", help="Provider/supplier id")
    _add_endpoint_mutation_args(endpoint_add_parser, model_required=True)

    endpoint_update_parser = endpoint_subparsers.add_parser(
        "update",
        help="Update an endpoint",
        description="Update one Endpoint selected by its OURO-generated id.",
    )
    endpoint_update_parser.add_argument("provider_id", help="Provider/supplier id")
    endpoint_update_parser.add_argument("endpoint_id", help="Endpoint id")
    _add_endpoint_mutation_args(endpoint_update_parser, model_required=False)

    endpoint_use_parser = endpoint_subparsers.add_parser("use", help="Set default endpoint")
    endpoint_use_parser.add_argument("provider_id", help="Provider/supplier id")
    endpoint_use_parser.add_argument("endpoint_id", help="Endpoint id")
    endpoint_use_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    endpoint_use_parser.add_argument("--json", action="store_true", help="Print JSON")

    endpoint_remove_parser = endpoint_subparsers.add_parser("remove", help="Remove an endpoint")
    endpoint_remove_parser.add_argument("provider_id", help="Provider/supplier id")
    endpoint_remove_parser.add_argument("endpoint_id", help="Endpoint id")
    endpoint_remove_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    endpoint_remove_parser.add_argument("--json", action="store_true", help="Print JSON")


def _add_endpoint_mutation_args(
    parser: argparse.ArgumentParser,
    *,
    model_required: bool,
) -> None:
    parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    parser.add_argument("--name", dest="display_name", help="Optional display name")
    route_group = parser.add_argument_group("model route")
    route_group.add_argument(
        "--model",
        required=model_required,
        metavar="MODEL_ID",
        help="Provider-native model id",
    )
    route_group.add_argument(
        "--base-url",
        metavar="URL",
        help="Endpoint URL override; otherwise inherit the Provider base URL",
    )
    route_group.add_argument(
        "--api-path",
        metavar="PATH",
        help="API path override; otherwise inherit Provider or protocol default",
    )
    route_group.add_argument(
        "--temperature",
        type=float,
        default=None,
        metavar="FLOAT",
        help="Sampling temperature (0-2)",
    )
    route_group.add_argument(
        "--context-window",
        type=int,
        default=None,
        metavar="TOKENS",
        help="Total model context window",
    )
    route_group.add_argument(
        "--max-output-tokens",
        type=_output_limit,
        default=argparse.SUPPRESS,
        metavar="TOKENS|none",
        help="Request output limit; none means no configured limit",
    )
    capability_group = parser.add_argument_group("endpoint capabilities")
    _add_endpoint_capability_args(capability_group)
    reasoning_group = parser.add_argument_group("reasoning contract")
    endpoint_reasoning_group = reasoning_group.add_mutually_exclusive_group()
    endpoint_reasoning_group.add_argument(
        "--reasoning-preset",
        dest="reasoning_effort_preset",
        type=_reasoning_effort_preset,
        metavar="PRESET",
        help="Install a common native reasoning effort mapping",
    )
    endpoint_reasoning_group.add_argument(
        "--reasoning-config",
        type=_endpoint_reasoning_config,
        metavar="JSON",
        help="Complete capability and native profile mapping as one JSON object",
    )
    reasoning_group.add_argument(
        "--request-options",
        type=_json_object,
        metavar="JSON",
        help="Additional provider request fields not owned by the reasoning profile",
    )
    parser.add_argument("--default", action="store_true", help="Set endpoint as default")
    parser.add_argument("--json", action="store_true", help="Print JSON")


def _add_provider_auth_parser(subparsers: argparse._SubParsersAction) -> None:
    auth = subparsers.add_parser(
        "auth",
        help="Inspect authentication and manage local account credentials",
        description=(
            "List login methods, authorize a configured Provider, or manage local credentials.\n"
            "Login waits for a saved result; Ctrl+C cancels and cleans up.\n"
            "Logout/forget clear local credentials, not the remote account session.\n"
            "Use 'methods PROVIDER' to discover its registered login methods."
        ),
    )
    commands = auth.add_subparsers(dest="auth_command", required=True)
    for command, help_text in (
        ("methods", "List available login methods for a Provider"),
        ("login", "Authorize a Provider and wait for its credentials to be saved"),
        ("status", "Read local authentication state without contacting the service"),
        ("logout", "Clear the local credential referenced by a Provider"),
        ("list", "List local credential summaries and their Provider references"),
        ("forget", "Clear a local credential, affecting all Providers that reference it"),
    ):
        parser = commands.add_parser(command, help=help_text, description=help_text)
        if command == "forget":
            parser.add_argument("credential_id", help="Credential id from 'provider auth list'")
        elif command != "list":
            parser.add_argument("provider_id", help="Configured Provider id")
        parser.add_argument("--home", default=argparse.SUPPRESS, help="Override OURO_HOME")
        parser.add_argument("--json", action="store_true", help="One final JSON result on stdout")
        if command == "login":
            parser.add_argument(
                "--method", help="Stable method id; required when several are available"
            )
            parser.add_argument(
                "--open-browser",
                action="store_true",
                help="Open the authorization URL in the system browser",
            )
            parser.epilog = (
                "Authorization URL/code are shown on stderr, including with --json.\n"
                "Use 'provider auth methods PROVIDER' to discover supported method ids."
            )


def _add_provider_identity_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--protocol",
        metavar="PROTOCOL",
        help=(
            "Wire protocol; required for custom Providers. "
            "Integrated types require their template protocol"
        ),
    )
    parser.add_argument(
        "--type",
        dest="provider_type",
        choices=CONFIGURABLE_PROVIDER_TYPES,
        required=True,
        help=(
            "Provider product type; integrated types install templates, custom types "
            "start without Endpoints"
        ),
    )


def _add_provider_mutation_args(
    parser: argparse.ArgumentParser,
    *,
    enabled_default: bool | None,
) -> None:
    parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    parser.add_argument(
        "--model",
        help="Override the default preset endpoint model id (integrated types only)",
    )
    parser.add_argument(
        "--base-url",
        help="Provider base URL used as the default for endpoints",
    )
    parser.add_argument(
        "--api-path",
        help="Provider API path used as the default for endpoints",
    )
    parser.add_argument(
        "--provider-options",
        type=_json_object,
        metavar="JSON",
        help="Additional request body defaults inherited by provider endpoints",
    )
    parser.add_argument(
        "--headers",
        type=_json_object,
        metavar="JSON",
        help="Replace provider header declarations (literal/env/context); {} clears all headers",
    )
    credentials = parser.add_mutually_exclusive_group()
    credentials.add_argument(
        "--api-key",
        help="Inline API key (stored as plain text in local config; prefer --api-key-env)",
    )
    credentials.add_argument(
        "--api-key-env",
        metavar="ENV",
        help="Environment variable containing the API key",
    )
    parser.add_argument("--timeout", type=float, default=None, help="Timeout seconds")
    parser.add_argument(
        "--retry-attempts",
        type=int,
        default=None,
        help="Maximum provider request attempts (including the first request)",
    )
    parser.add_argument(
        "--retry-initial-backoff",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Delay before the first retry",
    )
    parser.add_argument(
        "--retry-max-backoff",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Maximum delay between retries",
    )
    parser.add_argument(
        "--retry-backoff-multiplier",
        type=float,
        default=None,
        help="Exponential retry backoff multiplier",
    )
    parser.add_argument(
        "--enabled",
        action=argparse.BooleanOptionalAction,
        default=enabled_default,
        help="Enable or disable provider",
    )
    parser.add_argument(
        "--default",
        action="store_true",
        help="Set provider as default when it has a default endpoint",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON")


def _add_provider_args(
    parser: argparse.ArgumentParser,
    *,
    prefix: str = "",
    include_provider: bool = True,
) -> None:
    route_group = parser.add_argument_group("model route overrides")
    if include_provider:
        route_group.add_argument(
            "--provider",
            dest=f"{prefix}provider_id",
            default=None,
            metavar="PROVIDER_ID",
            help="Configured provider id",
        )
    route_group.add_argument(
        "--endpoint",
        dest=f"{prefix}endpoint_id",
        metavar="ENDPOINT_ID",
        help="Configured endpoint id",
    )
    route_group.add_argument(
        "--base-url",
        dest=f"{prefix}base_url",
        metavar="URL",
        help="Provider base URL",
    )
    route_group.add_argument(
        "--api-path",
        dest=f"{prefix}api_path",
        metavar="PATH",
        help="Provider API path",
    )
    route_group.add_argument(
        "--api-key",
        dest=f"{prefix}api_key",
        metavar="KEY",
        help="Inline API key for this command (prefer --api-key-env)",
    )
    route_group.add_argument(
        "--api-key-env",
        dest=f"{prefix}api_key_env",
        metavar="ENV",
        help="API key env var",
    )
    route_group.add_argument(
        "--timeout",
        dest=f"{prefix}timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Provider timeout seconds",
    )
    route_group.add_argument(
        "--temperature",
        dest=f"{prefix}temperature",
        type=float,
        default=None,
        metavar="FLOAT",
        help="Sampling temperature",
    )
    route_group.add_argument(
        "--context-window",
        dest=f"{prefix}context_window",
        type=int,
        default=None,
        metavar="INT",
        help="Endpoint context window",
    )
    route_group.add_argument(
        "--max-output-tokens",
        dest=f"{prefix}max_output_tokens",
        type=_output_limit,
        default=argparse.SUPPRESS,
        metavar="TOKENS|none",
        help="Request output limit; none clears the endpoint limit",
    )
    capability_group = parser.add_argument_group("endpoint capability overrides")
    _add_endpoint_capability_args(capability_group, prefix=prefix)

    reasoning_group = parser.add_argument_group("reasoning intent")
    reasoning_group.add_argument(
        "--thinking-mode",
        dest=f"{prefix}thinking_mode",
        choices=("default", "enabled", "disabled"),
        default=None,
        help="Reasoning intent; default uses the Endpoint's configured default",
    )
    reasoning_group.add_argument(
        "--thinking-effort",
        dest=f"{prefix}thinking_effort",
        choices=_THINKING_EFFORTS,
        default=None,
        help="Exact reasoning effort; implies enabled when mode is omitted",
    )


_THINKING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")


def _add_endpoint_capability_args(
    parser: argparse._ActionsContainer,
    *,
    prefix: str = "",
) -> None:
    for options, dest, help_text in (
        (("tools",), "supports_tools", "tool calling"),
        (("image-input",), "supports_image_input", "image input"),
        (("audio-input",), "supports_audio_input", "audio input"),
        (("video-input",), "supports_video_input", "video input"),
        (("streaming",), "supports_streaming", "streaming output"),
        (("structured-output",), "supports_structured_output", "structured output"),
    ):
        parser.add_argument(
            *(f"--{option}" for option in options),
            dest=f"{prefix}{dest}",
            action=argparse.BooleanOptionalAction,
            default=None,
            help=f"Enable or disable {help_text}",
        )


def _endpoint_reasoning_config(value: str) -> EndpointReasoningConfig:
    try:
        return EndpointReasoningConfig.model_validate(_json_object(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid reasoning config: {exc}") from exc


def _reasoning_effort_preset(value: str) -> EndpointReasoningConfig:
    try:
        return reasoning_effort_preset(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
