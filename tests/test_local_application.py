from __future__ import annotations

import sqlite3
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
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
from mailforge.interfaces.cli import build_parser
from mailforge.infrastructure.campaign_store import CampaignStore


def test_app_lifecycle_defaults_to_platform_data_directory() -> None:
    args = build_parser().parse_args(["app", "open"])
    assert args.data_dir is None
    assert args.port == 8765


def test_mac_browser_open_uses_launchservices(monkeypatch) -> None:
    import mailforge.application.local_application as local_application

    calls: list[list[str]] = []
    monkeypatch.setattr(local_application.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        local_application.subprocess,
        "Popen",
        lambda command, **_kwargs: calls.append(command),
    )
    local_application._open_browser("http://127.0.0.1:8765/")
    assert calls == [["/usr/bin/open", "http://127.0.0.1:8765/"]]


def test_start_recovers_a_live_instance_when_runtime_record_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mailforge.application.local_application as local_application

    data = DataDirectory.from_path(tmp_path / "operator-data").initialize()
    app = local_application.LocalApplication(data)
    health = {"application": "Mailforge", "runtime_id": "live", "started_at": "now"}
    monkeypatch.setattr(local_application, "_health", lambda _port: health)
    monkeypatch.setattr(local_application, "_listening_pid", lambda _port: 4321)
    monkeypatch.setattr(local_application, "_process_start_time", lambda _pid: "start")
    monkeypatch.setattr(local_application, "_open_browser", lambda _url: None)
    recovered = app.start()
    assert recovered["already_running"] is True
    assert recovered["recovered_record"] is True
    assert recovered["pid"] == 4321
    assert app.record_path.is_file()


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
    assert metadata["schema_version"] == 28
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


def test_concurrent_backup_publication_keeps_one_destination(tmp_path: Path) -> None:
    data = DataDirectory.from_path(tmp_path / "operator-data").initialize()
    CampaignStore(data.database)
    destination = data.backups / "concurrent.zip"

    def make_backup() -> str:
        try:
            return str(create_backup(data, destination))
        except FileExistsError:
            return "exists"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: make_backup(), range(2)))
    assert results.count("exists") == 1
    assert str(destination) in results
    assert validate_backup(destination)["valid"] is True


def test_newer_schema_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "newer.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO schema_migrations VALUES (29, 'now')")
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
