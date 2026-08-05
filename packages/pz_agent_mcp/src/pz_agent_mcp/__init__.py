"""MCP boundary for pz-agent.

A thin adapter over ``pz_agent_core``: it translates MCP tool calls into core
commands and serialises domain errors back out. Policy decisions are made in
core and are never re-implemented here — duplicating them is how a boundary
ends up disagreeing with the engine it fronts.

The layering is what makes the surface testable: everything except
:mod:`.server` imports nothing from the ``mcp`` SDK, which is an optional
dependency. The catalogue, the validator, the redaction and every gate are
therefore exercised whether or not the SDK is installed.
"""

from __future__ import annotations

from pz_agent_core.version import PRODUCT_VERSION

from .catalog import (
    RESOURCES,
    RESOURCES_BY_URI,
    TOOLS,
    TOOLS_BY_NAME,
    ResourceSpec,
    ToolKind,
    ToolSpec,
    published_tools,
    withheld_tools,
)
from .envelope import ToolFailure, ToolOutcome, ToolSuccess, is_retryable
from .idempotency import IdempotencyCache
from .ports import (
    ActionPort,
    ActionRecord,
    CapabilityPort,
    CoreServices,
    DiagnosticsPort,
    DoctorCheck,
    LogRecord,
    MemoryPort,
    MemoryRecord,
    ObservationPort,
    PlanPort,
    PlanRecord,
    PlanRequest,
    PlanStepRecord,
    SessionPort,
    SessionSnapshot,
    StopReport,
)
from .resources import ResourceReader
from .router import ToolRouter
from .validation import SchemaError, validate_arguments

__all__ = [
    "PRODUCT_VERSION",
    "RESOURCES",
    "RESOURCES_BY_URI",
    "TOOLS",
    "TOOLS_BY_NAME",
    "ActionPort",
    "ActionRecord",
    "CapabilityPort",
    "CoreServices",
    "DiagnosticsPort",
    "DoctorCheck",
    "IdempotencyCache",
    "LogRecord",
    "MemoryPort",
    "MemoryRecord",
    "ObservationPort",
    "PlanPort",
    "PlanRecord",
    "PlanRequest",
    "PlanStepRecord",
    "ResourceReader",
    "ResourceSpec",
    "SchemaError",
    "SessionPort",
    "SessionSnapshot",
    "StopReport",
    "ToolFailure",
    "ToolKind",
    "ToolOutcome",
    "ToolRouter",
    "ToolSpec",
    "ToolSuccess",
    "is_retryable",
    "published_tools",
    "validate_arguments",
    "withheld_tools",
]
