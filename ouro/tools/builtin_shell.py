from __future__ import annotations

import base64
import os
import shlex
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ouro.core.base import utc_now
from ouro.tools.base import (
    Tool,
    ToolDefinition,
    ToolExecutionContext,
    ToolInvocation,
    ToolProcessResult,
    ToolResult,
)

ShellKind = Literal["bash", "powershell", "windows_powershell", "cmd", "sh"]
ExecutableResolver = Callable[[str], str | None]


@dataclass(frozen=True)
class CommandShell:
    """One detected shell contract shared by model description and execution."""

    kind: ShellKind
    executable: str
    tool_name: str
    display_name: str

    def prepare_command(self, command: str) -> str:
        if self.kind in ("powershell", "windows_powershell"):
            return _powershell_command_line(self.executable, command)
        if self.kind == "bash":
            arguments = [
                self.executable,
                "--noprofile",
                "--norc",
                "-o",
                "pipefail",
                "-c",
                command,
            ]
            return f"BASH_ENV= ENV= {shlex.join(arguments)}"
        if self.kind == "sh":
            return f"ENV= {shlex.join([self.executable, '-c', command])}"
        return f"chcp 65001 >NUL & {command}"


def detect_command_shell(
    *,
    platform: str | None = None,
    resolve_executable: ExecutableResolver | None = None,
    environment: Mapping[str, str] | None = None,
) -> CommandShell | None:
    """Select the most reliable LLM-facing shell available on the host."""

    active_platform = platform or sys.platform
    resolver = resolve_executable or shutil.which
    active_environment = os.environ if environment is None else environment
    if active_platform == "win32":
        pwsh = resolver("pwsh")
        if pwsh:
            return CommandShell(
                kind="powershell",
                executable=pwsh,
                tool_name="PowerShell",
                display_name="PowerShell 7",
            )
        windows_powershell = resolver("powershell")
        if windows_powershell:
            return CommandShell(
                kind="windows_powershell",
                executable=windows_powershell,
                tool_name="PowerShell",
                display_name="Windows PowerShell",
            )
        command_prompt = _windows_command_prompt(resolver, active_environment)
        if command_prompt:
            return CommandShell(
                kind="cmd",
                executable=command_prompt,
                tool_name="CommandPrompt",
                display_name="Windows Command Prompt",
            )
        return None

    bash = resolver("bash")
    if bash:
        return CommandShell(
            kind="bash",
            executable=bash,
            tool_name="Bash",
            display_name="Bash",
        )
    posix_shell = resolver("sh")
    if posix_shell:
        return CommandShell(
            kind="sh",
            executable=posix_shell,
            tool_name="Sh",
            display_name="POSIX sh",
        )
    return None


def builtin_command_tool(shell: CommandShell) -> Tool:
    async def execute(
        invocation: ToolInvocation,
        *,
        context: ToolExecutionContext,
    ) -> ToolResult:
        return await _execute_command(shell, invocation, context=context)

    return Tool(
        definition=ToolDefinition(
            name=shell.tool_name,
            display_name=shell.display_name,
            description=_shell_description(shell),
            parameters_schema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "minLength": 1,
                        "description": _command_description(shell),
                    },
                    "cwd": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            "Existing directory in which to run this command, as an absolute "
                            "path or a path relative to the workspace. Omit to use the current "
                            "workspace. The choice applies only to this call."
                        ),
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "default": 30,
                        "description": (
                            "Maximum execution time in seconds. Omit for 30. Increase it only "
                            "when the command is expected to run longer; do not use a longer "
                            "timeout to accommodate an interactive or hung command."
                        ),
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
            permission_mode="approval",
            effect="state_changing",
            metadata={
                "shell_kind": shell.kind,
                "shell_executable": shell.executable,
            },
        ),
        handler=execute,
    )


async def _execute_command(
    shell: CommandShell,
    invocation: ToolInvocation,
    *,
    context: ToolExecutionContext,
) -> ToolResult:
    command = _text_arg(invocation, "command")
    cwd_arg = invocation.arguments.get("cwd")
    requested_cwd = str(cwd_arg) if isinstance(cwd_arg, str) and cwd_arg else None
    timeout_raw = invocation.arguments.get("timeout_seconds", 30)
    timeout_seconds = float(timeout_raw) if isinstance(timeout_raw, int | float) else 30.0
    cwd = context.resolve_working_directory(requested_cwd)
    process_result = await context.run_shell(
        shell.prepare_command(command),
        cwd=cwd,
        timeout_seconds=timeout_seconds,
    )

    stdout = process_result.stdout
    stderr = process_result.stderr
    if shell.kind in ("powershell", "windows_powershell"):
        stdout = _decode_powershell_clixml(stdout)
        stderr = _decode_powershell_clixml(stderr)
    stdout_text, stdout_truncated = context.limit_output(stdout)
    stderr_text, stderr_truncated = context.limit_output(stderr)
    return ToolResult(
        tool_call_id=invocation.tool_call_id,
        tool_name=invocation.tool_name,
        is_error=process_result.aborted
        or process_result.timed_out
        or (process_result.returncode not in (0, None)),
        content=_process_output(process_result, stdout=stdout_text, stderr=stderr_text),
        structured={
            "shell": shell.kind,
            "executable": shell.executable,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "returncode": process_result.returncode,
            "timed_out": process_result.timed_out,
            "aborted": process_result.aborted,
        },
        exit_code=process_result.returncode,
        ended_at=utc_now(),
    )


def _shell_description(shell: CommandShell) -> str:
    executable = Path(shell.executable).name
    if shell.kind in ("powershell", "windows_powershell"):
        edition = "PowerShell 7+" if shell.kind == "powershell" else "Windows PowerShell"
        return (
            f"Use this tool to run a non-interactive {edition} command with {executable} when "
            "you need shell-only operations such as tests, builds, version control, or system "
            "inspection. Prefer dedicated Read, Write, Edit, Glob, and Grep tools for simple "
            "file work. Do not call interactive programs or rely on state from an earlier "
            "call. Use PowerShell syntax only, never Bash or cmd syntax. Each call starts a "
            "clean -NoProfile "
            "session. Use full cmdlet names instead of aliases; PowerShell pipelines carry "
            "objects, so filter and select with Where-Object/Select-Object and format only at "
            "the end. Cmdlet errors terminate the call. Quote paths and prefer -LiteralPath "
            "when wildcard characters may be literal. Do not assume Unix utilities exist: "
            "verify native programs with Get-Command. Native programs report failure through "
            "$LASTEXITCODE; check it when later PowerShell statements could hide that status."
        )
    if shell.kind == "bash":
        return (
            f"Use this tool to run a non-interactive Bash command with {executable} when you "
            "need shell-only operations such as tests, builds, version control, or system "
            "inspection. Prefer dedicated Read, Write, Edit, Glob, and Grep tools for simple "
            "file work. Do not call interactive programs or rely on state from an earlier "
            "call. Use Bash syntax only, never PowerShell or cmd syntax. Profiles are disabled "
            "and pipefail is enabled. "
            "Quote paths and variable expansions, use -- before operands that can begin with "
            "'-', and use command -v before relying on optional programs. Unmatched globs stay "
            "literal by default. A semicolon continues after failure; use && when a later step "
            "requires an earlier one to succeed. Each call starts a fresh shell, so directory "
            "and variable changes do not persist."
        )
    if shell.kind == "sh":
        return (
            f"Use this tool to run a non-interactive POSIX sh command with {executable} when "
            "you need shell-only operations such as tests, builds, version control, or system "
            "inspection. Prefer dedicated file and search tools for simple file work. Do not "
            "call interactive programs or rely on state from an earlier call. Use portable sh "
            "syntax only: Bash-only arrays, [[ ... ]], brace expansion, and process "
            "substitution are invalid. Quote paths and variable expansions, use -- before "
            "operands that can begin with '-', and verify optional programs with command -v. "
            "Pipeline status is "
            "normally the last command's status, so check critical stages explicitly. Each "
            "call starts a fresh shell."
        )
    return (
        f"Use this tool to run a non-interactive Windows cmd.exe command with {executable} "
        "when you need shell-only operations such as tests, builds, version control, or system "
        "inspection. Prefer dedicated file and search tools for simple file work. Do not call "
        "interactive programs or rely on state from an earlier call. Use cmd syntax only, "
        "never Bash or PowerShell syntax. Quote paths containing spaces; escape cmd "
        "metacharacters such as &, |, (, ), <, >, and ^ when they are data. Use %NAME% for "
        "environment variables, && for dependent steps, and where.exe before relying on "
        "optional programs. Each call starts a fresh process, so cd and set changes do not "
        "persist."
    )


def _command_description(shell: CommandShell) -> str:
    if shell.kind in ("powershell", "windows_powershell"):
        return (
            "Complete PowerShell source text for this call. Use PowerShell cmdlets and syntax; "
            "Bash and cmd.exe constructs are invalid."
        )
    if shell.kind == "bash":
        return (
            "Complete Bash source text for this call. Profiles are disabled and pipefail is "
            "enabled; PowerShell and cmd.exe constructs are invalid."
        )
    if shell.kind == "sh":
        return "Complete portable POSIX sh source text; do not use Bash-specific features."
    return "Complete Windows cmd.exe command text; Bash and PowerShell syntax are invalid."


def _powershell_command_line(executable: str, command: str) -> str:
    encoded_command = base64.b64encode(command.encode("utf-8")).decode("ascii")
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        "$ProgressPreference = 'SilentlyContinue'\n"
        "$__ouroUtf8 = [System.Text.UTF8Encoding]::new($false)\n"
        "[Console]::OutputEncoding = $__ouroUtf8\n"
        "[Console]::InputEncoding = $__ouroUtf8\n"
        "$OutputEncoding = $__ouroUtf8\n"
        "$__ouroCommandText = [System.Text.Encoding]::UTF8.GetString("
        f"[System.Convert]::FromBase64String('{encoded_command}'))\n"
        "try {\n"
        "    $__ouroCommand = [ScriptBlock]::Create($__ouroCommandText)\n"
        "    & $__ouroCommand\n"
        "    if (-not $?) { exit 1 }\n"
        "} catch {\n"
        "    [Console]::Error.WriteLine(($_ | Out-String))\n"
        "    exit 1\n"
        "}\n"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return subprocess.list2cmdline(
        [
            executable,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-OutputFormat",
            "Text",
            "-EncodedCommand",
            encoded,
        ]
    )


def _decode_powershell_clixml(text: str) -> str:
    marker = "#< CLIXML"
    if not text.lstrip().startswith(marker):
        return text
    xml_start = text.find("<Objs")
    if xml_start < 0:
        return text
    try:
        root = ET.fromstring(text[xml_start:])
    except ET.ParseError:
        return text
    fragments = [
        _decode_clixml_string(element.text or "")
        for element in root.iter()
        if element.tag.rsplit("}", 1)[-1] == "S" and element.attrib.get("S") != "Progress"
    ]
    return "".join(fragments).strip()


def _decode_clixml_string(text: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(text):
        if text.startswith("_x", index) and index + 7 <= len(text) and text[index + 6] == "_":
            hexadecimal = text[index + 2 : index + 6]
            try:
                output.append(chr(int(hexadecimal, 16)))
            except ValueError:
                pass
            else:
                index += 7
                continue
        output.append(text[index])
        index += 1
    return "".join(output)


def _windows_command_prompt(
    resolver: ExecutableResolver,
    environment: Mapping[str, str],
) -> str | None:
    comspec = environment.get("COMSPEC")
    if comspec and Path(comspec).is_file():
        return comspec
    return resolver("cmd")


def _process_output(result: ToolProcessResult, *, stdout: str, stderr: str) -> str:
    if result.timed_out:
        status = "Command timed out."
    elif result.aborted:
        status = "Command aborted."
    else:
        status = ""
    if stdout and stderr:
        output = f"stdout:\n{stdout}\nstderr:\n{stderr}"
    else:
        output = stdout or stderr
    if status and output:
        return f"{status}\n{output}"
    return status or output


def _text_arg(invocation: ToolInvocation, name: str) -> str:
    value = invocation.arguments.get(name)
    if not isinstance(value, str):
        raise ValueError(f"tool argument '{name}' must be a string")
    return value
