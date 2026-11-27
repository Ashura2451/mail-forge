import json

import pytest

from mailforge.application.ingest import (
    InputLimitError,
    collect_unique_addresses,
    iter_structured_records,
)


@pytest.mark.parametrize(
    ("suffix", "content"),
    [
        (".txt", "Alice@Example.com\ninvalid\nalice@example.com\nbob@example.com"),
        (".csv", "name,email\nAlice,Alice@Example.com\nBob,bob@example.com"),
        (".json", json.dumps(["Alice@Example.com", {"email": "bob@example.com"}])),
        (".jsonl", '{"email": "Alice@Example.com"}\nnot-json bob@example.com\n'),
    ],
)
def test_supported_inputs_are_normalized_and_deduplicated(tmp_path, suffix, content):
    path = tmp_path / f"input{suffix}"
    path.write_text(content, encoding="utf-8")
    assert collect_unique_addresses(path) == ["alice@example.com", "bob@example.com"]


def test_oversized_input_is_rejected_before_processing(tmp_path):
    path = tmp_path / "input.txt"
    path.write_text("a@example.com", encoding="utf-8")
    with pytest.raises(InputLimitError):
        collect_unique_addresses(path, max_bytes=1)


def test_csv_structured_records_preserve_row_fields_without_header_candidates(tmp_path):
    path = tmp_path / "input.csv"
    path.write_text("email,name\nalice@example.com,Alice\n", encoding="utf-8")

    assert list(iter_structured_records(path)) == [
        (2, "alice@example.com", {"fields": {"email": "alice@example.com", "name": "Alice"}})
    ]
