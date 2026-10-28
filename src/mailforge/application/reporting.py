"""Canonical, read-only operational reporting over the Mailforge authority."""

# Report SQL and registry descriptions stay contiguous so the metric grain and
# source evidence remain easy to audit beside the query that uses them.
# ruff: noqa: E501

from __future__ import annotations

import csv
import html
import io
import json
import math
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mailforge.infrastructure.campaign_store import CampaignStore

REPORT_DEFINITION_VERSION = "022.1"
MAX_RANGE_DAYS = 366
MAX_LIMIT = 200
MAX_OFFSET = 1_000_000
MAX_EXPORT_ROWS = 5_000


class ReportingError(ValueError):
    """A bounded report request or source-evidence error."""

    def __init__(self, message: str, code: str = "report_invalid") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class MetricDefinition:
    key: str
    label: str
    meaning: str
    source_records: tuple[str, ...]
    time_field: str
    grain: str
    numerator: str | None
    denominator: str | None
    filters: str
    evidence_class: str
    missing_data_behavior: str
    unit: str
    deduplication: str
    origin: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "meaning": self.meaning,
            "source_records": list(self.source_records),
            "time_field": self.time_field,
            "grain": self.grain,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "filters": self.filters,
            "evidence_class": self.evidence_class,
            "missing_data_behavior": self.missing_data_behavior,
            "unit": self.unit,
            "deduplication": self.deduplication,
            "origin": self.origin,
        }


def _definition(
    key: str,
    label: str,
    meaning: str,
    source_records: tuple[str, ...],
    time_field: str,
    grain: str,
    *,
    numerator: str | None = None,
    denominator: str | None = None,
    filters: str = "report range and explicit filters",
    evidence_class: str = "durable_local_observation",
    missing_data_behavior: str = "report not_measured when no authoritative source exists",
    unit: str = "count",
    deduplication: str = "one source row per declared grain",
    origin: str = "local_durable_record",
) -> MetricDefinition:
    return MetricDefinition(
        key,
        label,
        meaning,
        source_records,
        time_field,
        grain,
        numerator,
        denominator,
        filters,
        evidence_class,
        missing_data_behavior,
        unit,
        deduplication,
        origin,
    )


METRIC_DEFINITIONS: tuple[MetricDefinition, ...] = (
    _definition(
        "campaign.count",
        "Campaigns",
        "Campaigns created in the range",
        ("campaigns",),
        "created_at",
        "campaign",
    ),
    _definition(
        "recipient.intent",
        "Prepared recipient intents",
        "Distinct recipient intents prepared for a campaign",
        ("delivery_intents",),
        "prepared_at",
        "campaign/contact",
    ),
    _definition(
        "recipient.state",
        "Recipient eligibility states",
        "Current durable state of each recipient intent",
        ("delivery_intents",),
        "prepared_at",
        "campaign/contact",
    ),
    _definition(
        "attempt.count",
        "Submission attempts",
        "One row per recorded outbound attempt",
        ("delivery_attempts",),
        "created_at",
        "attempt",
    ),
    _definition(
        "attempt.accepted",
        "Accepted submissions",
        "Attempts accepted by the configured transport",
        ("delivery_attempts",),
        "created_at",
        "attempt",
        evidence_class="transport_observation",
    ),
    _definition(
        "attempt.ambiguous",
        "Ambiguous submissions",
        "Attempts whose final external outcome is unresolved",
        ("delivery_attempts",),
        "created_at",
        "attempt",
        evidence_class="transport_observation",
    ),
    _definition(
        "provider.diagnostic",
        "Provider diagnostics",
        "Recorded account diagnostic observations",
        ("provider_diagnostics",),
        "observed_at",
        "diagnostic",
    ),
    _definition(
        "inbound.message",
        "Inbound messages",
        "Messages acquired by an inbound source",
        ("inbound_messages",),
        "acquired_at",
        "message",
    ),
    _definition(
        "inbound.review",
        "Pending inbound reviews",
        "Reviews currently awaiting an operator decision",
        ("inbound_reviews",),
        "created_at",
        "review",
    ),
    _definition(
        "workflow.run",
        "Workflow runs",
        "Durable workflow run records",
        ("automation_runs",),
        "created_at",
        "run",
    ),
    _definition(
        "workflow.step_failure",
        "Workflow step failures",
        "Recorded failed workflow steps",
        ("automation_step_history",),
        "started_at",
        "step",
    ),
    _definition(
        "list.job",
        "List Tools jobs",
        "List Tools processing executions",
        ("list_processing_jobs",),
        "created_at",
        "job",
    ),
    _definition(
        "data.job",
        "Data Studio jobs",
        "Data Studio processing executions",
        ("data_studio_jobs",),
        "created_at",
        "job",
    ),
    _definition(
        "message.revision",
        "Message revisions",
        "Message revisions created or published",
        ("message_revisions",),
        "created_at",
        "revision",
    ),
)
METRIC_REGISTRY = {item.key: item for item in METRIC_DEFINITIONS}


@dataclass(frozen=True)
class ReportRange:
    preset: str
    timezone: str
    start_utc: datetime
    end_utc: datetime
    start_local: datetime
    end_local: datetime

    @property
    def duration(self) -> timedelta:
        return self.end_utc - self.start_utc

    def to_dict(self) -> dict[str, str]:
        return {
            "preset": self.preset,
            "timezone": self.timezone,
            "start": self.start_local.isoformat(),
            "end": self.end_local.isoformat(),
            "start_utc": self.start_utc.isoformat(),
            "end_utc": self.end_utc.isoformat(),
        }


@dataclass(frozen=True)
class ReportFilters:
    preset: str = "last_7_days"
    start: str | None = None
    end: str | None = None
    timezone: str = "UTC"
    provider_ref: str | None = None
    campaign_id: str | None = None
    workflow_id: str | None = None
    source_ref: str | None = None
    account_ref: str | None = None
    folder: str | None = None
    job_kind: str | None = None
    limit: int = 50
    offset: int = 0

    def resolve(self, now_fn: Callable[[], datetime]) -> ReportRange:
        try:
            zone = ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as error:
            raise ReportingError(
                "timezone must be a valid IANA timezone", "timezone_invalid"
            ) from error
        now = now_fn().astimezone(zone)
        if self.preset not in {"today", "yesterday", "last_7_days", "last_30_days", "custom"}:
            raise ReportingError("unsupported report range preset", "range_invalid")
        if self.preset == "custom":
            if not self.start or not self.end:
                raise ReportingError("custom reports require start and end", "range_invalid")
            start_local = _parse_local(self.start, zone)
            end_local = _parse_local(self.end, zone)
        else:
            day = now.replace(hour=0, minute=0, second=0, microsecond=0)
            if self.preset == "today":
                start_local, end_local = day, now
            elif self.preset == "yesterday":
                start_local, end_local = day - timedelta(days=1), day
            else:
                days = 7 if self.preset == "last_7_days" else 30
                start_local, end_local = day - timedelta(days=days - 1), now
        start_utc = start_local.astimezone(UTC)
        end_utc = end_local.astimezone(UTC)
        if end_utc <= start_utc:
            raise ReportingError("report end must be after report start", "range_invalid")
        if end_utc - start_utc > timedelta(days=MAX_RANGE_DAYS):
            raise ReportingError("report ranges are limited to 366 days", "range_too_large")
        if self.limit < 1 or self.limit > MAX_LIMIT or self.offset < 0 or self.offset > MAX_OFFSET:
            raise ReportingError(
                "report pagination is outside the bounded range", "pagination_invalid"
            )
        for value, name in (
            (self.provider_ref, "provider_ref"),
            (self.campaign_id, "campaign_id"),
            (self.workflow_id, "workflow_id"),
            (self.source_ref, "source_ref"),
            (self.account_ref, "account_ref"),
            (self.folder, "folder"),
        ):
            if value is not None and (not value or len(value) > 160):
                raise ReportingError(f"{name} is invalid", "filter_invalid")
        if self.job_kind not in {None, "data_studio", "list_tools"}:
            raise ReportingError("job_kind is invalid", "filter_invalid")
        return ReportRange(self.preset, self.timezone, start_utc, end_utc, start_local, end_local)

    def to_dict(self) -> dict[str, Any]:
        return {
            "preset": self.preset,
            "start": self.start,
            "end": self.end,
            "timezone": self.timezone,
            "provider_ref": self.provider_ref,
            "campaign_id": self.campaign_id,
            "workflow_id": self.workflow_id,
            "source_ref": self.source_ref,
            "account_ref": self.account_ref,
            "folder": self.folder,
            "job_kind": self.job_kind,
            "limit": self.limit,
            "offset": self.offset,
        }


def _parse_local(value: str, zone: ZoneInfo) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ReportingError("report dates must be ISO-8601", "range_invalid") from error
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=zone)
    return parsed.astimezone(zone)


def _iso(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return value


def _safe_number(value: Any) -> int | float:
    if value is None:
        return 0
    result = float(value)
    if not math.isfinite(result):
        return 0
    return int(result) if result.is_integer() else round(result, 3)


class ReportingService:
    """Single metric authority shared by the web API, CLI, and browser."""

    def __init__(self, store: CampaignStore, *, now_fn: Callable[[], datetime] | None = None):
        self.store = store
        self.now_fn = now_fn or (lambda: datetime.now(UTC))

    def definitions(self) -> dict[str, Any]:
        return {
            "version": REPORT_DEFINITION_VERSION,
            "items": [item.to_dict() for item in METRIC_DEFINITIONS],
        }

    def summary(
        self, filters: ReportFilters | None = None, *, include_comparison: bool = True
    ) -> dict[str, Any]:
        filters = filters or ReportFilters()
        report_range = filters.resolve(self.now_fn)
        metrics = self._metrics(filters, report_range)
        previous = None
        if include_comparison:
            duration = report_range.duration
            previous_range = ReportRange(
                "previous_period",
                report_range.timezone,
                report_range.start_utc - duration,
                report_range.start_utc,
                report_range.start_local - duration,
                report_range.start_local,
            )
            previous = self._metrics(filters, previous_range)
        return self._envelope(
            "summary", filters, report_range, {"metrics": metrics, "previous": previous}
        )

    def campaigns(self, filters: ReportFilters | None = None) -> dict[str, Any]:
        filters = filters or ReportFilters()
        report_range = filters.resolve(self.now_fn)
        where, params = self._campaign_filter("c", filters, report_range, "c.created_at")
        with self.store.connect() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM campaigns c WHERE {where}", params
            ).fetchone()[0]
            rows = connection.execute(
                f"SELECT c.id,c.name,c.status,c.authorization,c.provider_account_ref,c.created_at,c.started_at,c.completed_at,"
                "(SELECT COUNT(*) FROM delivery_intents i WHERE i.campaign_id=c.id AND i.prepared_at>=? AND i.prepared_at<?) AS recipient_count,"
                "(SELECT COUNT(*) FROM delivery_attempts a JOIN delivery_intents i ON i.id=a.intent_id WHERE i.campaign_id=c.id AND a.created_at>=? AND a.created_at<?) AS attempt_count,"
                "(SELECT COUNT(*) FROM delivery_attempts a JOIN delivery_intents i ON i.id=a.intent_id WHERE i.campaign_id=c.id AND a.outcome='accepted' AND a.created_at>=? AND a.created_at<?) AS accepted_count "
                f"FROM campaigns c WHERE {where} ORDER BY c.created_at DESC,c.id LIMIT ? OFFSET ?",
                (
                    report_range.start_utc.isoformat(),
                    report_range.end_utc.isoformat(),
                    report_range.start_utc.isoformat(),
                    report_range.end_utc.isoformat(),
                    report_range.start_utc.isoformat(),
                    report_range.end_utc.isoformat(),
                    *params,
                    filters.limit,
                    filters.offset,
                ),
            ).fetchall()
        return self._envelope(
            "campaigns",
            filters,
            report_range,
            {"items": [dict(row) for row in rows], "total": total},
        )

    def campaign_detail(
        self, campaign_id: str, filters: ReportFilters | None = None
    ) -> dict[str, Any]:
        filters = filters or ReportFilters(campaign_id=campaign_id)
        filters = ReportFilters(**{**filters.to_dict(), "campaign_id": campaign_id})
        report_range = filters.resolve(self.now_fn)
        with self.store.connect() as connection:
            campaign = connection.execute(
                "SELECT id,name,description,status,authorization,provider_account_ref,"
                "created_at,started_at,completed_at,failure_reason FROM campaigns WHERE id=?",
                (campaign_id,),
            ).fetchone()
            if campaign is None:
                raise ReportingError("campaign was not found", "not_found")
            intents = connection.execute(
                "SELECT i.id,i.normalized_email,i.state,i.eligibility_reason,i.attempt_count,i.prepared_at,"
                "(SELECT COUNT(*) FROM delivery_attempts a WHERE a.intent_id=i.id AND a.created_at>=? AND a.created_at<?) AS recorded_attempts,"
                "(SELECT a.outcome FROM delivery_attempts a WHERE a.intent_id=i.id AND a.created_at>=? AND a.created_at<? ORDER BY a.attempt_number DESC LIMIT 1) AS latest_outcome "
                "FROM delivery_intents i WHERE i.campaign_id=? AND i.prepared_at>=? AND i.prepared_at<? "
                "ORDER BY i.normalized_email LIMIT ? OFFSET ?",
                (
                    report_range.start_utc.isoformat(),
                    report_range.end_utc.isoformat(),
                    report_range.start_utc.isoformat(),
                    report_range.end_utc.isoformat(),
                    campaign_id,
                    report_range.start_utc.isoformat(),
                    report_range.end_utc.isoformat(),
                    filters.limit,
                    filters.offset,
                ),
            ).fetchall()
            attempts = connection.execute(
                "SELECT a.id,a.intent_id,a.attempt_number,a.attempt_state,a.outcome,a.error_code,a.transport_phase,a.response_code,a.created_at,a.started_at,a.finished_at "
                "FROM delivery_attempts a JOIN delivery_intents i ON i.id=a.intent_id WHERE i.campaign_id=? AND a.created_at>=? AND a.created_at<? "
                "ORDER BY a.created_at DESC,a.id LIMIT ? OFFSET ?",
                (
                    campaign_id,
                    report_range.start_utc.isoformat(),
                    report_range.end_utc.isoformat(),
                    filters.limit,
                    filters.offset,
                ),
            ).fetchall()
            transitions = connection.execute(
                "SELECT from_status,to_status,reason,occurred_at FROM campaign_transitions WHERE campaign_id=? AND occurred_at>=? AND occurred_at<? ORDER BY occurred_at DESC LIMIT ? OFFSET ?",
                (
                    campaign_id,
                    report_range.start_utc.isoformat(),
                    report_range.end_utc.isoformat(),
                    filters.limit,
                    filters.offset,
                ),
            ).fetchall()
        payload = {
            "campaign": dict(campaign),
            "recipients": [dict(row) for row in intents],
            "attempts": [dict(row) for row in attempts],
            "transitions": [dict(row) for row in transitions],
        }
        return self._envelope("campaign_detail", filters, report_range, payload)

    def providers(self, filters: ReportFilters | None = None) -> dict[str, Any]:
        filters = filters or ReportFilters()
        report_range = filters.resolve(self.now_fn)
        ref_clause = " AND pa.ref=?" if filters.provider_ref else ""
        ref_params = (filters.provider_ref,) if filters.provider_ref else ()
        with self.store.connect() as connection:
            accounts = connection.execute(
                "SELECT pa.ref,pa.display_name,pa.status,pa.outbound_enabled,pa.inbound_enabled,pp.kind,pp.host,pp.port "
                "FROM provider_accounts pa JOIN provider_profiles pp ON pp.ref=pa.ref WHERE 1=1"
                + ref_clause
                + " ORDER BY pa.ref LIMIT ? OFFSET ?",
                (*ref_params, filters.limit, filters.offset),
            ).fetchall()
            diagnostics = connection.execute(
                "SELECT profile_ref,status,phase,external_access,observed_at,operation,detail FROM provider_diagnostics "
                "WHERE observed_at>=? AND observed_at<?"
                + (" AND profile_ref=?" if filters.provider_ref else "")
                + " ORDER BY observed_at DESC LIMIT ? OFFSET ?",
                (
                    report_range.start_utc.isoformat(),
                    report_range.end_utc.isoformat(),
                    *ref_params,
                    filters.limit,
                    filters.offset,
                ),
            ).fetchall()
            tests = connection.execute(
                "SELECT account_ref,status,phase,response_code,external_access,created_at FROM provider_test_submissions "
                "WHERE created_at>=? AND created_at<?"
                + (" AND account_ref=?" if filters.provider_ref else "")
                + " ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (
                    report_range.start_utc.isoformat(),
                    report_range.end_utc.isoformat(),
                    *ref_params,
                    filters.limit,
                    filters.offset,
                ),
            ).fetchall()
        return self._envelope(
            "providers",
            filters,
            report_range,
            {
                "accounts": [dict(row) for row in accounts],
                "diagnostics": [dict(row) for row in diagnostics],
                "test_submissions": [dict(row) for row in tests],
            },
        )

    def workflows(self, filters: ReportFilters | None = None) -> dict[str, Any]:
        filters = filters or ReportFilters()
        report_range = filters.resolve(self.now_fn)
        condition = "created_at>=? AND created_at<?"
        params: list[Any] = [report_range.start_utc.isoformat(), report_range.end_utc.isoformat()]
        if filters.workflow_id:
            condition += " AND workflow_id=?"
            params.append(filters.workflow_id)
        with self.store.connect() as connection:
            runs = connection.execute(
                f"SELECT id,workflow_id,revision_id,status,current_node_key,retry_count,waiting_since,due_at_utc,started_at,completed_at,created_at,updated_at,failure_reason FROM automation_runs WHERE {condition} ORDER BY created_at DESC,id LIMIT ? OFFSET ?",
                (*params, filters.limit, filters.offset),
            ).fetchall()
            step_failures = connection.execute(
                "SELECT COUNT(*) FROM automation_step_history WHERE status='failed' AND started_at>=? AND started_at<?",
                (report_range.start_utc.isoformat(), report_range.end_utc.isoformat()),
            ).fetchone()[0]
            workflows = connection.execute(
                "SELECT id,name,status,updated_at FROM automation_workflows ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (filters.limit, filters.offset),
            ).fetchall()
        return self._envelope(
            "workflows",
            filters,
            report_range,
            {
                "workflows": [dict(row) for row in workflows],
                "runs": [dict(row) for row in runs],
                "step_failures": step_failures,
            },
        )

    def inbound(self, filters: ReportFilters | None = None) -> dict[str, Any]:
        filters = filters or ReportFilters()
        report_range = filters.resolve(self.now_fn)
        message_scope, scope_params = self._inbound_scope("m", filters)
        run_scope, run_params = self._inbound_scope("r", filters, source_only=True)
        if filters.account_ref or filters.folder:
            run_scope, run_params = "0", ()
        start, end = report_range.start_utc.isoformat(), report_range.end_utc.isoformat()
        with self.store.connect() as connection:
            messages = connection.execute(
                "SELECT m.id,m.source_ref,m.account_ref,m.folder,c.classification,m.parse_status,m.received_at,m.acquired_at,m.processed_at FROM inbound_messages m "
                "LEFT JOIN (SELECT message_id,kind AS classification FROM inbound_classifications GROUP BY message_id) c ON c.message_id=m.id "
                f"WHERE m.acquired_at>=? AND m.acquired_at<? AND {message_scope} ORDER BY m.acquired_at DESC,m.id LIMIT ? OFFSET ?",
                (start, end, *scope_params, filters.limit, filters.offset),
            ).fetchall()
            classifications = connection.execute(
                f"SELECT c.kind,COUNT(*) AS count FROM inbound_classifications c JOIN inbound_messages m ON m.id=c.message_id WHERE c.occurred_at>=? AND c.occurred_at<? AND {message_scope} GROUP BY c.kind ORDER BY c.kind",
                (start, end, *scope_params),
            ).fetchall()
            trust = connection.execute(
                f"SELECT c.trust_status,COUNT(*) AS count FROM inbound_correlations c JOIN inbound_messages m ON m.id=c.message_id WHERE c.created_at>=? AND c.created_at<? AND {message_scope} GROUP BY c.trust_status ORDER BY c.trust_status",
                (start, end, *scope_params),
            ).fetchall()
            reviews = connection.execute(
                f"SELECT COUNT(*) FROM inbound_reviews r JOIN inbound_actions a ON a.id=r.action_id JOIN inbound_messages m ON m.id=r.message_id WHERE r.status='pending' AND {message_scope}",
                scope_params,
            ).fetchone()[0]
            runs = connection.execute(
                f"SELECT r.id,r.source_ref,r.status,r.started_at,r.finished_at,r.acquired_count,r.processed_count,r.failure_count,r.error_message FROM inbound_runs r WHERE r.started_at>=? AND r.started_at<? AND {run_scope} ORDER BY r.started_at DESC LIMIT ? OFFSET ?",
                (start, end, *run_params, filters.limit, filters.offset),
            ).fetchall()
        return self._envelope(
            "inbound",
            filters,
            report_range,
            {
                "messages": [dict(row) for row in messages],
                "classifications": [dict(row) for row in classifications],
                "trust": [dict(row) for row in trust],
                "pending_reviews": reviews,
                "pending_reviews_semantics": "current_snapshot_for_selected_source_scope",
                "runs": [dict(row) for row in runs],
                "runs_semantics": "not_measured_for_account_or_folder_scope"
                if filters.account_ref or filters.folder
                else "selected_source_scope",
            },
        )

    def jobs(self, filters: ReportFilters | None = None) -> dict[str, Any]:
        filters = filters or ReportFilters()
        report_range = filters.resolve(self.now_fn)
        start, end = report_range.start_utc.isoformat(), report_range.end_utc.isoformat()
        with self.store.connect() as connection:
            job_sources = []
            job_params: list[Any] = []
            if filters.job_kind != "list_tools":
                job_sources.append(
                    "SELECT 'data_studio' AS kind,id,status,processed_count AS processed,"
                    "output_count AS accepted,rejected_count AS rejected,error_count,created_at,"
                    "started_at,completed_at,error_message FROM data_studio_jobs "
                    "WHERE created_at>=? AND created_at<?"
                )
                job_params.extend((start, end))
            if filters.job_kind != "data_studio":
                job_sources.append(
                    "SELECT 'list_tools' AS kind,id,status,processed_count AS processed,"
                    "accepted_count AS accepted,0 AS rejected,0 AS error_count,created_at,"
                    "started_at,completed_at,error_message FROM list_processing_jobs "
                    "WHERE created_at>=? AND created_at<?"
                )
                job_params.extend((start, end))
            rows = (
                connection.execute(
                    "SELECT * FROM ("
                    + " UNION ALL ".join(job_sources)
                    + ") ORDER BY created_at DESC,id LIMIT ? OFFSET ?",
                    (*job_params, filters.limit, filters.offset),
                ).fetchall()
                if job_sources
                else []
            )
            artifact_sources = []
            artifact_params: list[Any] = []
            if filters.job_kind != "list_tools":
                artifact_sources.append(
                    "SELECT 'data_studio' AS source,job_id,id,filename,kind AS artifact_kind,"
                    "size_bytes,sha256,created_at FROM data_studio_artifacts "
                    "WHERE created_at>=? AND created_at<?"
                )
                artifact_params.extend((start, end))
            if filters.job_kind != "data_studio":
                artifact_sources.append(
                    "SELECT 'list_tools' AS source,job_id,id,filename,kind AS artifact_kind,"
                    "size_bytes,sha256,created_at FROM list_processing_artifacts "
                    "WHERE created_at>=? AND created_at<?"
                )
                artifact_params.extend((start, end))
            artifacts = (
                connection.execute(
                    "SELECT * FROM ("
                    + " UNION ALL ".join(artifact_sources)
                    + ") ORDER BY created_at DESC,id LIMIT ? OFFSET ?",
                    (*artifact_params, filters.limit, filters.offset),
                ).fetchall()
                if artifact_sources
                else []
            )
        return self._envelope(
            "jobs",
            filters,
            report_range,
            {"jobs": [dict(row) for row in rows], "artifacts": [dict(row) for row in artifacts]},
        )

    def email_standards(self, *, limit: int = 100) -> dict[str, Any]:
        """Report durable standards observations without creating a second store."""

        limit = max(1, min(int(limit), 500))
        with self.store.connect() as connection:
            runs = connection.execute(
                "SELECT id,kind,subject_ref,status,ruleset_version,findings_json,evidence_json,observed_at "
                "FROM email_standards_diagnostic_runs ORDER BY observed_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            reports = connection.execute(
                "SELECT report_id,source_org,report_domain,period_begin,period_end,source_sha256,imported_at "
                "FROM email_dmarc_aggregate_reports ORDER BY imported_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            identities = connection.execute(
                "SELECT identity_ref,visible_from,reply_to,envelope_from,sending_domain,dkim_domain,"
                "dkim_selector,provider_account_ref,provider_signing_status,updated_at "
                "FROM email_sender_identities ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return {
            "report_definition_version": REPORT_DEFINITION_VERSION,
            "evidence_class": "durable_local_observation",
            "identities": [dict(row) for row in identities],
            "diagnostic_runs": [
                {
                    **dict(row),
                    "findings": json.loads(row["findings_json"]),
                    "evidence": json.loads(row["evidence_json"]),
                }
                for row in runs
            ],
            "dmarc_reports": [dict(row) for row in reports],
        }

    def integration(self, *, limit: int = 100) -> dict[str, Any]:
        """Report integration inventory and durable delivery observations."""

        safe_limit = max(1, min(int(limit), 500))
        with self.store.connect() as connection:
            clients = connection.execute(
                "SELECT client_ref,name,scopes_json,expires_at,revoked_at,last_used_at,created_at FROM integration_clients ORDER BY created_at DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
            sources = connection.execute(
                "SELECT source_type,COUNT(*) AS count FROM data_connections GROUP BY source_type ORDER BY source_type"
            ).fetchall()
            snapshots = connection.execute(
                "SELECT status,COUNT(*) AS count FROM data_connection_snapshots GROUP BY status"
            ).fetchall()
            events = connection.execute(
                "SELECT outcome,COUNT(*) AS count FROM integration_events GROUP BY outcome"
            ).fetchall()
            conflicts = connection.execute(
                "SELECT COUNT(*) FROM integration_event_conflicts"
            ).fetchone()[0]
            subscriptions = connection.execute(
                "SELECT enabled,COUNT(*) AS count FROM integration_webhook_subscriptions GROUP BY enabled"
            ).fetchall()
            deliveries = connection.execute(
                "SELECT status,COUNT(*) AS count FROM integration_webhook_deliveries GROUP BY status"
            ).fetchall()
            last_success = connection.execute(
                "SELECT MAX(last_success_at) FROM integration_webhook_subscriptions"
            ).fetchone()[0]
            errors = connection.execute(
                "SELECT subscription_ref,last_error,updated_at FROM integration_webhook_subscriptions WHERE last_error IS NOT NULL ORDER BY updated_at DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
        return {
            "report_definition_version": REPORT_DEFINITION_VERSION,
            "evidence_class": "durable_local_observation",
            "clients": [
                {**dict(row), "scopes": json.loads(row["scopes_json"])} for row in clients
            ],
            "sources": [dict(row) for row in sources],
            "snapshots": {row["status"]: row["count"] for row in snapshots},
            "events": {row["outcome"]: row["count"] for row in events},
            "event_conflicts": conflicts,
            "subscriptions": {"enabled" if row["enabled"] else "disabled": row["count"] for row in subscriptions},
            "deliveries": {row["status"]: row["count"] for row in deliveries},
            "last_success_at": last_success,
            "errors": [dict(row) for row in errors],
            "limitations": [
                "HTTP 2xx is a receiving transport observation, not downstream business completion",
                "external providers and remote webhook services are not qualified by local fixtures",
            ],
        }

    def export(
        self, report_type: str, filters: ReportFilters | None = None, *, format: str = "json"
    ) -> tuple[bytes, str, str]:
        if format not in {"json", "csv", "html"}:
            raise ReportingError("format must be json, csv, or html", "format_invalid")
        report = self._named_report(report_type, filters)
        if format == "json":
            return (
                json.dumps(report, indent=2, sort_keys=True, default=str).encode(),
                "application/json",
                f"mailforge-{report_type}.json",
            )
        rows = self._flatten_rows(report)
        if len(rows) > MAX_EXPORT_ROWS:
            raise ReportingError("report export exceeds the bounded row limit", "export_too_large")
        if format == "csv":
            output = io.StringIO()
            fields = sorted({key for row in rows for key in row}) or ["report_type"]
            writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(
                {key: _formula_safe(row.get(key, "")) for key in fields} for row in rows
            )
            return (
                output.getvalue().encode(),
                "text/csv; charset=utf-8",
                f"mailforge-{report_type}.csv",
            )
        body = "".join(
            "<tr>"
            + "".join(f"<td>{html.escape(str(row.get(field, '')))}</td>" for field in sorted(row))
            + "</tr>"
            for row in rows[:MAX_EXPORT_ROWS]
        )
        headers = sorted({key for row in rows for key in row})
        document = (
            "<!doctype html><meta charset='utf-8'><title>Mailforge report</title><h1>Mailforge operational report</h1><p>Definition version: "
            + html.escape(REPORT_DEFINITION_VERSION)
            + "</p><table><thead><tr>"
            + "".join(f"<th>{html.escape(field)}</th>" for field in headers)
            + "</tr></thead><tbody>"
            + body
            + "</tbody></table>"
        )
        return document.encode(), "text/html; charset=utf-8", f"mailforge-{report_type}.html"

    def _named_report(self, report_type: str, filters: ReportFilters | None) -> dict[str, Any]:
        methods = {
            "summary": self.summary,
            "campaigns": self.campaigns,
            "providers": self.providers,
            "workflows": self.workflows,
            "inbound": self.inbound,
            "jobs": self.jobs,
            "integration": lambda filters: self.integration(),
        }
        if report_type not in methods:
            raise ReportingError("unsupported report type", "report_type_invalid")
        return methods[report_type](filters)

    def _metrics(self, filters: ReportFilters, report_range: ReportRange) -> dict[str, Any]:
        return {
            "campaigns": self._campaign_metrics(filters, report_range),
            "providers": self._provider_metrics(filters, report_range),
            "inbound": self._inbound_metrics(filters, report_range),
            "workflows": self._workflow_metrics(filters, report_range),
            "processing": self._processing_metrics(filters, report_range),
            "message_studio": self._message_metrics(filters, report_range),
            "insights": self._insights(filters, report_range),
        }

    def _campaign_metrics(
        self, filters: ReportFilters, report_range: ReportRange
    ) -> dict[str, Any]:
        start, end = report_range.start_utc.isoformat(), report_range.end_utc.isoformat()
        where, params = self._campaign_filter("c", filters, report_range, "c.created_at")
        attempt_where, attempt_params = self._campaign_filter(
            "c", filters, report_range, "a.created_at"
        )
        with self.store.connect() as connection:
            campaigns = connection.execute(
                f"SELECT COUNT(*) FROM campaigns c WHERE {where}", params
            ).fetchone()[0]
            intents = connection.execute(
                f"SELECT i.state,COUNT(*) AS count FROM delivery_intents i JOIN campaigns c ON c.id=i.campaign_id WHERE i.prepared_at>=? AND i.prepared_at<? AND {self._extra_campaign_filters('c', filters)} GROUP BY i.state",
                (start, end, *self._campaign_extra_params(filters)),
            ).fetchall()
            attempts = connection.execute(
                f"SELECT COALESCE(a.outcome,'unknown') AS outcome,COUNT(*) AS count FROM delivery_attempts a JOIN delivery_intents i ON i.id=a.intent_id JOIN campaigns c ON c.id=i.campaign_id WHERE {attempt_where} GROUP BY COALESCE(a.outcome,'unknown')",
                attempt_params,
            ).fetchall()
            latency = connection.execute(
                f"SELECT AVG((julianday(a.finished_at)-julianday(a.started_at))*86400.0) AS seconds FROM delivery_attempts a JOIN delivery_intents i ON i.id=a.intent_id JOIN campaigns c ON c.id=i.campaign_id WHERE {attempt_where} AND a.started_at IS NOT NULL AND a.finished_at IS NOT NULL",
                attempt_params,
            ).fetchone()[0]
            duration = connection.execute(
                f"SELECT AVG((julianday(c.completed_at)-julianday(c.started_at))*86400.0) AS seconds FROM campaigns c WHERE {where} AND c.started_at IS NOT NULL AND c.completed_at IS NOT NULL",
                params,
            ).fetchone()[0]
            transitions = connection.execute(
                f"SELECT t.to_status,COUNT(*) AS count FROM campaign_transitions t "
                f"JOIN campaigns c ON c.id=t.campaign_id WHERE t.occurred_at>=? AND t.occurred_at<? "
                f"AND {self._extra_campaign_filters('c', filters)} GROUP BY t.to_status",
                (start, end, *self._campaign_extra_params(filters)),
            ).fetchall()
            providers = connection.execute(
                f"SELECT COALESCE(c.provider_account_ref,'unassigned') AS provider_ref,COUNT(DISTINCT c.id) AS campaigns,COUNT(a.id) AS attempts FROM campaigns c LEFT JOIN delivery_intents i ON i.campaign_id=c.id LEFT JOIN delivery_attempts a ON a.intent_id=i.id AND a.created_at>=? AND a.created_at<? WHERE {where} GROUP BY COALESCE(c.provider_account_ref,'unassigned') ORDER BY campaigns DESC,provider_ref",
                (start, end, *params),
            ).fetchall()
        outcome = {row["outcome"]: row["count"] for row in attempts}
        states = {row["state"]: row["count"] for row in intents}
        return {
            "campaign_count": campaigns,
            "recipient_intents": sum(states.values()),
            "recipient_states": states,
            "attempts": {"total": sum(outcome.values()), **outcome},
            "average_attempt_latency_seconds": _safe_number(latency),
            "average_execution_duration_seconds": _safe_number(duration),
            "transitions": {row["to_status"]: row["count"] for row in transitions},
            "provider_breakdown": [dict(row) for row in providers],
            "evidence_class": "durable_local_observation",
            "limitations": [
                "accepted submissions are not inbox delivery",
                "delivery notifications are counted only when trusted inbound evidence exists",
            ],
        }

    def _provider_metrics(
        self, filters: ReportFilters, report_range: ReportRange
    ) -> dict[str, Any]:
        start, end = report_range.start_utc.isoformat(), report_range.end_utc.isoformat()
        account_filter = " AND profile_ref=?" if filters.provider_ref else ""
        params: tuple[Any, ...] = (start, end) + (
            (filters.provider_ref,) if filters.provider_ref else ()
        )
        with self.store.connect() as connection:
            diagnostics = connection.execute(
                "SELECT profile_ref,status,phase,external_access,COUNT(*) AS count FROM provider_diagnostics WHERE observed_at>=? AND observed_at<?"
                + account_filter
                + " GROUP BY profile_ref,status,phase,external_access ORDER BY profile_ref,status,phase",
                params,
            ).fetchall()
            accounts = connection.execute(
                "SELECT ref,display_name,status,outbound_enabled,inbound_enabled FROM provider_accounts"
                + (" WHERE ref=?" if filters.provider_ref else "")
                + " ORDER BY ref",
                ((filters.provider_ref,) if filters.provider_ref else ()),
            ).fetchall()
            failures = connection.execute(
                "SELECT profile_ref,status,phase,observed_at FROM provider_diagnostics WHERE observed_at>=? AND observed_at<?"
                + account_filter
                + " ORDER BY profile_ref,observed_at DESC",
                params,
            ).fetchall()
        consecutive: dict[str, int] = defaultdict(int)
        streaks: dict[str, int] = defaultdict(int)
        for row in failures:
            ref = row["profile_ref"]
            failed = str(row["status"]).lower() in {"failed", "error", "failure"}
            streaks[ref] = streaks[ref] + 1 if failed else 0
            consecutive[ref] = max(consecutive[ref], streaks[ref])
        return {
            "accounts": [dict(row) for row in accounts],
            "diagnostics": [dict(row) for row in diagnostics],
            "consecutive_failures": dict(consecutive),
            "evidence_class": "local_protocol_or_external_unverified",
            "limitations": [
                "provider configuration and presets are not proof of external connectivity",
                "external qualification is not claimed",
            ],
        }

    def _inbound_metrics(self, filters: ReportFilters, report_range: ReportRange) -> dict[str, Any]:
        start, end = report_range.start_utc.isoformat(), report_range.end_utc.isoformat()
        message_scope, scope_params = self._inbound_scope("m", filters)
        run_scope, run_params = self._inbound_scope("r", filters, source_only=True)
        with self.store.connect() as connection:
            messages = connection.execute(
                f"SELECT COUNT(*) FROM inbound_messages m WHERE m.acquired_at>=? AND m.acquired_at<? AND {message_scope}",
                (start, end, *scope_params),
            ).fetchone()[0]
            classifications = connection.execute(
                f"SELECT c.kind,COUNT(*) AS count FROM inbound_classifications c JOIN inbound_messages m ON m.id=c.message_id WHERE c.occurred_at>=? AND c.occurred_at<? AND {message_scope} GROUP BY c.kind",
                (start, end, *scope_params),
            ).fetchall()
            trust = connection.execute(
                f"SELECT c.trust_status,COUNT(*) AS count FROM inbound_correlations c JOIN inbound_messages m ON m.id=c.message_id WHERE c.created_at>=? AND c.created_at<? AND {message_scope} GROUP BY c.trust_status",
                (start, end, *scope_params),
            ).fetchall()
            pending = connection.execute(
                f"SELECT COUNT(*) FROM inbound_reviews r JOIN inbound_messages m ON m.id=r.message_id WHERE r.status='pending' AND {message_scope}",
                scope_params,
            ).fetchone()[0]
            runs = (
                connection.execute(
                    "SELECT COUNT(*) AS count,COALESCE(SUM(failure_count),0) AS failures,AVG((julianday(finished_at)-julianday(started_at))*86400.0) AS duration FROM inbound_runs r WHERE r.started_at>=? AND r.started_at<? AND "
                    + run_scope,
                    (start, end, *run_params),
                ).fetchone()
                if not (filters.account_ref or filters.folder)
                else {"count": 0, "failures": 0, "duration": None}
            )
            actions = connection.execute(
                f"SELECT a.status,COUNT(*) AS count FROM inbound_actions a JOIN inbound_messages m ON m.id=a.message_id WHERE a.proposed_at>=? AND a.proposed_at<? AND {message_scope} GROUP BY a.status",
                (start, end, *scope_params),
            ).fetchall()
        return {
            "messages": messages,
            "classifications": {row["kind"]: row["count"] for row in classifications},
            "trust": {row["trust_status"]: row["count"] for row in trust},
            "pending_reviews": pending,
            "pending_reviews_semantics": "current_snapshot_for_selected_source_scope",
            "runs": {
                "count": runs["count"],
                "failures": runs["failures"],
                "average_duration_seconds": _safe_number(runs["duration"]),
            },
            "runs_semantics": "not_measured_for_account_or_folder_scope"
            if filters.account_ref or filters.folder
            else "selected_source_scope",
            "actions": {row["status"]: row["count"] for row in actions},
            "evidence_class": "durable_local_observation",
            "limitations": ["untrusted reports do not count as confirmed delivery failure"],
        }

    def _workflow_metrics(
        self, filters: ReportFilters, report_range: ReportRange
    ) -> dict[str, Any]:
        start, end = report_range.start_utc.isoformat(), report_range.end_utc.isoformat()
        workflow_clause = " AND workflow_id=?" if filters.workflow_id else ""
        workflow_param = (filters.workflow_id,) if filters.workflow_id else ()
        with self.store.connect() as connection:
            workflows = connection.execute(
                "SELECT status,COUNT(*) AS count FROM automation_workflows"
                + (" WHERE id=?" if filters.workflow_id else "")
                + " GROUP BY status",
                (filters.workflow_id,) if filters.workflow_id else (),
            ).fetchall()
            runs = connection.execute(
                "SELECT status,COUNT(*) AS count FROM automation_runs WHERE created_at>=? AND created_at<?"
                + workflow_clause
                + " GROUP BY status",
                (start, end, *workflow_param),
            ).fetchall()
            steps = connection.execute(
                "SELECT h.status,COUNT(*) AS count FROM automation_step_history h "
                "JOIN automation_runs r ON r.id=h.run_id WHERE h.started_at>=? AND h.started_at<?"
                + (" AND r.workflow_id=?" if filters.workflow_id else "")
                + " GROUP BY h.status",
                (start, end, *workflow_param),
            ).fetchall()
            waits = connection.execute(
                "SELECT COUNT(*) AS count,AVG((julianday(COALESCE(completed_at,?))-julianday(waiting_since))*86400.0) AS duration "
                "FROM automation_runs WHERE status='waiting' AND waiting_since IS NOT NULL"
                + workflow_clause,
                (self.now_fn().isoformat(), *workflow_param),
            ).fetchone()
            stale_waits = connection.execute(
                "SELECT COUNT(*) FROM automation_runs WHERE status='waiting' AND waiting_since IS NOT NULL AND waiting_since<=?"
                + workflow_clause,
                ((self.now_fn() - timedelta(days=1)).isoformat(), *workflow_param),
            ).fetchone()
        return {
            "workflows": {row["status"]: row["count"] for row in workflows},
            "runs": {row["status"]: row["count"] for row in runs},
            "steps": {row["status"]: row["count"] for row in steps},
            "waiting": {
                "count": waits["count"],
                "average_duration_seconds": _safe_number(waits["duration"]),
            },
            "stale_waits": stale_waits[0],
            "waiting_semantics": "current_snapshot_for_selected_workflow_scope",
            "evidence_class": "durable_local_observation",
        }

    def _processing_metrics(
        self, filters: ReportFilters, report_range: ReportRange
    ) -> dict[str, Any]:
        start, end = report_range.start_utc.isoformat(), report_range.end_utc.isoformat()
        with self.store.connect() as connection:
            data = (
                connection.execute(
                    "SELECT status,COUNT(*) AS count,COALESCE(SUM(processed_count),0) AS processed,COALESCE(SUM(output_count),0) AS output,COALESCE(SUM(rejected_count),0) AS rejected,COALESCE(SUM(error_count),0) AS errors,AVG((julianday(completed_at)-julianday(started_at))*86400.0) AS duration FROM data_studio_jobs WHERE created_at>=? AND created_at<? GROUP BY status",
                    (start, end),
                ).fetchall()
                if filters.job_kind != "list_tools"
                else []
            )
            lists = (
                connection.execute(
                    "SELECT status,COUNT(*) AS count,COALESCE(SUM(processed_count),0) AS processed,COALESCE(SUM(accepted_count),0) AS output,AVG((julianday(completed_at)-julianday(started_at))*86400.0) AS duration FROM list_processing_jobs WHERE created_at>=? AND created_at<? GROUP BY status",
                    (start, end),
                ).fetchall()
                if filters.job_kind != "data_studio"
                else []
            )
            artifacts = 0
            if filters.job_kind != "list_tools":
                artifacts += connection.execute(
                    "SELECT COUNT(*) FROM data_studio_artifacts WHERE created_at>=? AND created_at<?",
                    (start, end),
                ).fetchone()[0]
            if filters.job_kind != "data_studio":
                artifacts += connection.execute(
                    "SELECT COUNT(*) FROM list_processing_artifacts WHERE created_at>=? AND created_at<?",
                    (start, end),
                ).fetchone()[0]

        def rows(values: list[Any]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for row in values:
                keys = set(row.keys())
                result[row["status"]] = {
                    "jobs": row["count"],
                    "processed": row["processed"],
                    "output": row["output"],
                    "rejected": row["rejected"] if "rejected" in keys else 0,
                    "errors": row["errors"] if "errors" in keys else 0,
                    "average_duration_seconds": _safe_number(row["duration"]),
                }
            return result

        return {
            "data_studio": rows(data),
            "list_tools": rows(lists),
            "artifact_count": artifacts,
            "job_kind": filters.job_kind or "all",
            "evidence_class": "durable_local_observation",
            "limitations": [
                "throughput is reported only when start and completion timestamps exist"
            ],
        }

    def _message_metrics(self, filters: ReportFilters, report_range: ReportRange) -> dict[str, Any]:
        start, end = report_range.start_utc.isoformat(), report_range.end_utc.isoformat()
        with self.store.connect() as connection:
            documents = connection.execute(
                "SELECT COUNT(*) FROM message_documents WHERE created_at>=? AND created_at<?",
                (start, end),
            ).fetchone()[0]
            revisions = connection.execute(
                "SELECT COUNT(*) FROM message_revisions WHERE created_at>=? AND created_at<?",
                (start, end),
            ).fetchone()[0]
            published = connection.execute(
                "SELECT COUNT(*) FROM message_revisions WHERE published_at>=? AND published_at<?",
                (start, end),
            ).fetchone()[0]
            snapshots = connection.execute(
                "SELECT COUNT(*) FROM message_campaign_snapshots WHERE created_at>=? AND created_at<?",
                (start, end),
            ).fetchone()[0]
        return {
            "documents_created": documents,
            "revisions_created": revisions,
            "revisions_published": published,
            "campaign_snapshots": snapshots,
            "render_failures": {"value": None, "status": "not_measured"},
            "evidence_class": "durable_local_observation",
            "limitations": [
                "render validation failures are not separately persisted by the current authority"
            ],
        }

    def _insights(self, filters: ReportFilters, report_range: ReportRange) -> list[dict[str, Any]]:
        campaign = self._campaign_metrics(filters, report_range)
        inbound = self._inbound_metrics(filters, report_range)
        workflow = self._workflow_metrics(filters, report_range)
        processing = self._processing_metrics(filters, report_range)
        findings: list[dict[str, Any]] = []
        ambiguous = campaign["attempts"].get("ambiguous", 0)
        attempt_where, attempt_params = self._campaign_filter(
            "c", filters, report_range, "a.created_at"
        )
        ambiguous_ids = self._record_ids(
            f"SELECT a.id FROM delivery_attempts a JOIN delivery_intents i ON i.id=a.intent_id "
            f"JOIN campaigns c ON c.id=i.campaign_id WHERE {attempt_where} "
            "AND COALESCE(a.outcome,'unknown')='ambiguous' ORDER BY a.created_at DESC,a.id",
            attempt_params,
        )
        if ambiguous:
            findings.append(
                {
                    "id": "ambiguous-submissions",
                    "severity": "attention",
                    "title": "Ambiguous submissions need reconciliation",
                    "observation": f"{ambiguous} attempt(s) have an unresolved outcome.",
                    "evidence": {
                        "metric": "attempt.ambiguous",
                        "count": ambiguous,
                        "record_ids": ambiguous_ids,
                    },
                    "next_action": "Inspect the affected campaign attempt records before retrying.",
                }
            )
        repeated_provider_failures = sum(
            value
            for value in self._provider_metrics(filters, report_range)[
                "consecutive_failures"
            ].values()
            if value >= 2
        )
        provider_id_clause = " AND profile_ref=?" if filters.provider_ref else ""
        provider_id_params: tuple[Any, ...] = (
            report_range.start_utc.isoformat(),
            report_range.end_utc.isoformat(),
            *((filters.provider_ref,) if filters.provider_ref else ()),
        )
        if repeated_provider_failures:
            findings.append(
                {
                    "id": "provider-failures",
                    "severity": "attention",
                    "title": "Provider diagnostics show repeated failures",
                    "observation": f"At least {repeated_provider_failures} consecutive provider failure(s) were observed.",
                    "evidence": {
                        "metric": "provider.diagnostic",
                        "count": repeated_provider_failures,
                        "record_ids": self._record_ids(
                            "SELECT id FROM provider_diagnostics WHERE observed_at>=? AND observed_at<? "
                            "AND status IN ('failed','error','failure')"
                            + provider_id_clause
                            + " ORDER BY observed_at DESC",
                            provider_id_params,
                        ),
                    },
                    "next_action": "Inspect the provider diagnostic phase and authorization boundary before retrying.",
                }
            )
        if inbound["pending_reviews"]:
            review_scope, review_params = self._inbound_scope("m", filters)
            findings.append(
                {
                    "id": "inbound-review-backlog",
                    "severity": "attention",
                    "title": "Inbound review backlog is open",
                    "observation": f"{inbound['pending_reviews']} review(s) remain pending.",
                    "evidence": {
                        "metric": "inbound.review",
                        "count": inbound["pending_reviews"],
                        "record_ids": self._record_ids(
                            f"SELECT r.id FROM inbound_reviews r JOIN inbound_messages m ON m.id=r.message_id WHERE r.status='pending' AND {review_scope} ORDER BY r.created_at DESC",
                            review_params,
                        ),
                    },
                    "next_action": "Open Inbound and review trusted/untrusted evidence.",
                }
            )
        if workflow["waiting"]["count"]:
            workflow_id_clause = " AND workflow_id=?" if filters.workflow_id else ""
            workflow_id_params = (filters.workflow_id,) if filters.workflow_id else ()
            findings.append(
                {
                    "id": "workflow-waiting",
                    "severity": "info",
                    "title": "Workflow runs are waiting",
                    "observation": f"{workflow['waiting']['count']} run(s) are waiting or scheduled.",
                    "evidence": {
                        "metric": "workflow.run",
                        "count": workflow["waiting"]["count"],
                        "record_ids": self._record_ids(
                            "SELECT id FROM automation_runs WHERE status='waiting'"
                            + workflow_id_clause
                            + " ORDER BY waiting_since ASC",
                            workflow_id_params,
                        ),
                    },
                    "next_action": "Inspect the workflow run timeline and due time.",
                }
            )
        if workflow["stale_waits"]:
            findings.append(
                {
                    "id": "stale-workflow-waits",
                    "severity": "attention",
                    "title": "Workflow waits are stale",
                    "observation": f"{workflow['stale_waits']} waiting run(s) have been waiting for at least one day.",
                    "evidence": {
                        "metric": "workflow.run",
                        "count": workflow["stale_waits"],
                        "record_ids": self._record_ids(
                            "SELECT id FROM automation_runs WHERE status='waiting' AND waiting_since<=?"
                            + workflow_id_clause
                            + " ORDER BY waiting_since ASC",
                            (
                                (self.now_fn() - timedelta(days=1)).isoformat(),
                                *workflow_id_params,
                            ),
                        ),
                    },
                    "next_action": "Inspect the wait due time and workflow revision before intervening.",
                }
            )
        failed_jobs = sum(
            value["jobs"]
            for source in (processing["data_studio"], processing["list_tools"])
            for status, value in source.items()
            if status == "failed"
        )
        if failed_jobs:
            findings.append(
                {
                    "id": "processing-failures",
                    "severity": "attention",
                    "title": "Processing jobs failed",
                    "observation": f"{failed_jobs} processing job(s) failed in the selected range.",
                    "evidence": {
                        "metric": "data.job",
                        "count": failed_jobs,
                        "record_ids": self._record_ids(
                            "SELECT id FROM data_studio_jobs WHERE status='failed' AND created_at>=? AND created_at<? "
                            "UNION ALL SELECT id FROM list_processing_jobs WHERE status='failed' AND created_at>=? AND created_at<?",
                            (
                                report_range.start_utc.isoformat(),
                                report_range.end_utc.isoformat(),
                                report_range.start_utc.isoformat(),
                                report_range.end_utc.isoformat(),
                            ),
                        ),
                    },
                    "next_action": "Open the affected job and inspect its error artifact.",
                }
            )
        if campaign["campaign_count"] and not campaign["attempts"]["total"]:
            findings.append(
                {
                    "id": "missing-attempt-evidence",
                    "severity": "info",
                    "title": "No submission attempts were observed",
                    "observation": "Campaign records exist but no attempt record is in the selected range.",
                    "evidence": {"metric": "attempt.count", "count": 0},
                    "next_action": "Inspect campaign preparation and execution state.",
                }
            )
        return findings

    def _campaign_filter(
        self, alias: str, filters: ReportFilters, report_range: ReportRange, time_field: str
    ) -> tuple[str, tuple[Any, ...]]:
        return (
            f"{time_field}>=? AND {time_field}<? AND {self._extra_campaign_filters(alias, filters)}",
            (
                report_range.start_utc.isoformat(),
                report_range.end_utc.isoformat(),
                *self._campaign_extra_params(filters),
            ),
        )

    @staticmethod
    def _extra_campaign_filters(alias: str, filters: ReportFilters) -> str:
        clauses = ["1=1"]
        if filters.provider_ref:
            clauses.append(f"{alias}.provider_account_ref=?")
        if filters.campaign_id:
            clauses.append(f"{alias}.id=?")
        return " AND ".join(clauses)

    @staticmethod
    def _campaign_extra_params(filters: ReportFilters) -> tuple[Any, ...]:
        return tuple(
            value for value in (filters.provider_ref, filters.campaign_id) if value is not None
        )

    @staticmethod
    def _inbound_scope(
        alias: str, filters: ReportFilters, *, source_only: bool = False
    ) -> tuple[str, tuple[Any, ...]]:
        clauses = ["1=1"]
        values: list[Any] = []
        if filters.source_ref:
            clauses.append(f"{alias}.source_ref=?")
            values.append(filters.source_ref)
        if not source_only and filters.account_ref:
            clauses.append(f"{alias}.account_ref=?")
            values.append(filters.account_ref)
        if not source_only and filters.folder:
            clauses.append(f"{alias}.folder=?")
            values.append(filters.folder)
        return " AND ".join(clauses), tuple(values)

    def _record_ids(self, query: str, params: tuple[Any, ...], *, limit: int = 20) -> list[str]:
        with self.store.connect() as connection:
            return [str(row[0]) for row in connection.execute(query, params).fetchmany(limit)]

    def _envelope(
        self,
        report_type: str,
        filters: ReportFilters,
        report_range: ReportRange,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "report_type": report_type,
            "definition_version": REPORT_DEFINITION_VERSION,
            "generated_at": self.now_fn().astimezone(UTC).isoformat(),
            "range": report_range.to_dict(),
            "filters": filters.to_dict(),
            "evidence": {
                "class": "durable_local_observation",
                "simulated": True,
                "limitations": [
                    "local synthetic observations are not real-provider qualification",
                    "SMTP acceptance is not inbox delivery",
                ],
            },
            **payload,
        }

    def _flatten_rows(self, value: Any, prefix: str = "") -> list[dict[str, Any]]:
        if isinstance(value, list):
            if all(isinstance(item, dict) for item in value):
                return [row for item in value for row in self._flatten_rows(item, prefix)]
            return [
                {prefix.rstrip("."): json.dumps(item, sort_keys=True, default=str)}
                for item in value
            ]
        if isinstance(value, dict):
            collection_key, collection = next(
                (
                    (key, item)
                    for key, item in value.items()
                    if isinstance(item, list) and all(isinstance(row, dict) for row in item)
                ),
                (None, None),
            )
            if collection is not None:
                metadata = {
                    key: json.dumps(item, sort_keys=True, default=str)
                    if isinstance(item, (dict, list))
                    else item
                    for key, item in value.items()
                    if key != collection_key
                }
                rows = self._flatten_rows(collection, prefix)
                return [{**metadata, **row} for row in rows] or [metadata]
            row: dict[str, Any] = {}
            for key, item in value.items():
                column = f"{prefix}{key}" if prefix else key
                if isinstance(item, (dict, list)):
                    row[column] = json.dumps(item, sort_keys=True, default=str)
                else:
                    row[column] = item
            return [row]
        return [{prefix.rstrip("."): value}]


def _formula_safe(value: Any) -> Any:
    if isinstance(value, str) and value[:1] in {"=", "+", "-", "@"}:
        return "'" + value
    return value
