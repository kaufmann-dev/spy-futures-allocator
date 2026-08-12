from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.domain.events import (
    PANDEMIC_CRITERIA,
    CriterionEvidence,
    DetectorVerdict,
    EventAssessment,
    EventOverlay,
    EventState,
    EventType,
    SourceClass,
    SourceDocument,
    VerifierVerdict,
    apply_event_cap,
    event_cap,
    qualify_event,
    release_confirmed_recovery,
    update_event_after_close,
)
from app.domain.types import DailyBar

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


class FakeNyseCalendar:
    def __init__(self, holidays: set[date] | None = None) -> None:
        self.holidays = holidays or set()

    def is_session(self, session_date: date) -> bool:
        return session_date.weekday() < 5 and session_date not in self.holidays

    def session_open(self, session_date: date) -> datetime:
        return datetime.combine(session_date, time(9, 30), NY)

    def session_close(self, session_date: date) -> datetime:
        return datetime.combine(session_date, time(16), NY)


def active_overlay(start: datetime | None = None) -> EventOverlay:
    window_start = start or datetime(2024, 4, 1, 12, tzinfo=UTC)
    return EventOverlay(
        event_id="event-1",
        event_type=EventType.PANDEMIC,
        state=EventState.WINDOW_ACTIVE,
        window_start=window_start,
        window_end=window_start + timedelta(days=60),
    )


def close(day: date, value: str) -> DailyBar:
    return DailyBar(day, Decimal(value), completed=True)


def completed_at(day: date) -> datetime:
    return datetime.combine(day, time(16, 1), NY)


def test_event_window_without_four_percent_gate_leaves_target_unchanged() -> None:
    overlay = update_event_after_close(
        active_overlay(),
        bar=close(date(2024, 4, 1), "96.01"),
        prior_20_session_high="100",
        completed_at=completed_at(date(2024, 4, 1)),
        calendar=FakeNyseCalendar(),
    )
    assert overlay.state == EventState.WINDOW_ACTIVE
    assert overlay.current_event_drawdown == Decimal("-0.0399")
    assert event_cap(overlay.state) == Decimal("4.85")
    assert apply_event_cap("3.25", overlay.state) == Decimal("3.25")


def test_exact_four_percent_gate_locks_and_forces_zero_target() -> None:
    overlay = update_event_after_close(
        active_overlay(),
        bar=close(date(2024, 4, 1), "96"),
        prior_20_session_high="100",
        completed_at=completed_at(date(2024, 4, 1)),
        calendar=FakeNyseCalendar(),
    )
    assert overlay.state == EventState.EVENT_LOCKED
    assert overlay.post_exit_low == Decimal("96")
    assert overlay.recovery_level == Decimal("102.72")
    assert event_cap(overlay.state) == 0
    assert apply_event_cap("4.85", overlay.state) == 0


def test_three_consecutive_recovery_closes_confirm_then_release_friday() -> None:
    calendar = FakeNyseCalendar()
    overlay = update_event_after_close(
        active_overlay(),
        bar=close(date(2024, 4, 1), "96"),
        prior_20_session_high="100",
        completed_at=completed_at(date(2024, 4, 1)),
        calendar=calendar,
    )
    for day in (date(2024, 4, 2), date(2024, 4, 3), date(2024, 4, 4)):
        overlay = update_event_after_close(
            overlay,
            bar=close(day, "103"),
            prior_20_session_high="100",
            completed_at=completed_at(day),
            calendar=calendar,
        )

    assert overlay.state == EventState.RECOVERY_CONFIRMED
    assert overlay.consecutive_recovery_closes == 3
    assert overlay.release_at == datetime(2024, 4, 5, 9, 30, tzinfo=NY)
    assert event_cap(overlay.state) == 0

    before = release_confirmed_recovery(overlay, at=datetime(2024, 4, 5, 9, 29, tzinfo=NY))
    assert before.state == EventState.RECOVERY_CONFIRMED
    released = release_confirmed_recovery(overlay, at=datetime(2024, 4, 5, 9, 30, tzinfo=NY))
    assert released.state == EventState.NO_EVENT
    assert event_cap(released.state) == Decimal("4.85")


def test_new_post_lock_low_resets_recovery_progress_and_level() -> None:
    calendar = FakeNyseCalendar()
    overlay = update_event_after_close(
        active_overlay(),
        bar=close(date(2024, 4, 1), "96"),
        prior_20_session_high="100",
        completed_at=completed_at(date(2024, 4, 1)),
        calendar=calendar,
    )
    overlay = update_event_after_close(
        overlay,
        bar=close(date(2024, 4, 2), "103"),
        prior_20_session_high="100",
        completed_at=completed_at(date(2024, 4, 2)),
        calendar=calendar,
    )
    assert overlay.consecutive_recovery_closes == 1
    overlay = update_event_after_close(
        overlay,
        bar=close(date(2024, 4, 3), "95"),
        prior_20_session_high="100",
        completed_at=completed_at(date(2024, 4, 3)),
        calendar=calendar,
    )
    assert overlay.state == EventState.RECOVERY_TRACKING
    assert overlay.post_exit_low == Decimal("95")
    assert overlay.recovery_level == Decimal("101.65")
    assert overlay.consecutive_recovery_closes == 0


def test_unlocked_window_expires_without_changing_exposure() -> None:
    start = datetime(2024, 1, 1, 12, tzinfo=UTC)
    overlay = update_event_after_close(
        active_overlay(start),
        bar=close(date(2024, 3, 4), "99"),
        prior_20_session_high="100",
        completed_at=completed_at(date(2024, 3, 4)),
        calendar=FakeNyseCalendar(),
    )
    assert overlay.state == EventState.NO_EVENT
    assert event_cap(overlay.state) == Decimal("4.85")


def _who_document(now: datetime) -> SourceDocument:
    return SourceDocument(
        document_id="who-1",
        url="https://www.who.int/example",
        publisher="World Health Organization",
        source_class=SourceClass.WHO,
        published_at=now - timedelta(hours=2),
        retrieved_at=now - timedelta(hours=1),
    )


def test_complete_pandemic_evidence_qualifies() -> None:
    now = datetime(2024, 4, 1, 18, tzinfo=UTC)
    assessment = EventAssessment(
        event_id="pandemic-1",
        event_type=EventType.PANDEMIC,
        original_qualifying_at=now - timedelta(hours=2),
        detector=DetectorVerdict.CANDIDATE,
        verifier=VerifierVerdict.CONFIRMED,
        criteria=tuple(
            CriterionEvidence(criterion, True, ("who-1",))
            for criterion in sorted(PANDEMIC_CRITERIA)
        ),
        source_documents=(_who_document(now),),
    )
    result = qualify_event(assessment, evaluated_at=now)
    assert result.eligible is True
    assert result.state == EventState.WINDOW_ACTIVE


def test_incomplete_who_evidence_requires_human_review() -> None:
    now = datetime(2024, 4, 1, 18, tzinfo=UTC)
    criteria = tuple(
        CriterionEvidence(criterion, True, ("who-1",))
        for criterion in sorted(PANDEMIC_CRITERIA - {"pheic_declared"})
    )
    assessment = EventAssessment(
        event_id="incomplete-1",
        event_type=EventType.PANDEMIC,
        original_qualifying_at=now - timedelta(hours=2),
        detector=DetectorVerdict.CANDIDATE,
        verifier=VerifierVerdict.CONFIRMED,
        criteria=criteria,
        source_documents=(_who_document(now),),
    )
    result = qualify_event(assessment, evaluated_at=now)
    assert result.eligible is False
    assert result.state == EventState.NEEDS_HUMAN_REVIEW
    assert any("pheic_declared" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    "false_positive",
    [
        "election",
        "sanctions",
        "mobilization",
        "civil war",
        "terrorism",
        "social-media rumor",
    ],
)
def test_detector_and_verifier_rejections_do_not_open_event_window(
    false_positive: str,
) -> None:
    now = datetime(2024, 4, 1, 18, tzinfo=UTC)
    assessment = EventAssessment(
        event_id=false_positive,
        event_type=None,
        original_qualifying_at=None,
        detector=DetectorVerdict.NO_CANDIDATE,
        verifier=VerifierVerdict.REJECTED,
    )
    result = qualify_event(assessment, evaluated_at=now)
    assert result.state == EventState.NO_EVENT
    assert result.eligible is False


def test_model_disagreement_requires_human_review() -> None:
    now = datetime(2024, 4, 1, 18, tzinfo=UTC)
    assessment = EventAssessment(
        event_id="disagreement",
        event_type=EventType.MAJOR_INTERSTATE_WAR,
        original_qualifying_at=now - timedelta(hours=1),
        detector=DetectorVerdict.CANDIDATE,
        verifier=VerifierVerdict.REJECTED,
    )
    result = qualify_event(assessment, evaluated_at=now)
    assert result.state == EventState.NEEDS_HUMAN_REVIEW
    assert result.eligible is False
