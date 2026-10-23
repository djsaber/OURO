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

from pydantic import Field

from ouro.core.base import utc_now
from ouro.tools.base import (
    ProcessResult,
    Tool,
    ToolDefinition,
    ToolExecutionContext,
    ToolInvocation,
    ToolResult,
)
from ouro.tools.builtin.common import BuiltinArguments

ShellKind = Literal["bash", "powershell", "windows_powershell", "cmd", "sh"]
ExecutableResolver = Callable[[str], str | None]


@dataclass(frozen=True)
class CommandShell:
    """One detected shell contract shared by model description and execution."""

    kind: ShellKind
    executable: str
    tool_name: str
    display_name: str

    def prepare_arguments(self, command: str) -> tuple[str, ...]:
        """Bind command text to the selected executable as a direct argv contract."""

        if self.kind in ("powershell", "windows_powershell"):
            return _powershell_arguments(self.executable, command)
        if self.kind == "bash":
            return (
                self.executable,
                "--noprofile",
                "--norc",
                "-o",
                "pipefail",
                "-c",
                command,
            )
        if self.kind == "sh":
            return (self.executable, "-c", command)
        return (self.executable, "/D", "/S", "/C", f"chcp 65001 >NUL & {command}")

    def execution_environment(self) -> Mapping[str, str]:
        environment = dict(os.environ)
        if self.kind in {"bash", "sh"}:
            environment.pop("BASH_ENV", None)
            environment.pop("ENV", None)
        return environment

    def prepare_command(self, command: str) -> str:
        arguments = self.prepare_arguments(command)
        if self.kind in ("powershell", "windows_powershell", "cmd"):
            return subprocess.list2cmdline(arguments)
        if self.kind == "bash":
            return f"BASH_ENV= ENV= {shlex.join(arguments)}"
        return f"ENV= {shlex.join(arguments)}"


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


class _ShellArguments(BuiltinArguments):
    command: str = Field(min_length=1, description="Non-interactive command in the selected shell.")
    cwd: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Existing working directory, absolute or workspace-relative. "
            "Defaults to the workspace, or the current directory if none is set; "
            "applies only to this call."
        ),
    )
    timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        description=(
            "Time limit in seconds. Increase only for expected long-running "
            "commands, not interactive or hung programs."
        ),
    )


def builtin_command_tool(shell: CommandShell) -> Tool:
    parameters = _ShellArguments.model_json_schema()
    parameters["properties"]["command"]["description"] = _command_description(shell)

    async def execute(invocation: ToolInvocation, context: ToolExecutionContext) -> ToolResult:
        return await _execute_command(shell, invocation, context=context)

    return Tool(
        definition=ToolDefinition(
            name=shell.tool_name,
            display_name=shell.display_name,
            description=_shell_description(shell),
            parameters_schema=parameters,
            permission_mode="approval",
            effect="state_changing",
            # The executable path is a local binding captured by the handler,
            # not part of the portable tool contract used by delegated Runs.
            metadata={"shell_kind": shell.kind},
        ),
        handler=execute,
    )


async def _execute_command(
    shell: CommandShell,
    invocation: ToolInvocation,
    *,
    context: ToolExecutionContext,
) -> ToolResult:
    arguments = _ShellArguments.model_validate(invocation.arguments)
    cwd = context.resolve_working_directory(arguments.cwd)
    process_result = await context.run_process(
        shell.prepare_arguments(arguments.command),
        cwd=cwd,
        timeout_seconds=arguments.timeout_seconds,
        environment=shell.execution_environment(),
    )

    stdout = process_result.stdout
    stderr = process_result.stderr
    if shell.kind in ("powershell", "windows_powershell"):
        stdout = _decode_powershell_clixml(stdout)
        stderr = _decode_powershell_clixml(stderr)
    return ToolResult(
        tool_call_id=invocation.tool_call_id,
        tool_name=invocation.tool_name,
        is_error=process_result.timed_out or (process_result.returncode not in (0, None)),
        content=_process_output(process_result, stdout=stdout, stderr=stderr),
        structured={
            "shell": shell.kind,
            "executable": shell.executable,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": process_result.stdout_truncated,
            "stderr_truncated": process_result.stderr_truncated,
            "returncode": process_result.returncode,
            "timed_out": process_result.timed_out,
        },
        exit_code=process_result.returncode,
        ended_at=utc_now(),
    )


def _shell_description(shell: CommandShell) -> str:
    if shell.kind in ("powershell", "windows_powershell"):
        edition = "PowerShell 7+" if shell.kind == "powershell" else "Windows PowerShell"
        return (
            f"Run non-interactive {edition} commands for tests, builds, version control, "
            "or system inspection. Prefer Read/Write/Edit/Glob/Grep for file work. Each call "
            "starts with -NoProfile; variables and directory changes do not persist. "
            "Use PowerShell syntax only "
            "and full cmdlet names. Pipelines carry objects: use Where-Object/Select-Object; "
            "format last. Cmdlet errors terminate the call. Quote paths; prefer -LiteralPath "
            "for literal wildcards. Verify native programs with Get-Command; check "
            "$LASTEXITCODE before later statements can hide failures."
        )
    if shell.kind == "bash":
        return (
            "Run non-interactive Bash commands for tests, builds, version control, or system "
            "inspection. Prefer Read/Write/Edit/Glob/Grep for file work. Each call starts "
            "fresh; variables and directory changes do not persist. Use Bash syntax only. "
            "Profiles are disabled; "
            "pipefail is enabled. Quote paths and variable expansions; use -- before operands "
            "that may start with '-'. Verify optional programs with command -v. Unmatched "
            "globs stay literal. Use && for dependent steps; semicolons continue after failure."
        )
    if shell.kind == "sh":
        return (
            "Run non-interactive POSIX sh commands for tests, builds, version control, or "
            "system inspection. Prefer Read/Write/Edit/Glob/Grep for file work. Each call "
            "starts fresh; variables and directory changes do not persist. Use portable sh "
            "syntax: no Bash arrays, "
            "[[ ... ]], brace expansion, or process substitution. Quote paths and variable "
            "expansions; use -- before operands that may start with '-'. Verify optional "
            "programs with command -v. Pipeline status is the last command's status; check "
            "critical stages explicitly."
        )
    return (
        "Run non-interactive Windows cmd.exe commands for tests, builds, version control, "
        "or system inspection. Prefer Read/Write/Edit/Glob/Grep for file work. Each call "
        "starts fresh; variables and directory changes do not persist. Use cmd syntax only. "
        "Quote paths with spaces; "
        "escape metacharacters &, |, (, ), <, >, ^ when literal. Use %NAME% for environment "
        "variables and && for dependent steps. Verify optional programs with where.exe."
    )


def _command_description(shell: CommandShell) -> str:
    if shell.kind in ("powershell", "windows_powershell"):
        return "PowerShell source code to execute."
    if shell.kind == "bash":
        return "Bash source code to execute."
    if shell.kind == "sh":
        return "Portable POSIX sh source code to execute."
    return "Windows cmd.exe command text to execute."


def _powershell_arguments(executable: str, command: str) -> tuple[str, ...]:
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
    return (
        executable,
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-OutputFormat",
        "Text",
        "-EncodedCommand",
        encoded,
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


def _process_output(result: ProcessResult, *, stdout: str, stderr: str) -> str:
    if result.timed_out:
        status = "Command timed out."
    else:
        status = ""
    if stdout and stderr:
        output = f"stdout:\n{stdout}\nstderr:\n{stderr}"
    else:
        output = stdout or stderr
    if status and output:
        return f"{status}\n{output}"
    return status or output
