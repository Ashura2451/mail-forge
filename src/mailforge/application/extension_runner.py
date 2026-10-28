"""Minimal subprocess entrypoint for explicitly trusted local extensions."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

MAX_OUTPUT_BYTES = 2 * 1024 * 1024


class _BoundedWriter:
    def __init__(self, stream):
        self.stream = stream
        self.written = 0

    def write(self, value: str) -> int:
        encoded = value.encode("utf-8")
        if self.written + len(encoded) > MAX_OUTPUT_BYTES:
            raise ValueError("extension output exceeds the local limit")
        self.written += len(encoded)
        return self.stream.write(value)

    def flush(self) -> None:
        self.stream.flush()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--entrypoint", default="run")
    args = parser.parse_args()
    source = Path(args.source)
    if not source.is_file() or source.is_symlink():
        return 2
    payload = json.load(sys.stdin)
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
        return 3
    specification = importlib.util.spec_from_file_location("mailforge_trusted_extension", source)
    if specification is None or specification.loader is None:
        return 4
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    function = getattr(module, args.entrypoint, None)
    if not callable(function):
        return 5
    result = function(payload["records"], payload.get("parameters", {}))
    if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
        return 6
    json.dump(result, _BoundedWriter(sys.stdout), ensure_ascii=False, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
