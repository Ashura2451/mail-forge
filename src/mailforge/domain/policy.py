"""Recipient policy vocabulary shared by preparation, dispatch, and review."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class CommunicationPurpose(enum.StrEnum):
    MARKETING = "marketing"
    NEWSLETTER = "newsletter"
    PRODUCT_ANNOUNCEMENT = "product_announcement"
    SERVICE_NOTIFICATION = "service_notification"
    TRANSACTIONAL = "transactional"


class EvidenceStatus(enum.StrEnum):
    UNKNOWN = "unknown"
    ESTABLISHED = "established"
    WITHDRAWN = "withdrawn"
    CONFLICTED = "conflicted"


class PreferenceState(enum.StrEnum):
    UNKNOWN = "unknown"
    ENABLED = "enabled"
    DISABLED = "disabled"


class EligibilityStatus(enum.StrEnum):
    ELIGIBLE = "eligible"
    EXCLUDED = "excluded"
    AUTHORIZATION_UNKNOWN = "authorization_unknown"
    REQUIRES_REVIEW = "requires_review"
    INVALID = "invalid"


class OperatorAction(enum.StrEnum):
    IMPORT_CONTACTS = "import_contacts"
    PREPARE_CAMPAIGN = "prepare_campaign"
    AUTHORIZE_EXECUTION = "authorize_execution"
    RECORD_DECLARATION = "record_declaration"
    CHANGE_PREFERENCE = "change_preference"
    RESOLVE_EVIDENCE = "resolve_evidence"
    CHANGE_SUPPRESSION = "change_suppression"
    EXPORT_CONTACTS = "export_contacts"
    DEACTIVATE_CONTACT = "deactivate_contact"
    PLAN_DELETION = "plan_deletion"


@dataclass(frozen=True)
class EligibilityDecision:
    """A policy result; preparation permission is separate from send permission."""

    status: EligibilityStatus
    reasons: tuple[str, ...] = field(default_factory=tuple)
    evidence_status: EvidenceStatus = EvidenceStatus.UNKNOWN
    preference_state: PreferenceState = PreferenceState.UNKNOWN
    can_prepare: bool = True
    can_execute: bool = False

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons) if self.reasons else self.status.value

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "reasons": list(self.reasons),
            "evidence_status": self.evidence_status.value,
            "preference_state": self.preference_state.value,
            "can_prepare": self.can_prepare,
            "can_execute": self.can_execute,
        }


@dataclass(frozen=True)
class AuthorizationEvidence:
    contact_id: str
    purpose: str
    status: EvidenceStatus
    source: str
    recorded_at: str
    recording_actor: str
    scope_type: str = "global"
    scope_id: str | None = None
    evidence_ref: str | None = None
    policy_version: str = "1"
    details: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class RecipientPreference:
    contact_id: str
    purpose: str
    state: PreferenceState
    source: str
    recorded_at: str
    recording_actor: str
    scope_type: str = "global"
    scope_id: str | None = None
    evidence_ref: str | None = None
    policy_version: str = "1"
