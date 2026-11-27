from __future__ import annotations

from pathlib import Path

from mailforge.application.list_comparison import ListComparisonService
from mailforge.application.sorting import EmailSorter, SortConfig
from mailforge.domain.sorting import DNSMode
from mailforge.infrastructure.campaign_store import CampaignStore


def _addresses(prefix: str, count: int) -> str:
    return "".join(f"{prefix}{index}@example.com\n" for index in range(count))


def test_sorter_qualifies_a_representative_large_file(tmp_path: Path) -> None:
    source = tmp_path / "large-sort-input.txt"
    source.write_text(_addresses("user", 12_000) + _addresses("user", 1_000), encoding="utf-8")
    config = SortConfig(
        results_dir=tmp_path / "results",
        config_dir=tmp_path / "config",
        log_dir=tmp_path / "logs",
        checkpoint_path=tmp_path / "checkpoint.json",
        checkpoint_interval=2_000,
        min_disk_space_gb=0,
        dns_mode=DNSMode.OFFLINE,
    )

    result = EmailSorter(config).sort_file(source, resume=False)

    assert result.committed is True
    assert result.stats.total_extracted == 13_000
    assert result.stats.unique_valid == 12_000
    assert result.stats.duplicates == 1_000


def test_comparison_qualifies_large_inputs_without_mutating_state(tmp_path: Path) -> None:
    main = tmp_path / "main.txt"
    reference = tmp_path / "reference.txt"
    main.write_text(_addresses("user", 20_000), encoding="utf-8")
    reference.write_text(_addresses("user", 10_000), encoding="utf-8")
    service = ListComparisonService(CampaignStore(tmp_path / "mailforge.sqlite3"))

    result = service.compare_files(main, [reference], operation="subtract")

    assert result["counts"] == {
        "result": 10_000,
        "main_unique": 20_000,
        "reference_unique": 10_000,
        "main_duplicates": 0,
        "main_invalid": 0,
    }
    assert result["non_destructive"] is True
