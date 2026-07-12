"""Resource persistence contracts and local immutable storage."""

from ouro.resources.base import ResourceStore
from ouro.resources.local import LocalResourceStore

__all__ = ["LocalResourceStore", "ResourceStore"]
