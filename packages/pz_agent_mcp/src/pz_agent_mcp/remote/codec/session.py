"""JSON for the session port's two records.

:class:`~pz_agent_mcp.ports.SessionSnapshot` is the answer to ``session.status``,
``session.arm`` and ``session.disarm``;
:class:`~pz_agent_mcp.ports.StopReport` is the answer to ``session.stop``. Both
travel as flat objects — every field a scalar, no nesting — because both records
are flat and inventing a shape the record does not have would only give the two
halves somewhere to disagree.

**Every field of the record is a required key**, including the four the
dataclass gives a default (``danger_level``, ``capability_revision``,
``game_heartbeat_ok``, ``sidecar_heartbeat_ok``). Those defaults exist so a
caller constructing a snapshot in-process may leave them out; they are not
permission to invent them from a payload that did not carry them. Defaulting
``game_heartbeat_ok`` would turn a renamed key into "the game stopped talking",
and defaulting ``danger_level`` would turn it into :attr:`DangerLevel.NONE` —
each of them a confident, wrong, safe-looking answer where the honest one is
that the payload could not be read. Only the four fields declared ``| None``
(``build``, ``save_id``, ``observation_seq``, ``active_action_id``) may be
absent, and those are encoded as an explicit ``null`` so the object always
carries the full field set; the decoder accepts either the null or the missing
key, since a peer that omits a null is saying the same thing.

Enums travel as ``.value``: ``mode`` as ``"OBSERVE"``, ``danger_level`` as
``"none"``. :func:`~pz_agent_mcp.remote.codec.require_enum` reads them back by
value and refuses an unknown one rather than falling back to a member.

Why ``save_id`` crosses this link
---------------------------------

:class:`~pz_agent_mcp.ports.SessionSnapshot` documents that ``save_id`` "never
leaves this process", and that rule is kept — but the process it means is not
the one this module runs in. The raw save id is a save-directory fragment that
can embed the player's profile name, and §3.13 keeps it off the *published*
boundary: what a tool caller is shown is
:func:`~pz_agent_core.observation.compact.save_scope` of it, a digest, and
:mod:`pz_agent_mcp.router` is where that substitution happens.

This codec sits one layer below that, on a link whose two ends are both ours:
the sidecar and the MCP server, on one machine, over a named pipe or a Unix
socket with no network address to bind, authenticated per run by a token. The
MCP server did not stop needing the raw value when it became a second process —
``_session_status`` computes ``save_scope(session.save_id)`` itself, so a codec
that dropped or pre-digested the field would leave the router with ``None`` and
make it publish "this session has no save" for every session that has one. That
is the failure this whole module is written against: a boundary reporting a
confident absence when it means it could not carry the answer.

So the field is carried verbatim, and the digesting stays exactly where it was,
at the boundary that faces outward. ``test_save_id_survives_verbatim_and_is_not
_digested_here`` pins both halves of that decision: the raw value round-trips,
and this codec emits no ``save_scope`` key — computing one here would quietly
move a security decision out of the module that owns it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pz_agent_core.protocol import DangerLevel, JsonDict, SessionMode
from pz_agent_mcp.ports import SessionSnapshot, StopReport
from pz_agent_mcp.remote.codec import (
    CodecError,
    optional_int,
    optional_str,
    require_bool,
    require_enum,
    require_int,
    require_str,
)

__all__ = [
    "decode_session_snapshot",
    "decode_stop_report",
    "encode_session_snapshot",
    "encode_stop_report",
]

_SNAPSHOT = "session snapshot"
_STOP = "stop report"


def encode_session_snapshot(record: SessionSnapshot) -> JsonDict:
    """*record* as the object to put in an RPC result."""
    return {
        "session_id": record.session_id,
        "mode": record.mode.value,
        "armed": record.armed,
        "connected": record.connected,
        "protocol_version": record.protocol_version,
        "build": record.build,
        "save_id": record.save_id,
        "danger_level": record.danger_level.value,
        "observation_seq": record.observation_seq,
        "capability_revision": record.capability_revision,
        "game_heartbeat_ok": record.game_heartbeat_ok,
        "sidecar_heartbeat_ok": record.sidecar_heartbeat_ok,
        "active_action_id": record.active_action_id,
    }


def decode_session_snapshot(payload: Mapping[str, Any]) -> SessionSnapshot:
    """Read a snapshot from *payload*, or refuse it.

    Raises:
        CodecError: a required field is missing or has the wrong type, or an
            enum carries a value this build does not know.
    """
    return SessionSnapshot(
        session_id=require_str(payload, "session_id", where=_SNAPSHOT),
        mode=require_enum(payload, "mode", SessionMode, where=_SNAPSHOT),
        armed=require_bool(payload, "armed", where=_SNAPSHOT),
        connected=require_bool(payload, "connected", where=_SNAPSHOT),
        protocol_version=require_str(payload, "protocol_version", where=_SNAPSHOT),
        build=optional_str(payload, "build", where=_SNAPSHOT),
        save_id=optional_str(payload, "save_id", where=_SNAPSHOT),
        danger_level=require_enum(payload, "danger_level", DangerLevel, where=_SNAPSHOT),
        observation_seq=optional_int(payload, "observation_seq", where=_SNAPSHOT),
        capability_revision=require_int(payload, "capability_revision", where=_SNAPSHOT),
        game_heartbeat_ok=require_bool(payload, "game_heartbeat_ok", where=_SNAPSHOT),
        sidecar_heartbeat_ok=require_bool(payload, "sidecar_heartbeat_ok", where=_SNAPSHOT),
        active_action_id=optional_str(payload, "active_action_id", where=_SNAPSHOT),
    )


def encode_stop_report(record: StopReport) -> JsonDict:
    """*record* as the object to put in an RPC result."""
    return {
        "cleared": record.cleared,
        "disarmed": record.disarmed,
        "mode": record.mode.value,
    }


def decode_stop_report(payload: Mapping[str, Any]) -> StopReport:
    """Read a stop report from *payload*, or refuse it.

    A negative ``cleared`` is refused, because :class:`StopReport` refuses it.
    The check is the record's own — this function calls the constructor and
    turns its ``ValueError`` into a :class:`CodecError` rather than restating
    ``cleared >= 0`` here, so the codec cannot drift into accepting a report the
    record itself would reject. The chain is dropped (``from None``) because the
    record's message quotes the offending number and this one must not.

    Raises:
        CodecError: a required field is missing, has the wrong type, carries an
            unknown ``mode``, or describes a stop the record refuses to hold.
    """
    cleared = require_int(payload, "cleared", where=_STOP)
    disarmed = require_bool(payload, "disarmed", where=_STOP)
    mode = require_enum(payload, "mode", SessionMode, where=_STOP)
    try:
        return StopReport(cleared=cleared, disarmed=disarmed, mode=mode)
    except ValueError:
        raise CodecError(f"{_STOP}: cleared must be non-negative") from None
