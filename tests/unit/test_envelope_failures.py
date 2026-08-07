"""What an MCP client is told when the link to the core fails.

The core is always in another process — an MCP client launches ``pz-agent-mcp``
as a subprocess — so "the call did not reach a record" is three different
events, and :mod:`pz_agent_mcp.remote.client` keeps them three different
exceptions. This file is about the last step of that journey: the
:class:`~pz_agent_mcp.envelope.ToolFailure` a client actually receives.

The distinction has to survive that step or it was never made. A client branches
on ``reason_code``; the type name inside ``message`` is prose, and prose is not
a branch. So the claims here are:

* each kind gets its **own** code — a refusal is not a dead sidecar;
* the ordering of the table cannot silently take that back, which is the one
  way this bug returns wearing a different hat: an entry for the shared base
  class :class:`~pz_agent_mcp.remote.client.RemoteCoreError` answers for all
  three;
* a fourth kind added later **fails a test** rather than quietly becoming
  ``INTERNAL_ERROR``, so the completeness check is driven off the class
  hierarchy and is itself checked for teeth;
* the core's own words are relayed and nothing else is added — in particular no
  path and no token, both of which sit in the state directory these failures are
  discovered in.

The kinds are fabricated here rather than provoked over a socket. Which
condition raises which kind is settled at length in
``tests/unit/test_remote_core_services.py`` and
``tests/contract/test_remote_core_round_trip.py``, against a real server on a
real transport; repeating it would test that suite rather than this mapping. The
one exception is the dead sidecar, which needs no server to be real — a state
directory with no sidecar in it is exactly the first thing a user runs into —
and so is driven through the real :class:`RemoteCoreServices` end to end.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import pytest

from pz_agent_core.actions import PreconditionFailed
from pz_agent_core.protocol import RETRYABLE_CODES, JsonDict, ReasonCode
from pz_agent_core.rpc.descriptor import RpcDescriptor, runtime_dir, write_descriptor
from pz_agent_core.rpc.token import issue_token
from pz_agent_core.rpc.transport import local_family, new_address
from pz_agent_core.rpc.wire import ErrorCode
from pz_agent_mcp.envelope import (
    MAPPED_EXCEPTIONS,
    MAX_MESSAGE_CHARS,
    ToolFailure,
    failure_from,
    is_retryable,
)
from pz_agent_mcp.remote.client import (
    SIDECAR_NOT_RUNNING,
    SIDECAR_REMEDY,
    CoreAnswerUnreadable,
    CoreRefused,
    RemoteCoreError,
    RemoteCoreServices,
    SidecarUnavailable,
)

#: Long enough for a loaded runner, short enough that a hang ends one test.
GRACE: Final = 30.0

#: The sentence the core sends back when it declines to arm. Written out here
#: rather than imported, because the claim is that *the core's* words arrive
#: unaltered, and a constant shared with the producer would arrive unaltered by
#: construction.
CORE_WORDS: Final = "a verified backup is required before arming; run 'pz-agent backup-save'"

#: A refusal longer than the envelope will carry. The core's message is the one
#: field on this path whose length this process does not choose — a doctor
#: report or a listed precondition can run to a paragraph — so the clip is
#: reachable in production and is exercised with an input that reaches it.
#:
#: A fixed literal rather than ``"x" * MAX_MESSAGE_CHARS * 2``: an input scaled
#: off the bound grows when the bound grows, so a raised bound would still be
#: exceeded and the tests below would pass while nothing was clipped. This is
#: 882 characters no matter what the module says the bound is.
OVERLONG_CORE_WORDS: Final = (
    "the core will not arm: " + "there is no verified backup for this save. " * 20
)

#: The code each kind must carry, stated independently of the table it is
#: checking. A table edited without a reason to edit this fails here.
EXPECTED_CODES: Final[Mapping[type[RemoteCoreError], ReasonCode]] = {
    CoreRefused: ReasonCode.PRECONDITION_FAILED,
    SidecarUnavailable: ReasonCode.STALE_SESSION,
    CoreAnswerUnreadable: ReasonCode.INTERNAL_ERROR,
}


#: The same three, in a stable order, for parametrising. Sorted by name so the
#: ids a run prints do not depend on dict order, and annotated here rather than
#: inlined into ``parametrize`` — whose argument is typed as an iterable of
#: ``object``, which would erase the class and take ``__name__`` with it.
EVERY_KIND: Final[tuple[type[RemoteCoreError], ...]] = tuple(
    sorted(EXPECTED_CODES, key=lambda kind: kind.__name__)
)


def a_refusal(detail: str = CORE_WORDS) -> CoreRefused:
    """What :meth:`CoreLink.result` raises when the core answers ``CORE_REFUSED``."""
    return CoreRefused("session.arm", ErrorCode.CORE_REFUSED, detail)


def a_sidecar_side_defect(method: str = "session.arm") -> CoreRefused:
    """A ``CORE_REFUSED`` that is not a refusal at all.

    ``CoreRouter.__call__`` catches bare ``Exception`` around the port call and
    answers ``CORE_REFUSED`` with ``f"{method}: {TypeName}: {exc}"``, and the
    transport does the same if the router itself ever raised
    (``docs/CORE_RPC.md``, "Failure is an answer"). So an ``AttributeError`` in
    a port arrives on this side wearing the same code as "make a backup first",
    and this is the phrasing it arrives in.
    """
    return CoreRefused(
        method,
        ErrorCode.CORE_REFUSED,
        f"{method}: AttributeError: 'NoneType' object has no attribute 'mode'",
    )


def a_dead_link(method: str = "session.status") -> SidecarUnavailable:
    """What :meth:`CoreLink._unavailable` raises, in its shipped phrasing."""
    return SidecarUnavailable(
        f"{method}: {SIDECAR_NOT_RUNNING} (core-rpc.json: no descriptor); {SIDECAR_REMEDY}"
    )


def an_unreadable_answer(method: str = "session.status") -> CoreAnswerUnreadable:
    """What :meth:`CoreLink.result` raises for an answer from another build."""
    return CoreAnswerUnreadable(
        f"{method}: the sidecar answered {ErrorCode.UNKNOWN_METHOD} instead of a result "
        f"(the two halves are from different installs, so reinstall both)"
    )


def one_of_each() -> tuple[RemoteCoreError, ...]:
    return (a_refusal(), a_dead_link(), an_unreadable_answer())


def a_dead_pid() -> int:
    """A pid nothing holds: spawned and reaped, never guessed at."""
    child = subprocess.Popen([sys.executable, "-c", ""])
    child.wait(timeout=GRACE)
    return child.pid


def kinds_below(root: type[BaseException]) -> frozenset[type[BaseException]]:
    """Every subclass of *root*, however deep.

    The point of walking the hierarchy rather than listing three names: a fourth
    kind is a class, and a class cannot be added without appearing here.
    """
    found: set[type[BaseException]] = set()
    for kind in root.__subclasses__():
        found.add(kind)
        found |= kinds_below(kind)
    return frozenset(found)


def unmapped_kinds(
    table: tuple[tuple[type[Exception], ReasonCode], ...],
) -> frozenset[type[BaseException]]:
    """The link failures *table* has no entry of its own for."""
    entries = {kind for kind, _ in table}
    return frozenset(kind for kind in kinds_below(RemoteCoreError) if kind not in entries)


class TestThreeKindsThreeCodes:
    """A refusal, a dead sidecar and an unreadable answer never share a code."""

    @pytest.mark.parametrize("exc", one_of_each(), ids=lambda e: type(e).__name__)
    def test_each_kind_carries_the_code_its_remedy_belongs_to(self, exc: RemoteCoreError) -> None:
        assert failure_from(exc).reason_code is EXPECTED_CODES[type(exc)]

    def test_the_three_codes_are_three(self) -> None:
        """The whole point, and the assertion an entry for the base class kills.

        ``RemoteCoreError`` is the parent of all three, so one table entry for
        it placed first would make every link failure answer with one code —
        which is the ``INTERNAL_ERROR`` bug again in a shape that looks
        deliberate.
        """
        codes = {failure_from(exc).reason_code for exc in one_of_each()}

        assert len(codes) == 3

    def test_a_crash_in_a_port_arrives_wearing_the_refusal_code(self) -> None:
        """The one thing ``PRECONDITION_FAILED`` is bought with, stated out loud.

        ``CORE_REFUSED`` does not mean "the core decided against this". It means
        "a port raised", and the sidecar's router catches bare ``Exception`` to
        produce it — so a genuine defect in a port is indistinguishable, on this
        side, from a precondition the engine declined. Nothing in the envelope
        can tell them apart: the wire carries one code and a sentence, and
        branching on the type name inside that sentence would be the prose-as-a-
        branch mistake this whole file exists to undo.

        The trade is deliberate and it is worth writing down which way it goes.
        Real refusals — no backup, game not running — are the overwhelming
        majority of ``CORE_REFUSED``, and telling a user their missing backup is
        an ``INTERNAL_ERROR`` is the bug that was just removed. The cost is this
        case: a crash reported as a refusal, with the exception's own type name
        relayed verbatim in the message so a reader is not left guessing, and
        not retryable either way, so nothing spins on it.

        If that ever stops being the right side of the trade, the fix is not
        here — it is a distinct ``ErrorCode`` for "a port raised something
        nobody wrote a refusal for", set in ``pz_agent_mcp.remote.server``.
        """
        failure = failure_from(a_sidecar_side_defect())

        assert failure.reason_code is ReasonCode.PRECONDITION_FAILED
        assert failure.retryable is False
        # The one mitigation: what actually happened survives into the sentence.
        assert "AttributeError" in failure.message

    def test_a_refusal_is_not_the_code_for_a_dead_sidecar(self) -> None:
        # Collapsing these two is the expensive direction: it tells a user to
        # restart a sidecar whose session is perfectly healthy.
        assert failure_from(a_refusal()).reason_code is not failure_from(a_dead_link()).reason_code

    def test_no_link_failure_is_advertised_as_retryable(self) -> None:
        """None of the three gets better by being tried again on its own.

        A dead sidecar needs a process started, a refusal needs the condition
        the core named, and an unreadable answer needs an install fixed. A
        ``retryable`` flag would have a client spin instead of report.
        """
        for exc in one_of_each():
            failure = failure_from(exc)
            assert failure.retryable is False
            assert failure.retryable is is_retryable(failure.reason_code)
            assert failure.reason_code not in RETRYABLE_CODES


class TestTheTableCannotSwallowASubclass:
    """Ordering is behaviour here, not tidiness."""

    def test_no_entry_precedes_a_subclass_of_itself(self) -> None:
        """The general form of the bug, checked over the whole table.

        ``failure_from`` returns on the first ``isinstance`` match, so an entry
        above one of its own subclasses answers for it and the subclass's code
        becomes unreachable. This fails the moment someone prepends a base
        class — ``RemoteCoreError`` being the one that is sitting right there.
        """
        kinds = [kind for kind, _ in MAPPED_EXCEPTIONS]

        for above, kind in enumerate(kinds):
            for below in kinds[above + 1 :]:
                assert not issubclass(below, kind), (
                    f"{below.__name__} is unreachable: {kind.__name__} matches it first"
                )

    def test_the_shared_base_class_has_no_entry_of_its_own(self) -> None:
        """Not even last.

        Placed last it would pass the ordering check above and still be wrong:
        a fourth kind would inherit its code and look classified, instead of
        surfacing as the unclassified defect it is.
        """
        assert RemoteCoreError not in {kind for kind, _ in MAPPED_EXCEPTIONS}

    def test_each_kind_is_named_individually(self) -> None:
        entries = {kind for kind, _ in MAPPED_EXCEPTIONS}

        assert entries.issuperset(EXPECTED_CODES)


class TestNoKindGoesUnclassified:
    """Driven off the hierarchy, so a fourth kind fails rather than defaults."""

    def test_every_remote_core_error_subclass_has_an_entry(self) -> None:
        assert unmapped_kinds(MAPPED_EXCEPTIONS) == frozenset()

    def test_the_hierarchy_is_the_three_this_file_knows_about(self) -> None:
        # If this fails, a kind was added: give it an entry and a code, and add
        # it to EXPECTED_CODES with the remedy it implies. It failing is the
        # feature.
        assert kinds_below(RemoteCoreError) == frozenset(EXPECTED_CODES)

    @pytest.mark.parametrize("dropped", EVERY_KIND)
    def test_the_completeness_check_notices_a_missing_entry(
        self, dropped: type[RemoteCoreError]
    ) -> None:
        """Proof the check above has teeth.

        Without this, a completeness test that could never fail would read
        exactly like one that can.
        """
        thinned = tuple((kind, code) for kind, code in MAPPED_EXCEPTIONS if kind is not dropped)

        assert unmapped_kinds(thinned) == frozenset({dropped})


class TestTheCoresOwnWordsAreRelayed:
    """The boundary must not re-decide what the core decided."""

    def test_a_refusal_carries_the_message_the_core_sent(self) -> None:
        failure = failure_from(a_refusal())

        assert CORE_WORDS in failure.message

    def test_a_refusal_is_not_dressed_up_as_a_boundary_exception(self) -> None:
        """Relayed the way ``PreconditionFailed`` is: no class-name prefix.

        The type name was the only thing distinguishing these before, and it
        distinguished them in prose. Now the code does it, and the message is
        spent on what the core said.
        """
        failure = failure_from(a_refusal())

        assert not failure.message.startswith("CoreRefused")
        assert failure.message.startswith("session.arm:")

    def test_a_refusal_never_says_the_sidecar_is_not_running(self) -> None:
        failure = failure_from(a_refusal())

        assert SIDECAR_NOT_RUNNING not in failure.message
        assert "pz-agent start" not in failure.message

    def test_an_unreadable_answer_is_neither_of_the_other_two(self) -> None:
        failure = failure_from(an_unreadable_answer())

        assert SIDECAR_NOT_RUNNING not in failure.message
        assert "the core refused" not in failure.message
        assert ErrorCode.UNKNOWN_METHOD in failure.message

    def test_a_local_precondition_failure_still_keeps_the_engines_own_code(self) -> None:
        # The relay rule this one established must not have moved when the
        # link failures joined it.
        failure = failure_from(
            PreconditionFailed("no such square", reason_code=ReasonCode.TARGET_NOT_LOADED)
        )

        assert failure.reason_code is ReasonCode.TARGET_NOT_LOADED
        assert failure.message == "no such square"

    def test_an_exception_nobody_wrote_a_message_for_keeps_its_type_name(self) -> None:
        """The other half of the relay rule, which must not have been widened.

        A bare ``ZeroDivisionError`` says nothing on its own, so the type name
        is the whole diagnostic and is still attached.
        """
        failure = failure_from(ZeroDivisionError("boom"))

        assert failure.reason_code is ReasonCode.INTERNAL_ERROR
        assert failure.message == "ZeroDivisionError: boom"


class TestNothingLeaksIntoTheMessage:
    """These failures are found in the state directory. Nothing from it travels."""

    def test_a_missing_sidecar_reaches_a_client_as_a_stale_session_with_a_remedy(
        self, tmp_path: Path
    ) -> None:
        """End to end through the real client: no server, because there is none.

        This is the first thing a user meets — an MCP client launching
        ``pz-agent-mcp`` before the sidecar exists — and it is the case the old
        mapping answered with ``INTERNAL_ERROR``.
        """
        state_dir = tmp_path / "profile-name" / "pz-agent"
        services = RemoteCoreServices.from_state_dir(state_dir)

        with pytest.raises(SidecarUnavailable) as raised:
            services.session.status()
        payload = failure_from(raised.value).to_payload(
            tool="pz_session_status", request_id="req-1"
        )

        assert payload["reason_code"] == ReasonCode.STALE_SESSION.value
        assert payload["retryable"] is False
        assert SIDECAR_NOT_RUNNING in str(payload["message"])
        assert "pz-agent start" in str(payload["message"])

    def test_a_missing_sidecar_never_names_the_state_directory(self, tmp_path: Path) -> None:
        """A state directory runs through the profile, and so through a name."""
        state_dir = tmp_path / "profile-name" / "pz-agent"
        services = RemoteCoreServices.from_state_dir(state_dir)

        with pytest.raises(SidecarUnavailable) as raised:
            services.session.status()
        rendered = _rendered(failure_from(raised.value))

        assert str(state_dir) not in rendered
        assert "profile-name" not in rendered
        assert os.sep not in rendered

    def test_a_stale_sidecar_never_quotes_the_token(self, tmp_path: Path) -> None:
        """The one value in that directory that is a secret.

        A dead pid rather than a missing file, so the descriptor and the token
        are both really there and really read before the refusal is written.
        The address is this platform's own — ``new_address`` picks the pipe on
        Windows and the socket path on POSIX — so the assertion that it does not
        travel is about the form actually shipped, not a POSIX-shaped string
        written by hand.
        """
        state_dir = tmp_path / "stale"
        runtime = runtime_dir(state_dir)
        runtime.mkdir(parents=True)
        token = issue_token(runtime)
        address = new_address(runtime)
        write_descriptor(
            state_dir,
            RpcDescriptor(address=address, family=local_family(), pid=a_dead_pid()),
        )

        with pytest.raises(SidecarUnavailable) as raised:
            RemoteCoreServices.from_state_dir(state_dir).session.status()
        rendered = _rendered(failure_from(raised.value))

        assert token.hex() not in rendered
        assert str(token) not in rendered
        assert address not in rendered
        assert str(tmp_path) not in rendered
        # The remedy sits at the *end* of this sentence and the cause is spliced
        # into the middle, so a longer cause is what would push it past the
        # bound and clip it off — leaving a user a code and no instruction. The
        # stale phrasing is the longest of the no-server causes, so it is the
        # one worth pinning.
        assert SIDECAR_REMEDY in _rendered_message(failure_from(raised.value))

    @pytest.mark.parametrize("exc", one_of_each(), ids=lambda e: type(e).__name__)
    def test_every_shipped_phrasing_arrives_whole(self, exc: RemoteCoreError) -> None:
        """The three as they are actually worded: relayed, not clipped.

        The second assertion is what makes the first one mean something. Every
        shipped phrasing is comfortably inside the bound, so ``==`` here is a
        statement about the relay; if a phrasing ever grew past the bound this
        would fail rather than quietly becoming a test of the clipper.
        """
        failure = failure_from(exc)

        assert failure.message == str(exc)
        assert len(str(exc)) < MAX_MESSAGE_CHARS

    def test_a_refusal_too_long_to_relay_is_clipped_to_the_bound(self) -> None:
        """The bound itself, exercised — which needs an input that exceeds it.

        Checked with a message that reaches the bound, rather than with the
        three shipped phrasings, none of which is two thirds of it. An
        assertion of ``<= MAX_MESSAGE_CHARS`` against those would hold whatever
        the clipper did — including doing nothing at all.
        """
        exc = a_refusal(OVERLONG_CORE_WORDS)
        assert len(str(exc)) > MAX_MESSAGE_CHARS

        failure = failure_from(exc)

        assert len(failure.message) == MAX_MESSAGE_CHARS
        assert failure.message != str(exc)
        # Clipped, and visibly so: a message that just stopped would read as the
        # core trailing off mid-sentence rather than as a message that was cut.
        assert failure.message.endswith("…")
        # The front is what survives, because the core says what it wants first.
        assert failure.message.startswith("session.arm: the core refused (the core will not arm:")

    def test_the_clip_is_what_a_client_receives(self) -> None:
        """Bounded in the document, not only on the exception.

        ``to_payload`` is what crosses to a client, and a bound applied to an
        attribute nobody serialises would bound nothing.
        """
        exc = a_refusal(OVERLONG_CORE_WORDS)

        rendered = _rendered_message(failure_from(exc))

        assert len(rendered) == MAX_MESSAGE_CHARS
        assert len(rendered) < len(str(exc))


def test_the_envelope_still_imports_on_its_own(tmp_path: Path) -> None:
    """The module-level import of the remote client is not a cycle.

    ``envelope`` is imported by ``pz_agent_mcp/__init__``, by the router and by
    the resource reader, and it now imports ``pz_agent_mcp.remote.client`` at
    module scope to name the three kinds in its table. A fresh interpreter
    importing the module first is the check that this is not resting on the
    package's own import order — inside the suite, something has always
    imported the package already.
    """
    proc = subprocess.run(
        [sys.executable, "-c", "import pz_agent_mcp.envelope"],
        # Not `check=True`: a raised CalledProcessError prints the return code
        # and swallows the traceback, and the traceback is the whole diagnostic
        # when an import cycle is what failed.
        check=False,
        capture_output=True,
        text=True,
        timeout=GRACE,
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)},
    )

    assert proc.returncode == 0, proc.stderr


def _rendered(failure: ToolFailure) -> str:
    """Everything a client would see, as one string to search."""
    return repr(_payload(failure))


def _rendered_message(failure: ToolFailure) -> str:
    """Only the sentence, for claims about what a user is told to do.

    Separate from :func:`_rendered` because the leakage checks want the whole
    document — a secret in ``diagnostics`` is a secret — while a remedy has to
    be in the one field a client shows.
    """
    return str(_payload(failure)["message"])


def _payload(failure: ToolFailure) -> JsonDict:
    return failure.to_payload(tool="pz_session_status", request_id="req-1")
