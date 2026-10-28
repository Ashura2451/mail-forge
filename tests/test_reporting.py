"""Focused coverage for canonical operational reporting semantics."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from mailforge.application.campaigns import CampaignService
from mailforge.application.reporting import ReportFilters, ReportingError, ReportingService
from mailforge.infrastructure.campaign_store import CampaignStore
from mailforge.interfaces.cli import main


def _service(tmp_path):
    store = CampaignStore(tmp_path / "reporting.sqlite3")
    return store, ReportingService(store, now_fn=lambda: datetime(2026, 11, 2, 12, tzinfo=UTC))


def test_metric_registry_declares_source_time_grain_and_evidence(tmp_path) -> None:
    _store, service = _service(tmp_path)

    definitions = service.definitions()

    assert definitions["version"] == "022.1"
    assert definitions["items"]
    assert all(
        item["source_records"] and item["time_field"] and item["grain"] and item["evidence_class"]
        for item in definitions["items"]
    )


def test_summary_is_read_only_and_comparison_is_explicit(tmp_path) -> None:
    store, service = _service(tmp_path)
    before = store.connect().execute("SELECT COUNT(*) FROM operational_events").fetchone()[0]

    report = service.summary(ReportFilters(preset="last_7_days"))

    after = store.connect().execute("SELECT COUNT(*) FROM operational_events").fetchone()[0]
    assert report["range"]["timezone"] == "UTC"
    assert report["previous"] is not None
    assert report["evidence"]["simulated"] is True
    assert before == after


def test_custom_range_preserves_timezone_dst_duration(tmp_path) -> None:
    _store, service = _service(tmp_path)

    report_range = ReportFilters(
        preset="custom",
        start="2026-11-01",
        end="2026-11-03",
        timezone="America/New_York",
    ).resolve(service.now_fn)

    assert report_range.duration.total_seconds() == 49 * 60 * 60
    assert report_range.start_utc.isoformat() == "2026-11-01T04:00:00+00:00"


def test_range_and_pagination_are_bounded(tmp_path) -> None:
    _store, service = _service(tmp_path)

    with pytest.raises(ReportingError, match="limited to 366 days"):
        service.summary(
            ReportFilters(
                preset="custom",
                start="2020-01-01",
                end="2022-01-01",
            )
        )
    with pytest.raises(ReportingError, match="pagination"):
        service.summary(ReportFilters(limit=201))


def test_campaign_export_escapes_spreadsheet_formulas(tmp_path) -> None:
    store, service = _service(tmp_path)
    list_id = store.create_list("reporting audience")
    CampaignService(store).create_campaign('=HYPERLINK("https://unsafe.invalid")', list_id)

    content, media_type, filename = service.export(
        "campaigns", ReportFilters(preset="last_30_days"), format="csv"
    )

    assert media_type.startswith("text/csv")
    assert filename == "mailforge-campaigns.csv"
    assert b"definition_version" in content
    assert b"range" in content
    assert b"'=HYPERLINK" in content


def test_campaign_detail_is_allowlisted_and_does_not_expose_configuration(tmp_path) -> None:
    store, service = _service(tmp_path)
    list_id = store.create_list("private audience")
    campaign_id = CampaignService(store).create_campaign(
        "Private campaign",
        list_id,
        configuration={"subject": "Private subject", "message": "Private body"},
    )

    detail = service.campaign_detail(
        campaign_id,
        ReportFilters(preset="last_30_days", campaign_id=campaign_id),
    )

    assert "configuration_json" not in detail["campaign"]
    assert "Private body" not in str(detail)


def test_empty_source_tables_return_complete_report_shapes(tmp_path) -> None:
    _store, service = _service(tmp_path)

    assert service.summary()["metrics"]["inbound"]["pending_reviews"] == 0
    assert service.inbound()["messages"] == []
    assert service.jobs()["jobs"] == []


def test_cli_reports_summary_and_export_use_canonical_service(tmp_path, capsys) -> None:
    database = tmp_path / "cli.sqlite3"
    output = tmp_path / "summary.csv"

    assert main(["reports", "summary", "--database", str(database)]) == 0
    assert '"report_type": "summary"' in capsys.readouterr().out
    assert (
        main(
            [
                "reports",
                "export",
                "--database",
                str(database),
                "--report-type",
                "summary",
                "--format",
                "csv",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert output.read_text(encoding="utf-8").startswith("definition_version")

    with pytest.raises(SystemExit, match="timezone_invalid"):
        main(["reports", "summary", "--database", str(database), "--timezone", "Not/AZone"])
