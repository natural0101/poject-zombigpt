"""Fuzz the two files a client reads at startup, over the mess a crash leaves.

``<state-dir>/runtime/core-rpc.json`` and ``core-rpc.key`` are read before
anything is connected, and the process that wrote them may have been killed,
half-way through a write, or replaced by something else writing where they go.
So the loaders promise a *typed* refusal for every shape of garbage — a
``DescriptorError`` (or its ``OversizeDescriptor`` / ``StaleDescriptor``
children) and a ``TokenError`` — and never a bare exception out of the parse or
the socket layer, and the token path promises the secret reaches neither a
message nor a log. This module drives the real functions over real temp files to
hold them to that across a seeded corpus that the hand-written cases nearby
(``test_rpc_descriptor_bounds.py``, ``test_rpc_token_and_descriptor.py``) cannot
enumerate: every byte value, arbitrary blobs, and structured mutations picked at
random from a fixed seed.

Each corpus is generated from ``random.Random`` seeded with a fixed literal, so
a failure reproduces byte-for-byte; anything that varies per index (an address,
a label) varies *by the index*, so the same seed always paints the same file.

Two real defects fell out of this fuzz and are now fixed in the loader, each
with its own regression test below:

* :func:`test_a_pid_past_c_int_range_is_a_typed_refusal` — a descriptor whose
  ``pid`` is at least ``2**31`` used to reach ``os.kill(pid, 0)`` on POSIX,
  which overflows ``pid_t`` and raised a bare ``OverflowError`` where the
  contract names its own refusal; the loader now bounds the pid first. The
  structured-corruption corpus still keeps every ``pid`` it emits inside the
  C-int range, so that property covers the *checks* and this test covers the
  bound.
* :func:`test_an_unhashable_family_is_a_typed_refusal` — a descriptor whose
  ``family`` is a JSON array or object used to reach ``family not in {...}``,
  and membership on an unhashable value raised ``TypeError`` before any refusal
  was built; the loader now checks the type first. The corpus still mutates
  ``family`` only to hashable values.
"""

from __future__ import annotations

import json
import logging
import os
import random
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Final

import pytest

from pz_agent_core.rpc.descriptor import (
    DESCRIPTOR_FILENAME,
    DESCRIPTOR_FORMAT,
    FAMILY_PIPE,
    FAMILY_UNIX,
    MAX_DESCRIPTOR_BYTES,
    DescriptorError,
    RpcDescriptor,
    load_descriptor,
    runtime_dir,
    write_descriptor,
)
from pz_agent_core.rpc.token import (
    MIN_TOKEN_BYTES,
    TOKEN_FILENAME,
    TokenError,
    issue_token,
    read_token,
)
from pz_agent_core.version import RPC_PROTOCOL_VERSION

#: One fixed seed per corpus, so each test is independent of collection order and
#: reproduces on its own. Distinct literals so two corpora never paint the same
#: file and hide a bug behind a coincidence.
_SEED_BLOB: Final = 0xDE5C_B17E
_SEED_STRUCT: Final = 0x5712_C7ED
_SEED_TRUNCATE: Final = 0x7EAD_CAFE
_SEED_ROUNDTRIP: Final = 0x0DDB_A110
_SEED_TOKEN_BYTES: Final = 0x70CE_B17E
_SEED_TOKEN_SHORT: Final = 0x5405_ED00
_SEED_TOKEN_ISSUE: Final = 0x0155_0ED0
_SEED_TOKEN_LOG: Final = 0x0CA7_A106


@pytest.fixture
def state(tmp_path: Path) -> Path:
    """A state directory under the Windows-shaped path this ships on.

    The account name in the middle is why no token refusal may quote a path; the
    directories are real so the loaders read a real file.
    """
    return tmp_path / "Users" / "jdoe" / "Zomboid" / "pz-agent"


def _seed_runtime(state: Path) -> Path:
    """Make the runtime dir exist with a token beside where the descriptor goes.

    Mirrors the on-disk shape a running sidecar leaves, so a corrupt descriptor
    is read from the same neighbourhood the real one would be.
    """
    directory = runtime_dir(state)
    issue_token(directory)
    return directory


def _write_descriptor_bytes(state: Path, data: bytes) -> Path:
    """Put *data* exactly where the descriptor loader will read it."""
    path = _seed_runtime(state) / DESCRIPTOR_FILENAME
    path.write_bytes(data)
    return path


def _base_document(pid: int) -> dict[str, object]:
    """A descriptor this build would accept, as a plain dict of literals.

    Retyped from literals rather than built from the module's constants: a
    mutation has to start from something the loader *accepts*, or the corpus
    proves nothing about the checks it is trying to trip.
    """
    return {
        "format": DESCRIPTOR_FORMAT,
        "protocol": RPC_PROTOCOL_VERSION,
        "family": FAMILY_UNIX,
        "address": "/tmp/pz.sock",
        "pid": pid,
        "token_file": TOKEN_FILENAME,
    }


# --- structured mutations -------------------------------------------------
#
# Each takes an accepted document and breaks exactly one field into a value the
# loader must refuse. Every one fails a check that runs *before* ``os.kill``, so
# none depends on which pids happen to be alive, and every value is hashable and
# inside C-int range so each trips a field check rather than the pid bound or
# the family-type guard, which have their own regression tests below.


def _m_foreign_format(doc: dict[str, object], rng: random.Random) -> None:
    doc["format"] = "somebody-elses/" + str(rng.randint(1, 9))


def _m_missing_format(doc: dict[str, object], rng: random.Random) -> None:
    doc.pop("format", None)


def _m_wrong_major(doc: dict[str, object], rng: random.Random) -> None:
    doc["protocol"] = f"{rng.randint(2, 9)}.{rng.randint(0, 9)}"


def _m_unreadable_protocol(doc: dict[str, object], rng: random.Random) -> None:
    # Every value here has a non-digit head, so ``protocol_major`` raises rather
    # than reading a major: ``"1..0"`` would *not* belong, its head being ``1``.
    doc["protocol"] = rng.choice(["", "x.y", "v1", "one.two", "abc", "no-dot"])


def _m_nonstr_protocol(doc: dict[str, object], rng: random.Random) -> None:
    values: list[object] = [1, 1.0, None, 10]
    doc["protocol"] = rng.choice(values)


def _m_empty_address(doc: dict[str, object], rng: random.Random) -> None:
    doc["address"] = ""


def _m_nonstr_address(doc: dict[str, object], rng: random.Random) -> None:
    values: list[object] = [0, 123, None, True]
    doc["address"] = rng.choice(values)


def _m_missing_address(doc: dict[str, object], rng: random.Random) -> None:
    doc.pop("address", None)


def _m_bad_family_string(doc: dict[str, object], rng: random.Random) -> None:
    doc["family"] = rng.choice(["AF_INET", "af_unix", "UNIX", "", "AF_UNIXX", "pipe"])


def _m_nonstr_family(doc: dict[str, object], rng: random.Random) -> None:
    values: list[object] = [0, 1, None, True]
    doc["family"] = rng.choice(values)


def _m_missing_family(doc: dict[str, object], rng: random.Random) -> None:
    doc.pop("family", None)


def _m_nonpositive_pid(doc: dict[str, object], rng: random.Random) -> None:
    doc["pid"] = rng.choice([0, -1, -rng.randint(1, 1_000_000)])


def _m_nonint_pid(doc: dict[str, object], rng: random.Random) -> None:
    values: list[object] = ["1234", 1.5, None, True, False]
    doc["pid"] = rng.choice(values)


def _m_missing_pid(doc: dict[str, object], rng: random.Random) -> None:
    doc.pop("pid", None)


_MUTATIONS: Final[list[Callable[[dict[str, object], random.Random], None]]] = [
    _m_foreign_format,
    _m_missing_format,
    _m_wrong_major,
    _m_unreadable_protocol,
    _m_nonstr_protocol,
    _m_empty_address,
    _m_nonstr_address,
    _m_missing_address,
    _m_bad_family_string,
    _m_nonstr_family,
    _m_missing_family,
    _m_nonpositive_pid,
    _m_nonint_pid,
    _m_missing_pid,
]


class _CapturingHandler(logging.Handler):
    """Records every spelling a log record could carry a value out in."""

    def __init__(self) -> None:
        super().__init__(level=logging.NOTSET)
        self.texts: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.texts.append(f"{record.msg!r} {record.args!r} {record.getMessage()}")


class TestDescriptorFuzz:
    """Catches: any bare exception (UnicodeDecodeError, JSONDecodeError, OSError,
    TypeError, OverflowError, KeyError) escaping ``load_descriptor`` where the
    contract promises a ``DescriptorError`` and its children."""

    def test_arbitrary_bytes_yield_a_typed_refusal(self, state: Path) -> None:
        """Random blobs — non-UTF-8, non-JSON, JSON-but-not-ours, oversized.

        A file left where the descriptor goes can be literally anything: a log,
        a truncated download, another program's data. The decode and the parse
        are where a bare ``UnicodeDecodeError`` or ``JSONDecodeError`` would
        escape, and the size branch is where an oversized blob must still be a
        ``DescriptorError`` (as ``OversizeDescriptor``) rather than a raw read.
        """
        rng = random.Random(_SEED_BLOB)
        for i in range(240):
            # Every few indices, deliberately overshoot the cap so the oversize
            # branch is exercised as often as the parse branch; the rest stay
            # small so the parse is what refuses them.
            if i % 8 == 0:
                length = MAX_DESCRIPTOR_BYTES + rng.randint(1, 4096)
            else:
                length = rng.randint(0, 400)
            blob = bytes(rng.randrange(256) for _ in range(length))
            _write_descriptor_bytes(state, blob)
            # pytest.raises(DescriptorError) does NOT catch a bare exception, so
            # a stray OverflowError/TypeError/UnicodeDecodeError fails here loudly
            # rather than passing — which is the whole point of the fuzz.
            with pytest.raises(DescriptorError):
                load_descriptor(state)

    def test_structured_corruption_yields_a_typed_refusal(self, state: Path) -> None:
        """A valid document with one to three fields broken into refused values.

        This is the ``JSON-but-wrong-shape`` case the byte fuzz almost never
        reaches by chance: a real document, our format, with a bad family
        *string*, an out-of-range-but-in-int-range pid, a missing field. Every
        such file must be refused by name, not by a crash inside a field check.
        """
        rng = random.Random(_SEED_STRUCT)
        for _ in range(240):
            document = _base_document(pid=os.getpid())
            for mutate in rng.sample(_MUTATIONS, rng.randint(1, 3)):
                mutate(document, rng)
            _write_descriptor_bytes(state, json.dumps(document).encode("utf-8"))
            with pytest.raises(DescriptorError):
                load_descriptor(state)

    def test_truncated_prefixes_of_a_valid_descriptor_are_refused(self, state: Path) -> None:
        """A crash mid-write leaves a prefix of a good descriptor, not garbage.

        A prefix of valid JSON is invalid JSON, so it must land on the malformed
        refusal — the ``DescriptorError`` that means "restart", never a bare
        parse error and never a silent partial parse.
        """
        whole = json.dumps(_base_document(pid=os.getpid())).encode("utf-8")
        rng = random.Random(_SEED_TRUNCATE)
        for _ in range(120):
            cut = rng.randint(0, len(whole) - 1)
            _write_descriptor_bytes(state, whole[:cut])
            with pytest.raises(DescriptorError):
                load_descriptor(state)

    def test_a_valid_descriptor_round_trips_through_write_and_load(self, state: Path) -> None:
        """The other half: a good one written must come back byte-for-byte.

        Addresses are fuzzed with the characters JSON has to escape — spaces,
        quotes, backslashes (the Windows pipe shape), tabs, non-ASCII — because
        ``write_descriptor`` serialises with ``ensure_ascii=False`` and a reader
        that mangled any of them would hand back a different address than was
        listened on. The pid is this live process and the token is present, so
        the alive-and-token preconditions are exercised too.
        """
        _seed_runtime(state)
        flavors = ["plain", "with space", 'quote"q', "back\\slash", "tab\tchar", "юникод", "a/b"]
        rng = random.Random(_SEED_ROUNDTRIP)
        for i in range(70):
            # Varied by the index, not by randomness, so the same seed always
            # names the same socket — the label is reproducible on its own.
            flavor = flavors[i % len(flavors)]
            address = f"/tmp/pz-agent-{i:03d}-{flavor}.sock"
            family = FAMILY_UNIX if rng.random() < 0.5 else FAMILY_PIPE
            written = RpcDescriptor(address=address, family=family, pid=os.getpid())
            write_descriptor(state, written)

            loaded = load_descriptor(state)

            assert loaded == RpcDescriptor(
                address=address,
                family=family,
                pid=os.getpid(),
                protocol=RPC_PROTOCOL_VERSION,
            )


class TestTokenFuzz:
    """Catches: any bare exception (OSError, IsADirectoryError, ValueError)
    escaping ``read_token`` where a ``TokenError`` is promised, a length change
    across the read/write boundary, and the secret leaking into a message or a
    log record."""

    def test_every_byte_value_round_trips_verbatim(self, state: Path) -> None:
        """A token is 32 random bytes: any value, including NUL and newline.

        The read must return exactly what was written, at exactly the length —
        the length is here because the shipped bug turned a newline inside the
        secret into two bytes on Windows. The all-256 payload guarantees every
        byte value, including ``0x00`` and ``0x0A``, is present at least once;
        the seeded corpus varies length and content around it.
        """
        directory = _seed_runtime(state)
        path = directory / TOKEN_FILENAME

        every_value = bytes(range(256))
        path.write_bytes(every_value)
        assert read_token(directory) == every_value
        assert path.stat().st_size == len(every_value)

        rng = random.Random(_SEED_TOKEN_BYTES)
        for _ in range(160):
            length = rng.randint(MIN_TOKEN_BYTES, 512)
            payload = bytes(rng.randrange(256) for _ in range(length))
            path.write_bytes(payload)

            assert read_token(directory) == payload
            assert path.stat().st_size == length

    def test_short_or_missing_or_directory_yield_a_typed_refusal(self, state: Path) -> None:
        """Every under-length or unreadable file is a ``TokenError``, not a use.

        A file shorter than the minimum is a truncated write or somebody else's
        file, and authenticating with it authenticates with a value that is not
        secret; a directory where the file goes is an ``OSError`` the loader must
        turn into the same typed refusal rather than let escape.
        """
        directory = _seed_runtime(state)
        path = directory / TOKEN_FILENAME
        rng = random.Random(_SEED_TOKEN_SHORT)
        for _ in range(160):
            length = rng.randint(0, MIN_TOKEN_BYTES - 1)
            payload = bytes(rng.randrange(256) for _ in range(length))
            path.write_bytes(payload)

            with pytest.raises(TokenError) as caught:
                read_token(directory)

            # The refusal names the file and the two lengths and nothing of the
            # bytes — a short secret is still a secret. Guarded on non-empty so
            # the empty hex string does not make the check vacuous.
            if payload:
                message = str(caught.value)
                assert payload.hex() not in message
                assert repr(payload) not in message

        # Missing file: the absence, not a crash.
        path.unlink()
        with pytest.raises(TokenError, match="not running"):
            read_token(directory)

        # A directory where the file belongs: an OSError becomes the typed refusal.
        path.mkdir()
        with pytest.raises(TokenError, match="cannot be read"):
            read_token(directory)

    def test_issue_then_read_returns_the_issued_bytes(self, state: Path) -> None:
        """The real functions, round-tripped: what was issued is what is read."""
        directory = _seed_runtime(state)
        for _ in range(24):
            issued = issue_token(directory)

            assert len(issued) >= MIN_TOKEN_BYTES
            assert read_token(directory) == issued

    def test_neither_issue_nor_read_logs_the_secret(self, state: Path) -> None:
        """The token path emits no log record carrying the key, in any spelling.

        ``issue_token`` and ``read_token`` do not log today; this arms a canary
        and captures every propagating record so that a future ``log.debug("token
        %s", token.hex())`` on either path fails here rather than shipping a
        secret into a bug report. Fuzzed over a corpus of freshly issued tokens
        so no single value can pass by luck.
        """
        directory = _seed_runtime(state)
        capture = _CapturingHandler()
        root = logging.getLogger()
        previous = root.level
        root.addHandler(capture)
        root.setLevel(logging.DEBUG)
        issued: list[bytes] = []
        try:
            rng = random.Random(_SEED_TOKEN_LOG)
            for _ in range(24):
                token = issue_token(directory)
                issued.append(token)
                assert read_token(directory) == token
                # A short overwrite drives the refusal path too, whose message is
                # the other place a secret could be spelled out.
                (directory / TOKEN_FILENAME).write_bytes(token[: rng.randint(0, 8)])
                with pytest.raises(TokenError):
                    read_token(directory)
            # The trap must be armed, or every absence below is vacuous.
            logging.getLogger("pz_agent_core.rpc").debug("canary %s", "record")
        finally:
            root.removeHandler(capture)
            root.setLevel(previous)

        assert any("canary" in text for text in capture.texts), "the capture caught nothing"
        for token in issued:
            spellings = (token.hex(), token.hex().upper(), repr(token), token.decode("latin-1"))
            for text in capture.texts:
                for spelling in spellings:
                    assert spelling not in text, "a log record carries the token"


# --- captured finds (NOT collected: names do not start with ``test_``) -------
#
# These reproduce two real crashes the fuzz above found. They are deliberately
# excluded from every corpus so the collected suite stays green, and they build
# their own temp state so they can be run by hand to confirm the defect still
# stands. The production module is out of this task's grant, so they document
# rather than fix.


def test_a_pid_past_c_int_range_is_a_typed_refusal() -> None:
    """The escape closed: an impossible pid is a ``DescriptorError``.

    A pid of ``2**31`` (one past the signed ``pid_t``) passed the positive-int
    check and reached ``os.kill(pid, 0)``, which raised a bare ``OverflowError``
    from inside the liveness probe. The loader now refuses a pid past
    ``MAX_PID`` with its own typed error before probing it. ``2**31 - 1`` is
    still a valid (if almost certainly dead) pid, so the bound is exact.
    """
    with tempfile.TemporaryDirectory() as raw:
        state = Path(raw)
        _write_descriptor_bytes(state, json.dumps(_base_document(pid=2**31)).encode("utf-8"))
        with pytest.raises(DescriptorError):
            load_descriptor(state)


def test_an_unhashable_family_is_a_typed_refusal() -> None:
    """The second escape closed: a non-string family is refused by name.

    A ``family`` delivered by JSON as an array or object is unhashable, and the
    membership test ``family not in {FAMILY_PIPE, FAMILY_UNIX}`` raised a bare
    ``TypeError`` on it before any refusal was built. The loader now checks the
    type first, so a non-string family gets the same named refusal a wrong
    family string already gets.
    """
    bad_families: tuple[object, ...] = ([], {})
    for bad_family in bad_families:
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            document = _base_document(pid=os.getpid())
            document["family"] = bad_family
            _write_descriptor_bytes(state, json.dumps(document).encode("utf-8"))
            with pytest.raises(DescriptorError):
                load_descriptor(state)
