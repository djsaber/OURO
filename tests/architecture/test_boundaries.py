from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CORE_DIR = PROJECT_ROOT / "ouro" / "core"
CONFIG_DIR = PROJECT_ROOT / "ouro" / "config"
APPLICATION_DIR = PROJECT_ROOT / "ouro" / "application"
SDK_DIR = PROJECT_ROOT / "ouro" / "sdk"
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
OURO_IMPORT_ALLOWLIST: dict[str, frozenset[str]] = {
    "__init__": frozenset(),
    "application": frozenset(
        {
            "billing",
            "config",
            "core",
            "mcp",
            "plugins",
            "prompts",
            "resources",
            "sessions",
            "skills",
            "tools",
        }
    ),
    "billing": frozenset({"core", "local_io", "local_locks"}),
    "builtin_plugins": frozenset({"core", "plugins", "sdk", "sessions"}),
    "cli": frozenset(
        {
            "application",
            "builtin_plugins",
            "config",
            "core",
            "deployment",
            "local_application",
            "mcp",
            "sdk",
            "skills",
            "tools",
        }
    ),
    "config": frozenset({"core", "local_io", "local_locks", "prompts"}),
    "core": frozenset(),
    "deployment": frozenset({"core"}),
    "local_application": frozenset(
        {
            "application",
            "billing",
            "builtin_plugins",
            "config",
            "core",
            "deployment",
            "mcp",
            "plugins",
            "providers",
            "resources",
            "sessions",
            "skills",
            "tools",
        }
    ),
    "local_io": frozenset({"core"}),
    "local_locks": frozenset(),
    "mcp": frozenset({"config", "core", "tools"}),
    "plugins": frozenset({"core", "local_locks", "mcp", "sdk", "sessions", "skills", "tools"}),
    "prompts": frozenset(),
    "providers": frozenset({"config", "core"}),
    "resources": frozenset({"core", "local_io", "local_locks"}),
    "sdk": frozenset(),
    "sessions": frozenset({"core", "local_io", "local_locks"}),
    "skills": frozenset({"config", "core"}),
    "tools": frozenset({"core"}),
}


def test_all_ouro_imports_follow_the_explicit_dependency_matrix() -> None:
    ouro_dir = PROJECT_ROOT / "ouro"
    violations: list[str] = []
    seen_roots: set[str] = set()
    for path in sorted(ouro_dir.rglob("*.py")):
        source_root = _ouro_source_root(path)
        seen_roots.add(source_root)
        allowed_targets = OURO_IMPORT_ALLOWLIST[source_root]
        for target_root, module in _imported_ouro_roots(path):
            if target_root == source_root or target_root in allowed_targets:
                continue
            violations.append(
                f"{path.relative_to(PROJECT_ROOT)} imports disallowed dependency {module}"
            )

    assert seen_roots == set(OURO_IMPORT_ALLOWLIST)
    assert violations == []


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


def test_plugin_sdk_is_standalone_from_ouro_internals() -> None:
    violations: list[str] = []
    for path in sorted(SDK_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module in _imported_modules(tree):
            if module.startswith("ouro."):
                violations.append(f"{path.relative_to(PROJECT_ROOT)} imports {module}")

    assert violations == []


def test_semantic_policy_modules_do_not_depend_on_observers() -> None:
    for filename in ("continuations.py", "prompt_policies.py", "tool_policies.py"):
        path = CORE_DIR / filename
        modules = _imported_modules(ast.parse(path.read_text(encoding="utf-8")))
        assert "ouro.core.observers" not in modules

    observer_modules = _imported_modules(
        ast.parse((CORE_DIR / "observers.py").read_text(encoding="utf-8"))
    )
    assert not {
        "ouro.core.continuations",
        "ouro.core.prompt_policies",
        "ouro.core.tool_policies",
    }.intersection(observer_modules)


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


def test_agent_service_is_application_owned_and_not_exposed_to_plugins_or_tools() -> None:
    assert (APPLICATION_DIR / "agents.py").exists()
    violations: list[str] = []
    for directory in ("plugins", "sdk", "tools"):
        for path in sorted((PROJECT_ROOT / "ouro" / directory).rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            if "AgentService" in source:
                violations.append(f"{path.relative_to(PROJECT_ROOT)} names AgentService")
            for module in _imported_modules(tree):
                if module == "ouro.application.agents":
                    violations.append(f"{path.relative_to(PROJECT_ROOT)} imports {module}")

    for path in sorted((PROJECT_ROOT / "ouro" / "cli").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if "ouro.application.agents" in _imported_modules(tree):
            violations.append(f"{path.relative_to(PROJECT_ROOT)} bypasses LocalApplication")

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


def test_interactive_cli_does_not_reach_through_runtime_to_session_store() -> None:
    violations: list[str] = []
    for filename in ("interactive.py", "interactive_commands.py", "interactive_io.py"):
        path = PROJECT_ROOT / "ouro" / "cli" / filename
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if _attribute_path(node) in {"runtime.store", "self._runtime.store"}:
                violations.append(f"{path.relative_to(PROJECT_ROOT)} reaches RuntimeService.store")

    assert violations == []


def test_plugin_local_composition_is_not_owned_by_application_service() -> None:
    application_plugins = ast.parse((APPLICATION_DIR / "plugins.py").read_text(encoding="utf-8"))
    application_imports = _imported_modules(application_plugins)
    assert "ouro.local_application" not in application_imports

    cli_plugins = ast.parse(
        (PROJECT_ROOT / "ouro" / "cli" / "plugin_commands.py").read_text(encoding="utf-8")
    )
    cli_imports = _imported_modules(cli_plugins)
    assert "ouro.local_application" in cli_imports
    assert not any(
        module.startswith(("ouro.plugins.store", "ouro.plugins.runtime")) for module in cli_imports
    )


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


def _ouro_source_root(path: Path) -> str:
    relative_parts = path.relative_to(PROJECT_ROOT / "ouro").parts
    return relative_parts[0] if len(relative_parts) > 1 else path.stem


def _imported_ouro_roots(path: Path) -> list[tuple[str, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    relative = path.relative_to(PROJECT_ROOT).with_suffix("")
    package_parts = relative.parts[:-1]
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level == 0:
            if node.module:
                modules.append(node.module)
            continue
        retained_count = len(package_parts) - node.level + 1
        resolved_parts = (*package_parts[:retained_count], *(node.module or "").split("."))
        modules.append(".".join(part for part in resolved_parts if part))

    return [
        (module.split(".", maxsplit=2)[1], module)
        for module in modules
        if module.startswith("ouro.")
    ]


def _attribute_path(node: ast.AST) -> str | None:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return None
