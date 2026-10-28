from __future__ import annotations

from decimal import Decimal

import pytest

from mailforge.application.campaigns import CampaignService
from mailforge.application.segmentation import (
    NUMBER_SCALE,
    SegmentationError,
    SegmentationService,
    _scaled_number,
)
from mailforge.infrastructure.campaign_store import CampaignStore


def make_store(tmp_path):
    return CampaignStore(tmp_path / "audience.sqlite3")


def make_contact(store, email):
    return store.upsert_contact(
        email,
        source_file="synthetic",
        source_record_position=1,
        original_input=email,
    )


def test_number_storage_is_fixed_point_and_rejects_precision_or_overflow():
    assert _scaled_number(Decimal("-12.345678")) == -12_345_678
    assert _scaled_number("0") == 0
    assert _scaled_number("999999999999.999999") == 999_999_999_999_999_999
    assert NUMBER_SCALE == 1_000_000
    with pytest.raises(SegmentationError):
        _scaled_number("1.0000001")
    with pytest.raises(SegmentationError):
        _scaled_number("1000000000000")
    with pytest.raises(SegmentationError):
        _scaled_number("1E+12")
    with pytest.raises(SegmentationError):
        _scaled_number(2.5)
    with pytest.raises(SegmentationError):
        _scaled_number("NaN")


def test_missing_empty_and_unknown_not_semantics(tmp_path):
    store = make_store(tmp_path)
    service = SegmentationService(store)
    field = service.create_field("tier", "Tier", "TEXT")
    for email in ("empty@example.com", "other@example.com", "missing@example.com"):
        make_contact(store, email)
    service.import_structured(
        [
            {"email": "empty@example.com", "tier": ""},
            {"email": "other@example.com", "tier": "other"},
        ],
        mappings={"email": "contact.email", "tier": field["id"]},
        list_id=store.create_list("all"),
    )
    definition = {
        "version": 1,
        "root": {
            "kind": "NOT",
            "child": {
                "kind": "PREDICATE",
                "field_id": field["id"],
                "operator": "EQUALS",
                "value": "other",
            },
        },
    }
    segment = service.create_segment("not-other", definition)
    preview = service.preview(segment["id"])
    assert [item["normalized_email"] for item in preview["items"]] == ["empty@example.com"]

    missing = service.create_segment(
        "missing-tier",
        {
            "version": 1,
            "root": {"kind": "PREDICATE", "field_id": field["id"], "operator": "IS_MISSING"},
        },
    )
    assert service.preview(missing["id"])["matched_count"] == 1


def test_nested_rules_are_parameterized_and_reject_injection(tmp_path):
    store = make_store(tmp_path)
    service = SegmentationService(store)
    field = service.create_field("score", "Score", "NUMBER")
    list_id = store.create_list("all")
    service.import_structured(
        [
            {"email": "one@example.com", "score": "-1.25"},
            {"email": "two@example.com", "score": "2.5"},
        ],
        mappings={"email": "contact.email", "score": field["id"]},
        list_id=list_id,
    )
    definition = {
        "version": 1,
        "root": {
            "kind": "ANY",
            "children": [
                {"kind": "PREDICATE", "field_id": field["id"], "operator": "LESS_THAN", "value": 0},
                {
                    "kind": "NOT",
                    "child": {
                        "kind": "PREDICATE",
                        "field_id": field["id"],
                        "operator": "EQUALS",
                        "value": "2.5",
                    },
                },
            ],
        },
    }
    segment = service.create_segment("scores", definition)
    assert service.preview(segment["id"])["matched_count"] == 1
    with pytest.raises(SegmentationError):
        service.create_segment(
            "bad",
            {
                "version": 1,
                "root": {
                    "kind": "PREDICATE",
                    "field_id": "score'); DROP TABLE contacts;--",
                    "operator": "EQUALS",
                    "value": 1,
                },
            },
        )


def test_import_conflicts_preserve_by_default_and_overwrite_is_audited(tmp_path):
    store = make_store(tmp_path)
    service = SegmentationService(store)
    field = service.create_field("region", "Region", "TEXT")
    list_id = store.create_list("all")
    service.import_structured(
        [{"email": "a@example.com", "region": "north"}],
        mappings={"email": "contact.email", "region": field["id"]},
        list_id=list_id,
        source_ref="first",
    )
    preserved = service.import_structured(
        [{"email": "a@example.com", "region": "south"}],
        mappings={"email": "contact.email", "region": field["id"]},
        list_id=list_id,
        source_ref="second",
    )
    assert preserved["conflicts"][0]["disposition"] == "preserved"
    replayed = service.import_structured(
        [{"email": "a@example.com", "region": "south"}],
        mappings={"email": "contact.email", "region": field["id"]},
        list_id=list_id,
        source_ref="second",
    )
    assert replayed["conflicts"] == preserved["conflicts"]
    overwritten = service.import_structured(
        [{"email": "a@example.com", "region": "south"}],
        mappings={"email": "contact.email", "region": field["id"]},
        list_id=list_id,
        source_ref="third",
        overwrite=True,
    )
    assert overwritten["conflicts"][0]["disposition"] == "overwritten"


def test_segment_revision_conflict_and_campaign_snapshot_stability(tmp_path):
    store = make_store(tmp_path)
    service = SegmentationService(store)
    field = service.create_field("kind", "Kind", "TEXT")
    list_id = store.create_list("all")
    service.import_structured(
        [{"email": "a@example.com", "kind": "a"}],
        mappings={"email": "contact.email", "kind": field["id"]},
        list_id=list_id,
    )
    segment = service.create_segment(
        "kind-a",
        {
            "version": 1,
            "root": {
                "kind": "PREDICATE",
                "field_id": field["id"],
                "operator": "EQUALS",
                "value": "a",
            },
        },
    )
    edited = service.edit_segment(
        segment["id"],
        {
            "version": 1,
            "root": {
                "kind": "PREDICATE",
                "field_id": field["id"],
                "operator": "EQUALS",
                "value": "b",
            },
        },
        expected_revision_id=segment["revision"]["id"],
    )
    with pytest.raises(SegmentationError, match="changed"):
        service.edit_segment(
            segment["id"],
            segment["definition"],
            expected_revision_id=segment["revision"]["id"],
        )
    campaign = CampaignService(store).create_campaign(
        "segment-campaign",
        None,
        audience_source_type="segment",
        audience_source_id=segment["id"],
        audience_revision_id=edited["revision"]["id"],
    )
    service.prepare_campaign(campaign)
    assert [row["normalized_email"] for row in store.list_intents(campaign)] == []
    with pytest.raises(SegmentationError, match="conflicts"):
        service.prepare_campaign(
            campaign,
            source_type="segment",
            source_id=segment["id"],
            revision_id=segment["revision"]["id"],
        )


def test_recurring_audience_runs_keep_independent_snapshots(tmp_path):
    store = make_store(tmp_path)
    service = SegmentationService(store)
    field = service.create_field("group_name", "Group", "TEXT")
    list_id = store.create_list("all")
    service.import_structured(
        [{"email": "a@example.com", "group_name": "one"}],
        mappings={"email": "contact.email", "group_name": field["id"]},
        list_id=list_id,
    )
    segment = service.create_segment(
        "one",
        {
            "version": 1,
            "root": {
                "kind": "PREDICATE",
                "field_id": field["id"],
                "operator": "EQUALS",
                "value": "one",
            },
        },
    )
    campaign = CampaignService(store).create_campaign(
        "recurring",
        list_id,
        audience_source_type="segment",
        audience_source_id=segment["id"],
        audience_revision_id=segment["revision"]["id"],
    )
    first = service.prepare_campaign(campaign, run_key="occurrence-1")
    assert len(service.audience_recipients(campaign, "occurrence-1")) == 1
    assert CampaignService(store).report(campaign, run_key="occurrence-1").total == 1
    service.import_structured(
        [{"email": "b@example.com", "group_name": "one"}],
        mappings={"email": "contact.email", "group_name": field["id"]},
        list_id=list_id,
    )
    second = service.prepare_campaign(campaign, run_key="occurrence-2")
    assert first["id"] != second["id"]
    assert len(service.audience_recipients(campaign, "occurrence-1")) == 1
    assert len(service.audience_recipients(campaign, "occurrence-2")) == 2


def test_recurring_list_runs_use_independent_snapshots(tmp_path):
    store = make_store(tmp_path)
    list_id = store.create_list("static")
    contact_id = make_contact(store, "static@example.com")
    store.add_to_list(list_id, contact_id)
    campaign = CampaignService(store).create_campaign("static-campaign", list_id)
    campaigns = CampaignService(store)
    campaigns.prepare(campaign, run_key="occurrence-1")
    assert len(SegmentationService(store).audience_recipients(campaign, "occurrence-1")) == 1
