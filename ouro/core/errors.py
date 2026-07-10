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


class SkillManagementError(OuroError):
    """An Agent Skill management operation was invalid or unsafe."""
