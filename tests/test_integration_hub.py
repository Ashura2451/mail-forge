from __future__ import annotations

import hashlib
import hmac
import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mailforge.application.data_connections import DataConnectionError, DataConnectionsService
from mailforge.application.integration_hub import (
    IntegrationHubError,
    IntegrationHubService,
    WebhookResponse,
)
from mailforge.infrastructure.campaign_store import CampaignStore
from mailforge.infrastructure.credentials import MappingCredentialResolver
from mailforge.interfaces.web import create_app

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


def _resolver() -> MappingCredentialResolver:
    return MappingCredentialResolver({"test:webhook": "synthetic-webhook-secret"})


def _event(
    event_id: str = "event-1", *, payload: dict[str, object] | None = None
) -> dict[str, object]:
    return {
        "event_id": event_id,
        "source": "fixture.source",
        "type": "external_dataset_available",
        "time": NOW.isoformat(),
        "schema_version": "1",
        "payload": payload or {"dataset_ref": "dataset-1", "records": 2},
        "provenance": {"source_kind": "synthetic_fixture", "observation_ref": "obs-1"},
    }


def test_schema_22_and_client_event_idempotency(tmp_path: Path) -> None:
    store = CampaignStore(tmp_path / "state.sqlite3")
    service = IntegrationHubService(store, resolver=_resolver(), now_fn=lambda: NOW)
    issued = service.issue_client(
        "fixture client", ["INGEST_LOCAL_EVENT"], expires_at=(NOW + timedelta(days=7)).isoformat()
    )
    assert issued["token"].startswith("mf_int_")
    with store.connect() as connection:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        stored = connection.execute(
            "SELECT token_hash,token_prefix FROM integration_clients"
        ).fetchone()
    assert {"integration_clients", "integration_events", "integration_webhook_deliveries"} <= tables
    assert issued["token"] not in stored["token_hash"]
    assert stored["token_prefix"] == issued["token"][:16]

    client = service.authenticate(issued["token"], "INGEST_LOCAL_EVENT")
    accepted = service.submit_event(client, _event())
    duplicate = service.submit_event(client, _event())
    conflict = service.submit_event(client, _event(payload={"dataset_ref": "different"}))
    assert accepted["outcome"] == "accepted"
    assert duplicate["outcome"] == "duplicate"
    assert conflict["outcome"] == "conflict"
    assert service.diagnostics()["events"]["accepted"] == 1
    assert service.diagnostics()["events"]["duplicate"] == 1

    rotated = service.rotate_client(
        issued["client_ref"], expires_at=(NOW + timedelta(days=8)).isoformat()
    )
    with pytest.raises(IntegrationHubError, match="invalid"):
        service.authenticate(issued["token"], "INGEST_LOCAL_EVENT")
    assert (
        service.authenticate(rotated["token"], "INGEST_LOCAL_EVENT")["client_ref"]
        == (issued["client_ref"])
    )
    service.revoke_client(issued["client_ref"])
    with pytest.raises(IntegrationHubError, match="revoked"):
        service.authenticate(rotated["token"], "INGEST_LOCAL_EVENT")


class _PagedHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/records?page=2"):
            body = json.dumps({"records": [{"id": 2, "email": "two@example.com"}]}).encode()
        else:
            body = json.dumps({"records": [{"id": 1, "email": "one@example.com"}]}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        return


def test_http_source_is_bounded_and_preserves_provenance(tmp_path: Path) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PagedHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    store = CampaignStore(tmp_path / "state.sqlite3")
    service = DataConnectionsService(
        store,
        tmp_path / "artifacts",
        allow_local_fixtures=True,
    )
    try:
        origin = f"http://127.0.0.1:{server.server_port}"
        profile = service.create_connection(
            "HTTP fixture",
            "http",
            {
                "origin": origin,
                "path": "/records",
                "format": "json",
                "query": {"page": "1"},
                "pagination": {"mode": "single", "page_size": 10},
                "allow_local_fixture": True,
            },
        )
        preview = service.preview(profile["id"], limit=10)
        assert preview["rows"][0]["email"] == "one@example.com"
        assert preview["rows"][0]["__provenance"]["source_type"] == "http"
        assert preview["rows"][0]["__source_id"]
        with pytest.raises(DataConnectionError, match="HTTPS"):
            service.create_connection(
                "External HTTP",
                "http",
                {"origin": "http://external.example", "path": "/records"},
            )
    finally:
        service.close()
        server.shutdown()
        server.server_close()


def test_webhook_signature_retry_and_terminal_state(tmp_path: Path) -> None:
    calls: list[tuple[str, bytes]] = []
    statuses = iter([WebhookResponse(503), WebhookResponse(200)])

    def transport(
        endpoint: str, headers: dict[str, str], body: bytes, timeout: float
    ) -> WebhookResponse:
        calls.append((headers["x-mailforge-signature"], body))
        return next(statuses)

    store = CampaignStore(tmp_path / "state.sqlite3")
    service = IntegrationHubService(
        store,
        resolver=_resolver(),
        webhook_transport=transport,
        now_fn=lambda: NOW,
        allow_local_fixtures=True,
    )
    subscription = service.create_subscription(
        "fixture webhook",
        "http://127.0.0.1:9/hook",
        ["list_processing_completed"],
        secret_ref="test:webhook",
        enabled=True,
        local_fixture=True,
    )
    with store.transaction(immediate=True) as connection:
        store.record_event(
            connection,
            "list",
            "list-1",
            "list_processing_completed",
            {"job_ref": "job-1", "records": 2},
        )
    first = service.process_due_deliveries()
    assert first[0]["state"] == "retrying"
    with store.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE integration_webhook_deliveries SET next_due_at=?", (NOW.isoformat(),)
        )
    second = service.process_due_deliveries()
    assert second[0]["state"] == "completed"
    signature, body = calls[-1]
    timestamp = str(int(NOW.timestamp()))
    expected = hmac.new(
        b"synthetic-webhook-secret", f"{timestamp}.".encode() + body, hashlib.sha256
    ).hexdigest()
    assert signature == f"v1={expected}"
    assert (
        IntegrationHubService.verify_signature(
            "synthetic-webhook-secret", body, timestamp=timestamp, signature=signature, now=NOW
        )
        is True
    )
    assert service.subscription(subscription["subscription_ref"])["last_success_at"] == (
        NOW.isoformat()
    )


def test_bearer_event_route_does_not_use_browser_session(tmp_path: Path) -> None:
    client = TestClient(
        create_app(tmp_path / "state.sqlite3", allowed_hosts={"testserver", "localhost"})
    )
    assert client.get("/api/v1/integrations").status_code == 401
    session = client.get("/api/v1/session")
    assert session.status_code == 200
    assert (
        client.post(
            "/api/v1/integrations/events",
            json=_event(),
        ).status_code
        == 401
    )
