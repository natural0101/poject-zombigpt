"""The doc generator: deterministic, bounded, honest about every status.

The corpora here are built in memory through the real model types — the
disk-touching honesty gates belong to the loader and are tested with it — so
these tests pin the renderers alone: same corpus same bytes, every enum member
has its title and its marker, the RU guide never shows a number without a
marker, an oversized render is a typed refusal, and ``docs_in_sync`` tells
stale from missing from clean.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pz_agent_core.goals.model import GoalKind
from pz_agent_core.knowledge import (
    GENERATED_DOCS,
    MAX_DOC_BYTES,
    REVISION_HEX_LEN,
    STATUS_MARKERS_RU,
    DocgenError,
    KnowledgeCorpus,
    KnowledgeDocument,
    KnowledgeDomain,
    KnowledgeNumber,
    KnowledgeRule,
    KnowledgeSource,
    KnowledgeStatus,
    corpus_revision_of,
    docs_in_sync,
    render_behavior_reference,
    render_guide_ru,
    render_sources,
    write_docs,
)
from pz_agent_core.knowledge.docgen import _DOMAIN_TITLES_EN, _DOMAIN_TITLES_RU, _SOURCE_NAMES_RU
from pz_agent_core.protocol import ActionName, RiskClass

# ---------------------------------------------------------------------------
# in-memory corpus plumbing
# ---------------------------------------------------------------------------


def _rule(**overrides: Any) -> KnowledgeRule:
    fields: dict[str, Any] = {
        "id": "movement_demo_rule",
        "build": "42",
        "source": KnowledgeSource.CODE,
        "source_detail": "packages/demo/src/policy.py::DemoPolicy",
        "status": KnowledgeStatus.VERIFIED_SCRIPT,
        "claim": "A demonstration claim long enough to satisfy the schema bounds.",
        "observed_inputs": ("player.position",),
        "preconditions": ("The demo precondition holds",),
        "decision": "The demo decision, made by DemoPolicy and nothing else.",
        "actions": (ActionName.MOVEMENT_MOVE_TO,),
        "postconditions": ("The demo postcondition is observed",),
        "on_failure": "The demo replans once and then refuses.",
        "risk_class": RiskClass.P2,
        "goal_kinds": (GoalKind.NAVIGATE_TO,),
        "proven_by": ("tests/unit/test_pin.py",),
        "numbers": (
            KnowledgeNumber(
                name="demo_bound",
                value=30.0,
                unit="squares",
                status=KnowledgeStatus.VERIFIED_SCRIPT,
            ),
        ),
    }
    fields.update(overrides)
    return KnowledgeRule(**fields)


def _wiki_rule(**overrides: Any) -> KnowledgeRule:
    fields: dict[str, Any] = {
        "id": "movement_wiki_lore",
        "source": KnowledgeSource.WIKI,
        "source_detail": "https://pzwiki.net/wiki/Demo — read for Build 41 lore",
        "status": KnowledgeStatus.UNVERIFIED,
        "claim": "A wiki claim nothing in this repository has ever observed happening.",
        "actions": (),
        "proven_by": (),
        "numbers": (
            KnowledgeNumber(
                name="folklore_rate",
                value=0.25,
                status=KnowledgeStatus.UNVERIFIED,
            ),
        ),
    }
    fields.update(overrides)
    return _rule(**fields)


def _corpus(*documents: KnowledgeDocument) -> KnowledgeCorpus:
    if not documents:
        documents = (
            KnowledgeDocument(domain=KnowledgeDomain.MOVEMENT, rules=(_rule(), _wiki_rule())),
            KnowledgeDocument(
                domain=KnowledgeDomain.MEDICAL,
                rules=(_rule(id="medical_demo_rule", actions=(ActionName.MEDICAL_BANDAGE,)),),
            ),
        )
    return KnowledgeCorpus(documents=documents)


RENDERERS = [render_behavior_reference, render_sources, render_guide_ru]


# ---------------------------------------------------------------------------
# vocabulary coverage: a new enum member cannot ship without its words
# ---------------------------------------------------------------------------


def test_domain_titles_cover_every_domain() -> None:
    assert set(_DOMAIN_TITLES_EN) == set(KnowledgeDomain)
    assert set(_DOMAIN_TITLES_RU) == set(KnowledgeDomain)


def test_status_markers_cover_every_status() -> None:
    assert set(STATUS_MARKERS_RU) == set(KnowledgeStatus)


def test_source_names_cover_every_source() -> None:
    assert set(_SOURCE_NAMES_RU) == set(KnowledgeSource)


def test_status_markers_are_distinct() -> None:
    # Two statuses sharing a marker would let a hypothesis wear a verified
    # label; the grep in the contract test relies on the spellings differing.
    markers = list(STATUS_MARKERS_RU.values())
    assert len(set(markers)) == len(markers)


# ---------------------------------------------------------------------------
# the corpus revision
# ---------------------------------------------------------------------------


def test_corpus_revision_is_stable_hex() -> None:
    corpus = _corpus()
    first = corpus_revision_of(corpus)
    assert first == corpus_revision_of(_corpus())
    assert len(first) == REVISION_HEX_LEN
    assert all(char in "0123456789abcdef" for char in first)


def test_corpus_revision_changes_with_content() -> None:
    base = _corpus()
    changed = KnowledgeCorpus(
        documents=(
            KnowledgeDocument(
                domain=KnowledgeDomain.MOVEMENT,
                rules=(_rule(build="42.20"), _wiki_rule()),
            ),
            *base.documents[1:],
        )
    )
    assert corpus_revision_of(base) != corpus_revision_of(changed)


# ---------------------------------------------------------------------------
# determinism and the generated-file header
# ---------------------------------------------------------------------------


def test_renderers_are_deterministic() -> None:
    corpus = _corpus()
    for render in RENDERERS:
        assert render(corpus) == render(corpus)


def test_headers_name_generator_revision_and_warning() -> None:
    corpus = _corpus()
    revision = corpus_revision_of(corpus)
    for name, render in GENERATED_DOCS:
        text = render(corpus)
        head = text[:400]
        assert head.startswith("<!--"), name
        assert "DO NOT EDIT BY HAND" in head, name
        assert "pz_agent_core.knowledge.docgen." in head, name
        assert revision in head, name


def test_an_empty_corpus_still_renders() -> None:
    empty = KnowledgeCorpus(documents=())
    for render in RENDERERS:
        text = render(empty)
        assert "DO NOT EDIT BY HAND" in text
        assert text.endswith("\n")


# ---------------------------------------------------------------------------
# the behavior reference
# ---------------------------------------------------------------------------


def test_behavior_reference_shows_the_rule_anatomy() -> None:
    text = render_behavior_reference(_corpus())
    assert "### `movement_demo_rule`" in text
    assert "**Status:** `verified_script` · **Source:** `code`" in text
    assert "`packages/demo/src/policy.py::DemoPolicy`" in text
    assert "- The demo precondition holds" in text
    assert "- `movement.move_to`" in text
    assert "- The demo postcondition is observed" in text
    assert "**Fallback** — The demo replans once and then refuses." in text
    assert "- `demo_bound` = 30 squares — `verified_script`" in text
    assert "**Proven by:** `tests/unit/test_pin.py`" in text


def test_behavior_reference_groups_by_domain_in_document_order() -> None:
    text = render_behavior_reference(_corpus())
    movement = text.index("## Movement (`movement`)")
    medical = text.index("## Medical (`medical`)")
    assert movement < medical
    assert text.index("### `movement_demo_rule`") < medical


def test_behavior_reference_is_honest_about_an_actionless_rule() -> None:
    text = render_behavior_reference(_corpus())
    assert "- None — the rule drives no action.\n" in text
    assert "**Proven by:** nothing — which is why the status says so." in text


# ---------------------------------------------------------------------------
# the provenance ledger
# ---------------------------------------------------------------------------


def test_sources_ledger_lists_every_rule_sorted_by_id() -> None:
    corpus = _corpus()
    text = render_sources(corpus)
    for rule in corpus.rules:
        assert f"| `{rule.id}` |" in text
    rows = [line for line in text.splitlines() if line.startswith("| `")]
    ids = [line.split("`")[1] for line in rows]
    assert ids == sorted(rule.id for rule in corpus.rules)


def test_sources_ledger_carries_the_provenance_columns() -> None:
    text = render_sources(_corpus())
    assert (
        "| `movement_wiki_lore` | 42 | `wiki` "
        "| `https://pzwiki.net/wiki/Demo — read for Build 41 lore` | `unverified` | — |"
    ) in text


def test_sources_ledger_escapes_table_breaking_text() -> None:
    tricky = _wiki_rule(
        id="movement_tricky_detail",
        source_detail="https://pzwiki.net/wiki/Demo|Section — read for lore",
    )
    corpus = KnowledgeCorpus(
        documents=(KnowledgeDocument(domain=KnowledgeDomain.MOVEMENT, rules=(tricky,)),)
    )
    text = render_sources(corpus)
    assert "Demo\\|Section" in text


# ---------------------------------------------------------------------------
# the RU guide and its marker discipline
# ---------------------------------------------------------------------------


def test_ru_guide_separates_verified_from_hypotheses() -> None:
    text = render_guide_ru(_corpus())
    section = text[text.index("## Движение (`movement`)") :]
    proven = section.index("Проверенные правила:")
    hypotheses = section.index("Гипотезы")
    assert proven < hypotheses
    assert section.index("`movement_demo_rule`") < hypotheses
    assert section.index("`movement_wiki_lore`") > hypotheses


def test_ru_guide_marks_every_rule_line() -> None:
    corpus = _corpus()
    text = render_guide_ru(corpus)
    for rule in corpus.rules:
        line = next(line for line in text.splitlines() if line.startswith(f"- `{rule.id}`"))
        assert f"**{STATUS_MARKERS_RU[rule.status]}**" in line


def test_ru_guide_never_shows_a_number_without_its_marker() -> None:
    corpus = _corpus()
    text = render_guide_ru(corpus)
    number_lines = [line for line in text.splitlines() if line.startswith("  - `")]
    # Both fixture numbers made it in…
    assert any("`demo_bound = 30 squares`" in line for line in number_lines)
    assert any("`folklore_rate = 0.25`" in line for line in number_lines)
    # …and every number line ends in exactly one marker, the number's own.
    for line in number_lines:
        assert sum(line.count(marker) for marker in STATUS_MARKERS_RU.values()) == 1
    unverified = next(line for line in number_lines if "folklore_rate" in line)
    assert STATUS_MARKERS_RU[KnowledgeStatus.UNVERIFIED] in unverified


def test_ru_guide_summary_counts_the_statuses() -> None:
    text = render_guide_ru(_corpus())
    assert "Правил всего: 3. Проверено кодом: 2, проверено в игре: 0, гипотез: 1." in text


# ---------------------------------------------------------------------------
# bounds
# ---------------------------------------------------------------------------


def test_an_oversized_render_is_a_typed_refusal() -> None:
    filler = "x" * 260
    rules = tuple(
        _rule(
            id=f"movement_big_rule_{index:03d}",
            claim=f"Claim {index:03d} " + filler[: 300 - 10],
            preconditions=tuple(f"Precondition {index:03d}.{n} " + filler[:150] for n in range(8)),
            postconditions=tuple(
                f"Postcondition {index:03d}.{n} " + filler[:150] for n in range(8)
            ),
        )
        for index in range(64)
    )
    documents = tuple(
        KnowledgeDocument(domain=domain, rules=rules)
        for domain in (KnowledgeDomain.MOVEMENT, KnowledgeDomain.MEDICAL)
    )
    with pytest.raises(DocgenError) as excinfo:
        render_behavior_reference(KnowledgeCorpus(documents=documents))
    assert str(MAX_DOC_BYTES) in str(excinfo.value)


# ---------------------------------------------------------------------------
# docs_in_sync: the --check the wrapper runs
# ---------------------------------------------------------------------------


def test_docs_in_sync_reports_clean_stale_and_missing(tmp_path: Path) -> None:
    corpus = _corpus()
    docs_root = tmp_path / "docs"
    docs_root.mkdir()
    write_docs(corpus, docs_root)
    assert docs_in_sync(corpus, docs_root) == []

    stale_name = GENERATED_DOCS[0][0]
    stale_path = docs_root / stale_name
    stale_path.write_text(stale_path.read_text(encoding="utf-8") + "edited\n", encoding="utf-8")
    assert docs_in_sync(corpus, docs_root) == [stale_name]

    missing_name = GENERATED_DOCS[2][0]
    (docs_root / missing_name).unlink()
    assert docs_in_sync(corpus, docs_root) == [stale_name, missing_name]


def test_docs_in_sync_detects_a_changed_corpus(tmp_path: Path) -> None:
    docs_root = tmp_path / "docs"
    docs_root.mkdir()
    write_docs(_corpus(), docs_root)
    changed = KnowledgeCorpus(
        documents=(
            KnowledgeDocument(
                domain=KnowledgeDomain.MOVEMENT,
                rules=(_rule(build="42.20"), _wiki_rule()),
            ),
            _corpus().documents[1],
        )
    )
    assert docs_in_sync(changed, docs_root) == [name for name, _ in GENERATED_DOCS]
