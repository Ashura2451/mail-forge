from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from tests.test_provider_interoperability import _SMTPServer, _ServerContext, _certificate
from mailforge.application.outbound import OutboundService
from mailforge.application.providers import ProviderService
from mailforge.domain.campaign import DeliveryState
from mailforge.domain.outbound import ScheduleMode, ScheduleSpec, SenderPolicy, TemplateDefinition
from mailforge.domain.provider import ProviderKind, ProviderProfile, TLSMode
from mailforge.infrastructure.campaign_store import CampaignStore
from mailforge.infrastructure.credentials import MappingCredentialResolver
from mailforge.infrastructure.smtp import LiveSendGate
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


def test_external_campaign_authorization_binds_prepared_identity_without_contact(
    tmp_path: Path,
):
    with TestClient(
        create_app(
            tmp_path / "external.sqlite3",
            allowed_hosts={"testserver", "127.0.0.1", "localhost", "::1"},
        )
    ) as client:
        headers = _csrf(client)
        list_id = client.post("/api/v1/lists", headers=headers, json={"name": "External"}).json()["id"]
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
                "ref": "external-provider",
                "kind": "smtp",
                "host": "smtp.example.invalid",
                "port": 2525,
                "username": "fixture-user",
                "credential_ref": "env:MAILFORGE_TEST_PASSWORD",
                "tls_mode": "starttls",
                "auth_method": "password",
                "sender_email": "sender@example.com",
            },
        )
        assert provider.status_code == 200, provider.text
        campaign = client.post(
            "/api/v1/campaigns",
            headers=headers,
            json={
                "name": "External authorization draft",
                "list_id": list_id,
                "provider_account_ref": "external-provider",
                "sender_email": "sender@example.com",
                "subject": "External qualification",
                "text": "Hello {{ email }}",
            },
        ).json()["id"]
        assert client.post(f"/api/v1/campaigns/{campaign}/prepare", headers=headers, json={}).status_code == 200
        preview = client.get(f"/api/v1/campaigns/{campaign}/external-authorization-preview")
        assert preview.status_code == 200, preview.text
        record = preview.json()
        assert record["external_access"] is False
        assert record["recipients"] == ["one@sandbox.example.com", "two@sandbox.example.com"]
        expires = (datetime.now(UTC) + timedelta(minutes=30)).isoformat()
        bound = client.post(
            f"/api/v1/campaigns/{campaign}/prepare-external-authorization",
            headers=headers,
            json={
                "purpose": "MAILFORGE-041 bounded synthetic campaign",
                "expires_at": expires,
                "max_submissions": 2,
                "prepared_set_hash": record["prepared_set_hash"],
                "confirm_exact_scope": True,
            },
        )
        assert bound.status_code == 200, bound.text
        workspace = client.get(f"/api/v1/campaigns/{campaign}/workspace").json()
        assert workspace["campaign"]["configuration"]["execution_mode"] == "external_smtp"
        assert workspace["execution"]["mode"] == "external_smtp"


def test_canonical_campaign_pacing_handles_provider_rate_policy(monkeypatch, tmp_path: Path):
    cert, key = _certificate(tmp_path, common_name="127.0.0.1", san="IP:127.0.0.1")
    smtp = _SMTPServer(cert, key, min_data_interval=0.08)
    monkeypatch.setenv("MAILFORGE_ENABLE_LOCAL_FIXTURES", "1")
    monkeypatch.setenv("MAILFORGE_LOCAL_CAPTURE_ENDPOINT", f"127.0.0.1:{smtp.server_address[1]}")
    store = CampaignStore(tmp_path / "pacing.sqlite3")
    store.register_sender_policy(
        SenderPolicy(
            ref="pacing-policy",
            provider="loopback",
            email="sender@sandbox.example.com",
            max_per_minute=60,
            max_per_hour=3_600,
            max_per_day=9_950,
            per_domain_per_minute=60,
        )
    )
    store.register_provider_profile(
        ProviderProfile(
            ref="pacing-loopback",
            kind=ProviderKind.SMTP,
            host="127.0.0.1",
            port=smtp.server_address[1],
            username="fixture-user",
            credential_ref="test:smtp",
            tls_mode=TLSMode.STARTTLS,
            sender_policy_ref="pacing-policy",
            ca_file=str(cert),
            metadata={"sandbox": {"key": "local_capture_only", "mode": "capture_only"}},
        )
    )
    account = store.upsert_provider_account(
        "pacing-loopback", sender_email="sender@sandbox.example.com"
    )
    list_id = store.create_list("pacing audience")
    for index in range(3):
        contact = store.upsert_contact(
            f"pacing-{index}@sandbox.example.com",
            source_file="test",
            source_record_position=index,
            original_input=f"pacing-{index}@sandbox.example.com",
        )
        store.add_to_list(list_id, contact)
    service = OutboundService(store)
    campaign_id = store.create_campaign(
        name="Pacing campaign",
        description="rate-policy fixture",
        sender_config_ref="pacing-policy",
        template_ref="pacing-template",
        audience_list_id=list_id,
    )
    sender = SenderPolicy(ref="pacing-policy", provider="loopback", email="sender@sandbox.example.com", per_domain_per_minute=60)
    with _ServerContext(smtp):
        service.prepare_campaign(
            campaign_id,
            template=TemplateDefinition("pacing-template", "1", "Pacing", "Hello {{ email }}"),
            sender=sender,
            schedule=ScheduleSpec(mode=ScheduleMode.IMMEDIATE, pacing_seconds=0.2),
        )
        service.authorize_simulation(campaign_id)
        report = None
        for _ in range(6):
            report, _ = service.execute_provider(
                campaign_id,
                "pacing-loopback",
                MappingCredentialResolver({"test:smtp": "synthetic-password"}),
                gate=LiveSendGate(enabled=True),
                allow_capture_only=True,
                retry_deferred=True,
            )
            if report.campaign_status == "completed":
                break
            retry_at = store.campaign_next_automatic_at(campaign_id)
            if retry_at:
                wait = max(0.0, (datetime.fromisoformat(retry_at) - datetime.now(UTC)).total_seconds())
                time.sleep(wait + 0.01)
        assert report is not None
    assert report.counts[DeliveryState.ACCEPTED.value] == 3
    assert report.counts.get(DeliveryState.PERMANENT_FAILURE.value, 0) == 0
    assert len(smtp.received_messages) == 3

