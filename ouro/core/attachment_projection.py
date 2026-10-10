"""Deterministic, bounded representations of immutable attachment bytes."""

from __future__ import annotations

import codecs


def image_media_type(prefix: bytes) -> str | None:
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if prefix.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if prefix.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP":
        return "image/webp"
    return None


def file_media_type(prefix: bytes) -> str | None:
    """Recognize bounded container signatures without decoding media streams."""
    image = image_media_type(prefix)
    if image is not None:
        return image
    if prefix.startswith(b"RIFF") and prefix[8:12] == b"WAVE":
        return "audio/wav"
    id3_header = (
        len(prefix) >= 10
        and prefix.startswith(b"ID3")
        and prefix[3] in (2, 3, 4)
        and prefix[4] != 0xFF
        and all(byte < 0x80 for byte in prefix[6:10])
    )
    if id3_header or (
        len(prefix) >= 4
        and prefix[0] == 0xFF
        and prefix[1] & 0xE6 == 0xE2  # MPEG audio sync, non-reserved version, layer III
        and prefix[1] & 0x18 != 0x08
        and prefix[2] & 0xF0 not in (0, 0xF0)
        and prefix[2] & 0x0C != 0x0C
    ):
        return "audio/mpeg"
    if len(prefix) >= 16 and prefix[4:8] == b"ftyp":
        brand = prefix[8:12]
        if brand in (b"M4A ", b"M4B ", b"M4P "):
            return "audio/mp4"
        if brand in (b"isom", b"iso2", b"mp41", b"mp42", b"avc1", b"M4V "):
            return "video/mp4"
    return None


def text_encoding(prefix: bytes) -> str | None:
    if prefix.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if prefix.startswith(b"\xff\xfe"):
        return "utf-16-le"
    if prefix.startswith(b"\xfe\xff"):
        return "utf-16-be"
    if any(byte < 32 and byte not in (9, 10, 12, 13) for byte in prefix):
        return None
    try:
        codecs.getincrementaldecoder("utf-8")().decode(prefix, final=False)
    except UnicodeDecodeError:
        return None
    return "utf-8"


def decode_slice(raw: bytes, encoding: str, *, offset: int, final: bool) -> tuple[str, int, int]:
    """Drop only incomplete boundary code units, preserving verifiable byte ranges."""
    skipped = 0
    if encoding.startswith("utf-16") and offset % 2:
        raw, skipped = raw[1:], 1
    if offset and encoding.startswith("utf-16") and len(raw) >= 2:
        unit = int.from_bytes(raw[:2], "little" if encoding.endswith("le") else "big")
        if 0xDC00 <= unit <= 0xDFFF:
            raw, skipped = raw[2:], skipped + 2
    if encoding.startswith("utf-8") and offset:
        while raw and raw[0] & 0xC0 == 0x80:
            raw, skipped = raw[1:], skipped + 1
    decoder = codecs.getincrementaldecoder(
        "utf-8" if offset and encoding == "utf-8-sig" else encoding
    )()
    text = decoder.decode(raw, final=final)
    pending = len(decoder.getstate()[0])
    return (
        text.removeprefix("\ufeff") if offset == 0 else text,
        offset + skipped,
        offset + skipped + len(raw) - pending,
    )
