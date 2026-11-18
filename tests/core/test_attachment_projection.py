import pytest

from ouro.core.attachment_projection import decode_slice


def test_utf8_signature_preserves_the_first_content_character():
    raw = "\ufefftext".encode("utf-8-sig")
    assert decode_slice(raw, "utf-8-sig", offset=0, final=True) == ("\ufefftext", 0, len(raw))


def test_utf8_tail_drops_only_a_partial_codepoint():
    assert decode_slice("🙂ok".encode()[1:], "utf-8", offset=1, final=True) == ("ok", 4, 6)
    with pytest.raises(UnicodeDecodeError):
        decode_slice(b"\x80" * 4 + b"ok", "utf-8", offset=1, final=True)
