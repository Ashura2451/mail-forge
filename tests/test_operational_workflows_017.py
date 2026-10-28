import time
from datetime import UTC, datetime, timedelta

from mailforge.application.automation import AutomationService
from mailforge.application.campaigns import CampaignService
from mailforge.application.list_processing import ListProcessingService
from mailforge.infrastructure.campaign_store import CampaignStore


def _active_workflow(store, trigger_kind, config=None):
    service = AutomationService(store, campaigns=CampaignService(store))
    workflow = service.create_workflow(f"workflow-{trigger_kind}-{time.time_ns()}")
    graph = workflow["draft"]["graph"]
    graph["nodes"][0]["config"] = {"trigger_kind": trigger_kind, **(config or {})}
    service.save_revision(workflow["id"], graph, revision_id=workflow["draft"]["id"])
    service.publish(workflow["id"])
    service.set_lifecycle(workflow["id"], "activate")
    return service, workflow["id"]


def test_operational_contact_event_is_automatic_and_idempotent(tmp_path):
    store = CampaignStore(tmp_path / "events.sqlite3")
    service, workflow_id = _active_workflow(store, "contact_imported")
    contact_id = store.upsert_contact(
        "event@example.com",
        source_file="synthetic.txt",
        source_record_position=1,
        original_input="event@example.com",
    )

    first = service.reconcile_operational_events()
    second = service.reconcile_operational_events()
    assert len(first) == 1
    assert second == []
    assert service.list_runs(workflow_id)["total"] == 1
    assert contact_id


def test_scheduled_occurrence_uses_durable_utc_state(tmp_path):
    store = CampaignStore(tmp_path / "schedule.sqlite3")
    due = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    service, workflow_id = _active_workflow(
        store, "scheduled_occurrence", {"due_at_utc": due, "subject_key": "synthetic-schedule"}
    )
    runs = service.reconcile_schedules(now=datetime.now(UTC))
    assert len(runs) == 1
    assert service.list_runs(workflow_id)["total"] == 1
    assert service.reconcile_schedules(now=datetime.now(UTC)) == []


def test_list_tools_completes_without_contact_side_effect(tmp_path):
    store = CampaignStore(tmp_path / "list-tools.sqlite3")
    source = tmp_path / "contacts.csv"
    source.write_text("email\nA@example.com\ninvalid\nA@example.com\n", encoding="utf-8")
    service = ListProcessingService(store, tmp_path / "jobs")
    try:
        job = service.create_job(source, source.name)
        service.start(job["id"])
        for _ in range(200):
            current = service.get_job(job["id"])
            if current["status"] not in {"pending", "running"}:
                break
            time.sleep(0.01)
        assert current["status"] == "completed"
        assert current["accepted_count"] == 1
        assert any(item["kind"] == "master_csv" for item in current["artifacts"])
        with store.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 0
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM operational_events "
                    "WHERE event_type = 'list_processing_completed'"
                ).fetchone()[0]
                == 1
            )
    finally:
        service.close()
