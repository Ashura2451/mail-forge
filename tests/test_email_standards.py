from __future__ import annotations

import io
import zipfile
from email.parser import BytesParser
from email.policy import SMTP
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mailforge.application.campaign_operations import (
    CampaignOperationsError,
    CampaignOperationsService,
)
from mailforge.application.campaigns import CampaignService
from mailforge.application.email_standards import EmailStandardsError, EmailStandardsService
from mailforge.application.message_studio import MessageStudioService
from mailforge.application.outbound import OutboundService
from mailforge.application.provider_operations import ProviderOperationsService
from mailforge.application.reporting import ReportingService
from mailforge.application.segmentation import SegmentationService
from mailforge.infrastructure.campaign_store import CampaignStore
from mailforge.interfaces.web import create_app


class FixtureResolver:
    def __init__(self, records: dict[tuple[str, str], list[str]]):
        self.records = records
        self.calls: list[tuple[str, str]] = []

    def resolve(self, name: str, record_type: str):
        self.calls.append((name, record_type))
        return self.records.get((name.rstrip("."), record_type), [])


def _service(tmp_path: Path, records: dict[tuple[str, str], list[str]] | None = None):
    store = CampaignStore(tmp_path / "standards.sqlite3")
    return store, EmailStandardsService(
        store,
        resolver=FixtureResolver(records or {}),
        endpoint_probe=lambda _url: True,
    )


def test_schema_and_sender_identity_are_durable(tmp_path: Path) -> None:
    store, service = _service(tmp_path)
    assert store.schema_version() == 24
    saved = service.register_sender_identity(
        "primary",
        visible_from="Sender@example.com",
        reply_to="reply@example.com",
        envelope_from="bounce@example.com",
        dkim_domain="example.com",
        dkim_selector="mail",
    )
    assert saved["sending_domain"] == "example.com"
    assert service.get_sender_identity("primary")["visible_from"] == "Sender@example.com"


def test_spf_fixture_distinguishes_syntax_from_evaluation_and_alignment(tmp_path: Path) -> None:
    records = {
        ("example.com", "TXT"): ["v=spf1 ip4:192.0.2.0/24 -all"],
        ("_dmarc.example.com", "TXT"): [
            "v=DMARC1; p=none; adkim=s; aspf=s; rua=mailto:dmarc@example.com"
        ],
        ("mail._domainkey.example.com", "TXT"): ["v=DKIM1; k=rsa; p=QUJDRA=="],
        ("example.com", "MX"): ["mx.example.com"],
        ("example.com", "A"): ["192.0.2.10"],
        ("example.com", "AAAA"): [],
        ("_dmarc.example.com", "MX"): [],
        ("mail._domainkey.example.com", "MX"): [],
    }
    _, service = _service(tmp_path, records)
    result = service.inspect_domain(
        "example.com",
        sender_ip="192.0.2.10",
        envelope_from="bounce@example.com",
        dkim_selector="mail",
        allow_network=True,
    )
    assert result["spf"]["status"] == "configuration_valid"
    assert result["spf"]["evaluation"]["status"] == "pass"
    assert result["dkim"]["status"] == "valid"
    assert result["dmarc"]["tags"]["adkim"] == "s"
    assert any(
        item["code"] == "dmarc_not_observed"
        for item in service.inspect_domain("other.example", allow_network=False)["findings"]
    )


def test_spf_lookup_limit_and_multiple_records_are_blocked(tmp_path: Path) -> None:
    chain = {("root.example", "TXT"): ["v=spf1 include:a.example -all"]}
    for left, right in zip("abcdefghijk", "bcdefghijkl", strict=True):
        chain[(f"{left}.example", "TXT")] = [f"v=spf1 include:{right}.example -all"]
    chain[("l.example", "TXT")] = ["v=spf1 ip4:192.0.2.0/24 -all"]
    chain[("root.example", "MX")] = []
    _, service = _service(tmp_path, chain)
    result = service.inspect_domain(
        "root.example", sender_ip="192.0.2.1", envelope_from="a@root.example", allow_network=True
    )
    assert any(item["code"] == "spf_lookup_limit" for item in result["findings"])
    _, multiple = _service(
        tmp_path / "multiple", {("example.com", "TXT"): ["v=spf1 -all", "v=spf1 ~all"]}
    )
    assert any(
        item["code"] == "spf_multiple_records"
        for item in multiple.inspect_domain("example.com", allow_network=True)["findings"]
    )


def test_message_validation_reports_cid_and_untrusted_authentication(tmp_path: Path) -> None:
    _, service = _service(tmp_path)
    raw = (
        b"From: sender@example.com\r\n"
        b"To: recipient@example.com\r\n"
        b"Date: Tue, 28 Oct 2026 12:00:00 +0000\r\n"
        b"Message-ID: <id@example.com>\r\n"
        b"Subject: Hello\r\n"
        b"Authentication-Results: mx.example; dkim=pass\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n\r\n"
        b'<img src="cid:missing"><a href="javascript:alert(1)">x</a>'
    )
    result = service.validate_message(raw, communication_purpose="marketing")
    codes = {item["code"] for item in result["findings"]}
    assert {"missing_plain_text", "cid_missing", "unsafe_url", "unsubscribe_missing"} <= codes
    assert (
        result["authentication"]["authentication_results"][0]["evidence"]
        == "untrusted_imported_claim"
    )


def test_unsubscribe_post_is_scoped_idempotent_and_get_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, service = _service(tmp_path)
    list_id = store.create_list("Newsletter")
    contact_id = store.upsert_contact(
        "alice@example.com",
        source_file="fixture",
        source_record_position=1,
        original_input="alice@example.com",
    )
    store.add_to_list(list_id, contact_id)
    service.configure_unsubscribe_endpoint(
        "local",
        "http://127.0.0.1:8777/api/v1/unsubscribe/local",
        verification_status="external_configured",
    )
    token = service.create_unsubscribe_token("local", list_id, "alice@example.com")
    processed = service.process_unsubscribe(
        "local", token["token"], method="POST", body="List-Unsubscribe=One-Click", list_id=list_id
    )
    assert processed["status"] == "processed"
    assert (
        service.process_unsubscribe(
            "local", token["token"], method="POST", body="List-Unsubscribe=One-Click"
        )["status"]
        == "already_processed"
    )
    with pytest.raises(EmailStandardsError, match="GET"):
        service.process_unsubscribe("local", token["token"], method="GET", body="")
    assert store.list_suppressions(normalized_email="alice@example.com")[0]["scope_id"] == list_id
    retry_token = service.create_unsubscribe_token("local", list_id, "alice@example.com")
    original_add_suppression = store.add_suppression

    def fail_suppression(*args, **kwargs):
        raise RuntimeError("injected policy write failure")

    monkeypatch.setattr(store, "add_suppression", fail_suppression)
    with pytest.raises(RuntimeError, match="policy write"):
        service.process_unsubscribe(
            "local",
            retry_token["token"],
            method="POST",
            body="List-Unsubscribe=One-Click",
            list_id=list_id,
        )
    monkeypatch.setattr(store, "add_suppression", original_add_suppression)
    assert (
        service.process_unsubscribe(
            "local", retry_token["token"], method="POST", body="List-Unsubscribe=One-Click"
        )["status"]
        == "processed"
    )
    with pytest.raises(EmailStandardsError, match="invalid"):
        service.configure_unsubscribe_endpoint(
            "public", "https://example.invalid/unsubscribe", verification_status="external_verified"
        )


def test_dmarc_report_import_is_bounded_and_deduplicated(tmp_path: Path) -> None:
    _, service = _service(tmp_path)
    xml = b"""<?xml version='1.0'?>
    <feedback>
      <report_metadata><org_name>fixture-mx</org_name><report_id>r-1</report_id>
        <date_range><begin>1</begin><end>2</end></date_range>
      </report_metadata>
      <policy_published><domain>example.com</domain><p>none</p><adkim>s</adkim><aspf>r</aspf></policy_published>
      <record><row><source_ip>192.0.2.1</source_ip><count>2</count>
        <policy_evaluated><disposition>none</disposition><dkim>pass</dkim><spf>pass</spf></policy_evaluated>
      </row><identifiers><header_from>example.com</header_from></identifiers></record>
    </feedback>"""
    imported = service.import_dmarc_report(xml, filename="report.xml")
    assert imported["status"] == "imported"
    assert service.import_dmarc_report(xml, filename="report.xml")["status"] == "duplicate"
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as compressed:
        compressed.writestr("../unsafe.xml", xml)
    with pytest.raises(EmailStandardsError, match="safe"):
        service.import_dmarc_report(archive.getvalue(), filename="report.zip")
    with pytest.raises(EmailStandardsError, match="entities"):
        service.import_dmarc_report(b"<!DOCTYPE foo [<!ENTITY x 'x'>]><feedback/>")
    with pytest.raises(EmailStandardsError, match="entities"):
        service.import_dmarc_report(
            "<!DOCTYPE foo [<!ENTITY x 'x'>]><feedback/>".encode("utf-16"),
            filename="report.xml",
        )


def test_email_standards_api_and_cookie_free_one_click_endpoint(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "web.sqlite3", allowed_hosts={"testserver"})) as client:
        session = client.get("/api/v1/session")
        headers = {"x-mailforge-csrf": session.json()["csrf_token"]}
        identity = client.post(
            "/api/v1/email-standards/identities",
            headers=headers,
            json={"identity_ref": "primary", "visible_from": "sender@example.com"},
        )
        assert identity.status_code == 200
        assert client.get("/api/v1/email-standards", headers=headers).json()["identities"]
        list_id = client.post("/api/v1/lists", headers=headers, json={"name": "API list"}).json()[
            "id"
        ]
        client.post(
            f"/api/v1/imports?list_id={list_id}",
            headers=headers,
            files={"file": ("contacts.txt", b"alice@example.com\n", "text/plain")},
        )
        endpoint = client.post(
            "/api/v1/email-standards/unsubscribe/endpoints",
            headers=headers,
            json={
                "endpoint_ref": "local",
                "base_url": "http://127.0.0.1:8777/api/v1/unsubscribe/local",
                "verification_status": "external_configured",
            },
        )
        assert endpoint.status_code == 200
        token = client.post(
            "/api/v1/email-standards/unsubscribe/endpoints/local/tokens",
            headers=headers,
            json={"list_id": list_id, "email": "alice@example.com"},
        ).json()["token"]
        one_click = client.post(
            f"/api/v1/unsubscribe/local/{token}",
            content=b"List-Unsubscribe=One-Click",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        assert one_click.status_code == 200
        assert one_click.json()["status"] == "processed"
        assert client.get(f"/api/v1/unsubscribe/local/{token}").status_code == 405


def test_marketing_campaign_preparation_emits_scoped_one_click_headers(tmp_path: Path) -> None:
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    list_id = store.create_list("Marketing audience")
    contact_id = store.upsert_contact(
        "alice@example.com",
        source_file="fixture",
        source_record_position=1,
        original_input="alice@example.com",
    )
    store.add_to_list(list_id, contact_id)
    standards = EmailStandardsService(
        store, resolver=FixtureResolver({}), endpoint_probe=lambda _url: True
    )
    standards.configure_unsubscribe_endpoint(
        "local",
        "https://127.0.0.1:8777/api/v1/unsubscribe/local",
        verification_status="external_configured",
    )
    standards.verify_local_fixture_endpoint("local")
    outbound = OutboundService(store)
    segmentation = SegmentationService(store)
    operations = CampaignOperationsService(
        store,
        campaigns=CampaignService(store),
        outbound=outbound,
        message_studio=MessageStudioService(store, outbound=outbound, segmentation=segmentation),
        segmentation=segmentation,
        provider_operations=ProviderOperationsService(store),
        reporting=ReportingService(store),
        email_standards=standards,
    )
    campaign_id = operations.create_draft(
        name="Marketing",
        list_id=list_id,
        purpose="marketing",
        unsubscribe_endpoint_ref="local",
    )
    prepared = operations.prepare(campaign_id)
    assert prepared["prepared"] == 1
    prepared_message = store.get_prepared_message(store.list_intents(campaign_id)[0]["id"])
    message = BytesParser(policy=SMTP).parsebytes(prepared_message["mime_bytes"])
    assert message["List-Unsubscribe"].startswith("<https://127.0.0.1:8777/")
    assert message["List-Unsubscribe-Post"] is None
    blocked_id = operations.create_draft(
        name="Marketing one-click",
        list_id=list_id,
        purpose="marketing",
        unsubscribe_endpoint_ref="local",
        one_click_unsubscribe=True,
        dkim_signed=True,
    )
    with pytest.raises(CampaignOperationsError, match="preflight"):
        operations.prepare(blocked_id)
