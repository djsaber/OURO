class UpgradeError(ValueError):
    """A safe diagnostic which never contains record contents."""


class UpgradeDataError(UpgradeError):
    """Unsupported or invalid data within an identified upgrade boundary."""


class UpgradeBlockedError(UpgradeDataError):
    """An incomplete source transaction must be recovered, not discarded."""


def require(condition: bool, location: str, reason: str) -> None:
    if not condition:
        raise UpgradeDataError(f"{location}: {reason}")
