"""Load and release private modules for one plugin instance."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

from ouro.sdk.plugins import v5 as sdk


class LoadedPluginModule:
    def __init__(self, module: ModuleType, package_root: Path, imported_names: set[str]) -> None:
        self.module = module
        self.package_root = package_root
        self.imported_names = imported_names
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for name in self.imported_names:
            module = sys.modules.get(name)
            if module is not None and _module_belongs_to(module, self.package_root):
                sys.modules.pop(name, None)


def load_plugin_factory(
    package_root: Path,
    entrypoint: str,
    *,
    module_namespace: str,
    generation_id: str,
) -> tuple[Callable[[sdk.PluginContext], object], LoadedPluginModule]:
    module_name, _, factory_name = entrypoint.partition(":")
    top_level = module_name.partition(".")[0]
    if top_level != module_namespace:
        raise RuntimeError(
            "plugin entrypoint escaped its owned top-level module: "
            f"{top_level} != {module_namespace}"
        )
    existing = sys.modules.get(top_level)
    if existing is not None:
        raise RuntimeError(f"plugin top-level module conflicts with loaded module: {top_level}")
    private_namespace = f"_ouro_plugin_{generation_id}"
    if private_namespace in sys.modules:
        raise RuntimeError(f"plugin generation module is already loaded: {generation_id}")
    before = set(sys.modules)
    previous_bytecode_setting = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        try:
            module = _load_private_plugin_module(
                package_root,
                module_name=module_name,
                module_namespace=module_namespace,
                private_namespace=private_namespace,
            )
            if not _module_belongs_to(module, package_root):
                raise RuntimeError(f"plugin entrypoint resolved outside package: {entrypoint}")
            factory = getattr(module, factory_name, None)
            if factory is None or not callable(factory):
                raise RuntimeError(f"plugin entrypoint factory is not callable: {entrypoint}")
        except BaseException:
            for name in set(sys.modules) - before:
                loaded = sys.modules.get(name)
                if loaded is not None and _module_belongs_to(loaded, package_root):
                    sys.modules.pop(name, None)
            raise
    finally:
        sys.dont_write_bytecode = previous_bytecode_setting
    imported = {
        name
        for name in set(sys.modules) - before
        if (loaded := sys.modules.get(name)) is not None
        and _module_belongs_to(loaded, package_root)
    }
    return factory, LoadedPluginModule(module, package_root, imported)


def _load_private_plugin_module(
    package_root: Path,
    *,
    module_name: str,
    module_namespace: str,
    private_namespace: str,
) -> ModuleType:
    package_directory = package_root / module_namespace
    package_init = package_directory / "__init__.py"
    module_file = package_root / f"{module_namespace}.py"
    suffix = module_name.removeprefix(module_namespace)
    if package_init.is_file():
        spec = importlib.util.spec_from_file_location(
            private_namespace,
            package_init,
            submodule_search_locations=[str(package_directory)],
        )
        root = _execute_private_module(spec, private_namespace)
        if not suffix:
            return root
        return importlib.import_module(private_namespace + suffix)
    if suffix:
        raise RuntimeError("single-file plugin entrypoint cannot name a child module")
    spec = importlib.util.spec_from_file_location(private_namespace, module_file)
    return _execute_private_module(spec, private_namespace)


def _execute_private_module(
    spec: importlib.machinery.ModuleSpec | None,
    module_name: str,
) -> ModuleType:
    if spec is None or spec.loader is None:
        raise RuntimeError(f"plugin module cannot be loaded: {module_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


def _module_belongs_to(module: ModuleType, package_root: Path) -> bool:
    filename = getattr(module, "__file__", None)
    if filename is None:
        return False
    try:
        return Path(filename).resolve().is_relative_to(package_root.resolve())
    except OSError:
        return False
