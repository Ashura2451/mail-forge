import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from mailforge.application.campaigns import CampaignService, ScriptedTransport, run_synthetic_demo
from mailforge.application.legacy_migration import rehearse_legacy_migration
from mailforge.domain.campaign import (
    AttemptOutcome,
    CampaignStatus,
    ContactCandidate,
    DeliveryResult,
    DeliveryState,
    SuppressionCategory,
)
from mailforge.infrastructure.campaign_store import CampaignStore
from mailforge.interfaces.cli import main


def make_campaign(tmp_path: Path):
    store = CampaignStore(tmp_path / "state.sqlite3")
    service = CampaignService(store)
    list_id = store.create_list("audience")
    return store, service, list_id


def import_emails(service, list_id, *emails):
    return service.import_candidates(
        [
            ContactCandidate(email, email, "synthetic.txt", position, {"source": "test"})
            for position, email in enumerate(emails, 1)
        ],
        list_id,
    )


def test_contact_import_is_durable_deduplicated_and_provenance_preserving(tmp_path):
    store, service, list_id = make_campaign(tmp_path)
    result = import_emails(service, list_id, "one@example.com", "one@example.com")

    assert result == {"records_imported": 2, "unique_contacts": 1}
    assert len(store.list_contacts(list_id)) == 1
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM contact_sources").fetchone()[0] == 2
    assert store.integrity_check()


def test_suppression_overrides_prepare_and_reimport(tmp_path):
    store, service, list_id = make_campaign(tmp_path)
    import_emails(service, list_id, "unsubscribe@example.com")
    service.add_suppression(
        "unsubscribe@example.com", SuppressionCategory.UNSUBSCRIBE, "operator unsubscribe"
    )
    campaign_id = service.create_campaign("suppressed", list_id)

    report = service.prepare(campaign_id)
    assert report.counts[DeliveryState.SUPPRESSED.value] == 1
    import_emails(service, list_id, "unsubscribe@example.com")
    assert service.prepare(campaign_id).counts[DeliveryState.SUPPRESSED.value] == 1


def test_campaign_transitions_and_simulation_outcomes_are_explicit(tmp_path):
    store, service, list_id = make_campaign(tmp_path)
    import_emails(
        service,
        list_id,
        "accepted@example.com",
        "transient@example.com",
        "permanent@example.com",
        "ambiguous@example.com",
    )
    campaign_id = service.create_campaign("outcomes", list_id)
    service.prepare(campaign_id)
    with pytest.raises(PermissionError):
        service.execute(campaign_id, ScriptedTransport())
    service.authorize_simulation(campaign_id)
    report = service.execute(
        campaign_id,
        ScriptedTransport(
            {
                "transient@example.com": AttemptOutcome.TRANSIENT_FAILURE,
                "permanent@example.com": AttemptOutcome.PERMANENT_FAILURE,
                "ambiguous@example.com": AttemptOutcome.AMBIGUOUS,
            }
        ),
    )

    assert report.campaign_status == CampaignStatus.PAUSED.value
    assert report.counts[DeliveryState.ACCEPTED.value] == 1
    assert report.counts[DeliveryState.DEFERRED.value] == 1
    assert report.counts[DeliveryState.PERMANENT_FAILURE.value] == 1
    assert report.counts[DeliveryState.AMBIGUOUS.value] == 1
    assert "ambiguous" in report.unresolved

    retry = ScriptedTransport()
    service.execute(campaign_id, retry)
    assert retry.calls == []


def test_deferred_retry_is_bounded(tmp_path):
    store, service, list_id = make_campaign(tmp_path)
    import_emails(service, list_id, "retry@example.com")
    campaign_id = service.create_campaign("retry", list_id)
    service.prepare(campaign_id)
    service.authorize_simulation(campaign_id)
    transport = ScriptedTransport({"retry@example.com": AttemptOutcome.TRANSIENT_FAILURE})

    report = service.execute(campaign_id, transport, retry_deferred=True)

    assert len(transport.calls) == 3
    assert report.counts[DeliveryState.DEFERRED.value] == 1
    assert report.campaign_status == CampaignStatus.PAUSED.value


def test_pause_request_is_consumed_by_resume(tmp_path):
    store, service, list_id = make_campaign(tmp_path)
    import_emails(service, list_id, "pause-one@example.com", "pause-two@example.com")
    campaign_id = service.create_campaign("pause", list_id)
    service.prepare(campaign_id)
    service.authorize_simulation(campaign_id)
    with pytest.raises(ValueError, match="only be paused while running"):
        service.pause(campaign_id)

    class PauseAfterOne:
        def __init__(self):
            self.calls = 0

        def send(self, email, intent_id, attempt_number):
            self.calls += 1
            if self.calls == 1:
                service.pause(campaign_id)
            return DeliveryResult(AttemptOutcome.ACCEPTED)

    first_transport = PauseAfterOne()
    first = service.execute(campaign_id, first_transport)
    assert first.campaign_status == CampaignStatus.PAUSED.value
    assert first_transport.calls == 1
    second = service.execute(campaign_id, ScriptedTransport())
    assert second.campaign_status == CampaignStatus.COMPLETED.value


def test_inflight_cancellation_keeps_intent_canceled(tmp_path):
    store, service, list_id = make_campaign(tmp_path)
    import_emails(service, list_id, "cancel-race@example.com")
    campaign_id = service.create_campaign("cancel-race", list_id)
    service.prepare(campaign_id)
    service.authorize_simulation(campaign_id)

    class CancelDuringSend:
        def send(self, email, intent_id, attempt_number):
            service.cancel(campaign_id)
            return DeliveryResult(AttemptOutcome.ACCEPTED)

    service.execute(campaign_id, CancelDuringSend())
    intent = store.list_intents(campaign_id)[0]
    assert intent["state"] == DeliveryState.CANCELED.value


def test_suppression_added_after_prepare_blocks_resume(tmp_path):
    store, service, list_id = make_campaign(tmp_path)
    import_emails(service, list_id, "later@example.com", "other@example.com")
    campaign_id = service.create_campaign("late-suppression", list_id)
    service.prepare(campaign_id)
    service.authorize_simulation(campaign_id)
    service.add_suppression("later@example.com", SuppressionCategory.MANUAL, "new exclusion")

    report = service.execute(campaign_id, ScriptedTransport(), stop_after=1)
    assert report.counts[DeliveryState.SUPPRESSED.value] == 1
    assert report.counts[DeliveryState.ACCEPTED.value] == 1


def test_crash_after_transport_becomes_ambiguous_and_is_not_retried(tmp_path):
    store, service, list_id = make_campaign(tmp_path)
    import_emails(service, list_id, "crash@example.com")
    campaign_id = service.create_campaign("crash", list_id)
    service.prepare(campaign_id)
    service.authorize_simulation(campaign_id)
    first = service.execute(
        campaign_id,
        ScriptedTransport(crash_after={"crash@example.com"}),
    )
    assert first.counts[DeliveryState.AMBIGUOUS.value] == 1
    second_transport = ScriptedTransport()
    service.execute(campaign_id, second_transport)
    assert second_transport.calls == []


def test_concurrent_reservation_is_single_owner(tmp_path):
    store, service, list_id = make_campaign(tmp_path)
    import_emails(service, list_id, "one@example.com", "two@example.com")
    campaign_id = service.create_campaign("concurrent", list_id)
    service.prepare(campaign_id)

    def reserve():
        row = store.reserve_next(campaign_id)
        return row["id"] if row else None

    with ThreadPoolExecutor(max_workers=2) as pool:
        reserved = list(pool.map(lambda _: reserve(), range(2)))
    assert sorted(item for item in reserved if item) == sorted(set(reserved))
    assert len([item for item in reserved if item]) == 2


def test_restart_recovery_distinguishes_reserved_and_started_work(tmp_path):
    store, service, list_id = make_campaign(tmp_path)
    import_emails(service, list_id, "reserved@example.com", "started@example.com")
    campaign_id = service.create_campaign("recovery", list_id)
    service.prepare(campaign_id)

    reserved = store.reserve_next(campaign_id)
    assert reserved is not None
    started = store.reserve_next(campaign_id)
    assert started is not None
    assert store.mark_attempt_started(started["id"], started["attempt_id"])

    assert store.recover_inflight(campaign_id) == {"reserved": 1, "ambiguous": 1}
    states = {row["normalized_email"]: row["state"] for row in store.list_intents(campaign_id)}
    assert states["reserved@example.com"] == DeliveryState.ELIGIBLE_PENDING.value
    assert states["started@example.com"] == DeliveryState.AMBIGUOUS.value
    assert store.latest_attempt(started["id"])["outcome"] == AttemptOutcome.AMBIGUOUS.value


def test_cancel_marks_pending_work_without_authorizing_transport(tmp_path):
    store, service, list_id = make_campaign(tmp_path)
    import_emails(service, list_id, "cancel@example.com")
    campaign_id = service.create_campaign("cancel", list_id)
    service.prepare(campaign_id)
    service.cancel(campaign_id)

    assert store.get_campaign(campaign_id)["status"] == CampaignStatus.CANCELED.value
    assert store.list_intents(campaign_id)[0]["state"] == DeliveryState.CANCELED.value


def test_legacy_migration_maps_uncertain_records_without_requeue(tmp_path):
    store, service, list_id = make_campaign(tmp_path)
    campaign_id = service.create_campaign("legacy", list_id)
    sent = tmp_path / "sent.txt"
    failed = tmp_path / "failed.txt"
    bounces = tmp_path / "hard_bounces.txt"
    sent.write_text("sent@example.com\n", encoding="utf-8")
    failed.write_text("failed@example.com\n", encoding="utf-8")
    bounces.write_text("bounced@example.com\n", encoding="utf-8")

    result = rehearse_legacy_migration(
        store,
        list_id=list_id,
        campaign_id=campaign_id,
        sent_path=sent,
        failed_path=failed,
        hard_bounces_path=bounces,
    )
    assert result == {"sent_ambiguous": 1, "failed_deferred": 1, "hard_bounces": 1}
    states = {row["normalized_email"]: row["state"] for row in store.list_intents(campaign_id)}
    assert states["sent@example.com"] == DeliveryState.AMBIGUOUS.value
    assert states["failed@example.com"] == DeliveryState.DEFERRED.value
    assert states["bounced@example.com"] == DeliveryState.PERMANENT_FAILURE.value
    assert store.suppression_reason(
        normalized_email="bounced@example.com",
        contact_id=next(
            row["id"]
            for row in store.list_contacts(list_id)
            if row["normalized_email"] == "bounced@example.com"
        ),
        list_id=list_id,
        campaign_id=campaign_id,
        sender_config_ref="synthetic-sender",
    )
    rehearse_legacy_migration(
        store,
        list_id=list_id,
        campaign_id=campaign_id,
        sent_path=sent,
        failed_path=failed,
        hard_bounces_path=bounces,
    )
    assert (
        store.latest_attempt(
            next(
                row["id"]
                for row in store.list_intents(campaign_id)
                if row["normalized_email"] == "bounced@example.com"
            )
        )["attempt_number"]
        == 1
    )


def test_backup_restore_and_secret_rejection(tmp_path):
    store, service, list_id = make_campaign(tmp_path)
    import_emails(service, list_id, "backup@example.com")
    backup = store.backup_to(tmp_path / "backup.sqlite3")
    restored = tmp_path / "restored.sqlite3"
    CampaignStore.restore_from(backup, restored)
    restored_store = CampaignStore(restored)
    assert len(restored_store.list_contacts(list_id)) == 1
    with pytest.raises(ValueError, match="secret-bearing"):
        store.create_campaign(
            name="unsafe",
            description="",
            sender_config_ref="synthetic",
            template_ref="synthetic",
            audience_list_id=list_id,
            configuration={"smtp_password": "synthetic-secret"},
        )
    with pytest.raises(ValueError, match="secret-bearing"):
        store.create_campaign(
            name="unsafe-reference",
            description="",
            sender_config_ref="password=synthetic-secret",
            template_ref="synthetic",
            audience_list_id=list_id,
        )
    with pytest.raises(ValueError, match="secret-bearing"):
        store.create_list("token=synthetic-secret")
    with pytest.raises(ValueError, match="secret-bearing"):
        store.upsert_contact(
            "provenance@example.com",
            source_file="token=synthetic-secret",
            source_record_position=1,
            original_input="provenance@example.com",
        )
    with pytest.raises(ValueError, match="invalid email identity"):
        store.upsert_contact(
            "not-an-email",
            source_file="synthetic.txt",
            source_record_position=1,
            original_input="not-an-email",
        )
    with pytest.raises(FileNotFoundError):
        CampaignStore.restore_from(tmp_path / "missing.sqlite3", tmp_path / "bad.sqlite3")
    with pytest.raises(FileNotFoundError):
        main(
            [
                "campaign",
                "backup",
                "--database",
                str(tmp_path / "missing.sqlite3"),
                "--destination",
                str(tmp_path / "bad-backup.sqlite3"),
            ]
        )


def test_synthetic_demo_is_end_to_end_and_json_serializable(tmp_path):
    result = run_synthetic_demo(tmp_path / "demo.sqlite3")
    assert result["preview"]["campaign_status"] == CampaignStatus.READY.value
    assert result["final"]["campaign_status"] == CampaignStatus.PAUSED.value
    assert "ambiguous" in result["final"]["unresolved"]
    json.dumps(result)


def test_campaign_cli_demo_uses_synthetic_database(tmp_path, capsys):
    database = tmp_path / "cli-demo.sqlite3"
    assert main(["campaign", "demo", "--database", str(database)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["preview"]["campaign_status"] == CampaignStatus.READY.value
    assert output["final"]["campaign_status"] == CampaignStatus.PAUSED.value
