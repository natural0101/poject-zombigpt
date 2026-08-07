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
from pz_agent_core.protocol import ReasonCode
from pz_agent_voice import intents, messages, phrases
from pz_agent_voice.intent import (
    ALL_VOICE_CAPABILITIES,
    GoalResolution,
    check_grammar,
    extract_quantities,
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

EVERYTHING = frozenset({EAT_CAP, DRINK_CAP, READ_CAP})

#: The five kinds the channel carries, as an independent literal.
KIND_VALUES = frozenset(
    {"satisfy_hunger", "satisfy_thirst", "read_for_boredom", "train_skill", "learn_recipe"}
)

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
    # An independently written literal. A sixth kind arrives here as a failure
    # rather than as a kind speech cannot reach.
    assert {kind.value for kind in GoalKind} == KIND_VALUES


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
# pz_agent_voice.intents — the utterance-to-GoalKind resolver
# ===========================================================================
#
# Everything below tests :mod:`pz_agent_voice.intents`, which is a separate
# module from the ``intent.resolve_goal`` grammar exercised above. The tables
# here are hand-written on both sides: a resolver and a test that both read
# ``intents.KIND_WORDS`` agree with each other no matter which kind each word
# points at, so "поешь" wired to ``satisfy_thirst`` would round-trip perfectly
# and still be wrong.

# --- stop, first and from anywhere -----------------------------------------

#: One Cyrillic letter, used only as padding to push a string past the bound.
#: Named because the confusable-character rule reads a bare Cyrillic "a" as a
#: mistyped Latin one, and rewriting it would change what is being measured.
PAD = "\u0430"

#: The Arabic-Indic digit five, written as an escape so this file carries no
#: ambiguous character. ``int()`` reads it as 5; the matcher must not.
ARABIC_INDIC_FIVE = "\u0665"

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
def test_intents_each_stop_word_stops(utterance: str) -> None:
    assert intents.resolve(utterance).outcome is intents.VoiceOutcome.STOP
    assert intents.is_stop_utterance(utterance)


@pytest.mark.parametrize("utterance", STOP_UTTERANCES)
def test_intents_stop_wins_over_a_goal_in_the_same_breath(utterance: str) -> None:
    """A stop next to a perfectly good goal is a stop, wherever it sits."""
    for phrase in (
        f"агент {utterance} поешь",
        f"поешь {utterance}",
        f"прокачай механику до 7 {utterance}",
        f"{utterance}, почитай книгу",
    ):
        resolution = intents.resolve(phrase)
        assert resolution.outcome is intents.VoiceOutcome.STOP, phrase
        assert resolution.kind is None
        assert resolution.refusal is None


def test_intents_stop_is_case_and_punctuation_insensitive() -> None:
    for phrase in ("СТОП", "Стоп!", "  стоп...  ", "АГЕНТ, СТОЙ!"):
        assert intents.resolve(phrase).outcome is intents.VoiceOutcome.STOP, phrase


def test_intents_stop_wins_over_the_length_bound() -> None:
    """An over-long utterance is refused — unless it contains a stop."""
    padding = PAD * (intents.MAX_UTTERANCE_CHARS * 2)
    stopped = intents.resolve(f"стоп {padding}")
    assert stopped.outcome is intents.VoiceOutcome.STOP
    assert stopped.truncated is True
    assert intents.resolve(f"поешь {padding}").refusal is not None


def test_intents_a_stop_past_the_bound_is_not_seen() -> None:
    """The documented limit of the bound, pinned so it cannot widen unnoticed.

    Nothing is buffered to find it: the mitigation is upstream, where every
    interim transcript is tested while the sentence is still short.
    """
    late = intents.resolve(PAD * intents.MAX_UTTERANCE_CHARS + " стоп")
    assert late.outcome is intents.VoiceOutcome.REFUSED
    assert late.refusal is not None
    assert late.refusal.code is intents.VoiceRefusalCode.TOO_LONG


def test_intents_a_word_containing_a_stop_word_is_not_a_stop() -> None:
    assert intents.resolve("стопка").outcome is intents.VoiceOutcome.REFUSED
    assert intents.resolve("остановка").outcome is intents.VoiceOutcome.REFUSED


# --- the mapping itself ----------------------------------------------------

#: (utterance, kind value, expected parameters). Literal on both sides.
GOAL_UTTERANCES: tuple[tuple[str, str, dict[str, object]], ...] = (
    ("поешь", "satisfy_hunger", {}),
    ("покушай", "satisfy_hunger", {}),
    ("я проголодался", "satisfy_hunger", {}),
    ("съешь что-нибудь", "satisfy_hunger", {}),
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
    ("тренируй рыбалку 30 страниц", "train_skill", {"skill": "fishing", "pages": 30}),
)


@pytest.mark.parametrize(("utterance", "kind_value", "expected"), GOAL_UTTERANCES)
def test_intents_utterance_resolves_to_its_kind(
    utterance: str, kind_value: str, expected: dict[str, object]
) -> None:
    resolution = intents.resolve(utterance)
    assert resolution.outcome is intents.VoiceOutcome.GOAL, utterance
    assert resolution.kind is not None
    assert resolution.kind.value == kind_value
    assert resolution.accepted is True

    params = resolution.params
    skill_value = params.skill.value if params.skill is not None else None
    assert skill_value == expected.get("skill")
    assert params.target_level == expected.get("target_level")
    assert params.satisfy_to == expected.get("satisfy_to")
    assert params.pages == expected.get("pages")
    assert params.present() == frozenset(expected)


def test_intents_every_kind_is_reachable_by_some_hand_written_phrase() -> None:
    """A kind nobody can say is a row in a table, not a feature."""
    reached = {kind_value for _, kind_value, _ in GOAL_UTTERANCES}
    assert reached == {kind.value for kind in GoalKind}


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
def test_intents_skill_utterance_resolves_to_its_skill(utterance: str, skill_value: str) -> None:
    resolution = intents.resolve(utterance)
    assert resolution.outcome is intents.VoiceOutcome.GOAL, utterance
    assert resolution.kind is GoalKind.TRAIN_SKILL
    assert resolution.params.skill is not None
    assert resolution.params.skill.value == skill_value


def test_intents_every_skill_is_reachable_by_some_hand_written_phrase() -> None:
    reached = {skill_value for _, skill_value in SKILL_UTTERANCES}
    assert reached == {skill.value for skill in TrainableSkill}


def test_intents_each_skill_phrase_names_exactly_one_skill() -> None:
    """Tokenisation is exact, so every inflection has to be in the table."""
    for utterance, _ in SKILL_UTTERANCES:
        words, _ = intents.bounded_words(utterance)
        assert len(intents.matched_skills(words)) == 1, utterance


# --- refusals --------------------------------------------------------------

#: (utterance, refusal code value, parameter named). Literal on both sides.
REFUSED_UTTERANCES: tuple[tuple[str, str, str], ...] = (
    ("", "empty", ""),
    ("   ...   ", "empty", ""),
    ("бла бла бла", "unmapped", ""),
    ("включи музыку", "unmapped", ""),
    ("почитай попей", "ambiguous_kind", ""),
    ("поешь и попей", "ambiguous_kind", ""),
    ("прокачай", "unknown_skill", "skill"),
    ("прокачай навык", "unknown_skill", "skill"),
    ("прокачай механику готовку", "ambiguous_skill", "skill"),
    ("почитай про механику", "parameter_not_allowed", "skill"),
    ("поешь 5 страниц", "parameter_not_allowed", "pages"),
    ("поешь 5", "unitless_number", ""),
    ("почитай 10 страниц до 5 процентов", "conflicting_parameters", ""),
    ("почитай 10 страниц 20 страниц", "ambiguous_number", ""),
    ("почитай страниц", "missing_number", "pages"),
    ("прокачай механику до 70", "out_of_range", "target_level"),
    ("почитай 500 страниц", "out_of_range", "pages"),
    ("почитай 1000 страниц", "out_of_range", "pages"),
    ("поешь до 150 процентов", "out_of_range", "satisfy_to"),
)


@pytest.mark.parametrize(("utterance", "code_value", "parameter"), REFUSED_UTTERANCES)
def test_intents_utterance_is_refused_with_its_reason(
    utterance: str, code_value: str, parameter: str
) -> None:
    resolution = intents.resolve(utterance)
    assert resolution.outcome is intents.VoiceOutcome.REFUSED, utterance
    assert resolution.kind is None
    assert resolution.accepted is False
    assert resolution.refusal is not None
    assert resolution.refusal.code.value == code_value
    assert resolution.refusal.parameter == parameter


def test_intents_every_refusal_code_is_reachable() -> None:
    """No refusal code is decoration: each has an utterance that produces it.

    Two are produced elsewhere because their utterances do not fit in a table —
    ``too_long`` needs a string longer than the bound and ``internal`` needs the
    module's range table to disagree with the core's.
    """
    reached = {code_value for _, code_value, _ in REFUSED_UTTERANCES}
    assert reached == {code.value for code in intents.VoiceRefusalCode} - {
        "internal",
        "too_long",
    }
    assert intents.resolve(PAD * (intents.MAX_UTTERANCE_CHARS + 1)).refusal is not None


def test_intents_an_unmapped_phrase_never_becomes_a_kind() -> None:
    for utterance in ("сделай что-нибудь", "работай", "давай", "агент", "иди на север"):
        resolution = intents.resolve(utterance)
        assert resolution.kind is None, utterance
        assert resolution.outcome is not intents.VoiceOutcome.GOAL


def test_intents_out_of_range_names_the_declared_range() -> None:
    resolution = intents.resolve("прокачай механику до 70")
    assert resolution.refusal is not None
    assert resolution.refusal.allowed == core_goals.NUMERIC_RANGES["target_level"]
    assert resolution.refusal.allowed is not None
    assert (resolution.refusal.allowed.minimum, resolution.refusal.allowed.maximum) == (1, 10)


def test_intents_a_residual_range_failure_becomes_an_internal_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The defensive branch: the module's range check disagreeing with the core.

    Widening the range this module reads leaves ``GoalParams`` as the only thing
    still enforcing it. The refusal that comes back must be the module's own
    constant, not the ``ValueError`` — which quotes the value the user spoke.
    """
    monkeypatch.setattr(
        intents,
        "NUMERIC_RANGES",
        {**core_goals.NUMERIC_RANGES, "target_level": core_goals.NumericRange(1, 999)},
    )
    resolution = intents.resolve("прокачай механику до 70")
    assert resolution.refusal is not None
    assert resolution.refusal.code is intents.VoiceRefusalCode.INTERNAL
    assert resolution.refusal.message == intents.REFUSAL_MESSAGES[intents.VoiceRefusalCode.INTERNAL]
    assert "70" not in resolution.refusal.message


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
def test_intents_a_value_at_the_edge_of_the_range_is_accepted(
    utterance: str, parameter: str, value: object
) -> None:
    resolution = intents.resolve(utterance)
    assert resolution.outcome is intents.VoiceOutcome.GOAL, utterance
    assert getattr(resolution.params, parameter) == value


@pytest.mark.parametrize("utterance", REFUSED_EDGES)
def test_intents_a_value_past_the_edge_of_the_range_is_refused(utterance: str) -> None:
    resolution = intents.resolve(utterance)
    assert resolution.outcome is intents.VoiceOutcome.REFUSED, utterance
    assert resolution.refusal is not None
    assert resolution.refusal.code is intents.VoiceRefusalCode.OUT_OF_RANGE


def test_intents_a_non_ascii_digit_is_not_a_number() -> None:
    """``int`` reads the Arabic-Indic five as five; the matcher must not."""
    resolution = intents.resolve(f"прокачай механику {ARABIC_INDIC_FIVE}")
    assert resolution.outcome is intents.VoiceOutcome.GOAL
    assert resolution.params.target_level is None
    assert resolution.params.skill is TrainableSkill.MECHANICS


# --- the bound -------------------------------------------------------------


def test_intents_the_bound_is_exact() -> None:
    head = "почитай "
    exact = head + PAD * (intents.MAX_UTTERANCE_CHARS - len(head))
    assert len(exact) == intents.MAX_UTTERANCE_CHARS
    accepted = intents.resolve(exact)
    assert accepted.outcome is intents.VoiceOutcome.GOAL
    assert accepted.truncated is False

    refused = intents.resolve(exact + PAD)
    assert refused.truncated is True
    assert refused.refusal is not None
    assert refused.refusal.code is intents.VoiceRefusalCode.TOO_LONG


def test_intents_bounded_words_normalises_and_bounds() -> None:
    assert intents.bounded_words("  ПОЕШЬ,  Ёлку!  ") == (("поешь", "елку"), False)
    words, truncated = intents.bounded_words("я" * (intents.MAX_UTTERANCE_CHARS + 50))
    assert truncated is True
    assert words == ("я" * intents.MAX_UTTERANCE_CHARS,)


def test_intents_nothing_is_carried_between_calls() -> None:
    """Two halves of a split sentence do not add up to a goal."""
    assert intents.resolve("прокачай").refusal is not None
    second = intents.resolve("механику")
    assert second.outcome is intents.VoiceOutcome.REFUSED
    assert second.refusal is not None
    assert second.refusal.code is intents.VoiceRefusalCode.UNMAPPED


def test_intents_resolve_is_total_over_hostile_input() -> None:
    hostile = (
        "",
        " ",
        "\x00\x01\x02",
        "\n\t\r",
        "?" * 500,
        "1234567890" * 40,
        ARABIC_INDIC_FIVE * 3,
        "поешь " * 60,
        "🍖🍖🍖",
        "<script>alert(1)</script>",
        "поешь; rm -rf /",
    )
    for utterance in hostile:
        assert isinstance(intents.resolve(utterance), intents.VoiceResolution), utterance


# --- table invariants ------------------------------------------------------


def test_intents_tables_cover_the_core_enums() -> None:
    assert set(intents.KIND_WORDS) == set(GoalKind)
    assert set(intents.SKILL_WORDS) == set(TrainableSkill)
    assert set(intents.REFUSAL_MESSAGES) == set(intents.VoiceRefusalCode)
    assert set(intents.REFUSAL_REASONS) == set(intents.VoiceRefusalCode)


def test_intents_no_vocabulary_word_is_a_stop_word() -> None:
    for words in (
        *intents.KIND_WORDS.values(),
        *intents.SKILL_WORDS.values(),
        frozenset(intents.UNIT_WORDS),
    ):
        assert VOICE_STOP_WORDS.isdisjoint(words)


def test_intents_no_word_belongs_to_two_vocabularies() -> None:
    all_words = [
        word
        for words in (
            *intents.KIND_WORDS.values(),
            *intents.SKILL_WORDS.values(),
            frozenset(intents.UNIT_WORDS),
        )
        for word in words
    ]
    assert len(all_words) == len(set(all_words))


def test_intents_every_unit_names_a_parameter_the_channel_bounds() -> None:
    for parameter in intents.UNIT_WORDS.values():
        assert parameter in core_goals.NUMERIC_RANGES


def test_intents_the_bare_number_kind_accepts_the_parameter_it_implies() -> None:
    for kind, parameter in intents.BARE_NUMBER_PARAM.items():
        spec = core_goals.GOAL_SPECS[kind]
        assert parameter in spec.required | spec.optional


def test_intents_import_check_rejects_a_stop_word_in_a_vocabulary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    poisoned = {**intents.KIND_WORDS, GoalKind.SATISFY_HUNGER: frozenset({"стоп"})}
    monkeypatch.setattr(intents, "KIND_WORDS", poisoned)
    with pytest.raises(RuntimeError, match="claim the same word"):
        intents._check_tables()


def test_intents_import_check_rejects_a_word_no_transcript_can_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for bad in ("ПОЕШЬ", "поёшь", "две слова"):
        monkeypatch.setattr(
            intents,
            "KIND_WORDS",
            {**intents.KIND_WORDS, GoalKind.SATISFY_HUNGER: frozenset({bad})},
        )
        with pytest.raises(RuntimeError, match="no transcript can match"):
            intents._check_tables()


def test_intents_import_check_rejects_a_kind_with_no_vocabulary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thinned = {k: v for k, v in intents.KIND_WORDS.items() if k is not GoalKind.LEARN_RECIPE}
    monkeypatch.setattr(intents, "KIND_WORDS", thinned)
    with pytest.raises(RuntimeError, match="learn_recipe"):
        intents._check_tables()


def test_intents_import_check_rejects_a_skill_with_no_vocabulary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thinned = {k: v for k, v in intents.SKILL_WORDS.items() if k is not TrainableSkill.FISHING}
    monkeypatch.setattr(intents, "SKILL_WORDS", thinned)
    with pytest.raises(RuntimeError, match="fishing"):
        intents._check_tables()


def test_intents_import_check_rejects_a_unit_with_no_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(intents, "UNIT_WORDS", {**intents.UNIT_WORDS, "дюймов": "inches"})
    with pytest.raises(RuntimeError, match="no declared range"):
        intents._check_tables()


def test_intents_import_check_rejects_an_unspeakable_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        intents,
        "REFUSAL_MESSAGES",
        {**intents.REFUSAL_MESSAGES, intents.VoiceRefusalCode.EMPTY: "   "},
    )
    with pytest.raises(RuntimeError, match="no refusal sentence"):
        intents._check_tables()


def test_intents_import_check_rejects_a_bound_above_the_transcript_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(intents, "MAX_UTTERANCE_CHARS", 10_000)
    with pytest.raises(RuntimeError, match="must not exceed"):
        intents._check_tables()


# --- refusal shape ---------------------------------------------------------


def test_intents_a_refusal_message_is_always_a_module_constant() -> None:
    for code in intents.VoiceRefusalCode:
        refusal = intents.VoiceRefusal(code=code)
        assert refusal.message is intents.REFUSAL_MESSAGES[code]
        assert refusal.message.strip()


def test_intents_no_refusal_message_has_a_slot_to_fill() -> None:
    """A template with a hole in it is how a transcript reaches a loudspeaker."""
    for message in intents.REFUSAL_MESSAGES.values():
        assert "{" not in message
        assert "}" not in message
        assert "%s" not in message
        assert "%d" not in message


def test_intents_refusal_reason_codes_are_pinned() -> None:
    assert intents.REFUSAL_REASONS[intents.VoiceRefusalCode.INTERNAL] is ReasonCode.INTERNAL_ERROR
    for code in intents.VoiceRefusalCode:
        if code is not intents.VoiceRefusalCode.INTERNAL:
            assert intents.REFUSAL_REASONS[code] is ReasonCode.INVALID_ARGUMENT


def test_intents_a_refusal_converts_to_the_core_shape() -> None:
    resolution = intents.resolve("бла бла")
    assert resolution.refusal is not None
    core = resolution.refusal.to_goal_refusal()
    assert core.reason_code is ReasonCode.INVALID_ARGUMENT
    assert core.message is intents.REFUSAL_MESSAGES[intents.VoiceRefusalCode.UNMAPPED]


def test_intents_a_refusal_cannot_name_an_invented_parameter() -> None:
    with pytest.raises(ValueError, match="declared goal parameter"):
        intents.VoiceRefusal(code=intents.VoiceRefusalCode.OUT_OF_RANGE, parameter="transcript")


def test_intents_a_range_without_a_parameter_is_refused() -> None:
    with pytest.raises(ValueError, match="which parameter"):
        intents.VoiceRefusal(
            code=intents.VoiceRefusalCode.OUT_OF_RANGE,
            allowed=core_goals.NUMERIC_RANGES["pages"],
        )


# --- resolution invariants -------------------------------------------------


def test_intents_a_goal_resolution_must_carry_a_kind() -> None:
    with pytest.raises(ValueError, match="a kind belongs to a goal resolution"):
        intents.VoiceResolution(outcome=intents.VoiceOutcome.GOAL)


def test_intents_a_stop_resolution_must_not_carry_a_kind() -> None:
    with pytest.raises(ValueError, match="a kind belongs to a goal resolution"):
        intents.VoiceResolution(outcome=intents.VoiceOutcome.STOP, kind=GoalKind.SATISFY_HUNGER)


def test_intents_a_refused_resolution_must_carry_a_refusal() -> None:
    with pytest.raises(ValueError, match="a refusal belongs to a refused resolution"):
        intents.VoiceResolution(outcome=intents.VoiceOutcome.REFUSED)


def test_intents_a_stop_resolution_must_not_carry_a_refusal() -> None:
    with pytest.raises(ValueError, match="a refusal belongs to a refused resolution"):
        intents.VoiceResolution(
            outcome=intents.VoiceOutcome.STOP,
            refusal=intents.VoiceRefusal(code=intents.VoiceRefusalCode.EMPTY),
        )


def test_intents_parameters_cannot_float_free_of_a_kind() -> None:
    with pytest.raises(ValueError, match="parameters belong to a kind"):
        intents.VoiceResolution(
            outcome=intents.VoiceOutcome.STOP, params=core_goals.GoalParams(pages=3)
        )


# --- handing the resolution on ---------------------------------------------


def test_intents_a_minted_key_is_built_from_a_kind_and_a_counter() -> None:
    assert (
        intents.mint_idempotency_key(GoalKind.TRAIN_SKILL, sequence=3) == "voice.train_skill.000003"
    )
    assert (
        intents.mint_idempotency_key(GoalKind.SATISFY_HUNGER, sequence=0)
        == "voice.satisfy_hunger.000000"
    )


def test_intents_a_key_counter_is_bounded() -> None:
    for bad in (-1, intents.MAX_VOICE_SEQUENCE + 1):
        with pytest.raises(ValueError, match="sequence must be within"):
            intents.mint_idempotency_key(GoalKind.SATISFY_THIRST, sequence=bad)


def test_intents_every_kind_mints_a_key_the_channel_accepts() -> None:
    """The core shape-checks the key; a kind whose key it rejects is a dead end."""
    params = {GoalKind.TRAIN_SKILL: core_goals.GoalParams(skill=TrainableSkill.COOKING)}
    for kind in GoalKind:
        resolution = intents.VoiceResolution(
            outcome=intents.VoiceOutcome.GOAL,
            kind=kind,
            params=params.get(kind, core_goals.GoalParams()),
        )
        request = intents.to_goal_request(resolution, sequence=intents.MAX_VOICE_SEQUENCE)
        assert isinstance(request, GoalRequest)
        assert request.kind is kind
        assert request.idempotency_key.startswith("voice.")


def test_intents_a_goal_request_carries_the_resolved_parameters() -> None:
    request = intents.to_goal_request(intents.resolve("прокачай механику до 7"), sequence=12)
    assert request.kind is GoalKind.TRAIN_SKILL
    assert request.params == core_goals.GoalParams(skill=TrainableSkill.MECHANICS, target_level=7)
    assert request.idempotency_key == "voice.train_skill.000012"
    assert request.effective_budget == core_goals.GOAL_SPECS[GoalKind.TRAIN_SKILL].budget


@pytest.mark.parametrize("utterance", ["стоп", "бла бла", "поешь 5"])
def test_intents_only_a_goal_becomes_a_request(utterance: str) -> None:
    with pytest.raises(ValueError, match="only a goal resolution"):
        intents.to_goal_request(intents.resolve(utterance), sequence=1)


# --- the bounds, as literals rather than as symbols -------------------------
#
# Every test above that touches a bound reads it out of the module. That is the
# right way to write "the bound is applied consistently" and the wrong way to
# write "the bound is 160": a test that imports the constant agrees with
# whatever the constant becomes. The three below are the numbers themselves.


def test_intents_the_declared_bounds_are_the_numbers_this_grammar_was_written_against() -> None:
    """Hand-written literals. Widening a bound arrives here as a failure.

    ``MAX_UTTERANCE_CHARS`` is the one that matters most: it is the whole of
    what makes the matcher's work bounded, and the module's only defence
    against a recogniser that emits an accumulated paragraph. Read as a symbol
    it is satisfied by any number at all.
    """
    assert intents.MAX_UTTERANCE_CHARS == 160
    assert intents.MAX_NUMBER_DIGITS == 3
    assert intents.MAX_VOICE_SEQUENCE == 999_999

    # And the behaviour at the literal, not at the symbol: 160 characters of
    # speech is one command, 161 is a paragraph and is refused as one.
    head = "почитай "
    assert intents.resolve(head + PAD * (160 - len(head))).outcome is intents.VoiceOutcome.GOAL
    over = intents.resolve(head + PAD * (161 - len(head)))
    assert over.truncated is True
    assert over.refusal is not None
    assert over.refusal.code is intents.VoiceRefusalCode.TOO_LONG


def test_intents_a_digit_run_past_the_bound_is_refused_even_when_its_value_would_fit() -> None:
    """The digit bound is judged on the run, before ``int`` ever sees it.

    ``0007`` is four digits and spells seven, which is well inside 1..200. It is
    refused anyway, and that is the observable difference the bound makes: a
    matcher that converted first and checked the range afterwards would accept
    it, and would be doing work proportional to a digit run the speaker chose
    for a value it could have rejected by length.
    """
    fits = intents.resolve("почитай 007 страниц")
    assert fits.outcome is intents.VoiceOutcome.GOAL
    assert fits.params.pages == 7

    refused = intents.resolve("почитай 0007 страниц")
    assert refused.outcome is intents.VoiceOutcome.REFUSED
    assert refused.refusal is not None
    assert refused.refusal.code is intents.VoiceRefusalCode.OUT_OF_RANGE
    assert refused.refusal.parameter == "pages"


# --- the import-time check, and that it is actually reached -----------------

_PROBE_NAME = "pz_agent_voice._intents_executed_afresh"


def _executed_afresh() -> ModuleType:
    """Run ``intents.py`` again as a new module, exactly as an import would.

    A fresh module object rather than :func:`importlib.reload`, which executes
    the file in the *live* module's namespace: a failure part-way through would
    leave the module every other test in this file imported half-rebuilt.
    """
    spec = importlib.util.spec_from_file_location(_PROBE_NAME, intents.__file__)
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


def test_intents_the_table_check_runs_at_import_and_not_only_when_it_is_called(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every check above calls ``_check_tables`` by hand; this one never does.

    A module whose self-check is written correctly and never invoked passes all
    of them and still ships a grammar that disagrees with itself. So the import
    is made to fail instead: the transcript bound is narrowed underneath the
    module, and a fresh execution of the same file has to refuse to finish.
    """
    assert _executed_afresh().MAX_UTTERANCE_CHARS == intents.MAX_UTTERANCE_CHARS

    monkeypatch.setattr(messages, "MAX_TRANSCRIPT_CHARS", 10)
    with pytest.raises(RuntimeError, match="must not exceed"):
        _executed_afresh()


def test_intents_import_check_rejects_a_refusal_too_long_to_speak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sentence past ``MAX_TEXT_CHARS`` is truncated on the way to the queue.

    Truncated, the user hears half of what went wrong and none of what to say
    instead, which is worse than the refusal not existing.
    """
    monkeypatch.setattr(
        intents,
        "REFUSAL_MESSAGES",
        {
            **intents.REFUSAL_MESSAGES,
            intents.VoiceRefusalCode.EMPTY: PAD * (messages.MAX_TEXT_CHARS + 1),
        },
    )
    with pytest.raises(RuntimeError, match="too long to speak"):
        intents._check_tables()


def test_intents_import_check_rejects_a_refusal_with_no_reason_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A code with no wire reason is a refusal nothing downstream can report."""
    monkeypatch.setattr(
        intents,
        "REFUSAL_REASONS",
        {
            code: reason
            for code, reason in intents.REFUSAL_REASONS.items()
            if code is not intents.VoiceRefusalCode.OUT_OF_RANGE
        },
    )
    with pytest.raises(RuntimeError, match="has no reason code"):
        intents._check_tables()


def test_intents_import_check_rejects_a_key_shape_the_channel_would_reject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A minted key longer than the channel accepts is a goal that cannot be sent.

    ``GoalRequest`` refuses it at construction, so the failure would land on the
    first voice-submitted goal of whichever kind has the longest name rather
    than at import.
    """
    monkeypatch.setattr(intents, "_KEY_PREFIX", "v" * core_goals.MAX_IDEMPOTENCY_KEY_LEN)
    with pytest.raises(RuntimeError, match="exceed the channel's bound"):
        intents._check_tables()


def test_intents_import_check_rejects_a_bare_number_the_kind_does_not_accept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare-number meaning the kind has no field for refuses nothing usefully.

    ``satisfy_hunger`` takes ``satisfy_to`` and no page count, so declaring
    pages as its bare-number parameter would turn "поешь 5" into a refusal the
    speaker cannot act on rather than into the goal they asked for.
    """
    monkeypatch.setattr(intents, "BARE_NUMBER_PARAM", {GoalKind.SATISFY_HUNGER: "pages"})
    with pytest.raises(RuntimeError, match="satisfy_hunger does not accept pages"):
        intents._check_tables()
