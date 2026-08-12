"""Fail-closed systemic-event qualification and price-overlay state machine."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from urllib.parse import urlparse

from .calculation import MAX_TARGET, ZERO
from .errors import DomainError, EventStateInconsistent, ScheduleUnavailable
from .schedule import MarketCalendar, next_valid_execution
from .types import DailyBar, DecimalLike, decimal_value


class EventState(StrEnum):
    NO_EVENT = "NO_EVENT"
    NEEDS_HUMAN_REVIEW = "NEEDS_HUMAN_REVIEW"
    WINDOW_ACTIVE = "WINDOW_ACTIVE"
    EVENT_LOCKED = "EVENT_LOCKED"
    RECOVERY_TRACKING = "RECOVERY_TRACKING"
    RECOVERY_CONFIRMED = "RECOVERY_CONFIRMED"


class EventType(StrEnum):
    PANDEMIC = "PANDEMIC"
    MAJOR_INTERSTATE_WAR = "MAJOR_INTERSTATE_WAR"


class DetectorVerdict(StrEnum):
    CANDIDATE = "CANDIDATE"
    NO_CANDIDATE = "NO_CANDIDATE"
    INCONCLUSIVE = "INCONCLUSIVE"


class VerifierVerdict(StrEnum):
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    INCONCLUSIVE = "INCONCLUSIVE"


class SourceClass(StrEnum):
    WHO = "WHO"
    UN = "UN"
    NATO = "NATO"
    GOVERNMENT = "GOVERNMENT"
    REUTERS = "REUTERS"
    AP = "AP"
    AFP = "AFP"
    REFERENCE_TABLE = "REFERENCE_TABLE"


PANDEMIC_CRITERIA = frozenset(
    {
        "pheic_declared",
        "novel_pathogen",
        "respiratory_pathogen",
        "human_to_human_transmission",
        "transmission_in_three_countries_outside_origin",
    }
)
WAR_CRITERIA = frozenset(
    {
        "cross_border_attack",
        "attacker_sovereign_state",
        "attacker_g20_or_nuclear_armed",
        "target_un_member_state",
        "two_independent_corroborators",
    }
)
WAR_CORROBORATOR_CLASSES = frozenset(
    {
        SourceClass.REUTERS,
        SourceClass.AP,
        SourceClass.AFP,
        SourceClass.NATO,
        SourceClass.GOVERNMENT,
    }
)


def _aware(value: datetime, *, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise EventStateInconsistent(f"{field} must include a timezone")


@dataclass(frozen=True, slots=True)
class SourceDocument:
    document_id: str
    url: str
    publisher: str
    source_class: SourceClass
    published_at: datetime
    retrieved_at: datetime
    title: str = ""
    evidence_excerpt: str = ""

    def __post_init__(self) -> None:
        if not self.document_id.strip():
            raise EventStateInconsistent("source document id is required")
        parsed = urlparse(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise EventStateInconsistent("source document URL is invalid")
        if not self.publisher.strip():
            raise EventStateInconsistent("source publisher is required")
        _aware(self.published_at, field="published_at")
        _aware(self.retrieved_at, field="retrieved_at")
        if self.published_at > self.retrieved_at:
            raise EventStateInconsistent("source publication time follows retrieval")


@dataclass(frozen=True, slots=True)
class CriterionEvidence:
    criterion: str
    supported: bool
    source_document_ids: tuple[str, ...]
    explanation: str = ""
    contradiction: bool = False


@dataclass(frozen=True, slots=True)
class EventAssessment:
    event_id: str
    event_type: EventType | None
    original_qualifying_at: datetime | None
    detector: DetectorVerdict
    verifier: VerifierVerdict
    criteria: tuple[CriterionEvidence, ...] = ()
    source_documents: tuple[SourceDocument, ...] = ()
    duplicate_of: str | None = None


@dataclass(frozen=True, slots=True)
class EventQualification:
    state: EventState
    eligible: bool
    reasons: tuple[str, ...]
    assessment: EventAssessment


def qualify_event(assessment: EventAssessment, *, evaluated_at: datetime) -> EventQualification:
    """Deterministically validate LLM output and its cited evidence.

    The language models can propose facts, but this gate is the only component
    that can activate a monitoring window.
    """

    _aware(evaluated_at, field="evaluated_at")
    if assessment.duplicate_of:
        return EventQualification(
            state=EventState.NO_EVENT,
            eligible=False,
            reasons=("event duplicates an existing record",),
            assessment=assessment,
        )
    if (
        assessment.detector == DetectorVerdict.NO_CANDIDATE
        and assessment.verifier == VerifierVerdict.REJECTED
    ):
        return EventQualification(
            state=EventState.NO_EVENT,
            eligible=False,
            reasons=("detector and verifier both rejected the event",),
            assessment=assessment,
        )

    reasons: list[str] = []
    if assessment.detector != DetectorVerdict.CANDIDATE:
        reasons.append("detector did not return CANDIDATE")
    if assessment.verifier != VerifierVerdict.CONFIRMED:
        reasons.append("verifier did not return CONFIRMED")
    if assessment.event_type is None:
        reasons.append("event type is missing")
    if assessment.original_qualifying_at is None:
        reasons.append("original qualifying timestamp is missing")
    else:
        try:
            _aware(assessment.original_qualifying_at, field="original_qualifying_at")
            if assessment.original_qualifying_at > evaluated_at:
                reasons.append("original qualifying timestamp is in the future")
        except EventStateInconsistent as exc:
            reasons.append(str(exc))

    documents: dict[str, SourceDocument] = {}
    for document in assessment.source_documents:
        if document.document_id in documents:
            reasons.append(f"duplicate source document id: {document.document_id}")
        documents[document.document_id] = document
        if document.retrieved_at > evaluated_at:
            reasons.append(f"source {document.document_id} has a future retrieval time")

    required = (
        PANDEMIC_CRITERIA
        if assessment.event_type == EventType.PANDEMIC
        else WAR_CRITERIA
        if assessment.event_type == EventType.MAJOR_INTERSTATE_WAR
        else frozenset()
    )
    criteria: dict[str, CriterionEvidence] = {}
    for item in assessment.criteria:
        if item.criterion in criteria:
            reasons.append(f"duplicate criterion: {item.criterion}")
        criteria[item.criterion] = item
        if item.contradiction:
            reasons.append(f"criterion has a contradiction: {item.criterion}")
        if not item.supported:
            reasons.append(f"criterion is not explicitly supported: {item.criterion}")
        if not item.source_document_ids:
            reasons.append(f"criterion has no source: {item.criterion}")
        for source_id in item.source_document_ids:
            if source_id not in documents:
                reasons.append(f"criterion {item.criterion} references missing source {source_id}")

    for missing in sorted(required - criteria.keys()):
        reasons.append(f"mandatory criterion is missing: {missing}")

    if assessment.event_type == EventType.PANDEMIC:
        for criterion in ("pheic_declared", "human_to_human_transmission"):
            evidence = criteria.get(criterion)
            cited = _cited_documents(evidence, documents)
            if not any(doc.source_class == SourceClass.WHO for doc in cited):
                reasons.append(f"{criterion} lacks a WHO source")

    if assessment.event_type == EventType.MAJOR_INTERSTATE_WAR:
        attack_evidence = criteria.get("cross_border_attack")
        if not any(
            doc.source_class == SourceClass.UN
            for doc in _cited_documents(attack_evidence, documents)
        ):
            reasons.append("cross_border_attack lacks an official UN source")

        corroborator_evidence = criteria.get("two_independent_corroborators")
        corroborators = {
            document.publisher.casefold()
            for document in _cited_documents(corroborator_evidence, documents)
            if document.source_class in WAR_CORROBORATOR_CLASSES
        }
        if len(corroborators) < 2:
            reasons.append("fewer than two independent permitted corroborators")

    if reasons:
        return EventQualification(
            state=EventState.NEEDS_HUMAN_REVIEW,
            eligible=False,
            reasons=tuple(dict.fromkeys(reasons)),
            assessment=assessment,
        )
    return EventQualification(
        state=EventState.WINDOW_ACTIVE,
        eligible=True,
        reasons=(),
        assessment=assessment,
    )


def _cited_documents(
    evidence: CriterionEvidence | None,
    documents: Mapping[str, SourceDocument],
) -> tuple[SourceDocument, ...]:
    if evidence is None:
        return ()
    return tuple(
        documents[source_id] for source_id in evidence.source_document_ids if source_id in documents
    )


@dataclass(frozen=True, slots=True)
class EventOverlay:
    event_id: str
    event_type: EventType
    state: EventState
    window_start: datetime
    window_end: datetime
    current_event_drawdown: Decimal | None = None
    lock_session: date | None = None
    post_exit_low: Decimal | None = None
    recovery_level: Decimal | None = None
    consecutive_recovery_closes: int = 0
    recovery_confirmed_at: datetime | None = None
    release_at: datetime | None = None
    last_evaluated_session: date | None = None

    def __post_init__(self) -> None:
        _aware(self.window_start, field="window_start")
        _aware(self.window_end, field="window_end")
        if self.window_end != self.window_start + timedelta(days=60):
            raise EventStateInconsistent("event window must be exactly 60 calendar days")
        if self.consecutive_recovery_closes not in range(4):
            raise EventStateInconsistent("recovery count must be between zero and three")
        locked_states = {
            EventState.EVENT_LOCKED,
            EventState.RECOVERY_TRACKING,
            EventState.RECOVERY_CONFIRMED,
        }
        if self.state in locked_states and (
            self.lock_session is None or self.post_exit_low is None or self.recovery_level is None
        ):
            raise EventStateInconsistent("locked event state lacks recovery data")
        if self.state == EventState.RECOVERY_CONFIRMED and (
            self.consecutive_recovery_closes != 3
            or self.recovery_confirmed_at is None
            or self.release_at is None
        ):
            raise EventStateInconsistent("confirmed recovery lacks its release window")


def activate_event(qualification: EventQualification) -> EventOverlay:
    if not qualification.eligible or qualification.state != EventState.WINDOW_ACTIVE:
        raise EventStateInconsistent("only a qualified event can activate a window")
    assessment = qualification.assessment
    if assessment.event_type is None or assessment.original_qualifying_at is None:
        raise EventStateInconsistent("qualified event lacks required identity")
    return EventOverlay(
        event_id=assessment.event_id,
        event_type=assessment.event_type,
        state=EventState.WINDOW_ACTIVE,
        window_start=assessment.original_qualifying_at,
        window_end=assessment.original_qualifying_at + timedelta(days=60),
    )


def update_event_after_close(
    overlay: EventOverlay,
    *,
    bar: DailyBar,
    prior_20_session_high: DecimalLike,
    completed_at: datetime,
    calendar: MarketCalendar,
) -> EventOverlay:
    """Apply the -4% lock and the three-completed-session recovery rule."""

    _aware(completed_at, field="completed_at")
    if not bar.completed:
        raise EventStateInconsistent("event state requires a completed SPY bar")
    if overlay.last_evaluated_session is not None:
        if bar.session_date < overlay.last_evaluated_session:
            raise EventStateInconsistent("event closes must be processed in order")
        if bar.session_date == overlay.last_evaluated_session:
            return overlay
    if completed_at < overlay.window_start:
        raise EventStateInconsistent("cannot evaluate a close before the event")

    try:
        prior_high = decimal_value(prior_20_session_high, field="prior_20_session_high")
    except DomainError as exc:
        raise EventStateInconsistent(str(exc)) from exc
    if prior_high <= ZERO:
        raise EventStateInconsistent("prior 20-session high must be positive")

    drawdown = bar.close / prior_high - Decimal("1")
    if overlay.state == EventState.WINDOW_ACTIVE:
        if completed_at > overlay.window_end:
            return replace(
                overlay,
                state=EventState.NO_EVENT,
                current_event_drawdown=drawdown,
                last_evaluated_session=bar.session_date,
            )
        if drawdown <= Decimal("-0.04"):
            return replace(
                overlay,
                state=EventState.EVENT_LOCKED,
                current_event_drawdown=drawdown,
                lock_session=bar.session_date,
                post_exit_low=bar.close,
                recovery_level=Decimal("1.07") * bar.close,
                consecutive_recovery_closes=0,
                last_evaluated_session=bar.session_date,
            )
        return replace(
            overlay,
            current_event_drawdown=drawdown,
            last_evaluated_session=bar.session_date,
        )

    if overlay.state in {EventState.EVENT_LOCKED, EventState.RECOVERY_TRACKING}:
        if overlay.post_exit_low is None:
            raise EventStateInconsistent("locked event lacks a post-exit low")
        post_exit_low = min(overlay.post_exit_low, bar.close)
        recovery_level = Decimal("1.07") * post_exit_low
        if post_exit_low < overlay.post_exit_low:
            consecutive = 0
        elif bar.close >= recovery_level:
            consecutive = overlay.consecutive_recovery_closes + 1
        else:
            consecutive = 0

        if consecutive >= 3:
            try:
                release_at = next_valid_execution(completed_at, calendar)
            except ScheduleUnavailable as exc:
                raise EventStateInconsistent(str(exc)) from exc
            return replace(
                overlay,
                state=EventState.RECOVERY_CONFIRMED,
                current_event_drawdown=drawdown,
                post_exit_low=post_exit_low,
                recovery_level=recovery_level,
                consecutive_recovery_closes=3,
                recovery_confirmed_at=completed_at,
                release_at=release_at,
                last_evaluated_session=bar.session_date,
            )
        return replace(
            overlay,
            state=EventState.RECOVERY_TRACKING,
            current_event_drawdown=drawdown,
            post_exit_low=post_exit_low,
            recovery_level=recovery_level,
            consecutive_recovery_closes=consecutive,
            last_evaluated_session=bar.session_date,
        )

    if overlay.state == EventState.RECOVERY_CONFIRMED:
        return replace(
            overlay,
            current_event_drawdown=drawdown,
            last_evaluated_session=bar.session_date,
        )
    if overlay.state in {EventState.NO_EVENT, EventState.NEEDS_HUMAN_REVIEW}:
        return replace(
            overlay,
            current_event_drawdown=drawdown,
            last_evaluated_session=bar.session_date,
        )
    raise EventStateInconsistent(f"unsupported event state: {overlay.state}")


def release_confirmed_recovery(overlay: EventOverlay, *, at: datetime) -> EventOverlay:
    _aware(at, field="at")
    if overlay.state != EventState.RECOVERY_CONFIRMED:
        return overlay
    if overlay.release_at is None:
        raise EventStateInconsistent("confirmed recovery lacks a release time")
    if at < overlay.release_at:
        return overlay
    return replace(overlay, state=EventState.NO_EVENT)


def event_cap(state: EventState) -> Decimal:
    if state in {
        EventState.NO_EVENT,
        EventState.NEEDS_HUMAN_REVIEW,
        EventState.WINDOW_ACTIVE,
    }:
        return MAX_TARGET
    if state in {
        EventState.EVENT_LOCKED,
        EventState.RECOVERY_TRACKING,
        EventState.RECOVERY_CONFIRMED,
    }:
        return ZERO
    raise EventStateInconsistent(f"unsupported event state: {state}")


def apply_event_cap(current_scheduled_target: DecimalLike, state: EventState) -> Decimal:
    try:
        scheduled = decimal_value(current_scheduled_target, field="current_scheduled_target")
    except DomainError as exc:
        raise EventStateInconsistent(str(exc)) from exc
    if scheduled < ZERO or scheduled > MAX_TARGET:
        raise EventStateInconsistent("current scheduled target must be between 0 and 4.85")
    return min(scheduled, event_cap(state), MAX_TARGET)
