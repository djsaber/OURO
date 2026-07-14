from __future__ import annotations


class OuroError(Exception):
    """Base error for OURO runtime failures."""


class ConfigError(OuroError):
    """Configuration is missing or invalid."""


class CredentialError(OuroError):
    """A credential is missing or unusable."""


class PersistenceError(OuroError):
    """Local persistence failed."""


class MigrationRequiredError(PersistenceError):
    """Persisted data cannot be opened without an explicit layout migration."""


class SessionBusyError(OuroError):
    """A session cannot accept the requested operation while another run owns it."""


class SessionNotFoundError(OuroError):
    """A requested canonical session does not exist."""


class SessionBranchError(OuroError):
    """A session cannot be branched at the requested canonical message boundary."""


class ResourceError(OuroError):
    """A resource operation could not preserve its public contract."""


class ResourceNotFoundError(ResourceError):
    """A requested resource record or immutable blob does not exist."""


class UnsafeResourcePathError(ResourceError):
    """A resource path crosses a filesystem safety boundary."""


class UnsupportedMediaTypeError(ResourceError):
    """A supplied media type is not syntactically usable by the resource system."""


class ResourceIntegrityError(ResourceError):
    """Persisted resource bytes do not match their canonical digest or size."""


class ResourceInUseError(ResourceError):
    """A resource reference cannot be removed while canonical messages use it."""


class ResourceAttachmentError(ResourceError):
    """A resource cannot be safely projected into a model request."""


class CompactionError(OuroError):
    """Context compaction could not produce and activate a valid summary."""


class CompactionStateError(CompactionError):
    """Persisted compaction coverage does not match canonical session history."""


class ContextOverflowError(CompactionError):
    """A context remains over its hard input budget after compaction."""


class ContextBlockedError(ContextOverflowError):
    """A run reached a durable boundary but cannot safely build another model context."""


class SkillManagementError(OuroError):
    """An Agent Skill management operation was invalid or unsafe."""
