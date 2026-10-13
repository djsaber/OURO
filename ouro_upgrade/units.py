"""Isolate conversion failures within explicitly owned data boundaries."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Literal

from ouro_upgrade.errors import UpgradeBlockedError, UpgradeDataError
from ouro_upgrade.workspace import Workspace


@dataclass(frozen=True)
class UpgradeUnit:
    kind: Literal["configuration", "session", "task", "session_store", "task_store", "usage"]
    identity: str


@dataclass(frozen=True)
class UpgradeFailure:
    unit: UpgradeUnit
    error: str
    discardable: bool


class UpgradeUnits:
    """Keep successful staging and roll back only a failed conversion's output."""

    def __init__(
        self,
        work: Workspace,
        *,
        isolate: bool = False,
        discard: frozenset[UpgradeUnit] = frozenset(),
    ) -> None:
        self.work = work
        self.isolate = isolate
        self.discard = discard
        self.failures: list[UpgradeFailure] = []
        self.discarded: list[UpgradeUnit] = []

    def run[T](
        self,
        unit: UpgradeUnit,
        convert: Callable[[], T],
        *,
        remove: Callable[[], None] | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> T | None:
        checkpoint = len(self.work.changes)
        failures = len(self.failures)
        discarded = len(self.discarded)
        if connection is not None:
            connection.execute("SAVEPOINT upgrade_unit")
        try:
            return convert()
        except UpgradeDataError as exc:
            if not self.isolate:
                raise
            if connection is not None:
                connection.execute("ROLLBACK TO upgrade_unit")
            self.work.rollback_staging(checkpoint)
            del self.failures[failures:]
            del self.discarded[discarded:]
            discardable = remove is not None and not isinstance(exc, UpgradeBlockedError)
            if discardable and unit in self.discard:
                assert remove is not None
                remove()
                self.discarded.append(unit)
            else:
                self.failures.append(UpgradeFailure(unit, str(exc), discardable))
            return None
        finally:
            if connection is not None:
                connection.execute("RELEASE upgrade_unit")

    def report(self) -> dict[str, object]:
        return {
            "failures": [asdict(failure) for failure in self.failures],
            "discarded": [asdict(unit) for unit in self.discarded],
        }
