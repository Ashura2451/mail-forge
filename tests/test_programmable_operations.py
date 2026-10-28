from pathlib import Path

import pytest

from mailforge.application.data_studio import DataStudioService
from mailforge.application.message_studio import MessageStudioService
from mailforge.application.programmable_operations import (
    ProgrammableOperationsError,
    ProgrammableOperationsService,
)
from mailforge.infrastructure.campaign_store import CampaignStore


def test_declarative_function_is_revisioned_bounded_and_durable(tmp_path: Path) -> None:
    service = ProgrammableOperationsService(CampaignStore(tmp_path / "state.sqlite3"))
    created = service.create_function(
        "clean_company",
        {
            "operation": "text.whitespace",
            "contexts": ["data_studio", "message_studio"],
            "parameters": {"field": "company", "output_field": "company_clean"},
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
        },
    )
    result = service.execute(
        "clean_company", [{"company": "  Acme   Co  "}], context="data_studio"
    )
    assert result["records"] == [{"company": "  Acme   Co  ", "company_clean": "Acme Co"}]
    assert result["content_hash"] == created["content_hash"]
    assert service.list_executions("clean_company")[0]["status"] == "completed"
    with pytest.raises(ProgrammableOperationsError, match="disabled"):
        service.execute("email_domain_normalizer", [{"email": "a@example.com"}])


def test_trusted_python_requires_checksum_install_and_enablement(tmp_path: Path) -> None:
    source = tmp_path / "extension.py"
    source.write_text(
        "def run(records, parameters):\n"
        "    return [{**record, 'tag': 'review'} for record in records]\n",
        encoding="utf-8",
    )
    service = ProgrammableOperationsService(CampaignStore(tmp_path / "state.sqlite3"))
    manifest = {
        "name": "tag_records",
        "version": "1.0",
        "entrypoint": "run",
        "definition": {
            "operation": "structured.label",
            "contexts": ["data_studio"],
            "parameters": {},
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
        },
    }
    with pytest.raises(ProgrammableOperationsError, match="confirmation"):
        service.install_python_extension(source, manifest)
    import hashlib

    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    installed = service.install_python_extension(source, manifest, confirm=True)
    assert installed["kind"] == "python"
    with pytest.raises(ProgrammableOperationsError, match="disabled"):
        service.execute("tag_records", [{"id": 1}], context="data_studio")
    service.set_enabled("tag_records", True)
    result = service.execute("tag_records", [{"id": 1}], context="data_studio")
    assert result["records"] == [{"id": 1, "tag": "review"}]
    assert service.get_function("tag_records")["source_sha256"] == digest


def test_data_studio_and_message_studio_use_pinned_function(tmp_path: Path) -> None:
    store = CampaignStore(tmp_path / "state.sqlite3")
    programmable = ProgrammableOperationsService(store)
    revision = programmable.create_function(
        "domain_for_message",
        {
            "operation": "email.domain",
            "contexts": ["data_studio", "message_studio"],
            "parameters": {"field": "email", "output_field": "domain"},
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
        },
    )
    studio = DataStudioService(store, tmp_path / "artifacts", programmable=programmable)
    try:
        (tmp_path / "source.json").write_text("[]", encoding="utf-8")
        preview = studio.preview(
            tmp_path / "source.json",
            {
                "steps": [
                    {
                        "type": "function",
                        "function_id": "domain_for_message",
                        "revision_id": revision["id"],
                        "content_hash": revision["content_hash"],
                    }
                ]
            },
        )
        assert preview["rows"] == []
    finally:
        studio.close()
    message = MessageStudioService(store, programmable=programmable)
    document = {
        "version": 1,
        "name": "Function message",
        "subject": "Hello",
        "preheader": "",
        "fallbacks": {},
        "blocks": [
            {
                "type": "function",
                "function_id": "domain_for_message",
                "revision_id": revision["id"],
                "content_hash": revision["content_hash"],
                "input_mapping": {"email": "email"},
                "output_field": "domain",
            }
        ],
        "attachments": [],
    }
    rendered = message.render_document(document, contact={"normalized_email": "a@example.com"})
    assert "example.com" in rendered["text"]
