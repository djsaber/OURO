from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp
from urllib.parse import unquote, urlsplit, urlunsplit
from uuid import uuid4

from ouro.config.file import JsonConfigFile
from ouro.config.skills import (
    ManagedSkillRecord,
    SkillConfigManager,
    SkillInstallScope,
)
from ouro.core.errors import SkillManagementError
from ouro.skills.context import discover_skill_catalog
from ouro.skills.loader import SkillLoader
from ouro.skills.models import SkillDiagnostic, SkillMetadata
from ouro.skills.parser import SkillDocumentParser

_IGNORED_INSTALL_DIRECTORIES = {".git", ".hg", ".svn", "__pycache__"}
_MAX_INSTALL_FILES = 10_000
_MAX_INSTALL_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True)
class ManagedSkillResult:
    name: str
    source: str
    target: Path
    scope: SkillInstallScope


@dataclass(frozen=True)
class SkillManagementResult:
    action: str
    skills: tuple[ManagedSkillResult, ...] = ()
    diagnostics: tuple[SkillDiagnostic, ...] = ()


@dataclass(frozen=True)
class SkillValidationResult:
    skills: tuple[SkillMetadata, ...]
    diagnostics: tuple[SkillDiagnostic, ...]

    @property
    def valid(self) -> bool:
        return bool(self.skills) and not any(item.level == "error" for item in self.diagnostics)


@dataclass(frozen=True)
class _ResolvedSource:
    root: Path
    source: str


@dataclass
class _Deployment:
    metadata: SkillMetadata
    source_dir: Path
    target: Path
    stage: Path
    backup: Path | None = None


class SkillManager:
    """Manage Ouro-owned Agent Skills without mutating community skill documents."""

    def __init__(
        self,
        home: str | Path | None = None,
        *,
        cwd: str | Path | None = None,
    ) -> None:
        self.config = SkillConfigManager(home)
        self.home = self.config.root.expanduser().resolve()
        self.cwd = (Path(cwd) if cwd is not None else Path.cwd()).expanduser().resolve()
        self.parser = SkillDocumentParser()

    def validate(self, path: str | Path) -> SkillValidationResult:
        target = Path(path).expanduser().resolve()
        if target.is_file():
            if target.name != "SKILL.md":
                raise SkillManagementError(f"expected SKILL.md, got: {target}")
            result = self.parser.parse(target)
            skills = (result.skill.metadata,) if result.skill else ()
            return SkillValidationResult(skills=skills, diagnostics=result.diagnostics)
        if not target.is_dir():
            raise SkillManagementError(f"skill path does not exist: {target}")
        catalog = SkillLoader(target).load_catalog()
        return SkillValidationResult(
            skills=tuple(catalog.skills),
            diagnostics=tuple(catalog.diagnostics),
        )

    def install(
        self,
        source: str,
        *,
        scope: SkillInstallScope = "user",
        selector: str | None = None,
        install_all: bool = False,
        force: bool = False,
        ref: str | None = None,
    ) -> SkillManagementResult:
        if selector and install_all:
            raise SkillManagementError("--skill and --all cannot be used together")
        with self._resolve_source(source, ref=ref) as resolved:
            loader = SkillLoader(resolved.root)
            catalog = loader.load_catalog()
            selected = self._select_skills(catalog.skills, selector, install_all)
            with JsonConfigFile(self.home).mutation():
                deployments = self._prepare_deployments(selected, scope=scope, force=force)
                records = {
                    item.metadata.name: ManagedSkillRecord(
                        source=resolved.source,
                        scope=scope,
                        target=str(item.target),
                        selector=item.metadata.name
                        if len(catalog.skills) > 1 or selector
                        else None,
                        ref=ref,
                    )
                    for item in deployments
                }
                self._commit_deployments(deployments, records)
                return SkillManagementResult(
                    action="installed",
                    skills=tuple(
                        ManagedSkillResult(
                            name=item.metadata.name,
                            source=resolved.source,
                            target=item.target,
                            scope=scope,
                        )
                        for item in deployments
                    ),
                    diagnostics=tuple(catalog.diagnostics),
                )

    def update(self, name: str, *, force: bool = True) -> SkillManagementResult:
        record = self.config.snapshot().config.installed.get(name)
        if record is None:
            raise SkillManagementError(f"skill is not managed by Ouro: {name}")
        result = self.install(
            record.source,
            scope=record.scope,
            selector=record.selector or name,
            force=force,
            ref=record.ref,
        )
        return SkillManagementResult(
            action="updated",
            skills=result.skills,
            diagnostics=result.diagnostics,
        )

    def remove(self, name: str, *, force: bool = False) -> SkillManagementResult:
        with JsonConfigFile(self.home).mutation():
            snapshot = self.config.snapshot()
            record = snapshot.config.installed.get(name)
            target = (
                Path(record.target).resolve() if record else self._unmanaged_native_target(name)
            )
            if target is None:
                raise SkillManagementError(f"skill is not managed by Ouro: {name}")
            if record is None and not force:
                raise SkillManagementError(
                    f"skill has no Ouro install record: {name}; use --force for native directories"
                )
            self._assert_managed_target(target)
            if not target.exists():
                self.config.forget_install(name)
                return SkillManagementResult(action="removed")

            trash = target.parent / f".{target.name}.remove-{uuid4().hex}"
            target.replace(trash)
            try:
                self.config.forget_install(name)
            except Exception:
                trash.replace(target)
                raise
            shutil.rmtree(trash)
            scope: SkillInstallScope = record.scope if record else self._scope_for_target(target)
            return SkillManagementResult(
                action="removed",
                skills=(
                    ManagedSkillResult(
                        name=name,
                        source=record.source if record else str(target),
                        target=target,
                        scope=scope,
                    ),
                ),
            )

    def set_enabled(self, name: str, *, enabled: bool) -> SkillManagementResult:
        catalog = discover_skill_catalog(
            home=self.home,
            cwd=self.cwd,
            include_disabled=True,
        )
        if name not in catalog.by_name() and name not in self.config.snapshot().config.installed:
            raise SkillManagementError(f"skill not found: {name}")
        self.config.set_skill_enabled(name, enabled=enabled)
        metadata = catalog.by_name().get(name)
        skills: tuple[ManagedSkillResult, ...] = ()
        if metadata:
            skills = (
                ManagedSkillResult(
                    name=name,
                    source=metadata.location,
                    target=Path(metadata.location).parent,
                    scope="workspace" if metadata.source == "workspace" else "user",
                ),
            )
        return SkillManagementResult(action="enabled" if enabled else "disabled", skills=skills)

    def _select_skills(
        self,
        skills: Sequence[SkillMetadata],
        selector: str | None,
        install_all: bool,
    ) -> list[SkillMetadata]:
        if not skills:
            raise SkillManagementError("source contains no valid Agent Skills")
        if selector:
            selected = [skill for skill in skills if skill.name == selector]
            if not selected:
                available = ", ".join(skill.name for skill in skills)
                raise SkillManagementError(
                    f"skill {selector!r} not found in source; available: {available}"
                )
            return selected
        if install_all:
            return list(skills)
        if len(skills) != 1:
            available = ", ".join(skill.name for skill in skills)
            raise SkillManagementError(
                f"source contains multiple skills ({available}); use --skill NAME or --all"
            )
        return [skills[0]]

    def _prepare_deployments(
        self,
        skills: Sequence[SkillMetadata],
        *,
        scope: SkillInstallScope,
        force: bool,
    ) -> list[_Deployment]:
        root = self._install_root(scope)
        root.mkdir(parents=True, exist_ok=True)
        deployments: list[_Deployment] = []
        targets: set[Path] = set()
        try:
            for metadata in skills:
                source_dir = Path(metadata.location).parent.resolve()
                self._assert_safe_source_tree(source_dir)
                target = (root / source_dir.name).resolve()
                if target.parent != root:
                    raise SkillManagementError(f"unsafe skill target: {target}")
                if target in targets:
                    raise SkillManagementError(f"multiple skills resolve to target: {target}")
                if target.exists() and not force:
                    raise SkillManagementError(f"skill target already exists: {target}")
                stage = Path(mkdtemp(prefix=".ouro-skill-stage-", dir=root))
                try:
                    shutil.rmtree(stage)
                    shutil.copytree(
                        source_dir,
                        stage,
                        ignore=shutil.ignore_patterns(*_IGNORED_INSTALL_DIRECTORIES),
                    )
                except Exception:
                    if stage.exists():
                        shutil.rmtree(stage)
                    raise
                deployments.append(
                    _Deployment(
                        metadata=metadata,
                        source_dir=source_dir,
                        target=target,
                        stage=stage,
                    )
                )
                targets.add(target)
        except Exception:
            for item in deployments:
                if item.stage.exists():
                    shutil.rmtree(item.stage)
            raise
        return deployments

    def _commit_deployments(
        self,
        deployments: Sequence[_Deployment],
        records: dict[str, ManagedSkillRecord],
    ) -> None:
        committed: list[_Deployment] = []
        try:
            for item in deployments:
                if item.target.exists():
                    item.backup = item.target.parent / f".{item.target.name}.backup-{uuid4().hex}"
                    item.target.replace(item.backup)
                item.stage.replace(item.target)
                committed.append(item)
            self.config.record_installs(records)
        except Exception:
            for item in reversed(deployments):
                if item.target.exists() and (item in committed or item.backup is not None):
                    shutil.rmtree(item.target)
                if item.backup and item.backup.exists():
                    item.backup.replace(item.target)
            for item in deployments:
                if item.stage.exists():
                    shutil.rmtree(item.stage)
            raise
        for item in deployments:
            if item.backup and item.backup.exists():
                shutil.rmtree(item.backup)

    @contextmanager
    def _resolve_source(self, source: str, *, ref: str | None) -> Iterator[_ResolvedSource]:
        local = Path(source).expanduser()
        if local.exists():
            yield _ResolvedSource(root=local.resolve(), source=str(local.resolve()))
            return

        url, subdirectory = self._git_source(source)
        git = shutil.which("git")
        if git is None:
            raise SkillManagementError("git is required to install remote skill sources")
        with TemporaryDirectory(prefix="ouro-skill-") as temp:
            clone = Path(temp) / "source"
            command = [git, "clone", "--depth", "1"]
            if ref:
                command.extend(["--branch", ref])
            command.extend([url, str(clone)])
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                detail = completed.stderr.strip() or completed.stdout.strip()
                raise SkillManagementError(f"failed to clone skill source: {detail}")
            root = (clone / subdirectory).resolve() if subdirectory else clone.resolve()
            if not root.is_relative_to(clone.resolve()) or not root.exists():
                raise SkillManagementError(f"invalid skill subdirectory: {subdirectory}")
            yield _ResolvedSource(root=root, source=source)

    def _git_source(self, source: str) -> tuple[str, str | None]:
        value = source[4:] if source.startswith("git+") else source
        if value.startswith("github:"):
            value = f"https://github.com/{value.removeprefix('github:')}.git"
        parsed = urlsplit(value)
        if parsed.scheme not in {"file", "http", "https", "ssh", "git"} and not value.startswith(
            "git@"
        ):
            raise SkillManagementError(
                f"skill source is neither a local path nor a git URL: {source}"
            )
        fragment = unquote(parsed.fragment).strip("/") or None
        if parsed.fragment:
            value = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
        return value, fragment

    def _assert_safe_source_tree(self, source: Path) -> None:
        file_count = 0
        total_bytes = 0
        for path in source.rglob("*"):
            relative = path.relative_to(source)
            if any(part in _IGNORED_INSTALL_DIRECTORIES for part in relative.parts):
                continue
            if path.is_symlink():
                raise SkillManagementError(f"skill sources may not contain symbolic links: {path}")
            if path.is_file():
                file_count += 1
                total_bytes += path.stat().st_size
                if file_count > _MAX_INSTALL_FILES:
                    raise SkillManagementError(
                        f"skill source exceeds {_MAX_INSTALL_FILES} files: {source}"
                    )
                if total_bytes > _MAX_INSTALL_BYTES:
                    raise SkillManagementError(
                        f"skill source exceeds {_MAX_INSTALL_BYTES} bytes: {source}"
                    )

    def _unmanaged_native_target(self, name: str) -> Path | None:
        catalog = discover_skill_catalog(
            home=self.home,
            cwd=self.cwd,
            include_disabled=True,
        )
        metadata = catalog.by_name().get(name)
        if metadata is None:
            return None
        target = Path(metadata.location).parent.resolve()
        return target if self._is_managed_target(target) else None

    def _assert_managed_target(self, target: Path) -> None:
        if not self._is_managed_target(target):
            raise SkillManagementError(
                f"refusing to remove skill outside Ouro-managed roots: {target}"
            )

    def _is_managed_target(self, target: Path) -> bool:
        return any(target.parent == root for root in self._managed_roots())

    def _scope_for_target(self, target: Path) -> SkillInstallScope:
        return "workspace" if target.parent == self._install_root("workspace") else "user"

    def _managed_roots(self) -> tuple[Path, Path]:
        return self._install_root("user"), self._install_root("workspace")

    def _install_root(self, scope: SkillInstallScope) -> Path:
        if scope == "workspace":
            return (self.cwd / ".ouro" / "skills").resolve()
        return (self.home / "skills").resolve()
