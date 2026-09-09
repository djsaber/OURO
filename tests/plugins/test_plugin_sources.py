from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from ouro.plugins.package import PackageLimits, PluginPackageError
from ouro.plugins.sources import GitPluginSource


def _git(repository, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )


def _create_repository(root) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "OURO tests")
    plugin = root / "plugins" / "example"
    (plugin / ".ouro-plugin").mkdir(parents=True)
    (plugin / ".ouro-plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "plugin_id": "dev.ouro.git_example",
                "name": "Git example",
                "version": "1.0.0",
                "namespace": "git_example",
                "engines": {"ouro": ">=0.1.0,<0.2.0", "plugin_api": "^3.0.0"},
                "runtime": {"kind": "declarative"},
                "contributes": {},
                "permissions": [],
                "dependencies": {"plugins": []},
                "metadata": {},
            }
        ),
        encoding="utf-8",
    )
    (root / "outside.bin").write_bytes(b"x" * 8192)
    _git(root, "add", ".")
    _git(root, "commit", "-m", "fixture")


def test_git_subdirectory_is_size_checked_before_sparse_checkout(tmp_path) -> None:
    repository = tmp_path / "repository"
    _create_repository(repository)
    destination = tmp_path / "materialized"
    destination.mkdir()
    source = GitPluginSource(
        repository.as_uri(),
        subdirectory="plugins/example",
        limits=PackageLimits(max_files=10, max_file_bytes=4096, max_total_bytes=8192),
    )

    materialized = source.materialize(destination)

    assert (materialized.root / ".ouro-plugin" / "plugin.json").is_file()
    assert not (destination / "repository" / "outside.bin").exists()


def test_git_oversized_plugin_blob_is_rejected_before_checkout(tmp_path) -> None:
    repository = tmp_path / "repository"
    _create_repository(repository)
    oversized = repository / "plugins" / "example" / "oversized.bin"
    oversized.write_bytes(b"x" * 8192)
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "oversized")
    destination = tmp_path / "materialized"
    destination.mkdir()
    source = GitPluginSource(
        repository.as_uri(),
        subdirectory="plugins/example",
        limits=PackageLimits(max_files=10, max_file_bytes=4096, max_total_bytes=16384),
    )

    with pytest.raises(PluginPackageError, match="exceeds size limit"):
        source.materialize(destination)

    assert not (destination / "repository" / "plugins" / "example" / "oversized.bin").exists()
