"""Seeded fuzz over the session/heartbeat handshake boundary (Agent S).

The mod writes ``heartbeat.game.json`` and ``session.json`` to the exchange
directory; the sidecar reads them back. Because the writer is a separate process
that can crash mid-write — and because the directory is on a real disk that can
corrupt a sector or hand back a torn fsync — the *on-disk state* the sidecar
reads is arbitrary: a truncated document, garbage bytes, a valid JSON array
where an object was promised, a field of the wrong type, an absurd
timestamp/version, non-UTF8 bytes, a document nested a thousand deep, an integer
literal of thousands of digits. Every one of those is a real state, not a
contrived one.

:mod:`tests.unit.test_session_heartbeat` and
:mod:`tests.unit.test_session_handshake` pin each of these as a hand-written
example. This module asserts the same contract as a *property* over a seeded
corpus, because the value of a fuzz is finding the one on-disk state nobody
thought to write by hand.

The contract under test is the boundary's own promise:

* :meth:`HeartbeatMonitor.read` returns ``Heartbeat | None`` and
  :meth:`HeartbeatMonitor.liveness` returns a :class:`PeerLiveness` — a corrupt
  file is "no readable heartbeat", never a raised exception.
* :meth:`SessionManager.load` returns ``SessionDescriptor | None`` and
  :meth:`SessionManager.staleness` / :meth:`SessionManager.resume` always return
  their typed outcome — a corrupt ``session.json`` is "no readable session", not
  a crash.
* :meth:`Heartbeat.from_dict` / :meth:`SessionDescriptor.from_dict` raise only
  their own :class:`HeartbeatError` / :class:`SessionError` (both ``ValueError``
  subclasses); nothing else — no ``KeyError`` from a missing field, no
  ``TypeError`` from calling a string method on an int, no ``ValueError`` the
  enum lookup forgot to wrap — may escape.
* :func:`evaluate_handshake` always returns a :class:`HandshakeResult`: accepted
  with a session, or refused with a reason code. It never raises on a payload.

Determinism is load-bearing: every corpus comes from ``random.Random`` seeded
from a fixed literal — never the global ``random`` module, never the clock — so
a failure names a reproducible on-disk state, and per-record identity is a
function of the *index*, not the RNG, so a round-trip mismatch points at the
exact record.

Writing this file found two real escapes at the shared disk boundary
(:func:`pz_agent_core.ipc.atomic.read_json_document`, which every reader here
funnels through): a document nested past ~900 deep left ``json.loads`` as a
``RecursionError``, and an integer literal past CPython's 4300-digit ceiling
left it as a bare ``ValueError`` — both under the 8 MiB byte cap, neither the
``DocumentError`` the function's own contract names. Both are now closed there
(a depth scan on the raw bytes before parsing, and a widened ``except`` that
reaches ``ValueError`` rather than only ``JSONDecodeError`` — the same pattern
:mod:`pz_agent_core.jsonbytes`, :mod:`pz_agent_core.rpc.wire` and
:mod:`pz_agent_core.ipc.journal` already carry), and each has its own regression
test at the bottom. The broad corpora steer under both boundaries so their
properties cover the general shape while those two tests pin the exact edges.

The corpus is sized to finish in a couple of seconds; the repository-wide
300-second pytest cap is the enforcement, not an assertion here — asserting wall
time would only make the suite flaky on a loaded runner.
"""

from __future__ import annotations

import json
import random
import uuid
from pathlib import Path
from typing import Any, Final

import pytest

from pz_agent_core.ipc.atomic import (
    MAX_DOCUMENT_DEPTH,
    DocumentError,
    read_json_document,
    write_json_atomic,
)
from pz_agent_core.protocol import DangerLevel, SessionMode
from pz_agent_core.session.handshake import (
    HandshakeResult,
    ResumeOutcome,
    SessionDescriptor,
    SessionError,
    SessionManager,
    SessionStaleness,
    evaluate_handshake,
)
from pz_agent_core.session.heartbeat import (
    Heartbeat,
    HeartbeatError,
    HeartbeatMonitor,
    Peer,
    PeerLiveness,
)
from pz_agent_core.version import PROTOCOL_VERSION
from tests.fixtures.ipc_builders import FakeClock, make_layout

#: One fixed literal seed per property. A failure message quotes the seed and
#: the case index, which together rebuild the exact bytes on any machine.
DISK_SEED: Final = 0x5E5510
FROMDICT_SEED: Final = 0x11A5DB
EVALUATE_SEED: Final = 0xBEA7ED
ROUND_TRIP_SEED: Final = 0xC0FFEE

#: A moment the FakeClock reports; handshake ages are measured against it.
NOW: Final = 1_700_000_000_000

#: Word material. Cyrillic and an astral glyph are deliberate: the project ships
#: to a Russian-language install, so multibyte content has to survive the disk
#: boundary, and a truncation that cuts one in half is exactly the torn-write
#: state this fuzz exists to reach.
_ALPHABETS: Final = (
    "abcdefghijklmnopqrstuvwxyz0123456789._-",
    "сессия",
    "нонс",
    "🧟🌲🔧",
)

#: Non-UTF8 byte runs: a stray BOM tail, high bytes with no lead, and a two-byte
#: sequence cut after its lead byte. Each decodes to ``UnicodeDecodeError``,
#: which the disk boundary must turn into ``DocumentError`` rather than let out.
_NON_UTF8: Final = (
    b"\xff\xfe\x00",
    b"\x80\x81\x82",
    b"\xc3\x28",
    b"payload-\xff-tail",
)

#: Wrong-typed and absurd values a corrupt or hostile record can put in a field
#: that the parser promises to reject with a typed error. ``10**400`` is a valid
#: Python int once parsed (its danger lives only in the *literal*, which the
#: disk boundary handles separately); the rest are type violations the
#: ``from_dict`` guards must name rather than trip over.
_WRONG_VALUES: Final[tuple[object, ...]] = (
    None,
    True,
    False,
    0,
    -1,
    2.5,
    10**400,
    -(10**400),
    "",
    "x",
    "GODMODE",
    [],
    [1, 2],
    {},
    {"k": "v"},
)


def _word(rng: random.Random, cap: int) -> str:
    alphabet = rng.choice(_ALPHABETS)
    return "".join(rng.choice(alphabet) for _ in range(rng.randint(1, cap)))


def _frame(document: dict[str, Any]) -> bytes:
    """*document* as the bytes the atomic writer would put on disk."""
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


def _valid_heartbeat(index: int, rng: random.Random, peer: Peer) -> Heartbeat:
    """A valid heartbeat whose identity is *index* and whose optional payload
    varies with the RNG. The identity fields are index-derived so a round-trip
    mismatch names the exact record rather than a value the parser might
    legitimately have changed.
    """
    rich = rng.random() < 0.5
    return Heartbeat(
        peer=peer,
        session_id=str(uuid.UUID(int=index)),
        nonce=f"nonce-{index:08x}",
        seq=index,
        timestamp_ms=NOW - rng.randint(0, 4_000),
        version=rng.choice(("0.1.0", "0.2.0", "1.0.0")),
        build=rng.choice(("42.20", "41.78")) if rich else None,
        player_present=rng.choice((True, False)) if rich else None,
        armed=rng.choice((True, False)) if rich else None,
        mode=rng.choice(tuple(SessionMode)) if rich else None,
        danger_level=rng.choice(tuple(DangerLevel)) if rich else None,
        active_action_id=f"act-{index}" if rich else None,
    )


def _valid_session(index: int, rng: random.Random) -> SessionDescriptor:
    """A valid session descriptor whose identity is *index*."""
    return SessionDescriptor(
        session_id=str(uuid.UUID(int=index)),
        nonce=f"nonce-{index:08x}",
        created_at_ms=NOW - rng.randint(0, 30_000),
        mode=rng.choice(tuple(SessionMode)),
        sidecar_version=rng.choice(("0.1.0", "0.2.0")),
        protocol_version=PROTOCOL_VERSION,
        requested_observation_hz=rng.randint(1, 30),
        generation=rng.randint(0, 5),
        save_id=rng.choice((None, f"save-{index}")),
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


def _drive_readers(root: Path, body: bytes, label: str) -> None:
    """The property every hostile on-disk body is held to.

    The same bytes are laid into both boundary files and every reader that a
    silent game forces the sidecar to run is exercised. A reader may parse the
    body — random edits sometimes land back on a valid record, and that is not a
    defect — or refuse it with its documented typed outcome. Anything raised is
    the crash this file exists to catch, and the failure names the label and the
    opening bytes so the case can be rebuilt.

    Defect class: any exception (``RecursionError`` from nesting, bare
    ``ValueError`` from an absurd integer literal, ``UnicodeDecodeError`` from
    non-UTF8, ``KeyError`` / ``TypeError`` from an unguarded field) reaching the
    caller where the boundary promises a typed refusal — a denial of service one
    corrupt file wide, since the caller is a loop diagnosing a silent peer.
    """
    layout = make_layout(root)
    layout.game_heartbeat.write_bytes(body)
    layout.session.write_bytes(body)

    clock = FakeClock(now=NOW)
    monitor = HeartbeatMonitor(layout, clock=clock)
    manager = SessionManager(layout, clock=clock, heartbeats=monitor)
    try:
        heartbeat = monitor.read(Peer.GAME)
        liveness = monitor.liveness(Peer.GAME)
        loaded = manager.load()
        stale = manager.staleness()
        resumed = manager.resume()
    except Exception as escape:
        pytest.fail(
            f"{label}: {type(escape).__name__} escaped a reader; "
            f"body starts {body[:64]!r} ({len(body)} bytes)"
        )

    assert heartbeat is None or isinstance(heartbeat, Heartbeat), label
    assert isinstance(liveness, PeerLiveness), label
    assert isinstance(liveness.alive, bool), label
    assert isinstance(liveness.detail, str), label
    assert loaded is None or isinstance(loaded, SessionDescriptor), label
    assert isinstance(stale, SessionStaleness), label
    assert isinstance(resumed, ResumeOutcome), label


# ---------------------------------------------------------------------------
# Round trip: a valid corpus survives the disk and reads back field-for-field.
# This is the control the corruption properties lean on — without it, a reader
# that dropped or mangled even healthy records could pass every "did not crash".
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("peer", [Peer.GAME, Peer.SIDECAR])
def test_a_valid_heartbeat_corpus_round_trips_through_disk(tmp_path: Path, peer: Peer) -> None:
    """A heartbeat written to disk and read back through the monitor is equal.

    Defect class: an optional field dropped or invented across ``to_dict`` /
    ``from_dict`` (a ``False`` flag collapsing to absent, an enum value not
    surviving), or the atomic writer/reader mangling multibyte or boolean
    content on the way through the file.
    """
    rng = random.Random(ROUND_TRIP_SEED)
    for index in range(200):
        layout = make_layout(tmp_path / f"hb-{peer.value}-{index}")
        original = _valid_heartbeat(index, rng, peer)
        path = layout.game_heartbeat if peer is Peer.GAME else layout.sidecar_heartbeat
        write_json_atomic(layout, path, original.to_dict())

        read_back = HeartbeatMonitor(layout, clock=FakeClock(now=NOW)).read(peer)

        label = f"seed={ROUND_TRIP_SEED:#x} heartbeat index={index}"
        assert read_back == original, label
        # to_dict/from_dict is the same contract without the disk in the middle.
        assert Heartbeat.from_dict(original.to_dict()) == original, label


def test_a_valid_session_corpus_round_trips_through_disk(tmp_path: Path) -> None:
    """A session descriptor written to disk reloads equal through the manager.

    Defect class: ``save_id`` presence/absence not preserved, a generation or
    observation-rate field silently defaulted, or the descriptor's own
    invariants (UUID id, bounded rate) rejecting a value it just produced.
    """
    rng = random.Random(ROUND_TRIP_SEED + 1)
    for index in range(200):
        layout = make_layout(tmp_path / f"sd-{index}")
        original = _valid_session(index, rng)
        write_json_atomic(layout, layout.session, original.to_dict())

        reloaded = SessionManager(layout, clock=FakeClock(now=NOW)).load()

        label = f"seed={ROUND_TRIP_SEED + 1:#x} session index={index}"
        assert reloaded == original, label
        assert SessionDescriptor.from_dict(original.to_dict()) == original, label


# ---------------------------------------------------------------------------
# On-disk corruption: the documented typed outcome, never a bare exception.
# ---------------------------------------------------------------------------


def test_arbitrary_bytes_on_disk_never_crash_the_readers(tmp_path: Path) -> None:
    """PROPERTY. Over random binary bodies every reader gives a typed answer.

    Random bytes are the state a corrupt sector or a half-flushed write leaves,
    and this is the generator most likely to reach a crash the structured
    mutators did not imagine — its entire value.

    Defect class: any unhandled exception reaching the caller from a reader that
    promises ``None`` / a typed outcome for an unreadable file.
    """
    rng = random.Random(DISK_SEED)
    for case in range(300):
        length = rng.randint(0, 400)
        body = bytes(rng.randrange(256) for _ in range(length))
        _drive_readers(tmp_path / f"rand-{case}", body, f"seed={DISK_SEED:#x} random case={case}")


def test_truncation_at_every_byte_boundary_is_refused_or_read(tmp_path: Path) -> None:
    """PROPERTY. Every prefix of a valid frame is a typed answer, never a crash.

    A mod killed mid-write leaves exactly a prefix of a good document. Walking
    every cut point provably includes the interesting ones — mid-token,
    mid-string, mid-multibyte character (the Cyrillic/astral frame guarantees
    those exist).

    Defect class: a ``JSONDecodeError`` from a torn document, or a
    ``UnicodeDecodeError`` from a character cut in half, escaping the parse
    instead of becoming "no readable record".
    """
    rng = random.Random(DISK_SEED + 1)
    frames = [_frame(_valid_heartbeat(i, rng, Peer.GAME).to_dict()) for i in range(2)]
    frames.append(_frame(_valid_session(3, rng).to_dict()))
    frames.append(
        _frame(
            {
                "peer": "game",
                "session_id": str(uuid.UUID(int=7)),
                "nonce": "нонс-🧟",
                "seq": 1,
                "timestamp_ms": NOW,
                "version": "лес-🌲",
                "protocol_version": PROTOCOL_VERSION,
            }
        )
    )
    for index, frame in enumerate(frames):
        for cut in range(len(frame) + 1):
            _drive_readers(
                tmp_path / f"trunc-{index}-{cut}",
                frame[:cut],
                f"seed={DISK_SEED + 1:#x} frame={index} cut={cut}",
            )


def test_byte_level_mutations_of_valid_records_never_crash(tmp_path: Path) -> None:
    """PROPERTY. Seeded flips/inserts/cuts/stutters of good frames stay typed.

    Defect class: byte soup that reaches an unguarded parse path no hand-picked
    example covered — the arbitrary-edit analog of the categories above.
    """
    rng = random.Random(DISK_SEED + 2)
    for case in range(200):
        if case % 2 == 0:
            source = _frame(_valid_heartbeat(case, rng, Peer.GAME).to_dict())
        else:
            source = _frame(_valid_session(case, rng).to_dict())
        body = _mutated_bytes(rng, source)
        _drive_readers(
            tmp_path / f"mut-{case}", body, f"seed={DISK_SEED + 2:#x} mutation case={case}"
        )


def test_non_utf8_bodies_are_refused_cleanly(tmp_path: Path) -> None:
    """PROPERTY. Undecodable bytes are "no readable record", never a raise.

    Defect class: a ``UnicodeDecodeError`` on non-UTF8 disk content reaching the
    caller instead of being caught and reported as an unreadable document.
    """
    rng = random.Random(DISK_SEED + 3)
    clean = _frame(_valid_heartbeat(0, rng, Peer.GAME).to_dict())
    session_clean = _frame(_valid_session(0, rng).to_dict())
    handcrafted: list[bytes] = [
        b"\xef\xbb\xbf" + clean,  # UTF-8 BOM prefix
        clean + b"\xff",  # trailing invalid byte
        *(session_clean + tail for tail in _NON_UTF8),
        *_NON_UTF8,
    ]
    for index, body in enumerate(handcrafted):
        _drive_readers(tmp_path / f"nonutf8-{index}", body, f"non-utf8 handcrafted case={index}")
    # Seeded splices of high/continuation/lead bytes land inside string literals
    # and multibyte sequences without any of it being hand-picked.
    for case in range(80):
        data = bytearray(clean)
        position = rng.randrange(len(data))
        splice = bytes(
            rng.choice((0x80, 0xC3, 0xE2, 0xED, 0xF0, 0xFF)) for _ in range(rng.randint(1, 4))
        )
        data[position:position] = splice
        _drive_readers(
            tmp_path / f"splice-{case}", bytes(data), f"seed={DISK_SEED + 3:#x} splice case={case}"
        )


def test_wrong_typed_and_absurd_records_are_refused_cleanly(tmp_path: Path) -> None:
    """PROPERTY. Valid JSON of the wrong shape reads as "no record", not a crash.

    Right syntax, wrong shape — an array, a scalar, a field of the wrong type,
    an absurd timestamp or version — is precisely what a producer at a different
    version or a corrupted-but-parseable file leaves.

    Defect class: a wrong-typed field slipping past the parser into a
    ``TypeError`` (calling a string method on an int, comparing across types) or
    a ``KeyError`` (a promised field absent) instead of the typed reject.
    """
    rng = random.Random(DISK_SEED + 4)
    base_hb = _valid_heartbeat(1, rng, Peer.GAME).to_dict()
    base_sd = _valid_session(1, rng).to_dict()
    bodies: list[bytes] = [
        _frame({"not": "a heartbeat"}),
        b"[1,2,3]",
        b"42",
        b'"a string"',
        b"true",
        b"null",
        b"{}",
    ]
    for base in (base_hb, base_sd):
        for key in base:
            for wrong in _WRONG_VALUES:
                mutated = dict(base)
                mutated[key] = wrong
                bodies.append(_frame(mutated))
            without = dict(base)
            del without[key]
            bodies.append(_frame(without))
    for index, body in enumerate(bodies):
        _drive_readers(
            tmp_path / f"wrong-{index}", body, f"seed={DISK_SEED + 4:#x} wrong-typed case={index}"
        )


def test_reading_a_corrupt_record_is_deterministic(tmp_path: Path) -> None:
    """Two fresh readers over the same bytes must agree exactly.

    Defect class: hidden nondeterminism (dict/set iteration order, ambient
    state) in the parse or the verdict, which would make a diagnostic depend on
    something other than the file.
    """
    rng = random.Random(DISK_SEED + 5)
    body = _mutated_bytes(rng, _frame(_valid_heartbeat(9, rng, Peer.GAME).to_dict()))
    layout = make_layout(tmp_path / "det")
    layout.game_heartbeat.write_bytes(body)

    first = HeartbeatMonitor(layout, clock=FakeClock(now=NOW)).liveness(Peer.GAME)
    second = HeartbeatMonitor(layout, clock=FakeClock(now=NOW)).liveness(Peer.GAME)

    assert first.alive == second.alive
    assert first.detail == second.detail
    assert (first.heartbeat is None) == (second.heartbeat is None)


# ---------------------------------------------------------------------------
# from_dict: any mapping yields the dataclass or the boundary's own typed error.
# These payloads are already-parsed dicts, which is all from_dict ever sees —
# read_json_document guarantees a non-dict never reaches here.
# ---------------------------------------------------------------------------


def _assert_heartbeat_from_dict_typed(payload: dict[str, Any], label: str) -> None:
    try:
        result = Heartbeat.from_dict(payload)
    except HeartbeatError:
        return
    except Exception as escape:
        pytest.fail(f"{label}: {type(escape).__name__} escaped Heartbeat.from_dict")
    assert isinstance(result, Heartbeat), label


def test_heartbeat_from_dict_raises_only_its_typed_error() -> None:
    """PROPERTY. Every field of a heartbeat, wrong-typed or absent, is a
    ``Heartbeat`` or a ``HeartbeatError`` — never another exception.

    Defect class: a ``KeyError`` from a missing field, a ``TypeError`` from a
    non-string handed to a string check, or a ``ValueError`` the enum lookup for
    ``mode`` / ``danger_level`` failed to wrap (an unhashable value handed to the
    enum constructor is the sharp edge) — each an untyped escape from a parser
    that promises one exception type.
    """
    rng = random.Random(FROMDICT_SEED)
    for index in range(6):  # RNG-varied bases so optionals differ across runs
        base = _valid_heartbeat(index, rng, Peer.GAME).to_dict()
        for key in list(base):
            for wrong in _WRONG_VALUES:
                mutated = dict(base)
                mutated[key] = wrong
                label = f"seed={FROMDICT_SEED:#x} index={index} field={key} wrong={wrong!r}"
                _assert_heartbeat_from_dict_typed(mutated, label)
            without = dict(base)
            del without[key]
            _assert_heartbeat_from_dict_typed(without, f"heartbeat missing field={key}")
    tail_base = _valid_heartbeat(6, rng, Peer.GAME).to_dict()
    _assert_heartbeat_from_dict_typed({}, "heartbeat empty")
    _assert_heartbeat_from_dict_typed({**tail_base, "extra": [1, 2, 3]}, "heartbeat extra field")
    _assert_heartbeat_from_dict_typed({**tail_base, "mode": ["x"]}, "heartbeat unhashable mode")
    _assert_heartbeat_from_dict_typed(
        {**tail_base, "danger_level": {"z": 1}}, "heartbeat unhashable danger"
    )


def _assert_session_from_dict_typed(payload: dict[str, Any], label: str) -> None:
    try:
        result = SessionDescriptor.from_dict(payload)
    except SessionError:
        return
    except Exception as escape:
        pytest.fail(f"{label}: {type(escape).__name__} escaped SessionDescriptor.from_dict")
    assert isinstance(result, SessionDescriptor), label


def test_session_from_dict_raises_only_its_typed_error() -> None:
    """PROPERTY. Every field of a session document, wrong-typed or absent, is a
    ``SessionDescriptor`` or a ``SessionError`` — never another exception.

    Defect class: the same untyped escapes as the heartbeat property, plus the
    UUID and observation-rate invariants raising a raw ``ValueError`` the
    descriptor forgot to convert into ``SessionError``.
    """
    rng = random.Random(FROMDICT_SEED + 1)
    for index in range(6):
        base = _valid_session(index, rng).to_dict()
        for key in list(base):
            for wrong in _WRONG_VALUES:
                mutated = dict(base)
                mutated[key] = wrong
                label = f"seed={FROMDICT_SEED + 1:#x} index={index} field={key} wrong={wrong!r}"
                _assert_session_from_dict_typed(mutated, label)
            without = dict(base)
            del without[key]
            _assert_session_from_dict_typed(without, f"session missing field={key}")
    _assert_session_from_dict_typed({}, "session empty")
    _assert_session_from_dict_typed({"mode": ["x"]}, "session unhashable mode")


# ---------------------------------------------------------------------------
# evaluate_handshake: any payload yields a verdict, never a raise.
# ---------------------------------------------------------------------------


def test_evaluate_handshake_always_returns_a_verdict() -> None:
    """PROPERTY. Wrong-typed payloads, absurd timestamps/versions and reused
    nonces all produce a ``HandshakeResult`` — accepted with a session, or
    refused with a reason code — never a bare exception.

    Defect class: an unguarded ``SessionDescriptor.from_dict`` escape reaching
    the caller (the §3.3 decision must express *every* rejection as a reason
    code, since both sides act on it), or ``protocol_compatible`` / the age
    arithmetic raising on a pathological version string or a huge integer
    timestamp.
    """
    rng = random.Random(EVALUATE_SEED)
    good = _valid_session(1, rng)
    previous = _valid_session(2, rng)
    base = good.to_dict()

    # A fully valid payload must still be accepted — the accept path has to be
    # reached, not just the refusals.
    accepted = evaluate_handshake(
        base,
        now_ms=NOW,
        peer=Peer.GAME,
        peer_liveness=PeerLiveness(Peer.GAME, None, alive=True, detail="fresh"),
    )
    assert accepted.accepted
    assert accepted.session is not None
    assert accepted.reason_code is None

    payloads: list[dict[str, Any]] = [dict(base)]
    for key in base:
        for wrong in _WRONG_VALUES:
            mutated = dict(base)
            mutated[key] = wrong
            payloads.append(mutated)
    # Absurd-but-parseable timestamps and versions, exercised through the whole
    # decision rather than only from_dict.
    payloads.append({**base, "created_at_ms": NOW + 10**12})
    payloads.append({**base, "created_at_ms": NOW - 10**12})
    payloads.append({**base, "created_at_ms": 10**400})
    payloads.append({**base, "protocol_version": "2.0"})
    payloads.append({**base, "protocol_version": "9" * 4000})
    payloads.append({**base, "protocol_version": "1"})
    payloads.append({**base, "nonce": previous.nonce})  # replayed nonce

    liveness_choices = (
        None,
        PeerLiveness(Peer.GAME, None, alive=True, detail="fresh"),
        PeerLiveness(Peer.GAME, None, alive=False, detail="silent for 9000 ms"),
    )
    for index, payload in enumerate(payloads):
        rng_local = random.Random(EVALUATE_SEED + index)
        peer = rng_local.choice((Peer.GAME, Peer.SIDECAR))
        liveness = rng_local.choice(liveness_choices)
        prev = rng_local.choice((None, previous))
        now = rng_local.choice((NOW, NOW + 1, NOW - 10**9, 0))
        label = f"seed={EVALUATE_SEED:#x} evaluate case={index}"
        try:
            result = evaluate_handshake(
                payload,
                now_ms=now,
                peer=peer,
                peer_liveness=liveness,
                previous=prev,
            )
        except Exception as escape:
            pytest.fail(f"{label}: {type(escape).__name__} escaped evaluate_handshake")
        assert isinstance(result, HandshakeResult), label
        if result.accepted:
            assert result.session is not None, label
            assert result.reason_code is None, label
        else:
            assert result.reason_code is not None, label
            assert result.session is None, label
            assert isinstance(result.detail, str), label


# ---------------------------------------------------------------------------
# The two escapes the fuzz found, now fixed at the shared disk boundary
# (pz_agent_core.ipc.atomic.read_json_document) and pinned here with their
# exact, minimal reproductions. Both were reachable through every reader in this
# module, because all of them funnel a mod-written file through that function.
# ---------------------------------------------------------------------------


def test_a_deeply_nested_document_is_refused_not_a_recursion_error(tmp_path: Path) -> None:
    """The escape closed: a deeply nested heartbeat/session is "no record".

    A document of ``b"["`` repeated a thousand times is a few kilobytes — far
    under the 8 MiB byte cap — yet ``json.loads`` recurses once per nesting
    level and overflowed the interpreter with a ``RecursionError`` that
    ``read_json_document`` did not catch, so it propagated out of
    :meth:`HeartbeatMonitor.read` and :meth:`SessionManager.load` where the
    contract promises ``None``. Depth is now measured on the raw bytes before
    parsing (:data:`MAX_DOCUMENT_DEPTH`), so the document is refused as
    ``DocumentError`` and the readers report an unreadable record.
    """
    deep = b"[" * 1000 + b"]" * 1000
    layout = make_layout(tmp_path / "deep")
    layout.game_heartbeat.write_bytes(deep)
    layout.session.write_bytes(deep)

    with pytest.raises(DocumentError) as caught:
        read_json_document(layout.game_heartbeat)
    assert "nests deeper" in str(caught.value)

    monitor = HeartbeatMonitor(layout, clock=FakeClock(now=NOW))
    assert monitor.read(Peer.GAME) is None
    liveness = monitor.liveness(Peer.GAME)
    assert not liveness.alive
    assert "no readable heartbeat" in liveness.detail

    assert SessionManager(layout, clock=FakeClock(now=NOW)).load() is None

    # A document one level under the bound still parses, so the refusal is the
    # depth guard doing its job, not a blanket rejection of any nesting.
    shallow = _frame({"nest": [[[[1]]]]})
    layout.game_heartbeat.write_bytes(shallow)
    assert isinstance(read_json_document(layout.game_heartbeat), dict)
    assert MAX_DOCUMENT_DEPTH >= 5


def test_a_huge_integer_literal_is_refused_not_a_value_error(tmp_path: Path) -> None:
    """The second escape closed: an absurd integer literal is "no record".

    A ``session.json`` whose ``created_at_ms`` is a literal of 5000 digits is
    far under the byte cap, but CPython's integer-string-conversion ceiling
    (``sys.get_int_max_str_digits()``, 4300) fires inside ``json.loads`` as a
    plain ``ValueError`` — not the ``JSONDecodeError`` subclass
    ``read_json_document`` used to catch — so it reached the caller raw.
    ``read_json_document`` now catches the broader ``ValueError`` and names it
    as malformed without echoing the digits, so :meth:`SessionManager.load`
    returns ``None``.
    """
    frame = b'{"created_at_ms":' + b"9" * 5000 + b',"nonce":"n"}'
    layout = make_layout(tmp_path / "bigint")
    layout.session.write_bytes(frame)
    layout.game_heartbeat.write_bytes(frame)

    with pytest.raises(DocumentError) as caught:
        read_json_document(layout.session)
    assert "9" * 20 not in str(caught.value), "the refusal must not echo the digits"

    manager = SessionManager(layout, clock=FakeClock(now=NOW))
    assert manager.load() is None
    assert manager.resume().resumed is False
    assert HeartbeatMonitor(layout, clock=FakeClock(now=NOW)).read(Peer.GAME) is None
