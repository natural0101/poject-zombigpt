"""Seeded fuzz over the RPC envelope decoders: refused or decoded, never a crash.

:mod:`tests.unit.test_rpc_wire` pins hand-picked frames — the caps, the pickle
refusal, the never-quote rule — and :mod:`tests.unit.test_rpc_adversarial`
replays hand-built abuse against a live server. Neither walks the
*neighbourhood* of a valid frame systematically, and the neighbourhood is where
decoder crashes live: one byte cut from a multibyte character, one field's type
swapped, one bracket too many. This file walks it with ``random.Random`` seeded
from fixed literals — never the global ``random`` module, never the clock — so
every run replays the same corpus byte for byte and a failing case names the
seed and index that rebuild it.

The contract under test is the one :func:`decode_request` and
:func:`decode_response` document: any bytes either decode to the typed
dataclass or raise :class:`RpcError` carrying a code from the envelope's own
closed set. Nothing else may escape — no ``KeyError`` from a missing field, no
``UnicodeDecodeError`` from a mangled encoding, no ``ValueError`` from a number
the interpreter refuses, no ``RecursionError`` from nesting — because the
caller is a server loop that answers refusals, and any other exception is a
denial of service one frame wide.

Writing this file found two real escapes — a ``RecursionError`` from deep
nesting and a ``ValueError`` from an absurd integer literal, both under the
byte cap. Both are now fixed in :mod:`pz_agent_core.rpc.wire` (a depth scan
before parsing and a widened ``except``), and each has its own regression test
at the bottom of this file. The broad corpus steers under both boundaries so
that its property covers the general shape while those two tests pin the exact
edges.

The corpus is sized to finish in a few seconds; the repository-wide 300-second
pytest cap is the enforcement, not an assertion here — asserting wall time
would make the suite flaky on a loaded runner without making it faster.
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable
from typing import Final

import pytest

from pz_agent_core.protocol import JsonDict
from pz_agent_core.rpc.wire import (
    MAX_ID_CHARS,
    MAX_METHOD_CHARS,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    ErrorCode,
    RpcError,
    RpcRequest,
    RpcResponse,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
)
from pz_agent_core.version import RPC_PROTOCOL_VERSION

#: One fixed literal seed per property. A failure message quotes the seed and
#: the case index, which together rebuild the exact bytes on any machine.
REFUSAL_SEED: Final = 0xDEC0DE
ROUND_TRIP_SEED: Final = 0x0FF1CE
RESIDUE_SEED: Final = 0xFACADE

#: The only codes the decode layer itself may report. A method-level code
#: (``CORE_REFUSED``, ``TIMEOUT``…) surfacing from ``decode_*`` would mean the
#: envelope invented an answer no method gave.
_DECODE_CODES: Final = frozenset(
    {ErrorCode.MALFORMED, ErrorCode.TOO_LARGE, ErrorCode.PROTOCOL_MISMATCH}
)

#: Word material for the generators. Each alphabet is a single script on
#: purpose: the project ships to a Russian-language install, so Cyrillic and
#: astral characters must survive the wire, and pure-script words keep the
#: multibyte content deliberate rather than accidental.
_ALPHABETS: Final = (
    "abcdefghijklmnopqrstuvwxyz0123456789._-",
    "выживание",
    "зомби",
    "🧟🌲🔧",
)

#: Protocol spellings a compatible peer may use: same major, any minor.
_MINOR_VERSIONS: Final = (RPC_PROTOCOL_VERSION, "1.1", "1.7", "1.999")

#: Deeper than anything honest, at the decoder's own ``MAX_NESTING_DEPTH``. The
#: broad corpus stays at or under this so its property is about the general
#: shape; the exact edge — where the decoder now refuses rather than crashing —
#: is pinned by :func:`test_deep_nesting_is_refused_not_a_recursion_error`.
_NESTING_BOUND: Final = 64


def _word(rng: random.Random, cap: int) -> str:
    alphabet = rng.choice(_ALPHABETS)
    return "".join(rng.choice(alphabet) for _ in range(rng.randint(1, cap)))


def _value(rng: random.Random, depth: int) -> object:
    """A JSON value that round-trips exactly: json floats travel by repr."""
    if depth <= 0 or rng.random() < 0.35:
        roll = rng.randrange(6)
        if roll == 0:
            return None
        if roll == 1:
            return rng.random() < 0.5
        if roll == 2:
            return rng.randrange(-(10**9), 10**9)
        if roll == 3:
            return rng.random()
        return _word(rng, 12)
    if rng.random() < 0.5:
        return [_value(rng, depth - 1) for _ in range(rng.randrange(4))]
    return _object(rng, depth - 1)


def _object(rng: random.Random, depth: int) -> JsonDict:
    return {_word(rng, 8): _value(rng, depth) for _ in range(rng.randrange(4))}


def _frame(document: dict[str, object]) -> bytes:
    """*document* as wire bytes, the same way ``wire._dumps`` writes them."""
    return json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _request_document(rng: random.Random) -> dict[str, object]:
    # The format marker is a string literal, as in test_rpc_adversarial: a peer
    # that sniffed one real frame has it, and routing these documents through
    # the module's own constant would tie the fuzz to the code it ambushes.
    return {
        "format": "pz-agent-core-rpc/1",
        "protocol": "1.0",
        "id": _word(rng, MAX_ID_CHARS),
        "method": _word(rng, MAX_METHOD_CHARS),
        "params": _object(rng, 2),
    }


def _response_document(rng: random.Random) -> dict[str, object]:
    return {
        "format": "pz-agent-core-rpc/1",
        "protocol": "1.0",
        "id": _word(rng, MAX_ID_CHARS),
        "ok": True,
        "result": _object(rng, 2),
    }


def _raw_request(id_bytes: bytes, method_bytes: bytes = b"m") -> bytes:
    """A request frame with attacker-chosen raw bytes spliced into a string.

    Built by concatenation, not ``json.dumps``, because the interesting inputs
    — encoded surrogates, overlong sequences, bare continuation bytes — cannot
    pass through a Python ``str`` on the way in.
    """
    return (
        b'{"format":"pz-agent-core-rpc/1","protocol":"1.0","id":"'
        + id_bytes
        + b'","method":"'
        + method_bytes
        + b'","params":{}}'
    )


def _mutated_bytes(rng: random.Random, frame: bytes) -> bytes:
    """*frame* with one to three byte-level edits: flip, insert, cut, stutter."""
    data = bytearray(frame)
    for _ in range(rng.randint(1, 3)):
        if not data:
            break
        operation = rng.randrange(4)
        position = rng.randrange(len(data))
        if operation == 0:
            data[position] ^= 1 << rng.randrange(8)
        elif operation == 1:
            data.insert(position, rng.randrange(256))
        elif operation == 2:
            del data[position : position + rng.randint(1, 8)]
        else:
            chunk = data[position : position + rng.randint(1, 16)]
            data[position:position] = chunk
    return bytes(data)


def _refused_or_decoded(
    decode: Callable[[bytes], RpcRequest | RpcResponse], data: bytes, label: str
) -> None:
    """The property every hostile case is held to.

    A decode may succeed — many mutations land back on valid frames, and that
    is not a defect — or it may raise :class:`RpcError` with an envelope-level
    code. Anything else is the crash this file exists to catch, and the failure
    message carries the label (seed + case) and the opening bytes so the case
    can be rebuilt and minimised.
    """
    try:
        decode(data)
    except RpcError as refusal:
        assert refusal.code in _DECODE_CODES, (
            f"{label}: refusal carried {refusal.code!r}, not an envelope-level code"
        )
        return
    except Exception as escape:
        pytest.fail(
            f"{label}: {type(escape).__name__} escaped the decoder; "
            f"frame starts {data[:64]!r} ({len(data)} bytes)"
        )


class TestEveryMutationIsRefusedOrDecodedNeverACrash:
    """Property (a): hostile bytes produce ``RpcError`` or a value, nothing else.

    Mutation check — the real defect class behind each test, the bug that
    would make it fail:

    * truncation: a raw ``JSONDecodeError``/``UnicodeDecodeError`` escaping an
      unwrapped parse, or an ``IndexError`` from slicing by parse position;
    * type swaps: ``KeyError`` from ``document["field"]``, ``AttributeError``
      or ``TypeError`` from calling string methods or ``len`` on a non-string;
    * absurd lengths: a cap checked after parsing (the allocation already
      made), or an interpreter-level ``ValueError`` on a pathological literal;
    * unicode garbage: ``UnicodeDecodeError`` escaping ``bytes.decode``, or
      surrogate content crashing a re-encode;
    * deep nesting: ``RecursionError`` escaping ``json.loads``;
    * byte soup: any of the above reached through an edit no one hand-picked.
    """

    def test_truncation_at_every_byte_boundary(self) -> None:
        """Exhaustive prefixes: every cut point of every corpus frame.

        Cheaper than guessing which boundaries are interesting, and it provably
        includes the ones that are — mid-token, mid-string, mid-multibyte
        character (the deterministic Cyrillic/astral frame guarantees those
        exist regardless of what the seed drew).
        """
        rng = random.Random(REFUSAL_SEED)
        frames = [_frame(_request_document(rng)) for _ in range(3)]
        frames += [_frame(_response_document(rng)) for _ in range(3)]
        frames.append(
            _frame(
                {
                    "format": "pz-agent-core-rpc/1",
                    "protocol": "1.0",
                    "id": "зомби🧟",
                    "method": "статус",
                    "params": {"лес": "🌲"},
                }
            )
        )
        for index, frame in enumerate(frames):
            for cut in range(len(frame)):
                label = f"seed={REFUSAL_SEED:#x} frame={index} cut={cut}"
                _refused_or_decoded(decode_request, frame[:cut], label)
                _refused_or_decoded(decode_response, frame[:cut], label)

    def test_type_swaps_in_every_field_of_both_envelopes(self) -> None:
        """Every field of every document, replaced by every wrong-typed value
        (and by absence), fed to both decoders — a request document arriving at
        ``decode_response`` is itself a swap the wire can deliver.
        """
        rng = random.Random(REFUSAL_SEED + 1)
        absent: Final = object()
        pool: tuple[object, ...] = (
            absent,
            None,
            True,
            False,
            0,
            1,
            -1,
            2.5,
            "",
            "x",
            [],
            [1, 2],
            {},
            {"k": "v"},
        )
        documents = [
            _request_document(rng),
            _request_document(rng),
            _response_document(rng),
            _response_document(rng),
        ]
        for index, document in enumerate(documents):
            for key in list(document):
                for wrong in pool:
                    mutated = dict(document)
                    if wrong is absent:
                        del mutated[key]
                    else:
                        mutated[key] = wrong
                    label = f"seed={REFUSAL_SEED + 1:#x} doc={index} field={key} wrong={wrong!r}"
                    _refused_or_decoded(decode_request, _frame(mutated), label)
                    _refused_or_decoded(decode_response, _frame(mutated), label)
        # The failed-response branch reads two nested fields no other document
        # has; swap those too, or the `error.get` lines go unfuzzed.
        base_error: dict[str, object] = {"code": "CORE_REFUSED", "message": "refused"}
        failed: dict[str, object] = {
            "format": "pz-agent-core-rpc/1",
            "protocol": "1.0",
            "id": "e1",
            "ok": False,
            "error": dict(base_error),
        }
        for key in base_error:
            for wrong in pool:
                error = dict(base_error)
                if wrong is absent:
                    del error[key]
                else:
                    error[key] = wrong
                mutated = dict(failed)
                mutated["error"] = error
                label = f"error field={key} wrong={wrong!r}"
                _refused_or_decoded(decode_response, _frame(mutated), label)

    def test_absurd_lengths_declared_against_the_caps(self) -> None:
        """Sizes far past every documented bound, on both sides of the parse.

        The over-cap padding case is additionally pinned to ``TOO_LARGE``: the
        guard alone would accept a generic refusal, and a cap that stopped
        reporting itself as the cap would strand the client's error handling.
        """
        rng = random.Random(REFUSAL_SEED + 4)
        base = _request_document(rng)
        cases: list[bytes] = []
        for length in (MAX_ID_CHARS + 1, 600, 60_000):
            oversized = dict(base)
            oversized["id"] = "i" * length
            cases.append(_frame(oversized))
        for length in (MAX_METHOD_CHARS + 1, 4_096):
            oversized = dict(base)
            oversized["method"] = "m" * length
            cases.append(_frame(oversized))
        padded_params = dict(base)
        padded_params["params"] = {"pad": "x" * (MAX_REQUEST_BYTES + 10)}
        cases.append(_frame(padded_params))
        # A string that declares itself open and supplies no close: the
        # declared-versus-supplied mismatch this format can actually express.
        cases.append(b'{"format":"pz-agent-core-rpc/1","protocol":"1.0","id":"' + b"a" * 1_000)
        # Absurd numeric literals. One thousand digits parses to a big int and
        # must be *survived*; the edge past the interpreter's 4300-digit ceiling
        # — where the decoder now refuses rather than raising a bare ValueError
        # — is pinned by test_a_huge_integer_literal_is_refused_not_a_value_error,
        # so this broad corpus stops under it.
        big_digits = b'"params":{"n":' + b"9" * 1_000 + b"}"
        cases.append(_raw_request(b"n").replace(b'"params":{}', big_digits))
        cases.append(_raw_request(b"n").replace(b'"params":{}', b'"params":{"n":1e400000}'))
        oversized_response: dict[str, object] = {
            "format": "pz-agent-core-rpc/1",
            "protocol": "1.0",
            "id": "big",
            "ok": True,
            "result": {"pad": "x" * MAX_RESPONSE_BYTES},
        }
        cases.append(_frame(oversized_response))
        for index, payload in enumerate(cases):
            label = f"seed={REFUSAL_SEED + 4:#x} absurd-length case={index}"
            _refused_or_decoded(decode_request, payload, label)
            _refused_or_decoded(decode_response, payload, label)

        padded = _frame(base) + b" " * MAX_REQUEST_BYTES
        with pytest.raises(RpcError) as caught:
            decode_request(padded)
        assert caught.value.code == ErrorCode.TOO_LARGE, (
            "over-cap bytes were refused, but not as TOO_LARGE"
        )

    def test_unicode_and_surrogate_garbage(self) -> None:
        """Bytes no honest UTF-8 encoder emits, plus escapes that decode to
        surrogates — content that cannot be built by passing a ``str`` through
        ``json.dumps``, which is exactly why it needs its own generator.
        """
        rng = random.Random(REFUSAL_SEED + 2)
        clean = _frame(_request_document(rng))
        handcrafted: list[bytes] = [
            b"\xef\xbb\xbf" + clean,  # UTF-8 BOM: json.loads refuses it
            b"\xff\xfe" + clean,  # UTF-16 BOM on a UTF-8-only pipe
            clean + b"\xff",  # trailing invalid byte
            _raw_request(b"\xed\xa0\x80"),  # UTF-8-encoded lone surrogate
            _raw_request(b"\xc0\xaf"),  # overlong encoding of '/'
            _raw_request(b"\x80\x80"),  # bare continuation bytes
            _raw_request("з".encode()[:1]),  # multibyte character cut in half
            _raw_request(b"\\ud800"),  # escaped lone high surrogate
            _raw_request(b"\\udfff"),  # escaped lone low surrogate
            _raw_request(b"\\ud83d\\ude00"),  # escaped surrogate *pair*
            _raw_request(b"a", method_bytes=b"\\u0000"),  # escaped NUL, legal JSON
            _raw_request(b"a\x00b"),  # raw NUL control byte inside a string
        ]
        for index, payload in enumerate(handcrafted):
            label = f"unicode handcrafted case={index}"
            _refused_or_decoded(decode_request, payload, label)
            _refused_or_decoded(decode_response, payload, label)
        # Seeded splices of high/continuation/lead bytes at random positions,
        # which lands them inside string literals, between tokens, and inside
        # existing multibyte sequences without hand-picking any of it.
        for case in range(60):
            data = bytearray(clean)
            position = rng.randrange(len(data))
            splice = bytes(
                rng.choice((0x80, 0xC3, 0xE2, 0xED, 0xF0, 0xFF)) for _ in range(rng.randint(1, 4))
            )
            data[position:position] = splice
            label = f"seed={REFUSAL_SEED + 2:#x} unicode splice case={case}"
            _refused_or_decoded(decode_request, bytes(data), label)
            _refused_or_decoded(decode_response, bytes(data), label)

    def test_deep_nesting_up_to_the_bounded_depth(self) -> None:
        """Nesting to :data:`_NESTING_BOUND`, inside params and as the whole
        document — the depth the decoder accepts. One level past it, where the
        decoder now refuses rather than overflowing, is pinned by
        :func:`test_deep_nesting_is_refused_not_a_recursion_error`.
        """
        rng = random.Random(REFUSAL_SEED + 5)
        for depth in range(1, _NESTING_BOUND + 1):
            nested: object = rng.choice((0, "leaf", None, True))
            for level in range(depth):
                nested = [nested] if (level + depth) % 2 else {"k": nested}
            document = _request_document(rng)
            document["params"] = {"deep": nested}
            label = f"seed={REFUSAL_SEED + 5:#x} nesting depth={depth}"
            _refused_or_decoded(decode_request, _frame(document), label)
            wall = b"[" * depth + b"]" * depth
            _refused_or_decoded(decode_request, wall, label)
            _refused_or_decoded(decode_response, wall, label)

    def test_duplicate_keys_take_the_last_value_and_a_poisoned_last_loses(self) -> None:
        """JSON tolerates duplicate keys and Python's parser keeps the last.

        Pinned as an observed postcondition, both ways: the benign duplicate
        decodes to the last spelling, and a frame whose *last* format marker is
        wrong is refused even though an earlier one was right — so a smuggled
        second key cannot slip a frame past the marker check.
        """
        doubled = (
            b'{"format":"pz-agent-core-rpc/1","format":"pz-agent-core-rpc/1",'
            b'"protocol":"1.0","protocol":"1.7",'
            b'"id":"first","id":"second",'
            b'"method":"m","method":"n",'
            b'"params":{},"params":{"tail":1}}'
        )
        request = decode_request(doubled)
        assert request.id == "second"
        assert request.method == "n"
        assert request.params == {"tail": 1}
        assert request.protocol == "1.7"

        poisoned = doubled.replace(
            b'"format":"pz-agent-core-rpc/1","format":"pz-agent-core-rpc/1"',
            b'"format":"pz-agent-core-rpc/1","format":"evil"',
        )
        with pytest.raises(RpcError):
            decode_request(poisoned)

    def test_byte_level_mutations_of_valid_frames(self) -> None:
        """Two hundred seeded edits nobody hand-picked: flips, inserts, cuts
        and stutters over fresh valid frames of both directions. This is the
        generator most likely to reach a crash the structured mutators did not
        imagine, which is its entire value.
        """
        rng = random.Random(REFUSAL_SEED + 3)
        for case in range(200):
            source = (
                _frame(_request_document(rng)) if case % 2 == 0 else _frame(_response_document(rng))
            )
            garbage = _mutated_bytes(rng, source)
            label = f"seed={REFUSAL_SEED + 3:#x} byte-mutation case={case}"
            _refused_or_decoded(decode_request, garbage, label)
            _refused_or_decoded(decode_response, garbage, label)


class TestRoundTripOverASeededCorpus:
    """Property (b): ``decode(encode(x)) == x``, over generated documents.

    Mutation check — the defects this catches that the hand-picked round-trip
    tests cannot: a field dropped or renamed in ``encode_*`` (hand-picked
    frames only cover the fields they spell out), a decoder default silently
    substituting for a value that was present, an ``ensure_ascii`` asymmetry
    mangling Cyrillic or astral text, and a numeric type collapsing (int to
    float, bool to int) anywhere in a nested params tree.
    """

    def test_requests_survive_the_wire_field_by_field(self) -> None:
        rng = random.Random(ROUND_TRIP_SEED)
        for case in range(250):
            request = RpcRequest(
                id=_word(rng, MAX_ID_CHARS),
                method=_word(rng, MAX_METHOD_CHARS),
                params=_object(rng, rng.randrange(4)),
                protocol=rng.choice(_MINOR_VERSIONS),
            )
            decoded = decode_request(encode_request(request))
            label = f"seed={ROUND_TRIP_SEED:#x} request case={case}"
            assert decoded.id == request.id, label
            assert decoded.method == request.method, label
            assert decoded.params == request.params, label
            assert decoded.protocol == request.protocol, label
            assert decoded == request, label

    def test_responses_survive_the_wire_field_by_field(self) -> None:
        rng = random.Random(ROUND_TRIP_SEED + 1)
        for case in range(250):
            identifier = _word(rng, MAX_ID_CHARS)
            protocol = rng.choice(_MINOR_VERSIONS)
            if rng.random() < 0.5:
                response = RpcResponse(
                    id=identifier,
                    ok=True,
                    result=_object(rng, rng.randrange(4)),
                    protocol=protocol,
                )
            else:
                response = RpcResponse(
                    id=identifier,
                    ok=False,
                    error_code=rng.choice(
                        (
                            ErrorCode.CORE_REFUSED,
                            ErrorCode.UNKNOWN_METHOD,
                            ErrorCode.TIMEOUT,
                            ErrorCode.UNAVAILABLE,
                        )
                    ),
                    error_message=rng.choice(("", _word(rng, 60))),
                    protocol=protocol,
                )
            decoded = decode_response(encode_response(response))
            label = f"seed={ROUND_TRIP_SEED + 1:#x} response case={case}"
            assert decoded.ok == response.ok, label
            assert decoded.result == response.result, label
            assert decoded.error_code == response.error_code, label
            assert decoded.error_message == response.error_message, label
            assert decoded.id == response.id, label
            assert decoded.protocol == response.protocol, label
            assert decoded == response, label


class TestNoCrossCaseState:
    """Property (d): decoding garbage leaves nothing behind.

    Mutation check — the defect class: a module-level cache, memo, or lazily
    mutated default (the classic shared ``{}`` default argument) that lets one
    call's input leak into the next call's output. Every garbage decode is
    immediately followed by the same reference frame, and the reference must
    decode identically all one hundred and twenty times.
    """

    def test_garbage_between_valid_frames_leaves_no_residue(self) -> None:
        rng = random.Random(RESIDUE_SEED)
        request = RpcRequest(id="steady", method="session.status", params={"case": 0})
        request_frame = encode_request(request)
        response = RpcResponse(id="steady", ok=True, result={"served": True})
        response_frame = encode_response(response)
        for case in range(120):
            source = request_frame if case % 2 == 0 else response_frame
            garbage = _mutated_bytes(rng, source)
            label = f"seed={RESIDUE_SEED:#x} residue case={case}"
            _refused_or_decoded(decode_request, garbage, label)
            _refused_or_decoded(decode_response, garbage, label)
            assert decode_request(request_frame) == request, (
                f"{label}: the garbage before this changed a valid request's decode"
            )
            assert decode_response(response_frame) == response, (
                f"{label}: the garbage before this changed a valid response's decode"
            )


# ---------------------------------------------------------------------------
# Real escapes the fuzz found. Deliberately NOT collected (no `test_` prefix):
# each reproduces a crash in pz_agent_core.rpc.wire, and fixing that module is
# outside this task's grant. They are kept executable so the eventual fix can
# turn each one into a `test_` by renaming it and inverting the expectation.
# ---------------------------------------------------------------------------


def test_deep_nesting_is_refused_not_a_recursion_error() -> None:
    """The escape the fuzzer found, now closed: deep nesting is an RpcError.

    Found by the deep-nesting mutator (seed ``0xDEC0DE + 5``): ``b"[" * 1000 +
    b"]" * 1000`` is thirty-two times under the 64 KiB request cap, yet
    ``json.loads`` recurses once per nesting level and overflowed the stack
    with a ``RecursionError`` the byte cap could not see. ``_loaded`` now
    measures depth on the raw bytes before parsing and refuses past
    :data:`MAX_NESTING_DEPTH`, so both decoders answer with a typed refusal
    carrying a code — never a bare interpreter error, on either side.
    """
    deep = b"[" * 1000 + b"]" * 1000
    for decode in (decode_request, decode_response):
        with pytest.raises(RpcError) as caught:
            decode(deep)
        assert caught.value.code == ErrorCode.MALFORMED
        assert "nests deeper" in str(caught.value)


def test_a_huge_integer_literal_is_refused_not_a_value_error() -> None:
    """The second escape closed: an absurd number is an RpcError.

    Found by the absurd-lengths mutator (seed ``0xDEC0DE + 4``): a valid
    envelope whose params carry ``b"9" * 5000`` as a bare number, far under
    every cap. CPython's integer-string-conversion ceiling
    (``sys.get_int_max_str_digits()``) fires inside ``json.loads`` as a plain
    ``ValueError``, not the ``JSONDecodeError`` subclass ``_loaded`` used to
    catch, so it reached the caller raw. ``_loaded`` now catches the broader
    ``ValueError`` and names it as malformed without echoing the digits.
    """
    frame = (
        b'{"format":"pz-agent-core-rpc/1","protocol":"1.0","id":"1","method":"m",'
        b'"params":{"n":' + b"9" * 5000 + b"}}"
    )
    with pytest.raises(RpcError) as caught:
        decode_request(frame)
    assert caught.value.code == ErrorCode.MALFORMED
    assert "9" * 20 not in str(caught.value), "the refusal must not echo the digits"
