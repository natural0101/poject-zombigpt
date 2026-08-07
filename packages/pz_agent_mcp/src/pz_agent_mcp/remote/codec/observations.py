"""The observation, across the process boundary.

Thin on purpose. :class:`~pz_agent_core.protocol.Observation` already has
``to_dict``/``from_dict``, because the same document travels between the Lua mod
and the sidecar and is pinned by ``schemas/observation.schema.json``. A second
encoding here would be a second definition of the largest structure in the
system, and the two would drift in the direction nobody tests: the mod↔sidecar
pair is exercised by the contract suite, this one would not be.

So this module contributes exactly one thing the protocol's own pair does not:
the *absence* of an observation. ``ObservationPort.latest()`` returns ``None``
before the first one arrives, and ``None`` is not a value ``from_dict`` can
produce. Encoding it as a missing key rather than as ``{}`` matters — an empty
object decodes as "an observation with every field missing", which is a decode
error, and the caller would learn "the answer was malformed" when the truth is
"there is nothing to report yet".
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pz_agent_core.protocol import JsonDict, Observation
from pz_agent_mcp.remote.codec import CodecError, require_mapping

__all__ = ["decode_latest_observation", "encode_latest_observation"]

#: The key the observation sits under. Present only when there is one.
_FIELD = "observation"


def encode_latest_observation(observation: Observation | None) -> JsonDict:
    """The result body for ``observation.latest``."""
    if observation is None:
        return {}
    return {_FIELD: observation.to_dict()}


def decode_latest_observation(payload: Mapping[str, Any]) -> Observation | None:
    """Read that body back, distinguishing "none yet" from "unreadable".

    Raises:
        CodecError: the key is present but is not an observation. Deliberately
            *not* raised when the key is absent: that is the documented "before
            the first observation arrives" answer, and turning it into an error
            would make a fresh session look like a broken link.
    """
    if _FIELD not in payload:
        return None
    document = require_mapping(payload, _FIELD, where="observation.latest")
    try:
        return Observation.from_dict(document)
    except (ValueError, TypeError, KeyError) as exc:
        # `from_dict` raises the protocol's own errors, whose text can quote a
        # field value. Replaced rather than chained through so nothing from the
        # payload reaches a traceback; the original is still the cause.
        raise CodecError("observation.latest: the observation could not be read") from exc
