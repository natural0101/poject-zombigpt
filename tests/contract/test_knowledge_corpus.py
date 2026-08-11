"""The shipped corpus conforms: schema-valid, honestly sourced, fully pinned.

``knowledge/gameplay/*.yaml`` is loaded through the real loader against the
real repository root, every raw document is validated against
``schemas/gameplay-knowledge.schema.json`` with the same jsonschema validator
the other conformance files use, and the corpus-wide honesty claims are
re-asserted explicitly: every ``verified_script`` proof path and every
code-sourced ``source_detail`` path exists in this tree, wiki and official
sources never carry a verified status, and ids are unique. The loader enforces
most of this at load; the point of repeating it here is that a regression in
the *loader* cannot quietly grandfather a dishonest corpus through.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import pytest
import yaml
from jsonschema import Draft202012Validator

from pz_agent_core.knowledge import (
    MAX_DOCUMENTS,
    MAX_RULES_PER_DOCUMENT,
    KnowledgeCorpus,
    KnowledgeDocument,
    KnowledgeSource,
    KnowledgeStatus,
    load_corpus,
)

pytestmark = pytest.mark.contract

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
SCHEMA_PATH: Final = REPO_ROOT / "schemas" / "gameplay-knowledge.schema.json"
GAMEPLAY_DIR: Final = REPO_ROOT / "knowledge" / "gameplay"

#: The corpus this epic ships: enough to be useful, small enough to be true.
MIN_CORPUS_RULES: Final = 40


@pytest.fixture(scope="module")
def corpus() -> KnowledgeCorpus:
    return load_corpus(REPO_ROOT)


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _raw_documents() -> list[tuple[Path, dict[str, Any]]]:
    documents = []
    for path in sorted(GAMEPLAY_DIR.glob("*.yaml")):
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(payload, dict), f"{path.name} is not a mapping"
        documents.append((path, payload))
    return documents


def test_the_corpus_loads_through_the_loader(corpus: KnowledgeCorpus) -> None:
    assert corpus.documents, "the shipped corpus must not be empty"


def test_every_raw_document_validates_against_the_schema(
    validator: Draft202012Validator,
) -> None:
    documents = _raw_documents()
    assert documents, "knowledge/gameplay holds no documents"
    for path, payload in documents:
        errors = sorted(validator.iter_errors(payload), key=lambda e: list(e.absolute_path))
        assert not errors, f"{path.name}: {errors[0].message}"


def test_the_loader_reading_also_validates_against_the_schema(
    corpus: KnowledgeCorpus, validator: Draft202012Validator
) -> None:
    # as_dict is what any exporter would emit; it must be as conformant as the
    # files themselves, or the loader is quietly rewriting the contract.
    for document in corpus.documents:
        errors = list(validator.iter_errors(document.as_dict()))
        assert not errors, f"{document.domain.value}: {errors[0].message}"


def test_each_file_is_named_after_its_domain() -> None:
    for path, payload in _raw_documents():
        document = KnowledgeDocument.from_mapping(payload)
        assert path.stem == document.domain.value, (
            f"{path.name} carries domain {document.domain.value}; one file per "
            "domain, named for it, is the corpus layout"
        )


def test_rule_count_is_within_the_declared_bounds(corpus: KnowledgeCorpus) -> None:
    total = len(corpus.rules)
    assert MIN_CORPUS_RULES <= total <= MAX_DOCUMENTS * MAX_RULES_PER_DOCUMENT, (
        f"the corpus holds {total} rules; the honest first corpus is "
        f"{MIN_CORPUS_RULES}..{MAX_DOCUMENTS * MAX_RULES_PER_DOCUMENT}"
    )
    for document in corpus.documents:
        assert 1 <= len(document.rules) <= MAX_RULES_PER_DOCUMENT


def test_rule_ids_are_unique_across_the_corpus(corpus: KnowledgeCorpus) -> None:
    ids = [rule.id for rule in corpus.rules]
    assert len(ids) == len(set(ids))


def test_every_proof_path_exists_in_this_tree(corpus: KnowledgeCorpus) -> None:
    for rule in corpus.rules:
        if rule.status is not KnowledgeStatus.VERIFIED_SCRIPT:
            continue
        assert rule.proven_by, f"{rule.id}: verified_script with nothing proving it"
        for proof in rule.proven_by:
            assert (REPO_ROOT / proof).is_file(), (
                f"{rule.id}: proven_by names {proof}, which is not a file in this tree"
            )


def test_every_code_source_path_exists_in_this_tree(corpus: KnowledgeCorpus) -> None:
    for rule in corpus.rules:
        if rule.source is not KnowledgeSource.CODE:
            continue
        repo_path = rule.source_detail.split("::", 1)[0]
        assert (REPO_ROOT / repo_path).is_file(), (
            f"{rule.id}: source_detail names {repo_path}, which is not a file in this tree"
        )


def test_secondary_sources_never_carry_a_verified_status(corpus: KnowledgeCorpus) -> None:
    # PZwiki is secondary by policy and The Indie Stone's material, however
    # official, has not been executed by anything in this repository: neither
    # can prove a rule on its own, so neither may be dressed as verified.
    for rule in corpus.rules:
        if rule.source in (KnowledgeSource.WIKI, KnowledgeSource.OFFICIAL):
            assert rule.status is KnowledgeStatus.UNVERIFIED, (
                f"{rule.id}: a {rule.source.value}-sourced rule claims {rule.status.value}"
            )


def test_every_number_states_its_own_status(corpus: KnowledgeCorpus) -> None:
    # A verified_script number inside an unverified rule would be a claim the
    # rule's own evidence does not cover; the corpus keeps numbers at or below
    # their rule's status.
    rank = {
        KnowledgeStatus.UNVERIFIED: 0,
        KnowledgeStatus.VERIFIED_SCRIPT: 1,
        KnowledgeStatus.VERIFIED_LIVE: 2,
    }
    for rule in corpus.rules:
        for number in rule.numbers:
            assert rank[number.status] <= rank[rule.status], (
                f"{rule.id}: number {number.name} is {number.status.value} inside "
                f"a {rule.status.value} rule"
            )
