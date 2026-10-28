"""Local application lifecycle, data-directory, and backup boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import signal
import socket
import sqlite3
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from mailforge.infrastructure.campaign_store import SCHEMA_VERSION, CampaignStore

APP_NAME = "Mailforge"
DEFAULT_PORT = 8765
BACKUP_FORMAT = "mailforge-application-backup"
BACKUP_VERSION = 1


def application_version() -> str:
    try:
        return version("mailforge")
    except PackageNotFoundError:
        return "0.1.0"


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_data_directory() -> Path:
    """Return the stable, user-owned data root for the current platform."""

    home = Path.home()
    system = platform.system()
    if system == "Darwin":
        return home / "Library" / "Application Support" / APP_NAME
    if system == "Windows":
        return Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local")) / APP_NAME
    return Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share")) / "mailforge"


@dataclass(frozen=True)
class DataDirectory:
    """Stable paths for mutable operator data, separate from application code."""

    root: Path

    @classmethod
    def from_path(cls, path: str | Path | None = None) -> DataDirectory:
        raw = Path(path).expanduser() if path is not None else default_data_directory()
        if raw.exists() and raw.is_symlink():
            raise ValueError("Mailforge data directory must not be a symlink")
        root = raw.resolve()
        if root == Path.home().resolve():
            raise ValueError("Mailforge data directory cannot be the user home directory")
        return cls(root)

    @property
    def database(self) -> Path:
        return self.root / "mailforge.sqlite3"

    @property
    def config(self) -> Path:
        return self.root / "config" / "settings.json"

    @property
    def backups(self) -> Path:
        return self.root / "backups"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def runtime(self) -> Path:
        return self.root / "runtime"

    @property
    def artifacts(self) -> Path:
        return self.root / "artifacts"

    @property
    def identity(self) -> str:
        return hashlib.sha256(str(self.root).encode("utf-8")).hexdigest()[:16]

    def initialize(self) -> DataDirectory:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink():
            raise ValueError("Mailforge data directory must not be a symlink")
        for path in (self.root / "config", self.backups, self.logs, self.runtime, self.artifacts):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.chmod(0o700)
        if not self.config.exists():
            _write_json(
                self.config,
                {
                    "format": 1,
                    "created_at": utc_now(),
                    "updated_at": utc_now(),
                    "onboarding_complete": False,
                    "preferences": {"simulation_mode": True, "timezone": "UTC"},
                },
            )
        return self


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_settings(data: DataDirectory) -> dict[str, Any]:
    data.initialize()
    try:
        payload = json.loads(data.config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(
            "Mailforge settings are unreadable; restore a backup or remove settings.json"
        ) from error
    if not isinstance(payload, dict) or payload.get("format") != 1:
        raise RuntimeError("Mailforge settings format is unsupported")
    return payload


def complete_onboarding(
    data: DataDirectory, *, preferences: dict[str, Any] | None = None
) -> dict[str, Any]:
    settings = read_settings(data)
    settings["onboarding_complete"] = True
    settings["updated_at"] = utc_now()
    if preferences:
        allowed = {"simulation_mode", "timezone", "sample_data"}
        settings["preferences"] = {
            **settings.get("preferences", {}),
            **{key: value for key, value in preferences.items() if key in allowed},
        }
    _write_json(data.config, settings)
    return settings


def current_schema_version(database: Path) -> int:
    if not database.is_file():
        return 0
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        if row is None:
            return 0
        version = connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()[0]
    return int(version)


def backup_database_snapshot(database: Path, destination: Path) -> Path:
    """Take a SQLite snapshot before an in-place compatible migration."""

    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with sqlite3.connect(database) as source, sqlite3.connect(destination) as target:
        source.backup(target)
        target.commit()
    with sqlite3.connect(destination) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("pre-migration SQLite snapshot failed integrity validation")
    os.chmod(destination, 0o600)
    return destination


def application_status(data: DataDirectory) -> dict[str, Any]:
    settings = read_settings(data)
    schema = current_schema_version(data.database)
    integrity = None
    if data.database.is_file():
        try:
            integrity = CampaignStore(data.database).integrity_check()
        except OSError, sqlite3.Error, RuntimeError:
            integrity = False
    return {
        "application": APP_NAME,
        "version": application_version(),
        "data_directory_id": data.identity,
        "database_present": data.database.is_file(),
        "schema_version": schema,
        "supported_schema_version": SCHEMA_VERSION,
        "database_integrity": integrity,
        "onboarding_complete": bool(settings.get("onboarding_complete")),
        "preferences": settings.get("preferences", {}),
        "backup_count": len(list(data.backups.glob("*.zip"))) if data.backups.is_dir() else 0,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_archive_name(name: str) -> None:
    candidate = Path(name)
    if candidate.is_absolute() or ".." in candidate.parts or "\\" in name:
        raise ValueError("backup contains an unsafe path")


def create_backup(data: DataDirectory, destination: str | Path | None = None) -> Path:
    data.initialize()
    if not data.database.is_file():
        CampaignStore(data.database).integrity_check()
    destination_path = (
        Path(destination).expanduser().resolve()
        if destination is not None
        else data.backups / f"mailforge-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.zip"
    )
    if destination_path.exists():
        raise FileExistsError(f"backup destination already exists: {destination_path.name}")
    if destination is not None and destination_path.parent != data.backups.resolve():
        raise ValueError("application backups must be stored in the configured backup directory")
    destination_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="mailforge-backup-", dir=data.runtime) as temporary:
        database_copy = Path(temporary) / "mailforge.sqlite3"
        CampaignStore(data.database).backup_to(database_copy)
        files: list[dict[str, Any]] = []
        with zipfile.ZipFile(destination_path, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(database_copy, "database/mailforge.sqlite3")
            files.append(
                {
                    "path": "database/mailforge.sqlite3",
                    "size": database_copy.stat().st_size,
                    "sha256": _sha256(database_copy),
                }
            )
            if data.config.is_file():
                archive.write(data.config, "config/settings.json")
                files.append(
                    {
                        "path": "config/settings.json",
                        "size": data.config.stat().st_size,
                        "sha256": _sha256(data.config),
                    }
                )
            for source_root in (data.artifacts, data.root / "list-tools"):
                if not source_root.is_dir():
                    continue
                for source in sorted(source_root.rglob("*")):
                    if not source.is_file() or source.is_symlink():
                        continue
                    relative = source.relative_to(data.root).as_posix()
                    archive_name = f"data/{relative}"
                    archive.write(source, archive_name)
                    files.append(
                        {
                            "path": archive_name,
                            "size": source.stat().st_size,
                            "sha256": _sha256(source),
                        }
                    )
            manifest = {
                "format": BACKUP_FORMAT,
                "format_version": BACKUP_VERSION,
                "application_version": application_version(),
                "schema_version": current_schema_version(data.database),
                "created_at": utc_now(),
                "data_directory_id": data.identity,
                "files": files,
            }
            archive.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    os.chmod(destination_path, 0o600)
    return destination_path


def validate_backup(source: str | Path) -> dict[str, Any]:
    source_path = Path(source).expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"backup does not exist: {source_path}")
    with zipfile.ZipFile(source_path) as archive:
        names = archive.namelist()
        for name in names:
            _safe_archive_name(name)
        try:
            manifest = json.loads(archive.read("manifest.json"))
        except (KeyError, json.JSONDecodeError) as error:
            raise ValueError("backup manifest is missing or invalid") from error
        if (
            manifest.get("format") != BACKUP_FORMAT
            or manifest.get("format_version") != BACKUP_VERSION
        ):
            raise ValueError("backup format is unsupported")
        if int(manifest.get("schema_version", 0)) > SCHEMA_VERSION:
            raise ValueError("backup database schema is newer than this application")
        members = set(names)
        for entry in manifest.get("files", []):
            name = entry.get("path")
            if not isinstance(name, str) or name not in members:
                raise ValueError("backup is incomplete")
            content = archive.read(name)
            if len(content) != int(entry.get("size", -1)):
                raise ValueError(
                    "backup integrity check failed: file size does not match its manifest"
                )
            if hashlib.sha256(content).hexdigest() != entry.get("sha256"):
                raise ValueError("backup integrity check failed")
        descriptor, temporary_path = tempfile.mkstemp(prefix="mailforge-verify-")
        os.close(descriptor)
        database = Path(temporary_path)
        try:
            database.write_bytes(archive.read("database/mailforge.sqlite3"))
            with sqlite3.connect(database) as connection:
                result = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise ValueError(f"backup SQLite integrity check failed: {result}")
        finally:
            database.unlink(missing_ok=True)
    return {
        "valid": True,
        "application_version": manifest.get("application_version"),
        "schema_version": manifest.get("schema_version"),
        "created_at": manifest.get("created_at"),
        "file_count": len(manifest.get("files", [])),
        "path": str(source_path),
    }


def restore_backup(source: str | Path, destination: str | Path, *, replace: bool = False) -> Path:
    source_path = Path(source).expanduser().resolve()
    destination_data = DataDirectory.from_path(destination)
    validate_backup(source_path)
    if destination_data.runtime.joinpath("application.json").is_file():
        try:
            record = json.loads(
                destination_data.runtime.joinpath("application.json").read_text(encoding="utf-8")
            )
            if _health(int(record.get("port", DEFAULT_PORT))):
                raise RuntimeError("cannot restore over an active Mailforge data directory")
        except OSError, TypeError, ValueError, json.JSONDecodeError:
            pass
    if destination_data.root.exists() and any(destination_data.root.iterdir()):
        if not replace:
            raise FileExistsError(
                "restore destination is not empty; choose a new directory or confirm replacement"
            )
        if destination_data.root == DataDirectory.from_path().root:
            raise ValueError("active default data directory cannot be destructively replaced")
    destination_data.root.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination_data.root.parent / f".mailforge-restore-{uuid.uuid4().hex}"
    temporary.mkdir(mode=0o700)
    try:
        with zipfile.ZipFile(source_path) as archive:
            for name in archive.namelist():
                _safe_archive_name(name)
                if name == "manifest.json":
                    continue
                if name == "database/mailforge.sqlite3":
                    target = temporary / "mailforge.sqlite3"
                elif name == "config/settings.json":
                    target = temporary / "config" / "settings.json"
                elif name.startswith("data/"):
                    target = temporary / name.removeprefix("data/")
                else:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                target.write_bytes(archive.read(name))
        for directory in ("backups", "logs", "runtime"):
            (temporary / directory).mkdir(mode=0o700)
        if destination_data.root.exists() and replace:
            backup_old = destination_data.root.parent / f".mailforge-old-{uuid.uuid4().hex}"
            destination_data.root.rename(backup_old)
        temporary.rename(destination_data.root)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    CampaignStore(destination_data.database).integrity_check()
    return destination_data.root


def list_backups(data: DataDirectory) -> list[dict[str, Any]]:
    data.initialize()
    items = []
    for path in sorted(data.backups.glob("*.zip"), reverse=True):
        try:
            item = validate_backup(path)
        except (OSError, ValueError, zipfile.BadZipFile) as error:
            item = {"valid": False, "path": str(path), "error": str(error)}
        items.append(item)
    return items


def _port_is_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.15)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _health(port: int) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=0.4) as response:
            payload = json.loads(response.read())
            return payload if payload.get("application") == APP_NAME else None
    except OSError, ValueError, json.JSONDecodeError, urllib.error.URLError:
        return None


class LocalApplication:
    """Own the normal local launch path without owning domain state."""

    def __init__(
        self, data: DataDirectory, *, port: int = DEFAULT_PORT, executable: str | None = None
    ):
        self.data = data
        self.port = port
        self.executable = executable or os.environ.get("MAILFORGE_EXECUTABLE") or os.sys.executable

    @property
    def record_path(self) -> Path:
        return self.data.runtime / "application.json"

    def _record(self) -> dict[str, Any] | None:
        if not self.record_path.is_file():
            return None
        try:
            value = json.loads(self.record_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except OSError, json.JSONDecodeError:
            return None

    def _live_record(self) -> dict[str, Any] | None:
        record = self._record()
        if not record:
            return None
        health = _health(int(record.get("port", self.port)))
        if health and health.get("runtime_id") == record.get("runtime_id"):
            return {**record, **health, "running": True}
        self.record_path.unlink(missing_ok=True)
        return None

    def start(self, *, open_browser: bool = True) -> dict[str, Any]:
        self.data.initialize()
        existing = self._live_record()
        if existing:
            if open_browser:
                webbrowser.open(f"http://127.0.0.1:{existing['port']}/")
            return {**existing, "already_running": True}
        if _port_is_open(self.port):
            raise RuntimeError(
                f"port {self.port} is already in use; Mailforge did not stop or replace "
                "the other process"
            )
        log_path = self.data.logs / "application.log"
        log = log_path.open("a", encoding="utf-8")
        command = [
            self.executable,
            "-m",
            "mailforge",
            "serve",
            "--database",
            str(self.data.database),
            "--data-dir",
            str(self.data.root),
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
        ]
        process = __import__("subprocess").Popen(
            command,
            stdin=__import__("subprocess").DEVNULL,
            stdout=log,
            stderr=__import__("subprocess").STDOUT,
            start_new_session=True,
            close_fds=True,
        )
        started = time.monotonic()
        try:
            while time.monotonic() - started < 10:
                health = _health(self.port)
                if health:
                    record = {
                        "pid": process.pid,
                        "port": self.port,
                        "runtime_id": health["runtime_id"],
                        "started_at": health["started_at"],
                    }
                    _write_json(self.record_path, record)
                    if open_browser:
                        webbrowser.open(f"http://127.0.0.1:{self.port}/")
                    return {**record, **health, "already_running": False}
                if process.poll() is not None:
                    break
                time.sleep(0.1)
        finally:
            log.close()
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
        detail = "inspect logs/application.log for startup diagnostics"
        raise RuntimeError(f"Mailforge did not become healthy; {detail}")

    def stop(self) -> dict[str, Any]:
        self.data.initialize()
        record = self._live_record()
        if not record:
            return {"running": False, "stopped": False, "message": "Mailforge is not running"}
        pid = int(record["pid"])
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if _health(int(record["port"])) is None:
                self.record_path.unlink(missing_ok=True)
                return {"running": False, "stopped": True, "pid": pid}
            time.sleep(0.1)
        return {
            "running": True,
            "stopped": False,
            "pid": pid,
            "message": "shutdown is still in progress",
        }

    def status(self) -> dict[str, Any]:
        self.data.initialize()
        live = self._live_record()
        return {**application_status(self.data), "running": bool(live), "runtime": live}

    def open(self) -> dict[str, Any]:
        status = self._live_record()
        if not status:
            status = self.start(open_browser=False)
        webbrowser.open(f"http://127.0.0.1:{status['port']}/")
        return status
