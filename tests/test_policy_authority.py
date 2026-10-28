from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from mailforge.application.campaigns import CampaignService, ScriptedTransport
from mailforge.application.outbound import OutboundService
from mailforge.application.policy import PolicyEngine
from mailforge.domain.campaign import (
    CampaignStatus,
    ContactCandidate,
    DeliveryState,
    ExecutionAuthorization,
    SuppressionCategory,
)
from mailforge.domain.outbound import SenderPolicy, TemplateDefinition
from mailforge.domain.policy import EvidenceStatus, PreferenceState
from mailforge.infrastructure.campaign_store import CampaignStore


def _setup(tmp_path: Path, *emails: str):
    store = CampaignStore(tmp_path / "policy.sqlite3")
    service = CampaignService(store)
    list_id = store.create_list("policy-audience")
    service.import_candidates(
        [
            ContactCandidate(email, email, "synthetic.txt", index, {})
            for index, email in enumerate(emails)
        ],
        list_id,
    )
    return store, service, list_id


def test_simple_list_keeps_unknown_evidence_explicit_and_simulation_usable(tmp_path):
    store, service, list_id = _setup(
        tmp_path, "a@example.com", "a@example.com", "blocked@example.com"
    )
    service.add_suppression(
        "blocked@example.com", SuppressionCategory.UNSUBSCRIBE, "trusted unsubscribe"
    )
    campaign_id = service.create_campaign("simple", list_id)

    eligibility = service.inspect_eligibility(campaign_id)
    assert eligibility[0]["status"] == "authorization_unknown"
    assert eligibility[0]["evidence_status"] == "unknown"
    assert store.list_authorization_evidence() == []

    report = service.prepare(campaign_id)
    assert report.counts[DeliveryState.ELIGIBLE_PENDING.value] == 1
    assert report.counts[DeliveryState.SUPPRESSED.value] == 1
    service.authorize_simulation(campaign_id)
    completed = service.execute(campaign_id, ScriptedTransport())
    assert completed.counts[DeliveryState.ACCEPTED.value] == 1
    assert len(store.list_operator_declarations(campaign_id=campaign_id)) == 1


def test_preferences_are_purpose_specific_and_history_is_preserved(tmp_path):
    store, service, list_id = _setup(tmp_path, "a@example.com")
    service.set_preference("a@example.com", purpose="marketing", state=PreferenceState.DISABLED)
    newsletter = service.create_campaign(
        "newsletter", list_id, configuration={"communication_purpose": "newsletter"}
    )
    service.prepare(newsletter)
    assert (
        service.preview(newsletter)["intents"][0]["state"] == DeliveryState.ELIGIBLE_PENDING.value
    )

    marketing = service.create_campaign(
        "marketing", list_id, configuration={"communication_purpose": "marketing"}
    )
    service.prepare(marketing)
    assert service.preview(marketing)["intents"][0]["state"] == DeliveryState.SUPPRESSED.value
    assert len(store.list_preferences()) == 1


def test_broader_preference_and_evidence_restrictions_are_not_hidden_by_narrower_records(
    tmp_path,
):
    store, service, list_id = _setup(tmp_path, "a@example.com")
    service.set_preference("a@example.com", state=PreferenceState.DISABLED)
    service.set_preference("a@example.com", state=PreferenceState.ENABLED, list_id=list_id)
    marketing_campaign = service.create_campaign("overlap", list_id)
    assert service.inspect_eligibility(marketing_campaign)[0]["status"] == "excluded"

    service.record_authorization_evidence(
        "a@example.com",
        purpose="newsletter",
        status=EvidenceStatus.WITHDRAWN,
        source="withdrawal",
    )
    service.record_authorization_evidence(
        "a@example.com",
        purpose="newsletter",
        status=EvidenceStatus.ESTABLISHED,
        source="list-import",
        list_id=list_id,
    )
    campaign_id = service.create_campaign(
        "overlap-newsletter", list_id, configuration={"communication_purpose": "newsletter"}
    )
    service.prepare(campaign_id)
    assert "authorization_withdrawn" in service.preview(campaign_id)["intents"][0]["reason"]
    service.record_authorization_evidence(
        "a@example.com",
        purpose="newsletter",
        status=EvidenceStatus.ESTABLISHED,
        source="renewal",
    )
    assert service.preview(campaign_id)["intents"][0]["current_eligibility"]["status"] == "eligible"


def test_evidence_records_are_not_fabricated_and_conflict_requires_review(tmp_path):
    store, service, list_id = _setup(tmp_path, "a@example.com")
    campaign_id = service.create_campaign("evidence", list_id)
    service.record_authorization_evidence(
        "a@example.com", source="imported_signup", evidence_ref="signup-1"
    )
    assert store.list_authorization_evidence()[0]["status"] == EvidenceStatus.ESTABLISHED.value
    service.record_authorization_evidence(
        "a@example.com", source="withdrawal", status=EvidenceStatus.WITHDRAWN
    )
    service.prepare(campaign_id)
    intent = service.preview(campaign_id)["intents"][0]
    assert intent["state"] == DeliveryState.ELIGIBLE_PENDING.value
    assert "authorization_withdrawn" in intent["reason"]
    service.authorize_simulation(campaign_id)
    transport = ScriptedTransport()
    report = service.execute(campaign_id, transport)
    assert transport.calls == []
    assert report.counts[DeliveryState.SUPPRESSED.value] == 1


def test_scope_precedence_and_reimport_do_not_erase_suppression(tmp_path):
    store, service, list_id = _setup(tmp_path, "a@example.com")
    service.add_suppression("a@example.com", SuppressionCategory.MANUAL_HOLD, "global hold")
    service.add_suppression(
        "a@example.com", SuppressionCategory.UNSUBSCRIBE, "purpose opt-out", purpose="marketing"
    )
    campaign_id = service.create_campaign("scope", list_id)
    service.prepare(campaign_id)
    intent = service.preview(campaign_id)["intents"][0]
    assert intent["state"] == DeliveryState.SUPPRESSED.value
    assert intent["reason"].startswith("unsubscribe:")
    service.import_candidates(
        [ContactCandidate("a@example.com", "a@example.com", "reimport.txt", 1, {})], list_id
    )
    assert (
        len(
            store.matching_suppressions(
                normalized_email="a@example.com",
                contact_id=store.contact_by_email("a@example.com")["id"],
                list_id=list_id,
                campaign_id=campaign_id,
                sender_config_ref="synthetic-sender",
                communication_purpose="marketing",
            )
        )
        == 2
    )


def test_stale_prepared_audience_is_rechecked_before_dispatch(tmp_path):
    store, service, list_id = _setup(tmp_path, "a@example.com")
    campaign_id = service.create_campaign("stale", list_id)
    service.prepare(campaign_id)
    service.authorize_simulation(campaign_id)
    service.set_preference("a@example.com", state=PreferenceState.DISABLED)
    transport = ScriptedTransport()
    report = service.execute(campaign_id, transport)
    assert transport.calls == []
    assert report.counts[DeliveryState.SUPPRESSED.value] == 1


def test_policy_revision_serializes_restriction_before_attempt_start(tmp_path, monkeypatch):
    store, service, list_id = _setup(tmp_path, "a@example.com")
    campaign_id = service.create_campaign("race", list_id)
    service.prepare(campaign_id)
    service.authorize_simulation(campaign_id)
    original = store.mark_attempt_started
    changed = False

    def race(intent_id, attempt_id, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            service.set_preference("a@example.com", state=PreferenceState.DISABLED)
        return original(intent_id, attempt_id, **kwargs)

    monkeypatch.setattr(store, "mark_attempt_started", race)
    transport = ScriptedTransport()
    report = service.execute(campaign_id, transport)
    assert transport.calls == []
    assert report.counts[DeliveryState.SUPPRESSED.value] == 1


def test_preference_evidence_reference_is_secret_safe(tmp_path):
    store, service, _ = _setup(tmp_path, "a@example.com")
    with pytest.raises(ValueError, match="secret-bearing"):
        service.set_preference(
            "a@example.com", state=PreferenceState.ENABLED, evidence_ref="api_token=secret"
        )
    assert store.list_preferences() == []


def test_suppression_reaffirmation_retains_before_and_after_history(tmp_path):
    store, service, _ = _setup(tmp_path, "a@example.com")
    service.add_suppression("a@example.com", SuppressionCategory.MANUAL_HOLD, "old reason")
    service.add_suppression("a@example.com", SuppressionCategory.MANUAL_HOLD, "new reason")
    with store.connect() as connection:
        history = connection.execute(
            "SELECT action, metadata_json FROM suppression_history ORDER BY rowid"
        ).fetchall()
    assert [row["action"] for row in history] == ["created", "reaffirmed"]
    metadata = json.loads(history[1]["metadata_json"])
    assert metadata["before"]["reason"] == "old reason"
    assert metadata["after"]["reason"] == "new reason"


def test_preview_reports_current_policy_separately_from_prepared_snapshot(tmp_path):
    store, service, list_id = _setup(tmp_path, "a@example.com")
    campaign_id = service.create_campaign("preview", list_id)
    service.prepare(campaign_id)
    service.set_preference("a@example.com", state=PreferenceState.DISABLED)
    item = service.preview(campaign_id)["intents"][0]
    assert item["prepared_state"] == DeliveryState.ELIGIBLE_PENDING.value
    assert item["state"] == DeliveryState.SUPPRESSED.value
    assert item["current_eligibility"]["status"] == "excluded"


def test_authorization_and_declaration_are_atomic(tmp_path):
    store, service, list_id = _setup(tmp_path, "a@example.com")
    campaign_id = service.create_campaign("atomic-auth", list_id)
    service.prepare(campaign_id)
    with store.transaction(immediate=True) as connection:
        connection.execute(
            "CREATE TRIGGER fail_operator_declaration BEFORE INSERT ON operator_declarations "
            "BEGIN SELECT RAISE(ABORT, 'synthetic declaration failure'); END"
        )
    with pytest.raises(Exception, match="synthetic declaration failure"):
        store.authorize_simulation(campaign_id)
    assert store.get_campaign(campaign_id)["authorization"] == "unauthorized"
    assert not store.has_execution_declaration(campaign_id)


def test_lifecycle_export_deactivate_and_deletion_conflict_are_auditable(tmp_path):
    store, service, list_id = _setup(tmp_path, "a@example.com")
    service.add_suppression("a@example.com", SuppressionCategory.HARD_BOUNCE, "permanent failure")
    contact = store.contact_by_email("a@example.com")
    service.correct_contact("a@example.com", display_name="Ada", metadata={"segment": "test"})
    service.deactivate_contact("a@example.com", reason="operator request")
    lifecycle_id = service.plan_deletion("a@example.com", reason="deletion request")
    exported = service.export_contacts(list_id=list_id)
    assert exported[0]["lifecycle_status"] == "deactivated"
    assert exported[0]["display_name"] == "Ada"
    record = next(row for row in store.list_lifecycle_records() if row["id"] == lifecycle_id)
    assert record["retention_conflict"] == 1
    service.import_candidates(
        [ContactCandidate("a@example.com", "a@example.com", "reimport.txt", 1, {})], list_id
    )
    assert store.get_contact(contact["id"])["lifecycle_status"] == "deactivated"


def test_operator_declaration_is_idempotent_and_backup_preserves_policy_state(tmp_path):
    store, service, list_id = _setup(tmp_path, "a@example.com")
    campaign_id = service.create_campaign("backup", list_id)
    service.prepare(campaign_id)
    service.authorize_simulation(campaign_id)
    service.authorize_simulation(campaign_id)
    assert len(store.list_operator_declarations(campaign_id=campaign_id)) == 1
    service.set_preference("a@example.com", state=PreferenceState.DISABLED)
    backup = store.backup_to(tmp_path / "backup.sqlite3")
    restored = CampaignStore.restore_from(backup, tmp_path / "restored.sqlite3")
    restored_store = CampaignStore(restored)
    assert restored_store.schema_version() == 10
    assert len(restored_store.list_preferences()) == 1
    assert len(restored_store.list_operator_declarations(campaign_id=campaign_id)) == 1
    assert restored_store.integrity_check()


def test_running_authorized_campaign_can_record_post_migration_declaration(tmp_path):
    store, service, list_id = _setup(tmp_path, "a@example.com")
    campaign_id = service.create_campaign("running", list_id)
    service.prepare(campaign_id)
    store.transition_campaign(campaign_id, CampaignStatus.RUNNING, "legacy-restart")
    with store.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE campaigns SET authorization = ? WHERE id = ?",
            (ExecutionAuthorization.SIMULATION_AUTHORIZED.value, campaign_id),
        )
    service.authorize_simulation(campaign_id)
    service.authorize_simulation(campaign_id)
    declarations = store.list_operator_declarations(campaign_id=campaign_id)
    assert len(declarations) == 1
    assert "schema migration" in declarations[0]["declaration"]


def test_revision_retry_does_not_consume_sender_capacity(tmp_path, monkeypatch):
    store = CampaignStore(tmp_path / "retry.sqlite3")
    service = OutboundService(store)
    list_id = store.create_list("retry-audience")
    service.campaigns.import_candidates(
        [ContactCandidate("a@example.com", "a@example.com", "test", 0, {})], list_id
    )
    store.upsert_contact(
        "unrelated@example.com",
        source_file="test",
        source_record_position=1,
        original_input="unrelated@example.com",
    )
    campaign_id = service.campaigns.create_campaign(
        "retry", list_id, sender_config_ref="retry-sender"
    )
    sender = SenderPolicy(
        ref="retry-sender",
        provider="synthetic",
        email="sender@example.com",
        max_per_minute=1,
        max_per_hour=1,
        max_per_day=1,
        per_domain_per_minute=1,
        max_concurrency=1,
    )
    service.prepare_campaign(
        campaign_id,
        template=TemplateDefinition(
            name="retry-template",
            version="1",
            subject="Hello",
            text="Hello",
        ),
        sender=sender,
    )
    service.authorize_simulation(campaign_id)
    original = store.mark_attempt_started
    changed = False

    def race(intent_id, attempt_id, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            service.campaigns.set_preference(
                "unrelated@example.com", state=PreferenceState.DISABLED
            )
        return original(intent_id, attempt_id, **kwargs)

    monkeypatch.setattr(store, "mark_attempt_started", race)
    report, transport = service.execute(campaign_id)
    assert len(transport.calls) == 1
    assert report.counts[DeliveryState.ACCEPTED.value] == 1
    with store.connect() as connection:
        attempts = connection.execute(
            "SELECT outcome FROM delivery_attempts WHERE intent_id = ?",
            (store.list_intents(campaign_id)[0]["id"],),
        ).fetchall()
    assert len(attempts) == 2
    assert sum(row["outcome"] == "canceled" for row in attempts) == 1


def test_canceling_reserved_work_cleans_attempt_and_policy_reservation(tmp_path):
    store, service, list_id = _setup(tmp_path, "a@example.com")
    campaign_id = service.create_campaign("cancel-reserved", list_id)
    service.prepare(campaign_id)
    service.authorize_simulation(campaign_id)
    intent = store.reserve_next(campaign_id)
    assert intent is not None
    store.register_sender_policy(
        SenderPolicy(
            ref="synthetic-sender",
            provider="synthetic",
            email="sender@example.com",
            max_per_minute=10,
            max_per_hour=10,
            max_per_day=10,
            per_domain_per_minute=10,
        )
    )
    decision = PolicyEngine(store).reserve(
        attempt_id=intent["attempt_id"],
        intent_id=intent["id"],
        recipient=intent["normalized_email"],
        preferred_ref="synthetic-sender",
    )
    assert decision.allowed
    service.cancel(campaign_id)
    attempt = store.latest_attempt(intent["id"])
    assert attempt["outcome"] == "canceled"
    assert attempt["attempt_state"] == DeliveryState.CANCELED.value
    with store.connect() as connection:
        assert (
            connection.execute(
                "SELECT released_at FROM policy_reservations WHERE attempt_id = ?",
                (intent["attempt_id"],),
            ).fetchone()
            is not None
        )


def test_concurrent_preference_writes_are_transaction_safe(tmp_path):
    store, service, _ = _setup(tmp_path, "a@example.com")

    def write(index: int):
        return service.set_preference(
            "a@example.com",
            purpose="newsletter",
            state=PreferenceState.DISABLED if index % 2 else PreferenceState.ENABLED,
            evidence_ref=f"change-{index}",
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(write, range(8)))
    assert len(set(ids)) == 8
    assert len(store.list_preferences()) == 8
