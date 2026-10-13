from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from pydantic import ConfigDict
from referencing import Registry
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012, Schema, SchemaResource

from ouro.core.base import OuroModel
from ouro.plugins.configuration_values import copy_configuration_values, copy_json

_DIALECT = "https://json-schema.org/draft/2020-12/schema"
_VOCABULARIES = frozenset(Draft202012Validator.META_SCHEMA["$vocabulary"])
_MISSING = object()


class ConfigurationSchemaError(ValueError):
    """A configuration definition cannot be used safely or deterministically."""


class ConfigurationIssue(OuroModel):
    model_config = ConfigDict(frozen=True)

    path: str
    code: str
    message: str
    children: tuple[ConfigurationIssue, ...] = ()


def configuration_pointer(parts: Iterable[str | int]) -> str:
    return "/" + "/".join(str(part).replace("~", "~0").replace("/", "~1") for part in parts)


class ConfigurationSchema:
    """Self-contained JSON Schema and its deterministic configuration defaults.

    The referencing resolver stays inside this library adapter. No registry
    retrieval callback is installed, including when validating nested schemas.
    """

    def __init__(self, document: dict[str, Any]) -> None:
        try:
            self._document = copy_configuration_values(document)
            Draft202012Validator.check_schema(self._document)
        except ValueError, SchemaError:
            raise ConfigurationSchemaError("Invalid plugin configuration schema") from None
        if self._document.get("type") != "object":
            raise ConfigurationSchemaError("Plugin configuration schema requires an object root")
        resource = DRAFT202012.create_resource(self._document)
        self._registry = Registry().with_resource(resource.id() or "", resource).crawl()
        self._resolver = self._registry.resolver_with_root(resource)
        self._validator = Draft202012Validator(self._document, registry=self._registry)
        nodes = list(self._resources(resource, self._resolver))
        known = {id(node) for node, _ in nodes}
        # JSON Pointers may reach schemas outside the usual applicator/$defs locations.
        # Follow those targets too, without interpreting unreferenced example payloads.
        for node, resolver in nodes:
            for target, scope in self._check_node(node, resolver):
                if id(target) in known:
                    continue
                try:
                    Draft202012Validator.check_schema(target)
                except SchemaError:
                    raise ConfigurationSchemaError(
                        "Invalid configuration reference target"
                    ) from None
                for child, child_scope in self._resources(
                    DRAFT202012.create_resource(target), scope
                ):
                    if id(child) not in known:
                        known.add(id(child))
                        nodes.append((child, child_scope))
        for node, resolver in nodes:
            # Optional containers must be checked even when absent from the initial values.
            self._normalize({}, node, resolver, frozenset())
            default = self._default(node, resolver)
            if default is not _MISSING:
                normalized = self._normalize(
                    copy_json(default), node, resolver, frozenset({id(node)})
                )
                validator = self._validator.evolve(schema=node, _resolver=resolver)
                if next(validator.iter_errors(normalized), None) is not None:
                    raise ConfigurationSchemaError("Invalid plugin configuration default")

    @property
    def document(self) -> dict[str, Any]:
        return copy_configuration_values(self._document)

    def normalize(self, values: object) -> dict[str, object]:
        copied = copy_configuration_values(values)
        try:
            result = self._normalize(copied, self._document, self._resolver, frozenset())
        except RecursionError:
            raise ConfigurationSchemaError(
                "Configuration defaults cannot expand recursively"
            ) from None
        assert isinstance(result, dict)
        return result

    def validate(self, values: object) -> tuple[ConfigurationIssue, ...]:
        copied = copy_configuration_values(values)
        try:
            issues = [
                issue for error in self._validator.iter_errors(copied) for issue in _issues(error)
            ]
        except RecursionError, Unresolvable:
            raise ConfigurationSchemaError(
                "Configuration schema cannot resolve this instance"
            ) from None
        unique = {(item.path, item.code): item for item in issues}
        return tuple(unique[key] for key in sorted(unique))

    def _resources(self, resource: SchemaResource, resolver: Any) -> Iterator[tuple[Schema, Any]]:
        yield resource.contents, resolver
        for child in resource.subresources():
            yield from self._resources(child, resolver.in_subresource(child))

    def _check_node(self, node: Schema, resolver: Any) -> Iterator[tuple[Schema, Any]]:
        if isinstance(node, bool):
            return
        if node.get("$schema", _DIALECT).rstrip("#") != _DIALECT:
            raise ConfigurationSchemaError("Unsupported plugin configuration schema dialect")
        if any(
            required and uri not in _VOCABULARIES
            for uri, required in node.get("$vocabulary", {}).items()
        ):
            raise ConfigurationSchemaError("Unsupported required configuration vocabulary")
        for keyword in ("$ref", "$dynamicRef"):
            if keyword in node:
                try:
                    resolved = resolver.lookup(node[keyword])
                except Unresolvable:
                    raise ConfigurationSchemaError(
                        "Configuration schema reference must resolve within its document"
                    ) from None
                yield resolved.contents, resolved.resolver

    def _views(
        self, node: Schema, resolver: Any, seen: frozenset[int] = frozenset()
    ) -> Iterator[tuple[dict[str, Any], Any]]:
        if isinstance(node, bool) or id(node) in seen:
            return
        yield cast(dict[str, Any], node), resolver
        for keyword in ("$ref", "$dynamicRef"):
            if keyword in node:
                resolved = resolver.lookup(node[keyword])
                yield from self._views(resolved.contents, resolved.resolver, seen | {id(node)})

    def _default(self, node: Schema, resolver: Any) -> object:
        defaults = [view["default"] for view, _ in self._views(node, resolver) if "default" in view]
        if defaults and any(not _same_default(item, defaults[0]) for item in defaults[1:]):
            raise ConfigurationSchemaError("Conflicting plugin configuration defaults")
        return defaults[0] if defaults else _MISSING

    def _normalize(
        self, value: object, node: Schema, resolver: Any, expanding: frozenset[int]
    ) -> object:
        views = tuple(self._views(node, resolver))
        if isinstance(value, dict):
            properties: dict[str, list[tuple[Schema, Any]]] = {}
            for view, scope in views:
                for name, child in view.get("properties", {}).items():
                    child_scope = scope.in_subresource(DRAFT202012.create_resource(child))
                    properties.setdefault(name, []).append((child, child_scope))
            for name, definitions in properties.items():
                child_expanding = expanding
                if name not in value:
                    defaults = [
                        (self._default(child, scope), child) for child, scope in definitions
                    ]
                    defaults = [
                        (default, child) for default, child in defaults if default is not _MISSING
                    ]
                    if not defaults:
                        continue
                    if any(
                        not _same_default(default, defaults[0][0]) for default, _ in defaults[1:]
                    ):
                        raise ConfigurationSchemaError("Conflicting plugin configuration defaults")
                    ids = {id(child) for _, child in defaults}
                    if ids & expanding:
                        raise ConfigurationSchemaError(
                            "Configuration defaults cannot expand recursively"
                        )
                    child_expanding |= ids
                    value[name] = copy_json(defaults[0][0])
                for child, scope in definitions:
                    value[name] = self._normalize(value[name], child, scope, child_expanding)
        elif isinstance(value, list):
            for view, scope in views:
                prefix = view.get("prefixItems", [])
                for index, item in enumerate(value):
                    child = prefix[index] if index < len(prefix) else view.get("items", True)
                    child_scope = scope.in_subresource(DRAFT202012.create_resource(child))
                    value[index] = self._normalize(item, child, child_scope, expanding)
        return value


def _same_default(left: object, right: object) -> bool:
    # Python equates True with 1; configuration defaults retain their JSON types.
    return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)


def _issues(error: ValidationError) -> tuple[ConfigurationIssue, ...]:
    path = ("values", *error.absolute_path)
    code = str(error.validator or "schema")
    if (
        code == "required"
        and isinstance(error.instance, dict)
        and isinstance(error.validator_value, list)
    ):
        return tuple(
            ConfigurationIssue(
                path=configuration_pointer((*path, key)),
                code=code,
                message="A required field is missing.",
            )
            for key in error.validator_value
            if key not in error.instance
        )
    return (
        ConfigurationIssue(
            path=configuration_pointer(path),
            code=code,
            message=_MESSAGES.get(code, "Value does not satisfy the configuration constraint."),
            children=tuple(issue for child in error.context for issue in _issues(child)),
        ),
    )


_MESSAGES = {
    "type": "Value has an incorrect JSON type.",
    "enum": "Select one of the declared values.",
    "const": "Value must match the declared constant.",
    "minimum": "Value is below the allowed minimum.",
    "maximum": "Value exceeds the allowed maximum.",
    "minLength": "Text is shorter than allowed.",
    "maxLength": "Text is longer than allowed.",
    "pattern": "Text does not match the required pattern.",
    "additionalProperties": "Object contains undeclared fields.",
    "unevaluatedProperties": "Object contains fields outside the accepted structure.",
    "oneOf": "Value must satisfy exactly one alternative.",
    "anyOf": "Value must satisfy at least one alternative.",
    "uniqueItems": "Array items must be unique.",
}
