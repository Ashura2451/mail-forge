"""A small local-process lock for the single-owner Mailforge runtime."""

from __future__ import annotations

import fcntl
import hashlib
import os
from pathlib import Path


class ProcessLockError(RuntimeError):
    """Raised when another local Mailforge process owns the database runtime."""


class DatabaseProcessLock:
    """Hold an advisory lock for the lifetime of one local web process."""

    def __init__(self, database: str | Path):
        self.database = Path(database).expanduser().resolve()
        try:
            identity = os.stat(self.database)
        except FileNotFoundError:
            lock_name = f".{self.database.name}.mailforge.lock"
        else:
            inode_key = f"{identity.st_dev}:{identity.st_ino}".encode("ascii")
            lock_name = f".mailforge-{hashlib.sha256(inode_key).hexdigest()[:24]}.lock"
        self.path = self.database.parent / lock_name
        self._handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self._handle.close()
            self._handle = None
            raise ProcessLockError(
                f"another Mailforge process already owns database {self.database}"
            ) from error

    def release(self) -> None:
        if self._handle is None:
            return
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None

    def __enter__(self) -> DatabaseProcessLock:
        self.acquire()
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.release()
