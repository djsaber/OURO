from __future__ import annotations

from ouro.tools.base import Tool
from ouro.tools.builtin_ask_user import builtin_ask_user_tool
from ouro.tools.builtin_files import builtin_file_tools
from ouro.tools.builtin_shell import builtin_command_tool, detect_command_shell
from ouro.tools.builtin_toolkit import builtin_toolkit_tools


def builtin_tools() -> list[Tool]:
    shell = detect_command_shell()
    return [
        *builtin_file_tools(),
        *builtin_toolkit_tools(),
        *([builtin_command_tool(shell)] if shell is not None else []),
        builtin_ask_user_tool(),
    ]
