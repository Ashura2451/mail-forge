"""Unified transformations for modern and legacy sorting workflows."""

from __future__ import annotations

import ast
from pathlib import Path


def format_email_file(input_path: Path, output_path: Path) -> None:
    """Preserve SORTER's newline-collapse formatting contract."""

    output_path.write_text(
        input_path.read_text(encoding="utf-8").replace("\n\n", "\n"), encoding="utf-8"
    )


def remove_duplicate_lines(path: Path) -> int:
    """Keep the first occurrence of each stripped line and return removals."""

    lines = path.read_text(encoding="utf-8").splitlines()
    unique = list(dict.fromkeys(line.strip() for line in lines))
    path.write_text("\n".join(unique) + ("\n" if unique else ""), encoding="utf-8")
    return len(lines) - len(unique)


def write_legacy_list(input_path: Path, output_path: Path) -> None:
    """Write SORTER's historical Python-list representation."""

    values = input_path.read_text(encoding="utf-8").splitlines()
    output_path.write_text(str([value.strip() for value in values]), encoding="utf-8")


def load_legacy_list(path: Path, *, max_bytes: int = 1_000_000) -> list[str]:
    """Safely parse the historical list representation without executing code."""

    if path.stat().st_size > max_bytes:
        raise ValueError(f"legacy list exceeds {max_bytes} bytes")
    value = ast.literal_eval(path.read_text(encoding="utf-8"))
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("legacy list must contain only strings")
    return value


def count_legacy_list(path: Path, *, max_bytes: int = 1_000_000) -> int:
    return len(load_legacy_list(path, max_bytes=max_bytes))
