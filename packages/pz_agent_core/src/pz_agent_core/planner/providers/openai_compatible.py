"""``provider = "openai_compatible"``: any local ``/v1/chat/completions`` server.

llama.cpp's server, LM Studio, Ollama's OpenAI shim and vLLM all speak the same
request and the same reply, so one provider covers the four ways a user is
likely to have a model running on their own machine. Only the base URL and the
model name differ, and both come from configuration.

**The model is not trusted, and nothing here makes it trusted.** What comes back
is a string. It is parsed as JSON, handed to
:meth:`~pz_agent_core.planner.plan.Plan.from_payload` — the same strict, total
parser the wire uses — and only then does it exist as a :class:`Plan`. This
module builds no :class:`~pz_agent_core.planner.plan.PlanStep` of its own and
repairs nothing: a payload that is almost a plan is a refusal carrying the
parse fault, because a provider that patched up model output would be deciding
what the model meant, and the critic behind it would be reviewing this module's
guess rather than the model's answer. Everything downstream — the critic,
capability validation, permission validation, reference validation — runs
afterwards regardless.

**The key is never in the configuration file.** ``api_key_env`` names an
environment variable; :func:`~pz_agent_core.planner.providers.transport.key_from_env`
reads it. The failure this prevents is specific and common: a user pastes their
``config.toml`` into a bug report. A missing key is
:class:`~pz_agent_core.planner.providers.transport.CredentialUnavailable`
naming the variable, raised by :meth:`OpenAICompatibleProvider.api_key` so the
CLI can check it before a session starts, rather than a 401 an hour later.

**The prompt carries the redacted view and nothing else.** The observation goes
through :func:`~pz_agent_core.observation.compact.compact_for_planner`, which is
a whitelist: the save id is a digest, there are no absolute paths, no Windows
user name and no raw chat text, and every game-authored string is quarantined
under ``untrusted_text`` with the rule that says what that means. The one other
free string a request can carry — the skill a ``train_skill`` goal names — goes
through :func:`~pz_agent_core.observation.compact.redact_text` first.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, ClassVar, Final

from ...capabilities.probes import (
    DRINK_CARRIED,
    EAT_PERCENTAGE,
    INVENTORY_TRANSFER,
    MOVE_TO_SQUARE,
    READ_LITERATURE,
)
from ...observation.compact import CONTENT_RULE, compact_for_planner, redact_text
from ...policy.config import CAPABILITY_DRINK_PERCENTAGE
from ...protocol import ActionName, CapabilityState, JsonDict, ReasonCode
from ...version import PRODUCT_VERSION
from ..plan import (
    MAX_CONSUME_FRACTION,
    MAX_READ_PAGES,
    MAX_WAIT_GAME_SECONDS,
    MIN_CONSUME_FRACTION,
    PLAN_SCHEMA_VERSION,
    PLANNABLE_ACTIONS,
    Plan,
    PlanRejected,
    SuccessKind,
)
from ..provider import PlanProposal, PlanRequest
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
    "ACTION_ARGUMENTS",
    "CHAT_COMPLETIONS_PATH",
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "DEFAULT_OPENAI_KEY_ENV",
    "MAX_OUTPUT_TOKENS",
    "PLANNER_CAPABILITIES",
    "PLANNER_TEMPERATURE",
    "PROVIDER_OPENAI_COMPATIBLE",
    "OpenAICompatibleConfig",
    "OpenAICompatibleProvider",
    "plan_from_text",
    "plan_instructions",
    "planner_capability_states",
    "planner_payload",
]

#: The value ``planner.provider`` takes for this provider.
PROVIDER_OPENAI_COMPATIBLE: Final = "openai_compatible"

CHAT_COMPLETIONS_PATH: Final = "/v1/chat/completions"

DEFAULT_OPENAI_KEY_ENV: Final = "PZ_AGENT_OPENAI_API_KEY"

#: A plan document is small; this is the ceiling on what the model may spend
#: producing one, and it is sent to the server so a runaway generation is cut
#: off there rather than only at this end's byte ceiling.
DEFAULT_MAX_OUTPUT_TOKENS: Final = 1024
MAX_OUTPUT_TOKENS: Final = 8192

#: Fixed, not configurable. Two identical worlds must produce the same proposal
#: for the critic's verdict to mean anything the user can rely on, and a
#: temperature knob is an invitation to make planning non-reproducible in a
#: build whose whole safety story is "the same input is answered the same way".
PLANNER_TEMPERATURE: Final = 0.0

#: The capabilities the model is told the state of. A
#: :class:`~pz_agent_core.policy.selection.CapabilityLookup` answers questions
#: but cannot be enumerated, so the set of questions has to be named somewhere.
#:
#: Named here rather than derived from
#: :data:`~pz_agent_core.capabilities.probes.PROBES`, even though that would
#: keep itself up to date: a probe exists for things a plan may not name, and
#: telling a model that ``equipment_equip`` is verified invites it to propose a
#: step the parser then throws the whole plan out for. This is the list of
#: capabilities behind :data:`~pz_agent_core.planner.plan.PLANNABLE_ACTIONS`,
#: and :mod:`tests.unit.test_provider_openai_compatible` checks each name is one
#: a probe or the policy layer actually answers.
PLANNER_CAPABILITIES: Final[tuple[str, ...]] = (
    CAPABILITY_DRINK_PERCENTAGE,
    DRINK_CARRIED,
    EAT_PERCENTAGE,
    INVENTORY_TRANSFER,
    MOVE_TO_SQUARE,
    READ_LITERATURE,
)

#: Argument shapes, in the prompt's own shorthand. The bounds are interpolated
#: from :mod:`pz_agent_core.planner.plan` rather than written out, so a limit
#: cannot be relaxed in the parser while the prompt still advertises the old
#: one. :mod:`tests.unit.test_provider_openai_compatible` asserts the keys are
#: exactly :data:`~pz_agent_core.planner.plan.PLANNABLE_ACTIONS`, so an action
#: added to the plan type cannot be left undocumented here.
ACTION_ARGUMENTS: Final[Mapping[ActionName, str]] = MappingProxyType(
    {
        ActionName.ACTION_WAIT: f'{{"game_seconds": 0..{MAX_WAIT_GAME_SECONDS:g}}}',
        ActionName.MOVEMENT_MOVE_TO: (
            '{"target": {"x": int, "y": int, "z": int}, "radius"?: number, '
            '"max_distance"?: int, "allow_doors"?: bool, "allow_stairs"?: bool}'
        ),
        ActionName.MOVEMENT_MOVE_NEAR: (
            '{"object_ref": "object:...", "radius"?: number, "max_distance"?: int}'
        ),
        ActionName.INVENTORY_TRANSFER: (
            '{"item_ref": "item:...", "destination_container_ref": "container:...", '
            '"source_container_ref"?: "container:..."}'
        ),
        ActionName.INVENTORY_ENSURE_MAIN: '{"item_ref": "item:..."}',
        ActionName.CONSUME_EAT: (
            f'{{"item_ref": "item:...", "fraction"?: '
            f"{MIN_CONSUME_FRACTION}..{MAX_CONSUME_FRACTION}}}"
        ),
        ActionName.CONSUME_DRINK: (
            f'{{"item_ref": "item:...", "fraction"?: '
            f"{MIN_CONSUME_FRACTION}..{MAX_CONSUME_FRACTION}}}"
        ),
        ActionName.LITERATURE_READ: f'{{"item_ref": "item:...", "pages"?: 1..{MAX_READ_PAGES}}}',
    }
)


@dataclass(frozen=True, slots=True)
class OpenAICompatibleConfig:
    """Where the endpoint is, which model to ask, and what to spend on it."""

    base_url: str
    model: str
    api_key_env: str = DEFAULT_OPENAI_KEY_ENV
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    transport: TransportConfig = DEFAULT_TRANSPORT_CONFIG

    def __post_init__(self) -> None:
        # Validating the URL and the variable name here means a bad one is a
        # configuration error the user sees at validate-config time, not a
        # traceback in the middle of a session.
        parse_endpoint(self.base_url)
        key_env = self.api_key_env.strip()
        if not key_env:
            raise ValueError("api_key_env must name the environment variable holding the key")
        object.__setattr__(self, "api_key_env", key_env)
        if not self.model.strip():
            raise ValueError("model must name the model the endpoint serves")
        if not 1 <= self.max_output_tokens <= MAX_OUTPUT_TOKENS:
            raise ValueError(
                f"max_output_tokens must be within 1..{MAX_OUTPUT_TOKENS}, "
                f"got {self.max_output_tokens}"
            )

    @property
    def endpoint_url(self) -> str:
        """The chat-completions URL, whether or not ``base_url`` ends in a slash."""
        return parse_endpoint(self.base_url).join(CHAT_COMPLETIONS_PATH)


class OpenAICompatibleProvider:
    """A :class:`~pz_agent_core.planner.provider.PlanProvider` over one chat endpoint."""

    name: ClassVar[str] = PROVIDER_OPENAI_COMPATIBLE

    def __init__(
        self,
        config: OpenAICompatibleConfig,
        *,
        transport: HttpTransport | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._config = config
        self._transport = transport or StdlibHttpTransport(config.transport)
        self._environ = environ

    @property
    def config(self) -> OpenAICompatibleConfig:
        return self._config

    def api_key(self) -> str:
        """The configured key.

        Raises:
            CredentialUnavailable: when the named variable holds nothing usable.
                Public so the CLI can ask before a session starts.
        """
        return key_from_env(self._config.api_key_env, environ=self._environ)

    def propose(self, request: PlanRequest) -> PlanProposal:
        """Ask the model for a plan. Answers with a refusal rather than raising."""
        try:
            key = self.api_key()
        except CredentialUnavailable as exc:
            return PlanProposal.refusal(ReasonCode.CAPABILITY_UNAVAILABLE, str(exc))
        try:
            response = self._transport.send(self._http_request(key, request))
        except TransportError as exc:
            return PlanProposal.refusal(ReasonCode.CAPABILITY_UNAVAILABLE, str(exc))
        if not response.ok:
            return _status_refusal(response)
        content = _completion_text(response)
        if content is None:
            return PlanProposal.refusal(
                ReasonCode.INVALID_ARGUMENT,
                "the endpoint's reply was not a chat completion with a message in it.",
            )
        return plan_from_text(content, request)

    # -- the request -------------------------------------------------------

    def _http_request(self, key: str, request: PlanRequest) -> HttpRequest:
        body = {
            "model": self._config.model,
            "temperature": PLANNER_TEMPERATURE,
            "max_tokens": self._config.max_output_tokens,
            "stream": False,
            # Honoured by llama.cpp, LM Studio, vLLM and Ollama's shim. It is a
            # request, not a guarantee: the reply is parsed strictly either way.
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": plan_instructions()},
                {
                    "role": "user",
                    "content": json.dumps(planner_payload(request), sort_keys=True),
                },
            ],
        }
        return HttpRequest(
            url=self._config.endpoint_url,
            method="POST",
            body=json.dumps(body).encode("utf-8"),
            headers={
                "authorization": f"Bearer {key}",
                "content-type": "application/json",
                "accept": "application/json",
                "user-agent": f"pz-agent/{PRODUCT_VERSION}",
            },
        )


# --------------------------------------------------------------------------
# the prompt
# --------------------------------------------------------------------------


def planner_capability_states(request: PlanRequest) -> dict[str, CapabilityState]:
    """The probe states named in :data:`PLANNER_CAPABILITIES`, as a mapping.

    An unprobed capability answers ``UNSUPPORTED``, which is what the lookup
    already reports, so a model is never told a feature exists because nobody
    asked about it.
    """
    return {name: request.capabilities.state(name) for name in PLANNER_CAPABILITIES}


def planner_payload(request: PlanRequest) -> JsonDict:
    """The user message: the goal, the redacted world, and the budget."""
    goal = request.goal
    payload: JsonDict = {
        "goal": {
            "goal_id": goal.goal_id,
            "kind": goal.kind.value,
        },
        "max_steps": request.max_steps,
        "observation": compact_for_planner(request.observation, planner_capability_states(request)),
        "recent_failures": [
            {
                "signature": failure.signature,
                "reason_code": failure.reason_code.value,
                "detail": redact_text(failure.detail),
            }
            for failure in request.recent_failures
        ],
    }
    if goal.skill:
        # Game-authored in every path that reaches here: a skill name comes from
        # the observation or from a user sentence, never from this codebase.
        payload["goal"]["skill"] = redact_text(goal.skill)
    return payload


def plan_instructions() -> str:
    """The system message. Built from the plan type's own constants."""
    actions = "\n".join(
        f"  {action.value} {ACTION_ARGUMENTS[action]}" for action in sorted(PLANNABLE_ACTIONS)
    )
    criteria = ", ".join(sorted(kind.value for kind in SuccessKind))
    return f"""\
You plan short sequences of in-game actions for a Project Zomboid survivor.

Reply with exactly one JSON object and nothing else: no prose, no markdown, no
code fence. The object is a plan document:

{{
  "schema_version": "{PLAN_SCHEMA_VERSION}",
  "goal_id": "<echo the goal_id you were given, unchanged>",
  "summary": "<one sentence a player can read>",
  "steps": [
    {{"step_id": "s1", "action": "<one of the actions below>", "args": {{...}},
      "success": {{"type": "<criterion>"}}, "on_failure": "stop|ask_user|replan_once|skip"}}
  ]
}}

Actions, with the only arguments each accepts ("?" marks optional):
{actions}

success.type is one of: {criteria}. Use "adapter_evidence" unless you have a
reason not to; it is accepted for every action. "hunger_at_most" and
"thirst_at_most" additionally need "value" between 0 and 1 and belong only to
consume.eat and consume.drink.

Rules, all of them enforced by a parser that refuses the whole plan rather than
dropping the offending part:

- Every item_ref, container_ref and object_ref must be copied exactly from the
  observation you were given. A reference you compose yourself names nothing.
- No field other than those listed. There is no field for code, a file path, a
  key sequence or a shell command, and a plan carrying one is discarded whole.
- Keep the plan to the fewest steps that serve the goal, and never more than
  the max_steps you were given. The world is re-observed between every step, so
  a long speculative plan is worse than a short one.
- If nothing in the observation serves the goal, still answer with a plan
  document containing the closest safe action rather than inventing an item.

{CONTENT_RULE}"""


# --------------------------------------------------------------------------
# the reply
# --------------------------------------------------------------------------


def plan_from_text(content: str, request: PlanRequest) -> PlanProposal:
    """Turn one model message into a proposal, or into the refusal that says why.

    The only tolerance is a markdown fence, which chat templates add whatever
    the instructions say. Nothing else is repaired: no brace hunting, no field
    renaming, no dropping of an unknown key — those would each be this module
    deciding what the model meant.
    """
    try:
        document = json.loads(_unfenced(content))
    except ValueError as exc:
        return PlanProposal.refusal(
            ReasonCode.INVALID_ARGUMENT,
            "the model's reply was not JSON, so there is no plan in it.",
            reasons=(redact_text(str(exc)),),
        )
    if not isinstance(document, dict):
        return PlanProposal.refusal(
            ReasonCode.INVALID_ARGUMENT,
            f"the model replied with a JSON {type(document).__name__}, not a plan object.",
        )
    try:
        plan = Plan.from_payload(document)
    except PlanRejected as exc:
        return PlanProposal.refusal(
            exc.reason_code,
            "the model's reply is not a valid plan.",
            reasons=(_fault_line(exc),),
        )
    if plan.goal_id != request.goal.goal_id:
        return PlanProposal.refusal(
            ReasonCode.INVALID_ARGUMENT,
            "the model answered a different goal than the one it was asked about.",
            reasons=(f"asked for {request.goal.goal_id}, got {plan.goal_id}",),
        )
    if len(plan.steps) > request.max_steps:
        return PlanProposal.refusal(
            ReasonCode.POLICY_DENIED,
            f"the model proposed {len(plan.steps)} steps and this request allows "
            f"{request.max_steps}.",
        )
    return PlanProposal.proposed(plan, plan.summary)


def _fault_line(rejected: PlanRejected) -> str:
    """The parse fault, in one line a user or a log can carry."""
    where = f" at {rejected.field}" if rejected.field else ""
    in_step = f" in step {rejected.step_id}" if rejected.step_id else ""
    return f"{rejected.fault.value}{in_step}{where}: {rejected.detail}"


def _unfenced(content: str) -> str:
    text = content.strip()
    if not text.startswith("```"):
        return text
    body = text[3:]
    if body.startswith("json"):
        body = body[4:]
    return body.removesuffix("```").strip()


def _completion_text(response: HttpResponse) -> str | None:
    """``choices[0].message.content``, or None when the reply is not that shape."""
    try:
        document = json.loads(response.body)
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None
    choices = document.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    return content if isinstance(content, str) else None


def _status_refusal(response: HttpResponse) -> PlanProposal:
    """A non-2xx, with whatever the server said about it.

    The server's own message is the useful half — "model not found" is what the
    user has to act on — but it is text from outside this process, so it is
    redacted and bounded before it is carried anywhere.
    """
    detail = _error_message(response)
    reasons = (redact_text(detail),) if detail else ()
    return PlanProposal.refusal(
        ReasonCode.CAPABILITY_UNAVAILABLE,
        f"the model endpoint answered HTTP {response.status}.",
        reasons=reasons,
    )


def _error_message(response: HttpResponse) -> str:
    try:
        document: Any = json.loads(response.body)
    except ValueError:
        return ""
    if not isinstance(document, dict):
        return ""
    error = document.get("error")
    if isinstance(error, str):
        return error
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        message: str = error["message"]
        return message
    return ""
