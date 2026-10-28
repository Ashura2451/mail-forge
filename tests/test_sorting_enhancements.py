import json

import pytest

from mailforge.application.sorting import EmailSorter, SortConfig, _csv_safe
from mailforge.domain.results import ReasonCode
from mailforge.interfaces.cli import build_parser


def make_config(tmp_path, **overrides):
    values = {
        "results_dir": tmp_path / "results",
        "config_dir": tmp_path / "config",
        "log_dir": tmp_path / "logs",
        "checkpoint_path": tmp_path / "checkpoint.json",
        "checkpoint_interval": 1,
        "min_disk_space_gb": 0,
    }
    values.update(overrides)
    return SortConfig(**values)


def test_multi_source_structured_results_and_quarantine_reconcile(tmp_path):
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("one@example.com\nbad..local@example.com\n", encoding="utf-8")
    second.write_text("one@example.com\ntwo@example.com\n", encoding="utf-8")

    result = EmailSorter(make_config(tmp_path)).sort_file([first, second], resume=False)

    assert result.committed
    assert result.structured_results_path is not None
    records = [
        json.loads(line)
        for line in result.structured_results_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [item["processing_disposition"] for item in records] == [
        "accepted",
        "rejected",
        "duplicate",
        "accepted",
    ]
    assert records[0]["source_file"] == str(first)
    assert records[-1]["source_file"] == str(second)
    assert ReasonCode.INVALID_SYNTAX.value in records[1]["diagnostic_reason_codes"]
    assert records[2]["deduplication_disposition"] == "duplicate"
    assert result.quarantine_path is not None
    assert len(result.quarantine_path.read_text(encoding="utf-8").splitlines()) == 1

    summary = json.loads((tmp_path / "results" / "summary.json").read_text(encoding="utf-8"))
    assert summary["counts"]["total_extracted"] == 4
    assert summary["counts"]["unique_valid"] == 2
    assert summary["counts"]["duplicates_removed"] == 1
    assert summary["counts"]["quarantined"] == 1


def test_dry_run_profile_writes_explainable_preview_without_legacy_outputs(tmp_path):
    source = tmp_path / "input.txt"
    source.write_text("one@example.com\ntwo@example.com\n", encoding="utf-8")

    result = EmailSorter(make_config(tmp_path, profile="report")).sort_file(
        source, resume=False, dry_run=True
    )

    assert result.committed
    assert result.structured_results_path is not None
    assert result.structured_results_path.exists()
    assert not (tmp_path / "results" / "master.csv").exists()
    assert not (tmp_path / "results" / "custom_domains.txt").exists()


def test_changed_input_is_not_resumed_silently(tmp_path):
    source = tmp_path / "input.txt"
    source.write_text("one@example.com\ntwo@example.com\n", encoding="utf-8")
    config = make_config(tmp_path)
    sorter = EmailSorter(config)

    original = sorter._flush_outputs

    def interrupt_after_first(handles, master):
        original(handles, master)
        raise KeyboardInterrupt

    sorter._flush_outputs = interrupt_after_first
    with pytest.raises(KeyboardInterrupt):
        sorter.sort_file(source, resume=False)
    source.write_text("changed@example.com\ntwo@example.com\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="does not match"):
        EmailSorter(config).sort_file(source, resume=True)


def test_configuration_change_is_not_resumed_silently(tmp_path):
    source = tmp_path / "input.txt"
    source.write_text("one@example.com\ntwo@example.com\n", encoding="utf-8")
    config = make_config(tmp_path)
    sorter = EmailSorter(config)
    original = sorter._flush_outputs

    def interrupt_after_first(handles, master):
        original(handles, master)
        raise KeyboardInterrupt

    sorter._flush_outputs = interrupt_after_first
    with pytest.raises(KeyboardInterrupt):
        sorter.sort_file(source, resume=False)
    config.profile = "report"

    with pytest.raises(RuntimeError, match="does not match"):
        EmailSorter(config).sort_file(source, resume=True)


def test_structured_resume_restores_dedupe_and_output_boundary(tmp_path):
    source = tmp_path / "input.txt"
    source.write_text("one@example.com\none@example.com\ntwo@example.com\n", encoding="utf-8")
    config = make_config(tmp_path, profile="report")
    sorter = EmailSorter(config)
    original = sorter._flush_outputs
    calls = 0

    def fail_after_first_checkpoint(handles, master):
        nonlocal calls
        original(handles, master)
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic structured flush failure")

    sorter._flush_outputs = fail_after_first_checkpoint
    with pytest.raises(RuntimeError):
        sorter.sort_file(source, resume=False)

    checkpoint = json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["output_offsets"]["processing.jsonl"] > 0

    result = EmailSorter(config).sort_file(source, resume=True)
    records = [
        json.loads(line)
        for line in result.structured_results_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [record["processing_disposition"] for record in records] == [
        "accepted",
        "duplicate",
        "accepted",
    ]


def test_single_source_provenance_uses_record_position(tmp_path):
    source = tmp_path / "input.txt"
    source.write_text("one@example.com and two@example.com\n", encoding="utf-8")

    result = EmailSorter(make_config(tmp_path, profile="report")).sort_file(source, resume=False)
    records = [
        json.loads(line)
        for line in result.structured_results_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [record["source_record_position"] for record in records] == [1, 1]


def test_csv_formula_guard_is_conservative():
    assert _csv_safe("=SUM(A1)") == "'=SUM(A1)"
    assert _csv_safe("normal@example.com") == "normal@example.com"
    assert _csv_safe("=SUM(A1)", enabled=False) == "=SUM(A1)"


def test_cli_exposes_multi_source_profiles_and_preview_controls():
    args = build_parser().parse_args(
        [
            "sort",
            "first.txt",
            "second.jsonl",
            "--profile",
            "report",
            "--dry-run",
            "--progress-every",
            "10",
        ]
    )
    assert [path.name for path in args.input] == ["first.txt", "second.jsonl"]
    assert args.profile == "report"
    assert args.dry_run
    assert args.progress_every == 10
