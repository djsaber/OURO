class UpgradeError(ValueError):
    """A safe diagnostic which never contains record contents."""


def require(condition: bool, location: str, reason: str) -> None:
    if not condition:
        raise UpgradeError(f"{location}: {reason}")
