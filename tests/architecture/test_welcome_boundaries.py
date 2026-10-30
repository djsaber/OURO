"""Optional welcome packages must not become dependencies of the core framework."""

import ast
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement

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


def test_core_and_theme_distribution_declare_compatible_releases():
    core = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    extension = tomllib.loads((EXTENSION / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    (theme_requirement,) = map(Requirement, core["optional-dependencies"]["themes"])
    assert theme_requirement.name == extension["name"]
    assert extension["version"] in theme_requirement.specifier
    core_requirement = next(
        Requirement(value)
        for value in extension["dependencies"]
        if Requirement(value).name == core["name"]
    )
    assert core["version"] in core_requirement.specifier
    assert not any(requirement.startswith("numpy") for requirement in core["dependencies"])
