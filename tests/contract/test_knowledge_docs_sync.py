"""The generated docs match the shipped corpus, byte for byte.

The same pattern ``scripts/generate_playbook.py --check`` runs for the live
playbook: regenerate from the source of truth and byte-compare against what is
committed, so a corpus edited without a regenerate — or a doc edited by hand —
fails here instead of shipping as drift. On top of the byte comparison: the
render is deterministic, every file stays under the documented bound, every
header names its generator and the *current* corpus revision, and the RU
guide's marker discipline holds — no rule line and no number line without its
status marker, and nothing unverified dressed as anything else.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest

from pz_agent_core.knowledge import (
    GENERATED_DOCS,
    MAX_DOC_BYTES,
    STATUS_MARKERS_RU,
    KnowledgeCorpus,
    KnowledgeStatus,
    corpus_revision_of,
    docs_in_sync,
    load_corpus,
    render_guide_ru,
)

pytestmark = pytest.mark.contract

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
DOCS_ROOT: Final = REPO_ROOT / "docs"


@pytest.fixture(scope="module")
def corpus() -> KnowledgeCorpus:
    return load_corpus(REPO_ROOT)


@pytest.mark.parametrize(("name", "render"), GENERATED_DOCS, ids=[n for n, _ in GENERATED_DOCS])
def test_the_committed_doc_matches_a_fresh_render(
    corpus: KnowledgeCorpus, name: str, render: object
) -> None:
    committed = (DOCS_ROOT / name).read_bytes()
    assert callable(render)
    regenerated = render(corpus).encode("utf-8")
    assert committed == regenerated, (
        f"docs/{name} does not match a render of knowledge/gameplay/*.yaml; "
        "regenerate it instead of editing either side by hand"
    )


def test_docs_in_sync_agrees_the_tree_is_clean(corpus: KnowledgeCorpus) -> None:
    assert docs_in_sync(corpus, DOCS_ROOT) == []


@pytest.mark.parametrize(("name", "render"), GENERATED_DOCS, ids=[n for n, _ in GENERATED_DOCS])
def test_rendering_is_deterministic(corpus: KnowledgeCorpus, name: str, render: object) -> None:
    assert callable(render)
    assert render(corpus) == render(corpus), name


@pytest.mark.parametrize("name", [n for n, _ in GENERATED_DOCS])
def test_each_generated_doc_is_bounded(name: str) -> None:
    size = (DOCS_ROOT / name).stat().st_size
    assert size <= MAX_DOC_BYTES, f"docs/{name} is {size} bytes, above {MAX_DOC_BYTES}"


@pytest.mark.parametrize("name", [n for n, _ in GENERATED_DOCS])
def test_each_header_names_its_generator_and_revision(corpus: KnowledgeCorpus, name: str) -> None:
    head = (DOCS_ROOT / name).read_text(encoding="utf-8")[:400]
    assert head.startswith("<!--")
    assert "DO NOT EDIT BY HAND" in head
    assert "pz_agent_core.knowledge.docgen." in head
    assert corpus_revision_of(corpus) in head, (
        f"docs/{name} was generated from a different corpus revision"
    )


# ---------------------------------------------------------------------------
# the RU guide's marker discipline, run against the shipped corpus
# ---------------------------------------------------------------------------


def test_ru_guide_marks_every_rule_line(corpus: KnowledgeCorpus) -> None:
    text = render_guide_ru(corpus)
    lines = text.splitlines()
    for rule in corpus.rules:
        line = next(entry for entry in lines if entry.startswith(f"- `{rule.id}`"))
        assert f"**{STATUS_MARKERS_RU[rule.status]}**" in line, rule.id


def test_ru_guide_never_presents_a_number_without_its_marker(
    corpus: KnowledgeCorpus,
) -> None:
    """The discipline docgen documents: numbers only via the one formatter.

    A corpus number appears in the guide only as an indented ``  - `name =
    value``` line, and that line must end in exactly one marker — the number's
    own. An unverified number therefore cannot be read as a fact.
    """
    text = render_guide_ru(corpus)
    number_lines = [line for line in text.splitlines() if line.startswith("  - `")]
    corpus_numbers = [number for rule in corpus.rules for number in rule.numbers]
    assert len(number_lines) == len(corpus_numbers)
    for line in number_lines:
        assert sum(line.count(marker) for marker in STATUS_MARKERS_RU.values()) == 1, line
    for rule in corpus.rules:
        for number in rule.numbers:
            line = next(
                entry for entry in text.splitlines() if entry.startswith(f"  - `{number.name} = ")
            )
            assert STATUS_MARKERS_RU[number.status] in line, f"{rule.id}.{number.name}"


def test_ru_guide_lists_every_unverified_rule_as_a_hypothesis(
    corpus: KnowledgeCorpus,
) -> None:
    text = render_guide_ru(corpus)
    marker = STATUS_MARKERS_RU[KnowledgeStatus.UNVERIFIED]
    for rule in corpus.rules:
        if rule.status is KnowledgeStatus.UNVERIFIED:
            line = next(entry for entry in text.splitlines() if entry.startswith(f"- `{rule.id}`"))
            assert marker in line, rule.id
