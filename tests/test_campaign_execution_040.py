from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from tests.test_provider_interoperability import _SMTPServer, _ServerContext, _certificate
from mailforge.interfaces.web import create_app


def _csrf(client: TestClient) -> dict[str, str]:
    session = client.get("/api/v1/session").json()
    return {"x-mailforge-csrf": session["csrf_token"]}


def test_campaign_executes_complete_loopback_smtp_lifecycle(monkeypatch, tmp_path: Path):
    cert, key = _certificate(tmp_path, common_name="127.0.0.1", san="IP:127.0.0.1")
    smtp = _SMTPServer(cert, key)
    monkeypatch.setenv("MAILFORGE_ENABLE_LOCAL_FIXTURES", "1")
    monkeypatch.setenv("MAILFORGE_LOCAL_CAPTURE_ENDPOINT", f"127.0.0.1:{smtp.server_address[1]}")
    monkeypatch.setenv("MAILFORGE_CAPTURE_PASSWORD", "synthetic-capture-password")

    with _ServerContext(smtp), TestClient(
        create_app(
            tmp_path / "campaign.sqlite3",
            allowed_hosts={"testserver", "127.0.0.1", "localhost", "::1"},
        )
    ) as client:
        headers = _csrf(client)
        list_id = client.post("/api/v1/lists", headers=headers, json={"name": "Loopback"}).json()["id"]
        imported = client.post(
            f"/api/v1/imports?list_id={list_id}",
            headers=headers,
            files={"file": ("audience.txt", b"one@sandbox.example.com\ntwo@sandbox.example.com\n", "text/plain")},
        )
        assert imported.status_code == 200
        provider = client.post(
            "/api/v1/providers",
            headers=headers,
            json={
                "ref": "loopback-campaign",
                "kind": "smtp",
                "host": "127.0.0.1",
                "port": smtp.server_address[1],
                "username": "fixture-user",
                "credential_ref": "env:MAILFORGE_CAPTURE_PASSWORD",
                "tls_mode": "starttls",
                "auth_method": "password",
                "ca_file": str(cert),
                "sender_email": "sender@sandbox.example.com",
                "sandbox_key": "local_capture_only",
            },
        )
        assert provider.status_code == 200, provider.text
        campaign = client.post(
            "/api/v1/campaigns",
            headers=headers,
            json={
                "name": "Loopback campaign",
                "list_id": list_id,
                "provider_account_ref": "loopback-campaign",
                "sender_email": "sender@sandbox.example.com",
                "subject": "Loopback campaign",
                "text": "Hello {{ email }}",
            },
        )
        assert campaign.status_code == 200, campaign.text
        campaign_id = campaign.json()["id"]
        assert client.post(f"/api/v1/campaigns/{campaign_id}/prepare", headers=headers, json={}).status_code == 200
        pacing = client.put(
            f"/api/v1/campaigns/{campaign_id}/schedule",
            headers=headers,
            json={"mode": "immediate", "timezone": "UTC", "pacing_seconds": 0.05},
        )
        assert pacing.status_code == 200, pacing.text
        assert client.post(f"/api/v1/campaigns/{campaign_id}/authorize", headers=headers).status_code == 200
        started = client.post(
            f"/api/v1/campaigns/{campaign_id}/execute",
            headers=headers,
            json={"mode": "loopback_smtp"},
        )
        assert started.status_code == 200, started.text

        final = None
        for _ in range(120):
            final = client.get(f"/api/v1/campaigns/{campaign_id}/progress").json()
            if final["campaign_status"] == "completed":
                break
            time.sleep(0.025)
        assert final is not None
        assert final["campaign_status"] == "completed", final
        assert final["counts"]["accepted"] == 2
        assert len(smtp.received_messages) == 2
        workspace = client.get(f"/api/v1/campaigns/{campaign_id}/workspace").json()
        assert workspace["execution"]["mode"] == "loopback_smtp"
        assert workspace["execution"]["last_transition"]["reason"] == "simulation_completed"

