"""Campaign workspace orchestration over Mailforge's existing authorities."""

from __future__ import annotations

import csv
import io
import json
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from mailforge.application.eligibility import EligibilityService
from mailforge.application.reporting import ReportFilters, ReportingService
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
            "unsubscribe_endpoint_ref",
            "one_click_unsubscribe",
            "dkim_signed",
        )
        if key in configuration
    }


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
        reporting: ReportingService,
        email_standards=None,
        worker_active: Callable[[str], bool] | None = None,
        worker_start: Callable[[str], bool] | None = None,
    ):
        self.store = store
        self.campaigns = campaigns
        self.outbound = outbound
        self.message_studio = message_studio
        self.segmentation = segmentation
        self.provider_operations = provider_operations
        self.reporting = reporting
        self.email_standards = email_standards
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
        if provider_account_ref:
            try:
                self.provider_operations.account(provider_account_ref)
            except KeyError as error:
                raise CampaignOperationsError(str(error), "provider_account_not_found") from error
        sender_ref = f"campaign-{uuid.uuid4().hex[:16]}"
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
        """Return standards-aware MIME options for every preparation entrypoint."""

        config = _safe_config(campaign)
        unsubscribe_url_callback = None
        endpoint_ref = config.get("unsubscribe_endpoint_ref")
        list_id = campaign["audience_list_id"]
        if self.email_standards is not None and endpoint_ref and list_id:

            def unsubscribe_url_callback(contact):
                keys = contact.keys() if hasattr(contact, "keys") else contact
                email = (
                    contact["normalized_email"]
                    if "normalized_email" in keys
                    else contact["email"]
                    if "email" in keys
                    else None
                )
                return self.email_standards.create_unsubscribe_url(
                    str(endpoint_ref), str(list_id), str(email)
                )

        return {
            "unsubscribe_url_callback": unsubscribe_url_callback,
            # The local build has no DKIM signer for final prepared bytes.
            # Keep campaign preparation to List-Unsubscribe until a real
            # signing boundary is supplied.
            "one_click_unsubscribe": False,
            "dkim_signed": False,
        }

    def preflight(self, campaign_id: str) -> dict[str, Any]:
        campaign = self.store.get_campaign(campaign_id)
        findings: list[PreflightFinding] = []
        source = campaign["audience_source_id"]
        try:
            if campaign["audience_source_type"] == "segment":
                self.segmentation.get_segment(
                    campaign["audience_source_id"], campaign["audience_revision_id"]
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
        if self.email_standards is not None:
            standards = self.email_standards.preflight_campaign(campaign)
            for finding in standards["findings"]:
                severity = {
                    "blocked": "blocker",
                    "warning": "warning",
                    "pass": "pass",
                    "unknown": "not_verified",
                }.get(finding["severity"], "not_verified")
                findings.append(
                    PreflightFinding(
                        "standards",
                        severity,
                        finding["code"],
                        finding["message"],
                        finding["source"],
                        finding.get("action") or None,
                    )
                )
            if _safe_config(campaign).get("one_click_unsubscribe"):
                findings.append(
                    PreflightFinding(
                        "standards",
                        "blocker",
                        "dkim_signing_unavailable",
                        "Campaign preparation cannot emit RFC 8058 one-click headers "
                        "without a real DKIM signing boundary.",
                        "MessageComposer / provider signing",
                        "use List-Unsubscribe only or provide a verified signing integration",
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

    def control(self, campaign_id: str, action: str) -> dict[str, Any]:
        if action == "pause":
            status = self.store.get_campaign(campaign_id)["status"]
            if status not in {CampaignStatus.PAUSED.value, CampaignStatus.PAUSING.value}:
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
            started = (
                False
                if campaign["status"] in {"completed", "canceled"}
                else self.worker_start(campaign_id)
            )
            return {
                "campaign_id": campaign_id,
                "action": action,
                "started": started,
                "mode": "synthetic",
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
                "SELECT a.* FROM delivery_attempts a JOIN delivery_intents i "
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
            "report": self.reporting.campaign_detail(
                campaign_id, ReportFilters(campaign_id=campaign_id)
            ),
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
