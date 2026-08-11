"""Russian speech onto the closed goal channel: what maps, and what is refused.

Everything here is written against the *outside* of :func:`resolve_goal`. The
tables it consults are re-stated in this file as literals rather than imported
and compared against themselves, because a test that reads the grammar back out
of the grammar passes whatever the grammar says — including "nothing".
"""

from __future__ import annotations

import importlib.util
import sys
from types import ModuleType

import pytest

from pz_agent_core import goals as core_goals
from pz_agent_core.goals import GoalKind, GoalRequest, TrainableSkill, parse_kind, parse_skill
from pz_agent_voice import intent, messages, phrases
from pz_agent_voice.intent import (
    ALL_VOICE_CAPABILITIES,
    GoalResolution,
    check_grammar,
    extract_quantities,
    is_stop,
    matched_skill,
    matched_skills,
    resolve_goal,
)
from pz_agent_voice.intent import STOP_WORDS as VOICE_STOP_WORDS
from pz_agent_voice.messages import MAX_TEXT_CHARS, IntentRefusal, VoiceInput, VoiceIntent

# The capability names the probe table publishes, written out here rather than
# imported: if one is renamed, a refusal stops naming the thing the user's
# capability report calls it, and this file is where that has to be noticed.
EAT_CAP = "eat_percentage"
DRINK_CAP = "drink_carried"
READ_CAP = "read_literature"
MOVE_CAP = "move_to_square"
BANDAGE_CAP = "medical_bandage"

EVERYTHING = frozenset({EAT_CAP, DRINK_CAP, READ_CAP, MOVE_CAP, BANDAGE_CAP})

#: The speakable kinds the channel carries, as an independent literal.
#: ``return_home`` joined with the goal-controller epic: the first
#: parameterless kind added since the voice epic, so the first new kind the
#: partition check admits as speakable.
KIND_VALUES = frozenset(
    {
        "satisfy_hunger",
        "satisfy_thirst",
        "read_for_boredom",
        "train_skill",
        "learn_recipe",
        "return_home",
        # The care wave's parameterless kind: speakable on return_home's own
        # argument — the bare word carries the whole goal, and the triage
        # stays the medical policy's.
        "treat_wounds",
        # The retreat kind: parameterless and urgent, speakable on the same
        # argument at its sharpest — the person shouting «отступай» is
        # watching a zombie close in, and where to run stays the avoid
        # mission's deterministic decision.
        "avoid_threat",
    }
)

#: Kinds the grammar refuses on purpose, each for its own stated reason: an
#: exact world square cannot be dictated safely, so ``navigate_to`` travels
#: only through ``pz_goal_submit``; ``loot_area`` is a legitimate spoken
#: sentence with no required parameter — a real SPEAKABLE candidate — but its
#: *optional* parameters (a scope token, a radius, ``take_all``, a category
#: list) are all unspeakable in the current grammar, whose parameter machinery
#: speaks numbers-with-units and skills only, and the grammar's own two-way
#: partition check refuses a speakable kind that takes unspeakable parameters
#: even optionally. The honest placement until the grammar grows closed-token
#: parameters is therefore unspeakable, and the dedicated test below pins both
#: halves of that reasoning so the decision is revisited rather than inherited.
#: ``explore_area`` sits here by exactly loot_area's argument, one wave later:
#: its optional parameters (a scope token, a radius) are unspeakable in this
#: grammar, and a spoken explore that silently swept the default radius would
#: be the invention the partition check exists to prevent.
#: ``rest_until`` and ``sleep_until_rested`` join with the care wave, each
#: with its own dedicated pin below: rest_until's *required* fraction has no
#: unit word of its own (the percent vocabulary is satisfy_to's, and one
#: namespace is the rule), and sleep_until_rested's optional ``hours`` is
#: unit-word-shaped but the spoken-quantity path does not carry an hour scale
#: end to end yet — a spoken sleep that silently slept the default night
#: would be the invention the partition check exists to prevent.
#: ``engage_single_zombie`` closes the list for a reason unlike every other
#: entry: not a parameter-machinery gap, but the partition doing its founding
#: job. The kind is parameterless and would clear the mechanical check — and
#: it must never be speakable anyway, because it is a kill order: a misheard
#: transcript resolving to any other kind wastes a sandwich or a walk, while
#: one resolving to this kind swings a weapon at whatever the mission
#: observes nearest, on the authority of words nobody said. The dedicated
#: test below pins that argument, not just the membership.
#: ``craft_item`` is the second entry of that sort and the last in this list.
#: Its required ``product`` is an item type as the build spells it, which no
#: closed vocabulary in this process may hold — what a build can craft is the
#: build's fact — so there is nothing for a matcher to match against, and a
#: small hand-written product set would be a guess at this install correct
#: only for vanilla. What makes the decision easy rather than merely awkward
#: is the cost of being wrong: a misheard craft *destroys* materials, and no
#: later observation returns them. Its dedicated test below pins that
#: argument too.
#: ``build_structure`` closes the list, and it is the craft's argument with the
#: last mitigation removed. It fails the mechanical check twice — a blueprint
#: identifier no vocabulary here may hold, and three coordinates that are
#: already unspeakable as navigation's — and the cost of being wrong is worse
#: than the craft's rather than equal to it: a misheard product wastes planks,
#: a misheard square puts a permanent wall somewhere nobody asked for, and this
#: project ships no action that takes one down. Its dedicated test pins that
#: argument too.
UNSPEAKABLE_KIND_VALUES = frozenset(
    {
        "navigate_to",
        "loot_area",
        "explore_area",
        "rest_until",
        "sleep_until_rested",
        "engage_single_zombie",
        "craft_item",
        "build_structure",
    }
)

#: The loot goal's parameters, restated as an independent literal for the
#: partition assertions below.
LOOT_PARAM_VALUES = frozenset({"scope", "radius", "take_all", "categories"})

#: Several attested phrasings per intent (T005). Keyed by the kind's wire value
#: so an entry naming a kind that no longer exists fails at :func:`parse_kind`
#: rather than failing to import.
ATTESTED: dict[str, tuple[str, ...]] = {
    "satisfy_hunger": (
        "агент, поешь",
        "съешь что-нибудь",
        "перекуси",
        "я проголодался",
        "агент, покушай",
    ),
    "satisfy_thirst": (
        "попей воды",
        "выпей воды",
        "агент, попить",
        "напейся",
        "мучает жажда",
    ),
    "read_for_boredom": (
        "почитай книгу",
        "агент, почитай",
        "мне скучно",
        "читай книгу",
        "возьми журнал",
    ),
    "train_skill": (
        "прокачай плотницкое",
        "тренируй механику",
        "изучай медицину",
        "прокачивай готовку",
        "почитай про рыбалку",
    ),
    "learn_recipe": (
        "выучи рецепт",
        "почитай рецепты",
        "агент, рецепты",
        "выучить рецепт",
        "почитай про рецепты",
    ),
    "return_home": (
        "домой",
        "идём домой",
        "агент, домой",
        "возвращайся домой",
        "вернись домой",
    ),
    "treat_wounds": (
        "перевяжись",
        "обработай раны",
        "агент, перевяжи рану",
        "забинтуйся",
        "перевяжи меня",
    ),
    "avoid_threat": (
        "отступай",
        "беги",
        "уходи оттуда",
        "агент, отступи",
        "спрячься",
    ),
}


def said(text: str, *, final: bool = True, confidence: float = 1.0) -> VoiceInput:
    return VoiceInput(transcript=text, at_ms=1_000, final=final, confidence=confidence)


def resolve(text: str, *, available: frozenset[str] = EVERYTHING) -> GoalResolution:
    return resolve_goal(said(text), available=available)


def spoken(resolution: GoalResolution) -> str:
    assert resolution.refusal is not None
    return phrases.intent_refusal(
        resolution.refusal,
        parameter=resolution.parameter,
        capability=resolution.capability,
    )


# --------------------------------------------------------------------------
# T001 / T005 — every supported intent resolves, with several phrasings each
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("transcript", "kind_value"),
    [(phrase, value) for value, phrases_ in ATTESTED.items() for phrase in phrases_],
)
def test_an_attested_phrasing_resolves_to_its_kind(transcript: str, kind_value: str) -> None:
    expected = parse_kind(kind_value)
    assert expected is not None, f"{kind_value} is not a goal kind"
    resolution = resolve(transcript)
    assert resolution.intent is VoiceIntent.GOAL
    assert resolution.kind is expected
    assert resolution.refusal is None


def test_every_kind_has_several_attested_phrasings() -> None:
    assert set(ATTESTED) == KIND_VALUES
    assert all(len(set(spellings)) >= 3 for spellings in ATTESTED.values())


def test_the_kind_set_is_the_one_this_grammar_was_written_against() -> None:
    # An independently written literal. A new kind arrives here as a failure
    # rather than as a kind speech silently cannot reach: it is either given a
    # vocabulary (KIND_VALUES) or declared unspeakable, on purpose, below.
    assert {kind.value for kind in GoalKind} == KIND_VALUES | UNSPEAKABLE_KIND_VALUES
    assert frozenset() == KIND_VALUES & UNSPEAKABLE_KIND_VALUES


def test_navigation_is_deliberately_unspeakable() -> None:
    """Coordinates do not cross a microphone; the exclusion is a decision.

    Both sides written as independent literals: the grammar's own declaration
    (``intent.UNSPEAKABLE_KINDS``) must say exactly this, and a transcript full
    of navigation words must resolve to no goal at all rather than to a guess.
    """
    assert {kind.value for kind in intent.UNSPEAKABLE_KINDS} == UNSPEAKABLE_KIND_VALUES
    assert (
        frozenset(
            {
                "target_x",
                "target_y",
                "target_z",
                "target_endurance",
                "hours",
                # craft_item's two, both unspeakable: an item type has no
                # spoken form this grammar could match, and a run count that
                # a mishearing bound to the wrong parameter would authorise
                # spending nobody asked for.
                "product",
                "count",
                # build_structure's blueprint: the same identifier problem as
                # the product, over an action nothing in this build can undo.
                # Its square rides the three navigation coordinates above,
                # which are unspeakable already.
                "structure",
            }
        )
        | LOOT_PARAM_VALUES
        == intent.UNSPEAKABLE_PARAMS
    )
    for transcript in ("агент, иди туда", "навигация 1200 3400", "иди на точку"):
        resolution = resolve(transcript)
        assert resolution.kind is None


def test_loot_area_is_unspeakable_until_the_grammar_can_qualify_it() -> None:
    """The loot decision, pinned so it is revisited rather than inherited.

    Both halves of the honesty argument are asserted. The kind *is* a
    speakable candidate — «облутай квартиру» needs no parameter, and the goal
    channel admits it bare, which the first assertion proves. What keeps it
    out of the grammar today is the second half: every optional parameter it
    takes is unspeakable here, and the grammar's partition check ("a speakable
    kind must not take unspeakable parameters, even optionally") would refuse
    the import — a spoken «облутай только еду» that looted everything is the
    invention that check exists to prevent. When the grammar grows closed-token
    parameters, this test is the one to delete alongside the exclusion.
    """
    loot = parse_kind("loot_area")
    assert loot is not None
    # Half one: no required parameters — the bare goal is admissible.
    assert GoalRequest(kind=loot, idempotency_key="k").params.present() == frozenset()
    # Half two: every one of its optional parameters is unspeakable, so a
    # speakable loot_area would violate the partition the grammar enforces.
    spec = core_goals.GOAL_SPECS[loot]
    assert spec.required == frozenset()
    assert spec.optional == LOOT_PARAM_VALUES
    assert spec.optional <= intent.UNSPEAKABLE_PARAMS
    assert loot in intent.UNSPEAKABLE_KINDS
    # And a lootish transcript resolves to no goal at all, never to a guess.
    # «обыщи дом» is deliberately absent since return_home became speakable:
    # its second word is now the homeward vocabulary's, which is exactly why
    # searching-shaped verbs stay out of these tables.
    for transcript in ("агент, облутай квартиру", "залутай все вокруг", "обыщи шкаф"):
        resolution = resolve(transcript)
        assert resolution.kind is None


def test_explore_area_is_unspeakable_by_loots_own_argument() -> None:
    """The explore decision, pinned the same way loot's is above.

    Half one: the bare goal is admissible — no required parameters. Half two:
    every optional parameter it takes (the scope token, the radius) is
    unspeakable in this grammar, so a speakable explore_area would violate the
    partition the grammar enforces at import. When the grammar grows
    closed-token parameters, this test is the one to delete alongside the
    exclusion.
    """
    explore = parse_kind("explore_area")
    assert explore is not None
    assert GoalRequest(kind=explore, idempotency_key="k").params.present() == frozenset()
    spec = core_goals.GOAL_SPECS[explore]
    assert spec.required == frozenset()
    assert spec.optional == frozenset({"scope", "radius"})
    assert spec.optional <= intent.UNSPEAKABLE_PARAMS
    assert explore in intent.UNSPEAKABLE_KINDS
    # And an explore-ish transcript resolves to no goal at all, never a guess.
    for transcript in ("агент, исследуй окрестности", "разведай территорию"):
        resolution = resolve(transcript)
        assert resolution.kind is None


def test_rest_until_is_unspeakable_by_the_required_parameter_argument() -> None:
    """The rest decision, pinned so it is revisited rather than inherited.

    ``target_endurance`` is *required* — the channel's own spec says so, the
    first assertion proves it — and it is unspeakable in this grammar: a
    fraction whose natural unit word is already ``satisfy_to``'s, under the
    one-namespace rule. A kind whose required parameter speech cannot supply
    is exactly what the pinned argument keeps out, so the kind travels
    through ``pz_goal_submit``.
    """
    rest = parse_kind("rest_until")
    assert rest is not None
    spec = core_goals.GOAL_SPECS[rest]
    assert spec.required == frozenset({"target_endurance"})
    assert spec.required <= intent.UNSPEAKABLE_PARAMS
    assert rest in intent.UNSPEAKABLE_KINDS
    # And a restful transcript resolves to no goal at all, never to a guess.
    for transcript in ("агент, отдохни", "передохни немного"):
        assert resolve(transcript).kind is None


def test_sleep_until_rested_is_unspeakable_until_hours_can_be_spoken() -> None:
    """The sleep decision, pinned the way loot's is above.

    Half one: the bare goal is admissible — no required parameters — so the
    kind is a real speakable candidate, like loot's founding sentence. Half
    two: its one optional parameter is declared unspeakable here, because
    the spoken-quantity path (this grammar's unit table *and* the session's
    spoken-scale conversion) does not carry an hour scale end to end, and
    the partition check refuses a speakable kind that takes an unspeakable
    parameter even optionally — «поспи два часа» that silently slept the
    adapter's default night would be the invention that check prevents.
    When the whole path grows the hour quantity, this test is the one to
    delete alongside the exclusion.
    """
    sleep = parse_kind("sleep_until_rested")
    assert sleep is not None
    assert GoalRequest(kind=sleep, idempotency_key="k").params.present() == frozenset()
    spec = core_goals.GOAL_SPECS[sleep]
    assert spec.required == frozenset()
    assert spec.optional == frozenset({"hours"})
    assert spec.optional <= intent.UNSPEAKABLE_PARAMS
    assert sleep in intent.UNSPEAKABLE_KINDS
    # And a sleepy transcript resolves to no goal at all, never to a guess.
    for transcript in ("агент, поспи", "ложись спать", "выспись"):
        assert resolve(transcript).kind is None


def test_the_bandage_word_resolves_to_treat_wounds() -> None:
    """«перевяжись» is the whole goal, pinned end to end like «домой».

    Parameterless by the kind's own spec — which wound and which dressing
    stay the medical policy's decisions — so the bare word, the two-word
    sentence and the wake-word sentence all reach the same member with no
    parameters at all. «перевязку» stays first aid's *skill* word: asking to
    train first aid and asking to be bandaged resolve to different kinds.
    """
    treat = parse_kind("treat_wounds")
    assert treat is not None
    assert core_goals.GOAL_SPECS[treat].required == frozenset()
    assert core_goals.GOAL_SPECS[treat].optional == frozenset()
    for transcript in ("перевяжись", "обработай раны", "агент, забинтуйся"):
        resolution = resolve(transcript)
        assert resolution.intent is VoiceIntent.GOAL, transcript
        assert resolution.kind is treat
        assert resolution.params.present() == frozenset()
    trained = resolve("изучай медицину")
    assert trained.kind is not None and trained.kind.value == "train_skill"


def test_the_homeward_word_resolves_to_return_home() -> None:
    """«домой» is the whole goal: kind resolved, no parameters, no refusal.

    The first new speakable kind since the voice epic, pinned end to end:
    the bare word, the full sentence and the wake-word sentence all reach the
    same member, and none of them carries a parameter — where home is stays
    in the save's memory, never in the transcript.
    """
    for transcript in ("домой", "идём домой", "возвращайся домой"):
        resolution = resolve(transcript)
        assert resolution.intent is VoiceIntent.GOAL, transcript
        assert resolution.kind is GoalKind.RETURN_HOME
        assert resolution.params.present() == frozenset()
        assert resolution.refusal is None


def test_the_retreat_word_resolves_to_avoid_threat() -> None:
    """«отступай» is the whole goal, pinned end to end like «домой».

    Parameterless by the kind's own spec — where to retreat to is the avoid
    mission's deterministic decision from the observed threat picture — so
    the bare word, the two-word sentence and the wake-word sentence all
    reach the same member with no parameters at all. And the stop vocabulary
    stays sovereign: «стой» beside a retreat word is a stop, because
    stopping and retreating are different orders and the safety one wins.
    """
    avoid = parse_kind("avoid_threat")
    assert avoid is not None
    assert core_goals.GOAL_SPECS[avoid].required == frozenset()
    assert core_goals.GOAL_SPECS[avoid].optional == frozenset()
    for transcript in ("отступай", "беги", "уходи оттуда", "агент, отступи"):
        resolution = resolve(transcript)
        assert resolution.intent is VoiceIntent.GOAL, transcript
        assert resolution.kind is avoid
        assert resolution.params.present() == frozenset()
        assert resolution.refusal is None
    assert resolve("стой, беги").intent is VoiceIntent.STOP


def test_combat_is_deliberately_unspeakable_because_it_is_a_kill_order() -> None:
    """The partition's founding case, pinned with its argument.

    ``engage_single_zombie`` is parameterless and would clear the mechanical
    partition check as speakable — return_home did exactly that — and it is
    excluded anyway, on purpose: a kill order by voice with a misheard target
    is the exact harm the partition exists for. A misheard «домой» walks the
    character somewhere safe; a misheard attack word would swing a weapon at
    whatever the mission observes nearest, on the authority of words nobody
    said. So the kind is declared unspeakable, no phrasing table names it,
    and every fight-shaped transcript resolves to no goal at all — while the
    words that make the character *safer* keep their meanings: the retreat
    vocabulary still retreats and the stop vocabulary still stops.
    """
    combat = parse_kind("engage_single_zombie")
    assert combat is not None
    # Parameterless — it would pass the mechanical partition check...
    assert core_goals.GOAL_SPECS[combat].required == frozenset()
    assert core_goals.GOAL_SPECS[combat].optional == frozenset()
    # ...and is excluded by declaration anyway, which is the decision.
    assert combat in intent.UNSPEAKABLE_KINDS
    assert combat not in intent.KIND_WORDS
    assert combat not in intent.CAPABILITY_FOR_KIND
    # No attack-shaped sentence reaches a goal, spoken plainly or woken.
    for transcript in ("атакуй", "убей зомби", "агент, убей зомби", "ударь зомби", "напади"):
        resolution = resolve(transcript)
        assert resolution.kind is None, transcript
        assert resolution.intent is not VoiceIntent.GOAL, transcript
    # The neighbouring vocabularies keep their own meanings.
    assert resolve("отступай").kind is parse_kind("avoid_threat")
    assert resolve("стоп").intent is VoiceIntent.STOP


def test_crafting_is_unspeakable_because_a_transcript_cannot_spell_a_product() -> None:
    """The crafting decision, pinned with its argument rather than inherited.

    Two independent reasons, and the test asserts both because either alone
    could be argued away later. The mechanical one: the kind *requires*
    ``product``, an item type as the build spells it, and this grammar's whole
    parameter machinery is numbers-with-unit-words and skills — so the
    partition check would refuse the kind as speakable even if somebody wanted
    it, and no closed product vocabulary may be written here to change that,
    because what a build can craft is the build's fact and a table of it in
    this package would be a drifting copy of the game's.

    The one that settles it: a misheard craft is not recoverable. «сделай
    копьё» resolved against a hand-written product set would spend the wrong
    planks on the authority of words nobody said, and unlike a wasted sandwich
    or an unnecessary walk, nothing observes the materials back. So the kind
    travels only through ``pz_goal_submit``, where the caller types the
    product — and every craft-shaped sentence resolves to no goal at all,
    while the neighbouring reading vocabulary keeps its own meaning: «выучи
    рецепт» is still a book to read, not a thing to make.
    """
    craft = parse_kind("craft_item")
    assert craft is not None
    # The mechanical half: a required parameter this grammar cannot supply.
    assert core_goals.GOAL_SPECS[craft].required == frozenset({"product"})
    assert core_goals.GOAL_SPECS[craft].optional == frozenset({"count"})
    assert {"product", "count"} <= intent.UNSPEAKABLE_PARAMS
    # The declaration half, which is the decision.
    assert craft in intent.UNSPEAKABLE_KINDS
    assert craft not in intent.KIND_WORDS
    assert craft not in intent.CAPABILITY_FOR_KIND
    # No product word is a vocabulary entry anywhere in the grammar.
    every_word = {word for words in intent.KIND_WORDS.values() for word in words}
    every_word |= {word for words in intent.SKILL_WORDS.values() for word in words}
    assert not every_word & {"сделай", "смастери", "скрафти", "craft", "копьё"}
    # No craft-shaped sentence reaches a goal, spoken plainly or woken.
    for transcript in ("сделай копьё", "агент, скрафти копьё", "смастери мне спальник", "craft"):
        resolution = resolve(transcript)
        assert resolution.kind is None, transcript
        assert resolution.intent is not VoiceIntent.GOAL, transcript
    # The neighbouring vocabulary keeps its meaning: learning a recipe from a
    # book is a reading goal and stays speakable.
    assert resolve("выучи рецепт").kind is parse_kind("learn_recipe")


def test_building_is_unspeakable_because_a_misheard_square_cannot_be_undone() -> None:
    """The building decision, pinned with the argument that makes it stricter.

    The mechanical half is the craft's, doubled: the kind requires a
    ``structure`` — a blueprint as the build spells it, which no vocabulary in
    this process may hold for the reason a product may not — *and* the three
    coordinates, which this grammar already declares unspeakable because a
    world square cannot be dictated. Either alone would fail the partition
    check.

    The half that settles it is not the craft's, and the difference is the
    point. A misheard craft spends planks; a misheard square raises a wall
    somewhere nobody asked for — in a doorway, across a stair, around the
    character — and there is no demolition action anywhere in this build,
    because taking down what somebody put there is a different authority this
    project does not have. So the mistake has no undo at all, and every action
    the kind issues is P4 besides, which has no autonomous path in this
    codebase. The kind travels only through ``pz_goal_submit``, where the
    caller types the blueprint and the square and can read both back before
    granting the placement.
    """
    build = parse_kind("build_structure")
    assert build is not None
    # The mechanical half: four required parameters, none of them speakable,
    # and nothing optional to soften it.
    assert core_goals.GOAL_SPECS[build].required == frozenset(
        {"structure", "target_x", "target_y", "target_z"}
    )
    assert core_goals.GOAL_SPECS[build].optional == frozenset()
    assert core_goals.GOAL_SPECS[build].required <= intent.UNSPEAKABLE_PARAMS
    # The declaration half, which is the decision.
    assert build in intent.UNSPEAKABLE_KINDS
    assert build not in intent.KIND_WORDS
    assert build not in intent.CAPABILITY_FOR_KIND
    # No building word is a vocabulary entry anywhere in the grammar.
    every_word = {word for words in intent.KIND_WORDS.values() for word in words}
    every_word |= {word for words in intent.SKILL_WORDS.values() for word in words}
    assert not every_word & {"построй", "постройся", "стена", "стену", "build", "wall"}
    # No build-shaped sentence reaches a goal, spoken plainly or woken.
    for transcript in (
        "построй стену",
        "агент, построй стену тут",
        "постройся на 1200 3400",
        "build",
    ):
        resolution = resolve(transcript)
        assert resolution.kind is None, transcript
        assert resolution.intent is not VoiceIntent.GOAL, transcript
    # The neighbouring vocabulary keeps its meaning: the carpentry skill is
    # still something speech may ask the character to train.
    assert resolve("прокачай плотника").kind is parse_kind("train_skill")


@pytest.mark.parametrize(
    "transcript",
    [
        "агент, поешь",
        "сегодня довольно пасмурно",
        "прокачай",
        "поешь и попей",
        "прокачай плотника до пятнадцатого уровня",
        "",
    ],
)
def test_a_transcript_resolves_or_is_refused_and_never_both(transcript: str) -> None:
    resolution = resolve(transcript)
    resolved = resolution.kind is not None
    refused = resolution.refusal is not None
    assert resolved != refused
    if resolved:
        assert resolution.intent is VoiceIntent.GOAL
    else:
        assert resolution.intent is VoiceIntent.UNKNOWN


# --------------------------------------------------------------------------
# T002 — an unmapped phrase gets a named refusal, never an invented kind
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "transcript",
    [
        "сегодня довольно пасмурно",
        "почини машину",
        "убей зомби во дворе",
        "открой дверь",
        "агент",
        "построй стену из досок",
    ],
)
def test_an_unmapped_phrase_is_refused_by_name(transcript: str) -> None:
    resolution = resolve(transcript)
    assert resolution.kind is None
    assert resolution.refusal is IntentRefusal.NOT_A_GOAL
    assert isinstance(resolution.refusal, IntentRefusal)
    assert spoken(resolution)


#: Phrases that must not become a kind, each for a different reason: no goal
#: word, two goal words, a stop, a kind whose required parameter is missing, and
#: a quantity outside its range. Written out so the assertion below is about
#: *these* transcripts rather than about the type of the ``kind`` field, which
#: is a ``GoalKind | None`` however badly the matcher behaves.
NOT_A_KIND: tuple[str, ...] = (
    "сегодня пасмурно",
    "поешь и попей",
    "стоп",
    "прокачай что-нибудь",
    "почитай 0 страниц",
)


def test_no_transcript_can_produce_a_kind_outside_the_closed_set() -> None:
    for spellings in ATTESTED.values():
        for transcript in spellings:
            resolution = resolve(transcript)
            assert resolution.kind is not None, transcript
            # KIND_VALUES is this file's own literal, not the enum read back.
            assert resolution.kind.value in KIND_VALUES, transcript
    for transcript in NOT_A_KIND:
        assert resolve(transcript).kind is None, transcript


#: Transcripts a recogniser can plausibly emit under noise, plus the shapes an
#: attacker would try. None of them may raise: a matcher that throws on the way
#: to a refusal takes the voice loop down instead of answering.
HOSTILE: tuple[str, ...] = (
    "",
    " ",
    "\x00\x01\x02",
    "\n\t\r",
    "?" * 500,
    "1234567890" * 40,
    "поешь " * 60,
    "🍖🍖🍖",
    "<script>alert(1)</script>",
    "поешь; rm -rf /",
    "прокачай плотницкое до " + "9" * 300 + " уровня",
)


def test_resolve_goal_answers_every_hostile_transcript_without_raising() -> None:
    for transcript in HOSTILE:
        resolution = resolve(transcript)
        assert resolution.intent in {VoiceIntent.GOAL, VoiceIntent.STOP, VoiceIntent.UNKNOWN}


def test_two_goals_are_an_ambiguity_that_names_both() -> None:
    resolution = resolve("поешь и попей")
    assert resolution.refusal is IntentRefusal.AMBIGUOUS_GOAL
    assert set(resolution.candidates) == {GoalKind.SATISFY_HUNGER, GoalKind.SATISFY_THIRST}
    assert resolution.kind is None


# --------------------------------------------------------------------------
# T003 — quantities and targets become typed fields
# --------------------------------------------------------------------------


def test_a_named_skill_becomes_a_trainable_skill_member() -> None:
    resolution = resolve("прокачай плотницкое дело")
    assert resolution.kind is GoalKind.TRAIN_SKILL
    assert resolution.params.skill is TrainableSkill.CARPENTRY
    assert resolution.params.skill is parse_skill("carpentry")


def test_a_spoken_level_becomes_an_integer_target_level() -> None:
    resolution = resolve("прокачай плотницкое до пятого уровня")
    level = resolution.params.target_level
    assert level == 5
    assert isinstance(level, int) and not isinstance(level, bool)


def test_a_digit_page_count_becomes_an_integer() -> None:
    resolution = resolve("почитай 40 страниц")
    assert resolution.kind is GoalKind.READ_FOR_BOREDOM
    assert resolution.params.pages == 40
    assert isinstance(resolution.params.pages, int)


def test_a_spoken_percentage_becomes_the_fraction_the_core_declares() -> None:
    resolution = resolve("поешь на восемьдесят процентов")
    assert resolution.kind is GoalKind.SATISFY_HUNGER
    assert resolution.params.satisfy_to == pytest.approx(0.8)


def test_a_resolved_goal_is_accepted_by_the_core_unchanged() -> None:
    # The strongest available statement that the extraction produced *typed*
    # fields: the core's own request constructor re-checks every one of them.
    resolution = resolve("прокачай механику до восьмого уровня")
    assert resolution.kind is not None
    request = GoalRequest(kind=resolution.kind, idempotency_key="voice-1", params=resolution.params)
    assert request.kind is GoalKind.TRAIN_SKILL
    assert request.params.skill is TrainableSkill.MECHANICS
    assert request.params.target_level == 8


def test_a_number_with_no_unit_word_names_no_parameter() -> None:
    resolution = resolve("почитай 40")
    assert resolution.kind is GoalKind.READ_FOR_BOREDOM
    assert resolution.params.pages is None
    assert extract_quantities(("почитай", "40")) == {}


def test_a_unit_word_binds_only_the_number_in_front_of_it() -> None:
    assert extract_quantities(("до", "пятого", "уровня")) == {"target_level": 5}
    assert extract_quantities(("уровня", "пять")) == {}


def test_a_unit_word_further_away_than_the_window_binds_nothing() -> None:
    # Two tokens is the documented reach, written out here rather than read from
    # UNIT_WINDOW: the claim under test is that *this* distance is the limit, and
    # a test that imported the constant would move with it.
    assert extract_quantities(("пять", "и", "уровня")) == {"target_level": 5}
    assert extract_quantities(("пять", "и", "еще", "уровня")) == {}
    assert extract_quantities(("прокачай", "80", "и", "еще", "процентов")) == {}


#: The Arabic-Indic digit five, written as an escape so this file carries no
#: ambiguous character. ``int()`` reads it as five and so does a ``\d`` pattern;
#: the matcher must not, or a digit nobody typed becomes a parameter value.
ARABIC_INDIC_5 = "\u0665"


def test_a_non_ascii_digit_is_not_a_number() -> None:
    assert int(ARABIC_INDIC_5) == 5  # what the matcher must not do
    assert extract_quantities((ARABIC_INDIC_5, "уровня")) == {}
    resolution = resolve(f"прокачай плотницкое до {ARABIC_INDIC_5} уровня")
    assert resolution.kind is GoalKind.TRAIN_SKILL
    assert resolution.params.target_level is None


def test_a_digit_run_past_the_number_bound_is_not_parsed_into_a_giant_int() -> None:
    """A long digit run is reported as above every range, not multiplied out.

    The sentinel is written as a literal: a matcher that dropped the bound would
    hand the range check the number the recogniser actually emitted, and the
    refusal that comes back would be identical, so only the value distinguishes
    the two.
    """
    huge = "9" * 12
    assert extract_quantities((huge, "уровня")) == {"target_level": 1_000_000}
    assert extract_quantities((huge, "страниц")) != {"pages": int(huge)}
    resolution = resolve(f"прокачай плотницкое до {huge} уровня")
    assert resolution.refusal is IntentRefusal.PARAMETER_OUT_OF_RANGE
    assert resolution.parameter == "target_level"


def test_two_skills_in_one_sentence_are_not_silently_narrowed_to_one() -> None:
    words = said("прокачай плотника и механику").words()
    assert matched_skills(words) == (TrainableSkill.CARPENTRY, TrainableSkill.MECHANICS)
    assert matched_skill(words) is None
    resolution = resolve("прокачай плотника и механику")
    assert resolution.refusal is IntentRefusal.SKILL_NOT_NAMED


# --------------------------------------------------------------------------
# T004 — a parameter outside its declared range is refused
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("transcript", "parameter"),
    [
        ("прокачай плотницкое до 11 уровня", "target_level"),
        ("прокачай плотницкое до 0 уровня", "target_level"),
        ("прокачай плотницкое до 999999999 уровня", "target_level"),
        ("почитай 201 страницу", "pages"),
        ("почитай 0 страниц", "pages"),
        ("поешь на 150 процентов", "satisfy_to"),
    ],
)
def test_a_parameter_outside_its_range_is_refused_by_parameter_name(
    transcript: str, parameter: str
) -> None:
    resolution = resolve(transcript)
    assert resolution.kind is None
    assert resolution.refusal is IntentRefusal.PARAMETER_OUT_OF_RANGE
    assert resolution.parameter == parameter
    assert spoken(resolution)


@pytest.mark.parametrize(
    ("transcript", "field_name", "value"),
    [
        ("прокачай плотницкое до 10 уровня", "target_level", 10),
        ("прокачай плотницкое до 1 уровня", "target_level", 1),
        ("почитай 200 страниц", "pages", 200),
        ("почитай 1 страницу", "pages", 1),
        ("поешь на сто процентов", "satisfy_to", 1.0),
    ],
)
def test_the_declared_bound_itself_is_inside_the_range(
    transcript: str, field_name: str, value: float
) -> None:
    # The literals above are the ranges as this test understands them: 1..10
    # levels, 1..200 pages, 0..100 per cent. Widening a range in the core
    # without revisiting them leaves the refusal tests below still meaningful.
    resolution = resolve(transcript)
    assert resolution.intent is VoiceIntent.GOAL
    assert getattr(resolution.params, field_name) == pytest.approx(value)


def test_a_parameter_the_kind_does_not_take_is_refused_rather_than_dropped() -> None:
    resolution = resolve("поешь 20 страниц")
    assert resolution.kind is None
    assert resolution.refusal is IntentRefusal.PARAMETER_NOT_ACCEPTED
    assert resolution.parameter == "pages"


def test_a_skill_on_a_kind_that_takes_none_is_refused() -> None:
    resolution = resolve("попей воды на плотницкое")
    assert resolution.refusal is IntentRefusal.PARAMETER_NOT_ACCEPTED
    assert resolution.parameter == "skill"


def test_train_skill_without_a_skill_says_so() -> None:
    resolution = resolve("прокачай навык")
    assert resolution.kind is None
    assert resolution.refusal is IntentRefusal.SKILL_NOT_NAMED


# --------------------------------------------------------------------------
# T006 — no transcript text reaches a kind or a parameter
# --------------------------------------------------------------------------

MARKERS = ("зомбипицца", "dropdatabase", "жбхжбх")


@pytest.mark.parametrize(
    "transcript",
    [
        "агент поешь зомбипицца dropdatabase",
        "прокачай зомбипиццу до 99 уровня жбхжбх",
        "почитай dropdatabase 500 страниц",
        "зомбипицца жбхжбх dropdatabase",
        "поешь на dropdatabase процентов",
    ],
)
def test_nothing_the_recogniser_produced_survives_into_the_resolution(transcript: str) -> None:
    resolution = resolve(transcript)
    rendered = repr(resolution)
    for marker in MARKERS:
        assert marker not in rendered
    if resolution.refusal is not None:
        for marker in MARKERS:
            assert marker not in spoken(resolution)
    if resolution.kind is not None:
        assert resolution.kind in set(GoalKind)
        for marker in MARKERS:
            assert marker not in phrases.KIND_ACCEPTED[resolution.kind]


def test_every_field_of_a_resolution_is_a_type_the_core_declared() -> None:
    resolution = resolve("прокачай плотницкое до пятого уровня зомбипицца")
    assert resolution.kind in set(GoalKind)
    assert resolution.params.skill in set(TrainableSkill)
    assert type(resolution.params.target_level) is int
    assert resolution.params.satisfy_to is None
    assert resolution.params.pages is None


def test_a_resolution_refuses_to_carry_a_name_this_package_did_not_mint() -> None:
    with pytest.raises(ValueError, match="declared goal parameter"):
        GoalResolution(
            intent=VoiceIntent.UNKNOWN,
            refusal=IntentRefusal.PARAMETER_OUT_OF_RANGE,
            parameter="зомбипицца",
        )
    with pytest.raises(ValueError, match="capability"):
        GoalResolution(
            intent=VoiceIntent.UNKNOWN,
            refusal=IntentRefusal.CAPABILITY_UNAVAILABLE,
            capability="dropdatabase",
        )


def test_a_resolution_is_a_stop_a_goal_or_a_refusal_and_nothing_else() -> None:
    with pytest.raises(ValueError, match="stop"):
        GoalResolution(intent=VoiceIntent.STOP, kind=GoalKind.SATISFY_HUNGER)
    with pytest.raises(ValueError, match="resolved goal"):
        GoalResolution(intent=VoiceIntent.GOAL)
    with pytest.raises(ValueError, match="must name a refusal"):
        GoalResolution(intent=VoiceIntent.UNKNOWN)


@pytest.mark.parametrize(
    "reason",
    [IntentRefusal.PARAMETER_OUT_OF_RANGE, IntentRefusal.PARAMETER_NOT_ACCEPTED],
)
def test_a_refusal_about_a_parameter_cannot_leave_the_parameter_unnamed(
    reason: IntentRefusal,
) -> None:
    # Unnamed, the sentence built from it would have to be generic, and the user
    # would be told a number was wrong without being told which one.
    with pytest.raises(ValueError, match="rejected parameter"):
        GoalResolution(intent=VoiceIntent.UNKNOWN, refusal=reason)


def test_an_ambiguous_resolution_cannot_be_built_without_the_alternatives() -> None:
    with pytest.raises(ValueError, match="alternatives"):
        GoalResolution(intent=VoiceIntent.UNKNOWN, refusal=IntentRefusal.AMBIGUOUS_GOAL)
    with pytest.raises(ValueError, match="alternatives"):
        GoalResolution(
            intent=VoiceIntent.UNKNOWN,
            refusal=IntentRefusal.AMBIGUOUS_GOAL,
            candidates=(GoalKind.SATISFY_HUNGER,),
        )


# --------------------------------------------------------------------------
# T010 — every refusal has an actionable spoken form
# --------------------------------------------------------------------------

#: One usable set of names per refusal, so every member can be spoken here.
REFUSAL_NAMES: dict[IntentRefusal, dict[str, str]] = {
    IntentRefusal.NOT_A_GOAL: {},
    IntentRefusal.AMBIGUOUS_GOAL: {},
    IntentRefusal.SKILL_NOT_NAMED: {},
    IntentRefusal.PARAMETER_OUT_OF_RANGE: {"parameter": "target_level"},
    IntentRefusal.PARAMETER_NOT_ACCEPTED: {"parameter": "pages"},
    IntentRefusal.CAPABILITY_UNAVAILABLE: {"capability": READ_CAP},
    IntentRefusal.INTERNAL: {},
}

#: The imperatives that make a refusal actionable. Written out here so a
#: sentence that only states a problem fails.
IMPERATIVES = ("Скажи", "Назови", "Повтори", "Сделай")


def test_every_refusal_reason_has_a_spoken_form() -> None:
    assert set(REFUSAL_NAMES) == set(IntentRefusal)


@pytest.mark.parametrize("reason", list(IntentRefusal))
def test_a_refusal_names_its_cause_and_says_what_to_do(reason: IntentRefusal) -> None:
    text = phrases.intent_refusal(reason, **REFUSAL_NAMES[reason])
    assert text.strip()
    assert len(text) <= MAX_TEXT_CHARS
    # Two sentences: the cause, then the instruction.
    assert text.count(".") >= 2
    assert any(word in text for word in IMPERATIVES)


def test_the_out_of_range_refusal_says_the_range() -> None:
    text = phrases.intent_refusal(IntentRefusal.PARAMETER_OUT_OF_RANGE, parameter="target_level")
    assert "от 1 до 10" in text
    pages = phrases.intent_refusal(IntentRefusal.PARAMETER_OUT_OF_RANGE, parameter="pages")
    assert "от 1 до 200" in pages
    fraction = phrases.intent_refusal(IntentRefusal.PARAMETER_OUT_OF_RANGE, parameter="satisfy_to")
    assert "от 0 до 100 процентов" in fraction


def test_a_refusal_cannot_be_spoken_about_a_name_this_package_did_not_mint() -> None:
    with pytest.raises(ValueError, match="declared goal parameter"):
        phrases.intent_refusal(IntentRefusal.PARAMETER_OUT_OF_RANGE, parameter="зомбипицца")
    with pytest.raises(ValueError, match="capability"):
        phrases.intent_refusal(IntentRefusal.CAPABILITY_UNAVAILABLE, capability="зомбипицца")


# --------------------------------------------------------------------------
# T011 — the stop grammar wins, from the first line of the matcher
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "transcript",
    [
        "поешь... нет, стоп",
        "прокачай плотницкое до 99 уровня, стоп",
        "почитай книгу — хватит",
        "стоп, поешь",
        "agent, eat, stop",
        "поешь и попей, прекрати",
    ],
)
def test_a_stop_word_beats_every_other_intent_in_the_same_sentence(transcript: str) -> None:
    resolution = resolve(transcript)
    assert resolution.intent is VoiceIntent.STOP
    assert resolution.kind is None
    assert resolution.refusal is None
    assert resolution.candidates == ()


def test_a_stop_is_matched_before_the_capability_and_range_checks() -> None:
    # Both of the checks this transcript would otherwise fail are below the stop
    # in resolve_goal. If either ran first, the answer would be a refusal.
    resolution = resolve("прокачай плотницкое до 99 уровня, стоп", available=frozenset())
    assert resolution.intent is VoiceIntent.STOP
    assert resolution.refusal is None


# --------------------------------------------------------------------------
# T012 — an overlong transcript is truncated, not buffered
# --------------------------------------------------------------------------


def test_a_goal_word_past_the_transcript_bound_is_not_matched() -> None:
    padded = "и " * 210 + "поешь"
    assert len(padded) > 400
    assert len(said(padded).normalised()) == 400
    assert resolve(padded).refusal is IntentRefusal.NOT_A_GOAL


def test_the_same_goal_word_inside_the_bound_is_matched() -> None:
    within = "и " * 100 + "поешь"
    assert len(within) < 400
    assert resolve(within).kind is GoalKind.SATISFY_HUNGER


def test_a_quantity_past_the_bound_cannot_reach_a_parameter() -> None:
    padded = "прокачай плотницкое " + "и " * 210 + "до 99 уровня"
    resolution = resolve(padded)
    assert resolution.kind is GoalKind.TRAIN_SKILL
    assert resolution.params.target_level is None


def test_nothing_is_carried_between_two_transcripts() -> None:
    first = resolve("прокачай навык")
    assert first.refusal is IntentRefusal.SKILL_NOT_NAMED
    second = resolve("плотницкое")
    assert second.refusal is IntentRefusal.NOT_A_GOAL
    again = resolve("прокачай навык")
    assert again.refusal is IntentRefusal.SKILL_NOT_NAMED


# --------------------------------------------------------------------------
# T013 — a kind the build cannot serve is refused with the capability named
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("transcript", "capability", "noun"),
    [
        ("поешь", EAT_CAP, "приём пищи"),
        ("попей воды", DRINK_CAP, "питьё"),
        ("почитай книгу", READ_CAP, "чтение"),
        ("прокачай плотницкое", READ_CAP, "чтение"),
        ("выучи рецепт", READ_CAP, "чтение"),
        ("иди домой", MOVE_CAP, "передвижение"),
        ("перевяжись", BANDAGE_CAP, "перевязка"),
        ("отступай", MOVE_CAP, "передвижение"),
    ],
)
def test_a_kind_the_build_cannot_serve_names_the_missing_capability(
    transcript: str, capability: str, noun: str
) -> None:
    resolution = resolve(transcript, available=frozenset())
    assert resolution.kind is None
    assert resolution.refusal is IntentRefusal.CAPABILITY_UNAVAILABLE
    assert resolution.capability == capability
    assert noun in spoken(resolution)


def test_only_the_missing_capability_is_refused() -> None:
    without_reading = frozenset({EAT_CAP, DRINK_CAP})
    assert resolve("поешь", available=without_reading).kind is GoalKind.SATISFY_HUNGER
    refused = resolve("почитай книгу", available=without_reading)
    assert refused.refusal is IntentRefusal.CAPABILITY_UNAVAILABLE
    assert refused.capability == READ_CAP


def test_the_capability_set_this_package_can_require() -> None:
    assert ALL_VOICE_CAPABILITIES == EVERYTHING


def test_an_unavailable_capability_must_be_named() -> None:
    with pytest.raises(ValueError, match="unavailable capability"):
        GoalResolution(intent=VoiceIntent.UNKNOWN, refusal=IntentRefusal.CAPABILITY_UNAVAILABLE)


# --------------------------------------------------------------------------
# T014 — the grammar and the kind set stay in step
# --------------------------------------------------------------------------

GOOD_GRAMMAR: dict[str, frozenset[str]] = {
    "satisfy_hunger": frozenset({"поешь"}),
    "satisfy_thirst": frozenset({"попей"}),
    "read_for_boredom": frozenset({"почитай"}),
    "train_skill": frozenset({"прокачай"}),
    "learn_recipe": frozenset({"рецепт"}),
    "return_home": frozenset({"домой"}),
    "treat_wounds": frozenset({"перевяжись"}),
    "avoid_threat": frozenset({"отступай"}),
}


def test_the_reference_grammar_in_this_test_is_accepted() -> None:
    check_grammar(GOOD_GRAMMAR)


def test_a_grammar_naming_a_kind_that_does_not_exist_is_rejected() -> None:
    invented = {**GOOD_GRAMMAR, "sharpen_axe": frozenset({"наточи"})}
    with pytest.raises(RuntimeError, match="do not exist"):
        check_grammar(invented)


def test_a_kind_with_no_russian_phrasing_is_rejected() -> None:
    incomplete = {name: words for name, words in GOOD_GRAMMAR.items() if name != "learn_recipe"}
    with pytest.raises(RuntimeError, match="no Russian phrasing"):
        check_grammar(incomplete)


def test_a_kind_with_an_empty_vocabulary_is_rejected() -> None:
    hollow = {**GOOD_GRAMMAR, "learn_recipe": frozenset()}
    with pytest.raises(RuntimeError, match="cannot be reached"):
        check_grammar(hollow)


def test_a_word_claimed_by_two_kinds_is_rejected() -> None:
    overlapping = {**GOOD_GRAMMAR, "learn_recipe": frozenset({"поешь"})}
    with pytest.raises(RuntimeError, match="claimed by both"):
        check_grammar(overlapping)


def test_the_shipped_grammar_reaches_every_kind() -> None:
    reached = set()
    for value, spellings in ATTESTED.items():
        for phrase in spellings:
            resolution = resolve(phrase)
            if resolution.kind is not None:
                reached.add(resolution.kind.value)
        assert value in reached, f"no attested phrase reaches {value}"
    assert reached == KIND_VALUES


# ===========================================================================
# The claims inherited from the deleted second resolver (R-007)
# ===========================================================================
#
# Everything below restates, against :func:`pz_agent_voice.intent.resolve_goal`,
# the behavioural claims of ``pz_agent_voice.intents`` — a second
# utterance-to-GoalKind resolver that production never imported and that
# ``docs/control/BLOCKERS.md`` R-007 deletes. The file's rules survive the move:
# stop wins first, the kind table is closed or the phrase is refused by name,
# numbers are typed and range-checked, and the bound is applied before any
# matching. Claims that only made sense for the deleted module's own design —
# a ``too_long`` refusal instead of bounded-prefix matching, dedicated
# ambiguous-skill/unitless-number codes instead of the survivor's folds, its
# private idempotency-key mint instead of the session's IdFactory — are gone
# with it, and the survivor's equivalents are pinned above and below instead.

# --- stop, first and from anywhere -----------------------------------------

#: One Cyrillic letter, used only as padding to push a string past the bound,
#: written as an escape so this file carries no ambiguous character.
PAD = "\u0430"

#: Written out rather than taken from STOP_WORDS: this list is the claim that
#: these particular sounds stop the agent, and it fails if one is dropped from
#: the vocabulary the resolver reads.
STOP_UTTERANCES: tuple[str, ...] = (
    "стоп",
    "стой",
    "стойте",
    "стоять",
    "остановись",
    "остановите",
    "остановить",
    "хватит",
    "прекрати",
    "прекратить",
    "stop",
    "halt",
    "abort",
)


@pytest.mark.parametrize("utterance", STOP_UTTERANCES)
def test_each_stop_word_stops(utterance: str) -> None:
    assert resolve(utterance).intent is VoiceIntent.STOP
    assert is_stop(said(utterance))


@pytest.mark.parametrize("utterance", STOP_UTTERANCES)
def test_stop_wins_over_a_goal_in_the_same_breath(utterance: str) -> None:
    """A stop next to a perfectly good goal is a stop, wherever it sits."""
    for phrase in (
        f"агент {utterance} поешь",
        f"поешь {utterance}",
        f"прокачай механику до 7 {utterance}",
        f"{utterance}, почитай книгу",
    ):
        resolution = resolve(phrase)
        assert resolution.intent is VoiceIntent.STOP, phrase
        assert resolution.kind is None
        assert resolution.refusal is None


def test_stop_is_case_and_punctuation_insensitive() -> None:
    for phrase in ("СТОП", "Стоп!", "  стоп...  ", "АГЕНТ, СТОЙ!"):
        assert resolve(phrase).intent is VoiceIntent.STOP, phrase


def test_stop_survives_the_transcript_bound() -> None:
    """An over-long utterance still stops when the stop is inside the bound.

    The bound is applied to the raw string before any matching, and the stop
    vocabulary is scanned from the surviving prefix like everything else — so a
    stop word inside the bound works however much noise follows it.
    """
    assert resolve("стоп " + PAD * 800).intent is VoiceIntent.STOP


def test_a_stop_past_the_bound_is_not_seen() -> None:
    """The documented limit of the bound, pinned so it cannot widen unnoticed.

    Nothing is buffered to find it: the mitigation is upstream, where the
    driver runs ``is_stop`` against every interim transcript, so the stop is
    caught while the sentence is still short.
    """
    late = resolve(PAD * 400 + " стоп")
    assert late.intent is VoiceIntent.UNKNOWN
    assert late.refusal is IntentRefusal.NOT_A_GOAL


def test_a_word_containing_a_stop_word_is_not_a_stop() -> None:
    for phrase in ("стопка", "остановка"):
        resolution = resolve(phrase)
        assert resolution.intent is not VoiceIntent.STOP, phrase
        assert resolution.refusal is IntentRefusal.NOT_A_GOAL


# --- the mapping itself ----------------------------------------------------

#: (utterance, kind value, expected parameters). Literal on both sides, so a
#: word wired to the wrong kind cannot round-trip its way past this table.
GOAL_UTTERANCES: tuple[tuple[str, str, dict[str, object]], ...] = (
    ("поешь", "satisfy_hunger", {}),
    ("покушай", "satisfy_hunger", {}),
    ("я проголодался", "satisfy_hunger", {}),
    ("съешь что-нибудь", "satisfy_hunger", {}),
    ("поешь на восемьдесят процентов", "satisfy_hunger", {"satisfy_to": 0.8}),
    ("поешь до 80 процентов", "satisfy_hunger", {"satisfy_to": 0.8}),
    ("поешь 80%", "satisfy_hunger", {"satisfy_to": 0.8}),
    ("попей", "satisfy_thirst", {}),
    ("попей воды", "satisfy_thirst", {}),
    ("выпей", "satisfy_thirst", {}),
    ("напейся", "satisfy_thirst", {}),
    ("попей до 50 процентов", "satisfy_thirst", {"satisfy_to": 0.5}),
    ("почитай", "read_for_boredom", {}),
    ("почитай книгу", "read_for_boredom", {}),
    ("мне скучно", "read_for_boredom", {}),
    ("почитай 20 страниц", "read_for_boredom", {"pages": 20}),
    ("выучи рецепт", "learn_recipe", {}),
    ("изучи рецепты 5 страниц", "learn_recipe", {"pages": 5}),
    ("прокачай плотничество", "train_skill", {"skill": "carpentry"}),
    ("прокачай механику до 7", "train_skill", {"skill": "mechanics", "target_level": 7}),
    ("прокачай механику до уровня 7", "train_skill", {"skill": "mechanics", "target_level": 7}),
    (
        "прокачай плотницкое до пятого уровня",
        "train_skill",
        {"skill": "carpentry", "target_level": 5},
    ),
    ("тренируй рыбалку 30 страниц", "train_skill", {"skill": "fishing", "pages": 30}),
    ("домой", "return_home", {}),
    ("идём домой", "return_home", {}),
    ("возвращайся домой", "return_home", {}),
    ("вернись домой", "return_home", {}),
    ("перевяжись", "treat_wounds", {}),
    ("обработай раны", "treat_wounds", {}),
    ("забинтуй рану", "treat_wounds", {}),
    ("отступай", "avoid_threat", {}),
    ("беги", "avoid_threat", {}),
    ("уходи оттуда", "avoid_threat", {}),
    ("агент, отступи", "avoid_threat", {}),
)


@pytest.mark.parametrize(("utterance", "kind_value", "expected"), GOAL_UTTERANCES)
def test_an_utterance_resolves_to_its_kind_and_nothing_else(
    utterance: str, kind_value: str, expected: dict[str, object]
) -> None:
    resolution = resolve(utterance)
    assert resolution.intent is VoiceIntent.GOAL, utterance
    assert resolution.resolved is True
    assert resolution.kind is not None
    assert resolution.kind.value == kind_value

    params = resolution.params
    skill_value = params.skill.value if params.skill is not None else None
    assert skill_value == expected.get("skill")
    assert params.target_level == expected.get("target_level")
    if "satisfy_to" in expected:
        assert params.satisfy_to == pytest.approx(expected["satisfy_to"])
    else:
        assert params.satisfy_to is None
    assert params.pages == expected.get("pages")
    assert params.present() == frozenset(expected)


def test_every_kind_is_reachable_by_some_hand_written_phrase() -> None:
    """A kind nobody can say is a row in a table, not a feature.

    Except the ones declared unspeakable, whose unreachability is the feature —
    pinned above in test_navigation_is_deliberately_unspeakable.
    """
    reached = {kind_value for _, kind_value, _ in GOAL_UTTERANCES}
    assert reached == {kind.value for kind in GoalKind} - UNSPEAKABLE_KIND_VALUES


#: One literal phrase per skill, so a skill whose vocabulary is wrong is named.
SKILL_UTTERANCES: tuple[tuple[str, str], ...] = (
    ("прокачай плотничество", "carpentry"),
    ("прокачай готовку", "cooking"),
    ("прокачай фермерство", "farming"),
    ("прокачай электрику", "electrical"),
    ("прокачай металлообработку", "metalworking"),
    ("прокачай механику", "mechanics"),
    ("прокачай шитье", "tailoring"),
    ("прокачай собирательство", "foraging"),
    ("прокачай рыбалку", "fishing"),
    ("прокачай ловушки", "trapping"),
    ("прокачай медицину", "first_aid"),
)


@pytest.mark.parametrize(("utterance", "skill_value"), SKILL_UTTERANCES)
def test_a_skill_utterance_resolves_to_its_skill(utterance: str, skill_value: str) -> None:
    resolution = resolve(utterance)
    assert resolution.intent is VoiceIntent.GOAL, utterance
    assert resolution.kind is GoalKind.TRAIN_SKILL
    assert resolution.params.skill is not None
    assert resolution.params.skill.value == skill_value


def test_every_skill_is_reachable_by_some_hand_written_phrase() -> None:
    reached = {skill_value for _, skill_value in SKILL_UTTERANCES}
    assert reached == {skill.value for skill in TrainableSkill}


def test_each_skill_phrase_names_exactly_one_skill() -> None:
    """Tokenisation is exact, so every inflection has to be in the table."""
    for utterance, _ in SKILL_UTTERANCES:
        assert len(matched_skills(said(utterance).words())) == 1, utterance


# --- refusals --------------------------------------------------------------

#: (utterance, refusal value, parameter named). Literal on both sides. The
#: folds are the survivor's own and are deliberate: empty and unmapped are both
#: NOT_A_GOAL, two skills fold into SKILL_NOT_NAMED rather than a dedicated
#: ambiguity, and a number the kind cannot take is refused by the parameter's
#: name rather than by a code about numbers in general.
REFUSED_UTTERANCES: tuple[tuple[str, str, str], ...] = (
    ("", "not_a_goal", ""),
    ("   ...   ", "not_a_goal", ""),
    ("бла бла бла", "not_a_goal", ""),
    ("включи музыку", "not_a_goal", ""),
    ("почитай попей", "ambiguous_goal", ""),
    ("поешь и попей", "ambiguous_goal", ""),
    ("прокачай", "skill_not_named", "skill"),
    ("прокачай навык", "skill_not_named", "skill"),
    ("прокачай механику готовку", "skill_not_named", "skill"),
    ("попей воды на плотницкое", "parameter_not_accepted", "skill"),
    ("поешь 5 страниц", "parameter_not_accepted", "pages"),
    ("почитай 50%", "parameter_not_accepted", "satisfy_to"),
    ("прокачай механику до 70", "parameter_out_of_range", "target_level"),
    ("почитай 500 страниц", "parameter_out_of_range", "pages"),
    ("почитай 1000 страниц", "parameter_out_of_range", "pages"),
    ("поешь до 150 процентов", "parameter_out_of_range", "satisfy_to"),
    ("поешь 150%", "parameter_out_of_range", "satisfy_to"),
)


@pytest.mark.parametrize(("utterance", "refusal_value", "parameter"), REFUSED_UTTERANCES)
def test_an_utterance_is_refused_with_its_reason(
    utterance: str, refusal_value: str, parameter: str
) -> None:
    resolution = resolve(utterance)
    assert resolution.intent is VoiceIntent.UNKNOWN, utterance
    assert resolution.kind is None
    assert resolution.resolved is False
    assert resolution.refusal is not None
    assert resolution.refusal.value == refusal_value
    assert resolution.parameter == parameter
    assert spoken(resolution)


def test_every_refusal_this_table_can_reach_is_reached() -> None:
    """No reachable refusal is decoration: each has an utterance producing it.

    Two are produced elsewhere because their utterances do not fit in a table:
    ``capability_unavailable`` needs an empty capability set (T013 above) and
    ``internal`` needs the module's range table to disagree with the core's
    (its own test below).
    """
    reached = {refusal_value for _, refusal_value, _ in REFUSED_UTTERANCES}
    assert reached == {refusal.value for refusal in IntentRefusal} - {
        "capability_unavailable",
        "internal",
    }


def test_an_unmapped_phrase_never_becomes_a_kind() -> None:
    for utterance in ("сделай что-нибудь", "работай", "давай", "агент", "иди на север"):
        resolution = resolve(utterance)
        assert resolution.kind is None, utterance
        assert resolution.intent is not VoiceIntent.GOAL


def test_a_residual_range_failure_becomes_the_internal_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The defensive branch: the module's range check disagreeing with the core.

    Widening the range this module reads leaves ``GoalParams`` as the only
    thing still enforcing it. The refusal that comes back must be the constant
    from the closed phrase table, not the ``ValueError`` — which quotes the
    value the user spoke.
    """
    monkeypatch.setattr(
        intent,
        "NUMERIC_RANGES",
        {**core_goals.NUMERIC_RANGES, "target_level": core_goals.NumericRange(1, 999)},
    )
    resolution = resolve("прокачай механику до 70")
    assert resolution.refusal is IntentRefusal.INTERNAL
    assert resolution.kind is None
    sentence = spoken(resolution)
    assert sentence == phrases.REFUSAL_SPEECH[IntentRefusal.INTERNAL]
    assert "70" not in sentence


# --- ranges, at the edges --------------------------------------------------

ACCEPTED_EDGES: tuple[tuple[str, str, object], ...] = (
    ("прокачай механику до 1", "target_level", 1),
    ("прокачай механику до 10", "target_level", 10),
    ("почитай 1 страницу", "pages", 1),
    ("почитай 200 страниц", "pages", 200),
    ("поешь до 0 процентов", "satisfy_to", 0.0),
    ("поешь до 100 процентов", "satisfy_to", 1.0),
)

REFUSED_EDGES: tuple[str, ...] = (
    "прокачай механику до 0",
    "прокачай механику до 11",
    "почитай 0 страниц",
    "почитай 201 страницу",
    "поешь до 101 процента",
)


@pytest.mark.parametrize(("utterance", "parameter", "value"), ACCEPTED_EDGES)
def test_a_value_at_the_edge_of_the_range_is_accepted(
    utterance: str, parameter: str, value: object
) -> None:
    resolution = resolve(utterance)
    assert resolution.intent is VoiceIntent.GOAL, utterance
    assert getattr(resolution.params, parameter) == value


@pytest.mark.parametrize("utterance", REFUSED_EDGES)
def test_a_value_past_the_edge_of_the_range_is_refused(utterance: str) -> None:
    resolution = resolve(utterance)
    assert resolution.refusal is IntentRefusal.PARAMETER_OUT_OF_RANGE, utterance
    assert resolution.parameter in {"target_level", "pages", "satisfy_to"}


# --- bare numbers: one closed table, one kind ------------------------------


def test_the_bare_number_table_is_exactly_one_entry_wide() -> None:
    """Hand-written literal: only a training target survives without a unit.

    "Прокачай механику до 7" is a target level and can be nothing else; "поешь
    5" could be a percentage or a page count, and guessing is the invention the
    matcher exists to avoid.
    """
    assert intent.BARE_NUMBER_PARAM == {GoalKind.TRAIN_SKILL: "target_level"}


def test_a_bare_number_reaches_only_the_kind_that_declares_a_meaning() -> None:
    fed = resolve("поешь 5")
    assert fed.intent is VoiceIntent.GOAL
    assert fed.params.present() == frozenset()
    read = resolve("почитай 40")
    assert read.intent is VoiceIntent.GOAL
    assert read.params.pages is None


def test_two_bare_numbers_fold_into_none_rather_than_a_guess() -> None:
    resolution = resolve("прокачай механику 3 5")
    assert resolution.intent is VoiceIntent.GOAL
    assert resolution.params.target_level is None


def test_a_unit_bound_number_disables_the_bare_reading() -> None:
    """Once any unit spoke, leftover numbers are not promoted to parameters."""
    resolution = resolve("прокачай механику до 7 уровня и 9")
    assert resolution.intent is VoiceIntent.GOAL
    assert resolution.params.target_level == 7


def test_a_bare_number_is_still_range_checked() -> None:
    resolution = resolve("прокачай механику сто")
    assert resolution.refusal is IntentRefusal.PARAMETER_OUT_OF_RANGE
    assert resolution.parameter == "target_level"


def test_a_digit_run_past_the_number_bound_is_refused_even_when_its_value_would_fit() -> None:
    """The digit bound is judged on the run, before ``int`` ever sees it.

    ``0000007`` is seven digits and spells seven, which is well inside 1..200.
    It is refused anyway, and that is the observable difference the bound
    makes: a matcher that converted first and checked the range afterwards
    would accept it, and would be doing work proportional to a digit run the
    speaker chose for a value it could have rejected by length.
    """
    fits = resolve("почитай 000007 страниц")
    assert fits.intent is VoiceIntent.GOAL
    assert fits.params.pages == 7

    refused = resolve("почитай 0000007 страниц")
    assert refused.refusal is IntentRefusal.PARAMETER_OUT_OF_RANGE
    assert refused.parameter == "pages"


def test_a_non_ascii_digit_is_not_a_bare_number() -> None:
    resolution = resolve(f"прокачай механику {ARABIC_INDIC_5}")
    assert resolution.intent is VoiceIntent.GOAL
    assert resolution.params.target_level is None
    assert resolution.params.skill is TrainableSkill.MECHANICS


# --- the percent sign ------------------------------------------------------


def test_the_percent_sign_binds_only_a_digit_run() -> None:
    """The sign is how a numeral recogniser spells "процентов", nothing more."""
    worded = resolve("поешь восемьдесят %")
    assert worded.intent is VoiceIntent.GOAL
    assert worded.params.satisfy_to is None


def test_a_digit_run_past_the_number_bound_with_a_percent_sign_is_refused() -> None:
    resolution = resolve("поешь " + "9" * 12 + "%")
    assert resolution.refusal is IntentRefusal.PARAMETER_OUT_OF_RANGE
    assert resolution.parameter == "satisfy_to"


def test_resolve_goal_is_total_over_percent_shaped_noise() -> None:
    for utterance in ("%%%", "% 80", "80 %", "поешь %", "поешь 80%%%"):
        resolution = resolve(utterance)
        assert resolution.intent in {VoiceIntent.GOAL, VoiceIntent.STOP, VoiceIntent.UNKNOWN}


# --- table invariants ------------------------------------------------------


def test_the_tables_cover_the_core_enums() -> None:
    assert set(intent.KIND_WORDS) == set(GoalKind) - intent.UNSPEAKABLE_KINDS
    assert set(intent.SKILL_WORDS) == set(TrainableSkill)
    assert set(intent.UNIT_WORDS) == set(core_goals.NUMERIC_RANGES) - intent.UNSPEAKABLE_PARAMS


def test_no_vocabulary_word_is_a_stop_word() -> None:
    for words in (
        *intent.KIND_WORDS.values(),
        *intent.SKILL_WORDS.values(),
        *intent.UNIT_WORDS.values(),
        frozenset(intent.NUMBER_WORDS),
    ):
        assert VOICE_STOP_WORDS.isdisjoint(words)


def test_no_word_belongs_to_two_vocabularies() -> None:
    all_words = [
        word
        for words in (
            *intent.KIND_WORDS.values(),
            *intent.SKILL_WORDS.values(),
            *intent.UNIT_WORDS.values(),
            frozenset(intent.NUMBER_WORDS),
        )
        for word in words
    ]
    assert len(all_words) == len(set(all_words))


def test_the_bare_number_kind_accepts_the_parameter_it_implies() -> None:
    for kind, parameter in intent.BARE_NUMBER_PARAM.items():
        spec = core_goals.GOAL_SPECS[kind]
        assert parameter in spec.required | spec.optional


def test_import_check_rejects_a_stop_word_in_a_vocabulary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    poisoned = {**intent.KIND_WORDS, GoalKind.SATISFY_HUNGER: frozenset({"стоп"})}
    monkeypatch.setattr(intent, "KIND_WORDS", poisoned)
    with pytest.raises(RuntimeError, match="claimed by both"):
        intent._check_channel_tables()


def test_import_check_rejects_a_word_no_transcript_can_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for bad in ("ПОЕШЬ", "поёшь", "две слова"):
        monkeypatch.setattr(
            intent,
            "KIND_WORDS",
            {**intent.KIND_WORDS, GoalKind.SATISFY_HUNGER: frozenset({bad})},
        )
        with pytest.raises(RuntimeError, match="no transcript can match"):
            intent._check_channel_tables()


def test_import_check_rejects_a_skill_with_no_vocabulary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thinned = {
        skill: words
        for skill, words in intent.SKILL_WORDS.items()
        if skill is not TrainableSkill.FISHING
    }
    monkeypatch.setattr(intent, "SKILL_WORDS", thinned)
    with pytest.raises(RuntimeError, match="fishing"):
        intent._check_channel_tables()


def test_import_check_rejects_a_unit_with_no_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    widened = {**intent.UNIT_WORDS, "inches": frozenset({"дюймов"})}
    monkeypatch.setattr(intent, "UNIT_WORDS", widened)
    with pytest.raises(RuntimeError, match="no declared numeric range"):
        intent._check_channel_tables()


def test_import_check_rejects_a_bare_number_the_kind_does_not_accept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare-number meaning the kind has no field for refuses nothing usefully.

    ``satisfy_hunger`` takes ``satisfy_to`` and no page count, so declaring
    pages as its bare-number parameter would turn "поешь 5" into a refusal the
    speaker cannot act on rather than into the goal they asked for.
    """
    monkeypatch.setattr(intent, "BARE_NUMBER_PARAM", {GoalKind.SATISFY_HUNGER: "pages"})
    with pytest.raises(RuntimeError, match="satisfy_hunger does not accept pages"):
        intent._check_channel_tables()


def test_import_check_rejects_an_unspeakable_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        phrases,
        "REFUSAL_SPEECH",
        {**phrases.REFUSAL_SPEECH, IntentRefusal.NOT_A_GOAL: "   "},
    )
    with pytest.raises(RuntimeError, match="no refusal sentence"):
        phrases._check_speech_tables()


def test_import_check_rejects_a_refusal_too_long_to_speak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sentence past ``MAX_TEXT_CHARS`` is truncated on the way to the queue.

    Truncated, the user hears half of what went wrong and none of what to say
    instead, which is worse than the refusal not existing.
    """
    monkeypatch.setattr(
        phrases,
        "REFUSAL_SPEECH",
        {**phrases.REFUSAL_SPEECH, IntentRefusal.NOT_A_GOAL: PAD * (MAX_TEXT_CHARS + 1)},
    )
    with pytest.raises(RuntimeError, match="too long to speak"):
        phrases._check_speech_tables()


# --- the import-time check, and that it is actually reached -----------------

_PROBE_NAME = "pz_agent_voice._intent_executed_afresh"


def _executed_afresh() -> ModuleType:
    """Run ``intent.py`` again as a new module, exactly as an import would.

    A fresh module object rather than :func:`importlib.reload`, which executes
    the file in the *live* module's namespace: a failure part-way through would
    leave the module every other test in this file imported half-rebuilt.
    """
    spec = importlib.util.spec_from_file_location(_PROBE_NAME, intent.__file__)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "pz_agent_voice"
    sys.modules[_PROBE_NAME] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(_PROBE_NAME, None)
    return module


def test_the_table_check_runs_at_import_and_not_only_when_it_is_called(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every check above calls ``_check_channel_tables`` by hand; this never does.

    A module whose self-check is written correctly and never invoked passes all
    of them and still ships a grammar that disagrees with itself. So the import
    is made to fail instead: a parameter's range is removed underneath the
    module, and a fresh execution of the same file has to refuse to finish.
    """
    assert _executed_afresh().MAX_NUMBER_CHARS == intent.MAX_NUMBER_CHARS

    thinned = {
        name: bounds for name, bounds in core_goals.NUMERIC_RANGES.items() if name != "pages"
    }
    monkeypatch.setattr(core_goals, "NUMERIC_RANGES", thinned)
    with pytest.raises(RuntimeError, match="no declared numeric range"):
        _executed_afresh()


# --- the bounds, as literals rather than as symbols -------------------------


def test_the_declared_bounds_are_the_numbers_this_grammar_was_written_against() -> None:
    """Hand-written literals. Widening a bound arrives here as a failure.

    The transcript bound is the whole of what makes the matcher's work bounded
    — the module's only defence against a recogniser that emits an accumulated
    paragraph — and read as a symbol it is satisfied by any number at all. The
    survivor truncates to it and matches the surviving prefix; the T012 tests
    above pin that behaviour at the literal.
    """
    assert messages.MAX_TRANSCRIPT_CHARS == 400
    assert intent.MAX_NUMBER_CHARS == 6
    assert intent.UNIT_WINDOW == 2
    assert len(said("я" * 500).normalised()) == 400


def test_word_tokens_are_normalised_and_bounded() -> None:
    assert said("  ПОЕШЬ,  Ёлку!  ").words() == ("поешь", "елку")
    words = said("я" * 500).words()
    assert words == ("я" * 400,)
