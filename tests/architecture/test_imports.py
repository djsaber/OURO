"""Startup boundaries tested in fresh interpreters, independent of test collection."""

import subprocess
import sys

import pytest


@pytest.mark.parametrize("module", ["ouro.cli.main", "ouro.tui.state", "ouro.mcp.models"])
def test_configuration_and_projection_imports_do_not_load_optional_runtimes(module):
    subprocess.run(
        [
            sys.executable,
            "-c",
            f"import {module}; import sys; "
            "assert 'mcp' not in sys.modules; "
            "assert 'ouro.mcp.sdk' not in sys.modules; "
            "assert 'ouro.tui.app' not in sys.modules",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
