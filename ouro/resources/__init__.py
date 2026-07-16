"""Resource persistence contracts and local immutable storage."""

from ouro.resources.base import BlobStore, ResourceCatalogStore, ResourceStore
from ouro.resources.local import LocalResourceStore

__all__ = ["BlobStore", "LocalResourceStore", "ResourceCatalogStore", "ResourceStore"]
