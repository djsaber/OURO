"""Optional welcome packages must not become dependencies of the core framework."""

import ast
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EXTENSION = ROOT / "packages/tui-themes"


def imported_modules(path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, path
            yield node.module or ""


def test_core_welcome_contracts_and_preferences_do_not_import_extensions():
    paths = [*ROOT.glob("ouro/tui/welcome/**/*.py"), ROOT / "ouro/tui/preferences.py"]
    for path in paths:
        for module in imported_modules(path):
            assert module.split(".")[0] not in {"numpy", "ouro_tui_themes"}, (path, module)


@pytest.mark.parametrize("module", ["__init__", "field", "raster", "widget", "requirements"])
def test_dot_matrix_is_independent_of_welcome_contracts_and_style_packages(module):
    path = EXTENSION / f"src/ouro_tui_themes/dot_matrix/{module}.py"
    for dependency in imported_modules(path):
        assert not dependency.startswith("ouro."), (path, dependency)
        if dependency.startswith("ouro_tui_themes."):
            assert dependency.startswith("ouro_tui_themes.dot_matrix."), (path, dependency)
        if module == "field":
            assert dependency.split(".")[0] not in {"rich", "textual"}, (path, dependency)


def test_core_and_single_theme_distribution_have_matching_releases():
    core = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    extension = tomllib.loads((EXTENSION / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert extension["version"] == core["version"]
    assert core["optional-dependencies"]["themes"] == [
        f"{extension['name']}=={extension['version']}"
    ]
    assert f"{core['name']}=={core['version']}" in extension["dependencies"]
    assert not any(requirement.startswith("numpy") for requirement in core["dependencies"])
