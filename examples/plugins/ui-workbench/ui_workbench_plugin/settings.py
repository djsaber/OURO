"""Validated, generation-local settings; optional integrations start disabled."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast
from urllib.parse import urlsplit

from .files import WorkbenchError

IMAGE_CREDENTIAL = "image_api_key"
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


@dataclass(frozen=True, slots=True)
class Settings:
    webdriver_url: str
    browser: str
    media_enabled: bool
    image_api_base: str
    image_model: str

    @classmethod
    def from_configuration(cls, values: Mapping[str, object]) -> Settings:
        image_base = _endpoint(cast(str, values.get("image_api_base", "")), local=False)
        model = cast(str, values.get("image_model", "")).strip()
        if bool(image_base) != bool(model):
            raise WorkbenchError(
                "Configure both image_api_base and image_model, or leave both empty."
            )
        return cls(
            webdriver_url=_endpoint(cast(str, values.get("webdriver_url", "")), local=True),
            browser=cast(str, values.get("browser", "chrome")),
            media_enabled=cast(bool, values.get("media_enabled", False)),
            image_api_base=image_base,
            image_model=model,
        )


def _endpoint(raw: str, *, local: bool) -> str:
    value = raw.strip().rstrip("/")
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        port = parsed.port
        no_controls = all(ord(char) > 32 and ord(char) != 127 for char in value)
        clean = (
            no_controls
            and "\\" not in value
            and "?" not in value
            and "#" not in value
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
        )
        acceptable = (
            port not in {None, 0} and parsed.hostname in LOOPBACK_HOSTS
            if local
            else parsed.scheme == "https" and port != 0
        )
        if clean and acceptable and parsed.scheme in {"http", "https"}:
            return value
    except ValueError:
        pass
    label = "webdriver_url" if local else "image_api_base"
    requirement = "a loopback HTTP(S) URL with an explicit port" if local else "an HTTPS URL"
    raise WorkbenchError(f"{label} must be {requirement} without credentials, query, or fragment.")
