"""Startup boundaries tested in fresh interpreters, independent of test collection."""

import subprocess
import sys

import pytest


def test_default_cli_paints_before_tui_without_loading_command_parser():
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import builtins
import io
import os
import sys
import threading

class Terminal(io.StringIO):
    def isatty(self): return True
    def fileno(self): return 1

output = Terminal()
sys.stdin = sys.stdout = output
os.get_terminal_size = lambda _: os.terminal_size((80, 24))
from ouro.cli.main import main
from ouro.tui.startup import TerminalStartup
assert 'textual' not in sys.modules
assert 'ouro.core' not in sys.modules
assert 'argparse' not in sys.modules
assert 'json' not in sys.modules

painted = False
draw = TerminalStartup._draw
def first_frame(self):
    global painted
    draw(self)
    painted = True
TerminalStartup._draw = first_frame

class StopStartup(Exception): pass
load = builtins.__import__
def importing(name, *args, **kwargs):
    if name == 'ouro.tui.entry':
        assert painted
        assert 'ouro.cli.dispatch' not in sys.modules
        assert 'ouro.cli.parser' not in sys.modules
        raise StopStartup()
    return load(name, *args, **kwargs)
builtins.__import__ = importing
try:
    main([])
except StopStartup:
    pass
else:
    raise AssertionError('TUI boundary was not reached')
assert not any(t.name == 'ouro-startup' for t in threading.enumerate())
assert output.getvalue().endswith('\\x1b[?25h')
""",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


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


@pytest.mark.parametrize(
    "setup",
    ["import ouro.cli.main", "from ouro.tools import builtin_tools; builtin_tools()"],
    ids=["cli-startup", "tool-registration"],
)
def test_startup_and_tool_registration_do_not_load_pillow(setup):
    subprocess.run(
        [
            sys.executable,
            "-c",
            f"{setup}; import sys; "
            "assert not any(name == 'PIL' or name.startswith('PIL.') for name in sys.modules)",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_tui_startup_does_not_load_unopened_configuration_editors():
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
import ouro.tui.app
for name in ("agents", "compaction", "mcp", "skills", "tools"):
    assert "ouro.tui.features.configuration." + name not in sys.modules, name
assert "ouro_upgrade.engine" not in sys.modules
""",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
