from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from pydantic import JsonValue


@dataclass(frozen=True)
class ConfigurationField:
    key: str
    title: str
    description: str
    required: bool
    kind: Literal["string", "integer", "number", "select", "json"]
    schema: dict[str, JsonValue]
    options: tuple[JsonValue, ...] = ()

    @property
    def nullable(self) -> bool:
        types = self.schema.get("type")
        return isinstance(types, list) and "null" in types


def configuration_fields(document: dict[str, JsonValue]) -> tuple[ConfigurationField, ...]:
    """Project editor hints; the application remains the only Schema validator."""

    properties = document.get("properties", {})
    if not isinstance(properties, dict):
        return ()
    required = document.get("required", [])
    fields = []
    for key, definition in properties.items():
        schema = _resolve(definition, document)
        kind: Literal["string", "integer", "number", "select", "json"] = "json"
        options: tuple[JsonValue, ...] = ()
        # A combined constraint can always be edited as JSON without guessing its branches.
        if not any(
            name in schema
            for name in ("$ref", "$dynamicRef", "allOf", "anyOf", "oneOf", "if", "not")
        ):
            enum = schema.get("enum")
            types = schema.get("type")
            if isinstance(enum, list):
                kind, options = "select", tuple(enum)
            elif "const" in schema:
                kind, options = "select", (schema["const"],)
            else:
                types = types if isinstance(types, list) else [types]
                concrete = [value for value in types if value != "null"]
                if concrete == ["boolean"]:
                    kind, options = (
                        "select",
                        (False, True, None) if "null" in types else (False, True),
                    )
                elif concrete == ["string"]:
                    kind = "string"
                elif concrete == ["integer"]:
                    kind = "integer"
                elif concrete == ["number"]:
                    kind = "number"
        fields.append(
            ConfigurationField(
                key=key,
                title=str(schema.get("title", key)),
                description=str(schema.get("description", "")),
                required=isinstance(required, list) and key in required,
                kind=kind,
                schema=schema,
                options=options,
            )
        )
    return tuple(fields)


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def schema_placeholder(schema: JsonValue, document: dict[str, JsonValue]) -> str:
    return json_text(_example(schema, document, depth=0))


def _resolve(
    schema: JsonValue, document: dict[str, JsonValue], seen: frozenset[str] = frozenset()
) -> dict[str, JsonValue]:
    if not isinstance(schema, dict):
        return {}
    reference = schema.get("$ref")
    if not isinstance(reference, str) or not reference.startswith("#/") or reference in seen:
        return schema
    if set(schema) - {"$ref", "title", "description", "examples", "default", "deprecated"}:
        return schema
    target: JsonValue = document
    for part in reference[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(target, dict) or part not in target:
            return schema
        target = target[part]
    resolved = _resolve(target, document, seen | {reference})
    return {**resolved, **{key: value for key, value in schema.items() if key != "$ref"}}


def _example(schema: JsonValue, document: dict[str, JsonValue], *, depth: int) -> JsonValue:
    schema = _resolve(schema, document)
    examples = schema.get("examples")
    if isinstance(examples, list) and examples:
        return examples[0]
    if "default" in schema:
        return schema["default"]
    if "const" in schema:
        return schema["const"]
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]
    if depth >= 3:
        return None
    for keyword in ("oneOf", "anyOf", "allOf"):
        branches = schema.get(keyword)
        if isinstance(branches, list) and branches:
            return _example(branches[0], document, depth=depth + 1)
    types = schema.get("type")
    kind = types[0] if isinstance(types, list) and types else types
    if kind == "object" or "properties" in schema:
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            return {
                key: _example(value, document, depth=depth + 1)
                for key, value in list(properties.items())[:8]
            }
    if kind == "array":
        prefix = schema.get("prefixItems")
        if isinstance(prefix, list):
            return [_example(item, document, depth=depth + 1) for item in prefix[:8]]
        return [_example(schema.get("items", {}), document, depth=depth + 1)]
    if kind == "boolean":
        return False
    if kind in ("integer", "number"):
        bound = schema.get("minimum", 0)
        return bound if isinstance(bound, int | float) else 0
    if kind == "string":
        return "example"
    return None
