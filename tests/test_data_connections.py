from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest
from openpyxl import Workbook

from mailforge.application.data_connections import (
    DataConnectionError,
    DataConnectionsService,
    _Adapter,
)
from mailforge.application.data_studio import DataStudioService
from mailforge.infrastructure.campaign_store import CampaignStore
from mailforge.infrastructure.credentials import MappingCredentialResolver


def _sqlite_source(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE contacts (
                id INTEGER PRIMARY KEY,
                email TEXT NOT NULL,
                score INTEGER,
                note TEXT
            );
            INSERT INTO contacts VALUES
                (2, 'second@example.com', 2, 'qualified'),
                (1, 'first@example.com', 1, NULL),
                (3, 'third@example.com', 3, 'qualified');
            """
        )


def test_sqlite_profile_preview_snapshot_and_idempotency(tmp_path: Path) -> None:
    source = tmp_path / "contacts.sqlite3"
    _sqlite_source(source)
    store = CampaignStore(tmp_path / "state.sqlite3")
    service = DataConnectionsService(store, tmp_path / "artifacts" / "data-connections")
    try:
        profile = service.create_connection("Fixture contacts", "sqlite", source_path=source)
        assert "source_path" not in json.dumps(profile)
        assert profile["config"]["source_name"] == source.name

        schema = service.schema(profile["id"])
        assert schema["objects"][0]["name"] == "contacts"
        assert {field["name"] for field in schema["objects"][0]["fields"]} >= {
            "email",
            "score",
        }
        score_field = next(
            field for field in schema["objects"][0]["fields"] if field["name"] == "score"
        )
        assert score_field["normalized_type"] == "integer"

        preview = service.preview(
            profile["id"],
            {
                "table": "contacts",
                "columns": ["email", "score"],
                "filters": [{"field": "score", "op": "contains", "value": "2"}],
            },
            limit=5000,
        )
        assert preview["limit"] == 500
        assert [row["email"] for row in preview["rows"]] == ["second@example.com"]
        assert preview["rows"][0]["__provenance"]["source_type"] == "sqlite"

        snapshot = service.start_snapshot(
            profile["id"],
            {"table": "contacts", "columns": ["id", "email", "score"]},
            idempotency_key="fixture-refresh-1",
            wait=True,
        )
        assert snapshot["status"] == "completed"
        assert snapshot["record_count"] == 3
        assert snapshot["artifact_available"] is True
        assert snapshot["artifact_sha256"]
        assert snapshot["consistency"]["bounded"] is True
        artifact = next((tmp_path / "artifacts" / "data-connections" / "snapshots").glob("*.jsonl"))
        assert artifact.stat().st_mode & 0o077 == 0
        records = [json.loads(line) for line in artifact.read_text().splitlines()]
        assert [record["id"] for record in records] == [1, 2, 3]

        same = service.start_snapshot(
            profile["id"], {"table": "contacts"}, idempotency_key="fixture-refresh-1", wait=True
        )
        assert same["id"] == snapshot["id"]
        second = service.create_connection("Fixture contacts copy", "sqlite", source_path=source)
        other = service.start_snapshot(
            second["id"], {"table": "contacts"}, idempotency_key="fixture-refresh-1", wait=True
        )
        assert other["id"] != snapshot["id"]
        assert len(service.events(profile["id"])) >= 4
    finally:
        service.close()


def test_xlsx_formula_policy_and_duplicate_headers(tmp_path: Path) -> None:
    source = tmp_path / "workbook.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Leads"
    sheet.append(["email", "email", "score", "formula"])
    sheet.append(["one@example.com", "alias@example.com", 4, "=1+1"])
    workbook.save(source)

    store = CampaignStore(tmp_path / "state.sqlite3")
    service = DataConnectionsService(store, tmp_path / "artifacts" / "data-connections")
    try:
        profile = service.create_connection("Workbook leads", "xlsx", source_path=source)
        schema = service.schema(profile["id"])
        fields = [field["name"] for field in schema["worksheets"][0]["fields"]]
        assert fields == ["email", "email__2", "score", "formula"]
        preview = service.preview(profile["id"], {"worksheet": "Leads", "columns": fields}, limit=1)
        row = preview["rows"][0]
        assert row["email"] == "one@example.com"
        assert row["email__2"] == "alias@example.com"
        assert row["formula"] is None
        assert row["__formula_fields"] == ["formula"]
        assert row["__conversion_errors"][0]["field"] == "formula"

        with pytest.raises(DataConnectionError, match="formula") as error:
            service.preview(
                profile["id"],
                {"worksheet": "Leads", "columns": fields, "formula_policy": "reject"},
            )
        assert error.value.code == "formula_unsupported"
    finally:
        service.close()


def test_postgres_profile_uses_symbolic_credential_and_redacts_connection_failure(
    tmp_path: Path,
) -> None:
    store = CampaignStore(tmp_path / "state.sqlite3")
    service = DataConnectionsService(
        store,
        tmp_path / "artifacts" / "data-connections",
        credential_resolver=MappingCredentialResolver({"test:postgres": "not-a-real-secret"}),
    )
    try:
        profile = service.create_connection(
            "Unavailable PostgreSQL",
            "postgresql",
            {
                "host": "127.0.0.1",
                "port": 65432,
                "database": "mailforge",
                "user": "mailforge",
            },
            credential_ref="test:postgres",
        )
        assert profile["credential_ref"] == "test:postgres"
        with pytest.raises(DataConnectionError) as error:
            service.test_connection(profile["id"])
        assert error.value.code == "source_unavailable"
        assert "not-a-real-secret" not in str(error.value)
        assert service.list_connections()["items"][0]["credential_ref"] == "test:postgres"
        with pytest.raises(DataConnectionError, match="unsupported"):
            service.create_connection(
                "DSN with secret",
                "postgresql",
                {
                    "host": "127.0.0.1",
                    "database": "mailforge",
                    "user": "mailforge",
                    "dsn": "postgresql://mailforge:plaintext-secret@host/db",
                },
                credential_ref="test:postgres",
            )
    finally:
        service.close()


def test_file_profile_rejects_arbitrary_configuration(tmp_path: Path) -> None:
    source = tmp_path / "contacts.sqlite3"
    _sqlite_source(source)
    service = DataConnectionsService(
        CampaignStore(tmp_path / "state.sqlite3"), tmp_path / "artifacts" / "data-connections"
    )
    try:
        with pytest.raises(DataConnectionError, match="unsupported"):
            service.create_connection(
                "Unsafe file profile",
                "sqlite",
                {"dsn": "sqlite:///tmp/secret-password"},
                source_path=source,
            )
    finally:
        service.close()


def test_data_studio_handoff_requires_completed_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "contacts.sqlite3"
    _sqlite_source(source)
    store = CampaignStore(tmp_path / "state.sqlite3")
    studio = DataStudioService(store, tmp_path / "artifacts" / "data-studio")
    service = DataConnectionsService(
        store, tmp_path / "artifacts" / "data-connections", data_studio=studio
    )
    try:
        profile = service.create_connection("Contacts", "sqlite", source_path=source)
        snapshot = service.start_snapshot(
            profile["id"], {"table": "contacts"}, idempotency_key="handoff", wait=True
        )
        artifact = next((tmp_path / "artifacts" / "data-connections" / "snapshots").glob("*.jsonl"))
        original = artifact.read_bytes()
        artifact.write_bytes(original + b"tampered\n")
        assert service.get_snapshot(snapshot["id"])["artifact_available"] is False
        with pytest.raises(DataConnectionError) as integrity_error:
            service.handoff_to_data_studio(snapshot["id"], "missing-revision")
        assert integrity_error.value.code == "snapshot_integrity"
        artifact.write_bytes(original)
        recipe = studio.create_recipe("Import contacts", definition={"steps": []})
        job = service.handoff_to_data_studio(snapshot["id"], recipe["current_revision_id"])
        assert job["status"] == "pending"
        assert service.events(profile["id"])[0]["event_type"] == "data_studio_handoff"
    finally:
        service.close()


class _SlowAdapter(_Adapter):
    def schema(self) -> dict[str, object]:
        return {
            "source_type": "synthetic",
            "objects": [{"name": "rows", "fields": [], "kind": "table"}],
        }

    def iter_records(self, selection, limit):
        for index in range(1000):
            time.sleep(0.001)
            yield {"value": index, "__source_row": index + 1}


def test_cancelled_snapshot_has_no_published_artifact(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "contacts.sqlite3"
    _sqlite_source(source)
    store = CampaignStore(tmp_path / "state.sqlite3")
    service = DataConnectionsService(store, tmp_path / "artifacts" / "data-connections")
    try:
        profile = service.create_connection("Contacts", "sqlite", source_path=source)
        monkeypatch.setattr(service, "_adapter", lambda _profile: _SlowAdapter())
        snapshot = service.start_snapshot(profile["id"], {}, idempotency_key="cancel-me")
        service.cancel_snapshot(snapshot["id"])
        for _ in range(50):
            current = service.get_snapshot(snapshot["id"])
            if current["status"] in {"canceled", "failed", "completed"}:
                break
            time.sleep(0.01)
        assert current["status"] == "canceled"
        assert current["artifact_available"] is False
    finally:
        service.close()
