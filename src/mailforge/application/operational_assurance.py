"""Local operational supervision over Mailforge's existing authorities.

This service observes durable state and records bounded operator observations;
it does not become a second scheduler, worker, delivery, or policy authority.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import zipfile
from datetime import UTC, datetime, timedelta
from typing import Any

from mailforge.application.local_application import DataDirectory, validate_backup
from mailforge.infrastructure.campaign_store import CampaignStore


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class OperationalAssuranceService:
    """Read-only health aggregation plus durable local intervention evidence."""

    def __init__(self, store: CampaignStore, data_directory: DataDirectory):
        self.store = store
        self.data_directory = data_directory

    def _counts(self, table: str, statuses: tuple[str, ...]) -> dict[str, int]:
        placeholders = ",".join("?" for _ in statuses)
        with self.store.connect() as connection:
            rows = connection.execute(
                f"SELECT status, COUNT(*) AS count FROM {table} "
                f"WHERE status IN ({placeholders}) GROUP BY status",
                statuses,
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    @staticmethod
    def _record_source_failure(
        failures: list[dict[str, str]] | None, source: str, error: Exception
    ) -> None:
        if failures is not None:
            failures.append(
                {
                    "source": source,
                    "reason": type(error).__name__,
                    "detail": OperationalAssuranceService._safe_error(error),
                }
            )

    def _all_counts(
        self, table: str, *, failures: list[dict[str, str]] | None = None
    ) -> dict[str, int]:
        try:
            with self.store.connect() as connection:
                rows = connection.execute(
                    f"SELECT status, COUNT(*) AS count FROM {table} GROUP BY status"
                ).fetchall()
            return {str(row["status"]): int(row["count"]) for row in rows}
        except Exception as error:
            self._record_source_failure(failures, table, error)
            return {}

    @staticmethod
    def _safe_error(value: Any) -> str:
        """Keep diagnostics useful without copying secrets or local paths."""

        text = str(value or "")
        if not text:
            return ""
        if re.search(r"(?i)(password|secret|token|api[_ -]?key|credential|bearer)", text):
            return "[redacted-sensitive-error]"
        if re.search(r"/(?:Users|private|var|tmp|home|Volumes|opt)/", text):
            return "[redacted-local-path]"
        lowered = text.lower()
        for label, terms in (
            ("timeout", ("timeout", "timed out")),
            ("interrupted", ("interrupt", "abandoned", "terminated")),
            ("validation", ("invalid", "validation", "schema")),
            ("authorization", ("authorization", "forbidden", "permission")),
        ):
            if any(term in lowered for term in terms):
                return f"operation-error:{label}"
        return "operation-error"

    def _job_rows(
        self,
        *,
        limit: int | None = 500,
        failures: list[dict[str, str]] | None = None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        tables = (
            ("campaigns", "campaigns", "failure_reason", "created_at", "updated_at"),
            ("list_processing_jobs", "list_tools", "error_message", "created_at", "updated_at"),
            ("data_studio_jobs", "data_studio", "error_message", "created_at", "updated_at"),
            (
                "data_connection_snapshots",
                "data_connections",
                "error_message",
                "created_at",
                "updated_at",
            ),
            ("automation_runs", "automation", "failure_reason", "created_at", "updated_at"),
            (
                "integration_webhook_deliveries",
                "integration",
                "last_error",
                "created_at",
                "updated_at",
            ),
            ("provider_test_submissions", "providers", "detail", "created_at", "updated_at"),
            (
                "inbound_runs",
                "inbound",
                "error_message",
                "started_at",
                "COALESCE(finished_at, started_at)",
            ),
            ("inbound_mailbox_actions", "inbound", "error_message", "created_at", "updated_at"),
        )
        try:
            connection = self.store.connect()
        except Exception as error:
            self._record_source_failure(failures, "job_store", error)
            return rows
        with connection:
            for table, subsystem, error_column, created_column, updated_column in tables:
                try:
                    limit_clause = f" LIMIT {limit}" if limit is not None else ""
                    result = connection.execute(
                        f"SELECT id, status, {created_column} AS created_at, "
                        f"{updated_column} AS updated_at, "
                        f"{error_column} AS error "
                        f"FROM {table} ORDER BY updated_at DESC{limit_clause}"
                    ).fetchall()
                except Exception as error:
                    self._record_source_failure(failures, table, error)
                    continue
                rows.extend(
                    {
                        "id": row["id"],
                        "subsystem": subsystem,
                        "status": row["status"],
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                        "error": self._safe_error(row["error"]),
                        "workbench": {
                            "list_tools": "list-tools",
                            "data_studio": "data-studio",
                            "data_connections": "data-connections",
                            "automation": "automations",
                            "integration": "integrations",
                            "campaigns": "campaigns",
                            "providers": "providers",
                            "inbound": "inbound",
                        }[subsystem],
                    }
                    for row in result
                )
        return rows

    def _backup_observation(
        self, *, failures: list[dict[str, str]] | None = None
    ) -> tuple[datetime | None, int]:
        paths = sorted(self.data_directory.backups.glob("*.zip"), reverse=True)
        latest: datetime | None = None
        for path in paths:
            try:
                item = validate_backup(path)
            except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as error:
                self._record_source_failure(failures, str(path.name), error)
                continue
            if item.get("valid"):
                latest = _parse(item.get("created_at"))
                if latest is not None:
                    break
        return latest, len(paths)

    def _incident(self, subsystem: str, condition: str, operation_id: str | None) -> dict[str, Any]:
        identity = hashlib.sha256(f"{subsystem}:{operation_id or ''}".encode()).hexdigest()[:24]
        severity = (
            "critical"
            if condition in {"database_unavailable", "database_integrity_failed"}
            else "warning"
        )
        return {
            "id": identity,
            "subsystem": subsystem,
            "condition": condition,
            "severity": severity,
            "affected_operation": operation_id,
            "disposition": "open",
            "guidance": "Inspect the originating workbench before retrying.",
        }

    def health(self, *, worker_status: dict[str, Any] | None = None) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        source_failures: list[dict[str, str]] = []
        try:
            integrity = self.store.integrity_check()
            checks.append(
                {
                    "key": "database",
                    "state": "healthy" if integrity else "action_required",
                    "detail": "SQLite integrity verified"
                    if integrity
                    else "SQLite integrity check failed",
                }
            )
        except Exception as error:
            checks.append(
                {"key": "database", "state": "unavailable", "detail": type(error).__name__}
            )
        try:
            usage = shutil.disk_usage(self.data_directory.root)
            free_ratio = usage.free / max(usage.total, 1)
            state = "action_required" if free_ratio < 0.05 else "healthy"
            checks.append(
                {
                    "key": "storage",
                    "state": state,
                    "free_bytes": usage.free,
                    "free_ratio": round(free_ratio, 4),
                }
            )
        except OSError as error:
            checks.append({"key": "storage", "state": "unknown", "detail": type(error).__name__})
        jobs = self._job_rows(limit=None, failures=source_failures)
        now = datetime.now(UTC)
        stale = [
            job
            for job in jobs
            if job["status"] in {"running", "delivering"}
            and (_parse(job["updated_at"]) or now) < now - timedelta(minutes=5)
        ]
        failed = [job for job in jobs if job["status"] in {"failed", "ambiguous", "suspended"}]
        if stale:
            checks.append({"key": "workers", "state": "action_required", "stale_count": len(stale)})
        elif failed:
            checks.append({"key": "workers", "state": "degraded", "failed_count": len(failed)})
        else:
            checks.append(
                {
                    "key": "workers",
                    "state": "healthy",
                    "active_count": sum(job["status"] in {"running", "delivering"} for job in jobs),
                }
            )
        subsystem_counts = {
            "campaigns": self._all_counts("campaigns", failures=source_failures),
            "provider_tests": self._all_counts(
                "provider_test_submissions", failures=source_failures
            ),
            "mailbox_sync": self._all_counts("inbound_runs", failures=source_failures),
            "mailbox_actions": self._all_counts(
                "inbound_mailbox_actions", failures=source_failures
            ),
        }
        provider_resources: list[dict[str, Any]] = []
        try:
            with self.store.connect() as connection:
                provider_resources = [
                    {
                        "account_ref": row["ref"],
                        "status": row["status"],
                        "configured_rate_per_minute": row["pacing_per_minute"],
                        "configured_concurrency": row["max_concurrency"],
                        "observed_local_usage": {
                            "attempts": int(row["attempts"] or 0),
                            "accepted": int(row["accepted"] or 0),
                            "refused": int(row["refused"] or 0),
                            "ambiguous": int(row["ambiguous"] or 0),
                            "other_failures": int(row["other_failures"] or 0),
                        },
                        "provider_reported_limit": None,
                        "quota_evidence": "configured-local-limit-and-local-attempts",
                    }
                    for row in connection.execute(
                        "SELECT a.ref,a.status,a.pacing_per_minute,a.max_concurrency, "
                        "COUNT(t.id) AS attempts, "
                        "SUM(CASE WHEN t.status='provider_accepted' "
                        "THEN 1 ELSE 0 END) AS accepted, "
                        "SUM(CASE WHEN t.status IN ('refused','provider_refused',"
                        "'rejected','permanent_rejection') THEN 1 ELSE 0 END) AS refused, "
                        "SUM(CASE WHEN t.status='ambiguous' THEN 1 ELSE 0 END) AS ambiguous, "
                        "SUM(CASE WHEN t.status NOT IN ("
                        "'provider_accepted','pending','reserved','ambiguous','refused',"
                        "'provider_refused','rejected','permanent_rejection') "
                        "THEN 1 ELSE 0 END) AS other_failures "
                        "FROM provider_accounts a LEFT JOIN provider_test_submissions t "
                        "ON t.account_ref=a.ref GROUP BY a.ref ORDER BY a.ref"
                    ).fetchall()
                ]
        except Exception as error:
            self._record_source_failure(source_failures, "provider_accounts", error)
            provider_resources = []
        if provider_resources:
            checks.append(
                {"key": "provider_capacity", "state": "healthy", "detail": "configured limits only"}
            )
        for key, counts in subsystem_counts.items():
            failed_count = sum(
                value
                for status, value in counts.items()
                if status in {"failed", "ambiguous", "error"}
            )
            if failed_count:
                checks.append({"key": key, "state": "degraded", "failed_count": failed_count})
            elif counts:
                checks.append({"key": key, "state": "healthy", "counts": counts})
        if worker_status:
            stopped = [
                key
                for key in ("scheduler", "automation_scheduler")
                if worker_status.get(key) != "running"
            ]
            if stopped:
                checks.append(
                    {"key": "supervisors", "state": "action_required", "stopped": stopped}
                )
            credential_store = worker_status.get("credential_store")
            if isinstance(credential_store, dict):
                keychain_state = credential_store.get("keychain", "unknown")
                checks.append(
                    {
                        "key": "keychain_backend",
                        "state": "healthy" if keychain_state == "available" else "unknown",
                        "detail": f"keychain backend {keychain_state}; references remain opaque",
                    }
                )
        try:
            latest, backup_count = self._backup_observation(failures=source_failures)
        except Exception as error:
            self._record_source_failure(source_failures, "backups", error)
            latest, backup_count = None, 0
        backup_state = (
            "healthy" if latest and latest >= now - timedelta(days=7) else "action_required"
        )
        checks.append(
            {
                "key": "backups",
                "state": backup_state,
                "latest_at": latest.isoformat() if latest else None,
                "count": backup_count,
            }
        )
        if source_failures:
            checks.append(
                {
                    "key": "source_evidence",
                    "state": "action_required",
                    "failures": source_failures,
                    "detail": "one or more operational evidence sources were unavailable",
                }
            )
        states = {check["state"] for check in checks}
        overall = (
            "unavailable"
            if "unavailable" in states
            else "action_required"
            if "action_required" in states
            else "degraded"
            if "degraded" in states
            else "healthy"
        )
        return {
            "state": overall,
            "observed_at": _now(),
            "checks": checks,
            "jobs": {
                "active": sum(job["status"] in {"running", "delivering"} for job in jobs),
                "failed": len(failed),
                "stale": len(stale),
            },
            "subsystems": subsystem_counts,
            "resources": {"providers": provider_resources},
            "worker_status": worker_status or {},
        }

    def jobs(self, *, subsystem: str | None = None, status: str | None = None) -> dict[str, Any]:
        failures: list[dict[str, str]] = []
        rows = self._job_rows(failures=failures)
        if subsystem:
            rows = [row for row in rows if row["subsystem"] == subsystem]
        if status:
            rows = [row for row in rows if row["status"] == status]
        return {"items": rows[:200], "count": len(rows), "source_failures": failures}

    def incidents(
        self,
        *,
        include_acknowledged: bool = False,
        worker_status: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._persist_incidents(self.health(worker_status=worker_status))
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT id, aggregate_id, event_type, payload_json, occurred_at "
                "FROM operational_events "
                "WHERE aggregate_type='operational_incident' "
                "ORDER BY occurred_at DESC, id DESC LIMIT 500"
            ).fetchall()
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            payload = json.loads(row["payload_json"] or "{}")
            latest.setdefault(
                row["aggregate_id"],
                {
                    "id": row["aggregate_id"],
                    "event_type": row["event_type"],
                    **payload,
                    "occurred_at": row["occurred_at"],
                },
            )
        items = list(latest.values())
        if not include_acknowledged:
            items = [item for item in items if item.get("disposition") == "open"]
        return {"items": items[:200], "count": len(items)}

    def _persist_incidents(self, result: dict[str, Any]) -> None:
        observed = {
            check["key"]: check
            for check in result["checks"]
            if check["state"] not in {"healthy", "unknown"}
        }
        with self.store.transaction(immediate=True) as connection:
            for check in result["checks"]:
                incident = self._incident(check["key"], check["state"], None)
                incident_id = incident["id"]
                previous = connection.execute(
                    "SELECT event_type, payload_json FROM operational_events "
                    "WHERE aggregate_type='operational_incident' AND aggregate_id=? "
                    "ORDER BY occurred_at DESC, id DESC LIMIT 1",
                    (incident_id,),
                ).fetchone()
                if check["key"] in observed:
                    signature = json.dumps(check, sort_keys=True, separators=(",", ":"))
                    previous_payload = (
                        json.loads(previous["payload_json"] or "{}") if previous else {}
                    )
                    if (
                        previous
                        and previous["event_type"] == "incident_observed"
                        and previous_payload.get("signature") == signature
                    ):
                        continue
                    self.store.record_event(
                        connection,
                        "operational_incident",
                        incident_id,
                        "incident_observed",
                        {**incident, "disposition": "open", "signature": signature},
                    )
                elif previous and previous["event_type"] in {
                    "incident_observed",
                    "incident_acknowledged",
                }:
                    self.store.record_event(
                        connection,
                        "operational_incident",
                        incident_id,
                        "incident_resolved",
                        {**incident, "disposition": "resolved"},
                    )

    def health_check(self, *, worker_status: dict[str, Any] | None = None) -> dict[str, Any]:
        result = self.health(worker_status=worker_status)
        self._persist_incidents(result)
        with self.store.transaction(immediate=True) as connection:
            self.store.record_event(
                connection,
                "operational",
                "runtime",
                "health_check",
                {
                    "state": result["state"],
                    "checks": [
                        {
                            "key": item["key"],
                            "state": item["state"],
                        }
                        for item in result["checks"]
                    ],
                },
            )
        return result

    def acknowledge(self, incident_id: str) -> dict[str, Any]:
        if not incident_id or len(incident_id) > 80:
            raise ValueError("incident identity is invalid")
        with self.store.transaction(immediate=True) as connection:
            latest = connection.execute(
                "SELECT payload_json FROM operational_events "
                "WHERE aggregate_type='operational_incident' AND aggregate_id=? "
                "ORDER BY occurred_at DESC, id DESC LIMIT 1",
                (incident_id,),
            ).fetchone()
            latest_payload = json.loads(latest["payload_json"] or "{}") if latest else {}
            self.store.record_event(
                connection,
                "operational_incident",
                incident_id,
                "incident_acknowledged",
                {
                    "disposition": "acknowledged",
                    "actor": "local-operator",
                    "signature": latest_payload.get("signature"),
                },
            )
        return {"id": incident_id, "disposition": "acknowledged"}

    def diagnostic_export(self, *, worker_status: dict[str, Any] | None = None) -> dict[str, Any]:
        health = self.health(worker_status=worker_status)
        self._persist_incidents(health)
        incidents = self.incidents(worker_status=worker_status)
        return {
            "format": "mailforge-operational-diagnostics",
            "generated_at": _now(),
            "health": health,
            "incidents": incidents,
            "jobs": self.jobs(),
            "limitations": [
                "local observations only",
                "provider and mailbox outcomes are not inferred",
            ],
        }
