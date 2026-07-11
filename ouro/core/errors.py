from __future__ import annotations


class OuroError(Exception):
    """Base error for OURO runtime failures."""


class ConfigError(OuroError):
    """Configuration is missing or invalid."""


class CredentialError(OuroError):
    """A credential is missing or unusable."""


class PersistenceError(OuroError):
    """Local persistence failed."""


class SessionBusyError(OuroError):
    """A session cannot accept the requested operation while another run owns it."""


class HookError(OuroError):
    """A runtime hook failed."""


class CompactionError(OuroError):
    """Context compaction could not produce and activate a valid summary."""


class CompactionStateError(CompactionError):
    """Persisted compaction coverage does not match canonical session history."""


class ContextOverflowError(CompactionError):
    """A context remains over its hard input budget after compaction."""


class SkillManagementError(OuroError):
    """An Agent Skill management operation was invalid or unsafe."""
