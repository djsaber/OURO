from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence

from ouro.cli.config_commands import (
    check_provider_connection,
    clear_system_prompt_config,
    list_provider_configs,
    remove_provider_config,
    set_provider_config,
    set_system_prompt_config,
    show_provider_config,
    show_system_prompt_config,
    use_provider_config,
)
from ouro.cli.interactive import run_interactive
from ouro.cli.parser import build_parser
from ouro.cli.run import (
    RuntimeProviderOptions,
    run_json,
    run_print,
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
    if args.command == "config":
        return _config_command(args)
    if args.command == "provider":
        return _provider_command(args)
    if args.command == "skills":
        return _skills_command(args)
    return asyncio.run(
        run_interactive(
            home=args.home,
            provider_options=_provider_options(args),
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
        )
    )


def _config_command(args: argparse.Namespace) -> int:
    if args.config_command == "check":
        return asyncio.run(
            check_provider_connection(
                home=args.check_home or args.home,
                provider_options=_provider_options(args, prefix="check_"),
                json_mode=args.check_json,
            )
        )

    home = args.prompt_home or args.home
    if args.prompt_value is not None:
        return set_system_prompt_config(
            args.prompt_value,
            home=home,
            json_mode=args.prompt_json,
        )
    if args.prompt_clear:
        return clear_system_prompt_config(home=home, json_mode=args.prompt_json)
    return show_system_prompt_config(home=home, json_mode=args.prompt_json)


def _provider_command(args: argparse.Namespace) -> int:
    home = args.provider_home or args.home
    if args.provider_command == "list":
        return list_provider_configs(home=home, json_mode=args.json)
    if args.provider_command == "show":
        return show_provider_config(args.provider_id, home=home, json_mode=args.json)
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
        return use_provider_config(args.provider_id, home=home, json_mode=args.json)
    if args.provider_command == "remove":
        return remove_provider_config(args.provider_id, home=home, json_mode=args.json)
    if args.provider_command == "check":
        return asyncio.run(
            check_provider_connection(
                home=home,
                provider_options=_provider_options(args, prefix="provider_check_"),
                provider_id=args.provider_id,
                json_mode=args.json,
            )
        )
    raise AssertionError(f"unknown provider command: {args.provider_command}")


def _skills_command(args: argparse.Namespace) -> int:
    home = args.skills_home or args.home
    if args.skills_command == "list":
        return list_skills(
            home=home,
            json_mode=args.json,
            include_disabled=args.skills_all,
        )
    if args.skills_command == "show":
        return show_skill(args.skill_id, home=home, json_mode=args.json)
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
        return update_skill(args.skill_id, home=home, json_mode=args.json)
    if args.skills_command in {"remove", "uninstall"}:
        return remove_skill(
            args.skill_id,
            home=home,
            force=args.force,
            json_mode=args.json,
        )
    if args.skills_command in {"enable", "disable"}:
        return set_skill_enabled(
            args.skill_id,
            enabled=args.skills_command == "enable",
            home=home,
            json_mode=args.json,
        )
    raise AssertionError(f"unknown skills command: {args.skills_command}")


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
