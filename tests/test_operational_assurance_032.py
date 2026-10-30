from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from mailforge.application.local_application import DataDirectory, create_backup
from mailforge.application.operational_assurance import OperationalAssuranceService
from mailforge.infrastructure.campaign_store import CampaignStore
from mailforge.infrastructure.credentials import KeychainCredentialResolver, LocalCredentialResolver
from mailforge.interfaces.web import create_app


def test_operational_health_and_incidents_are_durable(tmp_path: Path) -> None:
    data = DataDirectory.from_path(tmp_path / "data")
    data.initialize()
    store = CampaignStore(data.database)
    service = OperationalAssuranceService(store, data)

    initial = service.health()
    assert initial["state"] == "action_required"  # no verified backup exists yet
    checked = service.health_check()
    assert checked["observed_at"]
    events = service.incidents()
    assert events["count"] >= 1
    incident_id = events["items"][0]["id"]
    assert service.acknowledge(incident_id)["disposition"] == "acknowledged"
    assert service.incidents()["count"] == 0
    assert service.incidents(include_acknowledged=True)["count"] >= 1


def test_operational_api_exposes_jobs_health_and_redacted_export(tmp_path: Path) -> None:
    app = create_app(tmp_path / "state.sqlite3", allowed_hosts={"testserver"})
    with TestClient(app) as client:
        session = client.get("/api/v1/session")
        headers = {"x-mailforge-csrf": session.json()["csrf_token"]}
        health = client.get("/api/v1/operations/health")
        jobs = client.get("/api/v1/operations/jobs")
        export = client.get("/api/v1/operations/diagnostic-export")
        assert health.status_code == 200
        assert health.json()["state"] in {
            "healthy",
            "action_required",
            "degraded",
            "unknown",
            "unavailable",
        }
        assert jobs.status_code == 200
        assert export.status_code == 200
        assert "password" not in json.dumps(export.json()).lower()
        assert (
            client.post("/api/v1/runtime/backups/invalid.zip/verify", headers=headers).status_code
            == 400
        )
        checked = client.post("/api/v1/operations/health-check", headers=headers)
        assert checked.status_code == 200


def test_keychain_resolver_uses_opaque_reference_and_runtime_only_secret() -> None:
    calls: list[tuple[list[str], str | None]] = []
    values = {"mailforge-account": "synthetic-secret"}

    def runner(args, **kwargs):
        calls.append((args, kwargs.get("input")))
        if args[1] == "find-generic-password":
            if "mailforge-account" not in values:
                raise subprocess.CalledProcessError(44, args)
            return subprocess.CompletedProcess(args, 0, values["mailforge-account"], "")
        if args[1] == "add-generic-password":
            assert args[-1] == "-w"
            values["mailforge-account"] = kwargs["input"]
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[1] == "delete-generic-password":
            values.pop("mailforge-account", None)
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(args)

    resolver = KeychainCredentialResolver(runner=runner)
    resolver.store("keychain:mailforge-account", "rotated-secret")
    assert resolver.resolve("keychain:mailforge-account").secret == "rotated-secret"
    assert resolver.available("keychain:mailforge-account")
    assert (
        LocalCredentialResolver(keychain=resolver).resolve("keychain:mailforge-account").secret
        == "rotated-secret"
    )
    resolver.revoke("keychain:mailforge-account")
    assert not resolver.available("keychain:mailforge-account")
    assert all("com.mailforge.credential" in args for args, _ in calls)
    assert all("keychain:mailforge-account" not in args for args, _ in calls)
    assert all("rotated-secret" not in args for args, _ in calls)


def test_health_reports_stopped_supervisors_and_redacts_job_errors(tmp_path: Path) -> None:
    data = DataDirectory.from_path(tmp_path / "data")
    data.initialize()
    store = CampaignStore(data.database)
    now = datetime.now(UTC).isoformat()
    with store.transaction(immediate=True) as connection:
        connection.execute(
            "INSERT INTO list_processing_jobs "
            "(id,source_filename,source_path,source_sha256,profile,dns_mode,status,error_message,"
            "created_at,updated_at,checkpoint_path,job_dir,correlation_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "job-redaction",
                "source.csv",
                "/Volumes/private/source.csv",
                "digest",
                "default",
                "offline",
                "failed",
                "Bearer synthetic-marker at /Volumes/private/source.csv",
                now,
                now,
                "checkpoint",
                "job-dir",
                "correlation",
            ),
        )
    service = OperationalAssuranceService(store, data)
    health = service.health(
        worker_status={"scheduler": "stopped", "automation_scheduler": "running"}
    )
    assert health["state"] == "action_required"
    assert any(check["key"] == "supervisors" for check in health["checks"])
    export = json.dumps(service.diagnostic_export())
    assert "synthetic-marker" not in export
    assert "/Volumes/private" not in export


def test_health_exposes_source_failures_and_reopens_persistent_incidents(tmp_path: Path) -> None:
    data = DataDirectory.from_path(tmp_path / "data")
    data.initialize()
    store = CampaignStore(data.database)
    service = OperationalAssuranceService(store, data)

    original_connect = store.connect

    def unavailable_connect(*args, **kwargs):
        raise sqlite3.OperationalError("database temporarily unavailable")

    store.connect = unavailable_connect  # type: ignore[method-assign]
    failed = service.health()
    assert failed["state"] in {"unavailable", "action_required", "degraded"}
    source_check = next(check for check in failed["checks"] if check["key"] == "source_evidence")
    assert source_check["state"] == "action_required"
    assert any(item["source"] == "campaigns" for item in source_check["failures"])
    store.connect = original_connect  # type: ignore[method-assign]
    service._persist_incidents(failed)
    recovered = service.health()
    assert not any(check["key"] == "source_evidence" for check in recovered["checks"])
    resolved = service.incidents(include_acknowledged=True)
    source_incident = next(
        item for item in resolved["items"] if item.get("subsystem") == "source_evidence"
    )
    assert source_incident["disposition"] == "resolved"


def test_unknown_storage_does_not_resolve_prior_incident(tmp_path: Path, monkeypatch) -> None:
    data = DataDirectory.from_path(tmp_path / "data").initialize()
    service = OperationalAssuranceService(CampaignStore(data.database), data)
    healthy = service.health()
    action_required = dict(healthy)
    action_required["checks"] = [
        *healthy["checks"],
        {"key": "storage", "state": "action_required", "free_ratio": 0.01},
    ]
    service._persist_incidents(action_required)

    def unavailable_disk_usage(_path):
        raise OSError("storage observation unavailable")

    monkeypatch.setattr(
        "mailforge.application.operational_assurance.shutil.disk_usage",
        unavailable_disk_usage,
    )
    unknown = service.health()
    service._persist_incidents(unknown)
    incidents = service.incidents(include_acknowledged=True)
    storage = next(item for item in incidents["items"] if item.get("subsystem") == "storage")
    assert storage["disposition"] == "open"


def test_active_job_without_heartbeat_is_unknown_not_healthy(tmp_path: Path) -> None:
    data = DataDirectory.from_path(tmp_path / "data").initialize()
    service = OperationalAssuranceService(CampaignStore(data.database), data)
    service._job_rows = lambda **_kwargs: [  # type: ignore[method-assign]
        {"status": "running", "updated_at": datetime.now(UTC).isoformat()}
    ]
    health = service.health()
    workers = next(check for check in health["checks"] if check["key"] == "workers")
    assert workers["state"] == "unknown"
    assert "authoritative heartbeat/progress evidence is unavailable" in workers["evidence"]


def test_active_job_remains_unknown_with_running_supervisors(tmp_path: Path) -> None:
    data = DataDirectory.from_path(tmp_path / "data").initialize()
    service = OperationalAssuranceService(CampaignStore(data.database), data)
    service._job_rows = lambda **_kwargs: [  # type: ignore[method-assign]
        {"status": "running", "updated_at": datetime.now(UTC).isoformat()}
    ]
    health = service.health(
        worker_status={
            "scheduler": "running",
            "automation_scheduler": "running",
            "campaigns_active": 0,
        }
    )
    workers = next(check for check in health["checks"] if check["key"] == "workers")
    assert workers["state"] == "unknown"
    assert health["state"] == "action_required"  # no verified backup exists


def test_unknown_required_evidence_cannot_aggregate_to_healthy(
    tmp_path: Path, monkeypatch
) -> None:
    data = DataDirectory.from_path(tmp_path / "data").initialize()
    service = OperationalAssuranceService(CampaignStore(data.database), data)
    service._job_rows = lambda **_kwargs: []  # type: ignore[method-assign]
    service._all_counts = lambda *_args, **_kwargs: {}  # type: ignore[method-assign]
    service._backup_observation = lambda **_kwargs: (datetime.now(UTC), 1)  # type: ignore[method-assign]

    def unavailable_disk_usage(_path):
        raise OSError("storage observation unavailable")

    monkeypatch.setattr(
        "mailforge.application.operational_assurance.shutil.disk_usage",
        unavailable_disk_usage,
    )
    health = service.health(
        worker_status={"scheduler": "running", "automation_scheduler": "running"}
    )
    storage = next(check for check in health["checks"] if check["key"] == "storage")
    assert storage["state"] == "unknown"
    assert health["state"] == "unknown"


def test_web_health_uses_worker_status_without_overriding_job_evidence(tmp_path: Path) -> None:
    app = create_app(tmp_path / "state.sqlite3", allowed_hosts={"testserver"})
    runtime = app.state.runtime
    create_backup(runtime.data_directory)
    runtime.operational_assurance._job_rows = lambda **_kwargs: [  # type: ignore[method-assign]
        {"status": "running", "updated_at": datetime.now(UTC).isoformat()}
    ]
    with TestClient(app) as client:
        client.get("/api/v1/session")
        health = client.get("/api/v1/operations/health")
    workers = next(check for check in health.json()["checks"] if check["key"] == "workers")
    assert workers["state"] == "unknown"
    assert health.json()["state"] == "unknown"
