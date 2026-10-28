"""Durable vocabulary for local-first inbound email operations."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class InboundSourceType(enum.StrEnum):
    EML = "eml"
    MBOX = "mbox"
    SYNTHETIC = "synthetic"
    IMAP = "imap"


class ParseStatus(enum.StrEnum):
    PARSED = "parsed"
    PARTIALLY_PARSED = "partially_parsed"
    UNSUPPORTED = "unsupported"
    MALFORMED = "malformed"
    QUARANTINED = "quarantined"


class InboundKind(enum.StrEnum):
    HUMAN_REPLY = "human_reply"
    AUTOMATIC_REPLY = "automatic_reply"
    DELIVERY_STATUS = "delivery_status"
    COMPLAINT = "complaint"
    UNSUBSCRIBE = "unsubscribe"
    UNKNOWN = "unknown"
    MALFORMED = "malformed"


class DsnAction(enum.StrEnum):
    FAILED = "failed"
    DELAYED = "delayed"
    DELIVERED = "delivered"
    RELAYED = "relayed"
    EXPANDED = "expanded"
    UNKNOWN = "unknown"


class CorrelationKind(enum.StrEnum):
    EXACT = "exact"
    PROBABLE = "probable"
    AMBIGUOUS = "ambiguous"
    UNMATCHED = "unmatched"


class TrustStatus(enum.StrEnum):
    TRUSTED_SYNTHETIC = "trusted_synthetic"
    UNTRUSTED = "untrusted"
    UNKNOWN = "unknown"


class ActionType(enum.StrEnum):
    NO_ACTION = "no_action"
    RECORD_EVENT = "record_event"
    CREATE_UNSUBSCRIBE = "create_unsubscribe"
    CREATE_HARD_BOUNCE = "create_hard_bounce"
    CREATE_COMPLAINT = "create_complaint"
    RECONCILE_DELIVERED = "reconcile_delivered"
    REVIEW_REQUIRED = "review_required"


class ActionStatus(enum.StrEnum):
    PROPOSED = "proposed"
    APPLIED = "applied"
    REJECTED = "rejected"
    REVIEW_REQUIRED = "review_required"


@dataclass(frozen=True)
class InboundLimits:
    max_message_bytes: int = 10 * 1024 * 1024
    max_header_bytes: int = 256 * 1024
    max_header_count: int = 200
    max_mime_depth: int = 20
    max_part_count: int = 500
    max_decoded_payload_bytes: int = 20 * 1024 * 1024
    max_preview_chars: int = 4_000
    max_processing_seconds: float = 5.0


@dataclass(frozen=True)
class SourceMessage:
    source_ref: str
    source_identity: str
    raw_bytes: bytes
    mailbox: str | None = None
    uidvalidity: str | None = None
    uid: str | None = None
    received_at: str | None = None


@dataclass(frozen=True)
class ParseIssue:
    code: str
    message: str
    path: str | None = None


@dataclass(frozen=True)
class ParsedInbound:
    status: ParseStatus
    content_hash: str
    size_bytes: int
    headers: dict[str, tuple[str, ...]]
    message_id: str | None
    in_reply_to: str | None
    references: tuple[str, ...]
    sender: str | None
    recipients: tuple[str, ...]
    subject: str | None
    text_preview: str
    html_preview: str
    issues: tuple[ParseIssue, ...] = field(default_factory=tuple)
    dsn: DsnReport | None = None
    complaint: ComplaintReport | None = None


@dataclass(frozen=True)
class DsnRecipient:
    original_recipient: str | None
    final_recipient: str | None
    action: DsnAction
    status: str | None
    diagnostic_code: str | None
    last_attempt_date: str | None
    retry_until: str | None
    fields: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DsnReport:
    reporting_mta: str | None
    original_envelope_id: str | None
    arrival_date: str | None
    recipients: tuple[DsnRecipient, ...]
    fields: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ComplaintReport:
    feedback_type: str | None
    original_message_id: str | None
    original_recipient: str | None
    source: str | None
    abuse_indicator: bool
    fields: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Classification:
    kind: InboundKind
    reasons: tuple[str, ...]
    signals: dict[str, str]


@dataclass(frozen=True)
class Correlation:
    kind: CorrelationKind
    entity_type: str | None
    entity_id: str | None
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class PolicyProposal:
    action_type: ActionType
    status: ActionStatus
    reason: str
    target: str | None = None
    requires_review: bool = False


@dataclass(frozen=True)
class InboundProcessResult:
    message_id: str
    duplicate: bool
    parse_status: ParseStatus
    classification: Classification
    correlations: tuple[Correlation, ...]
    proposals: tuple[PolicyProposal, ...]
    applied_actions: tuple[str, ...] = field(default_factory=tuple)
    review_ids: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class InboundBatchReport:
    run_id: str
    source_ref: str
    acquired: int
    duplicates: int
    processed: int
    failures: int
    reviews: int
    applied_actions: int
    cursor: str | None = None
