"""Providers that ask something outside this process for a plan.

Each one implements :class:`~pz_agent_core.planner.provider.PlanProvider` and
nothing more: it is handed a :class:`~pz_agent_core.planner.provider.PlanRequest`
and answers with a :class:`~pz_agent_core.planner.provider.PlanProposal`. The
seam does not widen because a network is involved — what makes an untrusted
model safe is that its answer is parsed by
:meth:`~pz_agent_core.planner.plan.Plan.from_payload` and then reviewed by the
critic, capability validation, permission validation and reference validation.
A provider that assembled a :class:`~pz_agent_core.planner.plan.Plan` around the
parser would defeat all four at once, so none of them does: every plan in this
package comes out of the parser, and the modules here own only the request, the
transport and the reading of the reply.

:mod:`~pz_agent_core.planner.providers.transport` is the shared half — one
synchronous HTTP client over the standard library, because the core package has
no third-party dependency and the shipped product has no ``pip`` — and it is a
Protocol first, so every test here runs against a fake with no socket open.

``provider = "none"`` (:class:`~pz_agent_core.planner.provider.NullProvider`)
remains the default and needs none of this.
"""

from __future__ import annotations

from .openai_compatible import (
    CHAT_COMPLETIONS_PATH,
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_OPENAI_KEY_ENV,
    MAX_OUTPUT_TOKENS,
    PLANNER_CAPABILITIES,
    PROVIDER_OPENAI_COMPATIBLE,
    OpenAICompatibleConfig,
    OpenAICompatibleProvider,
    plan_instructions,
    planner_payload,
)
from .teamon import (
    CONTRACT_ID,
    DEFAULT_TEAMON_KEY_ENV,
    HEALTH_PATH,
    PLAN_PATH,
    PROVIDER_TEAMON,
    TeamONConfig,
    TeamONHealth,
    TeamONProvider,
)
from .transport import (
    DEFAULT_CONNECT_TIMEOUT_S,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_RESPONSE_BYTES,
    DEFAULT_READ_TIMEOUT_S,
    DEFAULT_TRANSPORT_CONFIG,
    MAX_ATTEMPTS,
    MAX_RESPONSE_BYTES,
    MAX_TIMEOUT_S,
    ConnectFailed,
    CredentialUnavailable,
    Endpoint,
    ExchangeFailed,
    HttpRequest,
    HttpResponse,
    HttpTransport,
    InvalidEndpoint,
    ReadTimedOut,
    ResponseTooLarge,
    StdlibHttpTransport,
    TlsFailed,
    TransportConfig,
    TransportError,
    ensure_env_name,
    key_from_env,
    parse_endpoint,
)

__all__ = [
    "CHAT_COMPLETIONS_PATH",
    "CONTRACT_ID",
    "DEFAULT_CONNECT_TIMEOUT_S",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "DEFAULT_MAX_RESPONSE_BYTES",
    "DEFAULT_OPENAI_KEY_ENV",
    "DEFAULT_READ_TIMEOUT_S",
    "DEFAULT_TEAMON_KEY_ENV",
    "DEFAULT_TRANSPORT_CONFIG",
    "HEALTH_PATH",
    "MAX_ATTEMPTS",
    "MAX_OUTPUT_TOKENS",
    "MAX_RESPONSE_BYTES",
    "MAX_TIMEOUT_S",
    "PLANNER_CAPABILITIES",
    "PLAN_PATH",
    "PROVIDER_OPENAI_COMPATIBLE",
    "PROVIDER_TEAMON",
    "ConnectFailed",
    "CredentialUnavailable",
    "Endpoint",
    "ExchangeFailed",
    "HttpRequest",
    "HttpResponse",
    "HttpTransport",
    "InvalidEndpoint",
    "OpenAICompatibleConfig",
    "OpenAICompatibleProvider",
    "ReadTimedOut",
    "ResponseTooLarge",
    "StdlibHttpTransport",
    "TeamONConfig",
    "TeamONHealth",
    "TeamONProvider",
    "TlsFailed",
    "TransportConfig",
    "TransportError",
    "ensure_env_name",
    "key_from_env",
    "parse_endpoint",
    "plan_instructions",
    "planner_payload",
]
