from __future__ import annotations

import os
import re
import shutil
from unittest.mock import AsyncMock

import pytest

from ouro.tools import ToolExecutionContext, ToolInvocation
from ouro.tools.builtin import search
from ouro.tools.builtin.search import search_glob


@pytest.fixture(params=["python", "ripgrep"])
def grep_tool(request, monkeypatch):
    if request.param == "python":
        monkeypatch.setattr(search, "detect_ripgrep", lambda: None)
    elif shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    return search.builtin_grep_tool(search.detect_ripgrep())


def _call(**arguments):
    return ToolInvocation(session_id="s", run_id="r", tool_name="Grep", arguments=arguments)


def _write(root, relative, content="needle\n"):
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8", newline="")
    return target


@pytest.mark.parametrize("include_ignored", [False, True])
async def test_directory_exclusions_override_includes_and_preserve_root_scope(
    tmp_path,
    grep_tool,
    include_ignored,
):
    excluded = (
        "node_modules/a.py",
        "src/node_modules/b.py",
        "vendor/generated/c.py",
        "cache[1]/d.py",
    )
    kept = ("src/main.py", "other/vendor/generated/e.py", "cache1/f.py", "upper/Node_Modules/g.py")
    for relative in (*excluded, *kept):
        _write(tmp_path, relative)
    _write(tmp_path, ".gitignore", "!node_modules/\n!vendor/\n")
    result = await grep_tool.execute(
        _call(
            pattern="needle",
            glob="*.py",
            include_ignored=include_ignored,
            exclude_dirs=["node_modules", "vendor/generated", "cache[1]"],
        ),
        context=ToolExecutionContext(workspace=tmp_path),
    )
    assert not result.is_error
    paths = {line.replace("\\", "/").removeprefix("./") for line in result.content.splitlines()}
    assert paths == set(kept)


async def test_exclusions_do_not_reject_files_with_the_same_name_or_explicit_targets(
    tmp_path, grep_tool
):
    _write(tmp_path, "src/cache")
    _write(tmp_path, "cache/hidden.txt")
    context = ToolExecutionContext(workspace=tmp_path)
    result = await grep_tool.execute(
        _call(pattern="needle", exclude_dirs=["cache"]), context=context
    )
    assert "src/cache" in result.content.replace("\\", "/")
    assert "hidden.txt" not in result.content
    result = await grep_tool.execute(
        _call(pattern="needle", path="cache/hidden.txt", exclude_dirs=["cache"]),
        context=context,
    )
    assert not result.is_error and "hidden.txt" in result.content


@pytest.mark.parametrize("directories", [None, "cache", [""], [1], [".."], ["/tmp"], ["C:\\temp"]])
async def test_invalid_exclusions_are_rejected_before_search(tmp_path, grep_tool, directories):
    context = ToolExecutionContext(workspace=tmp_path)
    context.run_process = AsyncMock()
    with pytest.raises(ValueError):
        await grep_tool.execute(_call(pattern="needle", exclude_dirs=directories), context=context)
    context.run_process.assert_not_called()


def test_excluded_directories_are_pruned_before_scanning(tmp_path, monkeypatch):
    _write(tmp_path, "cache/deep/skip.txt")
    _write(tmp_path, "src/kept.txt")
    real_scandir = os.scandir
    visited = []

    def scan(path):
        visited.append(path)
        assert path != tmp_path / "cache"
        return real_scandir(path)

    monkeypatch.setattr(search.os, "scandir", scan)
    entries = list(
        search.iter_files(
            tmp_path,
            include_ignored=True,
            exclude_dirs=search.DirectoryExclusions.parse(["cache/"]),
        )
    )
    assert [entry.relative for entry in entries] == ["src/kept.txt"]
    assert tmp_path / "src" in visited


def test_glob_filters_before_stat_and_preserves_file_order(tmp_path, monkeypatch):
    _write(tmp_path, "a.txt")
    _write(tmp_path, "b.py")
    _write(tmp_path, "dir/c.py")
    real_scandir = os.scandir
    statted = []

    class Entry:
        def __init__(self, entry):
            self.entry = entry
            self.name = entry.name

        def __getattr__(self, name):
            return getattr(self.entry, name)

        def stat(self, **kwargs):
            statted.append(self.name)
            return self.entry.stat(**kwargs)

    class Scan:
        def __init__(self, path):
            self.directory = real_scandir(path)

        def __enter__(self):
            return (Entry(entry) for entry in self.directory.__enter__())

        def __exit__(self, *args):
            return self.directory.__exit__(*args)

    monkeypatch.setattr(search.os, "scandir", Scan)
    result = search_glob(tmp_path, "*.py", include_ignored=False, limit=100)
    assert result.total == 2
    assert statted == ["b.py", "c.py"]


@pytest.mark.parametrize("pattern", ["needle", "need.e"])
def test_python_file_list_stops_matching_after_first_hit(tmp_path, monkeypatch, pattern):
    _write(tmp_path, "many.txt", "needle\n" * 25_000)
    real_numbers = search._matched_line_numbers
    yielded = 0

    def numbers(*args, **kwargs):
        nonlocal yielded
        for number in real_numbers(*args, **kwargs):
            yielded += 1
            assert yielded <= 1
            yield number

    monkeypatch.setattr(search, "_matched_line_numbers", numbers)
    result = search._grep(_call(pattern=pattern), ToolExecutionContext(workspace=tmp_path))
    assert result.structured["files"] == ["many.txt"]
    assert yielded == (0 if pattern == "needle" else 1)


def test_content_counts_remain_exact_after_collection_cap(tmp_path, monkeypatch):
    _write(tmp_path, "a.txt", "needle\n" * 2)
    _write(tmp_path, "b.txt", "needle\n" * 3)
    monkeypatch.setattr(search, "_MAX_COLLECTED_MATCHES", 2)
    result = search._grep(
        _call(pattern="needle", output_mode="content"),
        ToolExecutionContext(workspace=tmp_path),
    )
    assert result.structured["total"] == 5
    assert result.structured["truncated"] is True
    assert len(result.structured["matches"]) == 2
    assert all(match["path"] == "a.txt" for match in result.structured["matches"])


@pytest.mark.parametrize("pattern", ["needle", "need.e", "^needle$", "^", "\\n", "\u2028"])
@pytest.mark.parametrize("multiline", [False, True])
def test_python_modes_agree_on_matching_files_and_exact_line_counts(tmp_path, pattern, multiline):
    texts = {"empty.txt": "", "one.txt": "needle\r\nNEEDLE\n", "two.txt": "x\u2028needle\n"}
    for name, text in texts.items():
        _write(tmp_path, name, text)
    context = ToolExecutionContext(workspace=tmp_path)
    results = {
        mode: search._grep(_call(pattern=pattern, multiline=multiline, output_mode=mode), context)
        for mode in ("content", "files_with_matches", "count_matches")
    }
    content = results["content"].structured
    expected_files = {item["path"] for item in content.get("matches", [])}
    assert set(results["files_with_matches"].structured.get("files", [])) == expected_files
    assert results["count_matches"].structured["total"] == content["total"]
    if not multiline:
        regex = re.compile(pattern)
        expected_total = sum(
            bool(regex.search(line)) for text in texts.values() for line in text.splitlines()
        )
        assert content["total"] == expected_total
