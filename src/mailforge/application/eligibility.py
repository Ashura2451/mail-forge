"""The single recipient eligibility authority used by campaign workflows."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from typing import Any

from mailforge.domain.email import validate_email
from mailforge.domain.policy import (
    CommunicationPurpose,
    EligibilityDecision,
    EligibilityStatus,
    EvidenceStatus,
    PreferenceState,
)


class EligibilityService:
    """Evaluate mutable recipient policy at preparation and dispatch boundaries."""

    def __init__(self, store):
        self.store = store

    @staticmethod
    def purpose(campaign: Any) -> str:
        configuration = json.loads(campaign["configuration_json"] or "{}")
        return str(configuration.get("communication_purpose", CommunicationPurpose.MARKETING.value))

    @staticmethod
    def requires_operator_authorization(campaign: Any) -> bool:
        configuration = json.loads(campaign["configuration_json"] or "{}")
        return bool(configuration.get("operator_authorization_required", True))

    def evaluate_contact(
        self,
        contact: Any,
        *,
        campaign: Any,
        operator_authorized: bool | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> EligibilityDecision:
        email = contact["normalized_email"]
        if not validate_email(email).valid:
            return EligibilityDecision(
                EligibilityStatus.INVALID,
                ("invalid_syntax",),
                can_prepare=False,
            )
        if contact["lifecycle_status"] != "active":
            return EligibilityDecision(
                EligibilityStatus.EXCLUDED,
                (f"contact_{contact['lifecycle_status']}",),
                can_prepare=True,
            )

        purpose = self.purpose(campaign)
        matches = self.store.matching_suppressions(
            normalized_email=email,
            contact_id=contact["id"],
            list_id=campaign["audience_list_id"],
            campaign_id=campaign["id"],
            sender_config_ref=campaign["sender_config_ref"],
            communication_purpose=purpose,
            connection=connection,
        )
        if matches:
            return EligibilityDecision(
                EligibilityStatus.EXCLUDED,
                tuple(f"{row['category']}: {row['reason']}" for row in matches),
                can_prepare=True,
            )

        scopes = (
            ("purpose", purpose),
            ("list", campaign["audience_list_id"]),
            ("campaign", campaign["id"]),
            ("sender", campaign["sender_config_ref"]),
        )
        preference = self.store.latest_preference(
            contact["id"], purpose=purpose, scope_ids=scopes, connection=connection
        )
        preference_state = (
            PreferenceState(preference["state"]) if preference else PreferenceState.UNKNOWN
        )
        if preference_state is PreferenceState.DISABLED:
            return EligibilityDecision(
                EligibilityStatus.EXCLUDED,
                (f"preference_disabled:{purpose}",),
                preference_state=preference_state,
                can_prepare=True,
            )

        evidence = self.store.latest_authorization_evidence(
            contact["id"], purpose=purpose, scope_ids=scopes, connection=connection
        )
        evidence_status = EvidenceStatus(evidence["status"]) if evidence else EvidenceStatus.UNKNOWN
        if evidence_status in {EvidenceStatus.CONFLICTED, EvidenceStatus.WITHDRAWN}:
            return EligibilityDecision(
                EligibilityStatus.REQUIRES_REVIEW,
                (f"authorization_{evidence_status.value}",),
                evidence_status=evidence_status,
                preference_state=preference_state,
                can_prepare=True,
            )

        authorized = operator_authorized
        if authorized is None:
            authorized = campaign["authorization"] == "simulation_authorized"
        can_execute = bool(authorized)
        if self.requires_operator_authorization(campaign) and not can_execute:
            status = (
                EligibilityStatus.AUTHORIZATION_UNKNOWN
                if evidence_status is EvidenceStatus.UNKNOWN
                else EligibilityStatus.ELIGIBLE
            )
            reason = (
                "authorization_evidence_unknown; operator_authorization_required"
                if status is EligibilityStatus.AUTHORIZATION_UNKNOWN
                else "authorization_evidence_recorded; operator_authorization_required"
            )
            return EligibilityDecision(
                status,
                (reason,),
                evidence_status=evidence_status,
                preference_state=preference_state,
                can_prepare=True,
                can_execute=False,
            )
        return EligibilityDecision(
            EligibilityStatus.ELIGIBLE,
            ("eligible",)
            if evidence_status is EvidenceStatus.ESTABLISHED
            else ("authorization_evidence_unknown",),
            evidence_status=evidence_status,
            preference_state=preference_state,
            can_prepare=True,
            can_execute=can_execute,
        )

    def evaluate_campaign(
        self, campaign: Any, contacts: Iterable[Any]
    ) -> list[EligibilityDecision]:
        return [self.evaluate_contact(contact, campaign=campaign) for contact in contacts]
