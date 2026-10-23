from __future__ import annotations

from ouro.tools.base import Tool
from ouro.tools.builtin.ask_user import builtin_ask_user_tool
from ouro.tools.builtin.files import builtin_file_tools
from ouro.tools.builtin.glob import builtin_glob_tool
from ouro.tools.builtin.grep import builtin_grep_tool
from ouro.tools.builtin.shell import builtin_command_tool, detect_command_shell
from ouro.tools.builtin.todolist import builtin_todolist_tool
from ouro.tools.builtin.view_image import builtin_view_image_tool


def builtin_tools() -> list[Tool]:
    shell = detect_command_shell()
    return [
        *builtin_file_tools(),
        builtin_view_image_tool(),
        builtin_glob_tool(),
        builtin_grep_tool(),
        builtin_todolist_tool(),
        *([builtin_command_tool(shell)] if shell is not None else []),
        builtin_ask_user_tool(),
    ]
