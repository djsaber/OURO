from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence

from ouro.cli.agent_commands import run_agent_command
from ouro.cli.approval_commands import (
    abort_run_command,
    approve_tool_command,
    reject_tool_command,
)
from ouro.cli.config_commands import (
    show_agent_runtime_config,
    show_compaction_config,
    show_permissions_config,
    show_queued_input_config,
    show_run_budget_config,
    show_skill_discovery_config,
    show_tool_execution_config,
)
from ouro.cli.data_commands import run_data_command
from ouro.cli.errors import error_exit_code
from ouro.cli.mcp_commands import (
    check_mcp_connection,
    list_mcp_configs,
    remove_mcp_config,
    set_mcp_config,
    show_mcp_config,
)
from ouro.cli.parser import build_parser
from ouro.cli.plugin_commands import run_plugin_command
from ouro.cli.prompt_commands import run_prompt_command
from ouro.cli.provider_auth_commands import run_provider_auth_command
from ouro.cli.provider_commands import (
    check_provider_connection,
    create_provider_endpoint,
    list_provider_configs,
    list_provider_endpoints,
    list_provider_templates,
    mutate_provider_config,
    remove_provider_config,
    remove_provider_endpoint,
    restore_provider_template,
    show_provider_config,
    show_provider_endpoint,
    update_provider_endpoint,
    use_provider_config,
    use_provider_endpoint,
)
from ouro.cli.resource_commands import (
    export_resource,
    import_resource,
    list_resources,
    remove_resource,
    show_resource,
)
from ouro.cli.run import (
    RuntimeProviderOptions,
    run_json,
    run_print,
)
from ouro.cli.session_commands import (
    archive_session,
    branch_session,
    compact_session,
    list_session_events,
    list_session_messages,
    list_sessions,
    pin_session,
    remove_session,
    rename_session,
    replay_session,
    set_session_afk_mode,
    set_session_approval_mode,
    set_session_tags,
    show_session,
    show_session_context,
    show_session_summaries,
)
from ouro.cli.skills import (
    install_skills,
    list_skills,
    remove_skill,
    set_skill_enabled,
    show_skill,
    show_skill_roots,
    show_skills_prompt,
    update_skill,
    validate_skills,
)
from ouro.cli.usage_commands import (
    UsageFilterArgs,
    audit_usage,
    list_usage,
    modify_usage_entry,
    reset_usage,
    show_usage_entry,
    summarize_usage,
)
from ouro.core.errors import ConfigError, OuroError
from ouro.core.reasoning import ReasoningRequest
from ouro.deployment import SessionBackend
from ouro.mcp import MCPServerConfigUpdate


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _dispatch(args)
    except OuroError as exc:
        if bool(getattr(args, "json", False) or getattr(args, "json_mode", False)):
            print(
                json.dumps(
                    {
                        "status": "failed",
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    },
                    ensure_ascii=False,
                )
            )
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return error_exit_code(exc)


def _dispatch(args: argparse.Namespace) -> int:
    if args.command is not None and (
        args.resume_session_id is not None or args.tui_afk or args.tui_approval_mode != "inherit"
    ):
        raise ConfigError(
            "--session, --approval-mode, and --afk before a command are TUI options; "
            "use 'ouro COMMAND --help' for command-specific options"
        )
    if args.command is None:
        from ouro.tui.entry import run_tui

        return run_tui(
            home=args.home,
            system_prompt=args.system_prompt,
            session_backend=args.session_backend,
            task_backend=args.task_backend,
            approval_mode="auto" if args.tui_afk else args.tui_approval_mode,
            interaction_mode="unattended" if args.tui_afk else "interactive",
            resume_session_id=args.resume_session_id,
        )
    if args.command == "run":
        return _run_command(args)
    if args.command == "agent":
        return asyncio.run(run_agent_command(args))
    if args.command == "session":
        return _session_command(args)
    if args.command == "resource":
        return _resource_command(args)
    if args.command in {"approve", "reject", "abort"}:
        return _approval_command(args)
    if args.command == "config":
        return _config_command(args)
    if args.command == "provider":
        return _provider_command(args)
    if args.command == "mcp":
        return _mcp_command(args)
    if args.command == "plugin":
        return run_plugin_command(args)
    if args.command == "skills":
        return _skills_command(args)
    if args.command == "usage":
        return _usage_command(args)
    if args.command == "data":
        return run_data_command(args)
    raise AssertionError(f"unknown command: {args.command}")


def _run_command(args: argparse.Namespace) -> int:
    prompt = " ".join(args.prompt)
    home = args.run_home or args.home
    provider_options = _provider_options(args, prefix="run_")
    system_prompt = (
        args.run_system_prompt if args.run_system_prompt is not None else args.system_prompt
    )
    run = run_json if args.json_mode else run_print
    return asyncio.run(
        run(
            prompt,
            home=home,
            session_id=args.session_id,
            title=args.title,
            provider_options=provider_options,
            system_prompt=system_prompt,
            queue_mode=args.queue_mode,
            approval_mode="auto" if args.afk else args.approval_mode,
            interaction_mode="unattended" if args.afk else None,
            resource_refs=args.resource_ids,
            session_backend=_selected_session_backend(args, scoped="run_session_backend"),
            task_backend=args.run_task_backend or args.task_backend,
        )
    )


def _session_command(args: argparse.Namespace) -> int:
    home = args.home
    command = args.session_command
    session_backend = _selected_session_backend(args)
    if command == "list":
        return list_sessions(
            home=home,
            json_mode=args.json,
            search=args.search,
            statuses=args.statuses,
            archived=args.archived,
            pinned=args.pinned,
            workspace=args.workspace,
            model=args.model,
            tags=args.tags,
            sort=args.sort,
            session_backend=session_backend,
        )
    if command == "show":
        return show_session(
            args.session_id,
            home=home,
            json_mode=args.json,
            session_backend=session_backend,
        )
    if command == "summary":
        return show_session_summaries(
            args.session_id,
            home=home,
            json_mode=args.json,
            session_backend=session_backend,
        )
    if command == "context":
        return asyncio.run(
            show_session_context(
                args.session_id,
                include_content=args.content,
                home=home,
                json_mode=args.json,
                session_backend=session_backend,
            )
        )
    if command == "rename":
        return rename_session(
            args.session_id,
            args.title,
            home=home,
            json_mode=args.json,
            session_backend=session_backend,
        )
    if command == "tags":
        return set_session_tags(
            args.session_id,
            args.tags,
            home=home,
            json_mode=args.json,
            session_backend=session_backend,
        )
    if command == "approval-mode":
        return set_session_approval_mode(
            args.session_id,
            args.mode,
            home=home,
            json_mode=args.json,
            session_backend=session_backend,
        )
    if command == "afk":
        return set_session_afk_mode(
            args.session_id,
            enabled=args.state == "on",
            home=home,
            json_mode=args.json,
            session_backend=session_backend,
        )
    if command in {"archive", "unarchive"}:
        return archive_session(
            args.session_id,
            archived=command == "archive",
            home=home,
            json_mode=args.json,
            session_backend=session_backend,
        )
    if command in {"pin", "unpin"}:
        return pin_session(
            args.session_id,
            pinned=command == "pin",
            home=home,
            json_mode=args.json,
            session_backend=session_backend,
        )
    if command == "branch":
        return branch_session(
            args.session_id,
            args.message_id,
            home=home,
            json_mode=args.json,
            session_backend=session_backend,
        )
    if command == "replay":
        return replay_session(
            args.session_id,
            home=home,
            run_id=args.run_id,
            json_mode=args.json,
            session_backend=session_backend,
        )
    if command == "remove":
        return remove_session(
            args.session_id,
            home=home,
            force=args.force,
            json_mode=args.json,
            session_backend=session_backend,
        )
    if command == "messages":
        return list_session_messages(
            args.session_id,
            home=home,
            cursor=args.cursor,
            limit=args.limit,
            json_mode=args.json,
            session_backend=session_backend,
        )
    if command == "events":
        return list_session_events(
            args.session_id,
            home=home,
            run_id=args.run_id,
            cursor=args.cursor,
            limit=args.limit,
            json_mode=args.json,
            session_backend=session_backend,
        )
    if command == "compact":
        return asyncio.run(
            compact_session(
                args.session_id,
                home=home,
                json_mode=args.json,
                session_backend=session_backend,
            )
        )
    raise AssertionError(f"unknown session command: {command}")


def _approval_command(args: argparse.Namespace) -> int:
    home = args.home
    session_backend = _selected_session_backend(args)
    if args.command == "approve":
        return asyncio.run(
            approve_tool_command(
                args.session_id,
                home=home,
                run_id=args.run_id,
                arguments_override=args.modify,
                json_mode=args.json,
                session_backend=session_backend,
            )
        )
    if args.command == "reject":
        return asyncio.run(
            reject_tool_command(
                args.session_id,
                home=home,
                run_id=args.run_id,
                reason=args.reason,
                json_mode=args.json,
                session_backend=session_backend,
            )
        )
    if args.command == "abort":
        return asyncio.run(
            abort_run_command(
                args.session_id,
                home=home,
                run_id=args.run_id,
                reason=args.reason,
                json_mode=args.json,
                session_backend=session_backend,
            )
        )
    raise AssertionError(f"unknown approval command: {args.command}")


def _resource_command(args: argparse.Namespace) -> int:
    session_backend = _selected_session_backend(args)
    if args.resource_command == "import":
        return import_resource(
            args.session_id,
            args.path,
            home=args.home,
            media_type=args.media_type,
            json_mode=args.json,
            session_backend=session_backend,
        )
    if args.resource_command == "list":
        return list_resources(
            args.session_id,
            home=args.home,
            json_mode=args.json,
            session_backend=session_backend,
        )
    if args.resource_command == "show":
        return show_resource(
            args.session_id,
            args.resource_id,
            home=args.home,
            json_mode=args.json,
            session_backend=session_backend,
        )
    if args.resource_command == "export":
        return export_resource(
            args.session_id,
            args.resource_id,
            args.output,
            home=args.home,
            force=args.force,
            json_mode=args.json,
            session_backend=session_backend,
        )
    if args.resource_command == "remove":
        return remove_resource(
            args.session_id,
            args.resource_id,
            home=args.home,
            force=args.force,
            json_mode=args.json,
            session_backend=session_backend,
        )
    raise AssertionError(f"unknown resource command: {args.resource_command}")


def _selected_session_backend(
    args: argparse.Namespace,
    *,
    scoped: str = "command_session_backend",
) -> SessionBackend | None:
    return getattr(args, scoped, None) or getattr(args, "session_backend", None)


def _config_command(args: argparse.Namespace) -> int:
    home = args.home
    if args.config_command == "prompt":
        return run_prompt_command(args)

    if args.config_command == "permissions":
        return show_permissions_config(
            home=home,
            tool_name=args.tool,
            plugin_id=args.plugin,
            mode=args.mode,
            json_mode=args.json,
        )

    if args.config_command == "skills":
        return show_skill_discovery_config(
            home=home,
            enabled=args.discovery_enabled,
            enable_sources=args.enable_source,
            disable_sources=args.disable_source,
            json_mode=args.json,
        )

    if args.config_command == "compaction":
        return show_compaction_config(
            home=home,
            provider_id=args.provider_id,
            endpoint_id=args.endpoint_id,
            runtime_endpoint=args.runtime_endpoint,
            default_model=args.default_model,
            temperature=args.temperature,
            max_output_tokens=args.max_output_tokens,
            reasoning=_reasoning_request(args),
            clear=args.clear,
            json_mode=args.json,
        )

    if args.config_command == "tool-execution":
        return show_tool_execution_config(
            home=home,
            mode=args.mode,
            max_concurrency=args.max_concurrency,
            workspace_only=args.workspace_only,
            max_output_chars=args.max_output_chars,
            json_mode=args.json,
        )

    if args.config_command == "queued-inputs":
        return show_queued_input_config(
            home=home,
            consumption_mode=args.consumption_mode,
            json_mode=args.json,
        )

    if args.config_command == "run-budget":
        return show_run_budget_config(
            home=home,
            max_model_rounds=args.max_model_rounds,
            max_input_tokens=args.max_input_tokens,
            max_output_tokens=args.max_output_tokens,
            max_elapsed_seconds=args.max_elapsed_seconds,
            clear=args.clear,
            json_mode=args.json,
        )

    if args.config_command == "agents":
        return show_agent_runtime_config(
            home=home,
            expected_revision=args.expected_revision,
            allowed_agent_id_patterns=args.allowed_agent_id_patterns,
            allowed_model_patterns=args.allowed_model_patterns,
            allowed_execution_modes=args.allowed_execution_modes,
            allow_write=args.allow_write,
            allow_network=args.allow_network,
            allow_mcp=args.allow_mcp,
            allow_external_paths=args.allow_external_paths,
            json_mode=args.json,
        )

    raise AssertionError(f"unknown config command: {args.config_command}")


def _provider_command(args: argparse.Namespace) -> int:
    home = args.home
    if args.provider_command == "auth":
        return asyncio.run(run_provider_auth_command(args))
    if args.provider_command == "endpoint":
        return _provider_endpoint_command(args, home=home)
    if args.provider_command == "list":
        return list_provider_configs(home=home, json_mode=args.json)
    if args.provider_command == "show":
        return show_provider_config(args.provider_id, home=home, json_mode=args.json)
    if args.provider_command == "templates":
        return list_provider_templates(args.template_id, home=home, json_mode=args.json)
    if args.provider_command == "restore-template":
        return restore_provider_template(args.template_id, home=home, json_mode=args.json)
    if args.provider_command in {"add", "update", "replace"}:
        return mutate_provider_config(
            args.provider_command,
            getattr(args, "provider_id", None),
            home=home,
            display_name=getattr(args, "display_name", None),
            provider_type=getattr(args, "provider_type", None),
            protocol=getattr(args, "protocol", None),
            model=args.model,
            base_url=args.base_url,
            api_path=args.api_path,
            api_key=args.api_key,
            api_key_env=args.api_key_env,
            timeout=args.timeout,
            headers=args.headers,
            provider_options=args.provider_options,
            retry_max_attempts=args.retry_attempts,
            retry_initial_backoff_seconds=args.retry_initial_backoff,
            retry_max_backoff_seconds=args.retry_max_backoff,
            retry_backoff_multiplier=args.retry_backoff_multiplier,
            enabled=args.enabled,
            make_default=args.default,
            json_mode=args.json,
        )
    if args.provider_command == "use":
        return use_provider_config(args.provider_id, home=home, json_mode=args.json)
    if args.provider_command == "remove":
        return remove_provider_config(args.provider_id, home=home, json_mode=args.json)
    if args.provider_command == "check":
        return asyncio.run(
            check_provider_connection(
                home=home,
                provider_options=_provider_options(args, prefix="provider_check_"),
                json_mode=args.json,
            )
        )
    raise AssertionError(f"unknown provider command: {args.provider_command}")


def _mcp_command(args: argparse.Namespace) -> int:
    home = args.home
    if args.mcp_command == "list":
        return list_mcp_configs(home=home, json_mode=args.json)
    if args.mcp_command == "show":
        return show_mcp_config(args.server_id, home=home, json_mode=args.json)
    if args.mcp_command == "set":
        return set_mcp_config(
            args.server_id,
            _mcp_config_update(args),
            home=home,
            json_mode=args.json,
        )
    if args.mcp_command == "remove":
        return remove_mcp_config(args.server_id, home=home, json_mode=args.json)
    if args.mcp_command == "check":
        return asyncio.run(check_mcp_connection(args.server_id, home=home, json_mode=args.json))
    raise AssertionError(f"unknown MCP command: {args.mcp_command}")


def _mcp_config_update(args: argparse.Namespace) -> MCPServerConfigUpdate:
    values: dict[str, object] = {}
    for argument, field in (
        ("name", "name"),
        ("transport", "transport"),
        ("mcp_executable", "command"),
        ("url", "url"),
        ("enabled", "enabled"),
        ("auto_start", "auto_start"),
        ("auto_reconnect", "auto_reconnect"),
        ("max_reconnect_attempts", "max_reconnect_attempts"),
        ("reconnect_backoff", "reconnect_backoff_seconds"),
        ("timeout", "timeout_seconds"),
        ("health_check_interval", "health_check_interval_seconds"),
        ("permission_mode", "permission_mode"),
    ):
        value = getattr(args, argument)
        if value is not None:
            values[field] = value

    if args.clear_cwd:
        values["cwd"] = None
    elif args.cwd is not None:
        values["cwd"] = args.cwd

    for argument, field, empty in (
        ("args", "args", []),
        ("inherit_env", "inherit_env", []),
        ("env", "env", {}),
        ("headers", "headers", {}),
        ("credentials", "credential_refs", {}),
        ("tool_effects", "tool_effect_overrides", {}),
    ):
        supplied = getattr(args, argument)
        if getattr(args, f"clear_{argument}"):
            values[field] = empty
        elif supplied is not None:
            values[field] = dict(supplied) if isinstance(empty, dict) else supplied
    return MCPServerConfigUpdate.model_validate(values)


def _provider_endpoint_command(
    args: argparse.Namespace,
    *,
    home: str | None,
) -> int:
    if args.endpoint_command == "list":
        return list_provider_endpoints(args.provider_id, home=home, json_mode=args.json)
    if args.endpoint_command == "show":
        return show_provider_endpoint(
            args.provider_id,
            args.endpoint_id,
            home=home,
            json_mode=args.json,
        )
    if args.endpoint_command in {"add", "update"}:
        command = (
            create_provider_endpoint if args.endpoint_command == "add" else update_provider_endpoint
        )
        return command(
            args.provider_id,
            *(() if args.endpoint_command == "add" else (args.endpoint_id,)),
            home=home,
            display_name=args.display_name,
            model=args.model,
            base_url=args.base_url,
            api_path=args.api_path,
            temperature=args.temperature,
            context_window=args.context_window,
            max_output_tokens=args.max_output_tokens,
            supports_tools=args.supports_tools,
            supports_vision=args.supports_vision,
            supports_streaming=args.supports_streaming,
            supports_structured_output=args.supports_structured_output,
            reasoning=args.reasoning_config or args.reasoning_preset,
            request_options=args.request_options,
            make_default=args.default,
            json_mode=args.json,
        )
    if args.endpoint_command == "use":
        return use_provider_endpoint(
            args.provider_id,
            args.endpoint_id,
            home=home,
            json_mode=args.json,
        )
    if args.endpoint_command == "remove":
        return remove_provider_endpoint(
            args.provider_id,
            args.endpoint_id,
            home=home,
            json_mode=args.json,
        )
    raise AssertionError(f"unknown provider endpoint command: {args.endpoint_command}")


def _skills_command(args: argparse.Namespace) -> int:
    home = args.home
    if args.skills_command == "list":
        return list_skills(
            home=home,
            json_mode=args.json,
            include_disabled=args.skills_all,
        )
    if args.skills_command == "show":
        return show_skill(args.skill_name, home=home, json_mode=args.json)
    if args.skills_command == "roots":
        return show_skill_roots(home=home, json_mode=args.json)
    if args.skills_command == "prompt":
        return show_skills_prompt(home=home, json_mode=args.json)
    if args.skills_command == "validate":
        return validate_skills(args.path, home=home, json_mode=args.json)
    if args.skills_command == "install":
        return install_skills(
            args.source,
            home=home,
            scope=args.scope,
            selector=args.skill_selector,
            install_all=args.install_all,
            force=args.force,
            ref=args.ref,
            json_mode=args.json,
        )
    if args.skills_command == "update":
        return update_skill(args.skill_name, home=home, json_mode=args.json)
    if args.skills_command == "remove":
        return remove_skill(
            args.skill_name,
            home=home,
            force=args.force,
            json_mode=args.json,
        )
    if args.skills_command in {"enable", "disable"}:
        return set_skill_enabled(
            args.skill_name,
            enabled=args.skills_command == "enable",
            home=home,
            json_mode=args.json,
        )
    raise AssertionError(f"unknown skills command: {args.skills_command}")


def _usage_command(args: argparse.Namespace) -> int:
    command = args.usage_command
    common = {"home": args.home, "json_mode": args.json}
    if command == "summary":
        return summarize_usage(
            home=args.home,
            json_mode=args.json,
            **_usage_filters(args),
            group_by=args.group_by,
        )
    if command == "list":
        return list_usage(
            home=args.home,
            json_mode=args.json,
            **_usage_filters(args),
            limit=args.limit,
        )
    if command == "show":
        return show_usage_entry(args.entry_id, **common)
    if command == "modify":
        return modify_usage_entry(
            args.entry_id,
            **common,
            input_tokens=args.input_tokens,
            output_tokens=args.output_tokens,
            cache_read_tokens=args.cache_read_tokens,
            cache_write_tokens=args.cache_write_tokens,
            estimated_cost=args.estimated_cost,
            clear_estimated_cost=args.clear_estimated_cost,
            note=args.note,
        )
    if command == "reset":
        return reset_usage(
            home=args.home,
            json_mode=args.json,
            **_usage_filters(args),
            force=args.force,
            note=args.note,
        )
    if command == "audit":
        return audit_usage(**common, limit=args.limit)
    raise AssertionError(f"unknown usage command: {command}")


def _usage_filters(args: argparse.Namespace) -> UsageFilterArgs:
    return {
        "since": args.since,
        "until": args.until,
        "provider_ids": args.provider_ids,
        "endpoint_ids": args.endpoint_ids,
        "model_ids": args.model_ids,
        "session_ids": args.session_ids,
        "run_ids": args.run_ids,
        "purposes": args.purposes,
    }


def _provider_options(args: argparse.Namespace, *, prefix: str = "") -> RuntimeProviderOptions:
    return RuntimeProviderOptions(
        provider_id=_argument_value(args, prefix, "provider_id"),
        endpoint_id=_argument_value(args, prefix, "endpoint_id"),
        base_url=_argument_value(args, prefix, "base_url"),
        api_path=_argument_value(args, prefix, "api_path"),
        api_key=_argument_value(args, prefix, "api_key"),
        api_key_env=_argument_value(args, prefix, "api_key_env"),
        timeout=_argument_value(args, prefix, "timeout"),
        temperature=_argument_value(args, prefix, "temperature"),
        context_window=_argument_value(args, prefix, "context_window"),
        max_output_tokens=_argument_value(args, prefix, "max_output_tokens"),
        supports_tools=_argument_value(args, prefix, "supports_tools"),
        supports_vision=_argument_value(args, prefix, "supports_vision"),
        supports_streaming=_argument_value(args, prefix, "supports_streaming"),
        supports_structured_output=_argument_value(
            args,
            prefix,
            "supports_structured_output",
        ),
        reasoning_request=_reasoning_request(args, prefix=prefix),
    )


def _reasoning_request(
    args: argparse.Namespace,
    *,
    prefix: str = "",
) -> ReasoningRequest | None:
    mode = _argument_value(args, prefix, "thinking_mode")
    effort = _argument_value(args, prefix, "thinking_effort")
    if mode is None and effort is None:
        return None
    try:
        return ReasoningRequest(mode=mode or "default", effort=effort)
    except ValueError as exc:
        raise ConfigError(f"invalid reasoning request: {exc}") from exc


def _argument_value(
    args: argparse.Namespace,
    prefix: str,
    name: str,
):
    prefixed = getattr(args, f"{prefix}{name}", None)
    return prefixed if prefixed is not None else getattr(args, name, None)


if __name__ == "__main__":
    raise SystemExit(main())
