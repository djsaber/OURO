"""Availability of the optional numerical backend; no renderer imports."""

from importlib import import_module
from importlib.metadata import PackageNotFoundError, version

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion

_NUMPY_VERSIONS = SpecifierSet(">=2.3.3,<3")
_INSTALL_HINT = "Install ouro-agent[themes] in the OURO environment and restart the TUI."


def check_requirements() -> str | None:
    """Check the actual extension, not just the presence of package metadata."""
    try:
        installed = version("numpy")
    except PackageNotFoundError:
        return f"numpy is not installed. {_INSTALL_HINT}"
    try:
        compatible = installed in _NUMPY_VERSIONS
    except InvalidVersion:
        compatible = False
    if not compatible:
        return f"numpy {installed} is incompatible; requires {_NUMPY_VERSIONS}. {_INSTALL_HINT}"
    try:
        import_module("numpy")
    except (ImportError, OSError) as exc:
        return f"numpy could not load: {exc}. {_INSTALL_HINT}"
    return None
