"""Atomic local checkpoint persistence; not the authoritative campaign store."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_checkpoint(path: Path, state: dict[str, Any]) -> None:
    """Write a JSON checkpoint atomically and durably in the same directory."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def read_checkpoint(path: Path) -> dict[str, Any] | None:
    """Read a checkpoint, returning ``None`` for a missing or invalid file."""

    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError, json.JSONDecodeError, OSError:
        return None
    return value if isinstance(value, dict) else None
