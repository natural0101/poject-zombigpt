"""The two codecs that reuse the protocol's own serialisation.

An observation and a capability report both already have `to_dict`/`from_dict`,
because both already cross a boundary — the mod↔sidecar one — and are pinned by
schemas. These codecs add exactly one thing each, and that one thing is what
these tests are about.

For the observation it is **absence**. `ObservationPort.latest()` returns None
before the first one arrives, and None is not something `from_dict` can produce.
Encoding it as a missing key rather than as `{}` is the difference between "there
is nothing to report yet" and "the answer was unreadable", and a boundary that
confuses those tells the user their link is broken when their session is merely
new.

For the report it is the opposite: there is **no** absent state, so a missing key
is an error. An empty report is not the same as no report — an empty one
silently withdraws every tool.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from pz_agent_core.capabilities.model import CapabilityReport
from pz_agent_core.protocol import CapabilityState
from pz_agent_mcp.remote.codec import CodecError
from pz_agent_mcp.remote.codec.capabilities import (
    decode_capability_report,
    encode_capability_report,
)
from pz_agent_mcp.remote.codec.observations import (
    decode_latest_observation,
    encode_latest_observation,
)
from tests.fixtures import make_observation
from tests.fixtures.mcp_doubles import make_report

#: A value that must never appear in an error message. Shaped like a real leak
#: rather than the word "secret", so a substring check is meaningful.
SENTINEL = "C:/Users/Иван/Zomboid/leak.txt"


class TestTheObservation:
    def test_an_observation_survives_the_round_trip(self) -> None:
        observation = make_observation(seq=17)

        decoded = decode_latest_observation(encode_latest_observation(observation))

        assert decoded == observation

    def test_nothing_yet_is_not_an_error(self) -> None:
        """The state a fresh session is in, and the one it is easiest to break."""
        assert decode_latest_observation(encode_latest_observation(None)) is None

    def test_nothing_yet_is_absence_rather_than_an_empty_object(self) -> None:
        """`{}` would decode as "an observation with every field missing".

        Which is a decode error, so the caller would be told the link is broken
        when the truth is that no observation has arrived.
        """
        encoded = encode_latest_observation(None)

        assert encoded == {}
        assert "observation" not in encoded

    def test_the_encoded_form_crosses_a_process_boundary(self) -> None:
        encoded = encode_latest_observation(make_observation())

        assert json.loads(json.dumps(encoded)) == encoded

    def test_a_present_but_unreadable_observation_is_an_error_not_a_none(self) -> None:
        """Otherwise a corrupt answer is indistinguishable from a new session,
        and the boundary reports "no observation yet" forever."""
        with pytest.raises(CodecError):
            decode_latest_observation({"observation": {"seq": 1}})

    @pytest.mark.parametrize(
        "payload", [{"observation": 5}, {"observation": []}, {"observation": "x"}]
    )
    def test_an_observation_that_is_not_an_object_is_refused(self, payload: dict[str, Any]) -> None:
        with pytest.raises(CodecError):
            decode_latest_observation(payload)

    def test_the_refusal_does_not_quote_the_payload(self) -> None:
        """`Observation.from_dict` raises errors that can name a field's value.

        They are replaced rather than chained through, because this string
        reaches a traceback before any redactor sees it.
        """
        with pytest.raises(CodecError) as caught:
            decode_latest_observation({"observation": {"session_id": SENTINEL}})

        assert SENTINEL not in str(caught.value)
        assert "Иван" not in str(caught.value)

    def test_a_full_snapshot_and_a_diff_are_both_preserved(self) -> None:
        """`full` decides whether the reader may treat this as the whole world."""
        for full in (True, False):
            observation = make_observation(full=full)

            decoded = decode_latest_observation(encode_latest_observation(observation))

            assert decoded is not None
            assert decoded.full is full


class TestTheCapabilityReport:
    def test_a_report_survives_the_round_trip(self) -> None:
        report = make_report(usable=["eat_percentage"], unsupported=["drive_vehicle"])

        decoded = decode_capability_report(encode_capability_report(report))

        assert decoded == report

    def test_the_state_of_every_capability_is_preserved(self) -> None:
        """The report decides which tools are published at all.

        A codec that rebuilt these field by field could reconstruct a state the
        core never granted — §12.4 says a static scan yields
        `available_unverified` at best, and only a live acknowledgement promotes
        one to `verified`.
        """
        report = make_report(
            usable=["eat_percentage"],
            unsupported=["drive_vehicle"],
            experimental=["survival_sleep"],
        )

        decoded = decode_capability_report(encode_capability_report(report))

        assert decoded.state("eat_percentage") is report.state("eat_percentage")
        assert decoded.state("drive_vehicle") is CapabilityState.UNSUPPORTED
        assert decoded.state("survival_sleep") is CapabilityState.EXPERIMENTAL
        assert decoded.usable_names() == report.usable_names()

    def test_an_empty_report_round_trips_as_an_empty_report(self) -> None:
        report = CapabilityReport(build="42.20", capabilities=(), revision=0)

        decoded = decode_capability_report(encode_capability_report(report))

        assert decoded == report
        assert decoded.usable_names() == ()

    def test_the_encoded_form_crosses_a_process_boundary(self) -> None:
        encoded = encode_capability_report(make_report(usable=["eat_percentage"]))

        assert json.loads(json.dumps(encoded)) == encoded

    def test_a_missing_report_is_an_error_rather_than_an_empty_one(self) -> None:
        """An empty report silently withdraws every tool.

        A core that answered without one has not told us the capabilities are
        empty; it has failed to answer, and those must not look the same.
        """
        with pytest.raises(CodecError, match="report"):
            decode_capability_report({})

    def test_an_unreadable_report_is_refused(self) -> None:
        with pytest.raises(CodecError):
            decode_capability_report({"report": {"build": "42.20"}})

    def test_the_refusal_does_not_quote_the_payload(self) -> None:
        with pytest.raises(CodecError) as caught:
            decode_capability_report({"report": {"build": SENTINEL}})

        assert SENTINEL not in str(caught.value)

    def test_the_decoded_report_behaves_like_the_one_that_was_sent(self) -> None:
        """Equality is not enough on its own — the report has behaviour.

        `for_build` re-states a report against a different game build and
        downgrades what that build cannot support (§12.6: 42.19 proves nothing
        about 42.20). Asserting the *derived* report matches too is what catches
        a codec that reconstructed the fields but lost something the methods
        read. Deliberately not asserting what `for_build` decides — that is the
        capability model's business and its own tests', and restating it here
        would be a second copy of the policy.
        """
        report = make_report(usable=["eat_percentage"], build="42.20")

        decoded = decode_capability_report(encode_capability_report(report))

        assert decoded.build == "42.20"
        assert decoded.for_build("42.21") == report.for_build("42.21")
        assert decoded.unusable_reasons() == report.unusable_reasons()
