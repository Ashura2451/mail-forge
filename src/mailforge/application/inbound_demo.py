"""Deterministic inbound acceptance fixture for the CLI and operator docs."""

from __future__ import annotations

from email.message import EmailMessage, Message
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from mailforge.application.inbound import InboundService
from mailforge.application.inbound_sources import SyntheticMailboxSource
from mailforge.domain.campaign import DeliveryState
from mailforge.infrastructure.campaign_store import CampaignStore


def _dsn(outbound_id: str) -> bytes:
    outer = MIMEMultipart("report", report_type="delivery-status")
    outer["From"] = "MAILER-DAEMON@example.invalid"
    outer["To"] = "sender@example.invalid"
    outer["Subject"] = "Delivery Status Notification (Failure)"
    outer.attach(MIMEText("The message could not be delivered.", "plain"))
    machine_block = Message()
    machine_block["Reporting-MTA"] = "dns; mx.example.invalid"
    machine_block["Original-Envelope-Id"] = outbound_id
    machine_block["Arrival-Date"] = "Tue, 27 Oct 2026 12:00:00 +0000"
    recipient_block = Message()
    recipient_block["Final-Recipient"] = "rfc822; person@example.com"
    recipient_block["Action"] = "failed"
    recipient_block["Status"] = "5.1.1"
    recipient_block["Diagnostic-Code"] = "smtp; mailbox unavailable"
    report = Message()
    report.set_type("message/delivery-status")
    report.set_payload([machine_block, recipient_block])
    outer.attach(report)
    return outer.as_bytes()


def run_inbound_demo(database: str | Path) -> dict[str, object]:
    store = CampaignStore(database)
    list_id = store.create_list("inbound-demo")
    contact_id = store.upsert_contact(
        "person@example.com",
        source_file="synthetic",
        source_record_position=0,
        original_input="person@example.com",
    )
    store.add_to_list(list_id, contact_id)
    campaign_id = store.create_campaign(
        name="inbound-demo-campaign",
        description="synthetic inbound acceptance",
        sender_config_ref="synthetic-sender",
        template_ref="synthetic",
        audience_list_id=list_id,
    )
    intent_id = store.prepare_intent(
        campaign_id=campaign_id,
        contact_id=contact_id,
        normalized_email="person@example.com",
        state=DeliveryState.PREPARED,
        eligibility_reason="synthetic acceptance",
    )
    outbound_id = "<outbound-demo@mailforge.invalid>"
    store.save_prepared_message(
        intent_id=intent_id,
        template_name="synthetic",
        template_version="1",
        rendering_seed=1,
        message_id=outbound_id,
        subject="Synthetic campaign",
        from_email="sender@example.invalid",
        recipient="person@example.com",
        content_hash="synthetic",
        mime_bytes=b"Subject: Synthetic campaign\n\nbody\n",
        attachment_metadata=[],
    )
    human = EmailMessage()
    human["From"] = "person@example.com"
    human["To"] = "sender@example.invalid"
    human["Subject"] = "Re: Synthetic campaign"
    human["In-Reply-To"] = outbound_id
    human.set_content("Thanks, this is a human reply.")
    automatic = EmailMessage()
    automatic["From"] = "person@example.com"
    automatic["To"] = "sender@example.invalid"
    automatic["Subject"] = "Out of office"
    automatic["Auto-Submitted"] = "auto-replied"
    automatic.set_content("I am away.")
    complaint = EmailMessage()
    complaint["From"] = "feedback@example.invalid"
    complaint["To"] = "sender@example.invalid"
    complaint["Subject"] = "Feedback report"
    complaint.set_content("Abuse report")
    complaint.add_attachment(
        b"Feedback-Type: abuse\nOriginal-Message-ID: "
        + outbound_id.encode()
        + b"\nOriginal-Rcpt-To: rfc822; person@example.com\n",
        maintype="message",
        subtype="feedback-report",
    )
    unsubscribe = EmailMessage()
    unsubscribe["From"] = "person@example.com"
    unsubscribe["To"] = "sender@example.invalid"
    unsubscribe["Subject"] = "unsubscribe"
    unsubscribe.set_content("Please unsubscribe me")
    source = SyntheticMailboxSource(
        "inbound-demo",
        (
            human.as_bytes(),
            automatic.as_bytes(),
            _dsn(outbound_id),
            complaint.as_bytes(),
            unsubscribe.as_bytes(),
        ),
    )
    report = InboundService(store).ingest(source, trusted_synthetic=True)
    return {
        "report": report.__dict__,
        "stats": InboundService(store).stats(),
        "database": str(database),
    }
