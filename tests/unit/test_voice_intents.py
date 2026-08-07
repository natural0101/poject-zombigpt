"""Russian speech onto the closed goal channel: what maps, and what is refused.

Everything here is written against the *outside* of :func:`resolve_goal`. The
tables it consults are re-stated in this file as literals rather than imported
and compared against themselves, because a test that reads the grammar back out
of the grammar passes whatever the grammar says — including "nothing".
"""

from __future__ import annotations

import pytest

from pz_agent_core.goals import GoalKind, GoalRequest, TrainableSkill, parse_kind, parse_skill
from pz_agent_voice import phrases
from pz_agent_voice.intent import (
    ALL_VOICE_CAPABILITIES,
    GoalResolution,
    check_grammar,
    extract_quantities,
    matched_skill,
    matched_skills,
    resolve_goal,
)
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


def test_no_transcript_can_produce_a_kind_outside_the_closed_set() -> None:
    corpus = [phrase for spellings in ATTESTED.values() for phrase in spellings]
    corpus += [
        "сегодня пасмурно",
        "поешь и попей",
        "стоп",
        "прокачай что-нибудь",
        "почитай 0 страниц",
    ]
    for transcript in corpus:
        resolution = resolve(transcript)
        assert resolution.kind is None or resolution.kind in set(GoalKind)


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
