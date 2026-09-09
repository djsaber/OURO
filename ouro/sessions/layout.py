from __future__ import annotations

from datetime import datetime

from pydantic import Field

from ouro.core.base import OuroModel, utc_now

DATA_LAYOUT_VERSION = 7


class DataLayoutManifest(OuroModel):
    data_layout_version: int = DATA_LAYOUT_VERSION
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
