"""The bound on the descriptor read, and the refusal that comes with it.

``load_descriptor`` used to read this file whole. The MCP entry point reads the
same file behind an 8 KiB cap and its module comment claims the cap buys "one
bounded read instead of a parse" — which was not true end to end, because the
loader that runs first had no cap at all. Whoever put a gigabyte where the
descriptor goes got a gigabyte read into memory and then decoded.

So the tests here are about bytes, not about round trips. A descriptor that
survives a write and a load says nothing about how much was read to load it, and
every assertion below is written so that reverting to ``path.read_bytes()``
breaks it:

* the accepted and refused sizes are pinned one byte apart, so an off-by-one on
  either side of the cap is a failure rather than a rounding;
* the file that gets refused for its size is otherwise a *perfectly valid*
  descriptor, so a cap applied after the parse would pass its own test and fail
  this one;
* the read itself is watched, and the count of bytes it takes is asserted.

The refusal is checked for what it must not say as carefully as for what it must.
It runs through ``%LOCALAPPDATA%`` on the shipping platform, so the path carries
the user's account name; the message names a constant filename and two integers
and stops there.

That last claim is pinned as a whole sentence rather than as a set of substring
tests, because substring tests cannot see the three ways it goes wrong: the two
numbers swapping places, the remedy going missing, and a *prefix* of the file
being quoted. ``"8192" in message`` holds for all three.
"""

from __future__ import annotations

import json
import os
from pathlib import Path, PureWindowsPath
from types import TracebackType
from typing import IO, Any, Final

import pytest

from pz_agent_core.rpc import descriptor as descriptor_module
from pz_agent_core.rpc.descriptor import (
    DESCRIPTOR_FILENAME,
    DESCRIPTOR_FORMAT,
    FAMILY_PIPE,
    FAMILY_UNIX,
    MAX_DESCRIPTOR_BYTES,
    DescriptorError,
    OversizeDescriptor,
    RpcDescriptor,
    StaleDescriptor,
    descriptor_path,
    load_descriptor,
    runtime_dir,
    write_descriptor,
)
from pz_agent_core.rpc.token import TOKEN_FILENAME, issue_token
from pz_agent_core.version import RPC_PROTOCOL_VERSION

#: Retyped rather than imported. The helpers below write descriptors that the
#: loader must *accept*, so if they were built from the module's own constants a
#: renamed format string would rename both halves at once and every acceptance
#: test here would keep passing against a file no sidecar would ever write.
A_DESCRIPTOR_FORMAT: Final = "pz-agent-core-rpc-descriptor/1"
A_DESCRIPTOR_FILENAME: Final = "core-rpc.json"

#: The address used wherever the address is not what is under test. Short, so
#: that the padding below is what decides the file's size.
AN_ADDRESS: Final = "/tmp/pz.sock"

#: The whole refusal, retyped with the three varying parts left as fields.
#:
#: Retyped for the same reason the format string is: built from the module's own
#: text it would agree with any rewrite of it. Pinned *whole* because the rest of
#: this module tests the message by membership, and membership cannot see the
#: three ways this sentence breaks — the size and the limit swapping places, the
#: remedy being dropped, and a prefix of the file being quoted alongside them.
A_REFUSAL: Final = (
    "{filename}: is {size} bytes and the limit is {limit}; nothing this build writes "
    "there is that large, so that file is not a descriptor and something else is "
    "writing where it goes — take a copy of it before starting the sidecar, which "
    "replaces it"
)


@pytest.fixture
def state(tmp_path: Path) -> Path:
    """A state directory shaped like the one this ships on.

    ``Users/<account>/Zomboid`` is the Windows layout, and the account name in
    the middle is the reason no refusal in this module may quote a path. The
    directories are real ones under ``tmp_path`` so the loader reads a real
    file; only the *shape* is borrowed.
    """
    return tmp_path / "Users" / "jdoe" / "Zomboid" / "pz-agent"


def _live_document(**overrides: object) -> dict[str, object]:
    """A descriptor for this process, as a plain dictionary of literals."""
    document: dict[str, object] = {
        "format": A_DESCRIPTOR_FORMAT,
        "protocol": RPC_PROTOCOL_VERSION,
        "family": FAMILY_UNIX,
        "address": AN_ADDRESS,
        "pid": os.getpid(),
        "token_file": TOKEN_FILENAME,
    }
    document.update(overrides)
    return document


def _write_raw(state: Path, data: bytes) -> Path:
    """Put *data* exactly where the descriptor goes, with a token beside it."""
    issue_token(runtime_dir(state))
    path = runtime_dir(state) / A_DESCRIPTOR_FILENAME
    path.write_bytes(data)
    return path


def _valid_descriptor_of_exactly(size: int) -> bytes:
    """A loadable descriptor, padded with an unknown field to *size* bytes.

    The padding is a field the loader ignores, so the document stays valid at
    every size — which is the point: the file that gets refused further down is
    refused for its size and nothing else.
    """
    document = _live_document(padding="")
    encoded = json.dumps(document, sort_keys=True).encode("utf-8")
    filler = size - len(encoded)
    if filler < 0:
        raise ValueError(f"a descriptor cannot be squeezed below {len(encoded)} bytes")
    document["padding"] = "p" * filler
    out = json.dumps(document, sort_keys=True).encode("utf-8")
    if len(out) != size:
        raise ValueError(f"padded to {len(out)} bytes, not {size}")
    return out


class _WatchedFile:
    """A file handle that records every read the loader asks for."""

    def __init__(self, handle: IO[bytes], reads: list[int]) -> None:
        self._handle = handle
        self._reads = reads

    def read(self, size: int = -1) -> bytes:
        self._reads.append(size)
        return self._handle.read(size)

    def fileno(self) -> int:
        return self._handle.fileno()

    def __enter__(self) -> _WatchedFile:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._handle.close()


def _watch_the_read(monkeypatch: pytest.MonkeyPatch) -> tuple[list[int], list[str]]:
    """Record the sizes and modes with which the descriptor file is opened.

    Only the descriptor: everything else on the path — the token, the directory
    — goes through untouched, so what is recorded is the read under test.
    """
    reads: list[int] = []
    modes: list[str] = []
    original = Path.open

    def opening(self: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if self.name != A_DESCRIPTOR_FILENAME:
            return original(self, mode, *args, **kwargs)
        modes.append(mode)
        return _WatchedFile(original(self, "rb"), reads)

    monkeypatch.setattr(Path, "open", opening)
    return reads, modes


class TestTheCapItself:
    def test_the_cap_is_eight_kibibytes(self) -> None:
        """Retyped, because every size in this file is measured against it.

        The number is not arbitrary: the widest legal address is a Windows pipe
        name, 256 characters after ``\\\\.\\pipe\\``, and the five other fields
        are fixed and short. A descriptor cannot reach a kilobyte, so 8 KiB
        refuses only files that were never descriptors.
        """
        assert MAX_DESCRIPTOR_BYTES == 8192

    def test_the_widest_address_this_project_can_have_fits_far_inside_it(self, state: Path) -> None:
        """The justification for the number, built in the Windows shape.

        Windows is where this ships, and a named pipe is the longest address it
        can record: ``\\\\.\\pipe\\`` and up to 256 more characters, every
        backslash doubled again by JSON. If the cap were derived from the Unix
        socket paths the tests happen to use, this descriptor would be refused
        on the shipping platform and nowhere else.
        """
        address = str(PureWindowsPath("\\\\.\\pipe") / ("pz-agent-core-" + "a" * 242))
        assert len(address) == 265, address

        issue_token(runtime_dir(state))
        write_descriptor(state, RpcDescriptor(address=address, family=FAMILY_PIPE, pid=os.getpid()))
        written = descriptor_path(state).stat().st_size

        assert load_descriptor(state).address == address
        assert written < 1024, "a descriptor got large enough to argue about the cap"
        assert written * 8 <= MAX_DESCRIPTOR_BYTES

    def test_the_cap_is_not_below_the_one_the_mcp_entry_point_reads_with(self) -> None:
        """The two readers of this file must not disagree about its size.

        ``pz_agent_mcp.__main__`` reads a bounded prefix of the same file to
        classify a refusal the loader has already made. If the loader's cap were
        the smaller of the two, a file between the caps would be refused here and
        still parse whole there — and the classifier would read its ``protocol``
        field and send the user looking for a second install.
        """
        entry = pytest.importorskip(
            "pz_agent_mcp.__main__", reason="the MCP package is not importable here"
        )

        assert MAX_DESCRIPTOR_BYTES >= entry.MAX_DESCRIPTOR_BYTES

    def test_the_format_string_the_helpers_write_is_the_one_the_module_expects(self) -> None:
        """Guards the retyped literals above from drifting into vacuity."""
        assert DESCRIPTOR_FORMAT == A_DESCRIPTOR_FORMAT
        assert DESCRIPTOR_FILENAME == A_DESCRIPTOR_FILENAME

    def test_the_cap_and_the_refusal_are_both_published(self) -> None:
        """A refusal a caller cannot name is one they can only catch by accident.

        ``OversizeDescriptor`` exists so that a caller can tell it from a stale
        descriptor and refuse to start a sidecar over the file. A caller that
        cannot import the name has to catch ``DescriptorError`` and go back to
        matching on the message, which is the state this class was added to end.
        """
        assert "OversizeDescriptor" in descriptor_module.__all__
        assert "MAX_DESCRIPTOR_BYTES" in descriptor_module.__all__


class TestTheBoundary:
    def test_a_descriptor_of_exactly_the_cap_is_still_loaded(self, state: Path) -> None:
        """The cap is a limit, not the first refused size."""
        _write_raw(state, _valid_descriptor_of_exactly(MAX_DESCRIPTOR_BYTES))

        assert descriptor_path(state).stat().st_size == 8192
        assert load_descriptor(state).address == AN_ADDRESS

    def test_one_byte_past_the_cap_is_refused(self, state: Path) -> None:
        """And the same document one byte shorter is not, above."""
        _write_raw(state, _valid_descriptor_of_exactly(MAX_DESCRIPTOR_BYTES + 1))

        with pytest.raises(OversizeDescriptor) as caught:
            load_descriptor(state)

        assert "8193" in str(caught.value)
        assert "8192" in str(caught.value)

    def test_a_file_refused_for_its_size_is_otherwise_a_valid_descriptor(self, state: Path) -> None:
        """The check is on the bytes, before the parse — this is how you can tell.

        Everything else about this file is right: our format, our protocol, a
        live pid, a token beside it. A cap that ran after ``json.loads`` would
        accept it, having already paid the cost the cap exists to avoid.
        """
        raw = _valid_descriptor_of_exactly(MAX_DESCRIPTOR_BYTES * 4)
        document = json.loads(raw.decode("utf-8"))
        assert document["format"] == A_DESCRIPTOR_FORMAT
        assert document["pid"] == os.getpid()
        _write_raw(state, raw)

        with pytest.raises(OversizeDescriptor):
            load_descriptor(state)

    def test_a_file_of_line_endings_cannot_shrink_under_the_cap(self, state: Path) -> None:
        """The Windows shape of the same boundary, forced on Linux.

        Text mode folds ``\\r\\n`` to one byte — on Windows by the platform, and
        in Python on every platform through universal newlines. A read that was
        not binary would count this file at roughly half its size and let it
        through to the parser. The file is exactly one byte past the cap on disk,
        and that is the number the refusal has to quote.
        """
        raw = (b"\r\n" * ((MAX_DESCRIPTOR_BYTES + 1) // 2)) + b"\r"
        assert len(raw) == MAX_DESCRIPTOR_BYTES + 1
        _write_raw(state, raw)

        with pytest.raises(OversizeDescriptor) as caught:
            load_descriptor(state)

        assert "8193" in str(caught.value)


class TestTheRefusal:
    def test_it_names_the_size_it_found_and_the_limit(self, state: Path) -> None:
        """The size on disk, not the size of the prefix that was read.

        "8193 bytes" for every oversize file would be a lie about all but one of
        them, and the number is what tells a user whether they are looking at a
        log, a save, or a truncated download.
        """
        _write_raw(state, b"x" * 20_000)

        with pytest.raises(OversizeDescriptor) as caught:
            load_descriptor(state)

        message = str(caught.value)
        assert "20000" in message
        assert "8192" in message
        assert A_DESCRIPTOR_FILENAME in message

    def test_the_refusal_is_that_sentence_and_nothing_else(self, state: Path) -> None:
        """The whole string, with the two numbers in their own places.

        The test above holds just as well for "is 8192 bytes and the limit is
        20000", which is the same two substrings saying the opposite thing, and
        it holds for a message that has quietly lost its second half. That half
        is the entire reason this refusal is not a :class:`StaleDescriptor`:
        ``RpcSupervisor`` clears an unusable descriptor on the way up, so
        "restart the sidecar" destroys the file, and "take a copy first" is the
        only instruction here that a user acts on before doing anything else.
        Nothing else in this module fails if it disappears.
        """
        _write_raw(state, b"x" * 20_000)

        with pytest.raises(OversizeDescriptor) as caught:
            load_descriptor(state)

        assert str(caught.value) == A_REFUSAL.format(
            filename=A_DESCRIPTOR_FILENAME, size=20000, limit=8192
        )

    def test_the_size_it_quotes_is_never_below_what_it_read(
        self, state: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The floor under the ``fstat``, which no other test here would notice.

        The stat happens after the read, so a file truncated in between reports
        fewer bytes than are already in hand — and "is 0 bytes and the limit is
        8192" reads as a broken reader rather than as a fact about the file. The
        race cannot be scheduled, so the shrink is forced.
        """
        _write_raw(state, b"x" * (MAX_DESCRIPTOR_BYTES + 1))
        real_fstat = os.fstat

        def shrunk(fd: int) -> os.stat_result:
            got = tuple(real_fstat(fd))
            return os.stat_result((*got[:6], 0, *got[7:]))

        monkeypatch.setattr(os, "fstat", shrunk)

        with pytest.raises(OversizeDescriptor) as caught:
            load_descriptor(state)

        assert str(caught.value) == A_REFUSAL.format(
            filename=A_DESCRIPTOR_FILENAME, size=MAX_DESCRIPTOR_BYTES + 1, limit=8192
        )

    def test_it_never_names_the_path(self, state: Path) -> None:
        """The path runs through the user's profile and carries their account.

        The account name is checked for first in the path itself: without that,
        an assertion that it is absent from the message would pass on a state
        directory that never contained it.
        """
        path = _write_raw(state, b"x" * (MAX_DESCRIPTOR_BYTES + 1))
        assert "jdoe" in str(path), "the fixture stopped carrying an account name"

        with pytest.raises(OversizeDescriptor) as caught:
            load_descriptor(state)

        message = str(caught.value)
        assert "jdoe" not in message
        assert str(state) not in message
        assert str(path.parent) not in message
        assert "/" not in message
        assert "\\" not in message

    def test_it_is_not_the_answer_a_malformed_descriptor_gets(self, state: Path) -> None:
        """Two files, two refusals, and the remedies point opposite ways.

        A malformed descriptor is a descriptor: restart the sidecar over it. A
        file past the cap is not one, and starting a sidecar on top of it
        destroys whatever was writing there.
        """
        _write_raw(state, b"{ this is not json")

        with pytest.raises(DescriptorError) as malformed:
            load_descriptor(state)

        _write_raw(state, b"x" * (MAX_DESCRIPTOR_BYTES + 1))

        with pytest.raises(DescriptorError) as oversize:
            load_descriptor(state)

        assert not isinstance(malformed.value, OversizeDescriptor)
        assert isinstance(oversize.value, OversizeDescriptor)
        assert str(malformed.value) != str(oversize.value)

    def test_it_is_not_a_stale_descriptor(self, state: Path) -> None:
        """Callers branch on that type to mean "the sidecar stopped".

        ``pz_agent_mcp.__main__`` catches :class:`StaleDescriptor` before
        :class:`DescriptorError` and answers it with "start the sidecar". Nothing
        about an oversize file says the sidecar stopped, and telling someone to
        start one is how the file gets overwritten.
        """
        _write_raw(state, b"x" * (MAX_DESCRIPTOR_BYTES + 1))

        with pytest.raises(OversizeDescriptor) as caught:
            load_descriptor(state)

        assert not isinstance(caught.value, StaleDescriptor)
        assert issubclass(OversizeDescriptor, DescriptorError)

    def test_no_byte_of_the_file_reaches_the_message(self, state: Path) -> None:
        """A file this size is somebody else's data, and it may be anything.

        Checked over every window of the file, not just over the whole secret: a
        refusal that quoted a *prefix* — the first forty bytes, which is what a
        ``{raw[:40]!r}`` in the message would give — contains neither the full
        secret nor a run of the padding, and would pass an assertion written
        only against those two. It would still print the key.
        """
        secret = b"an-api-key-that-happened-to-be-in-that-directory"
        contents = secret + b"q" * MAX_DESCRIPTOR_BYTES
        _write_raw(state, contents)

        with pytest.raises(OversizeDescriptor) as caught:
            load_descriptor(state)

        message = str(caught.value)
        assert secret.decode() not in message
        assert "qqqq" not in message

        #: Eight bytes: long enough that no window of this file is a word the
        #: refusal legitimately contains, short enough to catch a quoted prefix.
        window = 8
        quoted = sorted(
            {
                contents[at : at + window].decode()
                for at in range(len(contents) - window + 1)
                if contents[at : at + window].decode() in message
            }
        )
        assert not quoted, f"the refusal quotes the file: {quoted[:3]}"


class TestTheReadIsBounded:
    def test_the_loader_asks_for_one_byte_more_than_the_cap_and_stops(
        self, state: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One read, and the size it asks for is the whole design.

        The ``+ 1`` is what separates "exactly the cap" from "past it" — without
        it a full 8192-byte read is indistinguishable from the first 8192 bytes
        of something enormous, and the loader would have to stat or read again to
        find out which it had. Asserting the list rather than a bound on it also
        rules out a loop that reads to the end and checks afterwards.
        """
        _write_raw(state, b"z" * (MAX_DESCRIPTOR_BYTES * 3))
        reads, _modes = _watch_the_read(monkeypatch)

        with pytest.raises(OversizeDescriptor):
            load_descriptor(state)

        assert reads == [MAX_DESCRIPTOR_BYTES + 1]

    def test_a_huge_file_is_not_read_past_the_cap(
        self, state: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The property the cap exists for, asserted on the read itself.

        A refusal alone would not show this: ``read_bytes()`` followed by a
        length check refuses the same file, having first pulled all of it into
        memory. So the handle is watched, and what is asserted is the number of
        bytes the loader was willing to take.
        """
        size = 4 * 1024 * 1024
        _write_raw(state, b"z" * size)
        reads, _modes = _watch_the_read(monkeypatch)

        with pytest.raises(OversizeDescriptor):
            load_descriptor(state)

        assert reads, "the descriptor was never opened by this path"
        assert all(requested > 0 for requested in reads), (
            f"an unbounded read: {reads} (a size of -1 or None takes the whole file)"
        )
        assert sum(reads) <= MAX_DESCRIPTOR_BYTES + 1
        assert max(reads) < size

    def test_the_file_is_opened_in_binary_mode(
        self, state: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Because the cap counts bytes and text mode counts characters."""
        _write_raw(state, _valid_descriptor_of_exactly(200))
        _reads, modes = _watch_the_read(monkeypatch)

        assert load_descriptor(state).address == AN_ADDRESS
        assert modes == ["rb"], modes

    def test_a_descriptor_within_the_cap_is_read_in_full(
        self, state: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other half of bounded: a real descriptor is not truncated.

        A cap that read fewer bytes than the file holds would turn every
        descriptor into a malformed one, which is the refusal that sends a user
        to reinstall.
        """
        _write_raw(state, _valid_descriptor_of_exactly(MAX_DESCRIPTOR_BYTES))
        _reads, _modes = _watch_the_read(monkeypatch)

        loaded = load_descriptor(state)

        assert loaded == RpcDescriptor(
            address=AN_ADDRESS,
            family=FAMILY_UNIX,
            pid=os.getpid(),
            protocol=RPC_PROTOCOL_VERSION,
        )

    def test_the_watcher_would_notice_an_unbounded_read(self, state: Path) -> None:
        """A negative control for the two tests above.

        Both of them assert over a list that a broken patch would leave empty,
        and an empty list satisfies ``all(...)`` and ``sum(...) <= cap``. This
        drives the same watcher over ``read_bytes`` — the call the loader used to
        make — and shows the recording that the assertions would then fail on.
        """
        reads: list[int] = []
        path = _write_raw(state, b"z" * (MAX_DESCRIPTOR_BYTES * 2))
        handle = _WatchedFile(path.open("rb"), reads)
        with handle:
            taken = handle.read()

        assert reads == [-1]
        assert len(taken) == MAX_DESCRIPTOR_BYTES * 2
        assert not all(requested > 0 for requested in reads)
        assert sum(reads) > MAX_DESCRIPTOR_BYTES + 1 or reads == [-1]


class TestTheOtherAnswersSurvive:
    """The read was restructured; these are the answers it must still give.

    Their own tests live in ``tests/unit/test_rpc_token_and_descriptor.py``.
    Repeated here only where the change to the read could plausibly break them:
    an absent file and an unreadable one are decided by which exception the new
    ``open`` raises, not by anything the old ``read_bytes`` did.
    """

    def test_no_file_at_all_still_says_the_sidecar_is_not_running(self, state: Path) -> None:
        issue_token(runtime_dir(state))

        with pytest.raises(StaleDescriptor, match="not running"):
            load_descriptor(state)

    def test_an_empty_file_is_malformed_rather_than_oversize(self, state: Path) -> None:
        """Zero bytes is under the cap, and the cap has no opinion about it."""
        _write_raw(state, b"")

        with pytest.raises(DescriptorError) as caught:
            load_descriptor(state)

        assert not isinstance(caught.value, OversizeDescriptor)

    def test_a_directory_where_the_file_goes_is_reported_as_unreadable(self, state: Path) -> None:
        """``open`` fails differently from ``read_bytes`` here on some platforms.

        Either way it is an ``OSError`` and either way the answer must be that
        the file cannot be read — not a traceback out of the loader.
        """
        issue_token(runtime_dir(state))
        (runtime_dir(state) / A_DESCRIPTOR_FILENAME).mkdir()

        with pytest.raises(DescriptorError) as caught:
            load_descriptor(state)

        assert not isinstance(caught.value, OversizeDescriptor)
        assert "cannot be read" in str(caught.value)
