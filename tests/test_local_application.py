from __future__ import annotations

import sqlite3
import tempfile
import zipfile
from pathlib import Path

import pytest

from mailforge.application.local_application import (
    DataDirectory,
    application_status,
    complete_onboarding,
    create_backup,
    restore_backup,
    validate_backup,
)
from mailforge.infrastructure.campaign_store import CampaignStore


def test_local_data_directory_onboarding_and_full_backup_restore(tmp_path: Path) -> None:
    data = DataDirectory.from_path(tmp_path / "operator-data").initialize()
    CampaignStore(data.database)
    (data.artifacts / "synthetic.txt").write_text("synthetic artifact\n", encoding="utf-8")

    assert application_status(data)["onboarding_complete"] is False
    complete_onboarding(data, preferences={"timezone": "America/New_York", "sample_data": True})
    assert application_status(data)["onboarding_complete"] is True
    assert application_status(data)["preferences"]["timezone"] == "America/New_York"

    backup = create_backup(data)
    metadata = validate_backup(backup)
    assert metadata["valid"] is True
    assert metadata["schema_version"] == 17
    restored = restore_backup(backup, tmp_path / "restored-data")
    assert (restored / "artifacts" / "synthetic.txt").read_text(
        encoding="utf-8"
    ) == "synthetic artifact\n"
    assert CampaignStore(restored / "mailforge.sqlite3").integrity_check()


def test_tampered_backup_is_refused(tmp_path: Path) -> None:
    data = DataDirectory.from_path(tmp_path / "operator-data").initialize()
    CampaignStore(data.database)
    backup = create_backup(data)
    with tempfile.NamedTemporaryFile(prefix="mailforge-tampered-", suffix=".zip") as handle:
        tampered = Path(handle.name)
        with zipfile.ZipFile(backup) as source, zipfile.ZipFile(tampered, "w") as target:
            for item in source.infolist():
                target.writestr(
                    item,
                    b"tampered"
                    if item.filename == "database/mailforge.sqlite3"
                    else source.read(item),
                )
        with pytest.raises(ValueError, match="integrity"):
            validate_backup(tampered)


def test_newer_schema_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "newer.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO schema_migrations VALUES (18, 'now')")
    with pytest.raises(RuntimeError, match="newer"):
        from mailforge.interfaces.web import WebRuntime

        WebRuntime(database, data_directory=tmp_path / "data")


def test_restore_refuses_an_active_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = DataDirectory.from_path(tmp_path / "operator-data").initialize()
    CampaignStore(data.database)
    backup = create_backup(data)
    destination = DataDirectory.from_path(tmp_path / "active-data").initialize()
    destination.runtime.joinpath("application.json").write_text(
        '{"port": 8765, "runtime_id": "live"}', encoding="utf-8"
    )
    import mailforge.application.local_application as local_application

    monkeypatch.setattr(local_application, "_health", lambda _port: {"application": "Mailforge"})
    with pytest.raises(RuntimeError, match="active"):
        restore_backup(backup, destination.root, replace=True)
