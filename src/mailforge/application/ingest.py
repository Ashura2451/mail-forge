"""Bounded, deterministic input ingestion for TXT, CSV, JSON, and JSONL."""

from __future__ import annotations

import csv
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import ijson

from mailforge.domain.email import extract_addresses, normalize_email, validate_email


class InputLimitError(ValueError):
    """Raised when an input exceeds explicit safety bounds."""


def _walk_values(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _walk_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_values(child)


def iter_raw_values(path: Path, *, max_bytes: int = 16 * 1024 * 1024) -> Iterator[str]:
    """Yield text values from supported formats without unbounded input reads."""

    size = path.stat().st_size
    if size > max_bytes:
        raise InputLimitError(f"input is {size} bytes; maximum is {max_bytes}")
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
            for row in csv.reader(handle):
                yield from row
        return
    if suffix in {".json", ".jsonl"}:
        if suffix == ".jsonl":
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for _line_number, line in enumerate(handle, 1):
                    if not line.strip():
                        continue
                    try:
                        yield from _walk_values(json.loads(line))
                    except json.JSONDecodeError:
                        yield line
            return
        with path.open("r", encoding="utf-8", errors="replace") as probe:
            first = next((char for char in probe.read(512) if not char.isspace()), "")
        if first == "{":
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        yield from _walk_values(json.loads(line))
                    except json.JSONDecodeError:
                        yield line
            return
        with path.open("rb") as handle:
            for _prefix, event, value in ijson.parse(handle):
                if event == "string":
                    yield value
        return
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        yield from handle


def iter_normalized_addresses(path: Path, **kwargs: Any) -> Iterator[str]:
    """Yield valid normalized addresses in source order."""

    for raw in iter_raw_values(path, **kwargs):
        for candidate in extract_addresses(raw):
            normalized = normalize_email(candidate)
            if validate_email(normalized).valid:
                yield normalized


def collect_unique_addresses(path: Path, **kwargs: Any) -> list[str]:
    """Collect first-seen unique addresses deterministically."""

    seen: set[str] = set()
    result: list[str] = []
    for address in iter_normalized_addresses(path, **kwargs):
        if address not in seen:
            seen.add(address)
            result.append(address)
    return result


def iter_jsonl_email_field(
    path: Path,
    *,
    field: str = "e",
    max_bytes: int = 16 * 1024 * 1024,
    dedupe: bool = True,
    normalize_lower: bool = True,
) -> Iterator[str]:
    """Extract one email field from JSONL while preserving first-seen order."""

    if path.stat().st_size > max_bytes:
        raise InputLimitError(f"input is {path.stat().st_size} bytes; maximum is {max_bytes}")
    seen: set[str] = set()
    for line in path.open("r", encoding="utf-8", errors="replace"):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        value = item.get(field) if isinstance(item, dict) else None
        if not isinstance(value, str):
            continue
        candidate = value.strip()
        if normalize_lower:
            candidate = candidate.lower()
        if not validate_email(candidate).valid or (dedupe and candidate in seen):
            continue
        seen.add(candidate)
        yield candidate
