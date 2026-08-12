from datetime import UTC, datetime

from app.domain.event_references import (
    G20_SOVEREIGN_MEMBERS,
    NUCLEAR_ARMED_STATES,
    UN_MEMBER_STATES,
    normalize_iso_country_code,
)
from app.integrations.openai_events import (
    PANDEMIC_CRITERIA,
    CriterionEvidence,
    EventAssessment,
    EventType,
    ModelVerdict,
    RetrievedSource,
    _collect_sources,
    is_allowed_source,
    validate_assessment,
)


def criterion(name: str, url: str) -> CriterionEvidence:
    return CriterionEvidence(
        criterion=name,
        supported=True,
        explanation="Explicit support.",
        citation_urls=[url],
    )


def complete_war_assessment(
    *, attacker_country_code: str | None = "PK", target_country_code: str | None = "UA"
) -> tuple[EventAssessment, list[RetrievedSource]]:
    un = "https://news.un.org/en/story/2026/08/example"
    reuters = "https://www.reuters.com/world/example"
    ap = "https://apnews.com/article/example"
    assessment = EventAssessment(
        verdict=ModelVerdict.CONFIRMED,
        event_type=EventType.MAJOR_INTERSTATE_WAR,
        event_key="state-a-state-b-2026",
        original_qualifying_timestamp=datetime(2026, 8, 1, tzinfo=UTC),
        attacker_country_code=attacker_country_code,
        target_country_code=target_country_code,
        summary="Candidate event.",
        criteria=[
            criterion("UN_CROSS_BORDER_OPERATION", un),
            criterion("ATTACKER_SOVEREIGN_STATE", un),
            criterion("ATTACKER_G20_OR_NUCLEAR", reuters),
            criterion("TARGET_UN_MEMBER_STATE", un),
            CriterionEvidence(
                criterion="TWO_INDEPENDENT_CORROBORATORS",
                supported=True,
                explanation="Reuters and AP independently corroborate the event.",
                citation_urls=[reuters, ap],
            ),
        ],
        contradictions=[],
    )
    sources = [
        RetrievedSource(url=un, title="UN", source_class="UN"),
        RetrievedSource(url=reuters, title="Reuters", source_class="REUTERS"),
        RetrievedSource(url=ap, title="AP", source_class="AP"),
    ]
    return assessment, sources


def test_source_allowlist_rejects_lookalike_domain() -> None:
    assert is_allowed_source("https://www.who.int/news/item")
    assert not is_allowed_source("https://who.int.attacker.example/item")
    assert not is_allowed_source("https://example.com/?next=who.int")


def test_collect_sources_uses_only_web_search_tool_provenance() -> None:
    retrieved = "https://www.who.int/news/item/retrieved"
    model_emitted = "https://www.who.int/news/item/model-emitted"
    unrelated = "https://www.reuters.com/world/unrelated"
    payload = {
        "output": [
            {
                "type": "web_search_call",
                "action": {
                    "type": "search",
                    "sources": [{"type": "url", "url": retrieved, "title": "Retrieved by search"}],
                },
            },
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "annotations": [{"type": "url_citation", "url": unrelated}],
                    }
                ],
            },
        ],
        "output_parsed": {
            "criteria": [{"citation_urls": [model_emitted]}],
        },
    }

    assert _collect_sources(payload) == [
        RetrievedSource(url=retrieved, title="Retrieved by search", source_class="WHO")
    ]


def test_model_emitted_citation_is_not_retrieved_evidence() -> None:
    model_emitted = "https://www.who.int/news/item/model-emitted"
    assessment = EventAssessment(
        verdict=ModelVerdict.CANDIDATE,
        event_type=EventType.PANDEMIC,
        event_key="pathogen-x-2026",
        original_qualifying_timestamp=datetime(2026, 8, 1, tzinfo=UTC),
        summary="Candidate event.",
        criteria=[
            criterion(name, model_emitted)
            for name in sorted(
                {
                    "WHO_PHEIC_DECLARATION",
                    "NOVEL_PATHOGEN",
                    "RESPIRATORY_PATHOGEN",
                    "WHO_HUMAN_TO_HUMAN",
                    "THREE_COUNTRIES_OUTSIDE_ORIGIN",
                }
            )
        ],
        contradictions=[],
    )

    problems = validate_assessment(assessment, _collect_sources({"output_parsed": {}}))

    assert "CITATION_NOT_RETRIEVED" in problems


def test_complete_pandemic_requires_who_for_who_claims() -> None:
    who = "https://www.who.int/news/item/qualifying-event"
    government = "https://www.whitehouse.gov/briefing-room/example"
    assessment = EventAssessment(
        verdict=ModelVerdict.CONFIRMED,
        event_type=EventType.PANDEMIC,
        event_key="pathogen-x-2026",
        original_qualifying_timestamp=datetime(2026, 8, 1, tzinfo=UTC),
        summary="Candidate event.",
        criteria=[
            criterion("WHO_PHEIC_DECLARATION", who),
            criterion("NOVEL_PATHOGEN", government),
            criterion("RESPIRATORY_PATHOGEN", government),
            criterion("WHO_HUMAN_TO_HUMAN", who),
            criterion("THREE_COUNTRIES_OUTSIDE_ORIGIN", government),
        ],
        contradictions=[],
    )
    sources = [
        RetrievedSource(url=who, title="WHO", source_class="WHO"),
        RetrievedSource(url=government, title="Government", source_class="GOVERNMENT"),
    ]
    assert validate_assessment(assessment, sources) == []


def test_non_war_country_fields_are_ignored_safely() -> None:
    who = "https://www.who.int/news/item/qualifying-event"
    assessment = EventAssessment(
        verdict=ModelVerdict.CONFIRMED,
        event_type=EventType.PANDEMIC,
        event_key="pathogen-x-2026",
        original_qualifying_timestamp=datetime(2026, 8, 1, tzinfo=UTC),
        attacker_country_code="ZZ",
        target_country_code="ZZ",
        summary="Candidate event.",
        criteria=[criterion(name, who) for name in sorted(PANDEMIC_CRITERIA)],
        contradictions=[],
    )
    sources = [RetrievedSource(url=who, title="WHO", source_class="WHO")]

    assert validate_assessment(assessment, sources) == []

    no_event = EventAssessment(
        verdict=ModelVerdict.NO_CANDIDATE,
        event_type=EventType.NONE,
        event_key="none",
        original_qualifying_timestamp=None,
        attacker_country_code="ZZ",
        target_country_code="ZZ",
        summary="No qualifying event.",
        criteria=[],
        contradictions=[],
    )
    assert validate_assessment(no_event, []) == []


def test_war_requires_un_and_two_independent_corroborators() -> None:
    un = "https://news.un.org/en/story/2026/08/example"
    reuters = "https://www.reuters.com/world/example"
    assessment = EventAssessment(
        verdict=ModelVerdict.CONFIRMED,
        event_type=EventType.MAJOR_INTERSTATE_WAR,
        event_key="state-a-state-b-2026",
        original_qualifying_timestamp=datetime(2026, 8, 1, tzinfo=UTC),
        attacker_country_code="RU",
        target_country_code="UA",
        summary="Candidate event.",
        criteria=[
            criterion("UN_CROSS_BORDER_OPERATION", un),
            criterion("ATTACKER_SOVEREIGN_STATE", un),
            criterion("ATTACKER_G20_OR_NUCLEAR", reuters),
            criterion("TARGET_UN_MEMBER_STATE", un),
            criterion("TWO_INDEPENDENT_CORROBORATORS", reuters),
        ],
        contradictions=[],
    )
    sources = [
        RetrievedSource(url=un, title="UN", source_class="UN"),
        RetrievedSource(url=reuters, title="Reuters", source_class="REUTERS"),
    ]
    assert "TWO_INDEPENDENT_CORROBORATORS_MISSING" in validate_assessment(assessment, sources)


def test_reference_tables_are_pinned_and_complete() -> None:
    assert UN_MEMBER_STATES.version == "UN_MEMBERS_193_2026-03-29"
    assert len(UN_MEMBER_STATES.alpha2_codes) == 193
    assert G20_SOVEREIGN_MEMBERS.alpha2_codes == frozenset(
        {
            "AR",
            "AU",
            "BR",
            "CA",
            "CN",
            "DE",
            "FR",
            "GB",
            "ID",
            "IN",
            "IT",
            "JP",
            "KR",
            "MX",
            "RU",
            "SA",
            "TR",
            "US",
            "ZA",
        }
    )
    assert NUCLEAR_ARMED_STATES.alpha2_codes == frozenset(
        {"CN", "FR", "GB", "IL", "IN", "KP", "PK", "RU", "US"}
    )


def test_war_accepts_alpha3_codes_and_nuclear_only_attacker() -> None:
    assessment, sources = complete_war_assessment(
        attacker_country_code="PAK", target_country_code="UKR"
    )

    assert normalize_iso_country_code("PAK") == "PK"
    assert normalize_iso_country_code("UKR") == "UA"
    assert validate_assessment(assessment, sources) == []


def test_war_rejects_attacker_outside_g20_and_nuclear_tables() -> None:
    assessment, sources = complete_war_assessment(attacker_country_code="NZ")

    assert "ATTACKER_NOT_G20_OR_NUCLEAR_ARMED" in validate_assessment(assessment, sources)


def test_war_rejects_non_un_target() -> None:
    assessment, sources = complete_war_assessment(target_country_code="VA")

    assert "TARGET_NOT_UN_MEMBER_STATE" in validate_assessment(assessment, sources)


def test_war_rejects_missing_or_invalid_country_codes() -> None:
    missing, sources = complete_war_assessment(attacker_country_code=None, target_country_code=None)
    invalid, _ = complete_war_assessment(
        attacker_country_code="Russia", target_country_code="UKRAINE"
    )

    assert {
        "ATTACKER_COUNTRY_CODE_MISSING",
        "TARGET_COUNTRY_CODE_MISSING",
    }.issubset(validate_assessment(missing, sources))
    assert {
        "ATTACKER_COUNTRY_CODE_INVALID",
        "TARGET_COUNTRY_CODE_INVALID",
    }.issubset(validate_assessment(invalid, sources))


def test_contradiction_never_validates() -> None:
    assessment = EventAssessment(
        verdict=ModelVerdict.CONTRADICTED,
        event_type=EventType.PANDEMIC,
        event_key="contradicted",
        original_qualifying_timestamp=datetime(2026, 8, 1, tzinfo=UTC),
        summary="Contradicted.",
        criteria=[],
        contradictions=["WHO document says no PHEIC was declared."],
    )
    assert "CONTRADICTION_REMAINS" in validate_assessment(assessment, [])
