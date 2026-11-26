from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mailforge.application.campaigns import CampaignService, ScriptedTransport
from mailforge.application.outbound import OutboundService
from mailforge.domain.campaign import CampaignStatus, DeliveryState
from mailforge.domain.outbound import ScheduleMode, ScheduleSpec
from mailforge.infrastructure.campaign_store import CampaignStore
from mailforge.interfaces.web import CampaignScheduleSupervisor, WebRuntime


class _RecordingWorkers:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.campaign_ids: list[str] = []

    def start(self, campaign_id: str) -> bool:
        self.campaign_ids.append(campaign_id)
        self.started.set()
        return True


def _paused_campaign(store: CampaignStore, *, name: str) -> str:
    list_id = store.create_list(f"{name}-audience")
    contact_id = store.upsert_contact(
        f"{name}@example.com",
        source_file="test-fixture",
        source_record_position=1,
        original_input=f"{name}@example.com",
    )
    store.add_to_list(list_id, contact_id)
    campaign_id = store.create_campaign(
        name=name,
        description="unattended continuation fixture",
        sender_config_ref=f"{name}-sender",
        template_ref=f"{name}-template",
        audience_list_id=list_id,
        configuration={"execution_mode": "external_smtp"},
    )
    store.prepare_intent(
        campaign_id=campaign_id,
        contact_id=contact_id,
        normalized_email=f"{name}@example.com",
        state=DeliveryState.ELIGIBLE_PENDING,
        eligibility_reason="fixture eligible",
    )
    store.transition_campaign(campaign_id, CampaignStatus.VALIDATING, "fixture")
    store.transition_campaign(campaign_id, CampaignStatus.READY, "fixture")
    store.authorize_simulation(campaign_id)
    store.set_campaign_execution_mode(campaign_id, "external_smtp")
    store.transition_campaign(campaign_id, CampaignStatus.RUNNING, "fixture")
    store.transition_campaign(campaign_id, CampaignStatus.COMPLETING, "fixture")
    store.transition_campaign(
        campaign_id,
        CampaignStatus.PAUSED,
        "retry_after_temporary_restriction",
    )
    return campaign_id


def test_automatic_boundary_excludes_submitted_deferred_outcomes(tmp_path: Path) -> None:
    store = CampaignStore(tmp_path / "deferred.sqlite3")
    campaign_id = _paused_campaign(store, name="deferred")
    with store.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE delivery_intents SET state = ?, not_before_utc = NULL WHERE campaign_id = ?",
            (DeliveryState.DEFERRED.value, campaign_id),
        )

    assert store.campaign_next_automatic_at(campaign_id) is None


def test_temporary_wait_does_not_cross_the_transport_boundary(tmp_path: Path) -> None:
    store = CampaignStore(tmp_path / "boundary.sqlite3")
    campaign_id = _paused_campaign(store, name="boundary")
    transport = ScriptedTransport()
    retry_at = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()
    report = CampaignService(store).execute(
        campaign_id,
        transport,
        dispatch_guard=lambda _intent: (False, "provider cooldown", retry_at),
    )

    assert report.campaign_status == CampaignStatus.PAUSED.value
    assert transport.calls == []
    assert store.campaign_next_automatic_at(campaign_id) == retry_at


def test_scheduler_started_worker_cannot_clear_a_new_operator_pause(tmp_path: Path) -> None:
    store = CampaignStore(tmp_path / "pause-race.sqlite3")
    campaign_id = _paused_campaign(store, name="pause-race")
    store.request_pause(campaign_id)
    transport = ScriptedTransport()
    report = CampaignService(store).execute(
        campaign_id,
        transport,
        allow_pause_clear=False,
    )

    assert report.campaign_status == CampaignStatus.PAUSED.value
    assert transport.calls == []
    assert store.campaign_flags(campaign_id)[0] is True


def test_scheduler_starts_authorized_external_campaign_after_wait(tmp_path: Path) -> None:
    store = CampaignStore(tmp_path / "scheduler.sqlite3")
    campaign_id = _paused_campaign(store, name="external-wait")
    store.upsert_schedule(
        campaign_id,
        ScheduleSpec(mode=ScheduleMode.IMMEDIATE, pacing_seconds=0.1),
    )
    workers = _RecordingWorkers()
    supervisor = CampaignScheduleSupervisor(store, OutboundService(store), workers)
    try:
        assert workers.started.wait(timeout=1.0)
        assert workers.campaign_ids == [campaign_id]
    finally:
        supervisor.shutdown()


def test_scheduler_does_not_restart_submitted_deferred_outcomes(tmp_path: Path) -> None:
    store = CampaignStore(tmp_path / "deferred-scheduler.sqlite3")
    campaign_id = _paused_campaign(store, name="deferred-scheduler")
    campaign = store.get_campaign(campaign_id)
    second_contact = store.upsert_contact(
        "second-deferred-scheduler@example.com",
        source_file="test-fixture",
        source_record_position=2,
        original_input="second-deferred-scheduler@example.com",
    )
    store.add_to_list(campaign["audience_list_id"], second_contact)
    store.prepare_intent(
        campaign_id=campaign_id,
        contact_id=second_contact,
        normalized_email="second-deferred-scheduler@example.com",
        state=DeliveryState.PREPARED,
        eligibility_reason="fixture prepared",
    )
    with store.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE delivery_intents SET state = ?, not_before_utc = NULL "
            "WHERE campaign_id = ? AND normalized_email = ?",
            (DeliveryState.DEFERRED.value, campaign_id, "deferred-scheduler@example.com"),
        )
    store.upsert_schedule(campaign_id, ScheduleSpec(mode=ScheduleMode.IMMEDIATE))
    workers = _RecordingWorkers()
    supervisor = CampaignScheduleSupervisor(store, OutboundService(store), workers)
    try:
        assert not workers.started.wait(timeout=0.25)
    finally:
        supervisor.shutdown()


def test_scheduler_fences_pending_work_when_another_attempt_is_ambiguous(tmp_path: Path) -> None:
    store = CampaignStore(tmp_path / "ambiguous-scheduler.sqlite3")
    campaign_id = _paused_campaign(store, name="ambiguous-scheduler")
    campaign = store.get_campaign(campaign_id)
    second_contact = store.upsert_contact(
        "second-ambiguous-scheduler@example.com",
        source_file="test-fixture",
        source_record_position=2,
        original_input="second-ambiguous-scheduler@example.com",
    )
    store.add_to_list(campaign["audience_list_id"], second_contact)
    store.prepare_intent(
        campaign_id=campaign_id,
        contact_id=second_contact,
        normalized_email="second-ambiguous-scheduler@example.com",
        state=DeliveryState.PREPARED,
        eligibility_reason="fixture prepared",
    )
    first_intent = store.list_intents(campaign_id)[0]
    store.set_intent_state(first_intent["id"], DeliveryState.AMBIGUOUS, "fixture uncertain")
    store.upsert_schedule(campaign_id, ScheduleSpec(mode=ScheduleMode.IMMEDIATE))
    workers = _RecordingWorkers()
    supervisor = CampaignScheduleSupervisor(store, OutboundService(store), workers)
    try:
        assert not workers.started.wait(timeout=0.25)
    finally:
        supervisor.shutdown()


def test_scheduler_does_not_resume_a_campaign_paused_by_restart(tmp_path: Path) -> None:
    store = CampaignStore(tmp_path / "restart-scheduler.sqlite3")
    campaign_id = _paused_campaign(store, name="restart-scheduler")
    store.transition_campaign(campaign_id, CampaignStatus.RUNNING, "fixture_restart")
    store.transition_campaign(campaign_id, CampaignStatus.PAUSING, "application_restarted")
    store.transition_campaign(campaign_id, CampaignStatus.PAUSED, "application_restarted")
    store.upsert_schedule(campaign_id, ScheduleSpec(mode=ScheduleMode.IMMEDIATE))
    workers = _RecordingWorkers()
    supervisor = CampaignScheduleSupervisor(store, OutboundService(store), workers)
    try:
        assert not workers.started.wait(timeout=0.25)
    finally:
        supervisor.shutdown()


def test_workspace_explains_temporary_wait_and_manual_pause(tmp_path: Path) -> None:
    database = tmp_path / "workspace.sqlite3"
    runtime = WebRuntime(database)
    campaign_id = _paused_campaign(runtime.store, name="workspace")
    future = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()
    with runtime.store.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE delivery_intents SET not_before_utc = ? WHERE campaign_id = ?",
            (future, campaign_id),
        )

    try:
        workspace = runtime.campaign_operations.workspace(campaign_id)
        assert workspace["execution"]["state"]["key"] == "waiting"
        assert workspace["execution"]["state"]["label"] == (
            "Waiting for an execution restriction"
        )
        runtime.store.request_pause(campaign_id)
        manual = runtime.campaign_operations.workspace(campaign_id)
        assert manual["execution"]["state"]["key"] == "manually_paused"
        assert manual["execution"]["state"]["automatic_continuation"] is False
    finally:
        runtime.close()


def test_workspace_explains_deferred_submission_as_reconciliation(tmp_path: Path) -> None:
    runtime = WebRuntime(tmp_path / "deferred-workspace.sqlite3")
    campaign_id = _paused_campaign(runtime.store, name="deferred-workspace")
    with runtime.store.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE delivery_intents SET state = ?, not_before_utc = NULL WHERE campaign_id = ?",
            (DeliveryState.DEFERRED.value, campaign_id),
        )
    try:
        workspace = runtime.campaign_operations.workspace(campaign_id)
        assert workspace["execution"]["state"]["key"] == "requires_reconciliation"
        assert workspace["execution"]["state"]["automatic_continuation"] is False
    finally:
        runtime.close()
