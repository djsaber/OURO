from __future__ import annotations

import os
from pathlib import Path

import pytest

from ouro.core.harness import HarnessState
from ouro.core.harness_calls import HarnessCallSnapshot, accept_state_changes
from ouro.tools import Tool, ToolExecutionContext, ToolInvocation, builtin_tools


class _ExecutionContext:
    """Minimal tool execution context rooted at the test workspace."""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace.resolve()

    @property
    def workspace(self) -> str:
        return str(self._workspace)

    @property
    def workspace_only(self) -> bool:
        return False

    @property
    def is_aborted(self) -> bool:
        return False

    @property
    def abort_reason(self) -> str:
        return ""

    def raise_if_aborted(self) -> None:
        return None

    async def sleep(self, seconds: float) -> None:
        del seconds

    def resolve_path(self, path: str) -> str:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self._workspace / candidate
        return str(candidate.resolve())

    def resolve_working_directory(self, cwd: str | None = None) -> str | None:
        if cwd is not None:
            return self.resolve_path(cwd)
        return str(self._workspace)


def _tools() -> dict[str, Tool]:
    return {tool.definition.name: tool for tool in builtin_tools()}


def _call(name: str, arguments: dict[str, object], *, session_id: str = "s1") -> ToolInvocation:
    return ToolInvocation(
        tool_call_id="call-1",
        session_id=session_id,
        run_id="run-1",
        tool_name=name,
        arguments=arguments,
    )


def _write(root: Path, relative: str, content: str | bytes, *, mtime: float | None = None) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture
def context(workspace: Path) -> _ExecutionContext:
    return _ExecutionContext(workspace)


# ---------------------------------------------------------------------------
# Definition and registry assembly
# ---------------------------------------------------------------------------


def test_search_and_todolist_tools_are_assembled_as_host_builtins() -> None:
    tools = _tools()

    assert {"Glob", "Grep", "TodoList"} <= tools.keys()
    assert all(tools[name].definition.source == "builtin" for name in ("Glob", "Grep", "TodoList"))
    assert all(
        "plugin_id" not in tools[name].definition.metadata for name in ("Glob", "Grep", "TodoList")
    )


async def test_search_and_todolist_contracts_guide_model_decisions_and_describe_fields() -> None:
    tools = _tools()

    glob = tools["Glob"]
    grep = tools["Grep"]
    todos = tools["TodoList"]

    assert glob.definition.description.startswith("Use this tool to discover")
    assert "Prefer Grep" in glob.definition.description
    assert "Do not use this tool" in glob.definition.description
    assert grep.definition.description.startswith("Use this tool to locate")
    assert "Prefer Glob" in grep.definition.description
    assert todos.definition.description.startswith("Use this tool to create")
    assert "When NOT to use" in todos.definition.description

    for tool in (glob, grep, todos):
        properties = tool.definition.parameters_schema["properties"]
        assert all("description" in schema for schema in properties.values())

    todo_item_properties = todos.definition.parameters_schema["properties"]["todos"]["items"][
        "properties"
    ]
    assert all("description" in schema for schema in todo_item_properties.values())


# ---------------------------------------------------------------------------
# Glob
# ---------------------------------------------------------------------------


async def test_glob_bare_pattern_matches_recursively_sorted_by_mtime(
    workspace: Path,
    context: _ExecutionContext,
) -> None:
    old = _write(workspace, "src/old.py", "old", mtime=1_000)
    new = _write(workspace, "new.py", "new", mtime=2_000)
    _write(workspace, "src/readme.md", "doc", mtime=3_000)
    tools = _tools()

    output = tools["Glob"].handler(_call("Glob", {"pattern": "*.py"}), context)

    expected = "\n".join(path.relative_to(workspace).as_posix() for path in (new, old))
    assert output.content == expected
    assert output.structured["total"] == 2


async def test_glob_anchored_pattern_matches_one_level(
    workspace: Path,
    context: _ExecutionContext,
) -> None:
    _write(workspace, "src/top.py", "a")
    _write(workspace, "src/pkg/nested.py", "b")
    tools = _tools()

    output = tools["Glob"].handler(_call("Glob", {"pattern": "src/*.py"}), context)

    assert output.content == "src/top.py"


async def test_glob_brace_expansion(
    workspace: Path,
    context: _ExecutionContext,
) -> None:
    _write(workspace, "a/main.ts", "a")
    _write(workspace, "b/main.tsx", "b")
    _write(workspace, "c/main.js", "c")
    tools = _tools()

    output = tools["Glob"].handler(_call("Glob", {"pattern": "*.{ts,tsx}"}), context)

    assert set(output.content.splitlines()) == {"a/main.ts", "b/main.tsx"}


async def test_glob_respects_gitignore_and_include_ignored(
    workspace: Path,
    context: _ExecutionContext,
) -> None:
    _write(workspace, ".gitignore", "build/\n")
    _write(workspace, "src/app.py", "a")
    _write(workspace, "build/out.py", "b")
    tools = _tools()

    default = tools["Glob"].handler(_call("Glob", {"pattern": "*.py"}), context)
    included = tools["Glob"].handler(
        _call("Glob", {"pattern": "*.py", "include_ignored": True}),
        context,
    )

    assert default.content == "src/app.py"
    assert set(included.content.splitlines()) == {"src/app.py", "build/out.py"}


async def test_glob_respects_nested_gitignore(
    workspace: Path,
    context: _ExecutionContext,
) -> None:
    _write(workspace, "src/.gitignore", "generated/\n")
    _write(workspace, "src/kept.py", "a")
    _write(workspace, "src/generated/skip.py", "b")
    tools = _tools()

    output = tools["Glob"].handler(_call("Glob", {"pattern": "*.py"}), context)

    assert output.content == "src/kept.py"


async def test_glob_always_excludes_sensitive_files_and_vcs_metadata(
    workspace: Path,
    context: _ExecutionContext,
) -> None:
    _write(workspace, ".env", "SECRET=1")
    _write(workspace, ".env.local", "SECRET=2")
    _write(workspace, ".git/config", "vcs")
    _write(workspace, "visible.txt", "ok")
    tools = _tools()

    output = tools["Glob"].handler(
        _call("Glob", {"pattern": "**/*", "include_ignored": True}),
        context,
    )

    assert output.content == "visible.txt"


async def test_glob_truncates_at_hundred_matches(
    workspace: Path,
    context: _ExecutionContext,
) -> None:
    for index in range(105):
        _write(workspace, f"files/file_{index:03}.txt", "x")
    tools = _tools()

    output = tools["Glob"].handler(_call("Glob", {"pattern": "files/*.txt"}), context)

    lines = output.content.splitlines()
    assert lines[0] == "[Truncated at 100 matches — use a more specific pattern]"
    assert lines[1] == "Only the first 100 matches are returned."
    assert len(lines) == 102
    assert output.structured["total"] == 105
    assert output.structured["truncated"] is True


async def test_glob_reports_empty_result_and_rejects_file_root(
    workspace: Path,
    context: _ExecutionContext,
) -> None:
    target = _write(workspace, "plain.txt", "x")
    tools = _tools()

    empty = tools["Glob"].handler(_call("Glob", {"pattern": "*.rs"}), context)
    assert empty.content == "No files found."

    with pytest.raises(ValueError, match="not a directory"):
        tools["Glob"].handler(
            _call("Glob", {"pattern": "*.txt", "path": str(target)}),
            context,
        )


# ---------------------------------------------------------------------------
# Grep
# ---------------------------------------------------------------------------


async def test_grep_content_mode_formats_path_line_and_text(
    workspace: Path,
    context: _ExecutionContext,
) -> None:
    _write(workspace, "src/main.py", "def main():\n    return 1\n")
    tools = _tools()

    output = tools["Grep"].handler(
        _call("Grep", {"pattern": "return", "output_mode": "content"}),
        context,
    )

    assert output.content == "src/main.py:2:    return 1"
    assert output.structured["total"] == 1


async def test_grep_case_insensitive_and_without_line_numbers(
    workspace: Path,
    context: _ExecutionContext,
) -> None:
    _write(workspace, "notes.txt", "Hello World\n")
    tools = _tools()

    output = tools["Grep"].handler(
        _call(
            "Grep",
            {"pattern": "hello", "output_mode": "content", "-i": True, "-n": False},
        ),
        context,
    )

    assert output.content == "notes.txt:Hello World"


async def test_grep_context_lines_merge_and_separate_blocks(
    workspace: Path,
    context: _ExecutionContext,
) -> None:
    _write(
        workspace,
        "log.txt",
        "one\ntwo\nthree\nfour\nfive\nsix\nseven\neight\nnine\nten\n",
    )
    tools = _tools()

    output = tools["Grep"].handler(
        _call("Grep", {"pattern": "two|nine", "output_mode": "content", "-C": 1}),
        context,
    )

    assert output.content == (
        "log.txt-1-one\n"
        "log.txt:2:two\n"
        "log.txt-3-three\n"
        "--\n"
        "log.txt-8-eight\n"
        "log.txt:9:nine\n"
        "log.txt-10-ten"
    )


async def test_grep_glob_filter_matches_basename_and_anchored_path(
    workspace: Path,
    context: _ExecutionContext,
) -> None:
    _write(workspace, "src/main.py", "needle\n")
    _write(workspace, "docs/guide.md", "needle\n")
    tools = _tools()

    basename = tools["Grep"].handler(
        _call("Grep", {"pattern": "needle", "glob": "*.py"}),
        context,
    )
    anchored = tools["Grep"].handler(
        _call("Grep", {"pattern": "needle", "glob": "**/docs/**"}),
        context,
    )

    assert basename.content == "src/main.py"
    assert anchored.content == "docs/guide.md"


async def test_grep_type_filter_and_unknown_type(
    workspace: Path,
    context: _ExecutionContext,
) -> None:
    _write(workspace, "src/main.py", "needle\n")
    _write(workspace, "src/main.ts", "needle\n")
    tools = _tools()

    output = tools["Grep"].handler(
        _call("Grep", {"pattern": "needle", "type": "py"}),
        context,
    )
    assert output.content == "src/main.py"

    with pytest.raises(ValueError, match="unsupported file type"):
        tools["Grep"].handler(_call("Grep", {"pattern": "needle", "type": "cobol"}), context)


async def test_grep_files_with_matches_sorted_by_mtime(
    workspace: Path,
    context: _ExecutionContext,
) -> None:
    _write(workspace, "old.py", "needle\n", mtime=1_000)
    _write(workspace, "new.py", "needle\n", mtime=2_000)
    tools = _tools()

    output = tools["Grep"].handler(_call("Grep", {"pattern": "needle"}), context)

    assert output.content == "new.py\nold.py"


async def test_grep_count_matches_reports_aggregate_and_per_file_counts(
    workspace: Path,
    context: _ExecutionContext,
) -> None:
    _write(workspace, "a.py", "needle\nneedle\n")
    _write(workspace, "b.py", "needle\n")
    tools = _tools()

    output = tools["Grep"].handler(
        _call("Grep", {"pattern": "needle", "output_mode": "count_matches"}),
        context,
    )

    assert output.content == "Found 3 matches in 2 files.\na.py:2\nb.py:1"


async def test_grep_head_limit_and_offset_paginate_content(
    workspace: Path,
    context: _ExecutionContext,
) -> None:
    _write(workspace, "big.txt", "".join(f"hit {index}\n" for index in range(10)))
    tools = _tools()

    first = tools["Grep"].handler(
        _call("Grep", {"pattern": "hit", "output_mode": "content", "head_limit": 3}),
        context,
    )
    second = tools["Grep"].handler(
        _call(
            "Grep",
            {"pattern": "hit", "output_mode": "content", "head_limit": 3, "offset": 3},
        ),
        context,
    )

    assert first.content == (
        "big.txt:1:hit 0\n"
        "big.txt:2:hit 1\n"
        "big.txt:3:hit 2\n"
        "Results truncated to 3 lines (total: 10). Use offset=3 to see more."
    )
    assert second.content.startswith("big.txt:4:hit 3\nbig.txt:5:hit 4\nbig.txt:6:hit 5\n")
    assert "Use offset=6 to see more." in second.content


async def test_grep_invalid_regex_is_a_clear_error(
    workspace: Path,
    context: _ExecutionContext,
) -> None:
    _write(workspace, "a.txt", "x\n")
    tools = _tools()

    with pytest.raises(ValueError, match="invalid regular expression"):
        tools["Grep"].handler(_call("Grep", {"pattern": "([unclosed"}), context)


async def test_grep_skips_binary_and_sensitive_files(
    workspace: Path,
    context: _ExecutionContext,
) -> None:
    _write(workspace, "binary.bin", b"needle\x00needle\n")
    _write(workspace, ".env", "needle\n")
    _write(workspace, "text.txt", "needle\n")
    tools = _tools()

    output = tools["Grep"].handler(
        _call("Grep", {"pattern": "needle", "include_ignored": True}),
        context,
    )

    assert output.content == "text.txt"


async def test_grep_multiline_pattern_spans_lines(
    workspace: Path,
    context: _ExecutionContext,
) -> None:
    _write(workspace, "code.py", "def f():\n    pass\n")
    tools = _tools()

    output = tools["Grep"].handler(
        _call(
            "Grep",
            {"pattern": "def f\\(\\):\\n    pass", "output_mode": "content", "multiline": True},
        ),
        context,
    )

    assert output.content == "code.py:1:def f():\ncode.py:2:    pass"


async def test_grep_searches_hidden_files_by_default(
    workspace: Path,
    context: _ExecutionContext,
) -> None:
    _write(workspace, ".github/workflows/ci.yml", "needle\n")
    tools = _tools()

    output = tools["Grep"].handler(_call("Grep", {"pattern": "needle"}), context)

    assert output.content == ".github/workflows/ci.yml"


async def test_grep_explicit_file_path_and_sensitive_refusal(
    workspace: Path,
    context: _ExecutionContext,
) -> None:
    target = _write(workspace, "one.txt", "needle\n")
    env = _write(workspace, ".env", "needle\n")
    tools = _tools()

    output = tools["Grep"].handler(
        _call("Grep", {"pattern": "needle", "path": str(target), "output_mode": "content"}),
        context,
    )
    assert output.content == "one.txt:1:needle"

    refused = tools["Grep"].handler(
        _call("Grep", {"pattern": "needle", "path": str(env)}),
        context,
    )
    assert refused.is_error is True
    assert "sensitive" in refused.content


async def test_grep_reports_no_matches(
    workspace: Path,
    context: _ExecutionContext,
) -> None:
    _write(workspace, "a.txt", "x\n")
    tools = _tools()

    output = tools["Grep"].handler(_call("Grep", {"pattern": "absent"}), context)

    assert output.content == "No matches found."


# ---------------------------------------------------------------------------
# TodoList
# ---------------------------------------------------------------------------


async def _todo(tool, arguments, state=None, *, session_id="s1"):
    snapshot = state or HarnessCallSnapshot(
        session_id=session_id,
        harness_version="harness_test",
        history_epoch=0,
        state=HarnessState(),
    )
    output = await tool.execute(
        _call("TodoList", arguments, session_id=session_id),
        context=ToolExecutionContext(harness=snapshot),
    )
    change = output.harness_change
    return output, accept_state_changes(snapshot, [change] if change else [])


async def test_todolist_update_query_and_clear_cycle() -> None:
    tools = _tools()
    tool = tools["TodoList"]
    todos = [
        {"title": "任务 A - 新建待处理", "status": "pending"},
        {"title": "任务 B - 进行中", "status": "in_progress"},
        {"title": "任务 C - 已完成", "status": "done"},
    ]
    updated, state = await _todo(tool, {"todos": todos})
    assert updated.content == (
        "Todo list updated.\n"
        "Current todo list:\n"
        "  [pending] 任务 A - 新建待处理\n"
        "  [in_progress] 任务 B - 进行中\n"
        "  [done] 任务 C - 已完成\n"
        "\n"
        "Ensure that you continue to use the todo list to track progress. Mark tasks done "
        "immediately after finishing them, and keep exactly one task in_progress when work "
        "is underway."
    )
    queried, unchanged = await _todo(tool, {}, state)
    assert unchanged is state
    assert queried.content == (
        "Current todo list:\n"
        "  [pending] 任务 A - 新建待处理\n"
        "  [in_progress] 任务 B - 进行中\n"
        "  [done] 任务 C - 已完成"
    )
    cleared, state = await _todo(tool, {"todos": []}, state)
    assert cleared.content == "Todo list cleared."
    empty, state = await _todo(tool, {}, state)
    assert empty.content == "Todo list is empty."
    assert not state.state.states


async def test_todolist_state_belongs_to_the_call_not_the_tool_instance() -> None:
    first = _tools()
    _, state = await _todo(first["TodoList"], {"todos": [{"title": "kept", "status": "pending"}]})
    second = _tools()
    queried, _ = await _todo(second["TodoList"], {}, state)
    assert queried.content == "Current todo list:\n  [pending] kept"
    other, _ = await _todo(first["TodoList"], {}, session_id="s2")
    assert other.content == "Todo list is empty."


async def test_todolist_rejects_invalid_items() -> None:
    tools = _tools()
    with pytest.raises(ValueError, match="status"):
        await _todo(tools["TodoList"], {"todos": [{"title": "x", "status": "doing"}]})
    with pytest.raises(ValueError, match="title"):
        await _todo(tools["TodoList"], {"todos": [{"title": "", "status": "done"}]})
    with pytest.raises(ValueError, match="must be a list"):
        await _todo(tools["TodoList"], {"todos": "not-a-list"})
