import json
import time

import pytest

from mailforge.application.data_studio import DataStudioError, DataStudioService, detect_schema
from mailforge.infrastructure.campaign_store import CampaignStore


def test_data_studio_reads_structured_formats_and_reports_schema(tmp_path):
    csv_path = tmp_path / "contacts.csv"
    csv_path.write_text("email,name\nAlice@example.com,Alice\n", encoding="utf-8")
    jsonl_path = tmp_path / "contacts.jsonl"
    jsonl_path.write_text('{"email":"bob@example.com"}\n', encoding="utf-8")
    assert detect_schema(csv_path)["fields"] == ["email", "name"]
    assert detect_schema(jsonl_path)["fields"] == ["email"]


def test_data_studio_typed_preview_is_deterministic_and_non_destructive(tmp_path):
    source = tmp_path / "contacts.csv"
    original = (
        "email,first,last\n"
        " Alice@Example.com ,Alice,One\n"
        "BOB@gmail.com,Bob,Two\n"
        "BOB@gmail.com,Bob,Two\n"
    )
    source.write_text(original, encoding="utf-8")
    service = DataStudioService(CampaignStore(tmp_path / "state.sqlite3"), tmp_path / "studio")
    definition = {
        "steps": [
            {"type": "normalize", "fields": ["email"], "operation": "email"},
            {
                "type": "computed",
                "field": "full_name",
                "expression": {"kind": "concat", "fields": ["first", "last"], "separator": " "},
            },
            {"type": "dedupe", "fields": ["email"]},
        ]
    }
    first = service.preview(source, definition)
    second = service.preview(source, definition)
    assert first["rows"] == second["rows"]
    assert first["counts"] == {"input": 3, "output": 2, "errors": 0, "removed": 1}
    assert source.read_text(encoding="utf-8") == original
    service.close()


def test_data_studio_recipes_pin_revisions_and_jobs_publish_atomic_artifacts(tmp_path):
    source = tmp_path / "data.json"
    source.write_text(
        json.dumps([{"email": "a@example.com"}, {"email": "b@example.com"}]), encoding="utf-8"
    )
    service = DataStudioService(CampaignStore(tmp_path / "state.sqlite3"), tmp_path / "studio")
    recipe = service.create_recipe(
        "Provider recipe", definition={"steps": [{"type": "provider_classify", "field": "email"}]}
    )
    revised = service.save_revision(
        recipe["id"],
        {"steps": [{"type": "dedupe", "fields": ["email"]}]},
        expected_revision_id=recipe["current_revision_id"],
    )
    with pytest.raises(DataStudioError, match="changed"):
        service.save_revision(
            recipe["id"], {"steps": []}, expected_revision_id=recipe["current_revision_id"]
        )
    job = service.create_job(source, "data.json", revised["current_revision_id"])
    service.start(job["id"])
    for _ in range(200):
        job = service.get_job(job["id"])
        if job["status"] not in {"pending", "running"}:
            break
        time.sleep(0.01)
    assert job["status"] == "completed"
    assert job["output_count"] == 2
    assert {artifact["kind"] for artifact in job["artifacts"]} == {"output", "errors"}
    service.close()


def test_data_studio_rejects_unbounded_or_executable_recipe_steps(tmp_path):
    service = DataStudioService(CampaignStore(tmp_path / "state.sqlite3"), tmp_path / "studio")
    with pytest.raises(DataStudioError, match="unsupported step"):
        service.validate_definition({"steps": [{"type": "python", "source": "__import__('os')"}]})
    with pytest.raises(DataStudioError, match="100 steps"):
        service.validate_definition({"steps": [{"type": "drop", "fields": []}] * 101})
    service.close()
