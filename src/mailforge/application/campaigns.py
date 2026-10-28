"""Application services for contacts, suppression, campaigns, and simulation."""

from __future__ import annotations

import json
import tempfile
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from mailforge.application.eligibility import EligibilityService
from mailforge.application.policy import PolicyEngine
from mailforge.application.scheduling import ScheduleEngine
from mailforge.application.sorting import EmailSorter, SortConfig
from mailforge.domain.campaign import (
    AttemptOutcome,
    CampaignStatus,
    ContactCandidate,
    DeliveryResult,
    DeliveryState,
    ExecutionAuthorization,
    SuppressionCategory,
)
from mailforge.domain.email import normalize_email, validate_email
from mailforge.domain.outbound import CatchUpPolicy, ScheduleMode, ScheduleSpec
from mailforge.domain.policy import (
    CommunicationPurpose,
    EligibilityStatus,
    EvidenceStatus,
    OperatorAction,
    PreferenceState,
)
from mailforge.infrastructure.campaign_store import CampaignStore


class SimulationTransport(Protocol):
    def send(self, email: str, intent_id: str, attempt_number: int) -> DeliveryResult: ...


class SimulatedCrash(RuntimeError):
    """Synthetic crash after a transport call, used to verify ambiguity handling."""


class ScriptedTransport:
    """Deterministic fake transport; it never opens a network connection."""

    def __init__(
        self,
        outcomes: Mapping[str, AttemptOutcome | DeliveryResult] | None = None,
        *,
        crash_after: Iterable[str] = (),
    ):
        self.outcomes = dict(outcomes or {})
        self.crash_after = set(crash_after)
        self.calls: list[tuple[str, str, int]] = []

    def send(self, email: str, intent_id: str, attempt_number: int) -> DeliveryResult:
        self.calls.append((email, intent_id, attempt_number))
        configured = self.outcomes.get(email, AttemptOutcome.ACCEPTED)
        result = (
            configured if isinstance(configured, DeliveryResult) else DeliveryResult(configured)
        )
        if email in self.crash_after:
            raise SimulatedCrash(f"synthetic crash after transport call for {email}")
        return result


@dataclass(frozen=True)
class CampaignReport:
    campaign_id: str
    campaign_status: str
    counts: dict[str, int]
    total: int
    unresolved: tuple[str, ...]
    recovered: dict[str, int]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class _CanonicalDispatchGuard:
    """Internal capability for dispatch through the canonical outbound path."""

    __slots__ = ("_owner", "_campaign_id", "_transport", "_token")

    def __init__(self, owner, campaign_id, transport, token):
        self._owner = owner
        self._campaign_id = campaign_id
        self._transport = transport
        self._token = token

    def valid_for(self, owner, campaign_id, transport) -> bool:
        return (
            self._owner is owner
            and self._campaign_id == campaign_id
            and self._transport is transport
            and self._token is owner._canonical_dispatch_token
        )

    def __call__(self, intent):
        return CampaignService._canonical_dispatch_check(self._owner, self._campaign_id, intent)


class CampaignService:
    """Coordinates durable domain operations without owning SMTP or files."""

    def __init__(self, store: CampaignStore, *, now_fn=None):
        self.store = store
        self._canonical_dispatch_token = object()
        self.now_fn = now_fn or (lambda: datetime.now(UTC))
        self._policy = PolicyEngine(store, now_fn=self.now_fn)
        self._eligibility = EligibilityService(store)
        self._schedules = ScheduleEngine()

    def _make_canonical_dispatch_guard(self, campaign_id, transport):
        return _CanonicalDispatchGuard(self, campaign_id, transport, self._canonical_dispatch_token)

    def _canonical_dispatch_check(self, campaign_id, intent):
        now = self.now_fn()
        schedule = self._stored_schedule(campaign_id)
        if schedule is not None and not self._schedules.due(schedule, now):
            retry_at = self._schedules.next_eligible(schedule, now)
            return (
                False,
                "sending window is not currently due",
                retry_at.isoformat() if retry_at else None,
            )
        campaign = self.store.get_campaign(campaign_id)
        decision = self._policy.reserve(
            attempt_id=intent["attempt_id"],
            intent_id=intent["id"],
            recipient=intent["normalized_email"],
            preferred_ref=campaign["sender_config_ref"],
        )
        if not decision.allowed:
            return decision.allowed, decision.reason, decision.retry_at
        pacing_allowed, retry_at = self.store.reserve_campaign_pacing(
            campaign_id,
            now=now,
            interval_seconds=schedule.pacing_seconds if schedule else 0.0,
        )
        if not pacing_allowed:
            self.store.release_policy_reservation(intent["attempt_id"])
            return False, "SMTP pacing interval is active", retry_at
        return decision.allowed, decision.reason, decision.retry_at

    def _stored_schedule(self, campaign_id: str) -> ScheduleSpec | None:
        row = self.store.get_schedule(campaign_id)
        if row is None:
            return None
        return ScheduleSpec(
            mode=ScheduleMode(row["mode"]),
            timezone=row["timezone"],
            scheduled_at=row["scheduled_at"],
            window_start=row["window_start"],
            window_end=row["window_end"],
            weekdays=tuple(json.loads(row["weekdays_json"])),
            pacing_seconds=row["pacing_seconds"],
            cooldown_seconds=row["cooldown_seconds"],
            recurrence=row["recurrence"],
            catch_up_policy=CatchUpPolicy(row["catch_up_policy"]),
            fold=row["fold"],
        )

    def import_candidates(
        self, candidates: Iterable[ContactCandidate], list_id: str
    ) -> dict[str, int]:
        imported = 0
        unique_contacts: set[str] = set()
        for candidate in candidates:
            contact_id = self.store.upsert_contact(
                candidate.normalized_email,
                metadata=candidate.metadata,
                source_file=candidate.source_file,
                source_record_position=candidate.source_record_position,
                original_input=candidate.original_input,
            )
            self.store.add_to_list(list_id, contact_id)
            imported += 1
            unique_contacts.add(contact_id)
        return {"records_imported": imported, "unique_contacts": len(unique_contacts)}

    def import_sort_results(self, processing_jsonl: str | Path, list_id: str) -> dict[str, int]:
        """Import the documented sorting result schema, not legacy output files."""

        candidates: list[ContactCandidate] = []
        with Path(processing_jsonl).open("r", encoding="utf-8") as handle:
            for line in handle:
                record = json.loads(line)
                if record.get("processing_disposition") != "accepted":
                    continue
                normalized = record.get("normalized_address")
                if not isinstance(normalized, str) or not validate_email(normalized).valid:
                    continue
                candidates.append(
                    ContactCandidate(
                        normalized_email=normalize_email(normalized),
                        original_input=str(record.get("original_input", normalized)),
                        source_file=str(record.get("source_file", "sorting")),
                        source_record_position=int(record.get("source_record_position", 0)),
                        metadata={
                            "validation": record.get("validation_result", {}),
                            "classification": record.get("classification_result", {}),
                            "rule_version": record.get("rule_version"),
                        },
                    )
                )
        return self.import_candidates(candidates, list_id)

    def add_suppression(
        self,
        email: str,
        category: SuppressionCategory,
        reason: str,
        *,
        campaign_id: str | None = None,
        list_id: str | None = None,
        sender_config_ref: str | None = None,
        purpose: str | None = None,
        actor: str = "local-operator",
    ) -> str:
        contact_id = None
        normalized = normalize_email(email)
        candidates = self.store.list_contacts(list_id) if list_id else []
        for row in candidates:
            if row["normalized_email"] == normalized:
                contact_id = row["id"]
                break
        if campaign_id:
            scope_type, scope_id = "campaign", campaign_id
        elif list_id:
            scope_type, scope_id = "list", list_id
        elif sender_config_ref:
            scope_type, scope_id = "sender", sender_config_ref
        elif purpose:
            scope_type, scope_id = "purpose", purpose
        else:
            scope_type, scope_id = "global", None
        return self.store.add_suppression(
            category=category,
            reason=reason,
            normalized_email=normalized,
            contact_id=contact_id,
            scope_type=scope_type,
            scope_id=scope_id,
            actor=actor,
            source="operator",
        )

    def record_authorization_evidence(
        self,
        email: str,
        *,
        purpose: str = CommunicationPurpose.MARKETING.value,
        status: EvidenceStatus = EvidenceStatus.ESTABLISHED,
        source: str,
        actor: str = "local-operator",
        evidence_ref: str | None = None,
        list_id: str | None = None,
        campaign_id: str | None = None,
        policy_version: str = "1",
        details: dict[str, object] | None = None,
    ) -> str:
        contact = self.store.contact_by_email(email)
        if contact is None:
            raise KeyError(f"contact not found: {email}")
        if campaign_id:
            scope_type, scope_id = "campaign", campaign_id
        elif list_id:
            scope_type, scope_id = "list", list_id
        else:
            scope_type, scope_id = "global", None
        return self.store.record_authorization_evidence(
            contact_id=contact["id"],
            purpose=purpose,
            status=status,
            source=source,
            recording_actor=actor,
            scope_type=scope_type,
            scope_id=scope_id,
            evidence_ref=evidence_ref,
            policy_version=policy_version,
            details=details,
        )

    def set_preference(
        self,
        email: str,
        *,
        purpose: str = CommunicationPurpose.MARKETING.value,
        state: PreferenceState,
        source: str = "operator",
        actor: str = "local-operator",
        evidence_ref: str | None = None,
        list_id: str | None = None,
        campaign_id: str | None = None,
        policy_version: str = "1",
    ) -> str:
        contact = self.store.contact_by_email(email)
        if contact is None:
            raise KeyError(f"contact not found: {email}")
        if campaign_id:
            scope_type, scope_id = "campaign", campaign_id
        elif list_id:
            scope_type, scope_id = "list", list_id
        else:
            scope_type, scope_id = "global", None
        return self.store.set_preference(
            contact_id=contact["id"],
            purpose=purpose,
            state=state,
            source=source,
            recording_actor=actor,
            scope_type=scope_type,
            scope_id=scope_id,
            evidence_ref=evidence_ref,
            policy_version=policy_version,
        )

    def inspect_eligibility(self, campaign_id: str) -> list[dict[str, object]]:
        campaign = self.store.get_campaign(campaign_id)
        contacts = (
            [
                self.store.get_contact(row["contact_id"])
                for row in self.store.list_intents(campaign_id)
            ]
            if campaign["audience_source_type"] == "segment"
            else self.store.list_contacts(campaign["audience_list_id"])
        )
        return [
            {
                "email": contact["normalized_email"],
                **self._eligibility.evaluate_contact(contact, campaign=campaign).to_dict(),
            }
            for contact in contacts
        ]

    def create_campaign(
        self,
        name: str,
        audience_list_id: str | None,
        *,
        template_ref: str = "synthetic",
        sender_config_ref: str = "synthetic-sender",
        description: str = "",
        configuration: dict[str, object] | None = None,
        audience_source_type: str = "list",
        audience_source_id: str | None = None,
        audience_revision_id: str | None = None,
        audience_run_key: str = "primary",
    ) -> str:
        if audience_source_type not in {"list", "segment"}:
            raise ValueError("audience_source_type must be list or segment")
        if audience_source_type == "segment" and not audience_list_id:
            anchor = f"__segment_anchor__{uuid.uuid4().hex}"
            audience_list_id = self.store.create_list(anchor, "Internal segment campaign anchor")
        if not audience_list_id:
            raise ValueError("a list audience requires a list id")
        source_id = audience_source_id or audience_list_id
        return self.store.create_campaign(
            name=name,
            description=description,
            sender_config_ref=sender_config_ref,
            template_ref=template_ref,
            audience_list_id=audience_list_id,
            configuration=configuration,
            audience_source_type=audience_source_type,
            audience_source_id=source_id,
            audience_revision_id=audience_revision_id,
            audience_run_key=audience_run_key,
        )

    def prepare(self, campaign_id: str, *, run_key: str = "primary") -> CampaignReport:
        campaign = self.store.get_campaign(campaign_id)
        if run_key != "primary" and campaign["audience_source_type"] == "list":
            from mailforge.application.segmentation import SegmentationService

            SegmentationService(self.store, now_fn=self.now_fn).prepare_campaign(
                campaign_id,
                source_type="list",
                source_id=campaign["audience_source_id"],
                run_key=run_key,
                eligibility_service=self._eligibility,
            )
            if (
                CampaignStatus(self.store.get_campaign(campaign_id)["status"])
                is CampaignStatus.DRAFT
            ):
                self.store.transition_campaign(
                    campaign_id, CampaignStatus.VALIDATING, "preparation_started"
                )
            if (
                CampaignStatus(self.store.get_campaign(campaign_id)["status"])
                is CampaignStatus.VALIDATING
            ):
                self.store.transition_campaign(
                    campaign_id, CampaignStatus.READY, "preparation_completed"
                )
            return self.report(campaign_id, run_key=run_key)
        if campaign["audience_source_type"] == "segment":
            from mailforge.application.segmentation import SegmentationService

            SegmentationService(self.store, now_fn=self.now_fn).prepare_campaign(
                campaign_id,
                run_key=run_key,
                eligibility_service=self._eligibility,
            )
            current = self.store.get_campaign(campaign_id)
            if CampaignStatus(current["status"]) is CampaignStatus.DRAFT:
                self.store.transition_campaign(
                    campaign_id, CampaignStatus.VALIDATING, "preparation_started"
                )
            if (
                CampaignStatus(self.store.get_campaign(campaign_id)["status"])
                is CampaignStatus.VALIDATING
            ):
                self.store.transition_campaign(
                    campaign_id, CampaignStatus.READY, "preparation_completed"
                )
            return self.report(campaign_id, run_key=run_key)
        if CampaignStatus(campaign["status"]) is CampaignStatus.DRAFT:
            self.store.transition_campaign(
                campaign_id, CampaignStatus.VALIDATING, "preparation_started"
            )
        elif CampaignStatus(campaign["status"]) not in {
            CampaignStatus.READY,
            CampaignStatus.PAUSED,
        }:
            raise ValueError(f"campaign cannot be prepared from {campaign['status']}")
        campaign = self.store.get_campaign(campaign_id)
        rows = self.store.list_contacts(campaign["audience_list_id"])
        for contact in rows:
            normalized = contact["normalized_email"]
            decision = self._eligibility.evaluate_contact(contact, campaign=campaign)
            if decision.status in {EligibilityStatus.EXCLUDED, EligibilityStatus.INVALID}:
                state = (
                    DeliveryState.NOT_ELIGIBLE
                    if decision.status is EligibilityStatus.INVALID
                    else DeliveryState.SUPPRESSED
                )
            else:
                state = DeliveryState.ELIGIBLE_PENDING
            eligibility_reason = decision.reason
            self.store.prepare_intent(
                campaign_id=campaign_id,
                contact_id=contact["id"],
                normalized_email=normalized,
                state=state,
                eligibility_reason=eligibility_reason,
            )
        if (
            CampaignStatus(self.store.get_campaign(campaign_id)["status"])
            is CampaignStatus.VALIDATING
        ):
            self.store.transition_campaign(
                campaign_id, CampaignStatus.READY, "preparation_completed"
            )
        return self.report(campaign_id)

    def preview(self, campaign_id: str) -> dict[str, object]:
        campaign = self.store.get_campaign(campaign_id)
        intents = []
        for row in self.store.list_intents(campaign_id):
            decision = self._eligibility.evaluate_contact(
                self.store.get_contact(row["contact_id"]),
                campaign=campaign,
                operator_authorized=(
                    campaign["authorization"] == ExecutionAuthorization.SIMULATION_AUTHORIZED.value
                ),
            )
            current_state = row["state"]
            if decision.status is EligibilityStatus.INVALID:
                current_state = DeliveryState.NOT_ELIGIBLE.value
            elif decision.status is EligibilityStatus.EXCLUDED:
                current_state = DeliveryState.SUPPRESSED.value
            intents.append(
                {
                    "email": row["normalized_email"],
                    "state": current_state,
                    "prepared_state": row["state"],
                    "reason": decision.reason,
                    "current_eligibility": decision.to_dict(),
                }
            )
        return {
            "campaign_id": campaign_id,
            "campaign_status": campaign["status"],
            "authorization": campaign["authorization"],
            "intents": intents,
        }

    def authorize_simulation(self, campaign_id: str) -> None:
        campaign = self.store.get_campaign(campaign_id)
        status = CampaignStatus(campaign["status"])
        if status is CampaignStatus.RUNNING:
            if campaign["authorization"] != ExecutionAuthorization.SIMULATION_AUTHORIZED.value:
                raise ValueError("running campaign is not already simulation-authorized")
            if not self.store.has_execution_declaration(campaign_id):
                self.store.record_operator_declaration(
                    campaign_id=campaign_id,
                    action=OperatorAction.AUTHORIZE_EXECUTION,
                    declaration=(
                        "I confirm the existing simulation authorization after a schema migration; "
                        "this recovery records no historical declaration and is not recipient "
                        "consent evidence."
                    ),
                    actor="local-operator",
                    idempotency_key=f"simulation-recovery:{campaign_id}",
                    metadata={"recovery": "schema-5-declaration"},
                )
            return
        if status not in {
            CampaignStatus.READY,
            CampaignStatus.SCHEDULED,
            CampaignStatus.PAUSED,
        }:
            raise ValueError("only prepared campaigns can be simulation-authorized")
        self.store.authorize_simulation(campaign_id)

    def pause(self, campaign_id: str) -> None:
        campaign = self.store.get_campaign(campaign_id)
        if CampaignStatus(campaign["status"]) is not CampaignStatus.RUNNING:
            raise ValueError("campaign can only be paused while running")
        self.store.request_pause(campaign_id)

    def cancel(self, campaign_id: str) -> None:
        campaign = self.store.get_campaign(campaign_id)
        if CampaignStatus(campaign["status"]) not in {
            CampaignStatus.RUNNING,
            CampaignStatus.PAUSED,
            CampaignStatus.READY,
        }:
            raise ValueError(f"campaign cannot be canceled from {campaign['status']}")
        self.store.request_cancel(campaign_id)
        if CampaignStatus(campaign["status"]) is not CampaignStatus.CANCELED:
            self.store.transition_campaign(
                campaign_id, CampaignStatus.CANCELED, "operator_canceled"
            )

    def execute(
        self,
        campaign_id: str,
        transport: SimulationTransport,
        *,
        retry_deferred: bool = False,
        stop_after: int | None = None,
        dispatch_guard: Callable[[object], tuple[bool, str, str | None]] | None = None,
        start_existing: bool = False,
        worker_id: str = "simulation-worker",
    ) -> CampaignReport:
        campaign = self.store.get_campaign(campaign_id)
        if getattr(transport, "requires_policy_guard", False):
            if type(dispatch_guard) is not _CanonicalDispatchGuard or not dispatch_guard.valid_for(
                self, campaign_id, transport
            ):
                raise PermissionError("SMTP transport requires the canonical outbound policy guard")
        if (
            campaign["authorization"] != ExecutionAuthorization.SIMULATION_AUTHORIZED.value
            or not self.store.has_execution_declaration(campaign_id)
        ):
            raise PermissionError(
                "simulation authorization and an operator declaration are required; "
                "live SMTP is not available"
            )
        status = CampaignStatus(campaign["status"])
        if status is CampaignStatus.COMPLETED:
            return self.report(campaign_id)
        if status not in {
            CampaignStatus.READY,
            CampaignStatus.PAUSED,
            CampaignStatus.SCHEDULED,
        } and not (start_existing and status is CampaignStatus.RUNNING):
            raise ValueError(f"campaign cannot execute from {status.value}")
        recovered = self.store.recover_inflight(campaign_id)
        self.store.clear_pause_request(campaign_id)
        if status is not CampaignStatus.RUNNING:
            self.store.transition_campaign(
                campaign_id,
                CampaignStatus.RUNNING,
                "schedule_due" if status is CampaignStatus.SCHEDULED else "simulation_started",
            )
        processed = 0
        while True:
            pause_requested, cancel_requested = self.store.campaign_flags(campaign_id)
            if cancel_requested:
                break
            if pause_requested:
                self.store.transition_campaign(
                    campaign_id, CampaignStatus.PAUSING, "pause_requested"
                )
                self.store.transition_campaign(
                    campaign_id, CampaignStatus.PAUSED, "paused_by_operator"
                )
                return self.report(campaign_id, recovered=recovered)
            intent = self.store.reserve_next(
                campaign_id,
                retry_deferred=retry_deferred,
                max_attempts=3,
                worker_id=worker_id,
            )
            if intent is None:
                break
            fresh_campaign = self.store.get_campaign(campaign_id)
            policy_revision = self.store.policy_revision()
            suppression = self.store.suppression_reason(
                normalized_email=intent["normalized_email"],
                contact_id=intent["contact_id"],
                list_id=fresh_campaign["audience_list_id"],
                campaign_id=campaign_id,
                sender_config_ref=fresh_campaign["sender_config_ref"],
                communication_purpose=self._eligibility.purpose(fresh_campaign),
            )
            if suppression:
                self.store.mark_intent_suppressed(intent["id"], suppression)
                continue
            contact = self.store.get_contact(intent["contact_id"])
            decision = self._eligibility.evaluate_contact(
                contact, campaign=fresh_campaign, operator_authorized=True
            )
            if decision.status in {EligibilityStatus.EXCLUDED, EligibilityStatus.REQUIRES_REVIEW}:
                self.store.mark_intent_suppressed(intent["id"], decision.reason)
                continue
            if dispatch_guard is not None:
                allowed, reason, retry_at = dispatch_guard(intent)
                if not allowed:
                    self.store.release_reservation(intent["id"], reason, not_before_utc=retry_at)
                    break
            if not self.store.mark_attempt_started(
                intent["id"],
                intent["attempt_id"],
                expected_policy_revision=policy_revision,
            ):
                current = self._eligibility.evaluate_contact(
                    self.store.get_contact(intent["contact_id"]),
                    campaign=self.store.get_campaign(campaign_id),
                    operator_authorized=True,
                )
                if current.status in {
                    EligibilityStatus.EXCLUDED,
                    EligibilityStatus.REQUIRES_REVIEW,
                }:
                    self.store.mark_intent_suppressed(intent["id"], current.reason)
                else:
                    self.store.release_reservation(intent["id"], "policy_changed_before_dispatch")
                continue
            try:
                result = transport.send(
                    intent["normalized_email"], intent["id"], intent["attempt_number"]
                )
            except SimulatedCrash as error:
                self.store.record_delivery_result(
                    intent_id=intent["id"],
                    attempt_id=intent["attempt_id"],
                    result=AttemptOutcome.AMBIGUOUS,
                    error_code="simulated_crash",
                    error_message=str(error),
                )
                self.store.transition_campaign(
                    campaign_id, CampaignStatus.COMPLETING, "ambiguous_transport_outcome"
                )
                self.store.transition_campaign(
                    campaign_id, CampaignStatus.PAUSED, "reconciliation_required"
                )
                return self.report(campaign_id, recovered=recovered)
            except BaseException as error:
                self.store.record_delivery_result(
                    intent_id=intent["id"],
                    attempt_id=intent["attempt_id"],
                    result=AttemptOutcome.AMBIGUOUS,
                    error_code="transport_exception",
                    error_message=type(error).__name__,
                )
                self.store.transition_campaign(
                    campaign_id, CampaignStatus.COMPLETING, "transport_exception"
                )
                self.store.transition_campaign(
                    campaign_id, CampaignStatus.PAUSED, "reconciliation_required"
                )
                return self.report(campaign_id, recovered=recovered)
            self.store.record_delivery_result(
                intent_id=intent["id"],
                attempt_id=intent["attempt_id"],
                result=result.outcome,
                error_code=result.error_code,
                error_message=result.error_message,
                transport_id=result.transport_id,
                transport_phase=result.transport_phase,
                response_code=result.response_code,
            )
            processed += 1
            if stop_after is not None and processed >= stop_after:
                self.store.transition_campaign(
                    campaign_id, CampaignStatus.PAUSING, "synthetic_interruption"
                )
                self.store.transition_campaign(
                    campaign_id, CampaignStatus.PAUSED, "resume_available"
                )
                return self.report(campaign_id, recovered=recovered)
        _, cancel_requested = self.store.campaign_flags(campaign_id)
        if cancel_requested:
            return self.report(campaign_id, recovered=recovered)
        counts = self.store.counts(campaign_id)
        unresolved = tuple(
            state
            for state in (
                DeliveryState.ELIGIBLE_PENDING.value,
                DeliveryState.PREPARED.value,
                DeliveryState.DEFERRED.value,
                DeliveryState.AMBIGUOUS.value,
                DeliveryState.RESERVED.value,
                DeliveryState.ATTEMPT_STARTED.value,
            )
            if counts.get(state, 0)
        )
        self.store.transition_campaign(campaign_id, CampaignStatus.COMPLETING, "simulation_drained")
        if unresolved:
            self.store.transition_campaign(
                campaign_id, CampaignStatus.PAUSED, "unresolved_delivery_state"
            )
        else:
            self.store.transition_campaign(
                campaign_id, CampaignStatus.COMPLETED, "simulation_completed"
            )
        return self.report(campaign_id, recovered=recovered)

    def report(
        self,
        campaign_id: str,
        *,
        recovered: dict[str, int] | None = None,
        run_key: str = "primary",
    ) -> CampaignReport:
        if run_key == "primary":
            counts = self.store.counts(campaign_id)
        else:
            with self.store.connect() as connection:
                rows = connection.execute(
                    "SELECT r.state, COUNT(*) AS count "
                    "FROM campaign_audience_recipients r "
                    "JOIN campaign_audiences a ON a.id = r.audience_id "
                    "WHERE a.campaign_id = ? AND a.run_key = ? GROUP BY r.state",
                    (campaign_id, run_key),
                ).fetchall()
            counts = {row["state"]: row["count"] for row in rows}
        unresolved = tuple(
            state
            for state in (
                DeliveryState.DEFERRED.value,
                DeliveryState.AMBIGUOUS.value,
                DeliveryState.ELIGIBLE_PENDING.value,
                DeliveryState.PREPARED.value,
                DeliveryState.RESERVED.value,
                DeliveryState.ATTEMPT_STARTED.value,
            )
            if counts.get(state, 0)
        )
        campaign = self.store.get_campaign(campaign_id)
        return CampaignReport(
            campaign_id=campaign_id,
            campaign_status=campaign["status"],
            counts=counts,
            total=sum(counts.values()),
            unresolved=unresolved,
            recovered=recovered or {"reserved": 0, "ambiguous": 0},
        )

    def export_contacts(self, *, list_id: str | None = None) -> list[dict[str, object]]:
        return self.store.export_contacts(list_id=list_id)

    def correct_contact(
        self,
        email: str,
        *,
        display_name: str | None = None,
        metadata: dict[str, object] | None = None,
        actor: str = "local-operator",
    ) -> None:
        contact = self.store.contact_by_email(email)
        if contact is None:
            raise KeyError(f"contact not found: {email}")
        self.store.update_contact(
            contact["id"], display_name=display_name, metadata=metadata, actor=actor
        )

    def deactivate_contact(self, email: str, *, reason: str, actor: str = "local-operator") -> None:
        contact = self.store.contact_by_email(email)
        if contact is None:
            raise KeyError(f"contact not found: {email}")
        self.store.deactivate_contact(contact["id"], reason=reason, actor=actor)

    def plan_deletion(self, email: str, *, reason: str, actor: str = "local-operator") -> str:
        contact = self.store.contact_by_email(email)
        if contact is None:
            raise KeyError(f"contact not found: {email}")
        return self.store.plan_lifecycle_action(
            contact_id=contact["id"],
            action="deletion_plan",
            reason=reason,
            actor=actor,
            idempotency_key=f"deletion:{contact['id']}:{reason}",
        )


def run_synthetic_demo(database: str | Path) -> dict[str, object]:
    """Run the complete local workflow against synthetic contacts and fake transport."""

    database = Path(database)
    store = CampaignStore(database)
    service = CampaignService(store)
    with tempfile.TemporaryDirectory(prefix="mailforge-demo-") as directory:
        source = Path(directory) / "contacts.txt"
        source.write_text(
            "alice@example.com\nbob@example.com\nsuppressed@example.com\n"
            "transient@example.com\npermanent@example.com\nambiguous@example.com\n",
            encoding="utf-8",
        )
        sort_config = SortConfig(
            results_dir=Path(directory) / "results",
            config_dir=Path(directory) / "config",
            log_dir=Path(directory) / "logs",
            checkpoint_path=Path(directory) / "checkpoint.json",
            profile="report",
            min_disk_space_gb=0,
        )
        sort_result = EmailSorter(sort_config).sort_file(source, resume=False, dry_run=True)
        list_id = store.create_list("synthetic-audience", "Synthetic MAILER-CORE-005 audience")
        service.import_sort_results(sort_result.structured_results_path, list_id)
    service.add_suppression(
        "suppressed@example.com",
        SuppressionCategory.UNSUBSCRIBE,
        "synthetic unsubscribe",
    )
    campaign_id = service.create_campaign("synthetic-campaign", list_id)
    service.prepare(campaign_id)
    preview = service.preview(campaign_id)
    service.authorize_simulation(campaign_id)
    outcomes = {
        "transient@example.com": AttemptOutcome.TRANSIENT_FAILURE,
        "permanent@example.com": AttemptOutcome.PERMANENT_FAILURE,
        "ambiguous@example.com": AttemptOutcome.AMBIGUOUS,
    }
    first = service.execute(campaign_id, ScriptedTransport(outcomes), stop_after=2)
    final = service.execute(campaign_id, ScriptedTransport(outcomes), retry_deferred=False)
    return {
        "campaign_id": campaign_id,
        "preview": preview,
        "first_run": first.to_dict(),
        "final": final.to_dict(),
    }
