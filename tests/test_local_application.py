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
    list_backups,
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
    assert metadata["schema_version"] == 24
    restored = restore_backup(backup, tmp_path / "restored-data")
    assert (restored / "artifacts" / "synthetic.txt").read_text(
        encoding="utf-8"
    ) == "synthetic artifact\n"
    assert CampaignStore(restored / "mailforge.sqlite3").integrity_check()

    replacement = DataDirectory.from_path(tmp_path / "replacement-data").initialize()
    CampaignStore(replacement.database)
    restore_backup(backup, replacement.root, replace=True)
    assert not list(replacement.root.parent.glob(".mailforge-old-*"))


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

    malformed = data.backups / "malformed.zip"
    with zipfile.ZipFile(malformed, "w") as archive:
        archive.writestr("manifest.json", "[]")
    assert list_backups(data)[0]["valid"] is False


def test_interrupted_backup_does_not_publish_partial_archive(tmp_path: Path, monkeypatch) -> None:
    data = DataDirectory.from_path(tmp_path / "operator-data").initialize()
    CampaignStore(data.database)
    import mailforge.application.local_application as local_application

    original_write = local_application.zipfile.ZipFile.writestr

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(local_application.zipfile.ZipFile, "writestr", interrupt)
    destination = data.backups / "interrupted.zip"
    with pytest.raises(KeyboardInterrupt):
        create_backup(data, destination)
    assert not destination.exists()
    assert not list(data.backups.glob(".mailforge-backup-publish-*.tmp"))
    monkeypatch.setattr(local_application.zipfile.ZipFile, "writestr", original_write)


def test_newer_schema_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "newer.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO schema_migrations VALUES (25, 'now')")
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


def test_restore_cleanup_failure_keeps_valid_new_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = DataDirectory.from_path(tmp_path / "operator-data").initialize()
    CampaignStore(data.database)
    backup = create_backup(data)
    destination = DataDirectory.from_path(tmp_path / "replacement-data").initialize()
    CampaignStore(destination.database)

    import mailforge.application.local_application as local_application

    original_rmtree = local_application.shutil.rmtree

    def fail_old_root(path: Path, *args: object, **kwargs: object) -> None:
        if path.name.startswith(".mailforge-old-"):
            raise OSError("simulated cleanup failure")
        original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(local_application.shutil, "rmtree", fail_old_root)
    with pytest.raises(RuntimeError, match="rollback copy was retained"):
        restore_backup(backup, destination.root, replace=True)
    assert CampaignStore(destination.database).integrity_check()
    assert list(destination.root.parent.glob(".mailforge-old-*"))
