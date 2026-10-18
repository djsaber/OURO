"""Prepare file sources from terminal pastes without interpreting shell commands."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import unquote, urlsplit

from ouro.core.base import new_id

PASTED_TEXT_ATTACHMENT_THRESHOLD = 4000


@contextmanager
def pasted_text_file(text: str) -> Iterator[Path]:
    """Keep one UTF-8 source alive for the ordinary attachment import operation."""
    with TemporaryDirectory(prefix="ouro-paste-") as directory:
        # System temporary roots may be aliases, such as /var on macOS. This
        # source is ours; external attachment paths still undergo link rejection.
        path = Path(directory).resolve(strict=True) / f"{new_id('pasted-text')}.txt"
        path.write_text(text, encoding="utf-8", newline="")
        yield path


def pasted_files(value: str) -> tuple[Path, ...]:
    """Return files only when the entire paste unambiguously names local files."""
    if not value.strip() or len(value) > 65536 or "\x00" in value:
        return ()
    candidates = [value.strip()]
    tokens = re.findall(r""""[^"]*"|'[^']*'|(?:\\ |[^\s"'])+""", value)
    remainder = re.sub(r""""[^"]*"|'[^']*'|(?:\\ |[^\s"'])+""", "", value)
    if remainder.strip():
        return ()
    groups = (candidates, tokens)
    for group in groups:
        if not group or len(group) > 16:
            continue
        paths: list[Path] = []
        for token in group:
            token = token.strip()
            if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
                token = token[1:-1]
            if token.startswith("file:"):
                uri = urlsplit(token)
                if uri.netloc not in ("", "localhost") or uri.query or uri.fragment:
                    break
                token = unquote(uri.path)
                if os.name == "nt" and re.match(r"^/[A-Za-z]:/", token):
                    token = token[1:]
            elif os.name != "nt":
                token = token.replace("\\ ", " ")
            path = Path(token)
            try:
                if not path.is_absolute() or not path.is_file():
                    break
            except OSError:
                break
            paths.append(path)
        else:
            return tuple(paths)
    return ()
