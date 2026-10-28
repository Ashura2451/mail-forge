import json
import time
from pathlib import Path

import pytest

from mailforge.application.automation import AutomationService
from mailforge.application.data_studio import DataStudioService
from mailforge.infrastructure.campaign_store import CampaignStore


def _wait(service: DataStudioService, job_id: str) -> dict:
    for _ in range(300):
        job = service.get_job(job_id)
        if job["status"] not in {"pending", "running"}:
            return job
        time.sleep(0.01)
    return service.get_job(job_id)


def test_multi_source_join_is_bounded_provenanced_and_csv_safe(tmp_path):
    left = tmp_path / "people.csv"
    right = tmp_path / "plans.csv"
    left.write_text("email,name\na@example.com,Ada\nb@example.com,Bob\n", encoding="utf-8")
    right.write_text("email,plan\na@example.com,=PROTECTED\n", encoding="utf-8")
    service = DataStudioService(CampaignStore(tmp_path / "state.sqlite3"), tmp_path / "studio")
    try:
        recipe = service.create_recipe(
            "Join export",
            definition={
                "combine": {
                    "operation": "join",
                    "keys": ["email"],
                    "join_type": "left",
                    "key_normalization": "lower",
                },
                "output": {"format": "csv", "include_provenance": True},
                "steps": [],
            },
        )
        job = service.create_multi_source_job(
            [(left, left.name, "people"), (right, right.name, "plans")],
            recipe["current_revision_id"],
        )
        service.start(job["id"])
        job = _wait(service, job["id"])
        assert job["status"] == "completed"
        output = next(item for item in job["artifacts"] if item["kind"] == "output")
        path, _ = service.artifact_path(job["id"], output["id"])
        content = path.read_text(encoding="utf-8")
        assert "' =PROTECTED" in content or "'=PROTECTED" in content
        rows = list(service.iter_artifact_records(job["id"], output["id"]))
        assert len(rows) == 2
        assert rows[0]["plan"] == "'=PROTECTED"
        assert len(json.loads(rows[0]["provenance"])) == 2
        assert rows[1]["plan"] == ""
    finally:
        service.close()


def test_join_collision_suffix_does_not_overwrite_existing_fields(tmp_path):
    left = tmp_path / "left.jsonl"
    right = tmp_path / "right.jsonl"
    left.write_text('{"id":"1","name":"left","name_right":"preserve"}\n', encoding="utf-8")
    right.write_text('{"id":"1","name":"right"}\n', encoding="utf-8")
    service = DataStudioService(CampaignStore(tmp_path / "state.sqlite3"), tmp_path / "studio")
    try:
        result = service.preview_sources(
            [(left, left.name, "left"), (right, right.name, "right")],
            {
                "combine": {"operation": "join", "keys": ["id"], "join_type": "inner"},
                "steps": [],
            },
        )
        assert result["rows"][0]["name_right"] == "preserve"
        assert result["rows"][0]["name_right2"] == "right"
    finally:
        service.close()


def test_aggregate_spans_processing_batches(tmp_path):
    source = tmp_path / "rows.jsonl"
    source.write_text(
        "".join('{"group":"same","amount":1}\n' for _ in range(501)), encoding="utf-8"
    )
    service = DataStudioService(CampaignStore(tmp_path / "state.sqlite3"), tmp_path / "studio")
    try:
        recipe = service.create_recipe(
            "Batch aggregate",
            definition={
                "steps": [
                    {"type": "cast", "field": "amount", "target": "number"},
                    {"type": "aggregate", "group_by": ["group"], "sum": ["amount"]},
                ]
            },
        )
        job = service.create_job(source, source.name, recipe["current_revision_id"])
        service.start(job["id"])
        job = _wait(service, job["id"])
        assert job["status"] == "completed"
        output = next(item for item in job["artifacts"] if item["kind"] == "output")
        rows = list(service.iter_artifact_records(job["id"], output["id"]))
        assert rows == [{"amount_sum": 501.0, "count": 501, "group": "same", "provenance": []}]
    finally:
        service.close()


@pytest.mark.parametrize("operation,expected", [("intersect", 1), ("subtract", 1)])
def test_multi_source_set_operations_use_explicit_keys(tmp_path, operation, expected):
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    first.write_text('{"email":"a@example.com"}\n{"email":"b@example.com"}\n', encoding="utf-8")
    second.write_text('{"email":"a@example.com"}\n', encoding="utf-8")
    service = DataStudioService(CampaignStore(tmp_path / "state.sqlite3"), tmp_path / "studio")
    try:
        result = service.preview_sources(
            [(first, first.name, "first"), (second, second.name, "second")],
            {"combine": {"operation": operation, "keys": ["email"]}, "steps": []},
        )
        assert result["counts"]["output"] == expected
    finally:
        service.close()


def test_union_rejects_incompatible_shared_field_types(tmp_path):
    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"
    first.write_text("email,count\na@example.com,1\n", encoding="utf-8")
    second.write_text("email,count\nb@example.com,not-a-number\n", encoding="utf-8")
    service = DataStudioService(CampaignStore(tmp_path / "state.sqlite3"), tmp_path / "studio")
    try:
        with pytest.raises(ValueError, match="incompatible types"):
            list(
                service.preview_sources(
                    [(first, first.name, "first"), (second, second.name, "second")],
                    {"combine": {"operation": "union"}, "steps": []},
                )["rows"]
            )
    finally:
        service.close()


def test_trusted_extension_requires_explicit_enablement(tmp_path):
    source = tmp_path / "source.json"
    source.write_text(json.dumps([{"email": "Ada@Example.com"}]), encoding="utf-8")
    service = DataStudioService(CampaignStore(tmp_path / "state.sqlite3"), tmp_path / "studio")
    try:
        service.register_extension()
        definition = {"steps": [{"type": "extension", "extension_id": "email_domain_normalizer"}]}
        with pytest.raises(ValueError, match="disabled"):
            service.preview(source, definition)
        service.set_extension_enabled("email_domain_normalizer", True)
        result = service.preview(source, definition)
        assert result["rows"][0]["email_domain"] == "example.com"
    finally:
        service.close()


def test_workflow_runs_pinned_data_studio_revision_idempotently(tmp_path):
    store = CampaignStore(tmp_path / "state.sqlite3")
    service = DataStudioService(store, tmp_path / "studio")
    source = tmp_path / "source.jsonl"
    source.write_text('{"email":"a@example.com"}\n', encoding="utf-8")
    try:
        recipe = service.create_recipe("Workflow recipe", definition={"steps": []})
        initial = service.create_job(source, source.name, recipe["current_revision_id"])
        service.start(initial["id"])
        initial = _wait(service, initial["id"])
        artifact = next(item for item in initial["artifacts"] if item["kind"] == "output")
        contact_id = store.upsert_contact(
            "operator@example.com",
            source_file="synthetic",
            source_record_position=1,
            original_input="operator@example.com",
        )
        automation = AutomationService(store, data_studio=service)
        workflow = automation.create_workflow("Data Studio workflow")
        graph = {
            "version": 1,
            "nodes": [
                {
                    "id": "trigger",
                    "kind": "TRIGGER",
                    "config": {"trigger_kind": "manual_enrollment"},
                },
                {
                    "id": "run-data",
                    "kind": "RUN_DATA_STUDIO",
                    "config": {
                        "recipe_revision_id": recipe["current_revision_id"],
                        "source_job_id": initial["id"],
                        "source_artifact_id": artifact["id"],
                        "destination": "artifact",
                    },
                },
                {"id": "end", "kind": "END", "config": {}},
            ],
            "edges": [
                {"source": "trigger", "target": "run-data"},
                {"source": "run-data", "target": "end"},
            ],
        }
        draft = automation.save_revision(workflow["id"], graph)
        automation.publish(workflow["id"], draft["id"])
        automation.set_lifecycle(workflow["id"], "activate", draft["id"])
        enrollment = automation.enroll(workflow["id"], contact_id=contact_id)
        detail = automation.advance(enrollment["run_id"], max_steps=20)
        assert detail["run"]["status"] == "completed"
        step = next(item for item in detail["steps"] if item["node_key"] == "run-data")
        assert (
            json.loads(step["output_json"])["recipe_revision_id"] == recipe["current_revision_id"]
        )
        duplicate = automation.enroll(workflow["id"], contact_id=contact_id)
        assert duplicate["deduplicated"] is True
    finally:
        service.close()


def test_running_job_recovers_cleans_partial_output_and_retries(tmp_path, monkeypatch):
    source = tmp_path / "source.jsonl"
    source.write_text(
        "".join(json.dumps({"email": f"person-{i}@example.com"}) + "\n" for i in range(2_000))
    )
    database = tmp_path / "state.sqlite3"
    root = tmp_path / "studio"
    service = DataStudioService(CampaignStore(database), root)
    recipe = service.create_recipe("Recovery", definition={"steps": []})
    job = service.create_job(source, source.name, recipe["current_revision_id"])
    original_execute = service._execute_rows

    def slow_execute(*args, **kwargs):
        time.sleep(0.02)
        return original_execute(*args, **kwargs)

    monkeypatch.setattr(service, "_execute_rows", slow_execute)
    service.start(job["id"])
    for _ in range(100):
        if service.get_job(job["id"])["status"] == "running":
            break
        time.sleep(0.005)
    canceled = service.cancel(job["id"])
    assert canceled["status"] == "canceled"
    with service.store.connect() as connection:
        job_dir = Path(
            connection.execute(
                "SELECT job_dir FROM data_studio_jobs WHERE id=?", (job["id"],)
            ).fetchone()[0]
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM data_studio_artifacts WHERE job_id=?", (job["id"],)
            ).fetchone()[0]
            == 0
        )
    assert not (job_dir / "output.rows.jsonl.tmp").exists()
    service.close()

    recovered_store = CampaignStore(database)
    with recovered_store.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE data_studio_jobs SET status='running',error_message=NULL,"
            "updated_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", job["id"]),
        )
        (job_dir / "output.rows.jsonl.tmp").write_text("partial\n", encoding="utf-8")
    recovered = DataStudioService(recovered_store, root)
    assert recovered.get_job(job["id"])["status"] == "pending"
    assert not (job_dir / "output.rows.jsonl.tmp").exists()
    recovered.start(job["id"])
    completed = _wait(recovered, job["id"])
    assert completed["status"] == "completed"
    assert completed["output_count"] == 2_000
    recovered.close()


def test_data_studio_backup_restores_database_and_artifacts(tmp_path):
    source = tmp_path / "source.jsonl"
    source.write_text('{"email":"restore@example.com"}\n', encoding="utf-8")
    store = CampaignStore(tmp_path / "state.sqlite3")
    service = DataStudioService(store, tmp_path / "studio")
    recipe = service.create_recipe("Backup", definition={"steps": []})
    job = service.create_job(source, source.name, recipe["current_revision_id"])
    service.start(job["id"])
    job = _wait(service, job["id"])
    backup = service.backup_to(tmp_path / "backup")
    service.close()
    restored_db, restored_root = DataStudioService.restore_from(
        backup, tmp_path / "restored.sqlite3", tmp_path / "restored-studio"
    )
    restored = DataStudioService(CampaignStore(restored_db), restored_root)
    restored_job = restored.get_job(job["id"])
    artifact = next(item for item in restored_job["artifacts"] if item["kind"] == "output")
    path, _ = restored.artifact_path(job["id"], artifact["id"])
    assert path.is_file()
    assert path.read_text(encoding="utf-8").count("restore@example.com") == 1
    source.unlink()
    with restored.store.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE data_studio_jobs SET status='failed',worker_token=NULL,"
            "error_message=? WHERE id=?",
            ("retry restored bundle", job["id"]),
        )
        connection.execute("DELETE FROM data_studio_artifacts WHERE job_id=?", (job["id"],))
    restored.start(job["id"])
    retried = _wait(restored, job["id"])
    assert retried["status"] == "completed"
    restored.close()
