from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence

from ouro.cli.approval_commands import (
    abort_run_command,
    approve_tool_command,
    reject_tool_command,
)
from ouro.cli.config_commands import (
    clear_system_prompt_config,
    set_system_prompt_config,
    show_compaction_config,
    show_permissions_config,
    show_queued_input_config,
    show_run_budget_config,
    show_system_prompt_config,
    show_tool_execution_config,
)
from ouro.cli.interactive import run_interactive
from ouro.cli.parser import build_parser
from ouro.cli.provider_commands import (
    check_provider_connection,
    list_provider_configs,
    list_provider_endpoints,
    remove_provider_config,
    remove_provider_endpoint,
    set_provider_config,
    set_provider_endpoint,
    show_provider_config,
    show_provider_endpoint,
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
    export_session,
    list_session_events,
    list_session_messages,
    list_sessions,
    pin_session,
    remove_session,
    rename_session,
    replay_session,
    show_session,
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


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run":
        return _run_command(args)
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
    if args.command == "skills":
        return _skills_command(args)
    return asyncio.run(
        run_interactive(
            home=args.home,
            system_prompt=args.system_prompt,
        )
    )


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
        )
    )


def _session_command(args: argparse.Namespace) -> int:
    home = args.home
    command = args.session_command
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
        )
    if command == "show":
        return show_session(args.session_id, home=home, json_mode=args.json)
    if command == "rename":
        return rename_session(args.session_id, args.title, home=home, json_mode=args.json)
    if command in {"archive", "unarchive"}:
        return archive_session(
            args.session_id,
            archived=command == "archive",
            home=home,
            json_mode=args.json,
        )
    if command in {"pin", "unpin"}:
        return pin_session(
            args.session_id,
            pinned=command == "pin",
            home=home,
            json_mode=args.json,
        )
    if command == "branch":
        return branch_session(
            args.session_id,
            args.message_id,
            home=home,
            json_mode=args.json,
        )
    if command == "replay":
        return replay_session(
            args.session_id,
            home=home,
            run_id=args.run_id,
            json_mode=args.json,
        )
    if command == "remove":
        return remove_session(args.session_id, home=home, force=args.force, json_mode=args.json)
    if command == "export":
        return export_session(
            args.session_id,
            home=home,
            output_path=args.output,
            json_mode=args.json,
        )
    if command == "messages":
        return list_session_messages(args.session_id, home=home, json_mode=args.json)
    if command == "events":
        return list_session_events(
            args.session_id,
            home=home,
            run_id=args.run_id,
            json_mode=args.json,
        )
    if command == "compact":
        return asyncio.run(
            compact_session(
                args.session_id,
                home=home,
                json_mode=args.json,
            )
        )
    raise AssertionError(f"unknown session command: {command}")


def _approval_command(args: argparse.Namespace) -> int:
    home = args.home
    if args.command == "approve":
        return asyncio.run(
            approve_tool_command(
                args.session_id,
                home=home,
                run_id=args.run_id,
                arguments_override=args.modify,
                json_mode=args.json,
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
            )
        )
    raise AssertionError(f"unknown approval command: {args.command}")


def _resource_command(args: argparse.Namespace) -> int:
    if args.resource_command == "import":
        return import_resource(
            args.session_id,
            args.path,
            home=args.home,
            media_type=args.media_type,
            json_mode=args.json,
        )
    if args.resource_command == "list":
        return list_resources(args.session_id, home=args.home, json_mode=args.json)
    if args.resource_command == "show":
        return show_resource(
            args.session_id,
            args.resource_id,
            home=args.home,
            json_mode=args.json,
        )
    if args.resource_command == "export":
        return export_resource(
            args.session_id,
            args.resource_id,
            args.output,
            home=args.home,
            force=args.force,
            json_mode=args.json,
        )
    if args.resource_command == "remove":
        return remove_resource(
            args.session_id,
            args.resource_id,
            home=args.home,
            force=args.force,
            json_mode=args.json,
        )
    raise AssertionError(f"unknown resource command: {args.resource_command}")


def _config_command(args: argparse.Namespace) -> int:
    home = args.home
    if args.config_command == "prompt":
        if args.prompt_value is not None:
            return set_system_prompt_config(
                args.prompt_value,
                home=home,
                json_mode=args.prompt_json,
            )
        if args.prompt_clear:
            return clear_system_prompt_config(home=home, json_mode=args.prompt_json)
        return show_system_prompt_config(home=home, json_mode=args.prompt_json)

    if args.config_command == "permissions":
        return show_permissions_config(
            home=home,
            tool_name=args.tool,
            mode=args.mode,
            json_mode=args.json,
        )

    if args.config_command == "compaction":
        return show_compaction_config(
            home=home,
            provider_id=args.provider_id,
            endpoint_id=args.endpoint_id,
            runtime_endpoint=args.runtime_endpoint,
            temperature=args.temperature,
            max_output_tokens=args.max_output_tokens,
            thinking_enabled=args.thinking,
            thinking_level=args.thinking_level,
            clear=args.clear,
            json_mode=args.json,
        )

    if args.config_command == "tool-execution":
        return show_tool_execution_config(
            home=home,
            mode=args.mode,
            max_concurrency=args.max_concurrency,
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

    raise AssertionError(f"unknown config command: {args.config_command}")


def _provider_command(args: argparse.Namespace) -> int:
    home = args.home
    if args.provider_command == "endpoint":
        return _provider_endpoint_command(args, home=home)
    if args.provider_command == "list":
        return list_provider_configs(home=home, json_mode=args.json)
    if args.provider_command == "show":
        return show_provider_config(args.provider_id, home=home, json_mode=args.json)
    if args.provider_command == "set":
        return set_provider_config(
            args.provider_id,
            home=home,
            provider_type=args.kind,
            base_url=args.base_url,
            api_path=args.api_path,
            api_key=args.api_key,
            api_key_env=args.api_key_env,
            timeout=args.timeout,
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
    if args.endpoint_command == "set":
        return set_provider_endpoint(
            args.provider_id,
            args.endpoint_id,
            home=home,
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
            supports_reasoning=args.supports_reasoning,
            supported_thinking_levels=args.thinking_levels,
            default_thinking_level=args.thinking_level,
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
        supports_reasoning=_argument_value(args, prefix, "supports_reasoning"),
        thinking_level=_argument_value(args, prefix, "thinking_level"),
    )


def _argument_value(
    args: argparse.Namespace,
    prefix: str,
    name: str,
):
    prefixed = getattr(args, f"{prefix}{name}", None)
    return prefixed if prefixed is not None else getattr(args, name, None)


if __name__ == "__main__":
    raise SystemExit(main())
