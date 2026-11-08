"""Check explicit runtime dependencies, including imports inside functions."""

from __future__ import annotations

import ast
import subprocess
import sys
from collections.abc import Iterator
from graphlib import CycleError, TopologicalSorter
from importlib.util import resolve_name
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_local_composition_keeps_contribution_adapters_lazy() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import ouro.local_application; "
            "assert 'ouro.plugins.contributions.inventory' not in sys.modules",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_explicit_runtime_imports_do_not_form_cycles() -> None:
    modules = {
        ".".join(path.relative_to(PROJECT_ROOT).with_suffix("").parts).removesuffix(
            ".__init__"
        ): path
        for path in (PROJECT_ROOT / "ouro").rglob("*.py")
    }
    graph: dict[str, set[str]] = {}
    for module, path in modules.items():
        package = module if path.name == "__init__.py" else module.rpartition(".")[0]
        imports = _runtime_imports(ast.parse(path.read_text(encoding="utf-8")))
        dependencies: set[str] = set()
        for node in imports:
            if isinstance(node, ast.Import):
                dependencies.update(alias.name for alias in node.names)
                continue
            parent = node.module or ""
            if node.level:
                parent = resolve_name("." * node.level + parent, package)
            # Importing a named submodule uses that module, not a package export.
            for alias in node.names:
                child = f"{parent}.{alias.name}"
                dependencies.add(child if child in modules else parent)
        graph[module] = dependencies & modules.keys()

    try:
        tuple(TopologicalSorter(graph).static_order())
    except CycleError as exc:
        pytest.fail("Runtime import cycle: " + " -> ".join(exc.args[1]))


def _runtime_imports(node: ast.AST) -> Iterator[ast.Import | ast.ImportFrom]:
    if isinstance(node, ast.If) and (
        isinstance(node.test, ast.Name)
        and node.test.id == "TYPE_CHECKING"
        or isinstance(node.test, ast.Attribute)
        and isinstance(node.test.value, ast.Name)
        and node.test.value.id == "typing"
        and node.test.attr == "TYPE_CHECKING"
    ):
        for alternate in node.orelse:
            yield from _runtime_imports(alternate)
        return
    if isinstance(node, ast.Import | ast.ImportFrom):
        yield node
    for child in ast.iter_child_nodes(node):
        yield from _runtime_imports(child)
