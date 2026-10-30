"""A small local-process lock for the single-owner Mailforge runtime."""

from __future__ import annotations

import fcntl
import os
import platform
from pathlib import Path


class ProcessLockError(RuntimeError):
    """Raised when another local Mailforge process owns the database runtime."""


def default_runtime_lock_path() -> Path:
    """Return the stable, user-scoped coordination marker for Mailforge."""

    home = Path.home()
    system = platform.system()
    if system == "Darwin":
        root = home / "Library" / "Application Support" / "Mailforge"
    elif system == "Windows":
        root = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local")) / "Mailforge"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share")) / "mailforge"
    return root / ".mailforge-runtime.lock"


def _reject_symlink(path: Path) -> None:
    """Reject a replaceable path component before opening the lock authority."""

    current = path
    while current != current.parent:
        if current.exists() and current.is_symlink():
            raise ProcessLockError(f"Mailforge lock path must not contain a symlink: {path}")
        current = current.parent


class DatabaseProcessLock:
    """Hold the single local Mailforge runtime ownership lock."""

    def __init__(self, database: str | Path, *, lock_path: str | Path | None = None):
        self.database = Path(database).expanduser().resolve()
        raw_lock_path = Path(lock_path).expanduser() if lock_path else default_runtime_lock_path()
        self.path = raw_lock_path.absolute()
        _reject_symlink(self.path)
        self._directory_fd: int | None = None
        self._handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        directory_flags = os.O_RDONLY
        try:
            self._directory_fd = os.open(self.path.parent, directory_flags)
            fcntl.flock(self._directory_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            if self._directory_fd is not None:
                os.close(self._directory_fd)
                self._directory_fd = None
            raise ProcessLockError(
                f"another Mailforge process already owns database {self.database}"
            ) from error
        except OSError as error:
            if self._directory_fd is not None:
                os.close(self._directory_fd)
                self._directory_fd = None
            raise ProcessLockError("Mailforge runtime lock authority is unavailable") from error

        try:
            flags = os.O_RDWR | os.O_CREAT
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(self.path, flags, 0o600)
            os.fchmod(fd, 0o600)
            self._handle = os.fdopen(fd, "a+", encoding="utf-8")
        except OSError as error:
            fcntl.flock(self._directory_fd, fcntl.LOCK_UN)
            os.close(self._directory_fd)
            self._directory_fd = None
            raise ProcessLockError(
                "Mailforge runtime lock marker is unsafe or unavailable"
            ) from error

    def release(self) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None
        if self._directory_fd is not None:
            fcntl.flock(self._directory_fd, fcntl.LOCK_UN)
            os.close(self._directory_fd)
            self._directory_fd = None

    def __enter__(self) -> DatabaseProcessLock:
        self.acquire()
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.release()
