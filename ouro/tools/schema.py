from __future__ import annotations

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
    errors = sorted(validator.iter_errors(arguments), key=_validation_error_key)
    if not errors:
        return None
    error = errors[0]
    location = ".".join(str(part) for part in error.absolute_path)
    subject = f"arguments.{location}" if location else "arguments"
    return f"{subject}: {error.message}"


def _check_schema(schema: dict[str, Any], *, tool_name: str, schema_name: str) -> None:
    try:
        validator_for(schema).check_schema(schema)
    except SchemaError as exc:
        raise ValueError(f"invalid {schema_name} for tool {tool_name}: {exc.message}") from exc


def _validation_error_key(error: ValidationError) -> tuple[str, str]:
    return (".".join(str(part) for part in error.absolute_path), error.message)
