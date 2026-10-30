from __future__ import annotations

import json
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from mailforge.application.local_application import DataDirectory
from mailforge.application.operational_assurance import OperationalAssuranceService
from mailforge.infrastructure.campaign_store import CampaignStore
from mailforge.infrastructure.credentials import KeychainCredentialResolver
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
            values["mailforge-account"] = args[-1]
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[1] == "delete-generic-password":
            values.pop("mailforge-account", None)
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(args)

    resolver = KeychainCredentialResolver(runner=runner)
    resolver.store("keychain:mailforge-account", "rotated-secret")
    assert resolver.resolve("keychain:mailforge-account").secret == "rotated-secret"
    assert resolver.available("keychain:mailforge-account")
    resolver.revoke("keychain:mailforge-account")
    assert not resolver.available("keychain:mailforge-account")
    assert all("com.mailforge.credential" in args for args, _ in calls)
    assert all("keychain:mailforge-account" not in args for args, _ in calls)
