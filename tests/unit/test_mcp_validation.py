"""The schema interpreter: what it refuses, and the bounds it applies anyway."""

from __future__ import annotations

from typing import Any

import pytest

from pz_agent_core.protocol import ReasonCode
from pz_agent_mcp.envelope import ToolFailure
from pz_agent_mcp.validation import (
    MAX_ARRAY_ITEMS,
    MAX_DEPTH,
    MAX_STRING_CHARS,
    SchemaError,
    validate_arguments,
)


def object_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required or [],
    }


def test_missing_payload_becomes_the_defaults() -> None:
    schema = object_schema({"limit": {"type": "integer", "default": 20}})

    assert validate_arguments(schema, None) == {"limit": 20}


def test_absent_optional_without_default_is_absent_not_none() -> None:
    # A caller distinguishes "not filtered" from "filtered by null"; emitting a
    # key with None would erase that distinction for every downstream handler.
    schema = object_schema({"category": {"type": "string"}})

    assert validate_arguments(schema, {}) == {}


def test_unknown_field_is_invalid_argument() -> None:
    schema = object_schema({"limit": {"type": "integer"}})

    with pytest.raises(ToolFailure) as caught:
        validate_arguments(schema, {"limit": 1, "lua": "os.execute('rm')"})

    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT
    assert "lua" in caught.value.message
    assert caught.value.retryable is False


def test_missing_required_field_names_it() -> None:
    schema = object_schema({"item_ref": {"type": "string"}}, ["item_ref"])

    with pytest.raises(ToolFailure) as caught:
        validate_arguments(schema, {})

    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT
    assert "item_ref" in caught.value.message


def test_boolean_is_not_an_integer() -> None:
    schema = object_schema({"pages": {"type": "integer"}})

    with pytest.raises(ToolFailure) as caught:
        validate_arguments(schema, {"pages": True})

    assert "boolean" in caught.value.message


def test_numeric_bounds_are_enforced_at_both_ends() -> None:
    schema = object_schema({"radius": {"type": "number", "minimum": 1, "maximum": 5}})

    with pytest.raises(ToolFailure) as low:
        validate_arguments(schema, {"radius": 0.5})
    with pytest.raises(ToolFailure) as high:
        validate_arguments(schema, {"radius": 5.1})

    assert "at least 1" in low.value.message
    assert "at most 5" in high.value.message


def test_exclusive_minimum_refuses_the_boundary() -> None:
    schema = object_schema({"game_seconds": {"type": "number", "exclusiveMinimum": 0}})

    with pytest.raises(ToolFailure) as caught:
        validate_arguments(schema, {"game_seconds": 0})

    assert "greater than 0" in caught.value.message


def test_enum_rejects_an_unlisted_value() -> None:
    schema = object_schema({"mode": {"type": "string", "enum": ["ASSISTED", "AUTONOMOUS"]}})

    with pytest.raises(ToolFailure) as caught:
        validate_arguments(schema, {"mode": "EXPERIMENTAL_INPUT"})

    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT


def test_pattern_rejects_a_value_of_the_wrong_shape() -> None:
    schema = object_schema({"item_ref": {"type": "string", "pattern": r"^item:.+$"}})

    with pytest.raises(ToolFailure) as caught:
        validate_arguments(schema, {"item_ref": "container:abc"})

    assert "format" in caught.value.message


@pytest.mark.parametrize("value", ["item:abc\n", "item:abc\r\n", "item:abc\n\n"])
def test_pattern_rejects_a_trailing_newline_the_anchor_alone_would_admit(value: str) -> None:
    # '$' also matches *before* a trailing newline, so re.match would accept a
    # ref, a filter token or a uuid with a line break glued to the end — a
    # control character in a field the rest of the boundary treats as safe.
    schema = object_schema({"item_ref": {"type": "string", "pattern": r"^item:[a-z]+$"}})

    with pytest.raises(ToolFailure) as caught:
        validate_arguments(schema, {"item_ref": value})

    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT
    assert "format" in caught.value.message


def test_string_length_ceiling_applies_even_when_the_schema_forgot_one() -> None:
    schema = object_schema({"goal": {"type": "string"}})

    with pytest.raises(ToolFailure) as caught:
        validate_arguments(schema, {"goal": "x" * (MAX_STRING_CHARS + 1)})

    assert f"at most {MAX_STRING_CHARS}" in caught.value.message


def test_a_schema_may_be_stricter_than_the_ceiling_but_not_looser() -> None:
    strict = object_schema({"goal": {"type": "string", "maxLength": 4}})
    loose = object_schema({"goal": {"type": "string", "maxLength": MAX_STRING_CHARS * 10}})

    with pytest.raises(ToolFailure):
        validate_arguments(strict, {"goal": "abcde"})
    with pytest.raises(ToolFailure) as caught:
        validate_arguments(loose, {"goal": "x" * (MAX_STRING_CHARS + 1)})

    assert f"at most {MAX_STRING_CHARS}" in caught.value.message


def test_array_item_ceiling_applies_even_when_the_schema_forgot_one() -> None:
    schema = object_schema({"types": {"type": "array", "items": {"type": "string"}}})

    with pytest.raises(ToolFailure) as caught:
        validate_arguments(schema, {"types": ["a"] * (MAX_ARRAY_ITEMS + 1)})

    assert f"at most {MAX_ARRAY_ITEMS}" in caught.value.message


def test_array_elements_are_validated_and_the_path_names_the_index() -> None:
    schema = object_schema({"types": {"type": "array", "items": {"type": "string"}}})

    with pytest.raises(ToolFailure) as caught:
        validate_arguments(schema, {"types": ["door", 7]})

    assert "types[1]" in caught.value.message


def test_nested_object_is_validated_and_defaults_are_applied_inside_it() -> None:
    schema = object_schema(
        {
            "limits": object_schema(
                {"max_steps": {"type": "integer", "minimum": 1, "default": 8}},
            )
        }
    )

    assert validate_arguments(schema, {"limits": {}}) == {"limits": {"max_steps": 8}}

    with pytest.raises(ToolFailure) as caught:
        validate_arguments(schema, {"limits": {"max_steps": 0}})

    assert "arguments.limits.max_steps" in caught.value.message


def test_depth_beyond_the_ceiling_is_refused() -> None:
    schema: dict[str, Any] = {"type": "integer"}
    for _ in range(MAX_DEPTH + 2):
        schema = object_schema({"n": schema})
    payload: Any = 1
    for _ in range(MAX_DEPTH + 2):
        payload = {"n": payload}

    with pytest.raises(ToolFailure) as caught:
        validate_arguments(schema, payload)

    assert f"deeper than {MAX_DEPTH}" in caught.value.message


def test_an_open_object_schema_is_our_bug_not_the_callers() -> None:
    schema = {"type": "object", "properties": {}, "required": []}

    with pytest.raises(SchemaError, match="additionalProperties"):
        validate_arguments(schema, {})


def test_an_unimplemented_keyword_is_reported_rather_than_ignored() -> None:
    # Silently treating 'oneOf' as satisfied would advertise a constraint the
    # boundary does not enforce, which is worse than not offering it.
    schema = object_schema({"x": {"type": "string", "oneOf": [{"const": "a"}]}})

    with pytest.raises(SchemaError, match="oneOf"):
        validate_arguments(schema, {"x": "a"})


def test_an_array_schema_without_items_is_our_bug() -> None:
    schema = object_schema({"types": {"type": "array"}})

    with pytest.raises(SchemaError, match="describe its items"):
        validate_arguments(schema, {"types": []})


def test_a_schema_without_a_type_is_our_bug() -> None:
    schema = object_schema({"x": {"minimum": 1}})

    with pytest.raises(SchemaError, match="single type"):
        validate_arguments(schema, {"x": 2})


def test_nan_does_not_pass_a_bound_it_cannot_satisfy() -> None:
    # NaN compares false against every bound, so `value < minimum` and
    # `value > maximum` are both false and an unchecked implementation lets it
    # through a range it plainly does not sit inside. A caller that then
    # filtered on it — `distance <= radius` — would see an empty world.
    schema = object_schema({"radius": {"type": "number", "minimum": 0.0, "maximum": 30.0}})

    with pytest.raises(ToolFailure) as caught:
        validate_arguments(schema, {"radius": float("nan")})

    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT
    assert "finite" in caught.value.message


def test_infinity_is_refused_even_where_no_maximum_is_declared() -> None:
    schema = object_schema({"weight": {"type": "number", "minimum": 0.0}})

    with pytest.raises(ToolFailure) as caught:
        validate_arguments(schema, {"weight": float("inf")})

    assert "finite" in caught.value.message


def test_a_finite_number_at_the_bound_is_still_accepted() -> None:
    schema = object_schema({"radius": {"type": "number", "minimum": 0.0, "maximum": 30.0}})

    assert validate_arguments(schema, {"radius": 30.0}) == {"radius": 30.0}
