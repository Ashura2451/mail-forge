"""One connected synthetic SMTP/IMAP lifecycle acceptance scenario."""

from __future__ import annotations

from datetime import UTC, datetime
from email.message import Message
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from mailforge.application.campaigns import CampaignService
from mailforge.application.inbound import InboundService
from mailforge.application.outbound import OutboundService
from mailforge.application.providers import ProviderService
from mailforge.application.reporting import ReportFilters, ReportingService
from mailforge.domain.campaign import ContactCandidate, DeliveryState
from mailforge.domain.outbound import SenderPolicy, TemplateDefinition
from mailforge.domain.provider import ProviderKind, ProviderProfile, TLSMode
from mailforge.infrastructure.campaign_store import CampaignStore
from mailforge.infrastructure.credentials import MappingCredentialResolver
from mailforge.infrastructure.smtp import LiveSendGate
from tests.test_provider_interoperability import (
    _certificate,
    _IMAPServer,
    _ServerContext,
    _SMTPServer,
)


def _dsn(message_id: str) -> bytes:
    outer = MIMEMultipart("report", report_type="delivery-status")
    outer["From"] = "MAILER-DAEMON@example.invalid"
    outer["To"] = "sender@example.com"
    outer["Subject"] = "Delivery Status Notification"
    outer.attach(MIMEText("The local fixture rejected the recipient.", "plain"))
    machine = Message()
    machine["Original-Envelope-Id"] = message_id
    recipient = Message()
    recipient["Final-Recipient"] = "rfc822; recipient@example.com"
    recipient["Action"] = "failed"
    recipient["Status"] = "5.1.1"
    report = Message()
    report.set_type("message/delivery-status")
    report.set_payload([machine, recipient])
    outer.attach(report)
    return outer.as_bytes()


def test_unified_local_smtp_imap_recovery_and_policy_lifecycle(tmp_path):
    cert, key = _certificate(tmp_path)
    smtp = _SMTPServer(cert, key)
    imap_messages: dict[int, bytes] = {}
    imap = _IMAPServer(cert, key, imap_messages)
    database = tmp_path / "unified-acceptance.sqlite3"
    resolver = MappingCredentialResolver(
        {"test:smtp": "synthetic-smtp-secret", "test:imap": "synthetic-imap-secret"}
    )
    store = CampaignStore(database)
    campaigns = CampaignService(store)
    outbound = OutboundService(store)
    providers = ProviderService(store)
    sender = SenderPolicy(
        ref="loopback-policy",
        provider="loopback",
        email="sender@example.com",
        max_per_minute=20,
        max_per_hour=20,
        max_per_day=20,
        per_domain_per_minute=20,
    )
    outbound.register_sender_policy(sender)
    list_id = store.create_list("unified-audience")
    campaigns.import_candidates(
        [
            ContactCandidate(
                "recipient@example.com", "recipient@example.com", "synthetic.csv", 1, {}
            )
        ],
        list_id,
    )
    campaign_id = campaigns.create_campaign(
        "unified-campaign",
        list_id,
        template_ref="unified-template",
        sender_config_ref=sender.ref,
        configuration={"communication_purpose": "transactional"},
    )
    store.register_provider_profile(
        ProviderProfile(
            ref="loopback-smtp",
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
    store.register_provider_profile(
        ProviderProfile(
            ref="loopback-imap",
            kind=ProviderKind.IMAP,
            host="localhost",
            port=imap.server_address[1],
            username="user",
            credential_ref="test:imap",
            tls_mode=TLSMode.IMPLICIT,
            ca_file=str(cert),
        )
    )

    with _ServerContext(smtp), _ServerContext(imap):
        assert providers.local_protocol_diagnostic("loopback-smtp", resolver)["status"] == (
            "local_protocol_passed"
        )
        assert providers.local_protocol_diagnostic("loopback-imap", resolver)["status"] == (
            "local_protocol_passed"
        )
        prepared = outbound.prepare_campaign(
            campaign_id,
            template=TemplateDefinition("unified-template", "1", "Unified", "Hello {{ email }}"),
            sender=sender,
        )
        assert prepared.prepared == 1
        outbound.authorize_simulation(campaign_id)
        report, _ = outbound.execute_provider(
            campaign_id, "loopback-smtp", resolver, gate=LiveSendGate(enabled=True)
        )
        assert report.counts[DeliveryState.ACCEPTED.value] == 1
        message_id = store.get_prepared_message(store.list_intents(campaign_id)[0]["id"])[
            "message_id"
        ]
        imap.messages[1] = _dsn(message_id)
        inbound = InboundService(store)
        acquired = inbound.ingest_provider("loopback-imap", resolver)
        assert acquired.acquired == 1
        review = inbound.review_queue()
        assert review[0]["action_type"] == "create_hard_bounce"
        inbound.decide(review[0]["action_id"], "approve", "verified synthetic DSN")
        assert inbound.review_queue() == []
        assert campaigns.inspect_eligibility(campaign_id)[0]["status"] == "excluded"

    second_list = store.create_list("ambiguous-audience")
    campaigns.import_candidates(
        [ContactCandidate("ambiguous@example.com", "ambiguous@example.com", "synthetic", 1, {})],
        second_list,
    )
    second_campaign = campaigns.create_campaign(
        "ambiguous-campaign",
        second_list,
        sender_config_ref=sender.ref,
        configuration={"communication_purpose": "transactional"},
    )
    outbound.prepare_campaign(
        second_campaign,
        template=TemplateDefinition("unified-template", "1", "Unified", "Hello {{ email }}"),
        sender=sender,
    )
    outbound.authorize_simulation(second_campaign)
    ambiguous = _SMTPServer(cert, key, disconnect_after_data=True)
    store.register_provider_profile(
        ProviderProfile(
            ref="loopback-ambiguous",
            kind=ProviderKind.SMTP,
            host="localhost",
            port=ambiguous.server_address[1],
            username="user",
            credential_ref="test:smtp",
            tls_mode=TLSMode.STARTTLS,
            sender_policy_ref=sender.ref,
            ca_file=str(cert),
        )
    )
    with _ServerContext(ambiguous):
        ambiguous_report, _ = outbound.execute_provider(
            second_campaign, "loopback-ambiguous", resolver, gate=LiveSendGate(enabled=True)
        )
        assert ambiguous_report.counts["ambiguous"] == 1
        assert ambiguous_report.campaign_status == "paused"
        command_count = len(ambiguous.commands)
        resumed_report, _ = outbound.execute_provider(
            second_campaign,
            "loopback-ambiguous",
            resolver,
            gate=LiveSendGate(enabled=True),
            retry_deferred=True,
        )
        assert resumed_report.counts["ambiguous"] == 1
        assert len(ambiguous.commands) == command_count

    report = ReportingService(store, now_fn=lambda: datetime.now(UTC)).summary(
        ReportFilters(preset="last_30_days")
    )
    assert report["metrics"]["campaigns"]["attempts"]["accepted"] >= 1
    assert report["metrics"]["campaigns"]["attempts"]["ambiguous"] >= 1
    assert report["metrics"]["inbound"]["messages"] >= 1

    backup = store.backup_to(tmp_path / "unified-backup.sqlite3")
    restored = CampaignStore.restore_from(backup, tmp_path / "unified-restored.sqlite3")
    assert CampaignStore(restored).integrity_check()
