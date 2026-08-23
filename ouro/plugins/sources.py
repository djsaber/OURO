from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from ouro.core.redaction import public_url
from ouro.plugins.models import PluginSourceKind
from ouro.plugins.package import PackageLimits, PluginPackageError
from ouro.plugins.paths import portable_relative_path


@dataclass(frozen=True)
class MaterializedPluginSource:
    root: Path
    source_kind: PluginSourceKind
    source_reference: str
    public_source: str
    resolved_reference: str | None = None


class PluginSourceProvider(Protocol):
    def materialize(self, destination: Path) -> MaterializedPluginSource: ...


@dataclass(frozen=True)
class LocalPluginSource:
    path: Path

    def materialize(self, destination: Path) -> MaterializedPluginSource:
        source = self.path.expanduser().resolve()
        if not source.is_dir():
            raise PluginPackageError(f"local plugin source is not a directory: {source}")
        target = destination / "package"
        shutil.copytree(source, target, symlinks=True)
        return MaterializedPluginSource(
            root=target,
            source_kind="local",
            source_reference=str(source),
            public_source=f"local:{source.name}",
            resolved_reference=str(source),
        )


@dataclass(frozen=True)
class ArchivePluginSource:
    path: Path
    limits: PackageLimits = PackageLimits()
    max_compression_ratio: float = 100.0

    def materialize(self, destination: Path) -> MaterializedPluginSource:
        source = self.path.expanduser().resolve()
        if not source.is_file():
            raise PluginPackageError(f"plugin archive does not exist: {source}")
        target = destination / "package"
        target.mkdir()
        if zipfile.is_zipfile(source):
            self._extract_zip(source, target)
        elif tarfile.is_tarfile(source):
            self._extract_tar(source, target)
        else:
            raise PluginPackageError(f"unsupported plugin archive: {source.name}")
        root = _single_directory_or_root(target)
        return MaterializedPluginSource(
            root=root,
            source_kind="archive",
            source_reference=str(source),
            public_source=f"archive:{source.name}",
            resolved_reference=source.name,
        )

    def _extract_zip(self, source: Path, target: Path) -> None:
        with zipfile.ZipFile(source) as archive:
            entries = archive.infolist()
            _validate_archive_count(entries, self.limits)
            total = 0
            for entry in entries:
                relative = _safe_archive_path(entry.filename)
                mode = entry.external_attr >> 16
                if mode and (mode & 0o170000) == 0o120000:
                    raise PluginPackageError(f"archive contains symlink: {entry.filename}")
                if entry.file_size > self.limits.max_file_bytes:
                    raise PluginPackageError(f"archive member exceeds size limit: {entry.filename}")
                total += entry.file_size
                if total > self.limits.max_total_bytes:
                    raise PluginPackageError("plugin archive exceeds total size limit")
                if entry.compress_size == 0 and entry.file_size:
                    raise PluginPackageError(f"archive member has invalid ratio: {entry.filename}")
                if (
                    entry.compress_size
                    and entry.file_size / entry.compress_size > self.max_compression_ratio
                ):
                    raise PluginPackageError(
                        f"archive member exceeds compression ratio: {entry.filename}"
                    )
                output = target.joinpath(*relative.parts)
                if entry.is_dir():
                    output.mkdir(parents=True, exist_ok=True)
                    continue
                output.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as reader, output.open("xb") as writer:
                    shutil.copyfileobj(reader, writer)

    def _extract_tar(self, source: Path, target: Path) -> None:
        with tarfile.open(source) as archive:
            entries = archive.getmembers()
            _validate_archive_count(entries, self.limits)
            total = 0
            for entry in entries:
                relative = _safe_archive_path(entry.name)
                if entry.issym() or entry.islnk() or entry.isdev() or entry.isfifo():
                    raise PluginPackageError(f"archive contains unsafe entry: {entry.name}")
                if not (entry.isfile() or entry.isdir()):
                    raise PluginPackageError(f"archive contains unsupported entry: {entry.name}")
                if entry.size > self.limits.max_file_bytes:
                    raise PluginPackageError(f"archive member exceeds size limit: {entry.name}")
                total += entry.size
                if total > self.limits.max_total_bytes:
                    raise PluginPackageError("plugin archive exceeds total size limit")
                output = target.joinpath(*relative.parts)
                if entry.isdir():
                    output.mkdir(parents=True, exist_ok=True)
                    continue
                output.parent.mkdir(parents=True, exist_ok=True)
                reader = archive.extractfile(entry)
                if reader is None:
                    raise PluginPackageError(f"could not read archive member: {entry.name}")
                with reader, output.open("xb") as writer:
                    shutil.copyfileobj(reader, writer)


@dataclass(frozen=True)
class GitPluginSource:
    url: str
    ref: str | None = None
    subdirectory: str | None = None
    limits: PackageLimits = PackageLimits()

    def materialize(self, destination: Path) -> MaterializedPluginSource:
        repository = destination / "repository"
        _run_git(["init", str(repository)])
        _run_git(["-C", str(repository), "remote", "add", "origin", self.url])
        requested_ref = self.ref or "HEAD"
        _run_git(
            [
                "-C",
                str(repository),
                "-c",
                "protocol.version=2",
                "fetch",
                "--depth=1",
                "--filter=blob:none",
                "--no-tags",
                "origin",
                requested_ref,
            ]
        )
        commit = _run_git(["-C", str(repository), "rev-parse", "FETCH_HEAD"]).strip()
        _validate_git_tree(
            repository,
            commit,
            subdirectory=self.subdirectory,
            limits=self.limits,
        )
        if self.subdirectory:
            _run_git(["-C", str(repository), "sparse-checkout", "init", "--no-cone"])
            _run_git(
                [
                    "-C",
                    str(repository),
                    "sparse-checkout",
                    "set",
                    "--no-cone",
                    self.subdirectory,
                ]
            )
        _run_git(["-C", str(repository), "checkout", "--detach", commit])
        source_root = repository
        if self.subdirectory:
            relative = _safe_archive_path(self.subdirectory)
            source_root = repository.joinpath(*relative.parts)
            if not source_root.is_dir():
                raise PluginPackageError(
                    f"Git plugin subdirectory does not exist: {self.subdirectory}"
                )
        target = destination / "package"
        shutil.copytree(source_root, target, ignore=shutil.ignore_patterns(".git"), symlinks=True)
        return MaterializedPluginSource(
            root=target,
            source_kind="git",
            source_reference=self.url,
            public_source=(
                public_url(self.url) if "://" in self.url else f"git:{Path(self.url).name}"
            ),
            resolved_reference=commit,
        )


def source_provider(
    source: str | Path,
    *,
    ref: str | None = None,
    subdirectory: str | None = None,
) -> PluginSourceProvider:
    value = str(source)
    path = Path(value).expanduser()
    if path.exists():
        if path.is_dir():
            return LocalPluginSource(path)
        return ArchivePluginSource(path)
    if value.startswith(("git+", "https://", "http://", "ssh://", "git@")):
        url = value.removeprefix("git+")
        return GitPluginSource(url=url, ref=ref, subdirectory=subdirectory)
    raise PluginPackageError(f"plugin source does not exist or use a supported Git URL: {value}")


class PluginSourceMaterializer:
    def __init__(self, provider: PluginSourceProvider) -> None:
        self.provider = provider
        self._temporary: tempfile.TemporaryDirectory[str] | None = None

    def __enter__(self) -> MaterializedPluginSource:
        self._temporary = tempfile.TemporaryDirectory(prefix="ouro-plugin-")
        try:
            return self.provider.materialize(Path(self._temporary.name))
        except BaseException:
            self._temporary.cleanup()
            self._temporary = None
            raise

    def __exit__(self, *_: object) -> None:
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None


def _safe_archive_path(value: str) -> PurePosixPath:
    try:
        return portable_relative_path(
            value,
            allow_backslash=True,
            allow_trailing_slash=True,
        )
    except ValueError as exc:
        raise PluginPackageError(f"archive path escapes package root: {value}: {exc}") from exc


def _validate_archive_count(entries: Sequence[object], limits: PackageLimits) -> None:
    if len(entries) > limits.max_files:
        raise PluginPackageError("plugin archive exceeds file count limit")


def _single_directory_or_root(root: Path) -> Path:
    children = list(root.iterdir())
    if len(children) == 1 and children[0].is_dir():
        return children[0]
    return root


def _run_git(arguments: list[str]) -> str:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            stdin=subprocess.DEVNULL,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=120,
            env={**os.environ, "GIT_LFS_SKIP_SMUDGE": "1", "GIT_TERMINAL_PROMPT": "0"},
        )
    except (FileNotFoundError, subprocess.SubprocessError, UnicodeError) as exc:
        raise PluginPackageError(f"Git source resolution failed: {type(exc).__name__}") from exc
    return completed.stdout


def _validate_git_tree(
    repository: Path,
    commit: str,
    *,
    subdirectory: str | None,
    limits: PackageLimits,
) -> None:
    arguments = ["-C", str(repository), "ls-tree", "-r", "-l", "-z", commit]
    if subdirectory:
        relative = _safe_archive_path(subdirectory)
        arguments.extend(["--", relative.as_posix()])
    raw = _run_git(arguments)
    entries = [entry for entry in raw.split("\0") if entry]
    if not entries:
        raise PluginPackageError("Git plugin source contains no files")
    if len(entries) > limits.max_files:
        raise PluginPackageError("Git plugin source exceeds file count limit")
    total = 0
    for entry in entries:
        metadata, separator, path = entry.partition("\t")
        if not separator:
            raise PluginPackageError("Git source tree returned an invalid entry")
        _safe_archive_path(path)
        parts = metadata.split()
        if len(parts) != 4 or parts[1] != "blob":
            raise PluginPackageError(f"Git source contains an unsupported entry: {path}")
        mode, _kind, _object_id, size_text = parts
        if mode == "120000":
            raise PluginPackageError(f"Git source contains a symlink: {path}")
        try:
            size = int(size_text)
        except ValueError as exc:
            raise PluginPackageError(f"Git source has an unknown file size: {path}") from exc
        if size > limits.max_file_bytes:
            raise PluginPackageError(f"Git source file exceeds size limit: {path}")
        total += size
        if total > limits.max_total_bytes:
            raise PluginPackageError("Git plugin source exceeds total size limit")
