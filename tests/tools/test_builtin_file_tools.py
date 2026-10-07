import pytest

from ouro.tools import (
    ToolExecutionContext,
    ToolExecutor,
    ToolInvocation,
    ToolRegistry,
    builtin_tools,
)


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    for tool in builtin_tools():
        registry.register(tool)
    return registry


def _invocation(tool_name: str, arguments: dict[str, object]) -> ToolInvocation:
    return ToolInvocation(
        session_id="ses_1",
        run_id="run_1",
        tool_name=tool_name,
        arguments=arguments,
    )


def test_file_tool_schema_exposes_paging_and_mutation_options() -> None:
    definitions = {item.name: item for item in _registry().definitions()}
    read = definitions["Read"]
    write = definitions["Write"]
    edit = definitions["Edit"]

    assert read.parameters_schema["properties"]["offset"]["default"] == 1
    assert read.parameters_schema["properties"]["limit"]["maximum"] == 1_000
    assert write.parameters_schema["properties"]["mode"]["enum"] == [
        "overwrite",
        "append",
    ]
    assert edit.parameters_schema["properties"]["replace_all"]["default"] is False


@pytest.mark.asyncio
async def test_read_returns_numbered_page_and_continuation_offset(tmp_path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("alpha\nbravo\ncharlie\ndelta\n", encoding="utf-8")

    outcome = await ToolExecutor(_registry()).execute(
        _invocation(
            "Read",
            {"path": str(target), "offset": 2, "limit": 2},
        )
    )

    assert outcome.result is not None
    assert outcome.result.content == "2\tbravo\n3\tcharlie"
    assert outcome.result.structured == {
        "kind": "file",
        "path": str(target.resolve()),
        "offset": 2,
        "limit": 2,
        "line_count": 2,
        "output_bytes": 17,
        "next_offset": 4,
        "line_truncated": False,
        "truncated": True,
    }


@pytest.mark.asyncio
async def test_read_numbers_universal_text_line_endings(tmp_path) -> None:
    target = tmp_path / "line-endings.txt"
    target.write_bytes(b"alpha\rbravo\r\ncharlie\n")

    outcome = await ToolExecutor(_registry()).execute(_invocation("Read", {"path": str(target)}))

    assert outcome.result is not None
    assert outcome.result.content == "1\talpha\n2\tbravo\n3\tcharlie"
    assert outcome.result.structured["line_count"] == 3
    assert outcome.result.structured["truncated"] is False


@pytest.mark.asyncio
async def test_read_rejects_directory_instead_of_listing_it(tmp_path) -> None:
    (tmp_path / "hidden.txt").write_text("must not be listed", encoding="utf-8")

    outcome = await ToolExecutor(_registry()).execute(_invocation("Read", {"path": str(tmp_path)}))

    assert outcome.result is not None
    assert outcome.result.is_error is True
    assert outcome.result.structured["error_type"] == "IsADirectoryError"
    assert "not a text file" in outcome.result.content
    assert "hidden.txt" not in outcome.result.content


@pytest.mark.asyncio
async def test_read_default_page_never_exceeds_one_thousand_lines(tmp_path) -> None:
    target = tmp_path / "many-lines.txt"
    target.write_text("".join(f"line-{index}\n" for index in range(1, 1_002)), encoding="utf-8")

    outcome = await ToolExecutor(_registry()).execute(_invocation("Read", {"path": str(target)}))

    assert outcome.result is not None
    rendered = outcome.result.content.splitlines()
    assert len(rendered) == 1_000
    assert rendered[0] == "1\tline-1"
    assert rendered[-1] == "1000\tline-1000"
    assert outcome.result.structured["next_offset"] == 1_001
    assert outcome.result.structured["truncated"] is True


@pytest.mark.asyncio
async def test_read_stops_before_next_line_when_page_reaches_byte_cap(tmp_path) -> None:
    target = tmp_path / "wide-lines.txt"
    target.write_text(f"{'a' * 60_000}\n{'b' * 60_000}\n", encoding="utf-8")

    outcome = await ToolExecutor(_registry()).execute(_invocation("Read", {"path": str(target)}))

    assert outcome.result is not None
    assert outcome.result.content == f"1\t{'a' * 60_000}"
    assert len(outcome.result.content.encode("utf-8")) <= 100_000
    assert outcome.result.structured["next_offset"] == 2
    assert outcome.result.structured["line_truncated"] is False


@pytest.mark.asyncio
async def test_read_clips_single_oversized_line_on_valid_utf8_boundary(tmp_path) -> None:
    target = tmp_path / "oversized-line.txt"
    target.write_text(f"{'你' * 40_000}\nnext", encoding="utf-8")
    executor = ToolExecutor(_registry())

    first = await executor.execute(_invocation("Read", {"path": str(target)}))
    second = await executor.execute(_invocation("Read", {"path": str(target), "offset": 2}))

    assert first.result is not None
    assert first.result.content.startswith("1\t你")
    assert len(first.result.content.encode("utf-8")) <= 100_000
    assert first.result.structured["line_truncated"] is True
    assert first.result.structured["next_offset"] == 2
    assert second.result is not None
    assert second.result.content == "2\tnext"


@pytest.mark.asyncio
async def test_read_honors_smaller_host_output_cap_without_executor_reformatting(tmp_path) -> None:
    target = tmp_path / "host-limit.txt"
    target.write_text("abcdef\nghijkl\n", encoding="utf-8")

    outcome = await ToolExecutor(_registry()).execute(
        _invocation("Read", {"path": str(target)}),
        context=ToolExecutionContext(max_output_chars=12),
    )

    assert outcome.result is not None
    assert outcome.result.content == "1\tabcdef"
    assert outcome.result.structured["next_offset"] == 2
    assert outcome.result.metadata.get("output_truncated") is None


@pytest.mark.asyncio
async def test_read_reports_non_utf8_input_as_tool_error(tmp_path) -> None:
    target = tmp_path / "binary.dat"
    target.write_bytes(b"valid\n\xffinvalid")

    outcome = await ToolExecutor(_registry()).execute(_invocation("Read", {"path": str(target)}))

    assert outcome.result is not None
    assert outcome.result.is_error is True
    assert outcome.result.structured["error_type"] == "UnicodeDecodeError"


@pytest.mark.asyncio
async def test_read_schema_rejects_more_than_one_thousand_requested_lines(tmp_path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("text", encoding="utf-8")

    outcome = await ToolExecutor(_registry()).execute(
        _invocation("Read", {"path": str(target), "limit": 1_001})
    )

    assert outcome.result is not None
    assert outcome.result.is_error is True
    assert outcome.result.structured["error_type"] == "ToolArgumentValidationError"


@pytest.mark.asyncio
async def test_write_creates_parents_and_supports_overwrite_and_append(tmp_path) -> None:
    target = tmp_path / "missing" / "parents" / "note.txt"
    executor = ToolExecutor(_registry())

    overwritten = await executor.execute(
        _invocation("Write", {"path": str(target), "content": "first\n"}),
        approved=True,
    )
    appended = await executor.execute(
        _invocation(
            "Write",
            {"path": str(target), "content": "second", "mode": "append"},
        ),
        approved=True,
    )

    assert target.read_bytes() == b"first\nsecond"
    assert overwritten.result is not None
    assert overwritten.result.structured["mode"] == "overwrite"
    assert appended.result is not None
    assert appended.result.structured["mode"] == "append"
    assert appended.result.structured["bytes"] == 6


@pytest.mark.asyncio
async def test_write_rejects_unknown_mode_before_requesting_approval(tmp_path) -> None:
    target = tmp_path / "note.txt"

    outcome = await ToolExecutor(_registry()).execute(
        _invocation(
            "Write",
            {"path": str(target), "content": "text", "mode": "merge"},
        )
    )

    assert outcome.status == "completed"
    assert outcome.approval is None
    assert outcome.result is not None
    assert outcome.result.structured["error_type"] == "ToolArgumentValidationError"
    assert not target.exists()


@pytest.mark.asyncio
async def test_edit_replaces_first_occurrence_or_all_occurrences(tmp_path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("old old old", encoding="utf-8")
    executor = ToolExecutor(_registry())

    first = await executor.execute(
        _invocation(
            "Edit",
            {"path": str(target), "old": "old", "new": "first"},
        ),
        approved=True,
    )
    all_remaining = await executor.execute(
        _invocation(
            "Edit",
            {
                "path": str(target),
                "old": "old",
                "new": "rest",
                "replace_all": True,
            },
        ),
        approved=True,
    )

    assert target.read_text(encoding="utf-8") == "first rest rest"
    assert first.result is not None
    assert first.result.structured["replacements"] == 1
    assert first.result.structured["replace_all"] is False
    assert all_remaining.result is not None
    assert all_remaining.result.structured["replacements"] == 2
    assert all_remaining.result.structured["replace_all"] is True


@pytest.mark.asyncio
async def test_edit_missing_text_is_error_and_leaves_file_unchanged(tmp_path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("original", encoding="utf-8")

    outcome = await ToolExecutor(_registry()).execute(
        _invocation(
            "Edit",
            {"path": str(target), "old": "missing", "new": "replacement"},
        ),
        approved=True,
    )

    assert outcome.result is not None
    assert outcome.result.is_error is True
    assert outcome.result.structured["replacements"] == 0
    assert target.read_text(encoding="utf-8") == "original"


@pytest.mark.asyncio
async def test_edit_matches_read_output_line_endings_in_crlf_file(tmp_path) -> None:
    target = tmp_path / "crlf-note.txt"
    target.write_bytes(b"alpha\r\nbravo\r\ncharlie\r\n")
    executor = ToolExecutor(_registry())

    read = await executor.execute(_invocation("Read", {"path": str(target)}))
    assert read.result is not None
    assert read.result.content == "1\talpha\n2\tbravo\n3\tcharlie"

    edited = await executor.execute(
        _invocation(
            "Edit",
            {"path": str(target), "old": "bravo\ncharlie", "new": "BRAVO\nCHARLIE"},
        ),
        approved=True,
    )

    assert edited.result is not None
    assert edited.result.is_error is False
    assert edited.result.structured["replacements"] == 1
    assert target.read_bytes() == b"alpha\r\nBRAVO\r\nCHARLIE\r\n"


@pytest.mark.asyncio
async def test_edit_replaces_all_occurrences_in_crlf_file(tmp_path) -> None:
    target = tmp_path / "crlf-all.txt"
    target.write_bytes(b"old\r\nold\r\n")

    outcome = await ToolExecutor(_registry()).execute(
        _invocation(
            "Edit",
            {"path": str(target), "old": "old\n", "new": "new\n", "replace_all": True},
        ),
        approved=True,
    )

    assert outcome.result is not None
    assert outcome.result.is_error is False
    assert outcome.result.structured["replacements"] == 2
    assert target.read_bytes() == b"new\r\nnew\r\n"


@pytest.mark.asyncio
async def test_edit_normalizes_crlf_line_endings_in_arguments(tmp_path) -> None:
    target = tmp_path / "lf-note.txt"
    target.write_bytes(b"alpha\nbravo\n")

    outcome = await ToolExecutor(_registry()).execute(
        _invocation(
            "Edit",
            {"path": str(target), "old": "alpha\r\nbravo", "new": "ALPHA\r\nBRAVO"},
        ),
        approved=True,
    )

    assert outcome.result is not None
    assert outcome.result.is_error is False
    assert target.read_bytes() == b"ALPHA\nBRAVO\n"
