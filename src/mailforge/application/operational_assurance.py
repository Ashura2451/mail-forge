"""Local operational supervision over Mailforge's existing authorities.

This service observes durable state and records bounded operator observations;
it does not become a second scheduler, worker, delivery, or policy authority.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime, timedelta
from typing import Any

from mailforge.application.local_application import DataDirectory, list_backups
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

    def _job_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        tables = (
            ("list_processing_jobs", "list_tools", "error_message"),
            ("data_studio_jobs", "data_studio", "error_message"),
            ("data_connection_snapshots", "data_connections", "error_message"),
            ("automation_runs", "automation", "failure_reason"),
            ("integration_webhook_deliveries", "integration", "last_error"),
        )
        with self.store.connect() as connection:
            for table, subsystem, error_column in tables:
                try:
                    result = connection.execute(
                        f"SELECT id, status, created_at, updated_at, "
                        f"{error_column} AS error "
                        f"FROM {table} ORDER BY updated_at DESC LIMIT 100"
                    ).fetchall()
                except Exception:
                    continue
                rows.extend(
                    {
                        "id": row["id"],
                        "subsystem": subsystem,
                        "status": row["status"],
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                        "error": str(row["error"] or "")[:300],
                    }
                    for row in result
                )
        return rows

    def _incident(self, subsystem: str, condition: str, operation_id: str | None) -> dict[str, Any]:
        identity = hashlib.sha256(
            f"{subsystem}:{condition}:{operation_id or ''}".encode()
        ).hexdigest()[:24]
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
        jobs = self._job_rows()
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
        backups = list_backups(self.data_directory)
        latest = max(
            (_parse(item.get("created_at")) for item in backups if item.get("valid")), default=None
        )
        backup_state = (
            "healthy" if latest and latest >= now - timedelta(days=7) else "action_required"
        )
        checks.append(
            {
                "key": "backups",
                "state": backup_state,
                "latest_at": latest.isoformat() if latest else None,
                "count": len(backups),
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
            "worker_status": worker_status or {},
        }

    def jobs(self, *, subsystem: str | None = None, status: str | None = None) -> dict[str, Any]:
        rows = self._job_rows()
        if subsystem:
            rows = [row for row in rows if row["subsystem"] == subsystem]
        if status:
            rows = [row for row in rows if row["status"] == status]
        return {"items": rows[:200], "count": len(rows)}

    def incidents(self, *, include_acknowledged: bool = False) -> dict[str, Any]:
        health = self.health()
        incidents = [
            self._incident(check["key"], check["state"], None)
            for check in health["checks"]
            if check["state"] not in {"healthy", "unknown"}
        ]
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT aggregate_id, event_type, payload_json, occurred_at "
                "FROM operational_events "
                "WHERE aggregate_type='operational_incident' ORDER BY occurred_at DESC LIMIT 200"
            ).fetchall()
        acknowledged = {
            row["aggregate_id"] for row in rows if row["event_type"] == "incident_acknowledged"
        }
        for row in rows:
            payload = json.loads(row["payload_json"] or "{}")
            if include_acknowledged or payload.get("disposition") != "acknowledged":
                incidents.append(
                    {
                        "id": row["aggregate_id"],
                        "event_type": row["event_type"],
                        **payload,
                        "occurred_at": row["occurred_at"],
                    }
                )
        unique = {
            item["id"]: item
            for item in incidents
            if include_acknowledged or item["id"] not in acknowledged
        }
        return {"items": list(unique.values())[:200], "count": len(unique)}

    def health_check(self) -> dict[str, Any]:
        result = self.health()
        with self.store.transaction(immediate=True) as connection:
            self.store.record_event(
                connection,
                "operational",
                "runtime",
                "health_check",
                {
                    "state": result["state"],
                    "checks": [
                        {"key": item["key"], "state": item["state"]} for item in result["checks"]
                    ],
                },
            )
        return result

    def acknowledge(self, incident_id: str) -> dict[str, Any]:
        if not incident_id or len(incident_id) > 80:
            raise ValueError("incident identity is invalid")
        with self.store.transaction(immediate=True) as connection:
            self.store.record_event(
                connection,
                "operational_incident",
                incident_id,
                "incident_acknowledged",
                {"disposition": "acknowledged", "actor": "local-operator"},
            )
        return {"id": incident_id, "disposition": "acknowledged"}

    def diagnostic_export(self) -> dict[str, Any]:
        health = self.health()
        incidents = self.incidents()
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
