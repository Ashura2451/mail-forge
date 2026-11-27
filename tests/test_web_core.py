from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from mailforge.interfaces.web import create_app


def _client(tmp_path: Path) -> tuple[TestClient, dict[str, str]]:
    client = TestClient(create_app(tmp_path / "mailforge.sqlite3", allowed_hosts={"testserver"}))
    client.__enter__()
    session = client.get("/api/v1/session")
    return client, {"x-mailforge-csrf": session.json()["csrf_token"]}


def test_local_session_and_retired_routes_are_not_registered(tmp_path: Path) -> None:
    client, headers = _client(tmp_path)
    try:
        assert client.post("/api/v1/lists", json={"name": "blocked"}).status_code == 403
        assert client.get("/api/v1/providers/legacy/mailboxes").status_code == 404
        assert client.get("/api/v1/status", headers={"host": "not-loopback"}).status_code == 400
        assert client.get("/api/v1/session").status_code == 200
    finally:
        client.__exit__(None, None, None)


def test_setup_backup_and_direct_campaign_lifecycle(tmp_path: Path) -> None:
    client, headers = _client(tmp_path)
    try:
        assert (
            client.post(
                "/api/v1/setup/complete",
                headers=headers,
                json={"timezone": "UTC", "simulation_mode": True, "sample_data": False},
            ).status_code
            == 200
        )
        list_id = client.post(
            "/api/v1/lists", headers=headers, json={"name": "Synthetic audience"}
        ).json()["id"]
        upload = {"file": ("recipients.txt", b"alice@example.com\nbob@example.com", "text/plain")}
        assert (
            client.post(
                f"/api/v1/imports?list_id={list_id}", headers=headers, files=upload
            ).status_code
            == 200
        )
        campaign = client.post(
            "/api/v1/campaigns",
            headers=headers,
            json={"name": "Synthetic send", "list_id": list_id},
        ).json()
        campaign_id = campaign["id"]
        assert (
            client.post(
                f"/api/v1/campaigns/{campaign_id}/prepare", headers=headers, json={}
            ).status_code
            == 200
        )
        assert (
            client.post(f"/api/v1/campaigns/{campaign_id}/authorize", headers=headers).status_code
            == 200
        )
        assert (
            client.post(f"/api/v1/campaigns/{campaign_id}/simulate", headers=headers).status_code
            == 200
        )
        for _ in range(40):
            progress = client.get(f"/api/v1/campaigns/{campaign_id}/progress").json()
            if progress["campaign_status"] == "completed":
                break
            time.sleep(0.025)
        assert progress["campaign_status"] == "completed"
        assert progress["counts"]["accepted"] == 2
        assert client.post("/api/v1/runtime/backup", headers=headers).status_code == 200
    finally:
        client.__exit__(None, None, None)
