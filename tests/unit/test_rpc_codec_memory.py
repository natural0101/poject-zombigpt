"""The memory codec: round trips, strictness, and the split it must not undo.

``MemoryRecord`` puts numbers, booleans and references in ``data`` and ``refs``,
and the one string a human would read in ``label``. That is the whole reason a
memory store cannot smuggle game text past the boundary's quarantine: the
router scrubs ``label`` by name, and there is no other place for a sentence to
be. A codec that let a string through ``data`` would reopen exactly that
channel, so most of this file is about refusals rather than round trips.

Two of the assertions here look redundant and are not:

* ``bool`` is a subclass of ``int`` and ``True == 1``, so a decoder that turned
  ``True`` into ``1`` would produce a record that compares **equal** to the one
  encoded. Every data round trip therefore asserts the *type* of each value,
  not just the record's equality.
* ``1 == 1.0`` for the same reason, so an integer coerced to a float would also
  survive equality.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from dataclasses import fields
from typing import Any, cast

import pytest

from pz_agent_mcp.ports import MemoryRecord
from pz_agent_mcp.remote.codec import CodecError
from pz_agent_mcp.remote.codec.memory import (
    MemoryQuery,
    decode_memory_query,
    decode_memory_record,
    decode_memory_records,
    encode_memory_query,
    encode_memory_record,
    encode_memory_records,
)

#: Shaped like something a redactor would be sorry to have missed. It is put in
#: the value — or the key — position of a field that is then rejected, and
#: asserted absent from the exception.
SECRET = "sk-live-51H0-PZ-token-9f8e7d6c5b4a3210"

#: The install this is built for is Russian. A label is game-derived text, and
#: a kind or a data key can be minted from it.
CYRILLIC_LABEL = "Склад возле Малдро — четыре банки супа"
CYRILLIC_KIND = "место_склад"
CYRILLIC_KEY = "ключ-склад-4"

RECORD_REQUIRED = ("kind", "key", "refs", "data")
RECORD_OPTIONAL = ("label",)

QUERY_REQUIRED = ("kinds", "limit")

#: One of each thing ``data`` is allowed to hold, so a round trip covers the
#: whole declared type rather than the easy half of it.
FULL_DATA: dict[str, float | int | bool | None] = {
    "count": 4,
    "distance_m": 12.5,
    "negative": -3,
    "zero": 0,
    "armed": True,
    "stale": False,
    "last_seen_ms": None,
}


def full_record(**overrides: Any) -> MemoryRecord:
    """A record with every field, including every optional one, populated."""
    values: dict[str, Any] = {
        "kind": "container",
        "key": "loot.shelf.7",
        "label": "shelf by the loading bay",
        "refs": ("ref:container:7f1c", "ref:room:2b"),
        "data": dict(FULL_DATA),
    }
    values.update(overrides)
    return MemoryRecord(**values)


def bare_record() -> MemoryRecord:
    """A record with every defaulted and optional field left out."""
    return MemoryRecord(kind="fact", key="k-0")


def smuggling_record(value: object) -> MemoryRecord:
    """A record whose ``data`` holds something its type says it cannot.

    The annotation is not enforced at runtime — that is precisely why the codec
    checks it — so the cast is how the test reproduces what a misbehaving
    memory store would hand the encoder.
    """
    return full_record(data=cast("Mapping[str, Any]", {"note": value}))


def assert_round_trip(record: MemoryRecord) -> None:
    """Encode, decode, and insist on the record — values, types and all."""
    decoded = decode_memory_record(encode_memory_record(record))
    assert decoded == record
    # `refs` is a tuple on a frozen record; a list would still compare unequal,
    # but saying so here makes the failure legible.
    assert isinstance(decoded.refs, tuple)
    assert set(decoded.data) == set(record.data)
    for name, value in record.data.items():
        # `True == 1` and `1 == 1.0`, so equality above does not pin the type.
        assert type(decoded.data[name]) is type(value)


# -- the parametrisation itself is pinned ---------------------------------


def test_every_record_field_is_classified_as_required_or_optional() -> None:
    """A field added to the record must land in one of the lists above.

    Otherwise the strictness tests keep passing while quietly not covering it.
    """
    declared = {f.name for f in fields(MemoryRecord)}
    assert declared == set(RECORD_REQUIRED) | set(RECORD_OPTIONAL)
    # "Optional" means the field itself is declared ``| None`` — matched by the
    # end of the annotation, not by containing "None". ``data`` is
    # ``Mapping[str, float | int | bool | None]``: the None in it is a
    # permitted *value*, not permission to omit the field.
    optional_by_type = {f.name for f in fields(MemoryRecord) if str(f.type).endswith("| None")}
    assert optional_by_type == set(RECORD_OPTIONAL)


def test_every_query_field_is_required() -> None:
    assert {f.name for f in fields(MemoryQuery)} == set(QUERY_REQUIRED)


def test_encode_emits_exactly_the_record_fields() -> None:
    """No field dropped on the way out, and none invented."""
    assert set(encode_memory_record(full_record())) == {f.name for f in fields(MemoryRecord)}
    assert set(encode_memory_record(bare_record())) == {f.name for f in fields(MemoryRecord)}


def test_encode_query_emits_exactly_the_query_fields() -> None:
    assert set(encode_memory_query(MemoryQuery(kinds=("a",), limit=5))) == set(QUERY_REQUIRED)


# -- round trips ----------------------------------------------------------


def test_record_round_trip_with_every_field_present() -> None:
    assert_round_trip(full_record())


def test_record_round_trip_with_every_optional_field_absent() -> None:
    assert_round_trip(bare_record())


def test_record_round_trip_without_a_label() -> None:
    assert_round_trip(full_record(label=None))


def test_record_decodes_when_the_label_key_is_omitted_entirely() -> None:
    """A peer that omits a null rather than sending it says the same thing."""
    record = full_record(label=None)
    payload = encode_memory_record(record)
    del payload["label"]
    assert decode_memory_record(payload) == record


def test_empty_refs_round_trips() -> None:
    record = full_record(refs=())
    assert_round_trip(record)
    assert decode_memory_record(encode_memory_record(record)).refs == ()


def test_refs_decode_to_a_tuple_and_keep_their_order() -> None:
    refs = ("ref:a:1", "ref:b:2", "ref:c:3")
    decoded = decode_memory_record(encode_memory_record(full_record(refs=refs)))
    assert decoded.refs == refs
    assert isinstance(decoded.refs, tuple)
    # Through real JSON, where the array arrives as a list.
    from_json = decode_memory_record(json.loads(json.dumps(encode_memory_record(full_record()))))
    assert isinstance(from_json.refs, tuple)


def test_empty_data_round_trips() -> None:
    assert_round_trip(full_record(data={}))


@pytest.mark.parametrize(
    "value",
    [4, 0, -3, 12.5, -0.5, 0.0, True, False, None],
)
def test_each_permitted_data_value_round_trips_with_its_type(
    value: float | int | bool | None,
) -> None:
    record = full_record(data={"v": value})
    decoded = decode_memory_record(encode_memory_record(record))
    assert decoded == record
    assert type(decoded.data["v"]) is type(value)


def test_none_is_a_permitted_data_value_and_is_not_a_missing_key() -> None:
    """``None`` is in the declared type: "known to be absent", not "absent"."""
    record = full_record(data={"last_seen_ms": None})
    decoded = decode_memory_record(encode_memory_record(record))
    assert "last_seen_ms" in decoded.data
    assert decoded.data["last_seen_ms"] is None


def test_a_bool_in_data_stays_a_bool_and_does_not_become_one() -> None:
    """``True == 1``, so record equality alone would accept the coercion.

    This is the field where that bites: ``armed`` and ``stale`` are the kind of
    thing a memory record carries, and a boundary reporting ``1`` where the core
    said ``True`` has changed what the record says.
    """
    record = full_record(data={"armed": True, "stale": False, "count": 1, "empty": 0})
    encoded = encode_memory_record(record)
    assert encoded["data"]["armed"] is True
    assert encoded["data"]["stale"] is False
    decoded = decode_memory_record(encoded)
    assert decoded.data["armed"] is True
    assert decoded.data["stale"] is False
    assert type(decoded.data["count"]) is int
    assert type(decoded.data["empty"]) is int
    # And it survives an actual serialisation, where `true` and `1` are
    # different tokens.
    through_json = decode_memory_record(json.loads(json.dumps(encoded)))
    assert through_json.data["armed"] is True
    assert type(through_json.data["count"]) is int


def test_an_int_in_data_stays_an_int_and_a_float_stays_a_float() -> None:
    """``1 == 1.0`` as well, so this needs the same treatment as the boolean."""
    decoded = decode_memory_record(
        encode_memory_record(full_record(data={"count": 4, "distance_m": 4.0}))
    )
    assert type(decoded.data["count"]) is int
    assert type(decoded.data["distance_m"]) is float


def test_decoding_copies_data_rather_than_holding_the_payload() -> None:
    """A record is frozen; a payload the caller still owns is not."""
    payload = encode_memory_record(full_record())
    decoded = decode_memory_record(payload)
    payload["data"]["count"] = 999
    assert decoded.data["count"] == 4


# -- the query and the list body ------------------------------------------


@pytest.mark.parametrize("kinds", [(), ("container",), ("container", "route", "hazard")])
@pytest.mark.parametrize("limit", [0, 1, 20, 50])
def test_query_round_trip(kinds: tuple[str, ...], limit: int) -> None:
    query = MemoryQuery(kinds=kinds, limit=limit)
    decoded = decode_memory_query(encode_memory_query(query))
    assert decoded == query
    assert isinstance(decoded.kinds, tuple)


def test_records_body_round_trip() -> None:
    records = (full_record(), bare_record(), full_record(key="loot.shelf.8", refs=()))
    decoded = decode_memory_records(encode_memory_records(records))
    assert decoded == records
    assert isinstance(decoded, tuple)


def test_an_empty_result_round_trips_and_is_not_a_missing_key() -> None:
    """ "Nothing matched" and "unreadable" must not have the same spelling."""
    body = encode_memory_records(())
    assert body == {"records": []}
    assert decode_memory_records(body) == ()


def test_a_missing_records_key_is_refused() -> None:
    with pytest.raises(CodecError) as excinfo:
        decode_memory_records({})
    assert "records" in str(excinfo.value)


@pytest.mark.parametrize("body", [{"records": {}}, {"records": "none"}, {"records": 0}])
def test_a_records_key_that_is_not_an_array_is_refused(body: dict[str, Any]) -> None:
    with pytest.raises(CodecError):
        decode_memory_records(body)


@pytest.mark.parametrize("item", [None, 3, "record", ["kind"]])
def test_a_records_entry_that_is_not_an_object_is_refused(item: Any) -> None:
    with pytest.raises(CodecError):
        decode_memory_records({"records": [encode_memory_record(full_record()), item]})


def test_a_records_entry_that_is_a_bad_record_is_refused() -> None:
    broken = encode_memory_record(full_record())
    del broken["kind"]
    with pytest.raises(CodecError):
        decode_memory_records({"records": [encode_memory_record(bare_record()), broken]})


# -- it has to survive a process boundary ---------------------------------


def test_encoded_record_is_json_serialisable() -> None:
    payload = encode_memory_record(full_record())
    assert json.loads(json.dumps(payload)) == payload


def test_encoded_bare_record_is_json_serialisable() -> None:
    payload = encode_memory_record(bare_record())
    assert json.loads(json.dumps(payload)) == payload


def test_encoded_records_body_is_json_serialisable() -> None:
    payload = encode_memory_records((full_record(), bare_record()))
    assert json.loads(json.dumps(payload)) == payload


def test_encoded_query_is_json_serialisable() -> None:
    payload = encode_memory_query(MemoryQuery(kinds=("container", "route"), limit=20))
    assert json.loads(json.dumps(payload)) == payload


def test_record_survives_an_actual_json_round_trip() -> None:
    record = full_record()
    assert decode_memory_record(json.loads(json.dumps(encode_memory_record(record)))) == record


def test_records_body_survives_an_actual_json_round_trip() -> None:
    records = (full_record(), bare_record())
    body = json.loads(json.dumps(encode_memory_records(records)))
    assert decode_memory_records(body) == records


def test_query_survives_an_actual_json_round_trip() -> None:
    query = MemoryQuery(kinds=("container",), limit=20)
    assert decode_memory_query(json.loads(json.dumps(encode_memory_query(query)))) == query


# -- Cyrillic --------------------------------------------------------------


def test_cyrillic_text_survives_the_round_trip() -> None:
    record = full_record(
        kind=CYRILLIC_KIND,
        key=CYRILLIC_KEY,
        label=CYRILLIC_LABEL,
        refs=("ref:контейнер:7f1c",),
        data={"количество": 4, "заряжено": True},
    )
    payload = encode_memory_record(record)
    assert payload["label"] == CYRILLIC_LABEL
    decoded = decode_memory_record(json.loads(json.dumps(payload)))
    assert decoded == record
    assert decoded.data["заряжено"] is True


def test_cyrillic_survives_ascii_escaped_json() -> None:
    """An encoder elsewhere in the stack may set ``ensure_ascii``; escaping is
    not allowed to change the strings."""
    record = full_record(kind=CYRILLIC_KIND, label=CYRILLIC_LABEL)
    text = json.dumps(encode_memory_record(record), ensure_ascii=True)
    assert decode_memory_record(json.loads(text)) == record


def test_cyrillic_kinds_survive_a_query_round_trip() -> None:
    query = MemoryQuery(kinds=(CYRILLIC_KIND, "место_маршрут"), limit=7)
    assert decode_memory_query(json.loads(json.dumps(encode_memory_query(query)))) == query


# -- strictness: nothing is defaulted -------------------------------------


@pytest.mark.parametrize("field_name", RECORD_REQUIRED)
def test_record_missing_required_field_is_refused(field_name: str) -> None:
    payload = encode_memory_record(full_record())
    del payload[field_name]
    with pytest.raises(CodecError) as excinfo:
        decode_memory_record(payload)
    assert field_name in str(excinfo.value)


@pytest.mark.parametrize("field_name", RECORD_REQUIRED)
def test_record_required_field_explicitly_null_is_refused(field_name: str) -> None:
    """``null`` is not "absent, so use the dataclass default" either."""
    payload = encode_memory_record(full_record())
    payload[field_name] = None
    with pytest.raises(CodecError):
        decode_memory_record(payload)


def test_the_defaulted_fields_are_still_required() -> None:
    """``refs`` and ``data`` have dataclass defaults; the wire has none.

    A decoder that fell back to ``()`` and ``{}`` would turn a renamed key into
    a record that had quietly forgotten its references and its numbers, and
    nothing downstream could tell that from a record that never had any.
    """
    for field_name in ("refs", "data"):
        payload = encode_memory_record(full_record())
        del payload[field_name]
        with pytest.raises(CodecError):
            decode_memory_record(payload)


@pytest.mark.parametrize("field_name", QUERY_REQUIRED)
def test_query_missing_required_field_is_refused(field_name: str) -> None:
    payload = encode_memory_query(MemoryQuery(kinds=("container",), limit=20))
    del payload[field_name]
    with pytest.raises(CodecError) as excinfo:
        decode_memory_query(payload)
    assert field_name in str(excinfo.value)


def test_an_empty_payload_is_refused() -> None:
    with pytest.raises(CodecError):
        decode_memory_record({})
    with pytest.raises(CodecError):
        decode_memory_query({})


# -- strictness: types are not coerced ------------------------------------


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("kind", 7),
        ("kind", True),
        ("kind", ["container"]),
        ("key", 7),
        ("key", 1.5),
        ("key", {"key": "k"}),
        ("label", 42),
        ("label", True),
        ("label", ["shelf"]),
        ("refs", "ref:a:1"),
        ("refs", {"0": "ref:a:1"}),
        ("refs", 3),
        ("refs", ["ref:a:1", 3]),
        ("refs", [None]),
        ("refs", [["ref:a:1"]]),
        ("data", []),
        ("data", "count=4"),
        ("data", 4),
        ("data", True),
    ],
)
def test_record_type_confusion_is_refused(field_name: str, value: Any) -> None:
    payload = encode_memory_record(full_record())
    payload[field_name] = value
    with pytest.raises(CodecError):
        decode_memory_record(payload)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("kinds", "container"),
        ("kinds", 3),
        ("kinds", None),
        ("kinds", ["container", 3]),
        ("kinds", [True]),
        ("limit", "20"),
        ("limit", 20.0),
        ("limit", True),
        ("limit", None),
        ("limit", ["20"]),
    ],
)
def test_query_type_confusion_is_refused(field_name: str, value: Any) -> None:
    payload = encode_memory_query(MemoryQuery(kinds=("container",), limit=20))
    payload[field_name] = value
    with pytest.raises(CodecError):
        decode_memory_query(payload)


# -- the split: `data` is the numeric half and stays that way -------------


@pytest.mark.parametrize(
    "value",
    [
        "a survivor left a note by the door",
        "",
        "42",
        SECRET,
    ],
)
def test_a_string_in_data_is_refused_on_decode(value: str) -> None:
    """The smuggling channel the record's shape exists to close.

    ``label`` is what the router scrubs and marks as untrusted content. A
    sentence arriving under ``data`` is text that never passed that check, so
    the payload is refused rather than carried.
    """
    payload = encode_memory_record(full_record())
    payload["data"] = {"note": value}
    with pytest.raises(CodecError):
        decode_memory_record(payload)


@pytest.mark.parametrize("value", ["a survivor left a note by the door", "", SECRET])
def test_a_string_in_data_is_refused_on_encode(value: str) -> None:
    """Refused on the side that holds the record, not only on the peer.

    A store that puts text in a numeric field is a bug in this process; letting
    it out and having the other end refuse it would report the link as broken.
    """
    with pytest.raises(CodecError):
        encode_memory_record(smuggling_record(value))


@pytest.mark.parametrize(
    "value",
    [
        {"nested": 1},
        {},
        ["a", "b"],
        [],
        [1, 2],
        {"deep": {"deeper": "text"}},
    ],
)
def test_a_container_in_data_is_refused_on_decode(value: Any) -> None:
    """Nothing nested: a container is where a string hides one level down."""
    payload = encode_memory_record(full_record())
    payload["data"] = {"blob": value}
    with pytest.raises(CodecError):
        decode_memory_record(payload)


@pytest.mark.parametrize("value", [{"nested": 1}, ["a"], (1, 2), {1, 2}])
def test_a_container_in_data_is_refused_on_encode(value: Any) -> None:
    with pytest.raises(CodecError):
        encode_memory_record(smuggling_record(value))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_number_in_data_is_refused(value: float) -> None:
    """``json.dumps`` writes these as bare ``NaN``/``Infinity``.

    That is not JSON, and nothing obliges the reader on the other end of the
    pipe to accept it. Refused on both sides rather than emitted.
    """
    assert not math.isfinite(value)
    with pytest.raises(CodecError):
        encode_memory_record(full_record(data={"distance_m": value}))
    payload = encode_memory_record(full_record())
    payload["data"] = {"distance_m": value}
    with pytest.raises(CodecError):
        decode_memory_record(payload)


def test_a_non_string_data_key_is_refused() -> None:
    """``json.dumps`` turns an ``int`` key into a string, silently.

    A record with a ``1`` key would arrive as one with a ``"1"`` key — a
    different record that both halves would agree was fine.
    """
    with pytest.raises(CodecError):
        encode_memory_record(full_record(data=cast("Mapping[str, Any]", {1: 4})))
    payload = encode_memory_record(full_record())
    payload["data"] = {2: 4}
    with pytest.raises(CodecError):
        decode_memory_record(payload)


@pytest.mark.parametrize("refs", ["ref:room:2b", "", b"ref:room:2b"])
def test_a_string_given_as_refs_is_refused_on_encode(refs: Any) -> None:
    """The container's type is checked, not just its elements.

    A ``str`` is a sequence of one-character strings, so an element-only check
    accepts ``refs="ref:room:2b"`` and emits eleven single-character
    references. That payload is well-formed: the peer decodes it without
    complaint, and both halves agree on a record the core never held. It is the
    same silent divergence the non-string ``data`` key check exists to stop, and
    it has to fail on the side holding the record.
    """
    with pytest.raises(CodecError):
        encode_memory_record(full_record(refs=cast("Any", refs)))


@pytest.mark.parametrize("kinds", ["container", "", {"container": 1}, 3, None])
def test_a_non_array_given_as_query_kinds_is_refused_on_encode(kinds: Any) -> None:
    """``kinds`` has the same hole as ``refs`` and closes it the same way.

    A ``Mapping`` is refused alongside the string: it iterates as its keys,
    which are strings too, so an element-only check would turn a dict into an
    array of its keys.
    """
    with pytest.raises(CodecError):
        encode_memory_query(MemoryQuery(kinds=cast("Any", kinds), limit=20))


@pytest.mark.parametrize("refs", [{"0": "ref:a:1"}, 3, None, {"ref:a:1"}])
def test_a_non_array_given_as_refs_is_refused_on_encode(refs: Any) -> None:
    with pytest.raises(CodecError):
        encode_memory_record(full_record(refs=cast("Any", refs)))


@pytest.mark.parametrize("data", [[("count", 4)], "count=4", 4, None, ("count",)])
def test_a_non_object_given_as_data_is_refused_on_encode(data: Any) -> None:
    """And it is a ``CodecError``, not whatever ``.items()`` raises.

    The boundary turns a ``CodecError`` into ``MALFORMED``; an ``AttributeError``
    escaping the codec is an internal error on a payload the codec is supposed
    to have judged.
    """
    with pytest.raises(CodecError):
        encode_memory_record(full_record(data=cast("Any", data)))


def test_the_label_is_still_free_text() -> None:
    """The refusals above are about ``data``, not about ``label``.

    ``label`` is the field the split exists to concentrate text *into*; a codec
    that rejected a sentence there would push stores into using ``data``.
    """
    record = full_record(label="a survivor left a note by the door: 'go north'")
    assert_round_trip(record)


# -- what a refusal is allowed to say -------------------------------------


def _messages(exc: BaseException) -> str:
    """Everything a traceback would print for *exc*, including its causes."""
    parts: list[str] = []
    current: BaseException | None = exc
    while current is not None:
        parts.append(str(current))
        parts.append(repr(current))
        current = current.__cause__ or current.__context__
    return "\n".join(parts)


def _record_payload_with_secret(field_name: str, value: Any) -> dict[str, Any]:
    payload = encode_memory_record(full_record(label=SECRET))
    payload[field_name] = value
    return payload


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        # The secret in the value position of the field being rejected.
        ("kind", [SECRET]),
        ("key", {"k": SECRET}),
        ("refs", [SECRET, 3]),
        ("refs", SECRET),
        ("data", {"note": SECRET}),
        ("data", {"blob": [SECRET]}),
        ("data", SECRET),
        # The secret in the *key* position, which is just as much payload.
        ("data", {SECRET: "text"}),
        ("data", {SECRET: {"nested": 1}}),
        # And the secret merely sitting in the record while another field fails.
        ("kind", 7),
        ("data", 4),
    ],
)
def test_no_refusal_repeats_the_value_it_rejected(field_name: str, value: Any) -> None:
    """These strings reach a traceback and a bug report before any redactor.

    The message names the field and the reason; the payload's own content —
    game text, a path, a token — never appears in it.
    """
    with pytest.raises(CodecError) as excinfo:
        decode_memory_record(_record_payload_with_secret(field_name, value))
    assert SECRET not in _messages(excinfo.value)
    assert field_name in str(excinfo.value)


def test_no_refusal_from_the_encoder_repeats_the_value_either() -> None:
    with pytest.raises(CodecError) as excinfo:
        encode_memory_record(smuggling_record(SECRET))
    assert SECRET not in _messages(excinfo.value)
    with pytest.raises(CodecError) as excinfo:
        encode_memory_record(full_record(data=cast("Mapping[str, Any]", {SECRET: ["x"]})))
    assert SECRET not in _messages(excinfo.value)


def test_no_encoder_refusal_for_a_bad_container_repeats_the_value_either() -> None:
    """The container refusals are as close to the payload as the value ones."""
    builds: tuple[Callable[[], object], ...] = (
        lambda: encode_memory_record(full_record(refs=cast("Any", SECRET))),
        lambda: encode_memory_record(full_record(data=cast("Any", SECRET))),
        lambda: encode_memory_query(MemoryQuery(kinds=cast("Any", SECRET), limit=20)),
        lambda: encode_memory_record(full_record(refs=cast("Any", {SECRET: 1}))),
    )
    for build in builds:
        with pytest.raises(CodecError) as excinfo:
            build()
        assert SECRET not in _messages(excinfo.value)


def test_no_query_refusal_repeats_the_value_it_rejected() -> None:
    payload = encode_memory_query(MemoryQuery(kinds=(SECRET,), limit=20))
    payload["limit"] = SECRET
    with pytest.raises(CodecError) as excinfo:
        decode_memory_query(payload)
    assert SECRET not in _messages(excinfo.value)
    assert "limit" in str(excinfo.value)


def test_no_records_refusal_repeats_the_value_it_rejected() -> None:
    with pytest.raises(CodecError) as excinfo:
        decode_memory_records({"records": [{"kind": "c", "key": "k", "data": {"n": SECRET}}]})
    assert SECRET not in _messages(excinfo.value)
