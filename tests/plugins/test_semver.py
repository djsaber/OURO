import pytest

from ouro.plugins.semver import SemVer, version_satisfies


def test_numeric_components_require_ascii_digits():
    with pytest.raises(ValueError, match="invalid semantic version"):
        SemVer.parse("1.2.3\u0660")


def test_unsatisfied_constraint_does_not_hide_invalid_version():
    with pytest.raises(ValueError, match="invalid semantic version"):
        version_satisfies("1.0.0", ">=2.0.0, <invalid")
