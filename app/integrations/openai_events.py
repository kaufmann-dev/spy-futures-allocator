from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import urlparse

from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field, field_validator

ALLOWED_DOMAINS = (
    "who.int",
    "un.org",
    "news.un.org",
    "nato.int",
    "reuters.com",
    "apnews.com",
    "afp.com",
    "whitehouse.gov",
    "state.gov",
    "defense.gov",
    "congress.gov",
    "gov.uk",
    "bundesregierung.de",
    "elysee.fr",
    "diplomatie.gouv.fr",
    "canada.ca",
    "pm.gc.ca",
    "gov.pl",
    "go.jp",
    "mofa.go.jp",
    "president.gov.ua",
    "gov.cn",
    "fmprc.gov.cn",
    "kremlin.ru",
)


class EventType(StrEnum):
    PANDEMIC = "PANDEMIC"
    MAJOR_INTERSTATE_WAR = "MAJOR_INTERSTATE_WAR"
    NONE = "NONE"


class ModelVerdict(StrEnum):
    NO_CANDIDATE = "NO_CANDIDATE"
    CANDIDATE = "CANDIDATE"
    CONFIRMED = "CONFIRMED"
    INCOMPLETE = "INCOMPLETE"
    CONTRADICTED = "CONTRADICTED"


class CriterionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    criterion: str
    supported: bool
    explanation: str = Field(max_length=1_500)
    citation_urls: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("citation_urls")
    @classmethod
    def unique_urls(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))


class EventAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: ModelVerdict
    event_type: EventType
    event_key: str = Field(max_length=200)
    original_qualifying_timestamp: datetime | None
    summary: str = Field(max_length=2_000)
    criteria: list[CriterionEvidence] = Field(default_factory=list, max_length=12)
    contradictions: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("original_qualifying_timestamp")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("event timestamp must include a timezone")
        return value


class RetrievedSource(BaseModel):
    url: str
    title: str
    source_class: str


class ModelRun(BaseModel):
    role: str
    model: str
    response_id: str
    checked_at: datetime
    assessment: EventAssessment
    sources: list[RetrievedSource]
    response_hash: str


class EventCheckResult(BaseModel):
    state: str
    reason: str
    detector: ModelRun
    verifier: ModelRun | None
    event_type: EventType
    event_key: str
    original_qualifying_timestamp: datetime | None


class ResponsesClient(Protocol):
    @property
    def responses(self) -> Any: ...


PANDEMIC_CRITERIA = {
    "WHO_PHEIC_DECLARATION",
    "NOVEL_PATHOGEN",
    "RESPIRATORY_PATHOGEN",
    "WHO_HUMAN_TO_HUMAN",
    "THREE_COUNTRIES_OUTSIDE_ORIGIN",
}

WAR_CRITERIA = {
    "UN_CROSS_BORDER_OPERATION",
    "ATTACKER_SOVEREIGN_STATE",
    "ATTACKER_G20_OR_NUCLEAR",
    "TARGET_UN_MEMBER_STATE",
    "TWO_INDEPENDENT_CORROBORATORS",
}


DETECTOR_PROMPT = """You are the conservative detector for ARL-5 Simplified.
Search only the configured domains for a NEW qualifying systemic event or materially changed
evidence. Do not use prices, social media, analyst commentary, or unlisted sources.

PANDEMIC requires all exact criterion keys:
WHO_PHEIC_DECLARATION, NOVEL_PATHOGEN, RESPIRATORY_PATHOGEN,
WHO_HUMAN_TO_HUMAN, THREE_COUNTRIES_OUTSIDE_ORIGIN.

MAJOR_INTERSTATE_WAR requires all exact criterion keys:
UN_CROSS_BORDER_OPERATION, ATTACKER_SOVEREIGN_STATE, ATTACKER_G20_OR_NUCLEAR,
TARGET_UN_MEMBER_STATE, TWO_INDEPENDENT_CORROBORATORS.

For each criterion, cite URLs that explicitly support it. A WHO source must support the WHO
criteria. A UN source must support UN_CROSS_BORDER_OPERATION. War corroboration must comprise at
least two independent organizations among Reuters, AP, AFP, NATO, attacker government, and target
government. Exclude elections, sanctions, mobilization, threats, terrorism, civil war, non-state
attacks, cyberattacks, isolated border incidents, rumors, and incomplete WHO evidence.
Use the original qualifying timestamp, not discovery time. If anything is incomplete or
contradictory, say INCOMPLETE or CONTRADICTED. Return NO_CANDIDATE/NONE when no event qualifies.
"""


VERIFIER_PROMPT = """You are an independent conservative verifier for ARL-5 Simplified.
Independently search the configured domains. Do not assume the detector is correct and do not cite
the detector. Confirm the same event only if every mandatory criterion is explicitly supported,
the original timestamp is sound, mandatory source classes are present, required corroborators are
independent, and no contradiction remains. Use CONFIRMED only for complete evidence; otherwise use
INCOMPLETE, CONTRADICTED, or NO_CANDIDATE. Use exactly the criterion keys supplied in the detector
instructions. Never use sources for market prices or sizing.
"""


def _hostname(url: str) -> str:
    return (urlparse(url).hostname or "").lower().rstrip(".")


def is_allowed_source(url: str) -> bool:
    host = _hostname(url)
    return bool(host) and any(
        host == domain or host.endswith(f".{domain}") for domain in ALLOWED_DOMAINS
    )


def source_class(url: str) -> str:
    host = _hostname(url)
    if host == "who.int" or host.endswith(".who.int"):
        return "WHO"
    if host == "un.org" or host.endswith(".un.org"):
        return "UN"
    if host == "nato.int" or host.endswith(".nato.int"):
        return "NATO"
    if host == "reuters.com" or host.endswith(".reuters.com"):
        return "REUTERS"
    if host == "apnews.com" or host.endswith(".apnews.com"):
        return "AP"
    if host == "afp.com" or host.endswith(".afp.com"):
        return "AFP"
    if is_allowed_source(url):
        return f"GOVERNMENT:{host}"
    return "UNAPPROVED"


def _collect_sources(payload: dict[str, Any]) -> list[RetrievedSource]:
    """Return only URLs reported by the web-search tool as consulted sources.

    The structured assessment also contains model-authored ``citation_urls``. Those
    are claims that must be checked against tool provenance, not provenance in
    their own right, so intentionally do not recursively scan the response.
    """

    found: dict[str, RetrievedSource] = {}
    output = payload.get("output")
    if not isinstance(output, list):
        return []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "web_search_call":
            continue
        action = item.get("action")
        if not isinstance(action, dict):
            continue
        sources = action.get("sources")
        if not isinstance(sources, list):
            continue
        for source in sources:
            if not isinstance(source, dict):
                continue
            url = source.get("url")
            if not isinstance(url, str) or not url.startswith(("https://", "http://")):
                continue
            title = source.get("title")
            found[url] = RetrievedSource(
                url=url,
                title=title if isinstance(title, str) else _hostname(url),
                source_class=source_class(url),
            )
    return list(found.values())


def _criterion_sources(assessment: EventAssessment) -> set[str]:
    return {url for item in assessment.criteria for url in item.citation_urls}


def validate_assessment(
    assessment: EventAssessment, sources: Iterable[RetrievedSource]
) -> list[str]:
    problems: list[str] = []
    source_map = {source.url: source for source in sources}
    cited = _criterion_sources(assessment)
    if assessment.event_type == EventType.NONE:
        if assessment.verdict != ModelVerdict.NO_CANDIDATE:
            problems.append("NONE_EVENT_WITH_NONEMPTY_VERDICT")
        return problems

    required = PANDEMIC_CRITERIA if assessment.event_type == EventType.PANDEMIC else WAR_CRITERIA
    evidence = {item.criterion: item for item in assessment.criteria}
    if set(evidence) != required:
        problems.append("CRITERIA_SET_MISMATCH")
    if assessment.original_qualifying_timestamp is None:
        problems.append("ORIGINAL_TIMESTAMP_MISSING")
    if (
        assessment.original_qualifying_timestamp is not None
        and assessment.original_qualifying_timestamp > datetime.now(UTC)
    ):
        problems.append("ORIGINAL_TIMESTAMP_IN_FUTURE")
    if not cited:
        problems.append("CITATIONS_MISSING")
    for url in cited:
        if not is_allowed_source(url):
            problems.append("UNAPPROVED_SOURCE")
        if url not in source_map:
            problems.append("CITATION_NOT_RETRIEVED")
    for criterion in required:
        item = evidence.get(criterion)
        if item is None or not item.supported or not item.citation_urls:
            problems.append(f"UNSUPPORTED:{criterion}")

    classes = {source_class(url) for url in cited}
    if assessment.event_type == EventType.PANDEMIC:
        who_criteria = {"WHO_PHEIC_DECLARATION", "WHO_HUMAN_TO_HUMAN"}
        for criterion in who_criteria:
            item = evidence.get(criterion)
            if item and not any(source_class(url) == "WHO" for url in item.citation_urls):
                problems.append(f"WHO_SOURCE_MISSING:{criterion}")
    else:
        un_item = evidence.get("UN_CROSS_BORDER_OPERATION")
        if un_item and not any(source_class(url) == "UN" for url in un_item.citation_urls):
            problems.append("UN_SOURCE_MISSING")
        corroborators = classes & {"REUTERS", "AP", "AFP", "NATO"}
        corroborators.update(item for item in classes if item.startswith("GOVERNMENT:"))
        if len(corroborators) < 2:
            problems.append("TWO_INDEPENDENT_CORROBORATORS_MISSING")
    if assessment.contradictions:
        problems.append("CONTRADICTION_REMAINS")
    return sorted(set(problems))


class OpenAIEventMonitor:
    def __init__(self, api_key: str, detector_model: str, verifier_model: str) -> None:
        self._client: ResponsesClient = OpenAI(api_key=api_key)
        self.detector_model = detector_model
        self.verifier_model = verifier_model

    def _run(self, *, role: str, model: str, prompt: str, context: str) -> ModelRun:
        response = self._client.responses.parse(
            model=model,
            reasoning={"effort": "low"},
            tools=[
                {
                    "type": "web_search",
                    "filters": {"allowed_domains": list(ALLOWED_DOMAINS)},
                    "search_context_size": "medium",
                }
            ],
            tool_choice="auto",
            include=["web_search_call.action.sources"],
            input=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": context},
            ],
            text_format=EventAssessment,
        )
        assessment = response.output_parsed
        if assessment is None:
            raise ValueError(f"{role} returned no structured assessment")
        dumped = response.model_dump(mode="json")
        sources = _collect_sources(dumped)
        evidence_payload = {
            "assessment": assessment.model_dump(mode="json"),
            "sources": sorted(
                (source.model_dump(mode="json") for source in sources),
                key=lambda source: source["url"],
            ),
        }
        canonical = json.dumps(evidence_payload, sort_keys=True, separators=(",", ":"))
        return ModelRun(
            role=role,
            model=model,
            response_id=response.id,
            checked_at=datetime.now(UTC),
            assessment=assessment,
            sources=sources,
            response_hash=hashlib.sha256(canonical.encode()).hexdigest(),
        )

    def detect(self, *, now: datetime | None = None) -> ModelRun:
        checked_at = now or datetime.now(UTC)
        return self._run(
            role="DETECTOR",
            model=self.detector_model,
            prompt=DETECTOR_PROMPT,
            context=(
                "Check for newly qualifying or materially changed events as of "
                f"{checked_at.isoformat()}."
            ),
        )

    def verify(self, detector: ModelRun) -> ModelRun:
        return self._run(
            role="VERIFIER",
            model=self.verifier_model,
            prompt=VERIFIER_PROMPT,
            context=(
                "Independently verify this detector claim. Do your own searches.\n"
                + detector.assessment.model_dump_json()
            ),
        )

    @staticmethod
    def classify_detector(detector: ModelRun) -> EventCheckResult | None:
        detector_problems = validate_assessment(detector.assessment, detector.sources)
        if detector.assessment.verdict != ModelVerdict.CANDIDATE:
            state = (
                "NO_EVENT"
                if detector.assessment.verdict == ModelVerdict.NO_CANDIDATE
                else "NEEDS_HUMAN_REVIEW"
            )
            return EventCheckResult(
                state=state,
                reason=",".join(detector_problems) or detector.assessment.verdict,
                detector=detector,
                verifier=None,
                event_type=detector.assessment.event_type,
                event_key=detector.assessment.event_key,
                original_qualifying_timestamp=detector.assessment.original_qualifying_timestamp,
            )
        if detector_problems:
            return EventCheckResult(
                state="NEEDS_HUMAN_REVIEW",
                reason=",".join(detector_problems),
                detector=detector,
                verifier=None,
                event_type=detector.assessment.event_type,
                event_key=detector.assessment.event_key,
                original_qualifying_timestamp=detector.assessment.original_qualifying_timestamp,
            )
        return None

    @staticmethod
    def classify_pair(detector: ModelRun, verifier: ModelRun) -> EventCheckResult:
        verifier_problems = validate_assessment(verifier.assessment, verifier.sources)
        agreement = (
            verifier.assessment.verdict == ModelVerdict.CONFIRMED
            and verifier.assessment.event_type == detector.assessment.event_type
            and verifier.assessment.event_key == detector.assessment.event_key
            and verifier.assessment.original_qualifying_timestamp
            == detector.assessment.original_qualifying_timestamp
        )
        if not agreement or verifier_problems:
            reason = verifier_problems or ["DETECTOR_VERIFIER_DISAGREEMENT"]
            return EventCheckResult(
                state="NEEDS_HUMAN_REVIEW",
                reason=",".join(reason),
                detector=detector,
                verifier=verifier,
                event_type=detector.assessment.event_type,
                event_key=detector.assessment.event_key,
                original_qualifying_timestamp=detector.assessment.original_qualifying_timestamp,
            )
        return EventCheckResult(
            state="WINDOW_ACTIVE",
            reason="DETECTOR_AND_VERIFIER_CONFIRMED",
            detector=detector,
            verifier=verifier,
            event_type=detector.assessment.event_type,
            event_key=detector.assessment.event_key,
            original_qualifying_timestamp=detector.assessment.original_qualifying_timestamp,
        )

    def check(self, *, now: datetime | None = None) -> EventCheckResult:
        checked_at = now or datetime.now(UTC)
        detector = self.detect(now=checked_at)
        initial = self.classify_detector(detector)
        if initial is not None:
            return initial
        verifier = self.verify(detector)
        return self.classify_pair(detector, verifier)
