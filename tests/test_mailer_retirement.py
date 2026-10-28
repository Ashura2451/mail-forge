import json
import smtplib
import subprocess
import sys
from datetime import UTC, datetime
from email.message import Message
from importlib.metadata import files as installed_files

import pytest

from mailforge.application.campaigns import (
    CampaignService,
    run_synthetic_demo,
)
from mailforge.application.inbound import InboundService
from mailforge.application.inbound_demo import run_inbound_demo
from mailforge.application.inbound_sources import SyntheticMailboxSource
from mailforge.application.mailer_compat import (
    load_legacy_configuration,
    load_legacy_targets,
    migrate_legacy_state,
    prepare_legacy_campaign,
)
from mailforge.application.outbound import OutboundService, PreparedSMTPTransport
from mailforge.application.policy import PolicyEngine
from mailforge.domain.campaign import AttemptOutcome, DeliveryState
from mailforge.domain.outbound import (
    AttachmentSpec,
    ScheduleMode,
    ScheduleSpec,
    SenderPolicy,
)
from mailforge.infrastructure.campaign_store import CampaignStore
from mailforge.infrastructure.smtp import (
    LiveSendAuthorizationRequired,
    LiveSendGate,
    SMTPConnectionPool,
    SMTPPolicyViolation,
    SMTPTransportConfig,
)


def _legacy_fixture(tmp_path):
    (tmp_path / "body.html").write_text(
        "<p>Hello {{ target.name }} ({{ target.email }})</p>", encoding="utf-8"
    )
    (tmp_path / "targets.csv").write_text(
        "email,name\nAlice@Example.com,Alice\nalice@example.com,Alice again\ninvalid,Ada\n",
        encoding="utf-8",
    )
    config = tmp_path / "config.yaml"
    config.write_text(
        "campaigns:\n"
        "  - name: invoice\n"
        "    template: body.html\n"
        "    subject: Invoice\n"
        "    from_name: Billing\n"
        "smtp_pool:\n"
        "  - name: primary\n"
        "    email: ${SENDER_EMAIL}\n"
        "    password: ${SENDER_PASSWORD}\n"
        "    server: smtp.example.invalid\n"
        "    port: 587\n"
        "targets:\n  file: targets.csv\n"
        "tracking:\n  enabled: true\n  open_tracking: false\n"
        "rate_limiting:\n  max_per_minute: 12\n"
        "domain_throttle:\n  max_per_domain_per_hour: 4\n"
        "  max_per_domain_per_minute: 1\n"
        "sending_window:\n  enabled: false\n"
        "files:\n  sent_file: sent.txt\n  failed_file: failed.txt\n  bounce_file: bounces.txt\n",
        encoding="utf-8",
    )
    return config


def test_legacy_translation_is_report_only_and_does_not_expose_password(tmp_path):
    config = _legacy_fixture(tmp_path)
    translated = load_legacy_configuration(
        config,
        environment={"SENDER_EMAIL": "sender@example.invalid", "SENDER_PASSWORD": "secret"},
    )
    assert translated.campaigns[0].name == "invoice"
    assert translated.smtp_accounts[0].email == "sender@example.invalid"
    assert translated.smtp_accounts[0].password == "secret"
    assert "tracking.open_tracking" in translated.migration.deprecated[0]
    assert "secret" not in json.dumps(translated.to_dict())
    translated.policies["advanced"]["password"] = "secret"
    assert translated.to_dict()["policies"]["advanced"]["password"] == "[REDACTED]"
    assert translated.paths.target_file == tmp_path / "targets.csv"

    candidates, report = load_legacy_targets(translated)
    assert [item.normalized_email for item in candidates] == ["alice@example.com"]
    assert report.duplicates == 1
    assert len(report.rejected) == 1


def test_legacy_campaign_prepares_through_canonical_services(tmp_path):
    config = _legacy_fixture(tmp_path)
    translated = load_legacy_configuration(
        config,
        environment={"SENDER_EMAIL": "sender@example.invalid", "SENDER_PASSWORD": "secret"},
    )
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    campaign_id, report = prepare_legacy_campaign(
        store, translated, campaign_name="invoice", sender_email="sender@example.com"
    )
    campaign = store.get_campaign(campaign_id)
    assert campaign["status"] == "ready"
    assert report.unique == 1
    prepared = [
        store.get_prepared_message(intent["id"])
        for intent in store.list_intents(campaign_id)
        if store.get_prepared_message(intent["id"]) is not None
    ]
    assert len(prepared) == 1
    assert b"Alice" in prepared[0]["mime_bytes"]


def test_legacy_state_migration_is_idempotent_and_preserves_ambiguity(tmp_path):
    store = CampaignStore(tmp_path / "state.sqlite3")
    list_id = store.create_list("legacy", "synthetic")
    campaign_id = store.create_campaign(
        name="legacy",
        description="synthetic",
        sender_config_ref="sender",
        template_ref="template",
        audience_list_id=list_id,
    )
    for name, content in {
        "sent.txt": "uncertain@example.com\nuncertain@example.com\n",
        "failed.txt": "uncertain@example.com # temporary\nretry@example.com # temporary\n",
        "bounces.txt": "blocked@example.com # hard bounce\n",
        "events.json": json.dumps({"type": "legacy_open"}),
        "campaign.log": "synthetic log\n",
    }.items():
        (tmp_path / name).write_text(content, encoding="utf-8")
    first = migrate_legacy_state(
        store,
        list_id=list_id,
        campaign_id=campaign_id,
        sent_path=tmp_path / "sent.txt",
        failed_path=tmp_path / "failed.txt",
        hard_bounces_path=tmp_path / "bounces.txt",
        events_path=tmp_path / "events.json",
        log_path=tmp_path / "campaign.log",
    )
    second = migrate_legacy_state(
        store,
        list_id=list_id,
        campaign_id=campaign_id,
        sent_path=tmp_path / "sent.txt",
        failed_path=tmp_path / "failed.txt",
        hard_bounces_path=tmp_path / "bounces.txt",
        events_path=tmp_path / "events.json",
        log_path=tmp_path / "campaign.log",
    )
    assert first.counts == {
        "sent_ambiguous": 1,
        "failed_deferred": 1,
        "hard_bounces": 1,
        "events_imported": 1,
    }
    assert first.duplicates == 2
    assert first.conflicts == 1
    (tmp_path / "late-failed.txt").write_text(
        "uncertain@example.com # later failure\n", encoding="utf-8"
    )
    late = migrate_legacy_state(
        store,
        list_id=list_id,
        campaign_id=campaign_id,
        sent_path=tmp_path / "missing-sent.txt",
        failed_path=tmp_path / "late-failed.txt",
        hard_bounces_path=tmp_path / "missing-bounces.txt",
    )
    assert late.already_present == 1
    assert second.already_present == 3
    assert second.counts["events_imported"] == 0
    assert first.logs_preserved
    with store.connect() as connection:
        event = connection.execute(
            "SELECT payload_json FROM operational_events WHERE event_type = 'legacy_event_imported'"
        ).fetchone()
        contact_events = connection.execute(
            "SELECT COUNT(*) AS count FROM operational_events WHERE event_type = 'contact_imported'"
        ).fetchone()["count"]
    assert json.loads(event["payload_json"])["legacy_record"]["type"] == "legacy_open"
    assert contact_events == 4
    assert any(
        row["state"] == DeliveryState.AMBIGUOUS.value for row in store.list_intents(campaign_id)
    )


class _FakeClient:
    def __init__(self):
        self.calls = []

    def ehlo(self):
        self.calls.append("ehlo")

    def starttls(self, *, context):
        self.calls.append("starttls")

    def login(self, username, password):
        self.calls.append(("login", username, password))

    def sendmail(self, sender, recipients, message):
        self.calls.append(("sendmail", sender, recipients, message))
        return {}

    def noop(self):
        self.calls.append("noop")
        return (250, b"OK")

    def quit(self):
        self.calls.append("quit")


class _OutcomeClient(_FakeClient):
    def sendmail(self, sender, recipients, message):
        super().sendmail(sender, recipients, message)
        recipient = recipients[0]
        if recipient == "temp@example.com":
            raise smtplib.SMTPDataError(451, b"try later")
        if recipient == "perm@example.com":
            raise smtplib.SMTPDataError(550, b"rejected")
        if recipient == "ambiguous@example.com":
            raise ConnectionError("connection lost after DATA")
        return {}


def test_smtp_pool_is_gated_reuses_sessions_and_enforces_sender_policy():
    clients = []

    def factory(_config):
        client = _FakeClient()
        clients.append(client)
        return client

    config = SMTPTransportConfig(
        host="smtp.example.invalid",
        port=587,
        username="user",
        password="secret",
        allowed_sender_domains=("example.com",),
        rotation_weight=2,
    )
    pool = SMTPConnectionPool([config], gate=LiveSendGate(enabled=True), connector_factory=factory)
    assert pool.sendmail("sender@example.com", "one@example.net", "body") == {}
    assert pool.sendmail("sender@example.com", "two@example.net", "body") == {}
    assert len(clients) == 1
    assert pool.health_check()["smtp.example.invalid"]
    with pytest.raises(SMTPPolicyViolation):
        pool.sendmail("sender@other.invalid", "three@example.net", "body")
    pool.close()
    assert clients[0].calls[-1] == "quit"


def test_prepared_smtp_execution_uses_policy_and_two_independent_gates(tmp_path):
    config = _legacy_fixture(tmp_path)
    translated = load_legacy_configuration(
        config,
        environment={"SENDER_EMAIL": "sender@example.invalid", "SENDER_PASSWORD": "secret"},
    )
    translated.policies["domain_throttle"].pop("max_per_domain_per_hour")
    store = CampaignStore(tmp_path / "smtp.sqlite3")
    campaign_id, _ = prepare_legacy_campaign(
        store, translated, campaign_name="invoice", sender_email="sender@example.com"
    )
    service = OutboundService(store)
    service.authorize_simulation(campaign_id)
    client = _FakeClient()
    pool = SMTPConnectionPool(
        [
            SMTPTransportConfig(
                host="smtp.example.invalid",
                port=587,
                allowed_sender_domains=("example.com",),
            )
        ],
        gate=LiveSendGate(enabled=True),
        connector_factory=lambda _config: client,
    )
    with pytest.raises(PermissionError, match="policy guard"):
        service.campaigns.execute(campaign_id, PreparedSMTPTransport(store, pool))
    report, _ = service.execute_smtp(campaign_id, pool)
    assert report.counts["accepted"] == 1
    assert any(call[0] == "sendmail" for call in client.calls if isinstance(call, tuple))


def test_smtp_pool_does_not_create_a_session_without_authorization():
    def factory(_config):
        raise AssertionError("connector must not run")

    pool = SMTPConnectionPool(
        [SMTPTransportConfig(host="smtp.example.invalid", port=587)],
        gate=LiveSendGate(),
        connector_factory=factory,
    )
    with pytest.raises(LiveSendAuthorizationRequired):
        pool.sendmail("sender@example.com", "recipient@example.com", "body")


def test_campaign_pacing_reservation_survives_a_new_store_instance(tmp_path):
    database = tmp_path / "pacing.sqlite3"
    store = CampaignStore(database)
    list_id = store.create_list("pacing", "synthetic")
    campaign_id = store.create_campaign(
        name="pacing",
        description="synthetic",
        sender_config_ref="sender",
        template_ref="template",
        audience_list_id=list_id,
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert store.reserve_campaign_pacing(campaign_id, now=now, interval_seconds=60)[0]
    allowed, retry_at = CampaignStore(database).reserve_campaign_pacing(
        campaign_id, now=now, interval_seconds=60
    )
    assert not allowed
    assert retry_at is not None


def test_legacy_hourly_domain_limit_is_enforced_after_reload(tmp_path):
    database = tmp_path / "hourly.sqlite3"
    store = CampaignStore(database)
    list_id = store.create_list("hourly", "synthetic")
    for position, email in enumerate(("one@example.com", "two@example.com"), 1):
        contact_id = store.upsert_contact(
            email,
            source_file="synthetic",
            source_record_position=position,
            original_input=email,
        )
        store.add_to_list(list_id, contact_id)
    campaign_id = store.create_campaign(
        name="hourly",
        description="synthetic",
        sender_config_ref="legacy-sender",
        template_ref="template",
        audience_list_id=list_id,
    )
    store.register_sender_policy(
        SenderPolicy(
            ref="legacy-sender",
            provider="synthetic",
            email="sender@example.com",
            max_per_minute=60,
            max_per_hour=60,
            max_per_day=60,
            per_domain_per_minute=60,
            per_domain_per_hour=1,
            max_concurrency=2,
        )
    )
    CampaignService(store).prepare(campaign_id)
    first = store.reserve_next(campaign_id, worker_id="one")
    second = store.reserve_next(campaign_id, worker_id="two")
    engine = PolicyEngine(CampaignStore(database))
    assert engine.reserve(
        attempt_id=first["attempt_id"],
        intent_id=first["id"],
        recipient=first["normalized_email"],
        preferred_ref="legacy-sender",
    ).allowed
    blocked = engine.reserve(
        attempt_id=second["attempt_id"],
        intent_id=second["id"],
        recipient=second["normalized_email"],
        preferred_ref="legacy-sender",
    )
    assert not blocked.allowed
    assert blocked.reason == "sender policy unavailable: legacy-sender"


def test_legacy_warmup_adaptive_and_reputation_controls_are_enforced(tmp_path):
    store = CampaignStore(tmp_path / "legacy-controls.sqlite3")
    now = datetime.now(UTC)

    def make_campaign(ref, emails, configuration):
        list_id = store.create_list(ref, "synthetic")
        for position, email in enumerate(emails, 1):
            contact_id = store.upsert_contact(
                email,
                source_file="synthetic",
                source_record_position=position,
                original_input=email,
            )
            store.add_to_list(list_id, contact_id)
        store.register_sender_policy(
            SenderPolicy(
                ref=ref,
                provider="synthetic",
                email="sender@example.com",
                max_per_minute=60,
                max_per_hour=60,
                max_per_day=10,
                per_domain_per_minute=60,
                per_domain_per_hour=60,
                max_concurrency=2,
            ),
            configuration,
        )
        campaign_id = CampaignService(store).create_campaign(ref, list_id, sender_config_ref=ref)
        CampaignService(store).prepare(campaign_id)
        return campaign_id

    def reserve(campaign_id, outcome=None):
        intent = store.reserve_next(campaign_id, worker_id="controls")
        decision = PolicyEngine(store).reserve(
            attempt_id=intent["attempt_id"],
            intent_id=intent["id"],
            recipient=intent["normalized_email"],
            preferred_ref=store.get_campaign(campaign_id)["sender_config_ref"],
        )
        if decision.allowed and outcome is not None:
            store.mark_attempt_started(intent["id"], intent["attempt_id"])
            store.record_delivery_result(
                intent_id=intent["id"],
                attempt_id=intent["attempt_id"],
                result=outcome,
            )
        return decision

    warmup = make_campaign(
        "warmup",
        ("one@warmup.example", "two@warmup.example"),
        {"warmup": {"enabled": True, "ramp_days": 10, "started_at": now.date().isoformat()}},
    )
    warmup_first = store.reserve_next(warmup, worker_id="warmup-one")
    warmup_policy = PolicyEngine(store)
    assert warmup_policy.reserve(
        attempt_id=warmup_first["attempt_id"],
        intent_id=warmup_first["id"],
        recipient=warmup_first["normalized_email"],
        preferred_ref="warmup",
    ).allowed
    warmup_second = store.reserve_next(warmup, worker_id="warmup-two")
    warmup_decision = warmup_policy.reserve(
        attempt_id=warmup_second["attempt_id"],
        intent_id=warmup_second["id"],
        recipient=warmup_second["normalized_email"],
        preferred_ref="warmup",
    )
    assert not warmup_decision.allowed
    assert warmup_decision.reason == "sender policy unavailable: warmup"

    adaptive = make_campaign(
        "adaptive",
        ("one@adaptive.example", "two@adaptive.example"),
        {
            "adaptive_domain_throttle": {
                "enabled": True,
                "bounce_rate_threshold": 0.5,
                "min_samples": 1,
                "cooldown_minutes": 60,
            }
        },
    )
    assert reserve(adaptive, AttemptOutcome.PERMANENT_FAILURE).allowed
    assert reserve(adaptive).reason == "sender policy unavailable: adaptive"

    reputation = make_campaign(
        "reputation",
        ("one@reputation.example", "two@other.example"),
        {
            "smtp_reputation": {
                "enabled": True,
                "failure_rate_threshold": 0.5,
                "min_samples": 1,
                "cooldown_minutes": 60,
            }
        },
    )
    assert reserve(reputation, AttemptOutcome.PERMANENT_FAILURE).allowed
    assert reserve(reputation).reason == "sender policy unavailable: reputation"


def test_mailer_retirement_synthetic_end_to_end_backup_repeat_and_inbound(tmp_path):
    config = _legacy_fixture(tmp_path)
    with (tmp_path / "targets.csv").open("a", encoding="utf-8") as targets:
        targets.write(
            "bob@example.com,Bob\n"
            "temp@example.com,Temporary\n"
            "perm@example.com,Permanent\n"
            "ambiguous@example.com,Ambiguous\n"
        )
    translated = load_legacy_configuration(
        config,
        environment={"SENDER_EMAIL": "sender@example.invalid", "SENDER_PASSWORD": "secret"},
    )
    translated.policies["domain_throttle"]["max_per_domain_per_minute"] = 60
    translated.policies["domain_throttle"]["max_per_domain_per_hour"] = 60
    database = tmp_path / "e2e.sqlite3"
    store = CampaignStore(database)
    attachment_path = tmp_path / "invoice.txt"
    attachment_path.write_text("synthetic invoice", encoding="utf-8")
    campaign_id, _ = prepare_legacy_campaign(
        store,
        translated,
        campaign_name="invoice",
        sender_email="sender@example.com",
        attachments=(AttachmentSpec(str(attachment_path)),),
        asset_root=tmp_path,
    )
    store.upsert_schedule(
        campaign_id,
        ScheduleSpec(mode=ScheduleMode.IMMEDIATE, pacing_seconds=0),
    )
    service = OutboundService(store)
    service.authorize_simulation(campaign_id)
    client = _OutcomeClient()
    pool = SMTPConnectionPool(
        [
            SMTPTransportConfig(
                host="smtp.example.invalid",
                port=587,
                allowed_sender_domains=("example.com",),
            )
        ],
        gate=LiveSendGate(enabled=True),
        connector_factory=lambda _config: client,
    )
    first, _ = service.execute_smtp(campaign_id, pool)
    assert first.counts["accepted"] == 2
    assert first.counts["deferred"] == 1
    assert first.counts["permanent_failure"] == 1
    assert first.counts["ambiguous"] == 1
    assert first.campaign_status == "paused"
    attachment_message = store.get_prepared_message(store.list_intents(campaign_id)[0]["id"])
    assert json.loads(attachment_message["attachment_metadata_json"])
    assert b"invoice.txt" in attachment_message["mime_bytes"]
    installed = installed_files("mailforge") or ()
    assert not any(
        any(part in {"MAILER", "MSORTER", "SORTER"} for part in path.parts) and path.suffix == ".py"
        for path in installed
    )
    installed_cli = subprocess.run(
        [
            sys.executable,
            "-c",
            "from mailforge.interfaces.cli import main; main(['--help'])",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "campaign" in installed_cli.stdout
    prepared = next(
        store.get_prepared_message(intent["id"])
        for intent in store.list_intents(campaign_id)
        if store.get_prepared_message(intent["id"]) is not None
    )
    outbound_id = prepared["message_id"]
    outer = Message()
    outer.set_type("multipart/report")
    outer.set_param("report-type", "delivery-status")
    machine = Message()
    machine["Original-Envelope-Id"] = outbound_id
    recipient = Message()
    recipient["Final-Recipient"] = "rfc822; alice@example.com"
    recipient["Action"] = "failed"
    recipient["Status"] = "5.1.1"
    report_part = Message()
    report_part.set_type("message/delivery-status")
    report_part.set_payload([machine, recipient])
    outer.set_payload([report_part])
    inbound_report = InboundService(store).ingest(
        SyntheticMailboxSource("synthetic", (outer.as_bytes(),)), trusted_synthetic=True
    )
    assert inbound_report.applied_actions == 1
    state_list = store.create_list("legacy-state", "synthetic")
    state_campaign = store.create_campaign(
        name="legacy-state",
        description="synthetic state rehearsal",
        sender_config_ref="sender",
        template_ref="template",
        audience_list_id=state_list,
    )
    for filename, content in {
        "sent-history.txt": "uncertain@example.com\n",
        "failed-history.txt": "retry@example.com\n",
        "bounce-history.txt": "blocked@example.com\n",
    }.items():
        (tmp_path / filename).write_text(content, encoding="utf-8")
    state_report = migrate_legacy_state(
        store,
        list_id=state_list,
        campaign_id=state_campaign,
        sent_path=tmp_path / "sent-history.txt",
        failed_path=tmp_path / "failed-history.txt",
        hard_bounces_path=tmp_path / "bounce-history.txt",
    )
    assert state_report.ambiguous == 1
    crashed_intent = store.reserve_next(
        campaign_id, retry_deferred=True, worker_id="synthetic-crash"
    )
    assert crashed_intent is not None
    assert store.mark_attempt_started(crashed_intent["id"], crashed_intent["attempt_id"])
    pool.close()
    restarted_store = CampaignStore(database)
    recovery = restarted_store.recover_inflight(campaign_id)
    assert recovery["ambiguous"] == 1
    restarted_service = OutboundService(restarted_store)
    resumed_client = _OutcomeClient()
    resumed_pool = SMTPConnectionPool(
        [
            SMTPTransportConfig(
                host="smtp.example.invalid",
                port=587,
                allowed_sender_domains=("example.com",),
            )
        ],
        gate=LiveSendGate(enabled=True),
        connector_factory=lambda _config: resumed_client,
    )
    second, _ = restarted_service.execute_smtp(campaign_id, resumed_pool, retry_deferred=True)
    assert second.campaign_status == "paused"
    assert len([item for item in client.calls if isinstance(item, tuple)]) == 5
    assert len([item for item in resumed_client.calls if isinstance(item, tuple)]) == 0
    backup = restarted_store.backup_to(tmp_path / "e2e-backup.sqlite3")
    restored = CampaignStore.restore_from(backup, tmp_path / "e2e-restored.sqlite3")
    assert CampaignStore(restored).integrity_check()
    inbound = run_inbound_demo(tmp_path / "inbound.sqlite3")
    assert inbound["stats"]["messages"] >= 1
    synthetic = run_synthetic_demo(tmp_path / "synthetic-core.sqlite3")
    assert synthetic["final"]["campaign_status"] in {"completed", "paused"}
