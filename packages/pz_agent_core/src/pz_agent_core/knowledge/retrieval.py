"""Bounded retrieval: the few rules the current tick can actually use.

The corpus is an encyclopedia and the planner prompt must not swallow one
(the epic's directive, verbatim). :func:`select_rules` is the gate between
the two: given the goal being planned, the needs the policy thresholds say
are active, and the kinds of object the mod reported nearby, it returns at
most :data:`MAX_RETRIEVED_RULES` rules, ranked by a scoring function small
enough to hold in one head:

* **Goal match is strongest** (:data:`GOAL_MATCH_SCORE`): a rule tagged with
  the goal being planned is about the decision at hand.
* **Need match is next** (:data:`NEED_MATCH_SCORE`): a rule speaking to an
  active need channel is about the state at hand.
* **Nearby match is weakest** (:data:`NEARBY_MATCH_SCORE`): a rule about a
  kind of object that happens to be in view is context, not decision.
* Matches add up, so a rule tagged with both the goal and an active need
  outranks one tagged with the goal alone.
* **General rules come last.** A rule with no tags at all applies anywhere
  and scores zero; a rule whose tags all miss is not retrieved, because
  "relevant to something else" is not a weaker form of relevant.
* **At equal relevance, evidence outranks hypothesis**: ``verified_live``
  over ``verified_script`` over ``unverified``. A measured fact and a wiki
  claim may tie on topic; they must not tie on trust.
* The final tie-break is the rule id, so two corpora holding the same rules
  in any file order select identically — the selection is a function of the
  corpus contents, never of directory listing order.

:func:`render_for_prompt` turns a selection into one bounded text block.
Unverified rules and unverified numbers are marked ``UNVERIFIED`` in the
text itself, because the model reading the block must see which numbers are
hypotheses. Truncation is honest: whole rules are dropped from the bottom of
the ranking, never mid-sentence, and the block's last line says how many.

:func:`default_corpus_root` answers "where is the repo-shipped corpus" for
callers with no configured root: the repository root when this module runs
from a source checkout that ships ``knowledge/gameplay``, and an honest
``None`` anywhere else — an installed wheel has no corpus above it, and
pretending otherwise would make an empty knowledge base look configured.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Final

from ..goals.model import GoalKind
from .loader import GAMEPLAY_SUBDIR, KnowledgeCorpus
from .model import KnowledgeNumber, KnowledgeRule, KnowledgeStatus

__all__ = [
    "DEFAULT_RENDER_CHARS",
    "GOAL_MATCH_SCORE",
    "MAX_RENDER_CHARS",
    "MAX_RETRIEVED_RULES",
    "MIN_RENDER_CHARS",
    "NEARBY_MATCH_SCORE",
    "NEED_MATCH_SCORE",
    "default_corpus_root",
    "render_for_prompt",
    "select_rules",
]

#: The hard cap on a selection. Twelve rules of bounded fields is a page the
#: model can actually weigh; the corpus may hold hundreds and must not arrive.
MAX_RETRIEVED_RULES: Final = 12

#: The scoring weights, ordered so no pile of weaker matches can outvote a
#: stronger one: a nearby match (1) plus a need match (2) is still below a
#: goal match (4). The ordering is the documented contract; the values only
#: have to preserve it.
GOAL_MATCH_SCORE: Final = 4
NEED_MATCH_SCORE: Final = 2
NEARBY_MATCH_SCORE: Final = 1

#: Bounds on the rendered block. The default keeps the knowledge block well
#: under the compact observation's own size; the ceiling keeps a configured
#: budget from quietly reinventing the swallowed encyclopedia.
DEFAULT_RENDER_CHARS: Final = 4_000
MIN_RENDER_CHARS: Final = 256
MAX_RENDER_CHARS: Final = 16_000

#: Room reserved for the truncation note, so reporting a drop can never itself
#: push the block over its budget.
_NOTE_RESERVE: Final = 80

#: The block's first line. It states the honesty convention the markers use,
#: because a marker the reader was never told about is decoration.
_HEADER: Final = (
    "Gameplay knowledge, retrieved for this goal "
    "(UNVERIFIED marks a hypothesis, not a measured fact):"
)

#: Evidence tiers for the equal-relevance tie-break. Higher wins.
_EVIDENCE_RANK: Final[Mapping[KnowledgeStatus, int]] = MappingProxyType(
    {
        KnowledgeStatus.VERIFIED_LIVE: 2,
        KnowledgeStatus.VERIFIED_SCRIPT: 1,
        KnowledgeStatus.UNVERIFIED: 0,
    }
)

#: How deep the repository root sits above this file in a source checkout:
#: ``knowledge → pz_agent_core → src → pz_agent_core → packages → <root>``.
_ANCHOR_DEPTH: Final = 5


def select_rules(
    corpus: KnowledgeCorpus,
    *,
    goal_kind: GoalKind | None,
    active_needs: frozenset[str],
    nearby_kinds: frozenset[str],
    limit: int = MAX_RETRIEVED_RULES,
) -> tuple[KnowledgeRule, ...]:
    """The rules relevant to this tick, best first, never more than *limit*.

    Args:
        corpus: the loaded corpus. Loading already enforced the schema and the
            honesty gates, so every rule here is one the loader vouched for.
        goal_kind: the goal being planned, or None when no goal is in play —
            with no goal, only need, nearby and general rules can surface.
        active_needs: need-channel tokens (``"hunger"``, ``"bleeding"``, …)
            that are currently firing. Matched against each rule's ``needs``
            by value, so the caller may pass the policy layer's own keys.
        nearby_kinds: kinds of object the observation reports in view.
        limit: the cap. Bounded by :data:`MAX_RETRIEVED_RULES`; a caller
            asking for more is asking for the swallowed encyclopedia back.

    Returns:
        At most *limit* rules in rank order: relevance, then evidence tier,
        then rule id. Deterministic for a given corpus content, whatever
        order its documents or rules arrived in.

    Raises:
        ValueError: when *limit* leaves the ``1..MAX_RETRIEVED_RULES`` range.
    """
    if not 1 <= limit <= MAX_RETRIEVED_RULES:
        raise ValueError(f"limit must be within 1..{MAX_RETRIEVED_RULES}, got {limit}")
    ranked: list[tuple[int, int, str, KnowledgeRule]] = []
    for rule in corpus.rules:
        relevance = _relevance(rule, goal_kind, active_needs, nearby_kinds)
        if relevance is None:
            continue
        ranked.append((-relevance, -_EVIDENCE_RANK[rule.status], rule.id, rule))
    ranked.sort(key=lambda entry: entry[:3])
    return tuple(entry[3] for entry in ranked[:limit])


def _relevance(
    rule: KnowledgeRule,
    goal_kind: GoalKind | None,
    active_needs: frozenset[str],
    nearby_kinds: frozenset[str],
) -> int | None:
    """The rule's score, 0 for a general rule, None for an irrelevant one."""
    score = 0
    if goal_kind is not None and goal_kind in rule.goal_kinds:
        score += GOAL_MATCH_SCORE
    if any(need.value in active_needs for need in rule.needs):
        score += NEED_MATCH_SCORE
    if any(kind in nearby_kinds for kind in rule.nearby_kinds):
        score += NEARBY_MATCH_SCORE
    if score:
        return score
    tagged = bool(rule.goal_kinds or rule.needs or rule.nearby_kinds)
    # A tagged rule that matched nothing is about some other situation and is
    # excluded; an untagged rule is general advice and ranks behind every match.
    return None if tagged else 0


def render_for_prompt(
    rules: tuple[KnowledgeRule, ...], *, max_chars: int = DEFAULT_RENDER_CHARS
) -> str:
    """One bounded text block: claim, decision, on_failure, marked numbers.

    Rules render in the order given — :func:`select_rules` already ranked
    them — one line each. When the budget runs out, every remaining rule is
    dropped whole and the final line reports the count; a lower-ranked rule
    is never smuggled in because it happened to be shorter. An empty
    selection renders as the empty string, so a caller can tell "nothing
    relevant" from "a block saying nothing".

    Raises:
        ValueError: when *max_chars* leaves ``MIN_RENDER_CHARS..MAX_RENDER_CHARS``.
    """
    if not MIN_RENDER_CHARS <= max_chars <= MAX_RENDER_CHARS:
        raise ValueError(
            f"max_chars must be within {MIN_RENDER_CHARS}..{MAX_RENDER_CHARS}, got {max_chars}"
        )
    if not rules:
        return ""
    budget = max_chars - _NOTE_RESERVE
    lines = [_HEADER]
    used = len(_HEADER)
    dropped = 0
    for index, rule in enumerate(rules):
        line = _rule_line(rule)
        cost = len(line) + 1  # the joining newline
        if used + cost > budget:
            dropped = len(rules) - index
            break
        lines.append(line)
        used += cost
    if dropped:
        lines.append(f"(+{dropped} relevant rule(s) dropped to keep this block bounded)")
    return "\n".join(lines)


def _rule_line(rule: KnowledgeRule) -> str:
    """One rule as one line. Unverified status is shouted, not implied."""
    marker = "UNVERIFIED" if rule.status is KnowledgeStatus.UNVERIFIED else rule.status.value
    parts = [f"- {rule.id} [{marker}, build {rule.build}]: {rule.claim}"]
    parts.append(f"do: {rule.decision}")
    parts.append(f"on_failure: {rule.on_failure}")
    if rule.numbers:
        parts.append("numbers: " + "; ".join(_number(number) for number in rule.numbers))
    return " | ".join(parts)


def _number(number: KnowledgeNumber) -> str:
    """``name=value unit``, with the hypothesis marker where it applies.

    A number's status is independent of its rule's — a verified refusal may
    cite unverified game folklore — so each number carries its own marker.
    """
    text = f"{number.name}={number.value:g}"
    if number.unit:
        text += f" {number.unit}"
    if number.status is KnowledgeStatus.UNVERIFIED:
        text += " (UNVERIFIED)"
    return text


def default_corpus_root(*, anchor: Path | None = None) -> Path | None:
    """The repository root shipping ``knowledge/gameplay``, or honest None.

    Args:
        anchor: a file at this module's depth in the tree, for tests that
            need the "no corpus here" answer without moving this module.
            Defaults to this file.

    Returns:
        The directory :func:`~pz_agent_core.knowledge.loader.load_corpus`
        can be pointed at, or None when no corpus ships above this code —
        which is every installed distribution, and the honest answer there
        is "no default", never a root that would load as silently empty.
    """
    source = (anchor if anchor is not None else Path(__file__)).resolve()
    parents = source.parents
    if len(parents) <= _ANCHOR_DEPTH:
        return None
    root = parents[_ANCHOR_DEPTH]
    return root if root.joinpath(*GAMEPLAY_SUBDIR).is_dir() else None
