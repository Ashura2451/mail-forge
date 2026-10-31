from __future__ import annotations

import hashlib
import io
import zipfile
from email.parser import BytesParser
from email.policy import SMTP
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mailforge.application.message_studio import MessageStudioError, MessageStudioService
from mailforge.application.outbound import OutboundService
from mailforge.domain.campaign import ContactCandidate
from mailforge.domain.outbound import SenderPolicy
from mailforge.domain.provider import ProviderKind, ProviderProfile, TLSMode
from mailforge.infrastructure.campaign_store import CampaignStore
from mailforge.infrastructure.credentials import MappingCredentialResolver
from mailforge.infrastructure.smtp import LiveSendGate
from mailforge.interfaces.web import create_app
from tests.test_provider_interoperability import (
    _certificate,
    _ServerContext,
    _SMTPServer,
)


def _studio(tmp_path: Path) -> MessageStudioService:
    store = CampaignStore(tmp_path / "custom-html.sqlite3")
    return MessageStudioService(store, outbound=OutboundService(store))


def _zip(entries: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return output.getvalue()


def test_custom_html_import_preserves_source_assets_preview_and_export(tmp_path: Path) -> None:
    studio = _studio(tmp_path)
    source = b"""<html><head><style>.hero { color: #05635d; }</style></head>
    <body><table role='presentation'><tr><td class='hero'>Hello {{ name }}</td></tr></table>
    <img src='images/logo.png' alt='Logo'><a href='https://example.com'>Read</a></body></html>"""
    document = studio.import_custom_html(
        "Imported newsletter",
        source,
        filename="newsletter.html",
        assets_zip=_zip({"images/logo.png": b"\x89PNG\r\n\x1a\nfixture"}),
    )
    revision = document["draft"]
    custom = revision["document"]["custom_html"]
    assert custom["original_source"] == source.decode()
    assert custom["source_hash"]
    assert custom["asset_manifest"][0]["reference"] == "images/logo.png"
    preview = studio.preview(revision_id=revision["id"])
    assert "Hello Sample recipient" in preview["html"]
    assert "data:image/png;base64" in preview["html"]
    assert "Read (https://example.com)" in preview["text"]
    exported, filename = studio.export_custom_html(document["id"])
    assert filename.endswith(".zip")
    with zipfile.ZipFile(io.BytesIO(exported)) as archive:
        assert archive.read("original.html") == source
        assert archive.read("images/logo.png").startswith(b"\x89PNG")


def test_custom_html_revisions_are_immutable_and_personalization_is_escaped(tmp_path: Path) -> None:
    studio = _studio(tmp_path)
    document = studio.import_custom_html(
        "Personalized",
        b"<table><tr><td>Hello {{ name }}</td></tr></table>",
        filename="mail.htm",
    )
    first = document["draft"]
    changed = {
        **first["document"],
        "custom_html": {
            **first["document"]["custom_html"],
            "original_source": "<p>Changed {{ name }}</p>",
        },
    }
    saved = studio.save_draft(document["id"], changed, expected_revision_id=first["id"])
    assert (
        saved["revisions"][-1]["document"]["custom_html"]["original_source"]
        == first["document"]["custom_html"]["original_source"]
    )
    assert (
        saved["draft"]["document"]["custom_html"]["original_source"] == "<p>Changed {{ name }}</p>"
    )
    assert "Changed Sample recipient" in studio.preview(revision_id=saved["draft"]["id"])["html"]
    assert "Changed Sample recipient" in studio.preview(revision_id=saved["draft"]["id"])["text"]


@pytest.mark.parametrize(
    "source",
    [
        b"<script>alert(1)</script>",
        b"<img src='javascript:alert(1)' onerror='alert(1)'>",
        b"<iframe src='https://evil.invalid'></iframe>",
    ],
)
def test_custom_html_rejects_active_content(tmp_path: Path, source: bytes) -> None:
    studio = _studio(tmp_path)
    with pytest.raises(MessageStudioError) as error:
        studio.import_custom_html("Unsafe", source, filename="unsafe.html")
    assert error.value.code in {"active_content", "unsafe_url"}


def test_custom_html_removes_event_handlers_when_attribute_contains_tag_end(tmp_path: Path) -> None:
    studio = _studio(tmp_path)
    with pytest.raises(MessageStudioError) as error:
        studio.import_custom_html(
            "Malformed active content",
            b'<p title="x > y" onclick="alert(1)">Hello</p>',
            filename="unsafe.html",
        )
    assert error.value.code == "event_handler"


def test_custom_html_rejects_non_html_file_content(tmp_path: Path) -> None:
    studio = _studio(tmp_path)
    with pytest.raises(MessageStudioError) as error:
        studio.import_custom_html("Not HTML", b"<>", filename="not-html.html")
    assert error.value.code == "html_signature"


def test_custom_html_removes_remote_srcset_and_poster_resources(tmp_path: Path) -> None:
    studio = _studio(tmp_path)
    document = studio.import_custom_html(
        "Remote resources",
        b"<img srcset='https://evil.invalid/a.png 1x, //evil.invalid/b.png 2x' poster='https://evil.invalid/poster.png'>",
        filename="remote.html",
    )
    preview = studio.preview(revision_id=document["draft"]["id"])
    assert "evil.invalid" not in preview["html"]
    assert any(item["code"] == "remote_resource" for item in preview["diagnostics"])


def test_zip_import_rejects_traversal_duplicates_and_nested_archive(tmp_path: Path) -> None:
    studio = _studio(tmp_path)
    for entries in (
        {"../escape.png": b"x"},
        {"a.png": b"x", "./a.png": b"y"},
        {"nested.zip": b"PK"},
    ):
        with pytest.raises(ValueError):
            studio.import_custom_html(
                "Unsafe archive", b"<p>Hello</p>", filename="x.html", assets_zip=_zip(entries)
            )


def test_custom_html_controlled_proof_uses_exact_composed_mime(tmp_path: Path) -> None:
    studio = _studio(tmp_path)
    document = studio.import_custom_html("Proof", b"<p>Hello {{ name }}</p>", filename="proof.html")
    published = studio.publish(document["id"], expected_revision_id=document["draft"]["id"])
    proof = studio.prepare_controlled_test(
        revision_id=published["published"]["id"],
        sender=SenderPolicy(ref="sender", provider="synthetic", email="sender@example.com"),
        recipient="owned@example.com",
    )
    stored = studio.store.get_outbound_proof(proof["proof_id"])
    assert stored["mime_bytes"]
    assert stored["proof"]
    assert stored["proof"]["source_identity"]
    parsed = BytesParser(policy=SMTP).parsebytes(stored["mime_bytes"])
    assert parsed["From"] == "sender@example.com"
    assert b"Hello there" in stored["mime_bytes"]


def test_custom_html_api_accepts_html_and_optional_asset_zip(tmp_path: Path) -> None:
    app = create_app(tmp_path / "api.sqlite3", allowed_hosts={"testserver"})
    with TestClient(app) as client:
        session = client.get("/api/v1/session")
        headers = {"x-mailforge-csrf": session.json()["csrf_token"]}
        response = client.post(
            "/api/v1/message-studio/custom-html/import",
            headers=headers,
            files={
                "html_file": ("mail.html", b"<p>Hello {{ name }}</p>", "text/html"),
                "assets_zip": (
                    "assets.zip",
                    _zip({"logo.png": b"\x89PNG\r\n\x1a\nfixture"}),
                    "application/zip",
                ),
            },
            data={"name": "API imported", "kind": "template"},
        )
        assert response.status_code == 200
        assert response.json()["import"]["asset_count"] == 1
    app.state.runtime.close()


def test_custom_html_fixture_received_mime_matches_proof_bound_preparation(tmp_path: Path) -> None:
    cert, key = _certificate(tmp_path)
    smtp = _SMTPServer(cert, key)
    store = CampaignStore(tmp_path / "mime.sqlite3")
    outbound = OutboundService(store)
    studio = MessageStudioService(store, outbound=outbound)
    sender = SenderPolicy(ref="custom-sender", provider="loopback", email="sender@example.com")
    outbound.register_sender_policy(sender)
    imported = studio.import_custom_html(
        "Fixture newsletter",
        (
            b"<html><body><table><tr><td>Hello {{ name }}</td></tr></table>"
            b"<img src='logo.png' alt='Logo'></body></html>"
        ),
        filename="newsletter.html",
        assets_zip=_zip({"logo.png": b"\x89PNG\r\n\x1a\nfixture"}),
    )
    published = studio.publish(imported["id"], expected_revision_id=imported["draft"]["id"])
    asset_id = published["published"]["document"]["custom_html"]["asset_manifest"][0]["asset_id"]
    list_id = store.create_list("Fixture audience")
    outbound.campaigns.import_candidates(
        [
            ContactCandidate(
                "owned@example.com", "owned@example.com", "synthetic", 1, {"name": "Owned"}
            )
        ],
        list_id,
    )
    campaign_id = outbound.campaigns.create_campaign(
        "Fixture custom HTML",
        list_id,
        sender_config_ref=sender.ref,
        template_ref="studio",
        message_revision_id=published["published"]["id"],
    )
    prepared_report = studio.prepare_campaign(
        campaign_id,
        sender=sender,
        revision_id=published["published"]["id"],
    )
    assert prepared_report.prepared == 1
    intent = store.list_intents(campaign_id)[0]
    prepared = store.get_prepared_message(intent["id"])
    proof = store.get_outbound_proof(prepared["proof_id"])
    assert proof is not None
    store.register_provider_profile(
        ProviderProfile(
            ref="custom-loopback",
            kind=ProviderKind.SMTP,
            host="localhost",
            port=smtp.server_address[1],
            username="user",
            credential_ref="test:smtp",
            tls_mode=TLSMode.STARTTLS,
            sender_policy_ref=sender.ref,
            ca_file=str(cert),
        )
    )
    resolver = MappingCredentialResolver({"test:smtp": "synthetic-password"})
    outbound.authorize_simulation(campaign_id)
    with _ServerContext(smtp):
        report, _ = outbound.execute_provider(
            campaign_id,
            "custom-loopback",
            resolver,
            gate=LiveSendGate(enabled=True),
        )
    assert report.counts["accepted"] == 1
    assert len(smtp.received_messages) == 1
    received = smtp.received_messages[0]
    assert received == bytes(prepared["mime_bytes"])
    assert proof["content_hash"] == hashlib.sha256(received).hexdigest()
    parsed = BytesParser(policy=SMTP).parsebytes(received)
    assert parsed["From"] == "sender@example.com"
    assert parsed["To"] == "owned@example.com"
    assert parsed["Subject"] == "A thoughtful note from Mailforge"
    parts = list(parsed.walk())
    assert any(
        part.get_content_type() == "text/html" and "Hello Owned" in part.get_content()
        for part in parts
    )
    assert any(
        part.get_content_type() == "text/plain" and "Hello Owned" in part.get_content()
        for part in parts
    )
    assert any(part.get("Content-ID") == f"<mailforge-custom-{asset_id}>" for part in parts)
    assert any(part.get_content_type() == "image/png" for part in parts)
