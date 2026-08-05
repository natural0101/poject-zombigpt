"""Input validation against the same schema object the tool publishes.

The alternative — a published schema plus a hand-written check — is two
statements of one contract, and the first divergence between them is a tool that
advertises a bound it does not enforce. So the declaration in :mod:`.catalog` is
the only statement, and this module is its interpreter.

It understands the subset of JSON Schema the tool inputs are written in: types,
``enum``, ``required``, ``properties`` with ``additionalProperties: false``,
numeric and length bounds, ``pattern``, ``items``, and ``default``. Anything
outside that subset appearing in a schema is a programming error and is reported
as one, rather than being silently treated as satisfied.

Three bounds apply regardless of what a schema says, because they protect the
process rather than the contract: :data:`MAX_DEPTH`, :data:`MAX_ARRAY_ITEMS` and
:data:`MAX_STRING_CHARS`. A schema may be stricter; it can never be looser.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

from pz_agent_core.protocol import JsonDict, ReasonCode

from .envelope import ToolFailure

__all__ = [
    "MAX_ARRAY_ITEMS",
    "MAX_DEPTH",
    "MAX_STRING_CHARS",
    "SchemaError",
    "validate_arguments",
]

#: Tool inputs are flat or one object deep; this leaves room and no more.
MAX_DEPTH: Final = 4

#: Hard ceilings, applied even where the schema forgot to state one.
MAX_ARRAY_ITEMS: Final = 32
MAX_STRING_CHARS: Final = 512

_KNOWN_KEYWORDS: Final[frozenset[str]] = frozenset(
    {
        "additionalProperties",
        "default",
        "description",
        "enum",
        "exclusiveMinimum",
        "items",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "pattern",
        "properties",
        "required",
        "title",
        "type",
    }
)

_TYPE_CHECKS: Final[dict[str, tuple[type, ...]]] = {
    "object": (Mapping,),
    "array": (list, tuple),
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
}


class SchemaError(ValueError):
    """A tool schema uses something this validator does not implement.

    Distinct from a rejected argument on purpose: this one is our bug, and
    reporting it as ``INVALID_ARGUMENT`` would blame the caller for it.
    """


def validate_arguments(schema: Mapping[str, Any], payload: Mapping[str, Any] | None) -> JsonDict:
    """Check *payload* against *schema* and return it with defaults applied.

    Raises:
        ToolFailure: ``INVALID_ARGUMENT`` for anything the schema refuses.
        SchemaError: when *schema* itself uses an unimplemented keyword.
    """
    value = _object(schema, {} if payload is None else payload, path="arguments", depth=0)
    return value


def _fail(path: str, detail: str) -> ToolFailure:
    return ToolFailure(ReasonCode.INVALID_ARGUMENT, f"{path} {detail}")


def _check_keywords(schema: Mapping[str, Any], path: str) -> None:
    unknown = sorted(set(schema) - _KNOWN_KEYWORDS)
    if unknown:
        raise SchemaError(f"{path}: schema uses unimplemented keyword(s) {unknown}")


def _validate(schema: Mapping[str, Any], value: Any, *, path: str, depth: int) -> Any:
    if depth > MAX_DEPTH:
        raise _fail(path, f"nests deeper than {MAX_DEPTH} levels")
    _check_keywords(schema, path)
    declared = schema.get("type")
    if not isinstance(declared, str):
        raise SchemaError(f"{path}: every schema must declare a single type")
    if declared == "object":
        return _object(schema, value, path=path, depth=depth)
    if declared == "array":
        return _array(schema, value, path=path, depth=depth)
    _check_type(declared, value, path=path)
    if declared == "string":
        return _string(schema, value, path=path)
    _enum(schema, value, path=path)
    return _number(schema, value, path=path)


def _check_type(declared: str, value: Any, *, path: str) -> None:
    expected = _TYPE_CHECKS.get(declared)
    if expected is None:
        raise SchemaError(f"{path}: unimplemented type {declared!r}")
    # ``bool`` is an ``int`` subclass in Python and never means one on the wire:
    # accepting ``true`` where a count is required would turn a type error into
    # a silent 1.
    if declared in {"number", "integer"} and isinstance(value, bool):
        raise _fail(path, f"must be a {declared}, got boolean")
    if not isinstance(value, expected):
        raise _fail(path, f"must be a {declared}, got {type(value).__name__}")


def _enum(schema: Mapping[str, Any], value: Any, *, path: str) -> None:
    allowed = schema.get("enum")
    if allowed is None:
        return
    if not isinstance(allowed, Sequence) or isinstance(allowed, str):
        raise SchemaError(f"{path}: 'enum' must be a list")
    if value not in allowed:
        raise _fail(path, f"must be one of {sorted(map(str, allowed))}, got {value!r}")


def _string(schema: Mapping[str, Any], value: str, *, path: str) -> str:
    _enum(schema, value, path=path)
    ceiling = min(int(schema.get("maxLength", MAX_STRING_CHARS)), MAX_STRING_CHARS)
    if len(value) > ceiling:
        raise _fail(path, f"must be at most {ceiling} characters, got {len(value)}")
    floor = int(schema.get("minLength", 0))
    if len(value) < floor:
        raise _fail(path, f"must be at least {floor} characters, got {len(value)}")
    pattern = schema.get("pattern")
    if pattern is not None and not re.match(str(pattern), value):
        raise _fail(path, "does not match the required format")
    return value


def _number(schema: Mapping[str, Any], value: Any, *, path: str) -> Any:
    minimum = schema.get("minimum")
    if minimum is not None and value < minimum:
        raise _fail(path, f"must be at least {minimum}, got {value}")
    exclusive = schema.get("exclusiveMinimum")
    if exclusive is not None and value <= exclusive:
        raise _fail(path, f"must be greater than {exclusive}, got {value}")
    maximum = schema.get("maximum")
    if maximum is not None and value > maximum:
        raise _fail(path, f"must be at most {maximum}, got {value}")
    return value


def _object(schema: Mapping[str, Any], value: Any, *, path: str, depth: int) -> JsonDict:
    _check_keywords(schema, path)
    _check_type("object", value, path=path)
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        raise SchemaError(f"{path}: 'properties' must be an object")
    if schema.get("additionalProperties") is not False:
        # Every tool input is closed, and closed by *saying so*: a schema that
        # merely omits the keyword would let a caller attach a field the tool
        # does not read and believe it was honoured.
        raise SchemaError(f"{path}: tool inputs must set additionalProperties to false")
    unknown = sorted(set(value) - set(properties))
    if unknown:
        raise _fail(path, f"has unsupported field(s) {unknown}")
    required = schema.get("required", ())
    if not isinstance(required, Sequence) or isinstance(required, str):
        raise SchemaError(f"{path}: 'required' must be a list")
    missing = sorted(name for name in required if name not in value)
    if missing:
        raise _fail(path, f"is missing required field(s) {missing}")

    out: JsonDict = {}
    for name, sub_schema in properties.items():
        if not isinstance(sub_schema, Mapping):
            raise SchemaError(f"{path}.{name}: schema must be an object")
        if name in value:
            out[name] = _validate(sub_schema, value[name], path=f"{path}.{name}", depth=depth + 1)
        elif "default" in sub_schema:
            out[name] = sub_schema["default"]
    return out


def _array(schema: Mapping[str, Any], value: Any, *, path: str, depth: int) -> list[Any]:
    _check_type("array", value, path=path)
    ceiling = min(int(schema.get("maxItems", MAX_ARRAY_ITEMS)), MAX_ARRAY_ITEMS)
    if len(value) > ceiling:
        raise _fail(path, f"must hold at most {ceiling} items, got {len(value)}")
    floor = int(schema.get("minItems", 0))
    if len(value) < floor:
        raise _fail(path, f"must hold at least {floor} items, got {len(value)}")
    items = schema.get("items")
    if not isinstance(items, Mapping):
        raise SchemaError(f"{path}: an array schema must describe its items")
    return [
        _validate(items, element, path=f"{path}[{index}]", depth=depth + 1)
        for index, element in enumerate(value)
    ]
