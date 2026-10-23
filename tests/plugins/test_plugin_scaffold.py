from __future__ import annotations

import pytest

from ouro.plugins.installation.scaffold import create_plugin_scaffold


def test_scaffold_refuses_to_replace_an_existing_destination(tmp_path) -> None:
    destination = tmp_path / "plugin"
    destination.mkdir()
    marker = destination / "keep.txt"
    marker.write_text("user content", encoding="utf-8")

    with pytest.raises(FileExistsError, match="already exists"):
        create_plugin_scaffold(
            destination,
            plugin_id="dev.ouro.existing",
            name="Existing plugin",
        )

    assert marker.read_text(encoding="utf-8") == "user content"


def test_invalid_scaffold_is_removed_before_publication(tmp_path) -> None:
    destination = tmp_path / "plugin"

    with pytest.raises(ValueError):
        create_plugin_scaffold(
            destination,
            plugin_id="dev.ouro.invalid",
            name="Invalid plugin",
            namespace="not portable",
        )

    assert not destination.exists()
    assert not list(tmp_path.glob(".plugin-*"))
