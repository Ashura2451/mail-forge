from datetime import UTC, datetime, timedelta

import pytest

from mailforge.application.automation import AutomationError, AutomationService
from mailforge.infrastructure.campaign_store import CampaignStore


def _active_service(tmp_path):
    store = CampaignStore(tmp_path / "automation.sqlite3")
    service = AutomationService(store)
    workflow = service.create_workflow("Local nurture")
    graph = {
        "version": 1,
        "nodes": [
            {
                "id": "trigger",
                "kind": "TRIGGER",
                "label": "When this happens",
                "config": {"trigger_kind": "manual_enrollment"},
            },
            {
                "id": "check",
                "kind": "CONDITION",
                "label": "Check whether…",
                "config": {"result": True},
            },
            {
                "id": "add",
                "kind": "ADD_TO_LIST",
                "label": "Then do…",
                "config": {"list_id": "target"},
            },
            {"id": "true-end", "kind": "END", "label": "End", "config": {}},
            {"id": "false-end", "kind": "END", "label": "Otherwise… End", "config": {}},
        ],
        "edges": [
            {"source": "trigger", "target": "check"},
            {"source": "check", "target": "add", "branch": "true"},
            {"source": "check", "target": "false-end", "branch": "false"},
            {"source": "add", "target": "true-end"},
        ],
    }
    target = store.create_list("Target list")
    graph["nodes"][2]["config"]["list_id"] = target
    draft = service.save_revision(workflow["id"], graph)
    service.publish(workflow["id"], draft["id"])
    service.set_lifecycle(workflow["id"], "activate", draft["id"])
    return store, service, workflow["id"], target


def test_workflow_validation_rejects_cycles_and_unreachable_nodes(tmp_path):
    service = AutomationService(CampaignStore(tmp_path / "automation.sqlite3"))
    graph = service.default_graph()
    graph["edges"].append({"source": "end", "target": "trigger"})
    with pytest.raises(AutomationError, match="cycle"):
        service.validate_graph(graph)


def test_workflow_validation_rejects_unbounded_or_ambiguous_waits(tmp_path):
    service = AutomationService(CampaignStore(tmp_path / "automation.sqlite3"))
    graph = service.default_graph()
    graph["nodes"].insert(
        1,
        {
            "id": "wait",
            "kind": "WAIT",
            "label": "Wait until…",
            "config": {"due_at_utc": "tomorrow", "timezone": "UTC"},
        },
    )
    graph["edges"] = [
        {"source": "trigger", "target": "wait"},
        {"source": "wait", "target": "end"},
    ]
    with pytest.raises(AutomationError, match="due_at_utc"):
        service.validate_graph(graph)


def test_workflow_validation_rejects_unsafe_configuration_keys(tmp_path):
    service = AutomationService(CampaignStore(tmp_path / "automation.sqlite3"))
    graph = service.default_graph()
    graph["nodes"][0]["config"]["smtp_password"] = "synthetic-secret"
    with pytest.raises(AutomationError, match="prohibited configuration"):
        service.validate_graph(graph)


def test_enrollment_is_revision_pinned_and_deduplicated(tmp_path):
    store, service, workflow_id, target = _active_service(tmp_path)
    contact_id = store.upsert_contact(
        "person@example.com",
        source_file="synthetic",
        source_record_position=1,
        original_input="person@example.com",
    )
    first = service.enroll(
        workflow_id,
        contact_id=contact_id,
        source_identity="synthetic:event:1",
        provenance={"fixture": True},
    )
    second = service.enroll(
        workflow_id,
        contact_id=contact_id,
        source_identity="synthetic:event:1",
        provenance={"fixture": True},
    )
    assert first["enrolled"] is True
    assert second["deduplicated"] is True
    assert service.run_detail(first["run_id"])["run"]["revision_id"]
    result = service.advance(first["run_id"])
    assert result["run"]["status"] == "completed"
    assert store.list_contacts(target)[0]["id"] == contact_id


def test_same_source_event_can_enroll_distinct_workflows(tmp_path):
    store, service, first_workflow_id, _target = _active_service(tmp_path)
    second = service.create_workflow("Second local nurture")
    revision = service.save_revision(second["id"], service.default_graph())
    service.publish(second["id"], revision["id"])
    service.set_lifecycle(second["id"], "activate", revision["id"])
    contact_id = store.upsert_contact(
        "shared-event@example.com",
        source_file="synthetic",
        source_record_position=1,
        original_input="shared-event@example.com",
    )
    first = service.enroll(
        first_workflow_id,
        contact_id=contact_id,
        source_identity="synthetic:shared-event",
        subject_key="contact:shared",
    )
    second_result = service.enroll(
        second["id"],
        contact_id=contact_id,
        source_identity="synthetic:shared-event",
        subject_key="contact:shared",
    )
    assert first["enrolled"] is True
    assert second_result["enrolled"] is True


def test_run_lease_prevents_a_second_worker_from_advancing(tmp_path):
    store, service, workflow_id, _target = _active_service(tmp_path)
    contact_id = store.upsert_contact(
        "lease@example.com",
        source_file="synthetic",
        source_record_position=1,
        original_input="lease@example.com",
    )
    run = service.enroll(workflow_id, contact_id=contact_id)
    start = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)
    lease_until = (start + timedelta(seconds=60)).isoformat()
    with store.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE automation_runs SET status = 'running', worker_id = ?, "
            "lease_expires_at = ? WHERE id = ?",
            ("worker-a", lease_until, run["run_id"]),
        )

    blocked = service.advance(run["run_id"], now=start + timedelta(seconds=1), worker_id="worker-b")
    assert blocked["run"]["status"] == "running"
    assert blocked["run"]["worker_id"] == "worker-a"


def test_wait_is_durable_across_clock_advance(tmp_path):
    store = CampaignStore(tmp_path / "automation.sqlite3")
    service = AutomationService(store)
    workflow = service.create_workflow("Wait flow")
    graph = {
        "version": 1,
        "nodes": [
            {
                "id": "trigger",
                "kind": "TRIGGER",
                "label": "When this happens",
                "config": {"trigger_kind": "manual_enrollment"},
            },
            {
                "id": "wait",
                "kind": "WAIT",
                "label": "Wait until…",
                "config": {"seconds": 60, "timezone": "UTC"},
            },
            {"id": "end", "kind": "END", "label": "End", "config": {}},
        ],
        "edges": [
            {"source": "trigger", "target": "wait"},
            {"source": "wait", "target": "end"},
        ],
    }
    draft = service.save_revision(workflow["id"], graph)
    service.publish(workflow["id"], draft["id"])
    service.set_lifecycle(workflow["id"], "activate", draft["id"])
    contact_id = store.upsert_contact(
        "wait@example.com",
        source_file="synthetic",
        source_record_position=1,
        original_input="wait@example.com",
    )
    start = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)
    run = service.enroll(workflow["id"], contact_id=contact_id)
    waiting = service.advance(run["run_id"], now=start)
    assert waiting["run"]["status"] == "waiting"
    assert waiting["run"]["due_at_utc"]
    assert (
        service.advance(run["run_id"], now=start + timedelta(seconds=61))["run"]["status"]
        == "completed"
    )
