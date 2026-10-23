from __future__ import annotations

import copy
import json

import pytest

from ouro.plugins.configuration.schema import ConfigurationSchema, ConfigurationSchemaError


def test_normalization_materializes_defaults_without_overwriting_explicit_values() -> None:
    schema = ConfigurationSchema(
        {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean", "default": True},
                "count": {"type": "integer", "default": 10},
                "text": {"type": "string", "default": "default"},
                "nullable": {"type": ["integer", "null"], "default": 5},
                "nested": {
                    "type": "object",
                    "default": {},
                    "properties": {"limit": {"type": "integer", "default": 4}},
                    "required": ["limit"],
                },
                "absent": {
                    "type": "object",
                    "properties": {"value": {"default": "not-created"}},
                },
            },
        }
    )
    source = {"enabled": False, "count": 0, "text": "", "nullable": None}
    expected = {**source, "nested": {"limit": 4}}
    assert schema.normalize(source) == expected
    assert "nested" not in source
    assert schema.normalize(expected) == expected
    assert not schema.validate(expected)
    assert schema.document["properties"]["nested"]["default"] == {}


def test_defaults_follow_references_and_existing_items_but_do_not_select_branches() -> None:
    schema = ConfigurationSchema(
        {
            "type": "object",
            "$defs": {"item": {"type": "object", "properties": {"n": {"default": 7}}}},
            "properties": {
                "items": {"type": "array", "items": {"$ref": "#/$defs/item"}},
                "pair": {"type": "array", "prefixItems": [{"$ref": "#/$defs/item"}]},
                "branch": {"oneOf": [{"const": "a", "default": "a"}, {"const": "b"}]},
            },
            "allOf": [{"properties": {"hidden": {"default": 1}}}],
        }
    )
    assert schema.normalize({"items": [{}], "pair": [{}]}) == {
        "items": [{"n": 7}],
        "pair": [{"n": 7}],
    }


@pytest.mark.parametrize(
    ("field", "accepted", "rejected"),
    [
        ({"type": ["number", "null"], "minimum": 0}, None, -1),
        ({"enum": [False, 3, {"x": 1}]}, {"x": 1}, "3"),
        (
            {
                "type": "object",
                "patternProperties": {"^x_": {"type": "integer"}},
                "additionalProperties": False,
            },
            {"x_one": 1},
            {"other": 1},
        ),
        (
            {
                "type": "array",
                "prefixItems": [{"type": "string"}, {"type": "number"}],
                "items": False,
                "minItems": 2,
            },
            ["x", 0.5],
            ["x", 0.5, 3],
        ),
        (
            {
                "type": "array",
                "contains": {"type": "integer"},
                "minContains": 2,
                "maxContains": 2,
                "uniqueItems": True,
            },
            [1, 2, "x"],
            [1, 1],
        ),
        (
            {"oneOf": [{"type": "integer"}, {"type": "string", "minLength": 2}]},
            "ab",
            "a",
        ),
        (
            {"type": "object", "dependentRequired": {"a": ["b"]}},
            {"a": 1, "b": 2},
            {"a": 1},
        ),
        (
            {"type": "object", "if": {"required": ["a"]}, "then": {"required": ["b"]}},
            {"b": 1},
            {"a": 1},
        ),
        (
            {
                "type": "object",
                "allOf": [{"properties": {"a": {"type": "integer"}}}],
                "unevaluatedProperties": False,
            },
            {"a": 1},
            {"a": 1, "b": 2},
        ),
        ({"anyOf": [{"const": "x"}, {"type": "integer"}], "not": {"const": 0}}, 1, 0),
        (
            {
                "type": "object",
                "dependentSchemas": {"a": {"properties": {"b": {"const": 2}}, "required": ["b"]}},
            },
            {"a": True, "b": 2},
            {"a": True, "b": 3},
        ),
    ],
)
def test_full_schema_constraints_survive_the_configuration_boundary(field, accepted, rejected):
    schema = ConfigurationSchema({"type": "object", "properties": {"field": field}})
    assert not schema.validate({"field": accepted})
    assert schema.validate({"field": rejected})


def test_recursive_schema_validates_finite_instances_without_generating_missing_children():
    schema = ConfigurationSchema(
        {
            "type": "object",
            "$dynamicAnchor": "node",
            "properties": {"name": {"type": "string"}, "child": {"$dynamicRef": "#node"}},
            "required": ["name"],
        }
    )
    value = {"name": "root", "child": {"name": "leaf"}}
    assert schema.normalize(value) == value
    assert not schema.validate(value)
    assert schema.validate({"name": "root", "child": {"name": 3}})


def test_local_resource_ids_and_anchors_resolve_without_retrieval():
    schema = ConfigurationSchema(
        {
            "$id": "https://example.test/config",
            "type": "object",
            "$defs": {
                "count": {"$id": "count", "$anchor": "value", "type": "integer", "default": 9}
            },
            "properties": {"count": {"$ref": "count#value"}},
        }
    )
    assert schema.normalize({}) == {"count": 9}
    assert not schema.validate({"count": 9})


@pytest.mark.parametrize(
    "reference", ["https://example.test/schema", "file:///secret", "other.json", "#/$defs/missing"]
)
def test_unknown_references_are_rejected_during_static_discovery(reference):
    with pytest.raises(ConfigurationSchemaError, match="reference"):
        ConfigurationSchema({"type": "object", "properties": {"x": {"$ref": reference}}})


def test_schema_like_user_data_is_not_interpreted_as_a_reference():
    schema = ConfigurationSchema(
        {
            "type": "object",
            "properties": {"payload": {"default": {"$ref": "https://example.test/data"}}},
        }
    )
    assert schema.normalize({}) == {"payload": {"$ref": "https://example.test/data"}}


@pytest.mark.parametrize(
    "target",
    [
        "not a schema",
        {"type": 42},
        {"$ref": "https://invalid.example/schema"},
        {"$schema": "http://json-schema.org/draft-07/schema#"},
    ],
)
def test_references_into_other_document_locations_still_require_valid_schemas(target):
    with pytest.raises(ConfigurationSchemaError):
        ConfigurationSchema(
            {
                "type": "object",
                "custom": {"target": target},
                "properties": {"x": {"$ref": "#/custom/target"}},
            }
        )


def test_references_can_reach_self_contained_definitions_in_other_document_locations():
    schema = ConfigurationSchema(
        {
            "type": "object",
            "custom": {"target": {"type": "integer", "minimum": 0, "default": 5}},
            "properties": {"x": {"$ref": "#/custom/target"}},
        }
    )
    assert schema.normalize({}) == {"x": 5}
    assert not schema.validate({"x": 1})
    assert schema.validate({"x": -1})[0].code == "minimum"


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "array"},
        {"type": "object", "$schema": "http://json-schema.org/draft-07/schema#"},
        {"type": "object", "$vocabulary": {"https://example.test/required": True}},
        {"type": "object", "properties": {"x": {"type": "integer", "default": "secret"}}},
        {"type": "object", "properties": {"child": {"$ref": "#", "default": {}}}},
        {
            "type": "object",
            "$defs": {"x": {"default": 1}},
            "properties": {"x": {"$ref": "#/$defs/x", "default": 2}},
        },
        {
            "type": "object",
            "$defs": {"x": {"default": True}},
            "properties": {"x": {"$ref": "#/$defs/x", "default": 1}},
        },
        {
            "type": "object",
            "$defs": {"node": {"properties": {"x": {"default": 1}}}},
            "properties": {
                "optional": {"$ref": "#/$defs/node", "properties": {"x": {"default": 2}}}
            },
        },
    ],
)
def test_invalid_contracts_are_rejected_before_any_configuration_is_used(schema):
    with pytest.raises(ConfigurationSchemaError) as error:
        ConfigurationSchema(schema)
    assert "secret" not in str(error.value)


def test_errors_locate_missing_fields_and_union_children_without_echoing_input():
    schema = ConfigurationSchema(
        {
            "type": "object",
            "required": ["missing/a~b"],
            "properties": {"choice": {"oneOf": [{"type": "integer"}, {"const": False}]}},
        }
    )
    issues = schema.validate({"choice": "EXAMPLE_PRIVATE_VALUE"})
    assert any(issue.path == "/values/missing~1a~0b" for issue in issues)
    union = next(issue for issue in issues if issue.code == "oneOf")
    assert union.path == "/values/choice"
    assert union.children
    assert "EXAMPLE_PRIVATE_VALUE" not in json.dumps([issue.model_dump() for issue in issues])


@pytest.mark.parametrize("value", [{"x": float("nan")}, {1: "x"}, {"x": (1, 2)}, []])
def test_configuration_input_rejects_non_json_values_without_coercion(value):
    schema = ConfigurationSchema({"type": "object"})
    with pytest.raises(ValueError):
        schema.normalize(value)


def test_definition_and_returned_document_do_not_alias_callers():
    source = {"type": "object", "properties": {"x": {"type": "integer", "default": 1}}}
    expected = copy.deepcopy(source)
    schema = ConfigurationSchema(source)
    source["properties"]["x"]["default"] = 2
    schema.document["properties"]["x"]["default"] = 3
    assert schema.document == expected
    assert schema.normalize({}) == {"x": 1}
