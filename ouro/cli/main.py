from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence

from ouro.cli.interactive import run_interactive
from ouro.cli.run import (
    RuntimeProviderOptions,
    check_provider_connection,
    list_provider_configs,
    remove_provider_config,
    run_json,
    run_print,
    set_provider_config,
    show_provider_config,
    use_provider_config,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ouro")
    parser.add_argument("--home", help="Override OURO_HOME")
    parser.add_argument("--system-prompt", help="Override the default system prompt")
    _add_provider_args(parser)
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Run a single prompt")
    mode = run_parser.add_mutually_exclusive_group()
    mode.add_argument("--print", dest="print_mode", action="store_true", help="Print final text")
    mode.add_argument("--json", dest="json_mode", action="store_true", help="Print JSON result")
    run_parser.add_argument("--home", dest="run_home", help="Override OURO_HOME")
    run_parser.add_argument("--session", dest="session_id", help="Continue an existing session")
    run_parser.add_argument("--title", help="Title for a new session")
    run_parser.add_argument(
        "--system-prompt",
        dest="run_system_prompt",
        help="Override the default system prompt",
    )
    _add_provider_args(run_parser, prefix="run_")
    run_parser.add_argument("prompt", nargs="+")

    config_parser = subparsers.add_parser("config", help="Inspect configuration")
    config_subparsers = config_parser.add_subparsers(dest="config_command", required=True)
    check_parser = config_subparsers.add_parser("check", help="Check provider connection")
    check_parser.add_argument("--home", dest="check_home", help="Override OURO_HOME")
    check_parser.add_argument("--json", dest="check_json", action="store_true", help="Print JSON")
    _add_provider_args(check_parser, prefix="check_")

    provider_parser = subparsers.add_parser("provider", help="Manage providers")
    provider_subparsers = provider_parser.add_subparsers(
        dest="provider_command",
        required=True,
    )

    provider_list_parser = provider_subparsers.add_parser("list", help="List providers")
    provider_list_parser.add_argument("--home", dest="provider_home", help="Override OURO_HOME")
    provider_list_parser.add_argument("--json", action="store_true", help="Print JSON")

    provider_show_parser = provider_subparsers.add_parser("show", help="Show a provider")
    provider_show_parser.add_argument("provider_id", nargs="?", help="Provider/supplier id")
    provider_show_parser.add_argument("--home", dest="provider_home", help="Override OURO_HOME")
    provider_show_parser.add_argument("--json", action="store_true", help="Print JSON")

    provider_set_parser = provider_subparsers.add_parser("set", help="Create or update a provider")
    provider_set_parser.add_argument("provider_id", help="Provider/supplier id")
    provider_set_parser.add_argument("--home", dest="provider_home", help="Override OURO_HOME")
    provider_set_parser.add_argument(
        "--kind",
        choices=["faux", "openai-compatible"],
        default=None,
        help="Provider kind",
    )
    provider_set_parser.add_argument("--model", help="Default model id")
    provider_set_parser.add_argument("--base-url", help="Provider base URL")
    provider_set_parser.add_argument("--api-key", help="Inline API key stored in local config")
    provider_set_parser.add_argument("--api-key-env", help="API key env var")
    provider_set_parser.add_argument("--timeout", type=float, default=None, help="Timeout seconds")
    provider_set_parser.add_argument(
        "--enabled",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable or disable provider",
    )
    provider_set_parser.add_argument(
        "--default",
        action="store_true",
        help="Set provider as default",
    )
    provider_set_parser.add_argument("--json", action="store_true", help="Print JSON")

    provider_use_parser = provider_subparsers.add_parser("use", help="Set default provider")
    provider_use_parser.add_argument("provider_id", help="Provider/supplier id")
    provider_use_parser.add_argument("--home", dest="provider_home", help="Override OURO_HOME")
    provider_use_parser.add_argument("--json", action="store_true", help="Print JSON")

    provider_remove_parser = provider_subparsers.add_parser("remove", help="Remove a provider")
    provider_remove_parser.add_argument("provider_id", help="Provider/supplier id")
    provider_remove_parser.add_argument("--home", dest="provider_home", help="Override OURO_HOME")
    provider_remove_parser.add_argument("--json", action="store_true", help="Print JSON")

    provider_check_parser = provider_subparsers.add_parser(
        "check",
        help="Check provider connection",
    )
    provider_check_parser.add_argument("provider_id", nargs="?", help="Provider/supplier id")
    provider_check_parser.add_argument("--home", dest="provider_home", help="Override OURO_HOME")
    provider_check_parser.add_argument("--json", action="store_true", help="Print JSON")
    _add_provider_args(provider_check_parser, prefix="provider_check_")

    args = parser.parse_args(argv)
    if args.command == "run":
        prompt = " ".join(args.prompt)
        home = args.run_home or args.home
        provider_options = _provider_options(args, prefix="run_")
        system_prompt = (
            args.run_system_prompt if args.run_system_prompt is not None else args.system_prompt
        )
        if args.json_mode:
            return asyncio.run(
                run_json(
                    prompt,
                    home=home,
                    session_id=args.session_id,
                    title=args.title,
                    provider_options=provider_options,
                    system_prompt=system_prompt,
                )
            )
        return asyncio.run(
            run_print(
                prompt,
                home=home,
                session_id=args.session_id,
                title=args.title,
                provider_options=provider_options,
                system_prompt=system_prompt,
            )
        )

    if args.command == "config" and args.config_command == "check":
        return asyncio.run(
            check_provider_connection(
                home=args.check_home or args.home,
                provider_options=_provider_options(args, prefix="check_"),
                json_mode=args.check_json,
            )
        )

    if args.command == "provider":
        home = getattr(args, "provider_home", None) or args.home
        if args.provider_command == "list":
            return list_provider_configs(home=home, json_mode=args.json)
        if args.provider_command == "show":
            return show_provider_config(
                args.provider_id,
                home=home,
                json_mode=args.json,
            )
        if args.provider_command == "set":
            return set_provider_config(
                args.provider_id,
                home=home,
                provider_type=args.kind,
                model=args.model,
                base_url=args.base_url,
                api_key=args.api_key,
                api_key_env=args.api_key_env,
                timeout=args.timeout,
                enabled=args.enabled,
                make_default=args.default,
                json_mode=args.json,
            )
        if args.provider_command == "use":
            return use_provider_config(
                args.provider_id,
                home=home,
                json_mode=args.json,
            )
        if args.provider_command == "remove":
            return remove_provider_config(
                args.provider_id,
                home=home,
                json_mode=args.json,
            )
        if args.provider_command == "check":
            return asyncio.run(
                check_provider_connection(
                    home=home,
                    provider_options=_provider_options(args, prefix="provider_check_"),
                    provider_id=args.provider_id,
                    json_mode=args.json,
                )
            )

    return asyncio.run(
        run_interactive(
            home=args.home,
            provider_options=_provider_options(args),
            system_prompt=args.system_prompt,
        )
    )


def _add_provider_args(parser: argparse.ArgumentParser, *, prefix: str = "") -> None:
    parser.add_argument(
        "--provider",
        dest=f"{prefix}provider",
        choices=["faux", "openai-compatible"],
        default=None,
        help="Provider kind",
    )
    parser.add_argument("--provider-id", dest=f"{prefix}provider_id", help="Provider/supplier id")
    parser.add_argument("--model", dest=f"{prefix}model", help="Model id")
    parser.add_argument("--base-url", dest=f"{prefix}base_url", help="Provider base URL")
    parser.add_argument("--api-key", dest=f"{prefix}api_key", help="Inline API key")
    parser.add_argument("--api-key-env", dest=f"{prefix}api_key_env", help="API key env var")
    parser.add_argument(
        "--timeout",
        dest=f"{prefix}timeout",
        type=float,
        default=None,
        help="Provider timeout seconds",
    )


def _provider_options(args: argparse.Namespace, *, prefix: str = "") -> RuntimeProviderOptions:
    provider = getattr(args, f"{prefix}provider", None) or getattr(args, "provider", None)
    return RuntimeProviderOptions(
        provider=provider,
        provider_id=getattr(args, f"{prefix}provider_id", None)
        or getattr(args, "provider_id", None),
        model=getattr(args, f"{prefix}model", None) or getattr(args, "model", None),
        base_url=getattr(args, f"{prefix}base_url", None) or getattr(args, "base_url", None),
        api_key=getattr(args, f"{prefix}api_key", None) or getattr(args, "api_key", None),
        api_key_env=getattr(args, f"{prefix}api_key_env", None)
        or getattr(args, "api_key_env", None),
        timeout=getattr(args, f"{prefix}timeout", None) or getattr(args, "timeout", None),
    )


if __name__ == "__main__":
    raise SystemExit(main())
