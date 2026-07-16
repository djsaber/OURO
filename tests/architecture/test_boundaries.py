from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CORE_DIR = PROJECT_ROOT / "ouro" / "core"
CONFIG_DIR = PROJECT_ROOT / "ouro" / "config"
APPLICATION_DIR = PROJECT_ROOT / "ouro" / "application"
ALLOWED_CORE_PREFIX = "ouro.core"
LOWER_LAYER_DIRS = (
    "billing",
    "config",
    "mcp",
    "plugins",
    "prompts",
    "providers",
    "resources",
    "sessions",
    "skills",
    "tools",
)


def test_core_does_not_import_outer_layers() -> None:
    violations: list[str] = []
    for path in sorted(CORE_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module in _imported_modules(tree):
            if module.startswith("ouro.") and not module.startswith(ALLOWED_CORE_PREFIX):
                violations.append(f"{path.relative_to(PROJECT_ROOT)} imports {module}")

    assert violations == []


def test_core_does_not_expose_application_service_shims() -> None:
    assert not (CORE_DIR / "runtime.py").exists()
    assert not (CORE_DIR / "session.py").exists()


def test_model_catalog_is_owned_by_application_not_core_or_providers() -> None:
    assert (APPLICATION_DIR / "models.py").exists()
    assert not (CORE_DIR / "models.py").exists()
    assert not (PROJECT_ROOT / "ouro" / "providers" / "catalog.py").exists()


def test_session_use_cases_and_host_services_are_application_owned() -> None:
    assert (APPLICATION_DIR / "sessions.py").exists()
    assert (APPLICATION_DIR / "session_models.py").exists()
    assert not (APPLICATION_DIR / "facade.py").exists()
    assert not (PROJECT_ROOT / "ouro" / "sessions" / "service.py").exists()

    session_commands = ast.parse(
        (PROJECT_ROOT / "ouro" / "cli" / "session_commands.py").read_text(encoding="utf-8")
    )
    imports = _imported_modules(session_commands)
    assert "ouro.application.sessions" in imports
    assert "ouro.application.facade" not in imports


def test_application_does_not_depend_on_cli_or_provider_implementations() -> None:
    violations: list[str] = []
    for path in sorted(APPLICATION_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module in _imported_modules(tree):
            if module.startswith(("ouro.cli", "ouro.providers")):
                violations.append(f"{path.relative_to(PROJECT_ROOT)} imports {module}")

    assert violations == []


def test_provider_configuration_does_not_depend_on_cli_or_provider_implementations() -> None:
    violations: list[str] = []
    for path in sorted(CONFIG_DIR.glob("provider*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module in _imported_modules(tree):
            if module.startswith(("ouro.cli", "ouro.providers", "ouro.application")):
                violations.append(f"{path.relative_to(PROJECT_ROOT)} imports {module}")

    assert violations == []


def test_lower_layers_do_not_depend_on_application_or_cli() -> None:
    violations: list[str] = []
    for directory in LOWER_LAYER_DIRS:
        for path in sorted((PROJECT_ROOT / "ouro" / directory).glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for module in _imported_modules(tree):
                if module.startswith(("ouro.application", "ouro.cli")):
                    violations.append(f"{path.relative_to(PROJECT_ROOT)} imports {module}")

    assert violations == []


def test_shared_local_infrastructure_is_not_owned_by_a_feature_adapter() -> None:
    for relative_path in ("billing/local.py", "config/file.py"):
        path = PROJECT_ROOT / "ouro" / relative_path
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        assert not any(module.startswith("ouro.sessions") for module in _imported_modules(tree))


def test_cli_configuration_hosts_do_not_use_infrastructure_managers() -> None:
    violations: list[str] = []
    for filename in (
        "config_commands.py",
        "provider_commands.py",
        "run.py",
        "skills.py",
    ):
        path = PROJECT_ROOT / "ouro" / "cli" / filename
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            for alias in node.names:
                if alias.name.endswith(("ConfigManager", "SkillManager")):
                    violations.append(f"{path.relative_to(PROJECT_ROOT)} imports {alias.name}")

    assert violations == []


def test_cli_application_commands_use_local_composition_boundary() -> None:
    forbidden = ("ouro.sessions", "ouro.resources", "ouro.billing")
    for filename in ("session_commands.py", "resource_commands.py", "usage_commands.py"):
        path = PROJECT_ROOT / "ouro" / "cli" / filename
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        modules = _imported_modules(tree)
        assert "ouro.local_application" in modules
        assert not any(module.startswith(forbidden) for module in modules)


def test_session_backend_selection_is_owned_by_deployment_composition() -> None:
    deployment = ast.parse((PROJECT_ROOT / "ouro" / "deployment.py").read_text(encoding="utf-8"))
    assert not any(
        module.startswith(("ouro.application", "ouro.cli", "ouro.sessions"))
        for module in _imported_modules(deployment)
    )

    forbidden = ("ouro.sessions", "ouro.resources")
    for filename in (
        "approval_commands.py",
        "interactive.py",
        "main.py",
        "resource_commands.py",
        "run.py",
        "session_commands.py",
    ):
        path = PROJECT_ROOT / "ouro" / "cli" / filename
        modules = _imported_modules(ast.parse(path.read_text(encoding="utf-8")))
        assert not any(module.startswith(forbidden) for module in modules)


def _imported_modules(tree: ast.AST) -> list[str]:
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    return modules
