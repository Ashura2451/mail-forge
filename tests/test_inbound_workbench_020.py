from email.message import EmailMessage

from mailforge.application.inbound import InboundService
from mailforge.application.inbound_operations import InboundOperations
from mailforge.application.inbound_sources import SyntheticMailboxSource
from mailforge.infrastructure.campaign_store import CampaignStore


def _message(subject: str = "Quarterly question") -> bytes:
    message = EmailMessage()
    message["From"] = "alice@example.com"
    message["To"] = "operator@example.com"
    message["Reply-To"] = "reply@example.com"
    message["Message-ID"] = "<incoming-020@example.com>"
    message["Date"] = "Tue, 28 Oct 2026 10:00:00 +0000"
    message["Subject"] = subject
    message.set_content("Please review this request.")
    message.add_attachment(
        b"safe fixture", maintype="text", subtype="plain", filename="../../note.txt"
    )
    return message.as_bytes()


def test_workbench_search_extract_rule_replay_and_drafts(tmp_path):
    store = CampaignStore(tmp_path / "mailforge.sqlite3")
    inbound = InboundService(store)
    operations = InboundOperations(store)
    inbound.set_operations(operations)
    rule = operations.create_rule(
        "Review questions",
        {
            "version": 1,
            "conditions": [{"field": "subject", "op": "contains", "value": "Quarterly"}],
            "actions": [
                {"type": "extract", "fields": ["sender", "reply_to", "subject"]},
                {"type": "record_event", "label": "question_received"},
            ],
        },
    )
    operations.set_rule_status(rule["id"], "active")

    report = inbound.ingest(
        SyntheticMailboxSource("fixture", (_message(),)), trusted_synthetic=True
    )
    assert report.processed == 1
    message_id = store.list_inbound_messages()[0]["id"]
    result = operations.search(q="quarterly", classification="human_reply")
    assert result["total"] == 1
    assert result["items"][0]["id"] == message_id
    detail = operations.detail(message_id)
    assert detail["attachments"][0]["filename"] == "note.txt"
    assert len(detail["rule_runs"]) == 1
    assert detail["rule_runs"][0]["status"] == "applied"
    assert {item["field_key"] for item in detail["extractions"]} == {
        "sender",
        "reply_to",
        "subject",
    }
    assert operations.apply_active_rules(message_id)[0]["id"] == detail["rule_runs"][0]["id"]

    reply = operations.prepare_draft(message_id, "reply", body="Thanks for the note.")
    forward = operations.prepare_draft(message_id, "forward")
    assert reply["subject"] == "Re: Quarterly question"
    assert forward["subject"] == "Fwd: Quarterly question"
    assert reply["provenance"]["message_id_header"] == "<incoming-020@example.com>"
    assert operations.prepare_draft(message_id, "reply")["id"] == reply["id"]


def test_workbench_rejects_unrestricted_rule_and_escapes_csv(tmp_path):
    store = CampaignStore(tmp_path / "mailforge.sqlite3")
    operations = InboundOperations(store)
    try:
        operations.create_rule(
            "unsafe",
            {"version": 1, "conditions": [], "actions": [{"type": "execute", "command": "rm"}]},
        )
    except ValueError as error:
        assert "unsupported rule action" in str(error)
    else:
        raise AssertionError("unsafe rule was accepted")
