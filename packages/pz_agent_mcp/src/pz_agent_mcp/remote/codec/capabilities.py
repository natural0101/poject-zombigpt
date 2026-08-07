"""The capability report, across the process boundary.

Like :mod:`.observations`, thin by choice:
:class:`~pz_agent_core.capabilities.model.CapabilityReport` already has
``to_dict``/``from_dict``, and a second encoding would be a second definition of
which tools the boundary is allowed to publish.

That is not a small thing to duplicate. The report is what decides whether a
tool is listed and callable at all, and §12.4's rule — a static scan yields
``available_unverified`` at best, and only a live acknowledgement promotes a
capability to ``verified`` — lives inside these objects. A codec that rebuilt
them field by field could reconstruct a state the core never granted, and the
boundary would publish a tool the game has not confirmed.

Unlike an observation, a report is never absent: ``CapabilityPort.report()``
returns one always, empty at worst. So this pair has no "nothing yet" case, and
a missing key here is an error rather than a state.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pz_agent_core.capabilities.model import CapabilityReport
from pz_agent_core.protocol import JsonDict
from pz_agent_mcp.remote.codec import CodecError, require_mapping

__all__ = ["decode_capability_report", "encode_capability_report"]

_FIELD = "report"


def encode_capability_report(report: CapabilityReport) -> JsonDict:
    """The result body for ``capabilities.report``."""
    return {_FIELD: report.to_dict()}


def decode_capability_report(payload: Mapping[str, Any]) -> CapabilityReport:
    """Read that body back.

    Raises:
        CodecError: the key is missing or the report cannot be read. There is no
            "no report yet" state to confuse this with — a core that answered
            without one has not told us the capabilities are empty, it has
            failed to answer, and an empty report would silently withdraw every
            tool instead of reporting a broken link.
    """
    document = require_mapping(payload, _FIELD, where="capabilities.report")
    try:
        return CapabilityReport.from_dict(document)
    except (ValueError, TypeError, KeyError) as exc:
        raise CodecError("capabilities.report: the report could not be read") from exc
