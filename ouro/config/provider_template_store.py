"""Local provider template catalog file: materialization, sync, and migration.

The inline catalog in ``provider_templates`` stays the single source of truth.
This store materializes it into ``config/provider-templates.json`` and keeps
that file synchronized under the rules of
``dev/specs/provider-templates-local.md``: the file is a materialized copy plus
a user overlay, content is compared per template revision, and templates the
user modified or took over are never overwritten.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from ouro.config.endpoints import _SENSITIVE_PUBLIC_KEYS
from ouro.config.file import JsonConfigFile
from ouro.config.provider_templates import (
    ProviderTemplate,
)
from ouro.config.provider_templates import (
    provider_templates as inline_provider_templates,
)
from ouro.core.base import OuroModel
from ouro.core.errors import ConfigError

#: Current structure (schema) version of the local template file.
SCHEMA_VERSION = 1

#: Field-level migration steps keyed by the structure version they produce.
#: A step mutates one raw template entry in place and must not touch ``meta``.
_STRUCTURE_MIGRATIONS: dict[int, Callable[[dict[str, Any]], None]] = {}


class ProviderTemplateMeta(OuroModel):
    """Overlay metadata persisted under ``meta`` for one template entry."""

    origin: Literal["inline", "custom"]
    baseline_revision: int | None = None
    baseline_fingerprint: str | None = None
    deprecated: bool = False
    enabled: bool = True


class ProviderTemplateEntry(OuroModel):
    """One catalog template together with its overlay state."""

    template: ProviderTemplate
    origin: Literal["inline", "custom"]
    deprecated: bool = False
    enabled: bool = True


class ProviderTemplateCatalogSnapshot(OuroModel):
    entries: tuple[ProviderTemplateEntry, ...]
    notices: tuple[str, ...] = ()
    config_path: Path


def template_fingerprint(business: Mapping[str, Any]) -> str:
    """Hash the canonical form of one template's business fields.

    The basis is the raw business mapping (``meta`` excluded) canonicalized
    with sorted keys and compact separators. Raw mappings are the only basis
    that stays comparable across structure versions, which the pre-migration
    modification check depends on.
    """

    payload = json.dumps(business, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _business(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in raw.items() if key != "meta"}


def _validation_summary(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors()[:3]:
        location = ".".join(str(piece) for piece in error["loc"]) or "(root)"
        parts.append(f"{location}: {error['msg']}")
    return "; ".join(parts)


def _plaintext_credential_notice(
    template_id: str,
    business: Mapping[str, Any],
) -> str | None:
    stack: list[Any] = [business]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if (
                    isinstance(key, str)
                    and key.lower() in _SENSITIVE_PUBLIC_KEYS
                    and isinstance(value, str)
                    and value
                ):
                    return (
                        f"provider template '{template_id}' has a possible plaintext"
                        f" credential at '{key}'; templates reference credentials"
                        f" through credential_env only"
                    )
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)
    return None


class _LoadedTemplate:
    """Raw file entry plus its parsed state during one synchronization pass."""

    def __init__(
        self,
        raw: dict[str, Any],
        meta: ProviderTemplateMeta,
        template: ProviderTemplate | None,
    ) -> None:
        self.raw = raw
        self.meta = meta
        self.template = template
        fingerprint = meta.baseline_fingerprint
        self.modified = (
            meta.origin == "inline"
            and template is not None
            and fingerprint is not None
            and template_fingerprint(_business(raw)) != fingerprint
        )


def _normalize_meta(
    raw_meta: Any,
    template_id: str,
    inline_ids: frozenset[str],
    business: Mapping[str, Any],
) -> ProviderTemplateMeta:
    """Read ``meta`` tolerantly; missing or mistyped fields fall back to safe defaults."""

    origin = "inline" if template_id in inline_ids else "custom"
    baseline_revision: int | None = None
    baseline_fingerprint: str | None = None
    deprecated = False
    enabled = True
    if isinstance(raw_meta, dict):
        stored_origin = raw_meta.get("origin")
        if stored_origin in ("inline", "custom"):
            origin = stored_origin
        stored_revision = raw_meta.get("baseline_revision")
        if type(stored_revision) is int:
            baseline_revision = stored_revision
        stored_fingerprint = raw_meta.get("baseline_fingerprint")
        if isinstance(stored_fingerprint, str) and stored_fingerprint:
            baseline_fingerprint = stored_fingerprint
        deprecated = raw_meta.get("deprecated") is True
        stored_enabled = raw_meta.get("enabled")
        if isinstance(stored_enabled, bool):
            enabled = stored_enabled
    if origin == "custom":
        return ProviderTemplateMeta(
            origin="custom",
            enabled=enabled,
        )
    # Meta-less inline-origin entries count as unmodified at revision 1; today
    # that state is inert because the inline catalog itself starts at revision 1.
    return ProviderTemplateMeta(
        origin="inline",
        baseline_revision=baseline_revision if baseline_revision is not None else 1,
        baseline_fingerprint=baseline_fingerprint
        if baseline_fingerprint is not None
        else template_fingerprint(business),
        deprecated=deprecated,
        enabled=enabled,
    )


def _fresh_loaded(template: ProviderTemplate) -> _LoadedTemplate:
    business = template.model_dump(mode="json")
    meta = ProviderTemplateMeta(
        origin="inline",
        baseline_revision=template.revision,
        baseline_fingerprint=template_fingerprint(business),
    )
    raw = {**business, "meta": meta.model_dump(mode="json")}
    return _LoadedTemplate(raw, meta, template)


def _apply_inline_template(item: _LoadedTemplate, inline_template: ProviderTemplate) -> None:
    """Overwrite only the business fields; keep user-controlled meta fields."""

    business = inline_template.model_dump(mode="json")
    meta = ProviderTemplateMeta(
        origin="inline",
        baseline_revision=inline_template.revision,
        baseline_fingerprint=template_fingerprint(business),
        enabled=item.meta.enabled,
    )
    item.raw.clear()
    item.raw.update(business)
    item.raw["meta"] = meta.model_dump(mode="json")
    item.meta = meta
    item.template = inline_template
    item.modified = False


def _sync_meta(item: _LoadedTemplate) -> None:
    item.raw["meta"] = item.meta.model_dump(mode="json")


def _is_schema_version(value: Any) -> bool:
    return type(value) is int and value >= 0


class ProviderTemplateStore:
    """Persist and serve the local provider template catalog file."""

    def __init__(self, root: str | Path | None = None) -> None:
        self._file = JsonConfigFile(root, name="provider-templates.json")

    @property
    def config_path(self) -> Path:
        return self._file.path

    def catalog(self) -> ProviderTemplateCatalogSnapshot:
        with self._file.mutation():
            return self._synchronize()

    def get(self, template_id: str) -> ProviderTemplate:
        return self.get_entry(template_id).template

    def get_entry(self, template_id: str) -> ProviderTemplateEntry:
        snapshot = self.catalog()
        normalized = template_id.strip().casefold()
        for entry in snapshot.entries:
            if entry.template.template_id == normalized:
                return entry
        available = ", ".join(
            sorted(entry.template.template_id for entry in snapshot.entries)
        )
        raise ConfigError(f"provider template not found: {template_id}; available: {available}")

    def restore(self, template_id: str) -> ProviderTemplate:
        """Drop the local overlay for one template and re-apply inline content."""

        with self._file.mutation():
            self._synchronize()
            normalized = template_id.strip().casefold()
            inline = {t.template_id: t for t in inline_provider_templates()}
            template = inline.get(normalized)
            if template is None:
                available = ", ".join(sorted(inline))
                raise ConfigError(
                    f"provider template not found in the inline catalog: {template_id};"
                    f" available: {available}"
                )
            try:
                raw_root = self._file.read()
            except ConfigError as exc:
                raise ConfigError(
                    f"cannot restore until the provider template file is fixed: {exc}"
                ) from exc
            templates_raw = raw_root.get("templates")
            if not isinstance(templates_raw, dict):
                templates_raw = {}
                raw_root["templates"] = templates_raw
            templates_raw[normalized] = _fresh_loaded(template).raw
            self._write_root(raw_root, templates_raw)
            return template

    def _synchronize(self) -> ProviderTemplateCatalogSnapshot:
        notices: list[str] = []
        inline = {t.template_id: t for t in inline_provider_templates()}
        inline_ids = frozenset(inline)

        if not self._file.path.exists():
            return self._materialize(inline, notices)

        try:
            raw_root = self._file.read()
        except ConfigError as exc:
            return self._inline_fallback(
                inline,
                notices,
                f"provider template file failed to load; the file was kept and the"
                f" inline catalog is used until it is fixed in place: {exc}",
            )
        templates_raw = raw_root.get("templates", {})
        if not isinstance(templates_raw, dict):
            return self._inline_fallback(
                inline,
                notices,
                "provider template file root is invalid: 'templates' must be an object",
            )
        local_schema = raw_root.get("schema_version")
        if not _is_schema_version(local_schema):
            return self._inline_fallback(
                inline,
                notices,
                f"provider template file has an invalid schema_version: {local_schema!r}",
            )
        assert isinstance(local_schema, int)

        loaded = self._load_templates(templates_raw, inline_ids, notices)
        if local_schema > SCHEMA_VERSION:
            # Newer structure than this build understands: load read-only.
            return self._snapshot(loaded, notices, inline)
        if local_schema < SCHEMA_VERSION and not self._migrate_structure(
            raw_root, templates_raw, loaded, local_schema, notices
        ):
            return self._inline_fallback(
                inline,
                notices,
                "provider template structure migration failed; the file was left unchanged",
            )
        self._upgrade_content(raw_root, templates_raw, loaded, inline, notices)
        return self._snapshot(loaded, notices, inline)

    def _load_templates(
        self,
        templates_raw: Mapping[str, Any],
        inline_ids: frozenset[str],
        notices: list[str],
    ) -> dict[str, _LoadedTemplate]:
        loaded: dict[str, _LoadedTemplate] = {}
        for template_id, raw in templates_raw.items():
            if not isinstance(raw, dict):
                notices.append(
                    f"provider template '{template_id}' skipped: entry must be an object"
                )
                continue
            business = _business(raw)
            meta = _normalize_meta(raw.get("meta"), template_id, inline_ids, business)
            notice = _plaintext_credential_notice(template_id, business)
            if notice is not None:
                notices.append(notice)
            template = self._parse_template(raw, template_id, notices)
            loaded[template_id] = _LoadedTemplate(raw, meta, template)
        return loaded

    def _parse_template(
        self,
        raw: Mapping[str, Any],
        template_id: str,
        notices: list[str],
    ) -> ProviderTemplate | None:
        try:
            return ProviderTemplate.model_validate(_business(raw))
        except ValidationError as exc:
            notices.append(
                f"provider template '{template_id}' skipped; validation failed"
                f" ({_validation_summary(exc)})"
            )
            return None

    def _materialize(
        self,
        inline: Mapping[str, ProviderTemplate],
        notices: list[str],
    ) -> ProviderTemplateCatalogSnapshot:
        templates_raw: dict[str, Any] = {}
        loaded: dict[str, _LoadedTemplate] = {}
        for template_id, template in inline.items():
            item = _fresh_loaded(template)
            templates_raw[template_id] = item.raw
            loaded[template_id] = item
        self._write_root({}, templates_raw)
        notices.append(f"provider template file created at {self._file.path}")
        return self._snapshot(loaded, notices, inline)

    def _migrate_structure(
        self,
        raw_root: Mapping[str, Any],
        templates_raw: dict[str, Any],
        loaded: Mapping[str, _LoadedTemplate],
        local_schema: int,
        notices: list[str],
    ) -> bool:
        backup = self._backup_file()
        notices.append(
            f"provider template file migrated from structure version {local_schema}"
            f" to {SCHEMA_VERSION}"
            + (f"; original backed up at {backup}" if backup is not None else "")
        )
        try:
            version = local_schema
            while version < SCHEMA_VERSION:
                version += 1
                step = _STRUCTURE_MIGRATIONS.get(version)
                if step is None:
                    raise ConfigError(
                        f"no structure migration registered for provider template"
                        f" version {version}"
                    )
                for item in loaded.values():
                    step(item.raw)
            # ``item.modified`` was captured before the steps rewrote the
            # entries, so unmodified templates can safely re-baseline now.
            for template_id, item in loaded.items():
                if item.template is None:
                    continue
                item.template = self._parse_template(item.raw, template_id, notices)
                if item.template is not None and not item.modified:
                    item.meta.baseline_fingerprint = template_fingerprint(_business(item.raw))
                    _sync_meta(item)
        except ConfigError as exc:
            notices.append(f"structure migration failed: {exc}")
            return False
        self._write_root(raw_root, templates_raw)
        return True

    def _upgrade_content(
        self,
        raw_root: Mapping[str, Any],
        templates_raw: dict[str, Any],
        loaded: dict[str, _LoadedTemplate],
        inline: Mapping[str, ProviderTemplate],
        notices: list[str],
    ) -> None:
        changed = False
        for template_id, inline_template in inline.items():
            item = loaded.get(template_id)
            if item is None or item.template is None:
                if template_id not in templates_raw:
                    fresh = _fresh_loaded(inline_template)
                    templates_raw[template_id] = fresh.raw
                    loaded[template_id] = fresh
                    changed = True
                    notices.append(
                        f"provider template '{template_id}' added from the inline catalog"
                    )
                # A locally broken entry occupies the id; keep it untouched.
                continue
            if item.meta.origin == "custom":
                notices.append(
                    f"provider template '{template_id}' conflicts with the inline"
                    f" catalog; the user-defined template wins"
                )
                continue
            if item.meta.deprecated:
                item.meta.deprecated = False
                _sync_meta(item)
                changed = True
                notices.append(
                    f"provider template '{template_id}' is available again;"
                    f" deprecated flag cleared"
                )
            baseline = item.meta.baseline_revision
            if baseline is None or baseline > inline_template.revision:
                # Local content is newer than this build ships; never rewrite it.
                continue
            if baseline == inline_template.revision:
                continue
            if item.modified:
                item.meta.baseline_revision = inline_template.revision
                _sync_meta(item)
                changed = True
                notices.append(
                    f"provider template '{template_id}' keeps the user version;"
                    f" the inline catalog moved to revision {inline_template.revision}"
                    f" (drop local changes to adopt it)"
                )
            else:
                _apply_inline_template(item, inline_template)
                changed = True
                notices.append(
                    f"provider template '{template_id}' updated to inline revision"
                    f" {inline_template.revision}"
                )

        for template_id, item in loaded.items():
            if template_id in inline or item.template is None:
                continue
            if item.meta.origin != "inline" or item.meta.deprecated:
                continue
            item.meta.deprecated = True
            _sync_meta(item)
            changed = True
            notices.append(
                f"provider template '{template_id}' is no longer in the inline"
                f" catalog; marked deprecated"
            )
        if changed:
            self._write_root(raw_root, templates_raw)

    def _inline_fallback(
        self,
        inline: Mapping[str, ProviderTemplate],
        notices: list[str],
        reason: str,
    ) -> ProviderTemplateCatalogSnapshot:
        notices.append(reason)
        loaded = {
            template_id: _LoadedTemplate(
                raw=template.model_dump(mode="json"),
                meta=ProviderTemplateMeta(
                    origin="inline",
                    baseline_revision=template.revision,
                ),
                template=template,
            )
            for template_id, template in inline.items()
        }
        return self._snapshot(loaded, notices, inline)

    def _snapshot(
        self,
        loaded: Mapping[str, _LoadedTemplate],
        notices: list[str],
        inline: Mapping[str, ProviderTemplate] | None = None,
    ) -> ProviderTemplateCatalogSnapshot:
        # The inline catalog's curation order is part of the source of truth;
        # templates the user added follow it in stable alphabetical order.
        ordered = list(inline or {})
        ordered.extend(template_id for template_id in sorted(loaded) if template_id not in ordered)

        def _entry(template_id: str) -> ProviderTemplateEntry | None:
            item = loaded.get(template_id)
            if item is None or item.template is None:
                return None
            return ProviderTemplateEntry(
                template=item.template,
                origin=item.meta.origin,
                deprecated=item.meta.deprecated,
                enabled=item.meta.enabled,
            )

        entries = tuple(entry for entry in map(_entry, ordered) if entry is not None)
        return ProviderTemplateCatalogSnapshot(
            entries=entries,
            notices=tuple(notices),
            config_path=self._file.path,
        )

    def _write_root(
        self,
        preserved_root: Mapping[str, Any],
        templates_raw: Mapping[str, Any],
    ) -> None:
        root = {
            key: value
            for key, value in preserved_root.items()
            if key not in ("schema_version", "templates")
        }
        root["schema_version"] = SCHEMA_VERSION
        root["templates"] = dict(templates_raw)
        self._file.write(root)

    def _backup_file(self) -> Path | None:
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        backup = self._file.path.with_name(
            f"{self._file.path.stem}.backup-{stamp}.json"
        )
        try:
            shutil.copy2(self._file.path, backup)
        except OSError:
            return None
        return backup
