from __future__ import annotations

import json
import sys
from pathlib import Path

from ouro.cli.errors import error_exit_code
from ouro.cli.run import build_default_runtime
from ouro.core.errors import OuroError
from ouro.core.results import RunResult
from ouro.local_application import LocalApplication


async def approve_tool_command(
    session_id: str,
    *,
    home: str | Path | None = None,
    run_id: str | None = None,
    arguments_override: dict[str, object] | None = None,
    json_mode: bool = False,
) -> int:
    try:
        runtime = build_default_runtime(home)
        async with LocalApplication.from_runtime(runtime, home=home):
            result = await runtime.approve_tool(
                session_id=session_id,
                run_id=run_id,
                approved=True,
                arguments_override=arguments_override,
            )
    except (OuroError, ValueError, RuntimeError) as exc:
        ouro_exc = exc if isinstance(exc, OuroError) else OuroError(str(exc))
        _print_error(ouro_exc, json_mode=json_mode)
        return error_exit_code(ouro_exc)

    return _print_run_result(result, json_mode=json_mode)


async def reject_tool_command(
    session_id: str,
    *,
    home: str | Path | None = None,
    run_id: str | None = None,
    reason: str = "User rejected",
    json_mode: bool = False,
) -> int:
    try:
        runtime = build_default_runtime(home)
        async with runtime:
            result = await runtime.approve_tool(
                session_id=session_id,
                run_id=run_id,
                approved=False,
                reason=reason,
            )
    except (OuroError, ValueError, RuntimeError) as exc:
        ouro_exc = exc if isinstance(exc, OuroError) else OuroError(str(exc))
        _print_error(ouro_exc, json_mode=json_mode)
        return error_exit_code(ouro_exc)

    return _print_run_result(result, json_mode=json_mode)


async def abort_run_command(
    session_id: str,
    *,
    home: str | Path | None = None,
    run_id: str | None = None,
    reason: str = "User aborted",
    json_mode: bool = False,
) -> int:
    try:
        runtime = build_default_runtime(home)
        async with runtime:
            result = await runtime.abort_run(
                session_id=session_id,
                run_id=run_id,
                reason=reason,
            )
    except (OuroError, ValueError, RuntimeError) as exc:
        ouro_exc = exc if isinstance(exc, OuroError) else OuroError(str(exc))
        _print_error(ouro_exc, json_mode=json_mode)
        return error_exit_code(ouro_exc)

    return _print_run_result(result, json_mode=json_mode)


def _print_run_result(result: RunResult, *, json_mode: bool) -> int:
    if json_mode:
        print(
            json.dumps(
                {
                    "status": result.run.status,
                    "session_id": result.session.session_id,
                    "run_id": result.run.run_id,
                    "final_text": result.final_text,
                    "error": result.run.error,
                    "usage": result.run.usage,
                    "events": [event.model_dump(mode="json") for event in result.events],
                },
                ensure_ascii=False,
            )
        )
    else:
        if result.run.status == "aborted":
            print(f"Run aborted: {result.run.error or 'User aborted'}")
        elif result.run.status == "completed":
            if result.final_text:
                print(result.final_text)
        elif result.run.error:
            print(f"Run {result.run.status}: {result.run.error}", file=sys.stderr)
        else:
            print(f"Run status: {result.run.status}")
    return 0 if result.run.status in {"completed", "aborted"} else 1


def _print_error(exc: Exception, *, json_mode: bool) -> None:
    message = str(exc)
    if json_mode:
        print(
            json.dumps(
                {"status": "failed", "error": message, "error_type": type(exc).__name__},
                ensure_ascii=False,
            )
        )
    else:
        print(f"Error: {message}", file=sys.stderr)
