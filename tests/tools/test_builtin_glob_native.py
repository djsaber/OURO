from __future__ import annotations

import os
import shutil
from unittest.mock import AsyncMock, patch

import pytest

from ouro.core.cancellation import AbortSignal
from ouro.core.tools import tool_definition_contract_hash
from ouro.tools import ProcessResult, ToolExecutionAborted, ToolExecutionContext, ToolInvocation
from ouro.tools.base import ToolPolicyError
from ouro.tools.builtin.search import builtin_search_tools, search_glob


def _call(pattern="**/*", **arguments):
    return ToolInvocation(
        session_id="s",
        run_id="r",
        tool_name="Glob",
        arguments={"pattern": pattern, **arguments},
    )


def _tool(executable):
    with patch("ouro.tools.builtin.search.detect_ripgrep", return_value=executable):
        return builtin_search_tools()[0]


@pytest.fixture
def native_tool():
    executable = shutil.which("rg")
    if executable is None:
        pytest.skip("ripgrep is not installed")
    return _tool(executable)


def _write(root, relative, *, mtime=1000, content=b""):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.utime(path, (mtime, mtime))
    return path


def test_glob_contract_is_independent_of_backend_and_executable_path():
    definitions = [_tool(executable).definition for executable in (None, "/bin/rg", "C:/rg.exe")]
    assert len({tool_definition_contract_hash(item) for item in definitions}) == 1
    assert definitions[0].effect == "read_only"
    assert definitions[0].permission_mode == "auto"


@pytest.mark.parametrize("executable", [None, "/native/rg"])
async def test_detection_binds_the_selected_backend(tmp_path, executable):
    _write(tmp_path, "file.txt")
    context = ToolExecutionContext(workspace=tmp_path)
    context.run_process = AsyncMock(return_value=ProcessResult("", "", 1))
    result = await _tool(executable).execute(_call(), context=context)
    if executable is None:
        assert result.structured["matches"] == ["file.txt"]
        context.run_process.assert_not_called()
    else:
        assert result.structured["total"] == 0
        assert context.run_process.call_args.args[0][0] == executable


async def test_path_stream_handles_split_unicode_and_line_breaks(tmp_path):
    names = ["中文/有 空格.py", "-leading.py", ".hidden.py"]
    if os.name != "nt":
        names += ["line\nbreak.py", "literal\\name.py"]
    for name in names:
        _write(tmp_path, name)
    payload = b"\0".join(os.fsencode(f"./{name}") for name in reversed(names)) + b"\0"

    async def run(arguments, *, cwd, on_output):
        assert cwd == tmp_path
        assert arguments[0] == "/native/rg"
        assert "--null" in arguments and "--no-config" in arguments
        assert arguments[-2:] == ["--", "."]
        for index in range(0, len(payload), 2):
            await on_output("stdout", payload[index : index + 2])
        await on_output("stdout", b"")
        return ProcessResult("truncated capture", "", 0, stdout_truncated=True)

    context = ToolExecutionContext(workspace=tmp_path)
    context.run_process = AsyncMock(side_effect=run)
    result = await _tool("/native/rg").execute(_call("*.py"), context=context)
    assert result.structured["matches"] == sorted(names)
    assert result.structured["total"] == len(names)


@pytest.mark.parametrize(
    "process, message",
    [
        (ProcessResult("", "permission denied", 2), "permission denied"),
        (ProcessResult("", "", -1, timed_out=True), "timed out"),
    ],
)
async def test_native_failures_do_not_report_partial_totals(tmp_path, process, message):
    _write(tmp_path, "visible.py")

    async def run(*args, on_output, **kwargs):
        await on_output("stdout", b"./visible.py\0")
        return process

    context = ToolExecutionContext(workspace=tmp_path)
    context.run_process = AsyncMock(side_effect=run)
    result = await _tool("/native/rg").execute(_call(), context=context)
    assert result.is_error
    assert message in result.content
    assert "total" not in result.structured
    context.run_process.assert_awaited_once()


async def test_native_glob_checks_arguments_and_workspace_before_spawning(tmp_path):
    context = ToolExecutionContext(workspace=tmp_path, workspace_only=True)
    context.run_process = AsyncMock()
    tool = _tool("/native/rg")
    with pytest.raises(ValueError):
        await tool.execute(_call(pattern=""), context=context)
    with pytest.raises(ToolPolicyError):
        await tool.execute(_call(path=".."), context=context)
    context.run_process.assert_not_called()


async def test_streamed_glob_observes_cancellation(tmp_path):
    signal = AbortSignal()
    context = ToolExecutionContext(workspace=tmp_path, signal=signal)

    async def run(*args, on_output, **kwargs):
        signal.abort("cancel enumeration")
        await on_output("stdout", b"./file.py\0")
        pytest.fail("cancelled enumeration must stop collecting files")

    context.run_process = AsyncMock(side_effect=run)
    with pytest.raises(ToolExecutionAborted, match="cancel enumeration"):
        await _tool("/native/rg").execute(_call(), context=context)


async def test_native_glob_counts_past_pipe_capture_limit_and_selects_latest(tmp_path, native_tool):
    for index in range(180):
        _write(tmp_path, f"files/{index:03}.py", mtime=1000 + index)
    result = await native_tool.execute(
        _call("*.py"),
        context=ToolExecutionContext(workspace=tmp_path, max_output_chars=64),
    )
    assert not result.is_error
    assert result.structured["total"] == 180
    assert result.structured["truncated"] is True
    assert result.structured["matches"] == [f"files/{index:03}.py" for index in range(179, 79, -1)]


async def test_native_glob_includes_empty_binary_and_large_files(tmp_path, native_tool):
    _write(tmp_path, "empty.bin")
    _write(tmp_path, "binary.bin", content=b"\0\xff")
    large = tmp_path / "large.bin"
    with large.open("wb") as stream:
        stream.truncate(33 * 1024 * 1024)
    result = await native_tool.execute(_call(), context=ToolExecutionContext(workspace=tmp_path))
    assert set(result.structured["matches"]) == {"empty.bin", "binary.bin", "large.bin"}


async def test_native_glob_exclusions_distinguish_files_and_directories(tmp_path, native_tool):
    for name in (".env", "src/.env.local", "src/.git/config", "src/.hg/history", ".svn/entries"):
        _write(tmp_path, name)
    # .env is excluded as a filename, while VCS names are excluded as directories.
    for name in ("config/.env/visible.py", ".git", ".hg", "src/.svn"):
        _write(tmp_path, name)
    result = await native_tool.execute(
        _call(include_ignored=True),
        context=ToolExecutionContext(workspace=tmp_path),
    )
    assert set(result.structured["matches"]) == {
        "config/.env/visible.py",
        ".git",
        ".hg",
        "src/.svn",
    }


async def test_native_glob_skips_file_and_directory_symlinks(tmp_path, native_tool):
    outside = tmp_path / "outside"
    _write(outside, "file.txt")
    root = tmp_path / "root"
    root.mkdir()
    try:
        (root / "linked.txt").symlink_to(outside / "file.txt")
        (root / "linked-dir").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")
    result = await native_tool.execute(_call(), context=ToolExecutionContext(workspace=root))
    assert result.structured["total"] == 0


async def test_native_glob_ignores_parent_rules_and_ripgrep_configuration(
    tmp_path, native_tool, monkeypatch
):
    _write(tmp_path, ".gitignore", content=b"*.py\n")
    config = _write(tmp_path, "rg-config", content=b"--glob=!*.py\n")
    monkeypatch.setenv("RIPGREP_CONFIG_PATH", str(config))
    root = tmp_path / "root"
    _write(root, "kept.py")
    result = await native_tool.execute(_call("*.py"), context=ToolExecutionContext(workspace=root))
    assert result.structured["matches"] == ["kept.py"]


def test_python_glob_counts_with_zero_limit_and_breaks_mtime_ties_by_path(tmp_path):
    for name in ("z.py", "a.py", "b.py"):
        _write(tmp_path, name)
    result = search_glob(tmp_path, "*.py", include_ignored=False, limit=2)
    assert result.total == 3
    assert [entry.relative for entry in result.matches] == ["a.py", "b.py"]
    result = search_glob(tmp_path, "*.py", include_ignored=False, limit=0)
    assert result.total == 3 and result.matches == ()
