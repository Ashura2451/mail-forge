from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mailforge.application.message_studio import MessageStudioService
from mailforge.application.outbound import OutboundService
from mailforge.application.provider_operations import ProviderOperationsService
from mailforge.domain.campaign import ContactCandidate, SuppressionCategory
from mailforge.domain.outbound import SenderPolicy
from mailforge.domain.provider import (
    AuthenticationMethod,
    ProviderKind,
    ProviderOperation,
    ProviderProfile,
    TLSMode,
)
from mailforge.infrastructure.campaign_store import CampaignStore


def _revision(studio: MessageStudioService) -> str:
    document = studio.create_document("Proof fixture")
    published = studio.publish(document["id"], expected_revision_id=document["draft"]["id"])
    return published["published"]["id"]


def _profile() -> ProviderProfile:
    return ProviderProfile(
        ref="local",
        kind=ProviderKind.SMTP,
        host="127.0.0.1",
        port=465,
        username="sender@example.com",
        credential_ref="test:provider",
        tls_mode=TLSMode.IMPLICIT,
        auth_method=AuthenticationMethod.PASSWORD,
    )


def test_controlled_proof_is_revision_and_mime_bound(tmp_path):
    store = CampaignStore(tmp_path / "proof.sqlite3")
    studio = MessageStudioService(store, outbound=OutboundService(store))
    revision_id = _revision(studio)
    proof = studio.prepare_controlled_test(
        revision_id=revision_id,
        sender=SenderPolicy(ref="sender", provider="synthetic", email="sender@example.com"),
        recipient="owned@example.com",
    )
    row = store.get_outbound_proof(proof["proof_id"])
    assert row is not None
    assert row["proof_hash"] == proof["proof_hash"]
    assert row["message_revision_id"] == revision_id
    assert row["mime_bytes"]

    providers = ProviderOperationsService(store)
    providers.register(_profile(), sender_email="sender@example.com")
    issued = providers.issue_grant(
        "local",
        ProviderOperation.CONTROLLED_TEST_SEND,
        purpose="synthetic proof qualification",
        sender="sender@example.com",
        recipient="owned@example.com",
        expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        proof_hash=proof["proof_hash"],
    )
    grant = providers.grant_for_authorization(
        "local",
        ProviderOperation.CONTROLLED_TEST_SEND,
        authorization_ref=issued["authorization_ref"],
        purpose="synthetic proof qualification",
        sender="sender@example.com",
        recipient="owned@example.com",
        expires_at=issued["expires_at"],
        proof_hash=proof["proof_hash"],
    )
    with pytest.raises(PermissionError, match="does not match"):
        providers.controlled_test_send(
            ref="local",
            sender="sender@example.com",
            recipient="owned@example.com",
            message="forged caller content",
            idempotency_key="proof-forged-content",
            grant=grant,
        )


def test_controlled_proof_rechecks_current_suppression(tmp_path):
    store = CampaignStore(tmp_path / "suppression.sqlite3")
    studio = MessageStudioService(store, outbound=OutboundService(store))
    store.upsert_contact(
        "owned@example.com",
        display_name="Owned",
        metadata={"name": "Owned"},
        source_file="synthetic",
        source_record_position=1,
        original_input="owned@example.com",
    )
    revision_id = _revision(studio)
    proof = studio.prepare_controlled_test(
        revision_id=revision_id,
        sender=SenderPolicy(ref="sender", provider="synthetic", email="sender@example.com"),
        recipient="owned@example.com",
    )
    store.add_suppression(
        category=SuppressionCategory.MANUAL_HOLD,
        contact_id=store.contact_by_email("owned@example.com")["id"],
        scope_type="purpose",
        scope_id="transactional",
        reason="operator hold",
        actor="test",
    )
    providers = ProviderOperationsService(store)
    providers.register(_profile(), sender_email="sender@example.com")
    issued = providers.issue_grant(
        "local",
        ProviderOperation.CONTROLLED_TEST_SEND,
        purpose="synthetic proof qualification",
        sender="sender@example.com",
        recipient="owned@example.com",
        expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        proof_hash=proof["proof_hash"],
    )
    grant = providers.grant_for_authorization(
        "local",
        ProviderOperation.CONTROLLED_TEST_SEND,
        authorization_ref=issued["authorization_ref"],
        purpose="synthetic proof qualification",
        sender="sender@example.com",
        recipient="owned@example.com",
        expires_at=issued["expires_at"],
        proof_hash=proof["proof_hash"],
    )
    with pytest.raises(PermissionError, match="suppressed"):
        providers.controlled_test_send(
            ref="local",
            sender="sender@example.com",
            recipient="owned@example.com",
            message=None,
            idempotency_key="suppressed-proof",
            grant=grant,
        )


def test_campaign_preparation_persists_proof_without_copying_mime(tmp_path):
    store = CampaignStore(tmp_path / "campaign-proof.sqlite3")
    outbound = OutboundService(store)
    studio = MessageStudioService(store, outbound=outbound)
    revision_id = _revision(studio)
    list_id = store.create_list("Proof audience")
    outbound.campaigns.import_candidates(
        [
            # The campaign service owns eligibility and recipient normalization.
            ContactCandidate("ada@example.com", "ada@example.com", "synthetic", 1, {"name": "Ada"})
        ],
        list_id,
    )
    campaign_id = outbound.campaigns.create_campaign(
        "Proof campaign",
        list_id,
        sender_config_ref="sender",
        template_ref="studio",
        message_revision_id=revision_id,
    )
    report = studio.prepare_campaign(
        campaign_id,
        sender=SenderPolicy(ref="sender", provider="synthetic", email="sender@example.com"),
        revision_id=revision_id,
    )
    assert report.prepared == 1
    prepared = store.get_prepared_message(store.list_intents(campaign_id)[0]["id"])
    assert prepared["proof_id"]
    proof = store.get_outbound_proof(prepared["proof_id"])
    assert proof["message_revision_id"] == revision_id
    assert proof["mime_bytes"] is None
    assert proof["content_hash"] == prepared["content_hash"]
