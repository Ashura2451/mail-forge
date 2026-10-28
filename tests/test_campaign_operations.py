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
from mailforge.application.reporting import ReportingService
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
    message_studio = MessageStudioService(store, outbound=outbound, segmentation=segmentation)
    operations = CampaignOperationsService(
        store,
        campaigns=campaigns,
        outbound=outbound,
        message_studio=message_studio,
        segmentation=segmentation,
        provider_operations=ProviderOperationsService(store),
        reporting=ReportingService(store),
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
        assert client.get(f"/api/v1/campaigns/{campaign_id}/export?format=csv").status_code == 200
