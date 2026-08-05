"""Concrete :class:`~pz_agent_voice.adapter.VoiceAdapter` implementations.

Two of them, and the split is the same one the MCP boundary makes: the fake is
complete, deterministic and needs nothing installed, so the whole conversation
can be exercised anywhere; the TeamON plugin is written against an injected
client interface, so it too is exercised without the vendor SDK present.
"""

from __future__ import annotations

from .fake import FakeVoiceAdapter
from .teamon import (
    TEAMON_SDK_MODULE,
    TeamONClient,
    TeamONSdkUnavailable,
    TeamONSpeech,
    TeamONTranscript,
    TeamONVoiceAdapter,
    require_teamon_sdk,
    teamon_sdk_available,
)

__all__ = [
    "TEAMON_SDK_MODULE",
    "FakeVoiceAdapter",
    "TeamONClient",
    "TeamONSdkUnavailable",
    "TeamONSpeech",
    "TeamONTranscript",
    "TeamONVoiceAdapter",
    "require_teamon_sdk",
    "teamon_sdk_available",
]
