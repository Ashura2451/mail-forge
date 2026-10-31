from __future__ import annotations

import io
import zipfile
from email.parser import BytesParser
from email.policy import SMTP
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mailforge.application.message_studio import MessageStudioError, MessageStudioService
from mailforge.application.outbound import OutboundService
from mailforge.domain.outbound import SenderPolicy
from mailforge.infrastructure.campaign_store import CampaignStore
from mailforge.interfaces.web import create_app


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
    changed = {**first["document"], "custom_html": {**first["document"]["custom_html"], "original_source": "<p>Changed {{ name }}</p>"}}
    saved = studio.save_draft(document["id"], changed, expected_revision_id=first["id"])
    assert saved["revisions"][-1]["document"]["custom_html"]["original_source"] == first["document"]["custom_html"]["original_source"]
    assert saved["draft"]["document"]["custom_html"]["original_source"] == "<p>Changed {{ name }}</p>"
    assert "Changed Sample recipient" in studio.preview(revision_id=saved["draft"]["id"])["html"]


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


def test_zip_import_rejects_traversal_duplicates_and_nested_archive(tmp_path: Path) -> None:
    studio = _studio(tmp_path)
    for entries in ({"../escape.png": b"x"}, {"a.png": b"x", "./a.png": b"y"}, {"nested.zip": b"PK"}):
        with pytest.raises(ValueError):
            studio.import_custom_html("Unsafe archive", b"<p>Hello</p>", filename="x.html", assets_zip=_zip(entries))


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
                "assets_zip": ("assets.zip", _zip({"logo.png": b"\x89PNG\r\n\x1a\nfixture"}), "application/zip"),
            },
            data={"name": "API imported", "kind": "template"},
        )
        assert response.status_code == 200
        assert response.json()["import"]["asset_count"] == 1
    app.state.runtime.close()
