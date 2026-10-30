"""Acceptance checks for the local browser API boundary."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mailforge.domain.campaign import CampaignStatus
from mailforge.interfaces.process_lock import DatabaseProcessLock, ProcessLockError
from mailforge.interfaces.web import WebRuntime, create_app


def _client(tmp_path: Path, request: pytest.FixtureRequest) -> tuple[TestClient, dict[str, str]]:
    client = TestClient(
        create_app(
            tmp_path / "mailforge.sqlite3",
            allowed_hosts={"testserver", "127.0.0.1", "localhost", "::1"},
        )
    )
    client.__enter__()
    request.addfinalizer(lambda: client.__exit__(None, None, None))
    session = client.get("/api/v1/session")
    return client, {"x-mailforge-csrf": session.json()["csrf_token"]}


def test_local_session_requires_csrf_and_rejects_non_loopback_host(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    client, headers = _client(tmp_path, request)
    assert client.post("/api/v1/lists", json={"name": "blocked"}).status_code == 403
    assert (
        client.post(
            "/api/v1/lists",
            headers={**headers, "origin": "https://attacker.invalid"},
            json={"name": "blocked"},
        ).status_code
        == 403
    )
    assert client.get("/api/v1/status", headers={"host": "evil.example"}).status_code == 400


def test_runtime_setup_health_and_application_backup_routes(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    client, headers = _client(tmp_path, request)
    health = client.get("/healthz")
    setup = client.get("/api/v1/setup")
    assert health.status_code == 200
    assert health.json()["loopback_only"] is True
    assert health.json()["version"]
    assert setup.json()["complete"] is False

    completed = client.post(
        "/api/v1/setup/complete",
        headers=headers,
        json={"timezone": "UTC", "simulation_mode": True, "sample_data": False},
    )
    assert completed.status_code == 200
    assert completed.json()["complete"] is True

    backup = client.post("/api/v1/runtime/backup", headers=headers)
    assert backup.status_code == 200
    assert backup.json()["metadata"]["valid"] is True
    backups = client.get("/api/v1/runtime/backups")
    assert backups.status_code == 200
    assert backups.json()["items"][0]["valid"] is True


def test_local_api_runs_contact_campaign_lifecycle(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    client, headers = _client(tmp_path, request)
    created = client.post("/api/v1/lists", headers=headers, json={"name": "Audience"})
    assert created.status_code == 200
    list_id = created.json()["id"]

    upload = {
        "file": (
            "audience.txt",
            b"alice@example.com\nbob@example.com\nalice@example.com\nnot-an-email",
            "text/plain",
        )
    }
    preview = client.post("/api/v1/imports/preview", headers=headers, files=upload)
    assert preview.json()["accepted"] == 2
    assert preview.json()["duplicates"] == 1
    imported = client.post(f"/api/v1/imports?list_id={list_id}", headers=headers, files=upload)
    assert imported.status_code == 200
    assert imported.json()["unique_contacts"] == 2

    campaign = client.post(
        "/api/v1/campaigns",
        headers=headers,
        json={"name": "Synthetic acceptance", "list_id": list_id},
    )
    campaign_id = campaign.json()["id"]
    prepared = client.post(
        f"/api/v1/campaigns/{campaign_id}/prepare",
        headers=headers,
        json={"sender_email": "sender@example.com", "subject": "Test"},
    )
    assert prepared.status_code == 200
    assert prepared.json()["prepared"] == 2
    assert (
        client.post(f"/api/v1/campaigns/{campaign_id}/authorize", headers=headers).status_code
        == 200
    )
    assert (
        client.post(f"/api/v1/campaigns/{campaign_id}/simulate", headers=headers).status_code == 200
    )

    for _ in range(20):
        progress = client.get(f"/api/v1/campaigns/{campaign_id}/progress").json()
        if progress["campaign_status"] == "completed":
            break
        time.sleep(0.025)
    assert progress["campaign_status"] == "completed"
    assert progress["counts"]["accepted"] == 2
    assert client.get("/api/v1/activity").json()["items"]


def test_local_api_data_connection_upload_preview_and_snapshot(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    client, headers = _client(tmp_path, request)
    source = tmp_path / "contacts.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE contacts (id INTEGER PRIMARY KEY, email TEXT)")
        connection.executemany(
            "INSERT INTO contacts(email) VALUES (?)",
            [("one@example.com",), ("two@example.com",)],
        )

    with source.open("rb") as handle:
        created = client.post(
            "/api/v1/data-connections/sources",
            headers=headers,
            data={"profile_name": "Uploaded contacts", "source_type": "sqlite"},
            files={"file": (source.name, handle, "application/vnd.sqlite3")},
        )
    assert created.status_code == 200
    profile_id = created.json()["id"]
    assert "source_path" not in created.text

    schema = client.get(f"/api/v1/data-connections/{profile_id}/schema")
    assert schema.status_code == 200
    assert schema.json()["objects"][0]["name"] == "contacts"
    preview = client.post(
        f"/api/v1/data-connections/{profile_id}/preview",
        headers=headers,
        json={"selection": {"table": "contacts"}, "limit": 1},
    )
    assert preview.status_code == 200
    assert len(preview.json()["rows"]) == 1
    snapshot = client.post(
        f"/api/v1/data-connections/{profile_id}/snapshots",
        headers=headers,
        json={"selection": {"table": "contacts"}, "idempotency_key": "web-1", "wait": True},
    )
    assert snapshot.status_code == 200
    assert snapshot.json()["status"] == "completed"
    assert snapshot.json()["record_count"] == 2


def test_local_api_exposes_operational_reports_with_bounded_exports(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    client, _headers = _client(tmp_path, request)

    definitions = client.get("/api/v1/reports/definitions")
    summary = client.get("/api/v1/reports/summary?preset=last_7_days&timezone=UTC")
    exported = client.get("/api/v1/reports/export?report_type=summary&format=csv")
    invalid = client.get("/api/v1/reports/summary?preset=custom&start=2020-01-01&end=2022-01-01")

    assert definitions.status_code == 200
    assert definitions.json()["version"] == "022.1"
    assert summary.status_code == 200
    assert summary.json()["evidence"]["simulated"] is True
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("text/csv")
    assert "attachment" in exported.headers["content-disposition"]
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "range_too_large"


def test_local_api_supports_typed_segments_and_recurring_snapshots(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    client, headers = _client(tmp_path, request)
    list_id = client.post("/api/v1/lists", headers=headers, json={"name": "Segments"}).json()["id"]
    field = client.post(
        "/api/v1/fields",
        headers=headers,
        json={"machine_key": "plan", "label": "Plan", "field_type": "TEXT"},
    )
    assert field.status_code == 200
    field_id = field.json()["id"]
    invalid_preview = client.post(
        "/api/v1/imports/structured/preview",
        headers=headers,
        json={
            "list_id": list_id,
            "rows": [{"email": "not-an-email", "plan": "pro"}],
            "mappings": {"email": "contact.email", "plan": field_id},
        },
    )
    assert invalid_preview.status_code == 200
    assert invalid_preview.json()["valid"] is False
    imported = client.post(
        "/api/v1/imports/structured",
        headers=headers,
        json={
            "list_id": list_id,
            "rows": [{"email": "a@example.com", "plan": "pro"}],
            "mappings": {"email": "contact.email", "plan": field_id},
        },
    )
    assert imported.status_code == 200
    definition = {
        "version": 1,
        "root": {
            "kind": "PREDICATE",
            "field_id": field_id,
            "operator": "EQUALS",
            "value": "pro",
        },
    }
    segment = client.post(
        "/api/v1/segments", headers=headers, json={"name": "Pro", "definition": definition}
    )
    assert segment.status_code == 200
    segment_body = segment.json()
    campaign = client.post(
        "/api/v1/campaigns",
        headers=headers,
        json={
            "name": "Segment campaign",
            "segment_id": segment_body["id"],
            "segment_revision_id": segment_body["revision"]["id"],
        },
    )
    assert campaign.status_code == 200
    campaign_id = campaign.json()["id"]
    prepared = client.post(f"/api/v1/campaigns/{campaign_id}/prepare", headers=headers, json={})
    assert prepared.status_code == 200
    assert (
        client.get(f"/api/v1/campaigns/{campaign_id}/audience").json()["audience"]["status"]
        == "prepared"
    )
    recurring = client.post(
        f"/api/v1/campaigns/{campaign_id}/prepare",
        headers=headers,
        json={"run_key": "occurrence-1"},
    )
    assert recurring.status_code == 200
    assert recurring.json()["audience"]["run_key"] == "occurrence-1"
    assert (
        len(
            client.get(f"/api/v1/campaigns/{campaign_id}/audience?run_key=occurrence-1").json()[
                "items"
            ]
        )
        == 1
    )
    type_change = client.patch(
        f"/api/v1/fields/{field_id}", headers=headers, json={"field_type": "NUMBER"}
    )
    assert type_change.status_code == 400
    assert type_change.json()["error"]["code"] == "field_type_migration_required"


def test_data_studio_api_shares_pinned_recipe_preview_and_job_authority(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    client, headers = _client(tmp_path, request)
    definition = {
        "steps": [
            {"type": "normalize", "fields": ["email"], "operation": "email"},
            {"type": "dedupe", "fields": ["email"]},
        ]
    }
    recipe = client.post(
        "/api/v1/data-studio/recipes",
        headers=headers,
        json={"name": "API recipe", "definition": definition},
    )
    assert recipe.status_code == 200
    source = {
        "file": ("contacts.csv", b"email\nALICE@example.com\nalice@example.com\n", "text/csv")
    }
    schema = client.post("/api/v1/data-studio/schema", headers=headers, files=source)
    assert schema.status_code == 200
    assert schema.json()["fields"] == ["email"]
    preview = client.post(
        "/api/v1/data-studio/preview",
        headers=headers,
        data={"definition": json.dumps(definition)},
        files=source,
    )
    assert preview.status_code == 200
    assert preview.json()["counts"]["removed"] == 1
    job = client.post(
        f"/api/v1/data-studio/jobs?recipe_revision_id={recipe.json()['current_revision_id']}",
        headers=headers,
        files=source,
    )
    assert job.status_code == 200
    started = client.post(f"/api/v1/data-studio/jobs/{job.json()['id']}/start", headers=headers)
    assert started.status_code == 200
    for _ in range(50):
        detail = client.get(f"/api/v1/data-studio/jobs/{job.json()['id']}").json()
        if detail["status"] not in {"pending", "running"}:
            break
        time.sleep(0.02)
    assert detail["status"] == "completed"
    assert {item["kind"] for item in detail["artifacts"]} == {"output", "errors"}


def test_data_studio_connected_destinations_are_explicit_and_snapshot_pinned(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    client, headers = _client(tmp_path, request)
    definition = {
        "output": {"format": "jsonl"},
        "steps": [{"type": "normalize", "fields": ["email"], "operation": "email"}],
    }
    recipe = client.post(
        "/api/v1/data-studio/recipes",
        headers=headers,
        json={"name": "Connected recipe", "definition": definition},
    )
    assert recipe.status_code == 200
    sources = [
        ("left.csv", b"email\nada@example.com\n", "text/csv"),
        ("right.csv", b"email\nbob@example.com\n", "text/csv"),
    ]
    multi_preview = client.post(
        "/api/v1/data-studio/preview-multi",
        headers=headers,
        data={"definition": json.dumps({"combine": {"operation": "append"}, "steps": []})},
        files=[("files", source) for source in sources],
    )
    assert multi_preview.status_code == 200
    assert multi_preview.json()["counts"]["output"] == 2
    job_response = client.post(
        f"/api/v1/data-studio/jobs-multi?recipe_revision_id={recipe.json()['current_revision_id']}",
        headers=headers,
        files=[("files", source) for source in sources],
    )
    assert job_response.status_code == 200
    job_id = job_response.json()["id"]
    assert (
        client.post(f"/api/v1/data-studio/jobs/{job_id}/start", headers=headers).status_code == 200
    )
    for _ in range(100):
        detail = client.get(f"/api/v1/data-studio/jobs/{job_id}").json()
        if detail["status"] not in {"pending", "running"}:
            break
        time.sleep(0.02)
    assert detail["status"] == "completed"
    output = next(item for item in detail["artifacts"] if item["kind"] == "output")

    list_id = client.post(
        "/api/v1/lists", headers=headers, json={"name": "Data Studio destination"}
    ).json()["id"]
    import_preview = client.post(
        f"/api/v1/data-studio/jobs/{job_id}/artifacts/{output['id']}/contacts/preview",
        headers=headers,
        json={"list_id": list_id, "mappings": {"email": "contact.email"}},
    )
    assert import_preview.status_code == 200
    assert import_preview.json()["valid"] is True
    imported = client.post(
        f"/api/v1/data-studio/jobs/{job_id}/artifacts/{output['id']}/contacts",
        headers=headers,
        json={"list_id": list_id, "mappings": {"email": "contact.email"}},
    )
    assert imported.status_code == 200
    assert imported.json()["created"] == 2
    contact_id = client.get("/api/v1/contacts").json()["items"][0]["id"]

    document = client.post(
        "/api/v1/message-studio/documents",
        headers=headers,
        json={"name": "Connected message", "kind": "message"},
    ).json()
    published = client.post(
        f"/api/v1/message-studio/documents/{document['id']}/publish", headers=headers
    )
    assert published.status_code == 200
    revision_id = published.json()["published"]["id"]
    message_preview = client.post(
        f"/api/v1/data-studio/jobs/{job_id}/artifacts/{output['id']}/message-preview",
        headers=headers,
        json={
            "revision_id": revision_id,
            "contact_id": contact_id,
            "field_mapping": {"email": "email"},
        },
    )
    assert message_preview.status_code == 200
    assert message_preview.json()["snapshot"]["revision_id"] == revision_id

    campaign = client.post(
        "/api/v1/campaigns",
        headers=headers,
        json={
            "name": "Data Studio prepared batch",
            "list_id": list_id,
            "message_revision_id": revision_id,
            "sender_email": "sender@example.com",
        },
    )
    assert campaign.status_code == 200
    prepared = client.post(
        f"/api/v1/campaigns/{campaign.json()['id']}/prepare-data-studio",
        headers=headers,
        json={
            "source_job_id": job_id,
            "source_artifact_id": output["id"],
            "recipe_revision_id": recipe.json()["current_revision_id"],
            "message_revision_id": revision_id,
            "field_mapping": {"email": "email"},
        },
    )
    assert prepared.status_code == 200
    assert prepared.json()["data_snapshot"]["artifact_sha256"] == output["sha256"]
    assert prepared.json()["prepared"] == 2

    assert (
        client.post(
            "/api/v1/data-studio/extensions/email_domain_normalizer/register", headers=headers
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/v1/data-studio/extensions/email_domain_normalizer/enable", headers=headers
        ).json()["enabled"]
        == 1
    )


def test_compiled_frontend_is_served(tmp_path: Path) -> None:
    frontend = Path(__file__).parents[1] / "src" / "mailforge" / "web_assets"
    client = TestClient(
        create_app(
            tmp_path / "mailforge.sqlite3",
            frontend_dir=frontend,
            allowed_hosts={"testserver"},
        )
    )
    response = client.get("/")
    assert response.status_code == 200
    assert "Mailforge" in response.text
    assert response.headers["content-security-policy"].startswith("default-src 'self'")


def test_provider_grant_http_boundary_rejects_changed_request_before_transport(
    tmp_path: Path, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = _client(tmp_path, request)
    account = client.post(
        "/api/v1/providers",
        headers=headers,
        json={
            "ref": "http-provider",
            "kind": "smtp",
            "host": "127.0.0.1",
            "port": 465,
            "username": "sender@example.com",
            "credential_ref": "test:provider",
            "tls_mode": "implicit_tls",
            "auth_method": "password",
            "sender_email": "sender@example.com",
        },
    )
    assert account.status_code == 200
    runtime = client.app.state.runtime
    transport_calls: list[object] = []
    monkeypatch.setattr(
        runtime.provider_operations.providers,
        "smtp_pool",
        lambda *args, **kwargs: transport_calls.append((args, kwargs)),
    )
    expiry = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    grant = client.post(
        "/api/v1/providers/http-provider/grants",
        headers=headers,
        json={
            "purpose": "http boundary fixture",
            "sender": "sender@example.com",
            "recipient": "owned@example.com",
            "expires_at": expiry,
        },
    )
    assert grant.status_code == 200
    authorization_ref = grant.json()["authorization_ref"]
    changed = client.post(
        "/api/v1/providers/http-provider/test-send",
        headers=headers,
        json={
            "sender": "sender@example.com",
            "recipient": "other@example.com",
            "message": "Subject: fixture\r\n\r\nbody",
            "idempotency_key": "http-boundary-key",
            "authorization_ref": authorization_ref,
            "purpose": "http boundary fixture",
            "expires_at": expiry,
        },
    )
    invented = client.post(
        "/api/v1/providers/http-provider/test-send",
        headers=headers,
        json={
            "sender": "sender@example.com",
            "recipient": "owned@example.com",
            "message": "Subject: fixture\r\n\r\nbody",
            "idempotency_key": "http-invented-key",
            "authorization_ref": "invented-authorization",
            "purpose": "http boundary fixture",
            "expires_at": expiry,
        },
    )
    assert changed.status_code == 400
    assert invented.status_code == 400
    assert transport_calls == []


def test_campaign_preview_outcomes_and_resume_are_versioned(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    client, headers = _client(tmp_path, request)
    list_id = client.post("/api/v1/lists", headers=headers, json={"name": "Preview"}).json()["id"]
    campaign_id = client.post(
        "/api/v1/campaigns", headers=headers, json={"name": "Preview", "list_id": list_id}
    ).json()["id"]
    assert client.get(f"/api/v1/campaigns/{campaign_id}/preview").status_code == 200
    assert client.get(f"/api/v1/campaigns/{campaign_id}/outcomes").status_code == 200
    assert (
        client.post(f"/api/v1/campaigns/{campaign_id}/resume", headers=headers).status_code == 400
    )


def test_inbound_eml_import_and_review_updates_eligibility(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    client, headers = _client(tmp_path, request)
    list_id = client.post("/api/v1/lists", headers=headers, json={"name": "Inbound"}).json()["id"]
    upload = {"file": ("audience.txt", b"alice@example.com\n", "text/plain")}
    client.post(f"/api/v1/imports?list_id={list_id}", headers=headers, files=upload)
    campaign_id = client.post(
        "/api/v1/campaigns", headers=headers, json={"name": "Inbound", "list_id": list_id}
    ).json()["id"]
    assert (
        client.post(f"/api/v1/campaigns/{campaign_id}/prepare", headers=headers).status_code == 200
    )
    preview = client.get(f"/api/v1/campaigns/{campaign_id}/preview").json()
    message_id = preview["messages"][0]["message_id"]

    message = EmailMessage()
    message["From"] = "alice@example.com"
    message["To"] = "sender@example.com"
    message["Subject"] = "unsubscribe"
    message["In-Reply-To"] = message_id
    message.set_content("Please unsubscribe me")
    inbound = {"file": ("unsubscribe.eml", message.as_bytes(), "message/rfc822")}
    imported = client.post("/api/v1/inbound/import", headers=headers, files=inbound)
    assert imported.status_code == 200
    assert imported.json()["report"]["processed"] == 1
    duplicate = client.post("/api/v1/inbound/import", headers=headers, files=inbound)
    assert duplicate.status_code == 200
    assert duplicate.json()["report"]["acquired"] == 0
    assert duplicate.json()["stats"]["messages"] == 1
    review = client.get("/api/v1/inbound").json()
    assert review["items"][0]["action_type"] == "create_unsubscribe"
    assert review["messages"][0]["classification"] == "unsubscribe"
    assert review["messages"][0]["correlations"][0]["match_kind"] == "exact"
    action_id = review["items"][0]["action_id"]
    decided = client.post(
        f"/api/v1/inbound/reviews/{action_id}",
        headers=headers,
        json={"decision": "approve", "reason": "Synthetic operator acceptance"},
    )
    assert decided.status_code == 200
    eligibility = client.get(f"/api/v1/campaigns/{campaign_id}/eligibility").json()["items"]
    assert eligibility[0]["status"] == "excluded"
    assert "unsubscribe" in eligibility[0]["reasons"][0]
    assert client.get("/api/v1/inbound").json()["items"] == []


def test_inbound_workbench_api_supports_detail_rules_extraction_and_drafts(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    client, headers = _client(tmp_path, request)
    message = EmailMessage()
    message["From"] = "alice@example.com"
    message["To"] = "operator@example.com"
    message["Subject"] = "Quarterly question"
    message.set_content("Please review.")
    message.add_attachment(b"fixture", maintype="text", subtype="plain", filename="note.txt")
    imported = client.post(
        "/api/v1/inbound/import",
        headers=headers,
        files={"file": ("message.eml", message.as_bytes(), "message/rfc822")},
    )
    assert imported.status_code == 200
    message_id = client.get("/api/v1/inbound").json()["messages"][0]["id"]
    rule = client.post(
        "/api/v1/inbound/rules",
        headers=headers,
        json={
            "name": "Extract questions",
            "definition": {
                "version": 1,
                "conditions": [{"field": "subject", "op": "contains", "value": "Quarterly"}],
                "actions": [{"type": "extract", "fields": ["sender", "subject"]}],
            },
        },
    )
    assert rule.status_code == 200
    assert (
        client.post(
            f"/api/v1/inbound/rules/{rule.json()['id']}/status",
            headers=headers,
            json={"status": "active"},
        ).status_code
        == 200
    )
    detail = client.get(f"/api/v1/inbound/messages/{message_id}").json()
    assert detail["attachments"][0]["filename"] == "note.txt"
    extracted = client.post(
        f"/api/v1/inbound/messages/{message_id}/extract",
        headers=headers,
        json={"fields": ["sender", "subject"]},
    )
    assert extracted.status_code == 200
    draft = client.post(
        f"/api/v1/inbound/messages/{message_id}/draft",
        headers=headers,
        json={"kind": "reply", "body": "Thanks."},
    )
    assert draft.status_code == 200
    assert draft.json()["provenance"]["message_id"] == message_id


def test_database_process_lock_prevents_two_local_runtimes(tmp_path: Path) -> None:
    database = tmp_path / "runtime.sqlite3"
    first = DatabaseProcessLock(database)
    second = DatabaseProcessLock(database)
    first.acquire()
    try:
        try:
            second.acquire()
        except ProcessLockError as error:
            assert "already owns database" in str(error)
        else:
            raise AssertionError("a second local runtime acquired the database lock")
    finally:
        first.release()
        second.release()


def test_database_process_lock_resolves_database_aliases(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    alias = tmp_path / "alias.sqlite3"
    database.touch()
    alias.symlink_to(database)
    first = DatabaseProcessLock(database)
    second = DatabaseProcessLock(alias)
    first.acquire()
    try:
        with pytest.raises(ProcessLockError):
            second.acquire()
    finally:
        first.release()
        second.release()

    hardlink = tmp_path / "hardlink.sqlite3"
    os.link(database, hardlink)
    first = DatabaseProcessLock(database)
    second = DatabaseProcessLock(hardlink)
    first.acquire()
    try:
        with pytest.raises(ProcessLockError):
            second.acquire()
    finally:
        first.release()
        second.release()


def test_database_process_lock_rejects_a_second_process(tmp_path: Path) -> None:
    database = tmp_path / "cross-process.sqlite3"
    marker = tmp_path / "holder.ready"
    holder_code = (
        "import time; "
        "from pathlib import Path; "
        "from mailforge.interfaces.process_lock import DatabaseProcessLock; "
        f"lock=DatabaseProcessLock(r'{database}'); lock.acquire(); "
        f"Path(r'{marker}').write_text('ready'); time.sleep(2); lock.release()"
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    holder = subprocess.Popen([sys.executable, "-c", holder_code], env=environment)
    try:
        for _ in range(40):
            if marker.is_file():
                break
            time.sleep(0.025)
        assert marker.is_file()
        contender = subprocess.run(
            [
                sys.executable,
                "-c",
                f"from mailforge.interfaces.process_lock import DatabaseProcessLock; "
                f"DatabaseProcessLock(r'{database}').acquire()",
            ],
            env=environment,
            capture_output=True,
            text=True,
        )
        assert contender.returncode != 0
        assert "already owns database" in contender.stderr
    finally:
        holder.terminate()
        holder.wait(timeout=5)


def test_web_runtime_reconciles_running_campaign_after_restart(tmp_path: Path) -> None:
    database = tmp_path / "restart.sqlite3"
    first = WebRuntime(database)
    list_id = first.store.create_list("restart-audience")
    campaign_id = first.store.create_campaign(
        name="restart-campaign",
        description="restart",
        sender_config_ref="sender",
        template_ref="template",
        audience_list_id=list_id,
    )
    first.store.transition_campaign(campaign_id, CampaignStatus.VALIDATING, "test")
    first.store.transition_campaign(campaign_id, CampaignStatus.READY, "test")
    first.store.transition_campaign(campaign_id, CampaignStatus.RUNNING, "test")
    first.close()

    second = WebRuntime(database)
    try:
        assert second.store.get_campaign(campaign_id)["status"] == "paused"
        with second.store.connect() as connection:
            transition = connection.execute(
                "SELECT from_status, to_status, reason FROM campaign_transitions "
                "WHERE campaign_id = ? AND reason = 'application_restarted'",
                (campaign_id,),
            ).fetchall()
        assert [(row["from_status"], row["to_status"]) for row in transition] == [
            ("running", "pausing"),
            ("pausing", "paused"),
        ]
    finally:
        second.close()
