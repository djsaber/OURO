from __future__ import annotations

from collections.abc import Generator
from pathlib import Path


def reverse_jsonl(path: Path, *, block_size: int = 65536) -> Generator[tuple[int, bytes]]:
    """Read a journal suffix in bounded blocks, yielding byte offsets and records.

    Keep bytes until a complete line is found so UTF-8 characters may straddle
    blocks. Neither locating nor truncating a tail copies the retained prefix.
    """

    if not path.exists():
        return
    with path.open("rb") as stream:
        position = stream.seek(0, 2)
        fragments: list[bytes] = []
        while position:
            length = min(position, block_size)
            position -= length
            stream.seek(position)
            block = stream.read(length)
            end = len(block)
            while (separator := block.rfind(b"\n", 0, end)) >= 0:
                fragments.append(block[separator + 1 : end])
                line = b"".join(reversed(fragments)).rstrip(b"\r")
                if line.strip():
                    yield position + separator + 1, line
                fragments = []
                end = separator
            fragments.append(block[:end])
        line = b"".join(reversed(fragments)).rstrip(b"\r")
        if line.strip():
            yield 0, line
