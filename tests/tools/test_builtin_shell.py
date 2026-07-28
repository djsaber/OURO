from pathlib import Path

import pytest

from ouro.tools import (
    ToolExecutionContext,
    ToolExecutor,
    ToolInvocation,
    ToolProcessResult,
    ToolRegistry,
    builtin_tools,
)
from ouro.tools.builtin_shell import (
    CommandShell,
    builtin_command_tool,
    detect_command_shell,
)


def _resolver(commands: dict[str, str]):
    return commands.get


def _invocation(shell: CommandShell, command: str) -> ToolInvocation:
    return ToolInvocation(
        session_id="ses_1",
        run_id="run_1",
        tool_name=shell.tool_name,
        arguments={"command": command},
    )


def test_windows_prefers_pwsh_then_windows_powershell_then_cmd() -> None:
    pwsh = detect_command_shell(
        platform="win32",
        resolve_executable=_resolver(
            {
                "pwsh": "C:/PowerShell/7/pwsh.exe",
                "powershell": "C:/Windows/powershell.exe",
                "cmd": "C:/Windows/cmd.exe",
            }
        ),
        environment={},
    )
    windows_powershell = detect_command_shell(
        platform="win32",
        resolve_executable=_resolver(
            {
                "powershell": "C:/Windows/powershell.exe",
                "cmd": "C:/Windows/cmd.exe",
            }
        ),
        environment={},
    )
    command_prompt = detect_command_shell(
        platform="win32",
        resolve_executable=_resolver({"cmd": "C:/Windows/cmd.exe"}),
        environment={},
    )

    assert pwsh is not None
    assert (pwsh.kind, pwsh.tool_name) == ("powershell", "PowerShell")
    assert windows_powershell is not None
    assert (windows_powershell.kind, windows_powershell.tool_name) == (
        "windows_powershell",
        "PowerShell",
    )
    assert command_prompt is not None
    assert (command_prompt.kind, command_prompt.tool_name) == ("cmd", "CommandPrompt")
    assert command_prompt.executable == "C:/Windows/cmd.exe"


def test_posix_prefers_bash_then_portable_sh() -> None:
    bash = detect_command_shell(
        platform="linux",
        resolve_executable=_resolver({"bash": "/bin/bash", "sh": "/bin/sh"}),
    )
    posix_shell = detect_command_shell(
        platform="darwin",
        resolve_executable=_resolver({"sh": "/bin/sh"}),
    )

    assert bash is not None
    assert (bash.kind, bash.tool_name) == ("bash", "Bash")
    assert posix_shell is not None
    assert (posix_shell.kind, posix_shell.tool_name) == ("sh", "Sh")


def test_no_command_tool_is_selected_when_no_supported_shell_exists() -> None:
    assert (
        detect_command_shell(
            platform="linux",
            resolve_executable=_resolver({}),
        )
        is None
    )


def test_builtin_tool_list_exposes_exactly_the_detected_shell() -> None:
    shell = detect_command_shell()
    command_definitions = [
        tool.definition for tool in builtin_tools() if "shell_kind" in tool.definition.metadata
    ]

    if shell is None:
        assert command_definitions == []
        return
    assert len(command_definitions) == 1
    assert command_definitions[0].name == shell.tool_name
    assert command_definitions[0].display_name == shell.display_name
    assert command_definitions[0].metadata["shell_executable"] == shell.executable


@pytest.mark.parametrize(
    ("shell", "required_guidance"),
    [
        (
            CommandShell("bash", "/bin/bash", "Bash", "Bash"),
            ("Bash syntax only", "pipefail", "command -v", "Unmatched globs"),
        ),
        (
            CommandShell("powershell", "C:/pwsh.exe", "PowerShell", "PowerShell 7"),
            (
                "PowerShell syntax only",
                "full cmdlet names",
                "pipelines carry objects",
                "-LiteralPath",
                "$LASTEXITCODE",
                "Get-Command",
            ),
        ),
        (
            CommandShell("sh", "/bin/sh", "Sh", "POSIX sh"),
            ("portable sh syntax", "[[ ... ]]", "command -v", "Pipeline status"),
        ),
        (
            CommandShell(
                "cmd",
                "C:/Windows/cmd.exe",
                "CommandPrompt",
                "Windows Command Prompt",
            ),
            ("cmd syntax only", "metacharacters", "%NAME%", "where.exe"),
        ),
    ],
)
def test_shell_contract_exposes_dialect_specific_pitfalls(
    shell: CommandShell,
    required_guidance: tuple[str, ...],
) -> None:
    definition = builtin_command_tool(shell).definition

    assert definition.name == shell.tool_name
    assert definition.display_name == shell.display_name
    assert definition.metadata["shell_kind"] == shell.kind
    assert definition.metadata["shell_executable"] == shell.executable
    assert definition.description.startswith("Use this tool to run a non-interactive")
    assert "Do not call interactive programs" in definition.description
    assert "Prefer dedicated" in definition.description
    assert all(text in definition.description for text in required_guidance)
    assert all(
        "description" in schema for schema in definition.parameters_schema["properties"].values()
    )


def test_powershell_command_is_bound_with_encoded_clean_session() -> None:
    shell = CommandShell(
        "windows_powershell",
        "C:/Windows/powershell.exe",
        "PowerShell",
        "Windows PowerShell",
    )

    prepared = shell.prepare_command("Write-Output 'hello' ")

    arguments = prepared.split()
    encoded = arguments[-1]
    import base64

    script = base64.b64decode(encoded).decode("utf-16-le")
    assert prepared.startswith("C:/Windows/powershell.exe -NoLogo -NoProfile -NonInteractive")
    assert "$ErrorActionPreference = 'Stop'" in script
    assert "$ProgressPreference = 'SilentlyContinue'" in script
    assert "[Console]::OutputEncoding" in script
    encoded_user_command = base64.b64encode(b"Write-Output 'hello' ").decode("ascii")
    assert encoded_user_command in script
    assert "[ScriptBlock]::Create" in script
    assert "[Console]::Error.WriteLine" in script
    assert "if (-not $?) { exit 1 }" in script


def test_bash_and_sh_commands_bind_the_selected_executable() -> None:
    bash = CommandShell("bash", "/opt/bin/bash", "Bash", "Bash")
    posix_shell = CommandShell("sh", "/opt/bin/sh", "Sh", "POSIX sh")

    bash_command = bash.prepare_command("printf '%s\\n' hello | grep hello")
    sh_command = posix_shell.prepare_command("printf '%s\\n' hello")

    assert bash_command.startswith("BASH_ENV= ENV= /opt/bin/bash")
    assert "--noprofile --norc -o pipefail -c" in bash_command
    assert sh_command.startswith("ENV= /opt/bin/sh -c")


@pytest.mark.asyncio
async def test_command_result_identifies_shell_and_preserves_both_output_streams() -> None:
    shell = CommandShell("bash", "/opt/bin/bash", "Bash", "Bash")
    seen_commands: list[str] = []

    class RecordingContext(ToolExecutionContext):
        async def run_shell(
            self,
            command: str,
            *,
            cwd: str | Path | None = None,
            timeout_seconds: float = 30.0,
        ) -> ToolProcessResult:
            del cwd, timeout_seconds
            seen_commands.append(command)
            return ToolProcessResult(
                stdout="standard output",
                stderr="diagnostic output",
                returncode=7,
            )

    registry = ToolRegistry()
    registry.register(builtin_command_tool(shell))
    outcome = await ToolExecutor(registry).execute(
        _invocation(shell, "run something"),
        approved=True,
        context=RecordingContext(),
    )

    assert outcome.result is not None
    assert outcome.result.is_error is True
    assert outcome.result.content == ("stdout:\nstandard output\nstderr:\ndiagnostic output")
    assert outcome.result.structured["shell"] == "bash"
    assert outcome.result.structured["executable"] == "/opt/bin/bash"
    assert outcome.result.structured["returncode"] == 7
    assert seen_commands == [shell.prepare_command("run something")]


@pytest.mark.asyncio
async def test_detected_shell_executes_its_own_syntax(tmp_path) -> None:
    shell = detect_command_shell()
    if shell is None:
        pytest.skip("no supported command shell is installed")
    command = {
        "bash": "printf '%s\\n' shell-ok",
        "sh": "printf '%s\\n' shell-ok",
        "powershell": "Write-Output 'shell-ok'",
        "windows_powershell": "Write-Output 'shell-ok'",
        "cmd": "echo shell-ok",
    }[shell.kind]
    registry = ToolRegistry()
    registry.register(builtin_command_tool(shell))

    outcome = await ToolExecutor(registry).execute(
        _invocation(shell, command),
        approved=True,
        context=ToolExecutionContext(workspace=tmp_path, workspace_only=True),
    )

    assert outcome.result is not None
    assert outcome.result.is_error is False
    assert outcome.result.content.strip() == "shell-ok"
    assert outcome.result.structured["shell"] == shell.kind
    assert Path(outcome.result.structured["executable"]) == Path(shell.executable)


@pytest.mark.asyncio
async def test_detected_shell_surfaces_command_failure(tmp_path) -> None:
    shell = detect_command_shell()
    if shell is None:
        pytest.skip("no supported command shell is installed")
    command = {
        "bash": "printf '%s\\n' shell-failed >&2; exit 7",
        "sh": "printf '%s\\n' shell-failed >&2; exit 7",
        "powershell": "Write-Error 'shell-failed'",
        "windows_powershell": "Write-Error 'shell-failed'",
        "cmd": "echo shell-failed 1>&2 & exit /b 7",
    }[shell.kind]
    registry = ToolRegistry()
    registry.register(builtin_command_tool(shell))

    outcome = await ToolExecutor(registry).execute(
        _invocation(shell, command),
        approved=True,
        context=ToolExecutionContext(workspace=tmp_path, workspace_only=True),
    )

    assert outcome.result is not None
    assert outcome.result.is_error is True
    assert outcome.result.exit_code not in (0, None)
    assert "shell-failed" in outcome.result.content
    assert "CLIXML" not in outcome.result.content
