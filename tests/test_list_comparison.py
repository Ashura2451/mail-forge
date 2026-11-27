from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from mailforge.application.list_comparison import ListComparisonService
from mailforge.infrastructure.campaign_store import CampaignStore
from mailforge.interfaces.web import create_app


def test_file_comparison_normalizes_deduplicates_and_preserves_invalid_count(
    tmp_path: Path,
) -> None:
    main = tmp_path / "main.csv"
    reference = tmp_path / "reference.txt"
    main.write_text("ALICE@example.com\nalice@example.com\nnot-an-address\nbob@example.com\n")
    reference.write_text("bob@example.com\ncarol@example.com\n")

    service = ListComparisonService(CampaignStore(tmp_path / "mailforge.sqlite3"))
    result = service.compare_files(main, [reference], operation="subtract")

    assert [item["email"] for item in result["results"]] == ["alice@example.com"]
    assert result["counts"] == {
        "result": 1,
        "main_unique": 2,
        "reference_unique": 2,
        "main_duplicates": 1,
        "main_invalid": 1,
    }
    assert result["non_destructive"] is True


def test_file_comparison_uses_csv_address_column_and_keeps_row_data_out_of_errors(
    tmp_path: Path,
) -> None:
    main = tmp_path / "main.csv"
    reference = tmp_path / "reference.csv"
    main.write_text(
        "email,name\nalice@example.com,Alice\nbob@example.com,Bob\n",
        encoding="utf-8",
    )
    reference.write_text("email,name\nbob@example.com,Old Bob\n", encoding="utf-8")

    result = ListComparisonService(CampaignStore(tmp_path / "structured.sqlite3")).compare_files(
        main, [reference], operation="subtract"
    )

    assert result["counts"]["main_invalid"] == 0
    assert [item["email"] for item in result["results"]] == ["alice@example.com"]


def test_saved_list_comparison_does_not_include_prepared_but_unsent_history(tmp_path: Path) -> None:
    store = CampaignStore(tmp_path / "mailforge.sqlite3")
    main_list = store.create_list("Main")
    reference_list = store.create_list("Reference")
    alice_id = store.upsert_contact(
        "alice@example.com",
        metadata={},
        source_file="main.txt",
        source_record_position=1,
        original_input="alice@example.com",
    )
    bob_id = store.upsert_contact(
        "bob@example.com",
        metadata={},
        source_file="main.txt",
        source_record_position=2,
        original_input="bob@example.com",
    )
    # Membership is intentionally explicit so this test also covers saved-list identity.
    store.add_to_list(main_list, alice_id)
    store.add_to_list(main_list, bob_id)
    store.add_to_list(reference_list, bob_id)

    result = ListComparisonService(store).compare_saved_lists(
        main_list, [reference_list], operation="subtract", history_campaign_ids=["missing"]
    )

    assert [item["email"] for item in result["results"]] == ["alice@example.com"]
    assert result["history_is_successful_submission_only"] is False


def test_web_comparison_endpoint_accepts_multiple_files_without_persistence_side_effect(
    tmp_path: Path,
) -> None:
    with TestClient(
        create_app(tmp_path / "web.sqlite3", allowed_hosts={"testserver", "127.0.0.1"})
    ) as client:
        csrf = client.get("/api/v1/session").json()["csrf_token"]
        response = client.post(
            "/api/v1/list-comparison/preview",
            headers={"x-mailforge-csrf": csrf},
            files=[
                ("main", ("main.txt", b"alice@example.com\nbob@example.com\n", "text/plain")),
                ("references", ("old.txt", b"bob@example.com\n", "text/plain")),
            ],
            data={"operation": "subtract", "campaign_ids": "[]", "reference_list_ids": "[]"},
        )

    assert response.status_code == 200
    assert [item["email"] for item in response.json()["results"]] == ["alice@example.com"]


def test_list_tools_preview_preserves_csv_row_boundaries(tmp_path: Path) -> None:
    with TestClient(
        create_app(tmp_path / "preview.sqlite3", allowed_hosts={"testserver", "127.0.0.1"})
    ) as client:
        csrf = client.get("/api/v1/session").json()["csrf_token"]
        response = client.post(
            "/api/v1/list-tools/preview",
            headers={"x-mailforge-csrf": csrf},
            files={
                "file": (
                    "recipients.csv",
                    b"email,name\nalice@example.com,Alice\n",
                    "text/csv",
                )
            },
        )

    assert response.status_code == 200
    assert response.json()["accepted"] == 1
    assert response.json()["invalid"] == 0


def test_sorted_result_handoff_reuses_the_canonical_saved_list_authority(tmp_path: Path) -> None:
    with TestClient(
        create_app(tmp_path / "handoff.sqlite3", allowed_hosts={"testserver", "127.0.0.1"})
    ) as client:
        csrf = client.get("/api/v1/session").json()["csrf_token"]
        headers = {"x-mailforge-csrf": csrf}
        created = client.post(
            "/api/v1/list-tools/jobs?profile=full&dns_mode=offline",
            headers=headers,
            files={"file": ("sorted.txt", b"alice@example.com\nbob@example.com\n", "text/plain")},
        )
        assert created.status_code == 200
        job_id = created.json()["id"]
        started = client.post(f"/api/v1/list-tools/jobs/{job_id}/start", headers=headers)
        assert started.status_code == 200
        for _ in range(100):
            job = client.get(f"/api/v1/list-tools/jobs/{job_id}").json()
            if job["status"] == "completed":
                break
            time.sleep(0.01)
        assert job["status"] == "completed"

        handoff = client.post(
            f"/api/v1/list-tools/jobs/{job_id}/handoff",
            headers=headers,
            json={},
        )
        assert handoff.status_code == 200, handoff.text
        assert handoff.json()["records_imported"] == 2
        list_id = handoff.json()["list"]["id"]

        repeated = client.post(
            f"/api/v1/list-tools/jobs/{job_id}/handoff",
            headers=headers,
            json={},
        )
        assert repeated.status_code == 200
        assert repeated.json()["reused"] is True
        assert repeated.json()["list"]["id"] == list_id

        comparison = client.post(
            "/api/v1/list-comparison/preview",
            headers=headers,
            data={"main_list_id": list_id, "operation": "subtract"},
        )
        assert comparison.status_code == 200
        assert [item["email"] for item in comparison.json()["results"]] == [
            "alice@example.com",
            "bob@example.com",
        ]


def test_direct_message_revision_can_bind_to_a_campaign_without_manual_template_setup(
    tmp_path: Path,
) -> None:
    with TestClient(
        create_app(tmp_path / "sender.sqlite3", allowed_hosts={"testserver", "127.0.0.1"})
    ) as client:
        csrf = client.get("/api/v1/session").json()["csrf_token"]
        headers = {"x-mailforge-csrf": csrf}
        list_id = client.post(
            "/api/v1/lists", headers=headers, json={"name": "direct-send"}
        ).json()["id"]
        assert (
            client.post(
                f"/api/v1/imports?list_id={list_id}",
                headers=headers,
                files={"file": ("recipients.txt", b"alice@example.com\n", "text/plain")},
            ).status_code
            == 200
        )
        document = client.post(
            "/api/v1/message-studio/documents",
            headers=headers,
            json={
                "name": "Direct HTML",
                "document": {
                    "version": 1,
                    "name": "Direct HTML",
                    "subject": "Hello",
                    "preheader": "",
                    "composition_mode": "custom_html",
                    "custom_html": {
                        "original_source": "<p>Hello {{ email }}</p>",
                        "asset_manifest": [],
                    },
                    "blocks": [{"type": "text", "text": "Hello"}],
                    "fallbacks": {},
                    "attachments": [],
                },
            },
        ).json()
        published = client.post(
            f"/api/v1/message-studio/documents/{document['id']}/publish", headers=headers
        ).json()
        campaign = client.post(
            "/api/v1/campaigns",
            headers=headers,
            json={
                "name": "Direct send",
                "list_id": list_id,
                "message_revision_id": published["current_published_revision_id"],
                "sender_email": "sender@example.com",
                "subject": "Hello",
                "text": "Hello {{ email }}",
            },
        )

    assert campaign.status_code == 200
