"""Bounded retrieval: the ranking, the cap, the honest rendering.

Three groups. The first pins :func:`select_rules`' documented ordering with a
hand corpus — goal match over need match over nearby match, matches adding up,
evidence outranking hypothesis at equal relevance, the rule id breaking the
final tie — and proves the whole thing is a function of corpus *contents* by
shuffling the corpus and selecting again. The second pins the rendering:
``UNVERIFIED`` markers on unverified rules and numbers, whole-rule truncation
that reports its count, the byte bound holding. The third covers
:func:`default_corpus_root`: the shipped corpus in this checkout, honest None
anywhere the tree does not ship one.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

import pytest

from pz_agent_core.goals.model import GoalKind
from pz_agent_core.knowledge import (
    DEFAULT_RENDER_CHARS,
    MAX_RENDER_CHARS,
    MAX_RETRIEVED_RULES,
    MIN_RENDER_CHARS,
    KnowledgeCorpus,
    KnowledgeDocument,
    KnowledgeDomain,
    KnowledgeNumber,
    KnowledgeRule,
    KnowledgeSource,
    KnowledgeStatus,
    NeedChannel,
    default_corpus_root,
    load_corpus,
    render_for_prompt,
    select_rules,
)
from pz_agent_core.protocol import RiskClass

# ---------------------------------------------------------------------------
# a hand corpus
# ---------------------------------------------------------------------------


def rule(rule_id: str, **overrides: Any) -> KnowledgeRule:
    """One valid rule; evidence fields consistent with the requested status."""
    status = overrides.pop("status", KnowledgeStatus.UNVERIFIED)
    base: dict[str, Any] = {
        "id": rule_id,
        "build": "42",
        "source": KnowledgeSource.WIKI,
        "status": status,
        "claim": f"claim of {rule_id}, long enough to satisfy the schema bound",
        "observed_inputs": (),
        "preconditions": (),
        "decision": f"decision of {rule_id}, also long enough",
        "actions": (),
        "postconditions": (),
        "on_failure": f"on failure of {rule_id}",
        "risk_class": RiskClass.P0,
        "proven_by": (),
    }
    if status is KnowledgeStatus.VERIFIED_SCRIPT:
        base["source"] = KnowledgeSource.CODE
        base["source_detail"] = "packages/pz_agent_core/src/pz_agent_core/policy/food.py"
        base["proven_by"] = ("tests/unit/test_policy_food.py",)
    if status is KnowledgeStatus.VERIFIED_LIVE:
        base["source"] = KnowledgeSource.LIVE_PROBE
        base["proven_by"] = ("S01",)
    base.update(overrides)
    return KnowledgeRule(**base)


def corpus_of(*rules: KnowledgeRule) -> KnowledgeCorpus:
    return KnowledgeCorpus(
        documents=(KnowledgeDocument(domain=KnowledgeDomain.FOOD_WATER, rules=rules),)
    )


def select(corpus: KnowledgeCorpus, **overrides: Any) -> tuple[str, ...]:
    base: dict[str, Any] = {
        "goal_kind": GoalKind.SATISFY_HUNGER,
        "active_needs": frozenset({"hunger"}),
        "nearby_kinds": frozenset({"container"}),
    }
    base.update(overrides)
    return tuple(r.id for r in select_rules(corpus, **base))


HUNGER_GOAL = (GoalKind.SATISFY_HUNGER,)
HUNGER_NEED = (NeedChannel.HUNGER,)
CONTAINER = ("container",)


# ---------------------------------------------------------------------------
# ranking
# ---------------------------------------------------------------------------


def test_goal_match_outranks_need_match_outranks_nearby_match_and_general_is_last() -> None:
    corpus = corpus_of(
        rule("general_rule"),
        rule("nearby_rule", nearby_kinds=CONTAINER),
        rule("need_rule", needs=HUNGER_NEED),
        rule("goal_rule", goal_kinds=HUNGER_GOAL),
    )

    assert select(corpus) == ("goal_rule", "need_rule", "nearby_rule", "general_rule")


def test_matches_add_up_so_goal_plus_need_outranks_goal_alone() -> None:
    corpus = corpus_of(
        rule("goal_only", goal_kinds=HUNGER_GOAL),
        rule("goal_and_need", goal_kinds=HUNGER_GOAL, needs=HUNGER_NEED),
    )

    assert select(corpus) == ("goal_and_need", "goal_only")


def test_a_tagged_rule_matching_nothing_current_is_not_retrieved() -> None:
    """Relevant to something else is not a weaker form of relevant."""
    corpus = corpus_of(
        rule("thirst_rule", goal_kinds=(GoalKind.SATISFY_THIRST,)),
        rule("sleep_need_rule", needs=(NeedChannel.FATIGUE,)),
        rule("door_rule", nearby_kinds=("door",)),
        rule("goal_rule", goal_kinds=HUNGER_GOAL),
    )

    assert select(corpus) == ("goal_rule",)


def test_with_no_goal_only_need_nearby_and_general_rules_surface() -> None:
    corpus = corpus_of(
        rule("goal_rule", goal_kinds=HUNGER_GOAL),
        rule("need_rule", needs=HUNGER_NEED),
        rule("general_rule"),
    )

    assert select(corpus, goal_kind=None) == ("need_rule", "general_rule")


def test_evidence_outranks_hypothesis_at_equal_relevance() -> None:
    corpus = corpus_of(
        rule("a_wiki_claim", goal_kinds=HUNGER_GOAL, status=KnowledgeStatus.UNVERIFIED),
        rule("b_code_claim", goal_kinds=HUNGER_GOAL, status=KnowledgeStatus.VERIFIED_SCRIPT),
        rule("c_live_claim", goal_kinds=HUNGER_GOAL, status=KnowledgeStatus.VERIFIED_LIVE),
    )

    # Ids alone would order a < b < c; the evidence tier reverses that.
    assert select(corpus) == ("c_live_claim", "b_code_claim", "a_wiki_claim")


def test_evidence_never_buys_relevance() -> None:
    """A live-verified nearby match stays behind an unverified goal match."""
    corpus = corpus_of(
        rule("live_nearby", nearby_kinds=CONTAINER, status=KnowledgeStatus.VERIFIED_LIVE),
        rule("wiki_goal", goal_kinds=HUNGER_GOAL, status=KnowledgeStatus.UNVERIFIED),
    )

    assert select(corpus) == ("wiki_goal", "live_nearby")


def test_ties_break_on_the_rule_id() -> None:
    corpus = corpus_of(
        rule("zeta_rule", goal_kinds=HUNGER_GOAL),
        rule("alpha_rule", goal_kinds=HUNGER_GOAL),
        rule("midway_rule", goal_kinds=HUNGER_GOAL),
    )

    assert select(corpus) == ("alpha_rule", "midway_rule", "zeta_rule")


def test_selection_is_deterministic_under_corpus_shuffle() -> None:
    """The ranking is a function of corpus contents, not of file or rule order."""
    rules = [
        rule("general_rule"),
        rule("nearby_rule", nearby_kinds=CONTAINER),
        rule("need_rule", needs=HUNGER_NEED),
        rule("goal_rule", goal_kinds=HUNGER_GOAL),
        rule("live_goal_rule", goal_kinds=HUNGER_GOAL, status=KnowledgeStatus.VERIFIED_LIVE),
        rule("script_goal_rule", goal_kinds=HUNGER_GOAL, status=KnowledgeStatus.VERIFIED_SCRIPT),
        rule("goal_need_rule", goal_kinds=HUNGER_GOAL, needs=HUNGER_NEED),
        rule("irrelevant_rule", nearby_kinds=("door",)),
    ]
    selections = []
    for seed in (1, 2, 3):
        shuffled = list(rules)
        random.Random(seed).shuffle(shuffled)
        corpus = corpus_of(*shuffled)
        # Run twice per shuffle: same corpus, same answer, no hidden state.
        selections.append(select(corpus))
        selections.append(select(corpus))

    assert len(set(selections)) == 1
    assert selections[0] == (
        "goal_need_rule",
        "live_goal_rule",
        "script_goal_rule",
        "goal_rule",
        "need_rule",
        "nearby_rule",
        "general_rule",
    )


def test_the_cap_holds() -> None:
    corpus = corpus_of(*(rule(f"rule_{index:02d}", goal_kinds=HUNGER_GOAL) for index in range(20)))

    assert len(select(corpus)) == MAX_RETRIEVED_RULES
    assert len(select(corpus, limit=3)) == 3


@pytest.mark.parametrize("limit", [0, -1, MAX_RETRIEVED_RULES + 1])
def test_a_limit_outside_the_bound_is_refused(limit: int) -> None:
    with pytest.raises(ValueError, match="limit"):
        select_rules(
            corpus_of(rule("goal_rule")),
            goal_kind=None,
            active_needs=frozenset(),
            nearby_kinds=frozenset(),
            limit=limit,
        )


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def test_render_marks_unverified_rules_and_numbers() -> None:
    verified_number = KnowledgeNumber(
        name="max_rot_progress", value=0.6, status=KnowledgeStatus.VERIFIED_SCRIPT
    )
    folklore_number = KnowledgeNumber(
        name="sickness_onset_hours", value=2.0, unit="hours", status=KnowledgeStatus.UNVERIFIED
    )
    verified = rule(
        "verified_rule", status=KnowledgeStatus.VERIFIED_SCRIPT, numbers=(verified_number,)
    )
    folklore = rule("folklore_rule", status=KnowledgeStatus.UNVERIFIED, numbers=(folklore_number,))

    block = render_for_prompt((verified, folklore))

    lines = block.splitlines()
    assert lines[0].startswith("Gameplay knowledge")
    assert "UNVERIFIED" in lines[0]  # the marker convention is stated up front
    verified_line = next(line for line in lines if "verified_rule" in line)
    folklore_line = next(line for line in lines if "folklore_rule" in line)
    assert "[verified_script, build 42]" in verified_line
    assert "UNVERIFIED" not in verified_line
    assert "[UNVERIFIED, build 42]" in folklore_line
    assert "max_rot_progress=0.6" in verified_line
    assert "sickness_onset_hours=2 hours (UNVERIFIED)" in folklore_line


def test_render_carries_claim_decision_and_on_failure() -> None:
    block = render_for_prompt((rule("goal_rule"),))

    assert "claim of goal_rule" in block
    assert "do: decision of goal_rule" in block
    assert "on_failure: on failure of goal_rule" in block


def test_truncation_drops_whole_rules_and_reports_the_count() -> None:
    rules = tuple(rule(f"rule_{index:02d}") for index in range(10))

    block = render_for_prompt(rules, max_chars=800)

    assert len(block) <= 800
    kept = [line for line in block.splitlines() if line.startswith("- rule_")]
    assert 0 < len(kept) < 10
    # Whole rules only: every kept line is complete, ending in its own fields.
    assert all("on_failure: on failure of" in line for line in kept)
    dropped = 10 - len(kept)
    assert f"(+{dropped} relevant rule(s) dropped" in block.splitlines()[-1]
    # The kept prefix is the top of the ranking, not a repacking by length.
    assert kept == [line for line in render_for_prompt(rules).splitlines()[1 : len(kept) + 1]]


def test_an_untruncated_block_reports_no_drop() -> None:
    block = render_for_prompt((rule("goal_rule"),))

    assert "dropped" not in block


def test_rendering_nothing_is_the_empty_string() -> None:
    assert render_for_prompt(()) == ""


@pytest.mark.parametrize("max_chars", [0, MIN_RENDER_CHARS - 1, MAX_RENDER_CHARS + 1])
def test_a_render_budget_outside_the_bound_is_refused(max_chars: int) -> None:
    with pytest.raises(ValueError, match="max_chars"):
        render_for_prompt((rule("goal_rule"),), max_chars=max_chars)


def test_the_default_budget_is_within_the_bound() -> None:
    assert MIN_RENDER_CHARS <= DEFAULT_RENDER_CHARS <= MAX_RENDER_CHARS


# ---------------------------------------------------------------------------
# the shipped corpus, end to end
# ---------------------------------------------------------------------------


def test_the_shipped_corpus_retrieves_food_rules_for_a_hunger_goal() -> None:
    root = default_corpus_root()
    assert root is not None, "this checkout ships knowledge/gameplay"
    corpus = load_corpus(root)

    rules = select_rules(
        corpus,
        goal_kind=GoalKind.SATISFY_HUNGER,
        active_needs=frozenset({"hunger"}),
        nearby_kinds=frozenset(),
    )

    assert 0 < len(rules) <= MAX_RETRIEVED_RULES
    for retrieved in rules:
        # Every retrieved rule is on topic or general; nothing tagged for a
        # different goal, need or object made it into the block.
        matched = GoalKind.SATISFY_HUNGER in retrieved.goal_kinds or any(
            need.value == "hunger" for need in retrieved.needs
        )
        tagged = bool(retrieved.goal_kinds or retrieved.needs or retrieved.nearby_kinds)
        assert matched or not tagged
    block = render_for_prompt(rules)
    assert 0 < len(block) <= DEFAULT_RENDER_CHARS


# ---------------------------------------------------------------------------
# the default root
# ---------------------------------------------------------------------------

#: The depth this module's sibling production code sits at; mirrored here so a
#: fake anchor exercises the same arithmetic the real one uses.
FAKE_ANCHOR_TAIL = (
    "packages",
    "pz_agent_core",
    "src",
    "pz_agent_core",
    "knowledge",
    "retrieval.py",
)


def test_default_corpus_root_finds_the_shipped_corpus() -> None:
    root = default_corpus_root()

    assert root is not None
    assert (root / "knowledge" / "gameplay").is_dir()


def test_default_corpus_root_is_honestly_none_without_the_tree(tmp_path: Path) -> None:
    anchor = tmp_path.joinpath(*FAKE_ANCHOR_TAIL)

    assert default_corpus_root(anchor=anchor) is None


def test_default_corpus_root_finds_a_corpus_above_an_anchor(tmp_path: Path) -> None:
    (tmp_path / "knowledge" / "gameplay").mkdir(parents=True)
    anchor = tmp_path.joinpath(*FAKE_ANCHOR_TAIL)

    assert default_corpus_root(anchor=anchor) == tmp_path


def test_default_corpus_root_survives_a_path_too_shallow_to_hold_a_repo() -> None:
    assert default_corpus_root(anchor=Path("/shallow.py")) is None
