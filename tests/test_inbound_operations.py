from __future__ import annotations

from email.message import EmailMessage, Message

import pytest

from mailforge.application.inbound import InboundService
from mailforge.application.inbound_parser import SafeInboundParser
from mailforge.application.inbound_sources import (
    CursorInvalidated,
    DisabledImapSource,
    EmlFileSource,
    MboxSource,
    POP3ConnectionConfig,
    Pop3Source,
    SimulatedImapSource,
    SyntheticMailboxSource,
)
from mailforge.domain.campaign import DeliveryState
from mailforge.domain.inbound import InboundLimits, ParseStatus, SourceMessage
from mailforge.infrastructure.campaign_store import CampaignStore


def _message(sender: str, subject: str, body: str = "body") -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "sender@example.com"
    message["Subject"] = subject
    message.set_content(body)
    return message.as_bytes()


def _dsn(outbound_id: str) -> bytes:
    outer = Message()
    outer.set_type("multipart/report")
    outer.set_param("report-type", "delivery-status")
    machine = Message()
    machine["Reporting-MTA"] = "dns; mx.example.com"
    machine["Original-Envelope-Id"] = outbound_id
    first = Message()
    first["Final-Recipient"] = "rfc822; alice@example.com"
    first["Action"] = "failed"
    first["Status"] = "5.1.1"
    first["Diagnostic-Code"] = "smtp; mailbox unavailable"
    second = Message()
    second["Final-Recipient"] = "rfc822; bob@example.com"
    second["Action"] = "delayed"
    second["Status"] = "4.4.1"
    report = Message()
    report.set_type("message/delivery-status")
    report.set_payload([machine, first, second])
    outer.set_payload([report])
    outer["From"] = "mailer-daemon@example.com"
    outer["To"] = "sender@example.com"
    return outer.as_bytes()


def _prepared_store(tmp_path):
    store = CampaignStore(tmp_path / "mailforge.sqlite3")
    list_id = store.create_list("audience")
    contact_id = store.upsert_contact(
        "alice@example.com",
        source_file="test",
        source_record_position=0,
        original_input="alice@example.com",
    )
    store.add_to_list(list_id, contact_id)
    campaign_id = store.create_campaign(
        name="campaign",
        description="test",
        sender_config_ref="sender",
        template_ref="template",
        audience_list_id=list_id,
    )
    intent_id = store.prepare_intent(
        campaign_id=campaign_id,
        contact_id=contact_id,
        normalized_email="alice@example.com",
        state=DeliveryState.PREPARED,
        eligibility_reason="test",
    )
    store.save_prepared_message(
        intent_id=intent_id,
        template_name="template",
        template_version="1",
        rendering_seed=1,
        message_id="<outbound@example.com>",
        subject="subject",
        from_email="sender@example.com",
        recipient="alice@example.com",
        content_hash="hash",
        mime_bytes=b"Subject: subject\n\nbody\n",
        attachment_metadata=[],
    )
    return store


def test_parser_is_bounded_and_does_not_preview_attachments():
    message = EmailMessage()
    message["From"] = "alice@example.com"
    message["To"] = "sender@example.com"
    message["Subject"] = "encoded"
    message.set_content("visible text")
    message.add_alternative("<p>safe</p><script>secret()</script>", subtype="html")
    message.add_attachment(
        b"binary-secret",
        maintype="application",
        subtype="octet-stream",
        filename="../../unsafe.bin",
    )
    parsed = SafeInboundParser().parse(SourceMessage("test", "1", message.as_bytes()))
    assert parsed.status is ParseStatus.PARSED
    assert "visible text" in parsed.text_preview
    assert "safe" in parsed.html_preview
    assert "secret" not in parsed.html_preview
    assert "binary-secret" not in parsed.text_preview

    limited = SafeInboundParser(InboundLimits(max_message_bytes=20)).parse(
        SourceMessage("test", "2", b"x" * 21)
    )
    assert limited.status is ParseStatus.QUARANTINED
    nested = EmailMessage()
    nested.set_content("nested")
    nested.add_alternative("<p>deep</p>", subtype="html")
    assert (
        SafeInboundParser(InboundLimits(max_mime_depth=0))
        .parse(SourceMessage("test", "depth", nested.as_bytes()))
        .status
        is ParseStatus.PARTIALLY_PARSED
    )


def test_dsn_preserves_multiple_recipients_and_fields():
    parsed = SafeInboundParser().parse(SourceMessage("test", "dsn", _dsn("<outbound@example.com>")))
    assert parsed.dsn is not None
    assert len(parsed.dsn.recipients) == 2
    assert parsed.dsn.recipients[0].final_recipient == "alice@example.com"
    assert parsed.dsn.recipients[0].status == "5.1.1"
    assert parsed.dsn.recipients[1].action.value == "delayed"


def test_arf_feedback_report_is_evidence_not_authentication():
    message = EmailMessage()
    message["From"] = "feedback@example.com"
    message["To"] = "sender@example.com"
    message.set_content("feedback")
    message.add_attachment(
        b"Feedback-Type: abuse\nOriginal-Message-ID: <outbound@example.com>\n"
        b"Original-Rcpt-To: rfc822; alice@example.com\n",
        maintype="message",
        subtype="feedback-report",
    )
    parsed = SafeInboundParser().parse(SourceMessage("test", "arf", message.as_bytes()))
    assert parsed.complaint is not None
    assert parsed.complaint.feedback_type == "abuse"
    assert parsed.complaint.original_recipient == "alice@example.com"


def test_trusted_exact_permanent_dsn_applies_hard_bounce(tmp_path):
    store = _prepared_store(tmp_path)
    service = InboundService(store)
    report = service.ingest(
        SyntheticMailboxSource("synthetic", (_dsn("<outbound@example.com>"),)),
        trusted_synthetic=True,
    )
    assert report.applied_actions == 1
    with store.connect() as connection:
        suppression = connection.execute(
            "SELECT category, normalized_email FROM suppressions"
        ).fetchone()
        action = connection.execute(
            "SELECT applied_at FROM inbound_actions WHERE status = 'applied'"
        ).fetchone()
    assert suppression["category"] == "hard_bounce"
    assert suppression["normalized_email"] == "alice@example.com"
    assert action["applied_at"] is not None
    with store.connect() as connection:
        assert (
            connection.execute("SELECT state FROM delivery_intents").fetchone()["state"]
            == DeliveryState.SUPPRESSED.value
        )
        history = connection.execute(
            "SELECT action, metadata_json FROM suppression_history"
        ).fetchall()
    assert [row["action"] for row in history] == ["created"]
    assert '"source": "trusted-inbound"' in history[0]["metadata_json"]


def test_delayed_dsn_does_not_create_suppression(tmp_path):
    store = _prepared_store(tmp_path)
    message = (
        _dsn("<outbound@example.com>")
        .replace(b"Action: failed", b"Action: delayed")
        .replace(b"Status: 5.1.1", b"Status: 4.4.1")
    )
    report = InboundService(store).ingest(SyntheticMailboxSource("synthetic", (message,)))
    assert report.failures == 0
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM suppressions").fetchone()[0] == 0


def test_untrusted_unsubscribe_requires_review(tmp_path):
    path = tmp_path / "unsubscribe.eml"
    path.write_bytes(_message("alice@example.com", "unsubscribe", "Please remove me"))
    store = CampaignStore(tmp_path / "mailforge.sqlite3")
    service = InboundService(store)
    report = service.ingest(EmlFileSource("local", path))
    assert report.reviews == 1
    assert service.review_queue()[0]["action_type"] == "create_unsubscribe"
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM suppressions").fetchone()[0] == 0


def test_only_synthetic_sources_can_be_marked_trusted(tmp_path):
    store = CampaignStore(tmp_path / "mailforge.sqlite3")
    service = InboundService(store)
    path = tmp_path / "message.eml"
    path.write_bytes(_message("alice@example.com", "hello"))
    with pytest.raises(ValueError, match="synthetic"):
        service.register_source(EmlFileSource("local", path), trusted_synthetic=True)


def test_replay_is_idempotent_and_fault_resume_is_durable(tmp_path):
    store = CampaignStore(tmp_path / "mailforge.sqlite3")
    service = InboundService(store)
    source = SyntheticMailboxSource(
        "synthetic", (_message("a@example.com", "one"), _message("b@example.com", "two"))
    )
    with pytest.raises(RuntimeError):
        service.ingest(source, fault_after=1)
    report = service.ingest(source)
    assert report.processed == 2
    assert service.replay("synthetic") == 2
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM inbound_messages").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM inbound_actions").fetchone()[0] == 2


def test_post_parse_failure_remains_pending_for_restart(tmp_path, monkeypatch):
    store = CampaignStore(tmp_path / "mailforge.sqlite3")
    service = InboundService(store)
    source = SyntheticMailboxSource("synthetic", (_message("a@example.com", "one"),))
    original = service._actions
    monkeypatch.setattr(
        service, "_actions", lambda *args: (_ for _ in ()).throw(RuntimeError("test failure"))
    )
    first = service.ingest(source)
    assert first.failures == 1
    monkeypatch.setattr(service, "_actions", original)
    second = service.ingest(source)
    assert second.processed == 1


def test_source_identity_content_mutation_is_not_silently_deduped(tmp_path):
    store = CampaignStore(tmp_path / "mailforge.sqlite3")
    store.register_inbound_source("source", "synthetic")
    first, duplicate = store.acquire_inbound_message(SourceMessage("source", "uid-1", b"one"))
    second, duplicate_again = store.acquire_inbound_message(
        SourceMessage("source", "uid-1", b"two")
    )
    assert duplicate is False
    assert duplicate_again is False
    assert first != second
    assert len(store.list_inbound_messages()) == 2


def test_mbox_and_simulated_imap_sources(tmp_path):
    import mailbox

    mbox_path = tmp_path / "mailbox"
    box = mailbox.mbox(mbox_path)
    box.add(_message("a@example.com", "one"))
    box.flush()
    box.close()
    assert len(list(MboxSource("mbox", mbox_path).messages())) == 1

    class Endpoint:
        uidvalidity = "1"
        messages = {"1": _message("a@example.com", "one")}

        def select(self, mailbox_name, readonly=True):
            assert readonly is True
            return self.uidvalidity, ("1",)

        def uids(self):
            return self.messages

        def fetch(self, uid):
            return self.messages[uid]

    endpoint = Endpoint()
    source = SimulatedImapSource("imap", endpoint)
    first = list(source.messages())
    assert first[0].uid == "1"
    assert list(source.messages(cursor="1:1")) == []
    endpoint.uidvalidity = "2"
    with pytest.raises(CursorInvalidated):
        list(source.messages(cursor="1:1"))
    with pytest.raises(Exception, match="disabled"):
        list(DisabledImapSource("real").messages())


def test_backup_restore_preserves_inbound_and_outbound_authority(tmp_path):
    store = CampaignStore(tmp_path / "mailforge.sqlite3")
    InboundService(store).ingest(
        SyntheticMailboxSource("synthetic", (_message("a@example.com", "one"),))
    )
    backup = store.backup_to(tmp_path / "backup.sqlite3")
    restored = CampaignStore.restore_from(backup, tmp_path / "restored.sqlite3")
    assert CampaignStore(restored).inbound_stats()["messages"] == 1


def test_pop3_uidl_adapter_is_bounded_and_never_deletes(monkeypatch):
    class FakePop3:
        commands = []

        def user(self, value):
            self.commands.append(("USER", value))

        def pass_(self, value):
            self.commands.append(("PASS", value))

        def uidl(self):
            self.commands.append(("UIDL",))
            return b"+OK", [b"1 uid-one", b"2 uid-two"], 0

        def retr(self, value):
            self.commands.append(("RETR", value))
            return b"+OK", [b"From: alice@example.com", b"", b"Hello"], 0

        def quit(self):
            self.commands.append(("QUIT",))

    fake = FakePop3()
    monkeypatch.setattr(
        "mailforge.application.inbound_sources.poplib.POP3_SSL",
        lambda *args, **kwargs: fake,
    )
    source = Pop3Source(
        "pop3-fixture",
        POP3ConnectionConfig(host="fixture", username="user", password="disposable"),
    )
    messages = list(source.messages(limit=1))
    assert [item.source_identity for item in messages] == ["uid-one"]
    assert ("RETR", 1) in fake.commands
    assert not any(command[0] == "DELE" for command in fake.commands)
