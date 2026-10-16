from __future__ import annotations

from heapq import nsmallest
from typing import Any

from jsonschema import ValidationError  # type: ignore[import-untyped]
from jsonschema.exceptions import SchemaError  # type: ignore[import-untyped]
from jsonschema.validators import validator_for  # type: ignore[import-untyped]

from ouro.core.tools import ToolDefinition


def validate_tool_definition(definition: ToolDefinition) -> None:
    """Reject malformed JSON Schemas at the registration boundary."""

    _check_schema(
        definition.parameters_schema,
        tool_name=definition.name,
        schema_name="parameters_schema",
    )
    _check_schema(
        definition.result_schema,
        tool_name=definition.name,
        schema_name="result_schema",
    )


def tool_argument_error(
    definition: ToolDefinition,
    arguments: dict[str, Any],
) -> str | None:
    """Return a deterministic, model-readable validation error, if any."""

    validator_type = validator_for(definition.parameters_schema)
    validator = validator_type(definition.parameters_schema)
    errors = nsmallest(3, validator.iter_errors(arguments), key=_validation_error_key)
    if not errors:
        return None
    messages = []
    for error in errors:
        location = ".".join(str(part) for part in error.absolute_path)
        subject = f"arguments.{location}" if location else "arguments"
        messages.append(f"{subject}: {error.message}")
    properties = definition.parameters_schema.get("properties", {})
    if properties and any(
        not error.absolute_path and error.validator in {"required", "additionalProperties"}
        for error in errors
    ):
        messages.append(f"Declared parameter names: {', '.join(properties)}")
    return "; ".join(messages)


def _check_schema(schema: dict[str, Any], *, tool_name: str, schema_name: str) -> None:
    try:
        validator_for(schema).check_schema(schema)
    except SchemaError as exc:
        raise ValueError(f"invalid {schema_name} for tool {tool_name}: {exc.message}") from exc


def _validation_error_key(error: ValidationError) -> tuple[str, str]:
    return (".".join(str(part) for part in error.absolute_path), error.message)
