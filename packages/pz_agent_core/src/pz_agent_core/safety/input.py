"""The gate a synthetic-input fallback has to pass, written before there is one.

§16 keeps an experimental visual/input adapter available for the actions a Lua
timed action cannot perform: a virtual gamepad or keyboard, driven from a
separate package, behind ``[experimental_input] enabled = false``. Nothing in
this build implements any of it, and this module implements none of it either —
it decides only whether such an adapter would be *permitted* to act, which is a
different question from whether one exists.

Writing the rule now rather than beside the first adapter is deliberate. "There
is no code that sends input" is a true sentence about today and not a safety
property: it stops holding the moment somebody writes that code, and the
constraint it was standing in for — **никакого synthetic input в multiplayer** —
is the one this project may not get wrong. A virtual gamepad on another
person's server is an input cheat whatever it was meant for, the server cannot
be told otherwise, and no anti-cheat distinguishes a helpful one. So the refusal
lives in one function that any such adapter must call, and it refuses by
default.

Three conditions, all of which must hold, and only one of them is about
convenience:

* **The mod read this session as single player.** ``None`` is refused exactly as
  ``True`` is. An absent reading is not a negative one — the same rule that stops
  a missing ``is_bleeding`` from meaning "not bleeding" — and a safety gate is
  the last place to read silence as permission. There is deliberately no setting
  that lifts this; a gate with an override beside it is not a gate.
* **The operator turned the adapter on.** §16.2's flag, off unless it is set.
* **The session is in the mode that is about it.** Authority in
  ``EXPERIMENTAL_INPUT`` is granted explicitly and never inherited from
  ``ASSISTED`` or ``AUTONOMOUS``.

Pure, like the rest of this package: no IO, no globals, no clock. The decision
is a function of an observation the mod published and a flag the operator set.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..protocol import Observation, ReasonCode, SessionMode

__all__ = [
    "SyntheticInputDecision",
    "SyntheticInputRequest",
    "evaluate_synthetic_input",
]


@dataclass(frozen=True, slots=True)
class SyntheticInputRequest:
    """What the gate needs to know, and where each part of it came from.

    ``multiplayer`` is the mod's own reading, carried through
    :meth:`from_observation` rather than passed by a caller that formed its own
    opinion: the caller is the adapter that wants to act, which is the last
    component whose view of the session should decide this.
    """

    mode: SessionMode
    multiplayer: bool | None
    enabled: bool = False

    @classmethod
    def from_observation(cls, observation: Observation, *, enabled: bool) -> SyntheticInputRequest:
        """Read the session out of the observation the adapter would act on."""
        return cls(
            mode=observation.safety.mode,
            multiplayer=observation.game.multiplayer,
            enabled=enabled,
        )


@dataclass(frozen=True, slots=True)
class SyntheticInputDecision:
    """Whether a synthetic-input adapter may act, and why not when it may not."""

    allowed: bool
    detail: str
    reason_code: ReasonCode | None = None

    def __post_init__(self) -> None:
        if not self.detail.strip():
            raise ValueError("a synthetic-input decision must explain itself")
        if self.allowed and self.reason_code is not None:
            raise ValueError("an allowance is not a refusal and carries no reason code")
        if not self.allowed and self.reason_code is None:
            raise ValueError("a refusal must carry a reason code")


def evaluate_synthetic_input(request: SyntheticInputRequest) -> SyntheticInputDecision:
    """Decide whether *request* may drive the character with synthetic input.

    The multiplayer reading is checked first so that the refusal a user is most
    likely to hit is also the one they are told about, and so that no later
    condition can be the reason an unread session was allowed.
    """
    if request.multiplayer is not False:
        seen = (
            "this is a multiplayer session"
            if request.multiplayer
            else (
                "the mod did not report whether this session is multiplayer, and an absent "
                "reading is not a negative one"
            )
        )
        return SyntheticInputDecision(
            allowed=False,
            detail=f"{seen}; synthetic input is refused there and no setting permits it",
            reason_code=ReasonCode.POLICY_DENIED,
        )
    if not request.enabled:
        return SyntheticInputDecision(
            allowed=False,
            detail="the experimental input adapter is not enabled",
            reason_code=ReasonCode.POLICY_DENIED,
        )
    if request.mode is not SessionMode.EXPERIMENTAL_INPUT:
        return SyntheticInputDecision(
            allowed=False,
            detail=(
                f"the session is in {request.mode.value}; synthetic input is only ever "
                f"permitted in {SessionMode.EXPERIMENTAL_INPUT.value}"
            ),
            reason_code=ReasonCode.POLICY_DENIED,
        )
    return SyntheticInputDecision(
        allowed=True,
        detail=(
            f"single player, {SessionMode.EXPERIMENTAL_INPUT.value}, and the adapter is enabled"
        ),
    )
