from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering

_SEMVER_PATTERN = re.compile(
    r"^(?P<major>0|[1-9]\d*)\."
    r"(?P<minor>0|[1-9]\d*)\."
    r"(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_COMPARATOR_PATTERN = re.compile(r"^(>=|<=|>|<|==|=)?\s*(\S+)$")


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
        prerelease = match.group("prerelease")
        if prerelease and any(
            item.isdigit() and len(item) > 1 and item.startswith("0")
            for item in prerelease.split(".")
        ):
            raise ValueError(f"invalid semantic version: {value}")
        return cls(
            major=int(match.group("major")),
            minor=int(match.group("minor")),
            patch=int(match.group("patch")),
            prerelease=tuple(prerelease.split(".")) if prerelease else (),
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
    if not normalized:
        raise ValueError("version range cannot be empty")
    if normalized == "*":
        return normalized
    if normalized.startswith(("^", "~")):
        SemVer.parse(normalized[1:])
        return normalized
    comparators = _split_range(normalized)
    if not comparators:
        raise ValueError("version range must contain a comparator")
    for comparator in comparators:
        match = _COMPARATOR_PATTERN.fullmatch(comparator)
        if match is None:
            raise ValueError(f"invalid version comparator: {comparator}")
        SemVer.parse(match.group(2))
    return normalized


def version_satisfies(version: str, requirement: str) -> bool:
    candidate = SemVer.parse(version)
    normalized = validate_version_range(requirement)
    if normalized == "*":
        return True
    if normalized.startswith("^"):
        lower = SemVer.parse(normalized[1:])
        upper = _caret_upper_bound(lower)
        return candidate >= lower and candidate < upper
    if normalized.startswith("~"):
        lower = SemVer.parse(normalized[1:])
        upper = SemVer(lower.major, lower.minor + 1, 0)
        return candidate >= lower and candidate < upper
    return all(_matches_comparator(candidate, item) for item in _split_range(normalized))


def _split_range(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _matches_comparator(candidate: SemVer, comparator: str) -> bool:
    match = _COMPARATOR_PATTERN.fullmatch(comparator)
    assert match is not None
    operator = match.group(1) or "=="
    expected = SemVer.parse(match.group(2))
    if operator in {"=", "=="}:
        return candidate == expected
    if operator == ">=":
        return candidate >= expected
    if operator == "<=":
        return candidate <= expected
    if operator == ">":
        return candidate > expected
    return candidate < expected


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
