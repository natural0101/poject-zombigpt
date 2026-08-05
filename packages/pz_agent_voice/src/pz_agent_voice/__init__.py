"""Voice companion adapters.

Defines the transport-agnostic ``VoiceAdapter`` protocol plus concrete
implementations. Core never imports this package: speech is an interface to the
agent, not part of its decision loop.

The package holds two guarantees that are worth naming here because they are
what the modules are arranged around.

**Stop bypasses everything.** :meth:`~pz_agent_voice.session.VoiceSession.handle`
tests for a stop word before the wake gate, before the interim/final test, before
the confidence threshold and before classification, and the driver cancels
speech before it classifies at all. There is no state — idle, listening,
mid-sentence, mid-plan — from which speaking "стоп" fails to reach
:meth:`~pz_agent_mcp.ports.SessionPort.stop`.

**A transcript is data.** The only thing that crosses from the microphone to the
planner is a member of :class:`~pz_agent_voice.messages.VoiceGoal`, submitted as
a :class:`~pz_agent_mcp.ports.PlanRequest` through the same
:class:`~pz_agent_mcp.ports.PlanPort` every other caller uses. Transcript text is
matched against a closed vocabulary and then discarded; it is never forwarded,
stored or spoken back.
"""

from __future__ import annotations

from pz_agent_core.version import PRODUCT_VERSION

from .adapter import SpeechCancelled, VoiceAdapter
from .adapters import FakeVoiceAdapter, TeamONVoiceAdapter
from .config import DEFAULT_VOICE_CONFIG, VoiceConfig
from .driver import VoiceCompanion
from .events import TtsEvent, TtsEventKind, TtsEventStream, TtsListener
from .intent import IntentMatch, classify, is_stop
from .messages import OutputKind, VoiceGoal, VoiceInput, VoiceIntent, VoiceOutput
from .ports import VoiceServices
from .queue import UtteranceQueue
from .session import VoiceSession
from .state import VoiceState, VoiceTurn

__all__ = [
    "DEFAULT_VOICE_CONFIG",
    "PRODUCT_VERSION",
    "FakeVoiceAdapter",
    "IntentMatch",
    "OutputKind",
    "SpeechCancelled",
    "TeamONVoiceAdapter",
    "TtsEvent",
    "TtsEventKind",
    "TtsEventStream",
    "TtsListener",
    "UtteranceQueue",
    "VoiceAdapter",
    "VoiceCompanion",
    "VoiceConfig",
    "VoiceGoal",
    "VoiceInput",
    "VoiceIntent",
    "VoiceOutput",
    "VoiceServices",
    "VoiceSession",
    "VoiceState",
    "VoiceTurn",
    "classify",
    "is_stop",
]
