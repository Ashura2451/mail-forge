import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mailforge.application.campaign_operations import (
    CampaignOperationsError,
    CampaignOperationsService,
)
from mailforge.application.campaigns import CampaignService
from mailforge.application.message_studio import MessageStudioService
from mailforge.application.outbound import OutboundService
from mailforge.application.provider_operations import ProviderOperationsService
from mailforge.application.segmentation import SegmentationService
from mailforge.domain.outbound import ScheduleMode, ScheduleSpec
from mailforge.infrastructure.campaign_store import CampaignStore
from mailforge.interfaces.web import create_app


def _operations(path: Path) -> tuple[CampaignStore, CampaignOperationsService, str]:
    store = CampaignStore(path)
    list_id = store.create_list("Operations audience")
    contact_id = store.upsert_contact(
        "operator@example.com",
        source_file="test",
        source_record_position=1,
        original_input="operator@example.com",
    )
    store.add_to_list(list_id, contact_id)
    campaigns = CampaignService(store)
    outbound = OutboundService(store)
    segmentation = SegmentationService(store)
    message_studio = MessageStudioService(store, outbound=outbound)
    operations = CampaignOperationsService(
        store,
        campaigns=campaigns,
        outbound=outbound,
        message_studio=message_studio,
        segmentation=segmentation,
        provider_operations=ProviderOperationsService(store),
    )
    return store, operations, list_id


def test_workspace_preflight_prepare_and_export_use_durable_authorities(tmp_path: Path) -> None:
    _store, operations, list_id = _operations(tmp_path / "campaign.sqlite3")
    campaign_id = operations.create_draft(name="Operations", list_id=list_id)

    preflight = operations.preflight(campaign_id)
    assert preflight["ready"] is True
    assert any(item["code"] == "not_prepared" for item in preflight["findings"])

    prepared = operations.prepare(campaign_id)
    assert prepared["prepared"] == 1
    workspace = operations.workspace(campaign_id)
    assert workspace["audience"]["status"] == "not_prepared" or workspace["progress"]["total"] == 1
    csv_body, media_type, _filename = operations.export(campaign_id, fmt="csv")
    assert media_type == "text/csv"
    assert "operator@example.com" in csv_body


def test_draft_update_requires_current_version_and_schedule_rejects_dst_gap(tmp_path: Path) -> None:
    store, operations, list_id = _operations(tmp_path / "campaign.sqlite3")
    campaign_id = operations.create_draft(name="Original", list_id=list_id)
    current = store.get_campaign(campaign_id)["updated_at"]

    operations.update_draft(
        campaign_id,
        expected_updated_at=current,
        name="Updated",
        list_id=list_id,
        segment_id=None,
        segment_revision_id=None,
        sender_email="sender@example.com",
        subject="Subject",
        text="Body",
        message_revision_id=None,
        provider_account_ref=None,
    )
    with pytest.raises(ValueError, match="changed since"):
        operations.update_draft(
            campaign_id,
            expected_updated_at=current,
            name="Stale",
            list_id=list_id,
            segment_id=None,
            segment_revision_id=None,
            sender_email="sender@example.com",
            subject="Subject",
            text="Body",
            message_revision_id=None,
            provider_account_ref=None,
        )
    with pytest.raises(CampaignOperationsError, match="does not exist"):
        operations.set_schedule(
            campaign_id,
            ScheduleSpec(
                mode=ScheduleMode.AT,
                timezone="America/New_York",
                scheduled_at="2026-03-08T02:30:00",
            ),
        )


def test_duplicate_and_archive_preserve_history_without_copying_execution_state(
    tmp_path: Path,
) -> None:
    store, operations, list_id = _operations(tmp_path / "campaign.sqlite3")
    original = operations.create_draft(name="Reusable", list_id=list_id)
    operations.prepare(original)
    operations.authorize(original)
    duplicate = operations.duplicate_campaign(original)
    copied = store.get_campaign(duplicate)
    assert copied["status"] == "draft"
    assert copied["authorization"] == "unauthorized"
    assert store.list_intents(duplicate) == []
    assert copied["name"] == "Reusable copy"
    archived = operations.archive_campaign(original)
    assert archived["status"] == "archived"
    assert json.loads(store.get_campaign(original)["configuration_json"])["archived_at"]


def test_browser_workspace_routes_share_the_operations_facade(tmp_path: Path) -> None:
    with TestClient(
        create_app(
            tmp_path / "web.sqlite3",
            allowed_hosts={"testserver", "127.0.0.1", "localhost", "::1"},
        )
    ) as client:
        session = client.get("/api/v1/session")
        headers = {"x-mailforge-csrf": session.json()["csrf_token"]}
        list_id = client.post(
            "/api/v1/lists", headers=headers, json={"name": "Browser audience"}
        ).json()["id"]
        client.post(
            f"/api/v1/imports?list_id={list_id}",
            headers=headers,
            files={"file": ("audience.txt", b"browser@example.com\n", "text/plain")},
        )
        created = client.post(
            "/api/v1/campaigns",
            headers=headers,
            json={"name": "Browser campaign", "list_id": list_id},
        )
        assert created.status_code == 200
        campaign_id = created.json()["id"]
        workspace = client.get(f"/api/v1/campaigns/{campaign_id}/workspace")
        assert workspace.status_code == 200
        assert workspace.json()["campaign"]["status"] == "draft"
        assert (
            client.post(f"/api/v1/campaigns/{campaign_id}/preflight", headers=headers).json()[
                "ready"
            ]
            is True
        )
        assert (
            client.post(
                f"/api/v1/campaigns/{campaign_id}/prepare",
                headers=headers,
                json={},
            ).status_code
            == 200
        )
        assert (
            client.get(f"/api/v1/campaigns/{campaign_id}/recipients").json()["items"][0]["email"]
            == "browser@example.com"
        )
        intent_id = client.get(f"/api/v1/campaigns/{campaign_id}/recipients").json()["items"][0][
            "id"
        ]
        inspected = client.get(f"/api/v1/campaigns/{campaign_id}/prepared-messages/{intent_id}")
        assert inspected.status_code == 200
        assert inspected.json()["recipient"] == "browser@example.com"
        assert inspected.json()["proof"]["content_hash"]
        assert client.get(f"/api/v1/campaigns/{campaign_id}/export?format=csv").status_code == 200


def test_recurring_campaign_creates_a_fresh_scheduled_run(tmp_path: Path) -> None:
    with TestClient(
        create_app(
            tmp_path / "recurring.sqlite3",
            allowed_hosts={"testserver", "127.0.0.1", "localhost", "::1"},
        )
    ) as client:
        session = client.get("/api/v1/session")
        headers = {"x-mailforge-csrf": session.json()["csrf_token"]}
        list_id = client.post(
            "/api/v1/lists", headers=headers, json={"name": "Recurring audience"}
        ).json()["id"]
        client.post(
            f"/api/v1/imports?list_id={list_id}",
            headers=headers,
            files={"file": ("audience.txt", b"recurring@example.com\n", "text/plain")},
        )
        campaign_id = client.post(
            "/api/v1/campaigns", headers=headers, json={"name": "Recurring", "list_id": list_id}
        ).json()["id"]
        assert (
            client.post(
                f"/api/v1/campaigns/{campaign_id}/prepare", headers=headers, json={}
            ).status_code
            == 200
        )
        assert (
            client.put(
                f"/api/v1/campaigns/{campaign_id}/schedule",
                headers=headers,
                json={
                    "mode": "recurring",
                    "timezone": "UTC",
                    "scheduled_at": "2020-01-01T00:00:00",
                    "recurrence": "daily",
                    "catch_up_policy": "next_window",
                },
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
            items = client.get("/api/v1/campaigns").json()["items"]
            if len(items) > 1 and any(item["status"] == "scheduled" for item in items):
                break
            time.sleep(0.025)
        assert len(items) == 2
        child = next(item for item in items if item["id"] != campaign_id)
        assert child["status"] == "scheduled"
        assert child["recurrence_parent_id"] == campaign_id
