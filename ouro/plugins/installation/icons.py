"""Read a packaged PNG for embedding in plugin query results."""

from __future__ import annotations

from pathlib import Path

from ouro.plugins.installation.paths import resolve_manifest_path

# Bound inline query payloads, not the size of images allowed in a plugin package.
MAX_INLINE_ICON_BYTES = 512 * 1024


def read_plugin_icon(root: Path, icon: str | None) -> bytes | None:
    if icon is None:
        return None
    path = resolve_manifest_path(root, icon, allow_directory=False)
    with path.open("rb") as stream:
        content = stream.read(MAX_INLINE_ICON_BYTES + 1)
    if len(content) > MAX_INLINE_ICON_BYTES or not content.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    return content
