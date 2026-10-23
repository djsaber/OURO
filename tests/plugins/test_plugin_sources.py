from __future__ import annotations

import json
import shutil
import subprocess
import tarfile
import zipfile

import pytest

from ouro.plugins.installation.package import PackageLimits
from ouro.plugins.installation.paths import PluginPackageError
from ouro.plugins.installation.sources import ArchivePluginSource, GitPluginSource


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
                "engines": {"ouro": ">=0.3.0,<0.4.0", "plugin_api": "^5.0.0"},
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


@pytest.mark.parametrize("archive_type", ["zip", "tar"])
def test_archive_materialization_excludes_cache_payload_and_limits(tmp_path, archive_type):
    source = tmp_path / "source"
    source.mkdir()
    (source / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    cache = source / "__pycache__" / "module.pyc"
    cache.parent.mkdir()
    cache.write_bytes(b"cached" * 2000)
    archive_path = tmp_path / f"source.{archive_type}"
    if archive_type == "zip":
        with zipfile.ZipFile(archive_path, "w") as archive:
            for path in source.rglob("*"):
                if path.is_file():
                    archive.write(path, path.relative_to(source).as_posix())
    else:
        with tarfile.open(archive_path, "w") as archive:
            for path in source.rglob("*"):
                if path.is_file():
                    archive.add(path, arcname=path.relative_to(source).as_posix())
    destination = tmp_path / "materialized"
    destination.mkdir()

    materialized = ArchivePluginSource(
        archive_path, limits=PackageLimits(max_files=1, max_file_bytes=100, max_total_bytes=100)
    ).materialize(destination)

    assert (materialized.root / "module.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert not (materialized.root / "__pycache__").exists()


def test_git_source_excludes_cache_payload_from_package_limits(tmp_path):
    repository = tmp_path / "repository"
    _create_repository(repository)
    cache = repository / "plugins" / "example" / "__pycache__" / "module.pyc"
    cache.parent.mkdir()
    cache.write_bytes(b"cached" * 2000)
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "cache")
    destination = tmp_path / "materialized"
    destination.mkdir()

    materialized = GitPluginSource(
        repository.as_uri(),
        subdirectory="plugins/example",
        limits=PackageLimits(max_files=1, max_file_bytes=4096, max_total_bytes=8192),
    ).materialize(destination)

    assert (materialized.root / ".ouro-plugin" / "plugin.json").is_file()
    assert not (materialized.root / "__pycache__").exists()


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
