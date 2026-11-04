"""Local-only acceptance for the controlled provider sandbox milestone."""

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from mailforge.application.message_studio import MessageStudioService
from mailforge.application.provider_operations import ProviderOperationsService
from mailforge.domain.outbound import SenderPolicy
from mailforge.domain.provider import (
    CAPTURE_ONLY_SANDBOX_KEY,
    SANDBOX_QUALIFICATION_PURPOSE,
    AuthenticationMethod,
    ExternalOperationGrant,
    ProviderKind,
    ProviderOperation,
    ProviderProfile,
    TLSMode,
)
from mailforge.infrastructure.campaign_store import CampaignStore
from mailforge.infrastructure.smtp import SMTPPhase, SMTPTransaction


class _CapturePool:
    def __init__(self):
        self.last_transaction = SMTPTransaction(
            "capture-only",
            SMTPPhase.FINAL_DATA_RESPONSE,
            response_code=250,
            tls_active=True,
            authenticated=True,
        )
        self.messages: list[tuple[str, str, bytes]] = []

    def sendmail(self, sender, recipient, message):
        self.messages.append((sender, recipient, message))
        return {}

    def close(self):
        return None


def _profile(*, sandbox: bool) -> ProviderProfile:
    return ProviderProfile(
        ref="capture",
        kind=ProviderKind.SMTP,
        host="127.0.0.1",
        port=2525,
        username="sender@sandbox.example.com",
        credential_ref="test:capture",
        tls_mode=TLSMode.IMPLICIT,
        auth_method=AuthenticationMethod.PASSWORD,
        metadata=(
            {"sandbox": {"key": CAPTURE_ONLY_SANDBOX_KEY, "mode": "capture_only"}}
            if sandbox
            else None
        ),
    )


def _proof(service: ProviderOperationsService) -> dict:
    studio = MessageStudioService(service.store)
    document = studio.create_document("Sandbox qualification")
    revision = studio.publish(document["id"], expected_revision_id=document["draft"]["id"])
    return studio.prepare_controlled_test(
        revision_id=revision["published"]["id"],
        sender=SenderPolicy(
            ref="capture", provider="synthetic", email="sender@sandbox.example.com"
        ),
        recipient="recipient@sandbox.example.com",
    )


def _grant(service: ProviderOperationsService, proof: dict) -> ExternalOperationGrant:
    account = service.account("capture")
    expires_at = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    service.issue_grant(
        "capture",
        ProviderOperation.CONTROLLED_TEST_SEND,
        purpose=SANDBOX_QUALIFICATION_PURPOSE,
        sender="sender@sandbox.example.com",
        recipient="recipient@sandbox.example.com",
        expires_at=expires_at,
        proof_hash=proof["proof_hash"],
    )
    row = service.store.list_provider_operation_grants("capture")[0]
    return ExternalOperationGrant(
        account_ref="capture",
        operation=ProviderOperation.CONTROLLED_TEST_SEND,
        host=account["host"],
        port=account["port"],
        purpose=SANDBOX_QUALIFICATION_PURPOSE,
        sender="sender@sandbox.example.com",
        recipient="recipient@sandbox.example.com",
        max_submissions=1,
        expires_at=expires_at,
        authorization_ref=row["authorization_ref"],
        proof_hash=proof["proof_hash"],
    )


def test_capture_only_classification_requires_allowlisted_loopback(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILFORGE_ENABLE_LOCAL_FIXTURES", "1")
    monkeypatch.setenv("MAILFORGE_LOCAL_CAPTURE_ENDPOINT", "127.0.0.1:2525")
    store = CampaignStore(tmp_path / "sandbox.sqlite3")
    service = ProviderOperationsService(store)
    service.register(
        _profile(sandbox=False),
        display_name="Capture",
        sender_email="sender@sandbox.example.com",
    )
    with pytest.raises(PermissionError, match="fixture is unavailable|approved"):
        service.issue_grant(
            "capture",
            ProviderOperation.CONTROLLED_TEST_SEND,
            purpose=SANDBOX_QUALIFICATION_PURPOSE,
            sender="sender@sandbox.example.com",
            recipient="recipient@sandbox.example.com",
            expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            proof_hash="not-a-proof",
        )


def test_capture_only_classification_requires_process_fixture_authority(tmp_path, monkeypatch):
    monkeypatch.delenv("MAILFORGE_ENABLE_LOCAL_FIXTURES", raising=False)
    monkeypatch.delenv("MAILFORGE_LOCAL_CAPTURE_ENDPOINT", raising=False)
    store = CampaignStore(tmp_path / "sandbox-process-gate.sqlite3")
    service = ProviderOperationsService(store)
    service.register(
        _profile(sandbox=True),
        display_name="Capture",
        sender_email="sender@sandbox.example.com",
    )
    with pytest.raises(PermissionError, match="fixture is unavailable|approved"):
        service.issue_grant(
            "capture",
            ProviderOperation.CONTROLLED_TEST_SEND,
            purpose=SANDBOX_QUALIFICATION_PURPOSE,
            sender="sender@sandbox.example.com",
            recipient="recipient@sandbox.example.com",
            expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            proof_hash="not-a-proof",
        )


def test_capture_only_classification_rejects_hostname_loopback(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILFORGE_ENABLE_LOCAL_FIXTURES", "1")
    monkeypatch.setenv("MAILFORGE_LOCAL_CAPTURE_ENDPOINT", "127.0.0.1:2525")
    store = CampaignStore(tmp_path / "sandbox-hostname.sqlite3")
    service = ProviderOperationsService(store)
    profile = _profile(sandbox=True)
    with pytest.raises(ValueError, match="IP literals"):
        service.register(
            profile.__class__(**{**profile.__dict__, "host": "localhost"}),
            display_name="Capture",
            sender_email="sender@sandbox.example.com",
        )


def test_sandbox_grant_requires_synthetic_recipient(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILFORGE_ENABLE_LOCAL_FIXTURES", "1")
    monkeypatch.setenv("MAILFORGE_LOCAL_CAPTURE_ENDPOINT", "127.0.0.1:2525")
    store = CampaignStore(tmp_path / "sandbox.sqlite3")
    service = ProviderOperationsService(store)
    service.register(
        _profile(sandbox=True),
        display_name="Capture",
        sender_email="sender@sandbox.example.com",
    )
    with pytest.raises(PermissionError, match="synthetic"):
        service.issue_grant(
            "capture",
            ProviderOperation.CONTROLLED_TEST_SEND,
            purpose=SANDBOX_QUALIFICATION_PURPOSE,
            sender="sender@sandbox.example.com",
            recipient="recipient@example.com",
            expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            proof_hash="not-a-proof",
        )


def test_controlled_sandbox_submission_uses_proof_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILFORGE_ENABLE_LOCAL_FIXTURES", "1")
    monkeypatch.setenv("MAILFORGE_LOCAL_CAPTURE_ENDPOINT", "127.0.0.1:2525")
    store = CampaignStore(tmp_path / "sandbox.sqlite3")
    service = ProviderOperationsService(store)
    service.register(
        _profile(sandbox=True),
        display_name="Capture",
        sender_email="sender@sandbox.example.com",
    )
    proof = _proof(service)
    grant = _grant(service, proof)
    pool = _CapturePool()
    monkeypatch.setattr(service.providers, "smtp_pool", lambda *args, **kwargs: pool)

    first = service.controlled_test_send(
        ref="capture",
        sender="sender@sandbox.example.com",
        recipient="recipient@sandbox.example.com",
        message=None,
        idempotency_key="sandbox-submit-001",
        grant=grant,
        proof_id=proof["proof_id"],
        proof_hash=proof["proof_hash"],
    )
    second = service.controlled_test_send(
        ref="capture",
        sender="sender@sandbox.example.com",
        recipient="recipient@sandbox.example.com",
        message=None,
        idempotency_key="sandbox-submit-001",
        grant=grant,
        proof_id=proof["proof_id"],
        proof_hash=proof["proof_hash"],
    )

    assert first["status"] == "provider_accepted"
    assert second["id"] == first["id"]
    assert len(pool.messages) == 1


def test_caller_invented_grant_is_rejected_before_transport(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILFORGE_ENABLE_LOCAL_FIXTURES", "1")
    monkeypatch.setenv("MAILFORGE_LOCAL_CAPTURE_ENDPOINT", "127.0.0.1:2525")
    store = CampaignStore(tmp_path / "forged.sqlite3")
    service = ProviderOperationsService(store)
    service.register(
        _profile(sandbox=True), display_name="Capture", sender_email="sender@sandbox.example.com"
    )
    proof = _proof(service)
    forged = ExternalOperationGrant(
        account_ref="capture",
        operation=ProviderOperation.CONTROLLED_TEST_SEND,
        host="127.0.0.1",
        port=2525,
        purpose=SANDBOX_QUALIFICATION_PURPOSE,
        sender="sender@sandbox.example.com",
        recipient="recipient@sandbox.example.com",
        max_submissions=1,
        expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        authorization_ref="caller-invented",
        proof_hash=proof["proof_hash"],
    )
    pool = _CapturePool()
    monkeypatch.setattr(service.providers, "smtp_pool", lambda *args, **kwargs: pool)
    with pytest.raises(PermissionError, match="not issued"):
        service.controlled_test_send(
            ref="capture",
            sender=forged.sender,
            recipient=forged.recipient,
            message=None,
            idempotency_key="forged-key",
            grant=forged,
            proof_id=proof["proof_id"],
            proof_hash=proof["proof_hash"],
        )
    assert not pool.messages
    assert store.get_provider_operation_grant("caller-invented", account_ref="capture") is None


def test_sandbox_alternate_purpose_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILFORGE_ENABLE_LOCAL_FIXTURES", "1")
    monkeypatch.setenv("MAILFORGE_LOCAL_CAPTURE_ENDPOINT", "127.0.0.1:2525")
    store = CampaignStore(tmp_path / "purpose.sqlite3")
    service = ProviderOperationsService(store)
    service.register(
        _profile(sandbox=True), display_name="Capture", sender_email="sender@sandbox.example.com"
    )
    with pytest.raises(PermissionError, match="sandbox_qualification"):
        service.issue_grant(
            "capture",
            ProviderOperation.CONTROLLED_TEST_SEND,
            purpose="owner-controlled local qualification fixture",
            sender="sender@sandbox.example.com",
            recipient="recipient@example.com",
            expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            proof_hash="not-a-proof",
        )
    assert not store.list_provider_operation_grants("capture")


def test_profile_security_configuration_drift_rejects_transport(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILFORGE_ENABLE_LOCAL_FIXTURES", "1")
    monkeypatch.setenv("MAILFORGE_LOCAL_CAPTURE_ENDPOINT", "127.0.0.1:2525")
    store = CampaignStore(tmp_path / "drift.sqlite3")
    service = ProviderOperationsService(store)
    profile = _profile(sandbox=True)
    service.register(profile, display_name="Capture", sender_email="sender@sandbox.example.com")
    proof = _proof(service)
    grant = _grant(service, proof)
    service.register(
        profile.__class__(**{**profile.__dict__, "credential_ref": "test:changed"}),
        display_name="Capture",
        sender_email="sender@sandbox.example.com",
    )
    pool = _CapturePool()
    monkeypatch.setattr(service.providers, "smtp_pool", lambda *args, **kwargs: pool)
    with pytest.raises(PermissionError, match="stored grant"):
        service.controlled_test_send(
            ref="capture",
            sender="sender@sandbox.example.com",
            recipient="recipient@sandbox.example.com",
            message=None,
            idempotency_key="drift-key",
            grant=grant,
            proof_id=proof["proof_id"],
            proof_hash=proof["proof_hash"],
        )
    assert not pool.messages


def test_reserved_submission_is_reconciled_as_ambiguous_without_replay(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILFORGE_ENABLE_LOCAL_FIXTURES", "1")
    monkeypatch.setenv("MAILFORGE_LOCAL_CAPTURE_ENDPOINT", "127.0.0.1:2525")
    database = tmp_path / "recovery.sqlite3"
    store = CampaignStore(database)
    service = ProviderOperationsService(store)
    service.register(
        _profile(sandbox=True), display_name="Capture", sender_email="sender@sandbox.example.com"
    )
    proof = _proof(service)
    grant = _grant(service, proof)
    proof_row = store.get_outbound_proof(proof["proof_id"])
    assert proof_row is not None
    store.reserve_provider_test_submission(
        account_ref="capture",
        authorization_ref=grant.authorization_ref,
        idempotency_key="crashed-key",
        sender="sender@sandbox.example.com",
        recipient="recipient@sandbox.example.com",
        message_digest=hashlib.sha256(bytes(proof_row["mime_bytes"])).hexdigest(),
        message_revision_id=proof_row["message_revision_id"],
        proof_id=proof["proof_id"],
    )
    restarted = CampaignStore(database)
    recovered = restarted.reconcile_abandoned_provider_test_submissions()
    row = restarted.get_provider_test_submission("crashed-key")
    assert len(recovered) == 1
    assert row["status"] == "ambiguous"
    assert row["phase"] == "ambiguous"
    assert (
        restarted.get_provider_operation_grant(grant.authorization_ref, account_ref="capture")[
            "consumed_submissions"
        ]
        == 1
    )
