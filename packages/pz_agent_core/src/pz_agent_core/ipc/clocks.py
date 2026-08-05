"""The clock abstraction shared by the IPC and session layers.

Wall-clock milliseconds, not monotonic ones: every timestamp in this subsystem
(``issued_at_ms``, heartbeat ages, lease deadlines) is compared across two
processes — the sidecar and the game — so it has to come from a shared frame of
reference. Durations measured *within* one process, where monotonicity matters,
use :func:`time.monotonic_ns` directly at the call site instead.

Every component takes a :data:`Clock` rather than calling :func:`system_clock_ms`
itself, which is what makes lease expiry and staleness testable without sleeps.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeAlias

#: Returns the current wall-clock time in milliseconds since the Unix epoch.
Clock: TypeAlias = Callable[[], int]


def system_clock_ms() -> int:
    """Default :data:`Clock` implementation."""
    return time.time_ns() // 1_000_000
