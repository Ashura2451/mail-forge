"""Campaign workspace orchestration over Mailforge's existing authorities."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from mailforge.application.eligibility import EligibilityService
from mailforge.application.scheduling import ScheduleEngine
from mailforge.domain.campaign import CampaignStatus
from mailforge.domain.email import validate_email
from mailforge.domain.outbound import (
    CatchUpPolicy,
    ScheduleMode,
    ScheduleSpec,
    SenderPolicy,
    TemplateDefinition,
)
from mailforge.infrastructure.campaign_store import CampaignStore


class CampaignOperationsError(ValueError):
    """A bounded, operator-facing campaign workspace error."""

    def __init__(self, message: str, code: str = "campaign_operation_invalid"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PreflightFinding:
    area: str
    severity: str
    code: str
    message: str
    source: str
    action: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _safe_config(campaign: Any) -> dict[str, Any]:
    configuration = json.loads(campaign["configuration_json"] or "{}")
    return {
        key: configuration[key]
        for key in (
            "communication_purpose",
            "sender_email",
            "subject",
            "text",
            "execution_mode",
            "external_authorization_ref",
            "external_authorization_purpose",
            "external_authorization_expires_at",
            "external_recipient_set_hash",
            "external_prepared_set_hash",
            "unsubscribe_endpoint_ref",
            "one_click_unsubscribe",
            "dkim_signed",
        )
        if key in configuration
    }


def _digest_values(values: list[str]) -> str:
    return hashlib.sha256(
        json.dumps(sorted(values), separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _safe_provider(account: dict[str, Any] | None) -> dict[str, Any] | None:
    if account is None:
        return None
    return {
        key: account[key]
        for key in (
            "ref",
            "display_name",
            "sender_email",
            "status",
            "pacing_per_minute",
            "max_concurrency",
            "outbound_enabled",
            "kind",
            "host",
            "port",
            "sender_policy_ref",
            "profile_enabled",
        )
        if key in account
    }


def _execution_state(
    campaign: Any,
    report: Any,
    last_transition: Any,
    next_automatic_at: str | None,
) -> dict[str, Any]:
    """Project canonical campaign facts into operator-facing lifecycle language."""

    status = CampaignStatus(campaign["status"])
    counts = report.counts
    pause_requested = bool(campaign["pause_requested"])
    transition_reason = last_transition["reason"] if last_transition else None
    if status is CampaignStatus.COMPLETED:
        if counts.get("permanent_failure", 0) or counts.get("deferred", 0):
            key, label = "completed_with_failures", "Completed with failures"
        else:
            key, label = "completed_successfully", "Completed successfully"
    elif status is CampaignStatus.PAUSED and pause_requested:
        key, label = "manually_paused", "Manually paused"
    elif status is CampaignStatus.PAUSED and (
        counts.get("ambiguous", 0) or counts.get("deferred", 0)
    ):
        key, label = "requires_reconciliation", "Requires reconciliation"
    elif (
        status is CampaignStatus.PAUSED
        and next_automatic_at is not None
        and transition_reason
        in {
            "retry_after_temporary_restriction",
            "worker_waiting_for_temporary_restriction",
            "unresolved_delivery_state",
        }
    ):
        key, label = "waiting", "Waiting for an execution restriction"
    else:
        key = status.value
        label = {
            "draft": "Draft",
            "validating": "Preparing",
            "ready": "Ready",
            "scheduled": "Scheduled",
            "running": "Running",
            "pausing": "Pausing",
            "paused": "Paused",
            "completing": "Completing",
            "failed": "Failed",
            "canceled": "Canceled",
        }.get(status.value, status.value.replace("_", " ").title())
    return {
        "key": key,
        "label": label,
        "canonical_status": status.value,
        "operator_pause_requested": pause_requested,
        "automatic_continuation": key == "waiting",
        "next_automatic_at": next_automatic_at if key == "waiting" else None,
        "last_transition_reason": transition_reason,
    }


class CampaignOperationsService:
    """The single composition boundary for campaign creation and supervision."""

    def __init__(
        self,
        store: CampaignStore,
        *,
        campaigns,
        outbound,
        message_studio,
        segmentation,
        provider_operations,
        worker_active: Callable[[str], bool] | None = None,
        worker_start: Callable[[str], bool] | None = None,
    ):
        self.store = store
        self.campaigns = campaigns
        self.outbound = outbound
        self.message_studio = message_studio
        self.segmentation = segmentation
        self.provider_operations = provider_operations
        self.worker_active = worker_active or (lambda _campaign_id: False)
        self.worker_start = worker_start or (lambda _campaign_id: False)
        self.schedules = ScheduleEngine()
        self.eligibility = EligibilityService(store)

    def _source(self, *, list_id: str | None, segment_id: str | None, revision_id: str | None):
        if bool(list_id) == bool(segment_id):
            raise CampaignOperationsError(
                "choose exactly one list or segment audience", "audience_required"
            )
        if list_id:
            with self.store.connect() as connection:
                if (
                    connection.execute("SELECT 1 FROM lists WHERE id=?", (list_id,)).fetchone()
                    is None
                ):
                    raise CampaignOperationsError("audience list was not found", "list_not_found")
            return "list", list_id, None
        try:
            segment = self.segmentation.get_segment(segment_id or "", revision_id)
        except Exception as error:
            raise CampaignOperationsError(str(error), "segment_not_found") from error
        return "segment", segment["id"], segment["revision"]["id"]

    def _validate_message_revision(self, revision_id: str | None) -> None:
        if not revision_id:
            return
        with self.store.connect() as connection:
            revision = connection.execute(
                "SELECT status FROM message_revisions WHERE id=?", (revision_id,)
            ).fetchone()
        if revision is None or revision["status"] != "published":
            raise CampaignOperationsError(
                "campaigns require a published message revision", "revision_not_published"
            )

    def create_draft(
        self,
        *,
        name: str,
        description: str = "",
        list_id: str | None = None,
        segment_id: str | None = None,
        segment_revision_id: str | None = None,
        sender_email: str = "sender@example.com",
        subject: str = "Synthetic Mailforge campaign",
        text: str = "Hello {{ email }}",
        purpose: str = "transactional",
        unsubscribe_endpoint_ref: str | None = None,
        one_click_unsubscribe: bool = False,
        dkim_signed: bool = False,
        message_revision_id: str | None = None,
        provider_account_ref: str | None = None,
    ) -> str:
        source_type, source_id, audience_revision_id = self._source(
            list_id=list_id, segment_id=segment_id, revision_id=segment_revision_id
        )
        if not validate_email(sender_email).valid:
            raise CampaignOperationsError("sender email is invalid", "sender_invalid")
        self._validate_message_revision(message_revision_id)
        sender_ref = f"campaign-{uuid.uuid4().hex[:16]}"
        if provider_account_ref:
            try:
                account = self.provider_operations.account(provider_account_ref)
            except KeyError as error:
                raise CampaignOperationsError(str(error), "provider_account_not_found") from error
            sender_ref = account.get("sender_policy_ref") or f"provider-{provider_account_ref}"
            if not account.get("sender_policy_ref") and not any(
                row["ref"] == sender_ref for row in self.store.list_sender_policies()
            ):
                self.store.register_sender_policy(
                    SenderPolicy(
                        ref=sender_ref,
                        provider="smtp",
                        email=account.get("sender_email") or sender_email,
                        max_per_minute=int(account.get("pacing_per_minute") or 60),
                        max_concurrency=int(account.get("max_concurrency") or 1),
                    )
                )
        return self.campaigns.create_campaign(
            name.strip(),
            list_id,
            description=description,
            template_ref=f"campaign-template-{uuid.uuid4().hex[:12]}",
            sender_config_ref=sender_ref,
            configuration={
                "communication_purpose": purpose,
                "operator_authorization_required": True,
                "sender_email": sender_email,
                "subject": subject,
                "text": text,
                "unsubscribe_endpoint_ref": unsubscribe_endpoint_ref,
                "one_click_unsubscribe": one_click_unsubscribe,
                "dkim_signed": dkim_signed,
            },
            audience_source_type=source_type,
            audience_source_id=source_id,
            audience_revision_id=audience_revision_id,
            message_revision_id=message_revision_id,
            provider_account_ref=provider_account_ref,
        )

    def duplicate_campaign(self, campaign_id: str, *, name: str | None = None) -> str:
        """Create a fresh editable draft without copying authorization or outcomes."""

        source = self.store.get_campaign(campaign_id)
        configuration = _safe_config(source)
        configuration.pop("archived_at", None)
        configuration.pop("recurrence_parent_id", None)
        configuration.pop("recurrence_index", None)
        base_name = name.strip() if name else f"{source['name']} copy"
        candidate = base_name
        suffix = 2
        with self.store.connect() as connection:
            while connection.execute(
                "SELECT 1 FROM campaigns WHERE name = ?", (candidate,)
            ).fetchone():
                candidate = f"{base_name} {suffix}"
                suffix += 1
        return self.campaigns.create_campaign(
            candidate,
            source["audience_list_id"],
            description=source["description"] or "",
            template_ref=f"campaign-template-{uuid.uuid4().hex[:12]}",
            sender_config_ref=source["sender_config_ref"],
            configuration=configuration,
            audience_source_type=source["audience_source_type"],
            audience_source_id=source["audience_source_id"],
            audience_revision_id=source["audience_revision_id"],
            message_revision_id=source["message_revision_id"],
            provider_account_ref=source["provider_account_ref"],
        )

    def archive_campaign(self, campaign_id: str) -> dict[str, Any]:
        """Archive a campaign and stop any not-yet-started recurring children."""

        source = self.store.get_campaign(campaign_id)
        self.store.archive_campaign(campaign_id)
        source_config = json.loads(source["configuration_json"] or "{}")
        series_id = source_config.get("recurrence_parent_id", campaign_id)
        with self.store.connect() as connection:
            children = connection.execute(
                "SELECT id, status, configuration_json FROM campaigns WHERE id != ?",
                (campaign_id,),
            ).fetchall()
        for child in children:
            config = json.loads(child["configuration_json"] or "{}")
            if config.get("recurrence_parent_id") != series_id:
                continue
            if child["status"] in {
                CampaignStatus.READY.value,
                CampaignStatus.SCHEDULED.value,
                CampaignStatus.PAUSED.value,
            }:
                try:
                    self.campaigns.cancel(child["id"])
                except ValueError:
                    pass
                self.store.archive_campaign(child["id"], reason="recurring_series_archived")
        return {"campaign_id": campaign_id, "status": "archived"}

    def create_next_recurring_run(self, campaign_id: str) -> str | None:
        """Create a fresh, independently prepared child for a recurring definition."""

        source = self.store.get_campaign(campaign_id)
        source_config = json.loads(source["configuration_json"] or "{}")
        if source_config.get("archived_at"):
            return None
        schedule_row = self.store.get_schedule(campaign_id)
        if schedule_row is None or schedule_row["mode"] != ScheduleMode.RECURRING.value:
            return None
        schedule = self._schedule_from_row(schedule_row)
        next_at = self.schedules.next_recurrence(schedule, self.outbound.now_fn())
        parent_id = source_config.get("recurrence_parent_id", campaign_id)
        index = int(source_config.get("recurrence_index", 0)) + 1
        local_next = next_at.astimezone(ZoneInfo(schedule.timezone)).replace(tzinfo=None)
        run_name = f"{source['name'].split(' · run ')[0]} · run {index}"
        child_config = _safe_config(source)
        child_config.update(
            {
                "recurrence_parent_id": parent_id,
                "recurrence_index": index,
                "recurrence_source_campaign_id": campaign_id,
            }
        )
        child_id = self.campaigns.create_campaign(
            run_name,
            source["audience_list_id"],
            description=source["description"] or "",
            template_ref=f"campaign-template-{uuid.uuid4().hex[:12]}",
            sender_config_ref=source["sender_config_ref"],
            configuration=child_config,
            audience_source_type=source["audience_source_type"],
            audience_source_id=source["audience_source_id"],
            audience_revision_id=source["audience_revision_id"],
            message_revision_id=source["message_revision_id"],
            provider_account_ref=source["provider_account_ref"],
        )
        child_schedule = ScheduleSpec(
            mode=ScheduleMode.RECURRING,
            timezone=schedule.timezone,
            scheduled_at=local_next.isoformat(),
            weekdays=schedule.weekdays,
            pacing_seconds=schedule.pacing_seconds,
            cooldown_seconds=schedule.cooldown_seconds,
            recurrence=schedule.recurrence,
            catch_up_policy=schedule.catch_up_policy,
            fold=schedule.fold,
        )
        self.prepare(child_id)
        self.set_schedule(child_id, child_schedule)
        self.authorize(child_id)
        return child_id

    def update_draft(
        self, campaign_id: str, *, expected_updated_at: str | None, **values: Any
    ) -> None:
        campaign = self.store.get_campaign(campaign_id)
        source_type, source_id, audience_revision_id = self._source(
            list_id=values.get("list_id"),
            segment_id=values.get("segment_id"),
            revision_id=values.get("segment_revision_id"),
        )
        self._validate_message_revision(values.get("message_revision_id"))
        if not validate_email(values.get("sender_email", "sender@example.com")).valid:
            raise CampaignOperationsError("sender email is invalid", "sender_invalid")
        provider_ref = values.get("provider_account_ref")
        if provider_ref:
            try:
                self.provider_operations.account(provider_ref)
            except KeyError as error:
                raise CampaignOperationsError(str(error), "provider_account_not_found") from error
        old = _safe_config(campaign)
        audience_list_id = values.get("list_id") or campaign["audience_list_id"]
        if source_type == "segment" and campaign["audience_source_type"] != "segment":
            audience_list_id = self.store.create_list(
                f"__segment_anchor__{campaign_id[:12]}",
                "Internal segment campaign anchor",
            )
        configuration = {
            **old,
            "communication_purpose": values.get(
                "purpose", old.get("communication_purpose", "transactional")
            ),
            "sender_email": values.get(
                "sender_email", old.get("sender_email", "sender@example.com")
            ),
            "subject": values.get("subject", old.get("subject", "Synthetic Mailforge campaign")),
            "text": values.get("text", old.get("text", "Hello {{ email }}")),
            "unsubscribe_endpoint_ref": values.get(
                "unsubscribe_endpoint_ref", old.get("unsubscribe_endpoint_ref")
            ),
            "one_click_unsubscribe": values.get(
                "one_click_unsubscribe", old.get("one_click_unsubscribe", False)
            ),
            "dkim_signed": values.get("dkim_signed", old.get("dkim_signed", False)),
        }
        self.store.update_campaign_draft(
            campaign_id,
            expected_updated_at=expected_updated_at,
            name=values.get("name", campaign["name"]).strip(),
            description=values.get("description", campaign["description"] or ""),
            sender_config_ref=campaign["sender_config_ref"],
            template_ref=campaign["template_ref"],
            audience_list_id=audience_list_id,
            audience_source_type=source_type,
            audience_source_id=source_id,
            audience_revision_id=audience_revision_id,
            audience_run_key="primary",
            message_revision_id=values.get("message_revision_id"),
            provider_account_ref=provider_ref,
            configuration=configuration,
        )

    def _sender(self, campaign: Any) -> SenderPolicy:
        configuration = _safe_config(campaign)
        email = configuration.get("sender_email", "sender@example.com")
        account = None
        if campaign["provider_account_ref"]:
            account = self.provider_operations.account(campaign["provider_account_ref"])
            email = account.get("sender_email") or email
        return SenderPolicy(
            ref=campaign["sender_config_ref"],
            provider="synthetic",
            email=email,
            max_per_minute=int(account["pacing_per_minute"]) if account else 60,
            max_concurrency=int(account["max_concurrency"]) if account else 1,
            enabled=bool(account.get("outbound_enabled", True)) if account else True,
        )

    def preparation_options(self, campaign: Any) -> dict[str, Any]:
        return {
            "unsubscribe_url_callback": None,
            # The local build has no DKIM signer for final prepared bytes.
            # Keep campaign preparation to List-Unsubscribe until a real
            # signing boundary is supplied.
            "one_click_unsubscribe": False,
            "dkim_signed": False,
        }

    def preflight(self, campaign_id: str) -> dict[str, Any]:
        campaign = self.store.get_campaign(campaign_id)
        findings: list[PreflightFinding] = []
        audience_summary = {"matched": 0, "eligible": 0, "excluded": 0, "invalid": 0}
        source = campaign["audience_source_id"]
        try:
            if campaign["audience_source_type"] == "segment":
                segment = self.segmentation.get_segment(
                    campaign["audience_source_id"], campaign["audience_revision_id"]
                )
                compiled = self.segmentation.compile(segment["definition"], canonical_numbers=True)
                with self.store.connect() as connection:
                    contacts = list(
                        connection.execute(
                            f"SELECT c.* FROM contacts c WHERE {compiled.sql} "
                            "ORDER BY normalized_email LIMIT 100001",
                            compiled.parameters,
                        )
                    )
            else:
                with self.store.connect() as connection:
                    exists = connection.execute(
                        "SELECT 1 FROM lists WHERE id=?", (source,)
                    ).fetchone()
                if exists is None:
                    findings.append(
                        PreflightFinding(
                            "audience",
                            "blocker",
                            "audience_missing",
                            "The selected list no longer exists.",
                            "lists",
                            "choose another audience",
                        )
                    )
                contacts = self.store.list_contacts(source) if exists is not None else []
            audience_summary["matched"] = len(contacts)
            for contact in contacts[:100_000]:
                decision = self.eligibility.evaluate_contact(contact, campaign=campaign)
                if decision.status.value in {"eligible", "ready"}:
                    audience_summary["eligible"] += 1
                elif decision.status.value == "invalid":
                    audience_summary["invalid"] += 1
                else:
                    audience_summary["excluded"] += 1
        except Exception as error:
            findings.append(
                PreflightFinding(
                    "audience",
                    "blocker",
                    "audience_invalid",
                    str(error),
                    "SegmentationService",
                    "fix the audience source",
                )
            )
        if campaign["message_revision_id"]:
            try:
                self.message_studio.validate_revision(campaign["message_revision_id"])
            except Exception as error:
                findings.append(
                    PreflightFinding(
                        "content",
                        "blocker",
                        "message_invalid",
                        str(error),
                        "MessageStudioService",
                        "publish a valid revision",
                    )
                )
        else:
            config = _safe_config(campaign)
            if not config.get("subject") or not config.get("text"):
                findings.append(
                    PreflightFinding(
                        "content",
                        "blocker",
                        "content_missing",
                        "A subject and message body are required.",
                        "campaign configuration",
                        "complete the message",
                    )
                )
        account = None
        if campaign["provider_account_ref"]:
            try:
                account = self.provider_operations.account(campaign["provider_account_ref"])
                if not account.get("outbound_enabled") or not account.get("profile_enabled"):
                    findings.append(
                        PreflightFinding(
                            "sender",
                            "blocker",
                            "account_disabled",
                            "The selected sender account is not enabled for outbound use.",
                            "ProviderOperationsService",
                            "restore or choose an account",
                        )
                    )
                if account.get("status") in {"suspended", "blocked"}:
                    findings.append(
                        PreflightFinding(
                            "sender",
                            "blocker",
                            "account_suspended",
                            "The selected sender account is suspended.",
                            "ProviderOperationsService",
                            "restore or choose an account",
                        )
                    )
            except KeyError:
                findings.append(
                    PreflightFinding(
                        "sender",
                        "blocker",
                        "account_missing",
                        "The selected sender account was not found.",
                        "provider_accounts",
                        "choose another account",
                    )
                )
        else:
            findings.append(
                PreflightFinding(
                    "sender",
                    "warning",
                    "synthetic_sender",
                    "No provider account is bound; execution remains synthetic/local.",
                    "campaign configuration",
                    "bind an account for account-specific pacing",
                )
            )
        schedule_row = self.store.get_schedule(campaign_id)
        if schedule_row:
            schedule = self._schedule_from_row(schedule_row)
            try:
                self.schedules.validate(schedule)
            except Exception as error:
                findings.append(
                    PreflightFinding(
                        "schedule",
                        "blocker",
                        "schedule_invalid",
                        str(error),
                        "ScheduleEngine",
                        "correct the schedule",
                    )
                )
        else:
            findings.append(
                PreflightFinding(
                    "schedule",
                    "warning",
                    "schedule_immediate",
                    "No schedule is configured; the synthetic run is eligible immediately "
                    "after authorization.",
                    "campaign_schedules",
                    "add a schedule if needed",
                )
            )
        if not self.store.list_intents(campaign_id):
            findings.append(
                PreflightFinding(
                    "execution",
                    "warning",
                    "not_prepared",
                    "Recipient and message snapshots have not been prepared.",
                    "CampaignService/MessageStudioService",
                    "prepare the campaign",
                )
            )
        for area, message in (
            ("audience", "The selected audience source is available."),
            ("content", "The selected campaign content is locally valid."),
            ("sender", "The selected sender configuration is locally available."),
            ("schedule", "The configured schedule is locally valid."),
        ):
            if not any(
                finding.area == area and finding.severity in {"blocker", "warning"}
                for finding in findings
            ):
                findings.append(
                    PreflightFinding(area, "pass", f"{area}_pass", message, "local authority")
                )
        findings.append(
            PreflightFinding(
                "policy",
                "not_verified",
                "external_policy_not_verified",
                "Local policy and suppression checks are authoritative; external provider "
                "qualification is not measured.",
                "ProviderOperationsService / local policy",
                "keep execution synthetic until external qualification is approved",
            )
        )
        blockers = sum(item.severity == "blocker" for item in findings)
        return {
            "campaign_id": campaign_id,
            "ready": blockers == 0,
            "blocker_count": blockers,
            "warning_count": sum(item.severity == "warning" for item in findings),
            "findings": [item.to_dict() for item in findings],
            "provider": _safe_provider(account),
            "audience_summary": audience_summary,
        }

    def prepare(self, campaign_id: str, *, run_key: str = "primary") -> dict[str, Any]:
        preflight = self.preflight(campaign_id)
        if not preflight["ready"]:
            raise CampaignOperationsError("campaign preflight has blockers", "preflight_blocked")
        campaign = self.store.get_campaign(campaign_id)
        self.segmentation.prepare_campaign(
            campaign_id,
            run_key=run_key,
            eligibility_service=self.eligibility,
        )
        sender = self._sender(campaign)
        config = _safe_config(campaign)
        preparation_options = self.preparation_options(campaign)
        if campaign["message_revision_id"]:
            if run_key != "primary":
                raise CampaignOperationsError(
                    "Message Studio campaigns use the primary audience run", "run_key_unsupported"
                )
            report = self.message_studio.prepare_campaign(
                campaign_id,
                sender=sender,
                revision_id=campaign["message_revision_id"],
                run_key=run_key,
                **preparation_options,
            )
            return asdict(report)
        report = self.outbound.prepare_campaign(
            campaign_id,
            template=TemplateDefinition(
                campaign["template_ref"],
                "1",
                config.get("subject", "Synthetic Mailforge campaign"),
                config.get("text", "Hello {{ email }}"),
            ),
            sender=sender,
            schedule=self._schedule_from_row(self.store.get_schedule(campaign_id))
            if self.store.get_schedule(campaign_id)
            else None,
            **preparation_options,
        )
        return asdict(report)

    def set_schedule(self, campaign_id: str, schedule: ScheduleSpec) -> dict[str, Any]:
        try:
            self.schedules.validate(schedule)
        except Exception as error:
            raise CampaignOperationsError(str(error), "schedule_invalid") from error
        campaign = self.store.get_campaign(campaign_id)
        if CampaignStatus(campaign["status"]) not in {
            CampaignStatus.DRAFT,
            CampaignStatus.READY,
            CampaignStatus.SCHEDULED,
            CampaignStatus.PAUSED,
        }:
            raise CampaignOperationsError(
                "schedule can only be changed before or between runs", "schedule_state"
            )
        self.store.upsert_schedule(campaign_id, schedule)
        next_at = self.schedules.next_eligible(schedule, self.outbound.now_fn())
        self.store.set_campaign_scheduled_at(campaign_id, next_at.isoformat() if next_at else None)
        current = CampaignStatus(self.store.get_campaign(campaign_id)["status"])
        if schedule.mode is not ScheduleMode.IMMEDIATE and current is CampaignStatus.READY:
            self.store.transition_campaign(campaign_id, CampaignStatus.SCHEDULED, "schedule_saved")
        return {
            "campaign_id": campaign_id,
            "schedule": asdict(schedule),
            "next_eligible_at": next_at.isoformat() if next_at else None,
        }

    def set_execution_mode(self, campaign_id: str, mode: str) -> dict[str, Any]:
        if mode not in {"synthetic", "loopback_smtp"}:
            raise CampaignOperationsError("unsupported execution mode", "execution_mode_invalid")
        campaign = self.store.get_campaign(campaign_id)
        status = CampaignStatus(campaign["status"])
        if status not in {
            CampaignStatus.READY,
            CampaignStatus.SCHEDULED,
            CampaignStatus.PAUSED,
        }:
            raise CampaignOperationsError(
                "execution mode can only be selected before or between runs",
                "execution_mode_state",
            )
        if mode == "loopback_smtp":
            provider_ref = campaign["provider_account_ref"]
            if not provider_ref:
                raise CampaignOperationsError(
                    "loopback SMTP execution requires a selected provider account",
                    "provider_account_required",
                )
            if not self.provider_operations.is_capture_only_sandbox(provider_ref):
                raise CampaignOperationsError(
                    "campaign SMTP execution is limited to a trusted local capture fixture",
                    "loopback_fixture_required",
                )
        self.store.set_campaign_execution_mode(campaign_id, mode)
        return {"campaign_id": campaign_id, "execution_mode": mode}

    def external_authorization_preview(
        self, campaign_id: str, *, max_recipients: int = 3
    ) -> dict[str, Any]:
        campaign = self.store.get_campaign(campaign_id)
        provider_ref = campaign["provider_account_ref"]
        if not provider_ref:
            raise CampaignOperationsError(
                "external campaign authorization requires a provider account",
                "provider_account_required",
            )
        account = self.provider_operations.account(provider_ref)
        if account.get("sandbox") or account.get("host", "").lower() in {
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            raise CampaignOperationsError(
                "external campaign authorization cannot use a local capture fixture",
                "external_provider_required",
            )
        intents = self.store.list_intents(campaign_id)
        if not intents or any(
            item["state"] not in {"prepared", "eligible_pending"} for item in intents
        ):
            raise CampaignOperationsError(
                "prepare a campaign with immutable recipient and message snapshots first",
                "campaign_not_prepared",
            )
        if len(intents) > max_recipients:
            raise CampaignOperationsError(
                f"external qualification is limited to {max_recipients} recipients",
                "recipient_limit_exceeded",
            )
        recipients = [item["normalized_email"] for item in intents]
        prepared_values = []
        for intent in intents:
            prepared = self.store.get_prepared_message(intent["id"])
            if prepared is None:
                raise CampaignOperationsError(
                    "prepared MIME artifact is missing", "prepared_artifact_missing"
                )
            if len(bytes(prepared["mime_bytes"])) > 1_048_576:
                raise CampaignOperationsError(
                    "prepared message exceeds the 1 MiB external qualification limit",
                    "message_limit_exceeded",
                )
            prepared_values.append(
                f"{intent['id']}:{prepared['content_hash']}:{prepared['proof_id'] or ''}"
            )
        return {
            "campaign_id": campaign_id,
            "provider": {
                "ref": provider_ref,
                "host": account["host"],
                "port": account["port"],
                "tls_mode": account["tls_mode"],
                "credential_ref": account["credential_ref"],
                "profile_fingerprint": self.provider_operations.profile_fingerprint(provider_ref),
            },
            "sender": json.loads(campaign["configuration_json"] or "{}").get(
                "sender_email", account.get("sender_email")
            ),
            "recipients": recipients,
            "recipient_set_hash": _digest_values(recipients),
            "prepared_set_hash": _digest_values(prepared_values),
            "max_submissions": len(recipients),
            "message_limit_bytes": min(
                int(account.get("max_message_bytes") or 1_048_576), 1_048_576
            ),
            "external_access": False,
            "stop_conditions": [
                "authorization expiry",
                "provider configuration drift",
                "recipient eligibility or suppression change",
                "ambiguous SMTP outcome",
                "submission budget exhausted",
            ],
        }

    def bind_external_authorization(
        self,
        campaign_id: str,
        *,
        authorization_ref: str,
        purpose: str,
        expires_at: str,
        recipient_set_hash: str,
        prepared_set_hash: str,
    ) -> dict[str, Any]:
        preview = self.external_authorization_preview(campaign_id)
        if (
            preview["recipient_set_hash"] != recipient_set_hash
            or preview["prepared_set_hash"] != prepared_set_hash
        ):
            raise CampaignOperationsError(
                "authorization does not match the prepared campaign identity",
                "campaign_identity_mismatch",
            )
        self.provider_operations.validate_campaign_grant(
            preview["provider"]["ref"],
            authorization_ref=authorization_ref,
            purpose=purpose,
            expires_at=expires_at,
            prepared_set_hash=prepared_set_hash,
        )
        configuration = json.loads(
            self.store.get_campaign(campaign_id)["configuration_json"] or "{}"
        )
        configuration.update(
            {
                "execution_mode": "external_smtp",
                "external_authorization_ref": authorization_ref,
                "external_authorization_purpose": purpose,
                "external_authorization_expires_at": expires_at,
                "external_recipient_set_hash": recipient_set_hash,
                "external_prepared_set_hash": prepared_set_hash,
            }
        )
        self.store.set_campaign_execution_mode(campaign_id, "external_smtp")
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE campaigns SET configuration_json = ?, updated_at = ? WHERE id = ?",
                (
                    json.dumps(configuration, sort_keys=True),
                    datetime.now(UTC).isoformat(),
                    campaign_id,
                ),
            )
        return {"campaign_id": campaign_id, "authorized": True, "external_access": False}

    def prepare_external_authorization(
        self,
        campaign_id: str,
        *,
        purpose: str,
        expires_at: str,
        max_submissions: int,
        prepared_set_hash: str,
        confirm_exact_scope: bool,
    ) -> dict[str, Any]:
        """Issue and bind the server grant for an explicitly confirmed scope."""

        if not confirm_exact_scope:
            raise CampaignOperationsError(
                "confirm the exact prepared recipients and submission scope",
                "scope_confirmation_required",
            )
        preview = self.external_authorization_preview(campaign_id)
        if max_submissions != len(preview["recipients"]):
            raise CampaignOperationsError(
                "submission scope must match the prepared recipient count",
                "submission_scope_mismatch",
            )
        provider_ref = preview["provider"]["ref"]
        grant = self.provider_operations.issue_campaign_grant(
            provider_ref,
            purpose=purpose,
            expires_at=expires_at,
            max_submissions=max_submissions,
            prepared_set_hash=prepared_set_hash,
        )
        self.authorize(campaign_id)
        bound = self.bind_external_authorization(
            campaign_id,
            authorization_ref=str(grant["authorization_ref"]),
            purpose=purpose,
            expires_at=expires_at,
            recipient_set_hash=str(preview["recipient_set_hash"]),
            prepared_set_hash=prepared_set_hash,
        )
        return {
            **bound,
            "scope": {
                "recipients": len(preview["recipients"]),
                "max_submissions": max_submissions,
                "expires_at": expires_at,
            },
        }

    def control(self, campaign_id: str, action: str) -> dict[str, Any]:
        if action == "pause":
            status = self.store.get_campaign(campaign_id)["status"]
            if status == CampaignStatus.PAUSED.value:
                # A temporary-restriction pause is resumable by the scheduler;
                # an operator pause must durably opt out of that continuation.
                self.store.request_pause(campaign_id)
            elif status != CampaignStatus.PAUSING.value:
                self.campaigns.pause(campaign_id)
            return {"campaign_id": campaign_id, "action": action, "idempotent": True}
        if action == "cancel":
            campaign = self.store.get_campaign(campaign_id)
            if campaign["status"] != CampaignStatus.CANCELED.value:
                self.campaigns.cancel(campaign_id)
            return {"campaign_id": campaign_id, "action": action, "idempotent": True}
        if action == "resume":
            campaign = self.store.get_campaign(campaign_id)
            if campaign["authorization"] != "simulation_authorized":
                raise CampaignOperationsError(
                    "authorize the campaign before resuming", "simulation_not_authorized"
                )
            self.store.clear_pause_request(campaign_id)
            configuration = json.loads(campaign["configuration_json"] or "{}")
            started = (
                False
                if campaign["status"] in {"completed", "canceled"}
                else self.worker_start(campaign_id)
            )
            return {
                "campaign_id": campaign_id,
                "action": action,
                "started": started,
                "mode": configuration.get("execution_mode", "synthetic"),
            }
        raise CampaignOperationsError("unsupported campaign action", "action_invalid")

    def authorize(self, campaign_id: str) -> dict[str, Any]:
        self.outbound.authorize_simulation(campaign_id)
        return {"campaign_id": campaign_id, "authorized": True, "mode": "simulation"}

    def workspace(self, campaign_id: str, *, preview_limit: int = 10) -> dict[str, Any]:
        campaign = self.store.get_campaign(campaign_id)
        schedule = self.store.get_schedule(campaign_id)
        audience = self.segmentation.get_audience(campaign_id) or {"status": "not_prepared"}
        provider = None
        if campaign["provider_account_ref"]:
            try:
                provider = _safe_provider(
                    self.provider_operations.account(campaign["provider_account_ref"])
                )
            except KeyError:
                provider = {"ref": campaign["provider_account_ref"], "status": "missing"}
        return {
            "campaign": {
                **{
                    key: campaign[key]
                    for key in (
                        "id",
                        "name",
                        "description",
                        "status",
                        "authorization",
                        "updated_at",
                        "created_at",
                        "audience_source_type",
                        "audience_source_id",
                        "audience_revision_id",
                        "message_revision_id",
                        "provider_account_ref",
                    )
                },
                "configuration": _safe_config(campaign),
                "worker_active": self.worker_active(campaign_id),
            },
            "audience": audience,
            "provider": provider,
            "schedule": asdict(self._schedule_from_row(schedule)) if schedule else None,
            "preflight": self.preflight(campaign_id),
            "progress": self.campaigns.report(campaign_id).to_dict(),
            "execution": {
                "mode": _safe_config(campaign).get("execution_mode", "synthetic"),
                "next_automatic_at": self.store.campaign_next_automatic_at(campaign_id),
                "last_transition": (
                    dict(self.store.campaign_last_transition(campaign_id))
                    if self.store.campaign_last_transition(campaign_id)
                    else None
                ),
                "state": _execution_state(
                    campaign,
                    self.campaigns.report(campaign_id),
                    self.store.campaign_last_transition(campaign_id),
                    self.store.campaign_next_automatic_at(campaign_id),
                ),
            },
            "preview": self.recipients(campaign_id, limit=preview_limit),
            "message_previews": self._message_previews(campaign_id, preview_limit),
        }

    def _message_previews(self, campaign_id: str, limit: int) -> list[dict[str, Any]]:
        campaign = self.store.get_campaign(campaign_id)
        rows = self.recipients(campaign_id, limit=max(1, min(limit, 10)))
        previews = []
        for item in rows:
            try:
                if campaign["message_revision_id"]:
                    contact = self.store.contact_by_email(item["email"])
                    if contact:
                        previews.append(
                            self.message_studio.preview(
                                revision_id=campaign["message_revision_id"],
                                contact_id=contact["id"],
                            )
                        )
                else:
                    previews.append(self.outbound.preview_message(campaign_id, item["email"]))
            except KeyError, ValueError:
                continue
        return previews

    def recipients(
        self, campaign_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(limit, 500))
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM delivery_intents WHERE campaign_id=? "
                "ORDER BY normalized_email LIMIT ? OFFSET ?",
                (campaign_id, safe_limit, max(0, offset)),
            ).fetchall()
        result = []
        for row in rows:
            latest = self.store.latest_attempt(row["id"])
            result.append(
                {
                    "id": row["id"],
                    "email": row["normalized_email"],
                    "state": row["state"],
                    "eligibility_reason": row["eligibility_reason"],
                    "attempt_count": row["attempt_count"],
                    "latest_outcome": latest["outcome"] if latest else None,
                    "latest_attempt": {
                        key: latest[key]
                        for key in (
                            "id",
                            "outcome",
                            "attempt_state",
                            "transport_phase",
                            "error_code",
                            "created_at",
                        )
                        if key in latest.keys()
                    }
                    if latest
                    else None,
                }
            )
        return result

    def attempts(
        self, campaign_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT a.*, i.normalized_email AS recipient "
                "FROM delivery_attempts a JOIN delivery_intents i "
                "ON i.id=a.intent_id WHERE i.campaign_id=? "
                "ORDER BY a.created_at DESC,a.id LIMIT ? OFFSET ?",
                (campaign_id, max(1, min(limit, 500)), max(0, offset)),
            ).fetchall()
        return [dict(row) for row in rows]

    def export(self, campaign_id: str, *, fmt: str = "json") -> tuple[str, str, str]:
        payload = {
            "campaign": self.workspace(campaign_id)["campaign"],
            "recipients": self.recipients(campaign_id, limit=5000),
            "attempts": self.attempts(campaign_id, limit=5000),
            "report": self.campaigns.report(campaign_id).to_dict(),
        }
        if fmt == "json":
            return (
                json.dumps(payload, sort_keys=True, default=str),
                "application/json",
                f"campaign-{campaign_id}.json",
            )
        if fmt != "csv":
            raise CampaignOperationsError("format must be json or csv", "export_format_invalid")
        output = io.StringIO()
        writer = csv.DictWriter(
            output,
            fieldnames=(
                "campaign_id",
                "definition_version",
                "evidence_class",
                "email",
                "state",
                "eligibility_reason",
                "attempt_count",
                "latest_outcome",
            ),
        )
        writer.writeheader()
        for item in payload["recipients"]:
            values = [
                campaign_id,
                "campaign-operations-023",
                "local_durable_observation",
                *[str(item.get(key, "")) for key in writer.fieldnames[3:]],
            ]
            values = [
                "'" + value if value.startswith(("=", "+", "-", "@")) else value for value in values
            ]
            writer.writerow(dict(zip(writer.fieldnames, values, strict=False)))
        return output.getvalue(), "text/csv", f"campaign-{campaign_id}.csv"

    @staticmethod
    def _schedule_from_row(row: Any) -> ScheduleSpec:
        if row is None:
            return ScheduleSpec()
        weekdays = json.loads(row["weekdays_json"] or "[]")
        return ScheduleSpec(
            mode=ScheduleMode(row["mode"]),
            timezone=row["timezone"],
            scheduled_at=row["scheduled_at"],
            window_start=row["window_start"],
            window_end=row["window_end"],
            weekdays=tuple(weekdays),
            pacing_seconds=float(row["pacing_seconds"]),
            cooldown_seconds=float(row["cooldown_seconds"]),
            recurrence=row["recurrence"],
            catch_up_policy=CatchUpPolicy(row["catch_up_policy"]),
            fold=row["fold"],
        )
