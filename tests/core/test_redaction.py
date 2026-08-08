from __future__ import annotations

from ouro.core.redaction import public_url, redact_text


def test_public_url_projects_only_public_route_components() -> None:
    value = "https://user:secret@example.com:8443/path?token=private#fragment"

    assert public_url(value) == "https://example.com:8443/path"


def test_public_url_rejects_non_numeric_port_without_raising() -> None:
    malformed = "https://example.com:i=5e3}n;if(window.crsRequestData={url:r.toString()}"

    assert public_url(malformed) == "***"


def test_redact_text_treats_malformed_url_from_tool_output_as_untrusted_text() -> None:
    output = (
        "downloaded script "
        "https://example.com:i=5e3}n;if(window.crsRequestData={url:r.toString()},i>=0){let"
    )

    assert redact_text(output) == "downloaded script ***"
