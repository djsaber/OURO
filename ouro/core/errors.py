from __future__ import annotations

from typing import Literal


class OuroError(Exception):
    """Base error for OURO runtime failures."""


class ConfigError(OuroError):
    """Configuration is missing or invalid."""


class CredentialError(OuroError):
    """A credential is missing or unusable."""


class PersistenceError(OuroError):
    """Local persistence failed."""


class DelegationRevisionConflictError(PersistenceError):
    """A Delegation compare-and-swap lost to a concurrent durable update."""


class MigrationRequiredError(PersistenceError):
    """Persisted data cannot be opened without an explicit layout migration."""


class SessionBusyError(OuroError):
    """A session cannot accept the requested operation while another run owns it."""


class SessionNotFoundError(OuroError):
    """A requested canonical session does not exist."""


class PromptRejectedError(OuroError):
    """A prompt was rejected before a canonical message or Run was created."""

    def __init__(
        self,
        reason: str,
        *,
        plugin_id: str,
        policy_id: str,
        policy_failed: bool = False,
        failure_error_type: str | None = None,
        failure_message: str | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.plugin_id = plugin_id
        self.policy_id = policy_id
        self.policy_failed = policy_failed
        self.failure_error_type = failure_error_type
        self.failure_message = failure_message


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


SkillActivationErrorCode = Literal[
    "unknown_skill",
    "disabled_skill",
    "not_user_invocable",
    "unloadable_skill",
]


class SkillActivationError(OuroError):
    """A user-selected Agent Skill could not become canonical Run input."""

    def __init__(self, code: SkillActivationErrorCode, skill_name: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.skill_name = skill_name
