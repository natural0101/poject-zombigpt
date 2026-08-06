"""``provider = "teamon"``: the user's own assistant platform, over HTTP.

**What is confirmed and what is not.** The TeamON SDK cannot be installed in
this environment and no TeamON endpoint has been contacted from this repository,
so this module is written the way
:mod:`pz_agent_voice.adapters.teamon` is written: against an interface this
project owns, with the vendor-facing half stated as a contract instead of
guessed at. Concretely:

*Confirmed, by the tests in ``tests/unit/test_provider_teamon.py``* — every
behaviour on this side of the wire. That the request carries only the redacted
planner payload; that a reply is parsed by
:meth:`~pz_agent_core.planner.plan.Plan.from_payload` and never assembled here;
that a 4xx is not retried and a failure to connect is, within a bounded budget;
that an over-large body is refused; that a refusal quoting an unknown reason
code cannot make this code raise; that :meth:`TeamONProvider.health` reports
``reachable`` only for a response it actually read.

*Not confirmed — the documented shape, awaiting a live endpoint* — every byte
of the HTTP contract below: the two paths, the request envelope, the
``plan``/``refusal`` reply and the health document. They are this project's
proposal, not an observation of a running server. If the live API differs, the
change is confined to :func:`request_payload`, :func:`_reply_proposal` and
:func:`_health_of`; nothing else in this module reads the wire.

**The contract.**

``POST {base_url}/v1/plan``
    Body: ``{"contract": "pz-agent-plan/1.0", "assistant": <str, optional>,
    "plan_schema_version": "1.0", "request": {…}}`` where ``request`` is the
    planner payload — goal, redacted observation, step budget, recent failures.
    Reply, 200: either ``{"plan": {…plan document…}}`` or
    ``{"refusal": {"reason_code": <ReasonCode>, "detail": <str>}}``. Any other
    status is a refusal carrying the status and the server's message.

``GET {base_url}/v1/health``
    Reply, 200: ``{"status": "ok", …}``. Anything else means not ready.

Both carry ``Authorization: Bearer <key>``, where the key comes from the
environment variable named by ``api_key_env`` and never from the config file.

**The platform is not a privileged caller.** TeamON is the user's own service,
which makes it trusted with the user's data and still not trusted to name an
action: what it returns is parsed by the same strict parser as any other model's
output and then reviewed by the same critic. A refusal it sends is quoted back
through :func:`~pz_agent_core.observation.compact.redact_text`, and a reason
code this build does not know becomes ``POLICY_DENIED`` rather than an
exception — an untrusted peer must not be able to choose which line of this
process raises.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, Final

from ...observation.compact import redact_text
from ...protocol import JsonDict, ReasonCode
from ...version import PRODUCT_VERSION
from ..plan import PLAN_SCHEMA_VERSION, Plan, PlanRejected
from ..provider import PlanProposal, PlanRequest
from .openai_compatible import planner_payload
from .transport import (
    DEFAULT_TRANSPORT_CONFIG,
    CredentialUnavailable,
    HttpRequest,
    HttpResponse,
    HttpTransport,
    StdlibHttpTransport,
    TransportConfig,
    TransportError,
    key_from_env,
    parse_endpoint,
)

__all__ = [
    "CONTRACT_ID",
    "DEFAULT_TEAMON_KEY_ENV",
    "HEALTH_PATH",
    "PLAN_PATH",
    "PROVIDER_TEAMON",
    "TeamONConfig",
    "TeamONHealth",
    "TeamONProvider",
    "request_payload",
]

#: The value ``planner.provider`` takes for this provider.
PROVIDER_TEAMON: Final = "teamon"

#: Names the shape of the request envelope, so a server can tell which contract
#: a client is speaking without inferring it from the fields present.
CONTRACT_ID: Final = "pz-agent-plan/1.0"

PLAN_PATH: Final = "/v1/plan"
HEALTH_PATH: Final = "/v1/health"

DEFAULT_TEAMON_KEY_ENV: Final = "PZ_AGENT_TEAMON_API_KEY"

#: What the health document must report for the endpoint to count as ready.
HEALTH_OK: Final = "ok"


@dataclass(frozen=True, slots=True)
class TeamONConfig:
    """Where the platform is, which assistant answers, and where the key lives."""

    base_url: str
    #: Which assistant on the platform should plan. Optional: an account with
    #: one assistant needs no name, and an empty value is left out of the
    #: request rather than sent as an empty string the server has to interpret.
    assistant: str = ""
    api_key_env: str = DEFAULT_TEAMON_KEY_ENV
    transport: TransportConfig = DEFAULT_TRANSPORT_CONFIG

    def __post_init__(self) -> None:
        parse_endpoint(self.base_url)
        key_env = self.api_key_env.strip()
        if not key_env:
            raise ValueError("api_key_env must name the environment variable holding the key")
        object.__setattr__(self, "api_key_env", key_env)
        object.__setattr__(self, "assistant", self.assistant.strip())

    def url(self, path: str) -> str:
        return parse_endpoint(self.base_url).join(path)


@dataclass(frozen=True, slots=True)
class TeamONHealth:
    """What a probe of the health endpoint actually observed.

    :attr:`reachable` is True only when a response was read and it reported
    :data:`HEALTH_OK`. A connection that was refused, a body that did not parse
    and a 200 saying "degraded" are all False — "I could not tell" is not
    "it is fine", which is the same rule the critic works under.
    """

    reachable: bool
    detail: str
    http_status: int | None = None
    reported_status: str = ""

    def __post_init__(self) -> None:
        if not self.detail.strip():
            raise ValueError("a health result must explain itself")


class TeamONProvider:
    """A :class:`~pz_agent_core.planner.provider.PlanProvider` over the TeamON contract."""

    name: ClassVar[str] = PROVIDER_TEAMON

    def __init__(
        self,
        config: TeamONConfig,
        *,
        transport: HttpTransport | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._config = config
        self._transport = transport or StdlibHttpTransport(config.transport)
        self._environ = environ

    @property
    def config(self) -> TeamONConfig:
        return self._config

    def api_key(self) -> str:
        """The configured key.

        Raises:
            CredentialUnavailable: when the named variable holds nothing usable.
        """
        return key_from_env(self._config.api_key_env, environ=self._environ)

    def propose(self, request: PlanRequest) -> PlanProposal:
        """Ask TeamON for a plan. Answers with a refusal rather than raising."""
        try:
            key = self.api_key()
        except CredentialUnavailable as exc:
            return PlanProposal.refusal(ReasonCode.CAPABILITY_UNAVAILABLE, str(exc))
        http_request = HttpRequest(
            url=self._config.url(PLAN_PATH),
            method="POST",
            body=json.dumps(request_payload(self._config, request), sort_keys=True).encode("utf-8"),
            headers=self._headers(key),
        )
        try:
            response = self._transport.send(http_request)
        except TransportError as exc:
            return PlanProposal.refusal(ReasonCode.CAPABILITY_UNAVAILABLE, str(exc))
        if not response.ok:
            return PlanProposal.refusal(
                ReasonCode.CAPABILITY_UNAVAILABLE,
                f"TeamON answered HTTP {response.status}.",
                reasons=_server_message(response),
            )
        return _reply_proposal(response, request)

    def health(self) -> TeamONHealth:
        """Probe the health endpoint. Never raises; the CLI doctor calls this."""
        try:
            key = self.api_key()
        except CredentialUnavailable as exc:
            return TeamONHealth(reachable=False, detail=str(exc))
        try:
            response = self._transport.send(
                HttpRequest(
                    url=self._config.url(HEALTH_PATH),
                    method="GET",
                    headers=self._headers(key),
                )
            )
        except TransportError as exc:
            return TeamONHealth(reachable=False, detail=str(exc))
        return _health_of(response)

    def _headers(self, key: str) -> dict[str, str]:
        return {
            "authorization": f"Bearer {key}",
            "content-type": "application/json",
            "accept": "application/json",
            "user-agent": f"pz-agent/{PRODUCT_VERSION}",
        }


def request_payload(config: TeamONConfig, request: PlanRequest) -> JsonDict:
    """The body of a ``POST /v1/plan``.

    ``request`` is the same document the OpenAI-compatible provider puts in its
    user message. That is deliberate rather than incidental: it is the one
    redaction path — save id digested, no paths, game text quarantined — and a
    second one built here would be a second thing to audit and a second place
    for a field to leak.
    """
    payload: JsonDict = {
        "contract": CONTRACT_ID,
        "plan_schema_version": PLAN_SCHEMA_VERSION,
        "request": planner_payload(request),
    }
    if config.assistant:
        payload["assistant"] = config.assistant
    return payload


def _reply_proposal(response: HttpResponse, request: PlanRequest) -> PlanProposal:
    """Read a 200 as either a plan or the platform's own refusal."""
    document = _json_object(response.body)
    if document is None:
        return PlanProposal.refusal(
            ReasonCode.INVALID_ARGUMENT,
            "TeamON replied with something that is not a JSON object.",
        )
    refusal = document.get("refusal")
    if isinstance(refusal, dict):
        return _quoted_refusal(refusal)
    plan_payload = document.get("plan")
    if not isinstance(plan_payload, dict):
        return PlanProposal.refusal(
            ReasonCode.INVALID_ARGUMENT,
            "TeamON's reply carried neither a plan nor a refusal.",
        )
    try:
        plan = Plan.from_payload(plan_payload)
    except PlanRejected as exc:
        return PlanProposal.refusal(
            exc.reason_code,
            "TeamON's reply is not a valid plan.",
            reasons=(f"{exc.fault.value}: {exc.detail}",),
        )
    if plan.goal_id != request.goal.goal_id:
        return PlanProposal.refusal(
            ReasonCode.INVALID_ARGUMENT,
            "TeamON answered a different goal than the one it was asked about.",
            reasons=(f"asked for {request.goal.goal_id}, got {plan.goal_id}",),
        )
    if len(plan.steps) > request.max_steps:
        return PlanProposal.refusal(
            ReasonCode.POLICY_DENIED,
            f"TeamON proposed {len(plan.steps)} steps and this request allows {request.max_steps}.",
        )
    return PlanProposal.proposed(plan, plan.summary)


def _quoted_refusal(refusal: Mapping[str, Any]) -> PlanProposal:
    """The platform's own "I will not plan that", quoted back safely.

    Both fields are attacker-controlled in the worst case, so neither is allowed
    to choose control flow: an unknown code — or ``POSTCONDITION_MET``, which
    :meth:`~pz_agent_core.planner.provider.PlanProposal.refusal` rejects — lands
    on ``POLICY_DENIED``, and the detail is redacted and bounded before it is
    shown to anyone.
    """
    raw = refusal.get("reason_code")
    code = ReasonCode.POLICY_DENIED
    if isinstance(raw, str):
        try:
            named = ReasonCode(raw)
        except ValueError:
            named = ReasonCode.POLICY_DENIED
        code = ReasonCode.POLICY_DENIED if named is ReasonCode.POSTCONDITION_MET else named
    detail = refusal.get("detail")
    quoted = redact_text(detail) if isinstance(detail, str) else ""
    return PlanProposal.refusal(
        code,
        f"TeamON declined to plan this: {quoted}" if quoted else "TeamON declined to plan this.",
    )


def _health_of(response: HttpResponse) -> TeamONHealth:
    document = _json_object(response.body)
    if document is None:
        return TeamONHealth(
            reachable=False,
            detail=f"the health endpoint answered HTTP {response.status} with a body that is "
            "not a JSON object.",
            http_status=response.status,
        )
    reported = document.get("status")
    status = redact_text(reported) if isinstance(reported, str) else ""
    if response.ok and status == HEALTH_OK:
        return TeamONHealth(
            reachable=True,
            detail="the health endpoint answered 200 and reported ok.",
            http_status=response.status,
            reported_status=status,
        )
    return TeamONHealth(
        reachable=False,
        detail=f"the health endpoint answered HTTP {response.status} and reported "
        f"{status or 'no status'}.",
        http_status=response.status,
        reported_status=status,
    )


def _json_object(body: bytes) -> dict[str, Any] | None:
    try:
        document: Any = json.loads(body)
    except ValueError:
        return None
    return document if isinstance(document, dict) else None


def _server_message(response: HttpResponse) -> tuple[str, ...]:
    """The platform's error text, redacted, or nothing when it sent none."""
    document = _json_object(response.body)
    if document is None:
        return ()
    for key in ("error", "message", "detail"):
        value = document.get(key)
        if isinstance(value, str) and value.strip():
            return (redact_text(value),)
        if isinstance(value, dict) and isinstance(value.get("message"), str):
            return (redact_text(value["message"]),)
    return ()
