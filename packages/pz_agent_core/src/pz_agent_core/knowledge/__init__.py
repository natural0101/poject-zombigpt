"""The gameplay knowledge base: typed rules with their evidence on their sleeve.

``knowledge/gameplay/*.yaml`` holds the corpus; ``schemas/
gameplay-knowledge.schema.json`` is its wire contract; this package is the
loader and the vocabulary. The rule of the whole epic: no claim dressed above
its evidence — ``verified_script`` restates what this repository's tested code
enforces and must point at the code and the tests, ``verified_live`` needs a
live evidence pointer, and everything else is ``unverified``. The loader
refuses violations with typed errors; it never corrects them.

:mod:`pz_agent_core.knowledge.retrieval` is the other half of the directive:
the planner prompt must not swallow the encyclopedia, so
:func:`~pz_agent_core.knowledge.retrieval.select_rules` hands a provider at
most :data:`~pz_agent_core.knowledge.retrieval.MAX_RETRIEVED_RULES` rules
relevant to the goal, the active needs and the nearby world, and
:func:`~pz_agent_core.knowledge.retrieval.render_for_prompt` renders them as
one bounded block with every unverified claim and number marked as such.
"""

from .docgen import (
    GENERATED_DOCS,
    MAX_DOC_BYTES,
    REVISION_HEX_LEN,
    STATUS_MARKERS_RU,
    DocgenError,
    corpus_revision_of,
    docs_in_sync,
    render_behavior_reference,
    render_guide_ru,
    render_sources,
)
from .loader import (
    GAMEPLAY_SUBDIR,
    MAX_DOCUMENTS,
    MAX_FILE_BYTES,
    CorpusError,
    KnowledgeCorpus,
    load_corpus,
)
from .model import (
    MAX_ACTIONS,
    MAX_GOAL_KINDS,
    MAX_NEARBY_KINDS,
    MAX_NEEDS,
    MAX_NUMBERS,
    MAX_OBSERVED_INPUTS,
    MAX_POSTCONDITIONS,
    MAX_PRECONDITIONS,
    MAX_PROVEN_BY,
    MAX_RULES_PER_DOCUMENT,
    SCHEMA_VERSION,
    KnowledgeDocument,
    KnowledgeDomain,
    KnowledgeNumber,
    KnowledgeRefusal,
    KnowledgeRule,
    KnowledgeSource,
    KnowledgeStatus,
    KnowledgeValidationError,
    NeedChannel,
)
from .retrieval import (
    DEFAULT_RENDER_CHARS,
    GOAL_MATCH_SCORE,
    MAX_RENDER_CHARS,
    MAX_RETRIEVED_RULES,
    MIN_RENDER_CHARS,
    NEARBY_MATCH_SCORE,
    NEED_MATCH_SCORE,
    default_corpus_root,
    render_for_prompt,
    select_rules,
)

__all__ = [
    "DEFAULT_RENDER_CHARS",
    "GAMEPLAY_SUBDIR",
    "GENERATED_DOCS",
    "GOAL_MATCH_SCORE",
    "MAX_ACTIONS",
    "MAX_DOCUMENTS",
    "MAX_DOC_BYTES",
    "MAX_FILE_BYTES",
    "MAX_GOAL_KINDS",
    "MAX_NEARBY_KINDS",
    "MAX_NEEDS",
    "MAX_NUMBERS",
    "MAX_OBSERVED_INPUTS",
    "MAX_POSTCONDITIONS",
    "MAX_PRECONDITIONS",
    "MAX_PROVEN_BY",
    "MAX_RENDER_CHARS",
    "MAX_RETRIEVED_RULES",
    "MAX_RULES_PER_DOCUMENT",
    "MIN_RENDER_CHARS",
    "NEARBY_MATCH_SCORE",
    "NEED_MATCH_SCORE",
    "REVISION_HEX_LEN",
    "SCHEMA_VERSION",
    "STATUS_MARKERS_RU",
    "CorpusError",
    "DocgenError",
    "KnowledgeCorpus",
    "KnowledgeDocument",
    "KnowledgeDomain",
    "KnowledgeNumber",
    "KnowledgeRefusal",
    "KnowledgeRule",
    "KnowledgeSource",
    "KnowledgeStatus",
    "KnowledgeValidationError",
    "NeedChannel",
    "corpus_revision_of",
    "default_corpus_root",
    "docs_in_sync",
    "load_corpus",
    "render_behavior_reference",
    "render_for_prompt",
    "render_guide_ru",
    "render_sources",
    "select_rules",
]
