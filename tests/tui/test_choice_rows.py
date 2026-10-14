from __future__ import annotations

import pytest
from rich.console import Console
from rich.text import Text

from ouro.tui.screens import Choice, grouped_choice_options


@pytest.mark.parametrize("width", (32, 40, 72))
@pytest.mark.parametrize("label", ("Settings (JSON)", "配置结构"))
def test_long_preview_preserves_the_label_and_stays_on_one_row(width, label):
    console = Console(width=width)
    preview = '{"message":"' + "示例 content " * 30 + '"}'
    option = grouped_choice_options((Choice("settings", label, trailing=preview),))[0]

    lines = console.render_lines(option.prompt)

    assert len(lines) == 1
    rendered = "".join(segment.text for segment in lines[0])
    assert rendered.startswith(label + "  ")
    assert "…" in rendered
    assert Text(rendered).cell_len == width


def test_short_status_leaves_room_for_a_long_label():
    label = "A descriptive setting name with details"
    console = Console(width=50)
    option = grouped_choice_options((Choice("setting", label, trailing="Ready"),))[0]

    rendered = "".join(segment.text for segment in console.render_lines(option.prompt)[0])

    assert rendered.startswith(label)
    assert rendered.endswith("Ready")


def test_long_label_and_preview_both_remain_identifiable():
    console = Console(width=40)
    option = grouped_choice_options(
        (Choice("setting", "Setting " * 20, trailing="Preview " * 20),)
    )[0]

    lines = console.render_lines(option.prompt)

    assert len(lines) == 1
    rendered = "".join(segment.text for segment in lines[0])
    assert rendered.startswith("Setting")
    assert "Preview" in rendered
    assert rendered.count("…") == 2
