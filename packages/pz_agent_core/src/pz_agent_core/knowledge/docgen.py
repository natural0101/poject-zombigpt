"""Render the knowledge corpus into the generated docs — code and text cannot drift.

Three documents come out of the one corpus the planner reads, and none of them
is written by hand:

* ``docs/BEHAVIOR_REFERENCE.md`` — :func:`render_behavior_reference`: every
  rule's anatomy (goals, preconditions, actions, postconditions, fallback),
  grouped by domain, each with its status badge and source.
* ``docs/BUILD42_MECHANICS_SOURCES.md`` — :func:`render_sources`: the
  provenance ledger. Every rule id with its build, source tier, source detail,
  status and proofs, sorted by id so a rule is one lookup away.
* ``docs/GAMEPLAY_AGENT_GUIDE_RU.md`` — :func:`render_guide_ru`: the Russian
  overview of what the agent can do per domain, split into code-verified rules
  and hypotheses.

Every renderer is a pure function of the corpus: same corpus, same bytes. No
timestamps, no environment, no locale — the only variable input is the corpus
itself, and its identity is stamped into each header as
:func:`corpus_revision_of`, a sixteen-hex-digit SHA-256 of the canonical
corpus serialisation (the same shape-then-hash pattern as
:func:`pz_agent_core.memory.model.content_revision_of`, and the same sixteen
digits: enough to detect a changed corpus, never meant to name one).

**Marker discipline (the RU guide's honesty contract).** Every rule line and
every number line in the Russian guide carries exactly one status marker from
:data:`STATUS_MARKERS_RU` — «проверено кодом», «проверено в игре» or
«не проверено (гипотеза)». Numbers are rendered only through one formatter,
which always appends the number's own marker, so an unverified number can
never appear dressed as a fact. ``tests/contract/test_knowledge_docs_sync.py``
greps the rendered guide for the discipline; the maps below are pinned
member-for-member against the enums by ``tests/unit/test_knowledge_docgen.py``.

Output is bounded like everything else: a render longer than
:data:`MAX_DOC_BYTES` is a typed :class:`DocgenError` refusal, never a
truncated document — a doc silently missing its tail would misstate the corpus
exactly the way this module exists to prevent.
"""

from __future__ import annotations

# The RU guide's frame prose mixes Russian sentences with ASCII code spans
# (`proven_by`, Build 41/42), which reads to the confusable-character rule as
# mistyped ASCII. Taking its suggestion would swap Cyrillic letters for Latin
# lookalikes and ship a guide that is no longer Russian.
# ruff: noqa: RUF001
import hashlib
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final

from .loader import KnowledgeCorpus
from .model import (
    KnowledgeDomain,
    KnowledgeNumber,
    KnowledgeRule,
    KnowledgeSource,
    KnowledgeStatus,
)

__all__ = [
    "GENERATED_DOCS",
    "MAX_DOC_BYTES",
    "REVISION_HEX_LEN",
    "STATUS_MARKERS_RU",
    "DocgenError",
    "corpus_revision_of",
    "docs_in_sync",
    "render_behavior_reference",
    "render_guide_ru",
    "render_sources",
    "write_docs",
]

#: Bytes one rendered document may occupy, aligned with the loader's own
#: per-file bound: the corpus fits in 256 KiB, so must any honest retelling.
MAX_DOC_BYTES: Final = 256 * 1024

#: Hex digits the corpus revision keeps, mirroring the memory package's
#: ``CONTENT_REVISION_HEX_LEN``: a change detector, not an identifier.
REVISION_HEX_LEN: Final = 16

#: English section titles, one per domain. Pinned against the enum by the unit
#: tests so a new domain cannot ship without its heading.
_DOMAIN_TITLES_EN: Final[Mapping[KnowledgeDomain, str]] = {
    KnowledgeDomain.MOVEMENT: "Movement",
    KnowledgeDomain.DOORS_WINDOWS: "Doors and windows",
    KnowledgeDomain.CONTAINERS_LOOT: "Containers and loot",
    KnowledgeDomain.INVENTORY_EQUIPMENT: "Inventory and equipment",
    KnowledgeDomain.FOOD_WATER: "Food and water",
    KnowledgeDomain.MEDICAL: "Medical",
    KnowledgeDomain.REST_SLEEP: "Rest and sleep",
    KnowledgeDomain.THREAT_COMBAT: "Threats and combat",
    KnowledgeDomain.CLOTHING_PROTECTION: "Clothing and protection",
    KnowledgeDomain.LITERATURE_SKILLS: "Literature and skills",
    KnowledgeDomain.CRAFTING: "Crafting",
    KnowledgeDomain.BUILDING: "Building",
    KnowledgeDomain.UTILITIES_SURVIVAL: "Utilities and survival",
    KnowledgeDomain.MEMORY_PLACES: "Memory and places",
}

#: Russian section titles, same coverage contract as the English map.
_DOMAIN_TITLES_RU: Final[Mapping[KnowledgeDomain, str]] = {
    KnowledgeDomain.MOVEMENT: "Движение",
    KnowledgeDomain.DOORS_WINDOWS: "Двери и окна",
    KnowledgeDomain.CONTAINERS_LOOT: "Контейнеры и лут",
    KnowledgeDomain.INVENTORY_EQUIPMENT: "Инвентарь и экипировка",
    KnowledgeDomain.FOOD_WATER: "Еда и вода",
    KnowledgeDomain.MEDICAL: "Медицина",
    KnowledgeDomain.REST_SLEEP: "Отдых и сон",
    KnowledgeDomain.THREAT_COMBAT: "Угрозы и бой",
    KnowledgeDomain.CLOTHING_PROTECTION: "Одежда и защита",
    KnowledgeDomain.LITERATURE_SKILLS: "Литература и навыки",
    KnowledgeDomain.CRAFTING: "Крафт",
    KnowledgeDomain.BUILDING: "Строительство",
    KnowledgeDomain.UTILITIES_SURVIVAL: "Утилиты выживания",
    KnowledgeDomain.MEMORY_PLACES: "Память о местах",
}

#: The one marker per status the RU guide is allowed to use. Every rule line
#: and every number line carries exactly one of these; the contract test greps
#: for it. Pinned against the enum by the unit tests.
STATUS_MARKERS_RU: Final[Mapping[KnowledgeStatus, str]] = {
    KnowledgeStatus.VERIFIED_LIVE: "проверено в игре",
    KnowledgeStatus.VERIFIED_SCRIPT: "проверено кодом",
    KnowledgeStatus.UNVERIFIED: "не проверено (гипотеза)",
}

#: Russian names for the source tiers, used beside the marker so a hypothesis
#: also says where the folklore came from.
_SOURCE_NAMES_RU: Final[Mapping[KnowledgeSource, str]] = {
    KnowledgeSource.CODE: "код этого репозитория",
    KnowledgeSource.LIVE_PROBE: "живая проба",
    KnowledgeSource.OFFICIAL: "официальные материалы",
    KnowledgeSource.WIKI: "wiki",
}


class DocgenError(ValueError):
    """A render refused: the output would breach a bound.

    Raised instead of truncating, because a document silently missing rules
    would misstate the corpus — the exact drift this module exists to prevent.
    """


def corpus_revision_of(corpus: KnowledgeCorpus) -> str:
    """A short digest of the corpus content, stamped into every header.

    The hash runs over the canonical JSON of every document's ``as_dict()``
    in corpus order, key-sorted, NUL-separated — so YAML formatting, comments
    and key order cannot move the revision, while any change to a rule's
    content does. Sixteen hex digits, like the memory package's
    ``content_revision_of``: a change detector, not an identifier.
    """
    digest = hashlib.sha256()
    for document in corpus.documents:
        canonical = json.dumps(document.as_dict(), sort_keys=True, ensure_ascii=True)
        digest.update(canonical.encode("ascii"))
        digest.update(b"\x00")
    return digest.hexdigest()[:REVISION_HEX_LEN]


def _header(function_name: str, corpus: KnowledgeCorpus) -> str:
    """The generated-file stamp: generator, corpus revision, the warning."""
    return (
        "<!--\n"
        "  GENERATED FILE - DO NOT EDIT BY HAND.\n"
        f"  Generator: pz_agent_core.knowledge.docgen.{function_name}\n"
        f"  Corpus revision: {corpus_revision_of(corpus)} "
        f"(sha256/{REVISION_HEX_LEN} of the canonical corpus)\n"
        "  Edit knowledge/gameplay/*.yaml and regenerate; the drift test\n"
        "  byte-compares this file against a fresh render.\n"
        "-->\n"
    )


def _bounded(text: str, function_name: str) -> str:
    size = len(text.encode("utf-8"))
    if size > MAX_DOC_BYTES:
        raise DocgenError(
            f"{function_name} rendered {size} bytes, above the {MAX_DOC_BYTES}-byte "
            "bound; refusing to emit a document that large rather than truncating it"
        )
    return text


def _format_value(number: KnowledgeNumber) -> str:
    """``8``, ``0.25`` — the shortest exact spelling, unit appended if any."""
    value = format(number.value, "g")
    return f"{value} {number.unit}" if number.unit else value


def _cell(text: str) -> str:
    """Free corpus text made safe for one markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ")


def _bullets(items: tuple[str, ...], *, none: str) -> list[str]:
    if not items:
        return [f"- {none}\n"]
    return [f"- {item}\n" for item in items]


# ---------------------------------------------------------------------------
# docs/BEHAVIOR_REFERENCE.md
# ---------------------------------------------------------------------------


def _render_rule_en(rule: KnowledgeRule) -> list[str]:
    out: list[str] = [f"### `{rule.id}`\n", "\n"]
    badge = (
        f"**Status:** `{rule.status.value}` · **Source:** `{rule.source.value}`"
        + (f" — `{rule.source_detail}`" if rule.source_detail else "")
        + f" · **Build:** {rule.build} · **Risk:** {rule.risk_class.value}\n"
    )
    out += [badge, "\n", f"{rule.claim}\n", "\n"]
    if rule.goal_kinds:
        goals = ", ".join(f"`{kind.value}`" for kind in rule.goal_kinds)
        out.append(f"- **Goals:** {goals}\n")
    if rule.needs:
        needs = ", ".join(f"`{need.value}`" for need in rule.needs)
        out.append(f"- **Needs:** {needs}\n")
    if rule.nearby_kinds:
        nearby = ", ".join(f"`{kind}`" for kind in rule.nearby_kinds)
        out.append(f"- **Nearby:** {nearby}\n")
    if rule.observed_inputs:
        inputs = ", ".join(f"`{item}`" for item in rule.observed_inputs)
        out.append(f"- **Observed inputs:** {inputs}\n")
    if rule.goal_kinds or rule.needs or rule.nearby_kinds or rule.observed_inputs:
        out.append("\n")
    out += ["**Preconditions**\n", "\n"]
    out += _bullets(rule.preconditions, none="None recorded.")
    out += ["\n", "**Actions**\n", "\n"]
    if rule.actions:
        out += [f"- `{action.value}`\n" for action in rule.actions]
    else:
        out.append("- None — the rule drives no action.\n")
    out += ["\n", f"**Decision** — {rule.decision}\n", "\n"]
    out += ["**Postconditions**\n", "\n"]
    out += _bullets(rule.postconditions, none="None recorded.")
    out += ["\n", f"**Fallback** — {rule.on_failure}\n", "\n"]
    if rule.numbers:
        out += ["**Numbers**\n", "\n"]
        out += [
            f"- `{number.name}` = {_format_value(number)} — `{number.status.value}`\n"
            for number in rule.numbers
        ]
        out.append("\n")
    if rule.proven_by:
        proofs = ", ".join(f"`{proof}`" for proof in rule.proven_by)
        out.append(f"**Proven by:** {proofs}\n")
    else:
        out.append("**Proven by:** nothing — which is why the status says so.\n")
    out.append("\n")
    return out


def render_behavior_reference(corpus: KnowledgeCorpus) -> str:
    """``docs/BEHAVIOR_REFERENCE.md``: every rule's anatomy, by domain."""
    out: list[str] = [
        _header("render_behavior_reference", corpus),
        "\n",
        "# Behavior reference\n",
        "\n",
        "Generated from `knowledge/gameplay/*.yaml` — the corpus the planner's bounded\n",
        "retrieval reads. Grouped by domain; every rule shows what triggers it, what it\n",
        "does, what proves it, and what happens when it fails. The status badge is the\n",
        "honesty contract: `verified_script` restates behaviour this repository's tested\n",
        "code enforces, `verified_live` was observed in a live session, `unverified` is\n",
        "a hypothesis and drives nothing by itself.\n",
        "\n",
    ]
    for document in corpus.documents:
        title = _DOMAIN_TITLES_EN[document.domain]
        out += [f"## {title} (`{document.domain.value}`)\n", "\n"]
        for rule in document.rules:
            out += _render_rule_en(rule)
    return _bounded("".join(out), "render_behavior_reference")


# ---------------------------------------------------------------------------
# docs/BUILD42_MECHANICS_SOURCES.md
# ---------------------------------------------------------------------------


def render_sources(corpus: KnowledgeCorpus) -> str:
    """``docs/BUILD42_MECHANICS_SOURCES.md``: the provenance ledger."""
    out: list[str] = [
        _header("render_sources", corpus),
        "\n",
        "# Build 42 mechanics: the provenance ledger\n",
        "\n",
        "Every rule in the corpus with where its claim comes from and how far it has\n",
        "been verified, sorted by rule id. Source tiers, most authoritative first:\n",
        "`code` (this repository's shipped policy or adapter), `live_probe` (a live\n",
        "session or capability probe), `official` (The Indie Stone's published\n",
        "material), `wiki` (PZwiki — secondary by policy, never sufficient for a\n",
        "verified status on its own). A `verified_script` row's proofs are repo test\n",
        "paths; a `verified_live` row's proofs include a live evidence pointer; an\n",
        "`unverified` row proves nothing and says so.\n",
        "\n",
        "| Rule | Build | Source | Source detail | Status | Proven by |\n",
        "| --- | --- | --- | --- | --- | --- |\n",
    ]
    for rule in sorted(corpus.rules, key=lambda entry: entry.id):
        proofs = ", ".join(f"`{_cell(proof)}`" for proof in rule.proven_by) or "—"
        detail = f"`{_cell(rule.source_detail)}`" if rule.source_detail else "—"
        out.append(
            f"| `{rule.id}` | {rule.build} | `{rule.source.value}` | {detail} "
            f"| `{rule.status.value}` | {proofs} |\n"
        )
    return _bounded("".join(out), "render_sources")


# ---------------------------------------------------------------------------
# docs/GAMEPLAY_AGENT_GUIDE_RU.md
# ---------------------------------------------------------------------------


def _render_rule_ru(rule: KnowledgeRule) -> list[str]:
    marker = STATUS_MARKERS_RU[rule.status]
    source = _SOURCE_NAMES_RU[rule.source]
    out = [f"- `{rule.id}` — **{marker}** (источник: {source}). {rule.claim}\n"]
    out += [
        f"  - `{number.name} = {_format_value(number)}` — {STATUS_MARKERS_RU[number.status]}\n"
        for number in rule.numbers
    ]
    return out


def render_guide_ru(corpus: KnowledgeCorpus) -> str:
    """``docs/GAMEPLAY_AGENT_GUIDE_RU.md``: русский обзор корпуса знаний."""
    rules = corpus.rules
    verified = sum(1 for rule in rules if rule.status is KnowledgeStatus.VERIFIED_SCRIPT)
    live = sum(1 for rule in rules if rule.status is KnowledgeStatus.VERIFIED_LIVE)
    unverified = sum(1 for rule in rules if rule.status is KnowledgeStatus.UNVERIFIED)
    out: list[str] = [
        _header("render_guide_ru", corpus),
        "\n",
        "# Что агент умеет в игре: обзор корпуса знаний\n",
        "\n",
        "Этот обзор сгенерирован из `knowledge/gameplay/*.yaml` — того же корпуса,\n",
        "который читает планировщик. Здесь нет ни одного утверждения, которого нет в\n",
        "корпусе: рамочный текст фиксирован, всё содержимое по правилам — сгенерировано.\n",
        "Формулировки правил (claim) оставлены на английском намеренно: это дословные\n",
        "утверждения корпуса, и пересказ был бы вторым источником правды.\n",
        "\n",
        "## Как читать маркеры\n",
        "\n",
        "Каждое правило и каждое число несёт ровно один маркер доверия:\n",
        "\n",
        "- **проверено кодом** — правило пересказывает то, что делает код этого\n",
        "  репозитория, и закреплено его тестами (пути в `proven_by`).\n",
        "- **проверено в игре** — постусловие наблюдалось в живой сессии; указатель на\n",
        "  свидетельство лежит в `proven_by`.\n",
        "- **не проверено (гипотеза)** — источник wiki или официальные материалы;\n",
        "  планировщик может учитывать это как фон, но не как факт.\n",
        "\n",
        "Число без маркера в этом документе не появляется. Если рядом с числом стоит\n",
        "«не проверено (гипотеза)» — это фольклор Build 41/42, а не измеренный факт, и\n",
        "код на него не опирается.\n",
        "\n",
        "## Сводка\n",
        "\n",
        f"Правил всего: {len(rules)}. Проверено кодом: {verified}, "
        f"проверено в игре: {live}, гипотез: {unverified}.\n",
        "\n",
    ]
    for document in corpus.documents:
        title = _DOMAIN_TITLES_RU[document.domain]
        out += [f"## {title} (`{document.domain.value}`)\n", "\n"]
        proven = tuple(
            rule for rule in document.rules if rule.status is not KnowledgeStatus.UNVERIFIED
        )
        hypotheses = tuple(
            rule for rule in document.rules if rule.status is KnowledgeStatus.UNVERIFIED
        )
        if proven:
            out += ["Проверенные правила:\n", "\n"]
            for rule in proven:
                out += _render_rule_ru(rule)
            out.append("\n")
        if hypotheses:
            out += ["Гипотезы (фон для проверенных отказов, не руководство к действию):\n", "\n"]
            for rule in hypotheses:
                out += _render_rule_ru(rule)
            out.append("\n")
    return _bounded("".join(out), "render_guide_ru")


# ---------------------------------------------------------------------------
# the sync contract the wrapper and the drift test share
# ---------------------------------------------------------------------------

#: Every generated document: its filename under ``docs/`` and its renderer.
GENERATED_DOCS: Final[tuple[tuple[str, Callable[[KnowledgeCorpus], str]], ...]] = (
    ("BEHAVIOR_REFERENCE.md", render_behavior_reference),
    ("BUILD42_MECHANICS_SOURCES.md", render_sources),
    ("GAMEPLAY_AGENT_GUIDE_RU.md", render_guide_ru),
)


def docs_in_sync(corpus: KnowledgeCorpus, docs_root: Path) -> list[str]:
    """The generated docs that no longer match a fresh render.

    Returns the filenames from :data:`GENERATED_DOCS` (paths relative to
    *docs_root*, in that fixed order) whose on-disk bytes differ from what the
    renderer produces for *corpus* — including files that are missing or
    unreadable, because "cannot compare" and "stale" earn the same answer:
    regenerate. An empty list means every generated doc is byte-identical to
    its render, which is exactly what the ``--check``-style wrapper asserts.
    """
    stale: list[str] = []
    for name, render in GENERATED_DOCS:
        expected = render(corpus).encode("utf-8")
        try:
            actual = (docs_root / name).read_bytes()
        except OSError:
            # Discarding which OS error: a missing and an unreadable doc both
            # mean the same thing to the caller — this file needs regenerating.
            stale.append(name)
            continue
        # A Windows text-mode write turns every \n into \r\n on disk. That is
        # a checkout/write artefact, not a content change, and calling such a
        # file "stale" would make the drift gate cry wolf on one platform —
        # exactly what a Windows CI run did. Content is what the gate guards,
        # so the disk side is normalised before comparing; write_docs below
        # writes LF explicitly so freshly generated files match the repository
        # (.gitattributes pins eol=lf) byte for byte anyway.
        if actual.replace(b"\r\n", b"\n") != expected:
            stale.append(name)
    return stale


def write_docs(corpus: KnowledgeCorpus, docs_root: Path) -> None:
    """Write every generated doc with explicit LF line endings.

    ``Path.write_text`` translates newlines per platform, which on Windows
    would produce CRLF files that disagree with the repository's pinned LF —
    the one writer everything (the scripts wrapper and the tests) shares is
    how that stays impossible.
    """
    for name, render in GENERATED_DOCS:
        with (docs_root / name).open("w", encoding="utf-8", newline="\n") as sink:
            sink.write(render(corpus))
