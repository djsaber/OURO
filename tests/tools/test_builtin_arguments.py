import pytest

from ouro.tools import ToolExecutionContext, ToolInvocation, builtin_tools
from ouro.tools.schema import tool_argument_error


@pytest.mark.parametrize(
    "name, arguments",
    [
        ("Write", {"path": "sample.txt", "content": "changed", "unknown": True}),
        ("Write", {"path": "sample.txt", "content": "changed", "mode": None}),
        ("Edit", {"path": "sample.txt", "old": "original", "new": "", "replace_all": 1}),
        ("Glob", {"pattern": "*", "include_ignored": "false"}),
        ("Grep", {"pattern": "original", "-n": 0}),
        ("TodoList", {}),
        ("TodoList", {"todos": None}),
        ("TodoList", {"todos": [{"title": "x", "status": "pending", "unknown": True}]}),
    ],
)
async def test_builtin_argument_errors_are_rejected_before_execution(tmp_path, name, arguments):
    target = tmp_path / "sample.txt"
    target.write_text("original", encoding="utf-8")
    tool = next(tool for tool in builtin_tools() if tool.definition.name == name)
    invocation = ToolInvocation(
        session_id="session", run_id="run", tool_name=name, arguments=arguments
    )

    assert tool_argument_error(tool.definition, arguments) is not None
    with pytest.raises(ValueError):
        await tool.execute(invocation, context=ToolExecutionContext(workspace=tmp_path))
    assert target.read_text(encoding="utf-8") == "original"


@pytest.mark.parametrize(
    "name, arguments",
    [
        ("Glob", {"pattern": "*.txt", "path": None}),
        ("Grep", {"pattern": "original", "path": None, "glob": None, "type": None, "-C": None}),
    ],
)
async def test_nullable_search_options_use_workspace_and_no_filter(tmp_path, name, arguments):
    (tmp_path / "sample.txt").write_text("original", encoding="utf-8")
    tool = next(tool for tool in builtin_tools() if tool.definition.name == name)
    invocation = ToolInvocation(
        session_id="session", run_id="run", tool_name=name, arguments=arguments
    )

    assert tool_argument_error(tool.definition, arguments) is None
    result = await tool.execute(invocation, context=ToolExecutionContext(workspace=tmp_path))
    assert not result.is_error
    assert "sample.txt" in result.content
