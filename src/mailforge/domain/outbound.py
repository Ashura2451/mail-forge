"""Domain vocabulary for synthetic outbound preparation and policy."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class ScheduleMode(enum.StrEnum):
    IMMEDIATE = "immediate"
    AT = "at"
    WINDOW = "window"
    RECURRING = "recurring"


class CatchUpPolicy(enum.StrEnum):
    SKIP = "skip"
    NEXT_WINDOW = "next_window"


class TransportPhase(enum.StrEnum):
    NOT_STARTED = "not_started"
    CONNECTING = "connecting"
    BEFORE_DATA = "before_data"
    AFTER_DATA = "after_data"
    ACCEPTED = "accepted"
    UNKNOWN = "unknown"


class ReconciliationDecision(enum.StrEnum):
    ACCEPTED = "accepted"
    PERMANENT_FAILURE = "permanent_failure"
    TRANSIENT_FAILURE = "transient_failure"
    KEEP_AMBIGUOUS = "keep_ambiguous"


@dataclass(frozen=True)
class AttachmentSpec:
    path: str
    filename: str | None = None
    content_type: str | None = None
    content_id: str | None = None


@dataclass(frozen=True)
class TemplateDefinition:
    name: str
    version: str
    subject: str
    text: str
    html: str | None = None
    reply_to: str | None = None
    max_rendered_bytes: int = 256 * 1024
    max_expansion_depth: int = 8


@dataclass(frozen=True)
class SenderPolicy:
    ref: str
    provider: str
    email: str
    display_name: str = ""
    reply_to: str | None = None
    max_per_minute: int = 60
    max_per_hour: int = 3_600
    max_per_day: int = 9_950
    per_domain_per_minute: int = 10
    per_domain_per_hour: int = 0
    max_concurrency: int = 1
    cooldown_seconds: int = 0
    enabled: bool = True
    priority: int = 0


@dataclass(frozen=True)
class ScheduleSpec:
    mode: ScheduleMode = ScheduleMode.IMMEDIATE
    timezone: str = "UTC"
    scheduled_at: str | None = None
    window_start: str | None = None
    window_end: str | None = None
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)
    pacing_seconds: float = 0.0
    cooldown_seconds: float = 0.0
    recurrence: str | None = None
    catch_up_policy: CatchUpPolicy = CatchUpPolicy.SKIP
    fold: int | None = None


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    sender_ref: str | None = None
    reason: str = ""
    retry_at: str | None = None


@dataclass(frozen=True)
class PreparationIssue:
    code: str
    message: str
    email: str | None = None


@dataclass(frozen=True)
class OutboundPreparationReport:
    campaign_id: str
    prepared: int
    excluded: int
    issues: tuple[PreparationIssue, ...] = field(default_factory=tuple)
    estimated_seconds: float = 0.0
    schedule: ScheduleSpec | None = None

    @property
    def ready(self) -> bool:
        return not self.issues
