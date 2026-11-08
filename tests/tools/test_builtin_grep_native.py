from __future__ import annotations

import shutil
from unittest.mock import AsyncMock, Mock

import pytest

from ouro.core.cancellation import AbortSignal
from ouro.core.tools import tool_definition_contract_hash
from ouro.tools import (
    ProcessResult,
    ToolExecutionAborted,
    ToolExecutionContext,
    ToolInvocation,
    builtin_tools,
)
from ouro.tools.builtin.search import registration, ripgrep
from ouro.tools.builtin.search.registration import (
    builtin_grep_tool,
    builtin_ripgrep_tool,
    detect_ripgrep,
)
from ouro.tools.schema import tool_argument_error


def _call(**arguments):
    return ToolInvocation(session_id="s", run_id="r", tool_name="Grep", arguments=arguments)


@pytest.mark.parametrize(
    "commands, expected",
    [
        ({"rg": "/bin/rg", "grep": "/bin/grep"}, "/bin/rg"),
        ({"grep": "/bin/grep"}, None),
        ({"rg": "C:/bin/rg.exe"}, "C:/bin/rg.exe"),
        ({}, None),
    ],
)
def test_detection_only_selects_ripgrep(commands, expected):
    assert detect_ripgrep(resolve_executable=commands.get) == expected


@pytest.mark.parametrize("executable", ["/native/rg", None])
@pytest.mark.parametrize("external", [False, True])
async def test_registry_binds_both_search_tools_with_one_detection(
    monkeypatch, tmp_path, executable, external
):
    detect = Mock(return_value=executable)
    monkeypatch.setattr(registration, "detect_ripgrep", detect)
    tools = {tool.definition.name: tool for tool in builtin_tools()}
    tool = tools["Grep"]
    assert tool.definition.effect == "read_only"
    assert tool.definition.permission_mode == "auto"
    assert tool.definition.metadata.get("grep_backend") == ("ripgrep" if executable else None)
    assert {"multiline", "type", "exclude_dirs"} <= tool.definition.parameters_schema[
        "properties"
    ].keys()
    (tmp_path / "one.txt").write_text("needle", encoding="utf-8")
    workspace = tmp_path / "workspace" if external else tmp_path
    workspace.mkdir(exist_ok=True)
    context = ToolExecutionContext(workspace=workspace)
    context.run_process = AsyncMock(return_value=ProcessResult("", "", 1))
    for name, pattern in (("Glob", "*.txt"), ("Grep", "needle")):
        result = await tools[name].execute(
            ToolInvocation(
                session_id="s",
                run_id="r",
                tool_name=name,
                arguments={"pattern": pattern, "path": ".." if external else "."},
            ),
            context=context,
        )
        assert not result.is_error
        if executable is None:
            assert result.content == "one.txt"
    if executable is None:
        context.run_process.assert_not_called()
    else:
        assert context.run_process.await_count == 2
        assert all(call.args[0][0] == executable for call in context.run_process.call_args_list)
    detect.assert_called_once_with()


def test_contract_hash_does_not_depend_on_executable_location():
    assert tool_definition_contract_hash(builtin_ripgrep_tool("/bin/rg").definition) == (
        tool_definition_contract_hash(builtin_ripgrep_tool("C:/bin/rg.exe").definition)
    )


@pytest.mark.parametrize("options", [{"-C": -1}, {"-i": 1}, {"timeout_seconds": 0}, {"args": []}])
async def test_invalid_arguments_never_start_processes(tmp_path, options):
    tool = builtin_ripgrep_tool("unused")
    invocation = _call(pattern="x", **options)
    context = ToolExecutionContext(workspace=tmp_path)
    context.run_process = AsyncMock()
    assert tool_argument_error(tool.definition, invocation.arguments) is not None
    with pytest.raises(ValueError):
        await tool.execute(invocation, context=context)
    context.run_process.assert_not_called()


async def test_process_arguments_are_literal_and_configuration_is_disabled(tmp_path):
    target = tmp_path / "- with spaces.txt"
    target.write_text("needle", encoding="utf-8")
    context = ToolExecutionContext(workspace=tmp_path)
    context.run_process = AsyncMock(return_value=ProcessResult("hit", "", 0))
    pattern = "--pre=program; $(command) | &"
    result = await builtin_ripgrep_tool("/native/rg").execute(
        _call(pattern=pattern, path=target.name, fixed_strings=True, **{"-i": True}),
        context=context,
    )
    assert not result.is_error
    call = context.run_process.call_args
    argv = call.args[0]
    assert argv[0] == "/native/rg"
    assert argv[-4:] == ["-e", pattern, "--", f"./{target.name}"]
    assert "-i" in argv and "--no-config" in argv
    assert call.kwargs["cwd"] == tmp_path


@pytest.mark.parametrize(
    "process, error, expected",
    [
        (ProcessResult("", "", 1), False, "No matches found."),
        (ProcessResult("partial", "invalid regex", 2), True, "invalid regex"),
        (ProcessResult("partial", "", -1, timed_out=True), True, "Search timed out."),
        (ProcessResult("partial", "", 0, stdout_truncated=True), False, "narrow the path"),
    ],
)
async def test_native_exit_and_truncation_semantics(tmp_path, process, error, expected):
    context = ToolExecutionContext(workspace=tmp_path)
    context.run_process = AsyncMock(return_value=process)
    result = await builtin_ripgrep_tool("unused").execute(_call(pattern="x"), context=context)
    assert result.is_error is error
    assert expected in result.content
    assert result.exit_code == process.returncode
    assert result.structured["stdout_truncated"] == process.stdout_truncated
    assert result.structured["timed_out"] == process.timed_out
    context.run_process.assert_awaited_once()


async def test_paths_and_sensitive_files_are_checked_before_execution(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env.local").write_text("secret", encoding="utf-8")
    context = ToolExecutionContext(workspace=workspace)
    context.run_process = AsyncMock()
    tool = builtin_ripgrep_tool("unused")
    result = await tool.execute(_call(pattern="x", path=".env.local"), context=context)
    assert result.is_error and "sensitive" in result.content
    context.run_process.assert_not_called()


async def test_aborted_search_does_not_spawn_a_process(tmp_path):
    signal = AbortSignal()
    signal.abort("cancel search")
    context = ToolExecutionContext(workspace=tmp_path, signal=signal)
    context.run_process = AsyncMock()
    with pytest.raises(ToolExecutionAborted, match="cancel search"):
        await builtin_grep_tool(registration.detect_ripgrep()).execute(
            _call(pattern="x"), context=context
        )
    context.run_process.assert_not_called()


@pytest.fixture
def native_tool():
    executable = shutil.which("rg")
    if executable is None:
        pytest.skip("ripgrep is not installed")
    return builtin_ripgrep_tool(executable)


def _write(root, path, content="needle\n"):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


async def test_real_search_preserves_exclusions_and_ignore_override(tmp_path, native_tool):
    for name in (
        "visible.txt",
        ".hidden.txt",
        "ignored.txt",
        "nested/.env.local",
        ".env",
        ".git/data",
    ):
        _write(tmp_path, name)
    _write(tmp_path, ".gitignore", "ignored.txt\n")
    context = ToolExecutionContext(workspace=tmp_path)
    result = await native_tool.execute(_call(pattern="needle"), context=context)
    assert not result.is_error
    assert "visible.txt" in result.content and ".hidden.txt" in result.content
    assert "ignored.txt" not in result.content
    assert ".env" not in result.content and "data" not in result.content
    result = await native_tool.execute(
        _call(pattern="needle", include_ignored=True, glob="*"), context=context
    )
    assert "ignored.txt" in result.content
    assert ".env" not in result.content and "data" not in result.content


async def test_real_search_content_counts_and_literal_patterns(tmp_path, native_tool):
    _write(tmp_path, "a space.txt", "before\nNEEDLE.*\nafter\nneedle\n")
    _write(tmp_path, "unmatched.txt", "absent\n")
    context = ToolExecutionContext(workspace=tmp_path)
    result = await native_tool.execute(
        _call(
            pattern="needle.*", fixed_strings=True, output_mode="content", **{"-C": 1, "-i": True}
        ),
        context=context,
    )
    assert not result.is_error
    assert "a space.txt:2:NEEDLE.*" in result.content
    assert "a space.txt-1-before" in result.content and "a space.txt-3-after" in result.content
    result = await native_tool.execute(
        _call(pattern="needle", output_mode="count_matches", **{"-i": True}),
        context=context,
    )
    assert "a space.txt:2" in result.content
    assert "unmatched.txt" not in result.content
    assert not (await native_tool.execute(_call(pattern="missing"), context=context)).is_error
    assert (await native_tool.execute(_call(pattern="["), context=context)).is_error


async def test_real_explicit_file_bypasses_filters_and_dash_is_a_filename(tmp_path, native_tool):
    _write(tmp_path, "-")
    result = await native_tool.execute(
        _call(pattern="needle", path="-", glob="*.py"),
        context=ToolExecutionContext(workspace=tmp_path),
    )
    assert not result.is_error
    assert "-" in result.content and "No matches" not in result.content


async def test_real_large_files_and_directory_symlinks_are_skipped(
    tmp_path, native_tool, monkeypatch
):
    monkeypatch.setattr(ripgrep, "_MAX_SEARCHABLE_FILE_BYTES", 32)
    _write(tmp_path, "large.txt", "needle\n" * 10)
    root = tmp_path / "root"
    root.mkdir()
    try:
        (root / "link").symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pass  # Windows without symlink privilege still exercises the size limit.
    _write(root, "large.txt", "needle\n" * 10)
    result = await native_tool.execute(
        _call(pattern="needle"), context=ToolExecutionContext(workspace=root)
    )
    assert not result.is_error
    assert result.content == "No matches found."
    result = await native_tool.execute(
        _call(pattern="needle", path="large.txt"),
        context=ToolExecutionContext(workspace=root),
    )
    assert not result.is_error
    assert result.content == "No matches found."


async def test_real_ripgrep_type_glob_multiline_and_config_isolation(tmp_path, monkeypatch):
    executable = shutil.which("rg")
    if executable is None:
        pytest.skip("ripgrep is not installed")
    tool = builtin_ripgrep_tool(executable)
    _write(tmp_path, "src/main.py", "hello\nworld\n")
    _write(tmp_path, "src/main.txt", "hello\nworld\n")
    config = _write(tmp_path, "config", "--glob=!*.py\n")
    monkeypatch.setenv("RIPGREP_CONFIG_PATH", str(config))
    result = await tool.execute(
        _call(pattern="hello.*world", multiline=True, type="py", output_mode="content"),
        context=ToolExecutionContext(workspace=tmp_path),
    )
    assert not result.is_error
    assert "main.py:1:hello" in result.content and "main.py:2:world" in result.content
    assert "main.txt" not in result.content
    result = await tool.execute(
        _call(pattern="hello", glob="src/**", type="py"),
        context=ToolExecutionContext(workspace=tmp_path),
    )
    assert not result.is_error
    assert "main.py" in result.content and "main.txt" in result.content
