from __future__ import annotations

import json
import threading
import time
from email.parser import BytesParser
from email.policy import SMTP
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mailforge.application.data_studio import DataStudioService
from mailforge.application.message_studio import MessageStudioError, MessageStudioService
from mailforge.application.outbound import OutboundService
from mailforge.domain.campaign import ContactCandidate, DeliveryState
from mailforge.domain.outbound import SenderPolicy, TemplateDefinition
from mailforge.infrastructure.campaign_store import CampaignStore
from mailforge.interfaces.web import create_app


def _studio(tmp_path: Path) -> tuple[CampaignStore, MessageStudioService]:
    store = CampaignStore(tmp_path / "studio.sqlite3")
    outbound = OutboundService(store)
    return store, MessageStudioService(store, outbound=outbound)


def test_message_schema_is_current_and_migration_is_repeatable(tmp_path: Path) -> None:
    store, _ = _studio(tmp_path)
    assert store.schema_version() == 18
    assert store.migrate() == 18
    with store.connect() as connection:
        names = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert {
        "message_documents",
        "message_revisions",
        "message_assets",
        "message_campaign_snapshots",
    } <= names


def test_message_preparation_lock_covers_snapshot_selection(tmp_path: Path, monkeypatch) -> None:
    store, studio = _studio(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    acquired = threading.Event()

    def blocked_prepare(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return "prepared"

    monkeypatch.setattr(studio, "_prepare_campaign", blocked_prepare)
    sender = SenderPolicy(ref="sender", provider="synthetic", email="sender@example.com")
    result: dict[str, object] = {}

    worker = threading.Thread(
        target=lambda: result.setdefault(
            "value", studio.prepare_campaign("campaign", sender=sender)
        )
    )
    worker.start()
    assert entered.wait(1)

    def acquire_lock() -> None:
        with store.campaign_operation_lock():
            acquired.set()

    lock_worker = threading.Thread(target=acquire_lock)
    lock_worker.start()
    assert not acquired.wait(0.1)
    release.set()
    worker.join(timeout=2)
    assert result["value"] == "prepared"
    lock_worker.join(timeout=2)
    assert acquired.is_set()


def test_document_revisions_validate_and_reject_stale_save(tmp_path: Path) -> None:
    _, studio = _studio(tmp_path)
    created = studio.create_document("Welcome")
    saved = studio.save_draft(
        created["id"],
        {**created["draft"]["document"], "subject": "Updated {{ name }}"},
        expected_revision_id=created["draft"]["id"],
    )
    assert saved["draft"]["document"]["subject"] == "Updated {{ name }}"
    with pytest.raises(MessageStudioError, match="another session"):
        studio.save_draft(
            created["id"],
            created["draft"]["document"],
            expected_revision_id=created["draft"]["id"],
        )
    published = studio.publish(created["id"], expected_revision_id=saved["draft"]["id"])
    assert published["published"]["id"] == saved["draft"]["id"]


def test_renderer_is_deterministic_escaped_and_warns_on_missing_fields(tmp_path: Path) -> None:
    _, studio = _studio(tmp_path)
    document = studio.blank_document("Personalized")
    document["subject"] = "Hello {{ contact.company }}"
    document["blocks"] = [{"type": "text", "text": "Hi {{ contact.company }} — {{ name }}"}]
    first = studio.preview(document=document)
    second = studio.preview(document=document)
    assert first["html"] == second["html"]
    assert first["text"] == "Hi  — Sample recipient"
    assert "missing merge field: contact.company" in first["warnings"]
    with pytest.raises(MessageStudioError, match="unsafe markup"):
        studio.validate_document(
            {**document, "blocks": [{"type": "text", "text": "<script>x</script>"}]}
        )
    with pytest.raises(MessageStudioError, match="unsafe"):
        studio.validate_document(
            {
                **document,
                "blocks": [{"type": "button", "label": "Go", "url": "javascript:alert(1)"}],
            }
        )
    with pytest.raises(MessageStudioError, match="filename"):
        studio.create_asset("../private.txt", "text/plain", b"no path access")


def test_asset_cid_and_campaign_snapshot_use_canonical_mime(tmp_path: Path) -> None:
    store, studio = _studio(tmp_path)
    asset = studio.create_asset("pixel.png", "image/png", b"PNG synthetic")
    document = studio.blank_document("Image note")
    document["blocks"] = [{"type": "image", "asset_id": asset["id"], "alt": "Synthetic image"}]
    created = studio.create_document("Image note", kind="template", document=document)
    published = studio.publish(created["id"], expected_revision_id=created["draft"]["id"])
    list_id = store.create_list("Audience")
    OutboundService(store).campaigns.import_candidates(
        [ContactCandidate("ada@example.com", "ada@example.com", "synthetic", 1, {"name": "Ada"})],
        list_id,
    )
    attachment = studio.create_asset("ada.pdf", "application/pdf", b"private synthetic document")
    contact_id = store.list_contacts(list_id)[0]["id"]
    studio.map_recipient_attachment(published["published"]["id"], contact_id, attachment["id"])
    campaign_id = OutboundService(store).campaigns.create_campaign(
        "Image campaign",
        list_id,
        sender_config_ref="sender",
        template_ref="studio",
        message_revision_id=published["published"]["id"],
    )
    report = studio.prepare_campaign(
        campaign_id,
        sender=SenderPolicy(ref="sender", provider="synthetic", email="sender@example.com"),
        revision_id=published["published"]["id"],
    )
    assert report.prepared == 1
    prepared = store.list_intents(campaign_id)[0]
    persisted = store.get_prepared_message(prepared["id"])
    message = BytesParser(policy=SMTP).parsebytes(persisted["mime_bytes"])
    assert any(part["Content-ID"] == f"<mailforge-{asset['id']}>" for part in message.walk())
    assert "ada.pdf" in persisted["attachment_metadata_json"]
    with store.connect() as connection:
        snapshot = connection.execute(
            "SELECT * FROM message_campaign_snapshots WHERE campaign_id = ?", (campaign_id,)
        ).fetchone()
    assert snapshot is not None
    assert json.loads(snapshot["document_json"])["name"] == "Image note"


def test_message_studio_api_and_isolated_preview(tmp_path: Path) -> None:
    app = create_app(tmp_path / "web.sqlite3", allowed_hosts={"testserver"})
    with TestClient(app) as client:
        session = client.get("/api/v1/session")
        headers = {"x-mailforge-csrf": session.json()["csrf_token"]}
        created = client.post(
            "/api/v1/message-studio/documents",
            headers=headers,
            json={"name": "API message", "kind": "message"},
        )
        assert created.status_code == 200
        preview = client.post(
            "/api/v1/message-studio/preview",
            headers=headers,
            json={"revision_id": created.json()["draft"]["id"]},
        )
        assert preview.status_code == 200
        assert "subject" in preview.json()
        assert (
            client.post(
                "/api/v1/message-studio/preview",
                json={"revision_id": created.json()["draft"]["id"]},
            ).status_code
            == 403
        )
    app.state.runtime.close()


def test_data_studio_recipient_batch_freezes_source_context_and_message(tmp_path: Path) -> None:
    store = CampaignStore(tmp_path / "batch.sqlite3")
    outbound = OutboundService(store)
    data_studio = DataStudioService(store, tmp_path / "data-studio")
    studio = MessageStudioService(store, outbound=outbound, data_studio=data_studio)
    source = tmp_path / "processed.jsonl"
    source.write_text(
        '{"email":"ada@example.com","name":"Ada","plan":"pro"}\n',
        encoding="utf-8",
    )
    recipe = data_studio.create_recipe("Batch recipe", definition={"steps": []})
    job = data_studio.create_job(source, source.name, recipe["current_revision_id"])
    data_studio.start(job["id"])
    for _ in range(200):
        job = data_studio.get_job(job["id"])
        if job["status"] not in {"pending", "running"}:
            break
        time.sleep(0.01)
    assert job["status"] == "completed"
    output = next(item for item in job["artifacts"] if item["kind"] == "output")

    list_id = store.create_list("Batch audience")
    from mailforge.application.segmentation import SegmentationService

    SegmentationService(store).import_structured(
        [{"email": "ada@example.com"}, {"email": "extra@example.com"}],
        mappings={"email": "contact.email"},
        list_id=list_id,
        source_ref="batch-reconcile",
    )
    document = studio.blank_document("Batch message")
    document["subject"] = "Hello {{ name }}"
    document["blocks"] = [{"type": "text", "text": "Plan: {{ contact.plan }}"}]
    created = studio.create_document("Batch message", document=document)
    published = studio.publish(created["id"], expected_revision_id=created["draft"]["id"])
    campaign_id = outbound.campaigns.create_campaign(
        "Prepared batch",
        list_id,
        sender_config_ref="batch-sender",
        template_ref="batch-template",
        message_revision_id=published["published"]["id"],
    )
    snapshot = studio.create_data_studio_snapshot(
        campaign_id,
        source_job_id=job["id"],
        artifact_id=output["id"],
        recipe_revision_id=recipe["current_revision_id"],
        revision_id=published["published"]["id"],
        field_mapping={"email": "email", "name": "name", "plan": "contact.plan"},
    )
    assert snapshot["artifact_sha256"] == output["sha256"]
    assert snapshot["recipe_revision_id"] == recipe["current_revision_id"]
    assert snapshot["recipients"][0]["context"]["metadata"]["plan"] == "pro"
    with pytest.raises(ValueError, match="Data Studio recipient snapshot"):
        outbound.prepare_campaign(
            campaign_id,
            template=TemplateDefinition(name="bypass", version="1", subject="", text=""),
            sender=SenderPolicy(
                ref="batch-sender", provider="synthetic", email="sender@example.com"
            ),
        )
    with pytest.raises(MessageStudioError, match="different revision or run"):
        studio.prepare_campaign(
            campaign_id,
            sender=SenderPolicy(
                ref="batch-sender", provider="synthetic", email="sender@example.com"
            ),
            revision_id="different-published-revision",
        )
    report = studio.prepare_campaign(
        campaign_id,
        sender=SenderPolicy(ref="batch-sender", provider="synthetic", email="sender@example.com"),
        revision_id=published["published"]["id"],
        data_snapshot_id=snapshot["id"],
    )
    assert report.prepared == 1
    prepared_rows = [
        store.get_prepared_message(intent["id"])
        for intent in store.list_intents(campaign_id)
        if store.get_prepared_message(intent["id"]) is not None
    ]
    assert len(prepared_rows) == 1
    prepared = prepared_rows[0]
    assert b"Plan: pro" in prepared["mime_bytes"]
    second_report = studio.prepare_campaign(
        campaign_id,
        sender=SenderPolicy(ref="batch-sender", provider="synthetic", email="sender@example.com"),
        revision_id=published["published"]["id"],
    )
    assert second_report.prepared == 0
    assert all(
        store.get_prepared_message(intent["id"]) is not None
        for intent in store.list_intents(campaign_id)
        if intent["contact_id"] == snapshot["recipients"][0]["contact_id"]
    )
    assert (
        next(
            intent["state"]
            for intent in store.list_intents(campaign_id)
            if intent["contact_id"] != snapshot["recipients"][0]["contact_id"]
        )
        == "not_eligible"
    )
    store.upsert_contact(
        "ada@example.com",
        display_name="Changed later",
        metadata={"plan": "changed"},
        source_file="later",
        source_record_position=1,
        original_input="ada@example.com",
    )
    extra_intent = next(
        intent
        for intent in store.list_intents(campaign_id)
        if intent["contact_id"] != snapshot["recipients"][0]["contact_id"]
    )
    included_intent = next(
        intent
        for intent in store.list_intents(campaign_id)
        if intent["contact_id"] == snapshot["recipients"][0]["contact_id"]
    )
    store.set_intent_state(included_intent["id"], DeliveryState.PREPARED, "synthetic refresh")
    store.set_intent_state(extra_intent["id"], DeliveryState.PREPARED, "synthetic race")
    refreshed = studio.prepare_campaign(
        campaign_id,
        sender=SenderPolicy(ref="batch-sender", provider="synthetic", email="sender@example.com"),
        revision_id=published["published"]["id"],
        data_snapshot_id=snapshot["id"],
    )
    assert refreshed.prepared == 1
    assert b"Plan: pro" in store.get_prepared_message(included_intent["id"])["mime_bytes"]
    data_studio.close()
