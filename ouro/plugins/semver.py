from __future__ import annotations

import operator
import re
from dataclasses import dataclass
from functools import total_ordering

_SEMVER_PATTERN = re.compile(
    r"^(?P<major>0|[1-9]\d*)\."
    r"(?P<minor>0|[1-9]\d*)\."
    r"(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$",
    re.ASCII,
)
_COMPARATOR_PATTERN = re.compile(r"^(>=|<=|>|<|==|=)?\s*(\S+)$")
_OPERATORS = {
    "=": operator.eq,
    "==": operator.eq,
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
}


@total_ordering
@dataclass(frozen=True)
class SemVer:
    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()

    @classmethod
    def parse(cls, value: str) -> SemVer:
        match = _SEMVER_PATTERN.fullmatch(value.strip())
        if match is None:
            raise ValueError(f"invalid semantic version: {value}")
        raw = match.group("prerelease")
        prerelease = tuple(raw.split(".")) if raw else ()
        if any(item.isdigit() and len(item) > 1 and item.startswith("0") for item in prerelease):
            raise ValueError(f"invalid semantic version: {value}")
        return cls(
            major=int(match.group("major")),
            minor=int(match.group("minor")),
            patch=int(match.group("patch")),
            prerelease=prerelease,
        )

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        left = (self.major, self.minor, self.patch)
        right = (other.major, other.minor, other.patch)
        if left != right:
            return left < right
        return _prerelease_less_than(self.prerelease, other.prerelease)


def validate_version_range(value: str) -> str:
    normalized = value.strip()
    _parse_range(normalized)
    return normalized


def _parse_range(normalized: str) -> list[tuple[str, SemVer]]:
    if not normalized:
        raise ValueError("version range cannot be empty")
    if normalized == "*":
        return []
    if normalized.startswith(("^", "~")):
        lower = SemVer.parse(normalized[1:])
        upper = (
            _caret_upper_bound(lower)
            if normalized[0] == "^"
            else SemVer(lower.major, lower.minor + 1, 0)
        )
        return [(">=", lower), ("<", upper)]
    comparators = []
    for comparator in map(str.strip, normalized.split(",")):
        if not comparator:
            continue
        match = _COMPARATOR_PATTERN.fullmatch(comparator)
        if match is None:
            raise ValueError(f"invalid version comparator: {comparator}")
        comparators.append((match.group(1) or "=", SemVer.parse(match.group(2))))
    if not comparators:
        raise ValueError("version range must contain a comparator")
    return comparators


def version_satisfies(version: str, requirement: str) -> bool:
    candidate = SemVer.parse(version)
    return all(_OPERATORS[op](candidate, bound) for op, bound in _parse_range(requirement.strip()))


def _caret_upper_bound(version: SemVer) -> SemVer:
    if version.major:
        return SemVer(version.major + 1, 0, 0)
    if version.minor:
        return SemVer(0, version.minor + 1, 0)
    return SemVer(0, 0, version.patch + 1)


def _prerelease_less_than(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    if left == right:
        return False
    if not left:
        return False
    if not right:
        return True
    for left_item, right_item in zip(left, right, strict=False):
        if left_item == right_item:
            continue
        left_numeric = left_item.isdigit()
        right_numeric = right_item.isdigit()
        if left_numeric and right_numeric:
            return int(left_item) < int(right_item)
        if left_numeric != right_numeric:
            return left_numeric
        return left_item < right_item
    return len(left) < len(right)
