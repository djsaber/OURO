from __future__ import annotations

import hashlib

from ouro.core.resources import ResourceSource, SessionResourceReference


def resource_record(
    *, content: bytes = b"fixture", filename: str = "fixture.txt", **kwargs
) -> SessionResourceReference:
    return SessionResourceReference(
        filename=filename,
        digest=f"sha256:{hashlib.sha256(content).hexdigest()}",
        size=len(content),
        media_type="text/plain",
        source=ResourceSource(name=filename),
        **kwargs,
    )
