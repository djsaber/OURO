from __future__ import annotations

from ouro.tools.base import Tool
from ouro.tools.builtin.ask_user import builtin_ask_user_tool
from ouro.tools.builtin.files import builtin_file_tools
from ouro.tools.builtin.search import builtin_search_tools
from ouro.tools.builtin.shell import builtin_command_tools
from ouro.tools.builtin.todolist import builtin_todolist_tool
from ouro.tools.builtin.view_image import builtin_view_image_tool


def builtin_tools() -> list[Tool]:
    return [
        *builtin_file_tools(),
        builtin_view_image_tool(),
        *builtin_search_tools(),
        builtin_todolist_tool(),
        *builtin_command_tools(),
        builtin_ask_user_tool(),
    ]
