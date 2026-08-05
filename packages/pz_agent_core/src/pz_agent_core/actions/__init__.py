"""The action engine: one command, one transaction, one honest terminal result.

The lifecycle is ``validate → prepare → enqueue → observe → verify → finalise``
with ``timeout / cancel / fail`` as the alternative exits. It is split three
ways: :mod:`.adapter` states what an action must be able to tell the engine,
:mod:`.engine` drives the lifecycle against injected ports, and :mod:`.builtin`
implements the actions that need no game-specific API.
"""

from __future__ import annotations

from .adapter import (
    ActionAdapter,
    AdapterRegistry,
    DuplicateAdapterError,
    Evidence,
    PreconditionFailed,
    ProgressReporter,
    UnknownActionError,
)
from .builtin import CancelAdapter, WaitAdapter, register_builtins
from .engine import (
    ActionEngine,
    ActionRequest,
    CommandSink,
    Dispatch,
    ObservationSource,
    deny_capability,
    no_panic,
)

__all__ = [
    "ActionAdapter",
    "ActionEngine",
    "ActionRequest",
    "AdapterRegistry",
    "CancelAdapter",
    "CommandSink",
    "Dispatch",
    "DuplicateAdapterError",
    "Evidence",
    "ObservationSource",
    "PreconditionFailed",
    "ProgressReporter",
    "UnknownActionError",
    "WaitAdapter",
    "deny_capability",
    "no_panic",
    "register_builtins",
]
