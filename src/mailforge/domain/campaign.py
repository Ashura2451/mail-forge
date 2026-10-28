"""Durable campaign-domain vocabulary and state-transition rules."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import UTC, datetime


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class CampaignStatus(enum.StrEnum):
    DRAFT = "draft"
    VALIDATING = "validating"
    READY = "ready"
    SCHEDULED = "scheduled"
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    COMPLETING = "completing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class DeliveryState(enum.StrEnum):
    NOT_ELIGIBLE = "not_eligible"
    SUPPRESSED = "suppressed"
    ELIGIBLE_PENDING = "eligible_pending"
    PREPARED = "prepared"
    RESERVED = "reserved"
    ATTEMPT_STARTED = "attempt_started"
    ACCEPTED = "accepted"
    TRANSIENT_FAILURE = "transient_failure"
    PERMANENT_FAILURE = "permanent_failure"
    DEFERRED = "deferred"
    AMBIGUOUS = "ambiguous"
    CANCELED = "canceled"


class SuppressionCategory(enum.StrEnum):
    UNSUBSCRIBE = "unsubscribe"
    MANUAL = "manual"
    HARD_BOUNCE = "hard_bounce"
    CAMPAIGN = "campaign"
    LIST = "list"
    SENDER = "sender"
    COMPLAINT = "complaint"
    MANUAL_HOLD = "manual_hold"


class AttemptOutcome(enum.StrEnum):
    ACCEPTED = "accepted"
    TRANSIENT_FAILURE = "transient_failure"
    PERMANENT_FAILURE = "permanent_failure"
    AMBIGUOUS = "ambiguous"
    CANCELED = "canceled"
    LEGACY_RECORDED = "legacy_recorded"


class ExecutionAuthorization(enum.StrEnum):
    UNAUTHORIZED = "unauthorized"
    SIMULATION_AUTHORIZED = "simulation_authorized"


CAMPAIGN_TRANSITIONS: dict[CampaignStatus, frozenset[CampaignStatus]] = {
    CampaignStatus.DRAFT: frozenset({CampaignStatus.VALIDATING, CampaignStatus.CANCELED}),
    CampaignStatus.VALIDATING: frozenset({CampaignStatus.READY, CampaignStatus.FAILED}),
    CampaignStatus.READY: frozenset(
        {CampaignStatus.SCHEDULED, CampaignStatus.RUNNING, CampaignStatus.CANCELED}
    ),
    CampaignStatus.SCHEDULED: frozenset(
        {CampaignStatus.RUNNING, CampaignStatus.PAUSING, CampaignStatus.CANCELED}
    ),
    CampaignStatus.RUNNING: frozenset(
        {
            CampaignStatus.PAUSING,
            CampaignStatus.COMPLETING,
            CampaignStatus.FAILED,
            CampaignStatus.CANCELED,
        }
    ),
    CampaignStatus.PAUSING: frozenset({CampaignStatus.PAUSED, CampaignStatus.FAILED}),
    CampaignStatus.PAUSED: frozenset(
        {CampaignStatus.RUNNING, CampaignStatus.CANCELED, CampaignStatus.FAILED}
    ),
    CampaignStatus.COMPLETING: frozenset(
        {CampaignStatus.COMPLETED, CampaignStatus.PAUSED, CampaignStatus.FAILED}
    ),
    CampaignStatus.COMPLETED: frozenset(),
    CampaignStatus.FAILED: frozenset({CampaignStatus.READY, CampaignStatus.CANCELED}),
    CampaignStatus.CANCELED: frozenset(),
}


def validate_campaign_transition(current: CampaignStatus, target: CampaignStatus) -> None:
    if target not in CAMPAIGN_TRANSITIONS[current]:
        raise ValueError(f"invalid campaign transition: {current.value} -> {target.value}")


@dataclass(frozen=True)
class ContactCandidate:
    normalized_email: str
    original_input: str
    source_file: str
    source_record_position: int
    metadata: dict[str, object]


@dataclass(frozen=True)
class DeliveryResult:
    outcome: AttemptOutcome
    error_code: str | None = None
    error_message: str | None = None
    transport_id: str | None = None
    transport_phase: str | None = None
    response_code: int | None = None
