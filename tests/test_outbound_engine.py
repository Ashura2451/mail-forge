import json
import sqlite3
from datetime import UTC, datetime, timedelta
from email.parser import BytesParser
from email.policy import SMTP
from pathlib import Path
from threading import Lock

import pytest

from mailforge.application.composition import (
    AssetResolver,
    MessageComposer,
    MessagePreparationError,
    UnsafeAssetError,
)
from mailforge.application.outbound import OutboundService
from mailforge.application.scheduling import ScheduleEngine, ScheduleError, resolve_local_time
from mailforge.application.templates import SafeTemplateRenderer, TemplateRenderError
from mailforge.domain.campaign import (
    AttemptOutcome,
    ContactCandidate,
    DeliveryState,
    SuppressionCategory,
)
from mailforge.domain.outbound import (
    AttachmentSpec,
    CatchUpPolicy,
    ScheduleMode,
    ScheduleSpec,
    SenderPolicy,
    TemplateDefinition,
)
from mailforge.infrastructure.campaign_store import SCHEMA_SQL, CampaignStore


def _sender(**overrides) -> SenderPolicy:
    values = {
        "ref": "sender-policy",
        "provider": "synthetic",
        "email": "sender@example.com",
        "display_name": "Mailforge",
        "max_per_minute": 100,
        "max_per_hour": 100,
        "max_per_day": 100,
        "per_domain_per_minute": 100,
    }
    values.update(overrides)
    return SenderPolicy(**values)


def _template(**overrides) -> TemplateDefinition:
    values = {
        "name": "welcome",
        "version": "1",
        "subject": "Hello {{ name }} {there|friend}",
        "text": "Welcome {{ name }} to Mailforge.",
        "html": "<p>Welcome {{ name }}.</p>",
    }
    values.update(overrides)
    return TemplateDefinition(**values)


def _campaign(tmp_path: Path, *emails: str):
    store = CampaignStore(tmp_path / "state.sqlite3")
    service = OutboundService(store)
    list_id = store.create_list("audience")
    service.campaigns.import_candidates(
        [
            ContactCandidate(email, email, "contacts.txt", index, {"name": email.split("@")[0]})
            for index, email in enumerate(emails, 1)
        ],
        list_id,
    )
    campaign_id = service.campaigns.create_campaign(
        "campaign", list_id, template_ref="welcome", sender_config_ref="sender-policy"
    )
    return store, service, campaign_id


def test_template_renderer_is_deterministic_escaped_and_sandboxed():
    renderer = SafeTemplateRenderer()
    definition = _template(
        subject="{one|two} {{ name }}",
        text="{first|second} {random_int} {date} {time}",
        html="<p>{{ name }}</p>",
    )
    now = datetime(2026, 10, 27, 15, 4, tzinfo=UTC)
    first = renderer.render(definition, {"name": "<Ada>"}, seed=42, now=now)
    second = renderer.render(definition, {"name": "<Ada>"}, seed=42, now=now)

    assert first == second
    assert "&lt;Ada&gt;" in first.html
    assert "2026-10-27" in first.text
    with pytest.raises(TemplateRenderError):
        renderer.render(_template(text="{{ missing }}"), {})
    with pytest.raises(TemplateRenderError):
        renderer.render(_template(text="{{ self.__class__ }}"), {})


def test_template_renderer_bounds_context_and_streamed_output():
    renderer = SafeTemplateRenderer(max_context_items=3, max_context_depth=2)
    with pytest.raises(TemplateRenderError, match="item limit"):
        renderer.render(_template(text="{{ values|length }}"), {"values": [1, 2, 3, 4]})
    with pytest.raises(TemplateRenderError, match="depth limit"):
        SafeTemplateRenderer(max_context_items=10, max_context_depth=2).render(
            _template(text="{{ nested.value }}"), {"nested": {"value": {"x": 1}}}
        )
    with pytest.raises(TemplateRenderError, match="output exceeds"):
        SafeTemplateRenderer(max_context_items=10).render(
            _template(
                text="{% for value in values %}{{ value }}{% endfor %}", max_rendered_bytes=3
            ),
            {"values": ["aa", "bb"]},
        )


def test_mime_composition_has_alternative_inline_and_regular_attachment(tmp_path):
    asset = tmp_path / "notice.txt"
    asset.write_text("notice", encoding="utf-8")
    rendered = SafeTemplateRenderer().render(_template(), {"name": "Ada"}, seed=3)
    attachments = (
        AssetResolver(tmp_path).resolve(AttachmentSpec("notice.txt", content_id="notice")),
        AssetResolver(tmp_path).resolve(AttachmentSpec("notice.txt", filename="copy.txt")),
    )
    composed = MessageComposer().compose(
        sender=_sender(reply_to="reply@example.com"),
        recipient="ada@example.com",
        rendered=rendered,
        attachments=attachments,
        unsubscribe_url="https://example.com/unsubscribe/ada",
    )
    message = BytesParser(policy=SMTP).parsebytes(composed.serialized)
    assert message["Message-ID"] == composed.message_id
    assert message["Reply-To"] == "reply@example.com"
    assert message["List-Unsubscribe"] == "<https://example.com/unsubscribe/ada>"
    parts = list(message.walk())
    assert any(part.get_content_type() == "text/plain" for part in parts)
    assert any(part.get_content_type() == "text/html" for part in parts)
    assert any(part["Content-ID"] == "<notice>" for part in parts)
    assert any(part.get_filename() == "copy.txt" for part in parts)

    with pytest.raises(MessagePreparationError, match="header injection"):
        MessageComposer().compose(
            sender=_sender(),
            recipient="ada@example.com",
            rendered=_template_rendered(),
        )


def _template_rendered():
    from mailforge.application.templates import RenderedTemplate

    return RenderedTemplate("subject\nInjected", "text", None)


def test_one_click_unsubscribe_requires_explicit_operational_prerequisites():
    rendered = SafeTemplateRenderer().render(_template(), {"name": "Ada"})
    composer = MessageComposer()
    with pytest.raises(MessagePreparationError, match="HTTPS"):
        composer.compose(
            sender=_sender(),
            recipient="ada@example.com",
            rendered=rendered,
            unsubscribe_url="http://example.com/unsubscribe",
            one_click_unsubscribe=True,
            dkim_signed=True,
        )
    with pytest.raises(MessagePreparationError, match="DKIM"):
        composer.compose(
            sender=_sender(),
            recipient="ada@example.com",
            rendered=rendered,
            unsubscribe_url="https://example.com/unsubscribe",
            one_click_unsubscribe=True,
        )
    message = BytesParser(policy=SMTP).parsebytes(
        composer.compose(
            sender=_sender(),
            recipient="ada@example.com",
            rendered=rendered,
            unsubscribe_url="https://example.com/unsubscribe",
            one_click_unsubscribe=True,
            dkim_signed=True,
        ).serialized
    )
    assert message["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


def test_asset_resolver_confines_paths_and_enforces_limits(tmp_path):
    asset = tmp_path / "asset.bin"
    asset.write_bytes(b"asset")
    resolver = AssetResolver(tmp_path, max_bytes=5)
    assert resolver.resolve(AttachmentSpec("asset.bin")).data == b"asset"
    with pytest.raises(UnsafeAssetError):
        resolver.resolve(AttachmentSpec("../outside.bin"))
    asset.write_bytes(b"oversize")
    with pytest.raises(MessagePreparationError, match="size limit"):
        resolver.resolve(AttachmentSpec("asset.bin"))


def test_schedule_engine_rejects_dst_gap_requires_fold_and_uses_utc():
    with pytest.raises(ScheduleError, match="does not exist"):
        resolve_local_time("2026-03-08T02:30:00", "America/New_York")
    with pytest.raises(ScheduleError, match="fold"):
        resolve_local_time("2026-11-01T01:30:00", "America/New_York")
    first = resolve_local_time("2026-11-01T01:30:00", "America/New_York", fold=0)
    second = resolve_local_time("2026-11-01T01:30:00", "America/New_York", fold=1)
    assert second - first == timedelta(hours=1)
    engine = ScheduleEngine()
    schedule = ScheduleSpec(
        mode=ScheduleMode.WINDOW,
        timezone="America/New_York",
        window_start="09:00",
        window_end="17:00",
        weekdays=(0, 1, 2, 3, 4),
        catch_up_policy=CatchUpPolicy.NEXT_WINDOW,
    )
    assert engine.due(schedule, datetime(2026, 10, 27, 15, 0, tzinfo=UTC))
    assert not engine.due(schedule, datetime(2026, 10, 27, 22, 0, tzinfo=UTC))


def test_recurring_schedule_advances_daily_and_weekly_occurrences():
    engine = ScheduleEngine()
    daily = ScheduleSpec(
        mode=ScheduleMode.RECURRING,
        timezone="UTC",
        scheduled_at="2026-10-27T09:00:00",
        recurrence="daily",
        catch_up_policy=CatchUpPolicy.NEXT_WINDOW,
    )
    weekly = ScheduleSpec(
        mode=ScheduleMode.RECURRING,
        timezone="UTC",
        scheduled_at="2026-10-27T09:00:00",
        recurrence="weekly",
        catch_up_policy=CatchUpPolicy.NEXT_WINDOW,
    )
    after_daily = datetime(2026, 10, 28, 10, 0, tzinfo=UTC)
    after_weekly = datetime(2026, 11, 3, 10, 0, tzinfo=UTC)
    assert engine.due(daily, after_daily)
    assert engine.due(weekly, after_weekly)
    assert engine.next_eligible(daily, after_daily) == after_daily
    assert engine.next_eligible(weekly, after_weekly) == after_weekly
    skipped = ScheduleSpec(
        mode=ScheduleMode.RECURRING,
        timezone="UTC",
        scheduled_at="2026-10-27T09:00:00",
        recurrence="daily",
        catch_up_policy=CatchUpPolicy.SKIP,
    )
    after_initial = datetime(2026, 10, 27, 10, 0, tzinfo=UTC)
    assert engine.due(skipped, after_initial)
    assert engine.due(skipped, after_daily)
    assert engine.next_eligible(skipped, after_initial) == datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
    assert engine.next_eligible(skipped, datetime(2026, 10, 28, 8, 0, tzinfo=UTC)) == datetime(
        2026, 10, 28, 9, 0, tzinfo=UTC
    )


def test_preparation_persists_exact_mime_and_suppression_without_attempts(tmp_path):
    store, service, campaign_id = _campaign(
        tmp_path, "alice@example.com", "blocked@example.com", "bob@example.com"
    )
    service.campaigns.add_suppression(
        "blocked@example.com", SuppressionCategory.UNSUBSCRIBE, "opted out"
    )
    report = service.prepare_campaign(
        campaign_id,
        template=_template(),
        sender=_sender(),
    )
    assert report.prepared == 2
    assert report.excluded == 1
    assert len(store.list_intents(campaign_id)) == 3
    assert all(store.latest_attempt(row["id"]) is None for row in store.list_intents(campaign_id))
    preview = service.preview_message(campaign_id, "alice@example.com")
    prepared = next(
        row
        for row in store.list_intents(campaign_id)
        if row["normalized_email"] == "alice@example.com"
    )
    stored = store.get_prepared_message(prepared["id"])
    assert stored["content_hash"] == preview["content_hash"]
    assert preview["text"].strip() == "Welcome alice to Mailforge."

    service.authorize_simulation(campaign_id)
    report, transport = service.execute(
        campaign_id, outcomes={"bob@example.com": AttemptOutcome.TRANSIENT_FAILURE}, stop_after=1
    )
    assert len(transport.calls) == 1
    assert report.counts[DeliveryState.ACCEPTED.value] == 1


def test_dispatch_uses_the_campaign_sender_policy(tmp_path):
    store, service, campaign_id = _campaign(tmp_path, "sender-bound@example.com")
    alternate = _sender(ref="alternate", email="alternate@example.com", priority=-1)
    service.register_sender_policy(alternate)
    service.prepare_campaign(campaign_id, template=_template(), sender=_sender())
    service.authorize_simulation(campaign_id)
    service.execute(campaign_id)
    attempt = store.latest_attempt(store.list_intents(campaign_id)[0]["id"])
    assert attempt["sender_policy_ref"] == "sender-policy"


def test_policy_registration_preserves_runtime_cooldown(tmp_path):
    store, service, campaign_id = _campaign(tmp_path, "cooldown@example.com")
    sender = _sender(cooldown_seconds=60)
    service.prepare_campaign(campaign_id, template=_template(), sender=sender)
    service.authorize_simulation(campaign_id)
    service.execute(campaign_id, stop_after=1)
    before = store.list_sender_policies()[0]["cooldown_until"]
    assert before is not None
    service.prepare_campaign(campaign_id, template=_template(), sender=sender)
    assert store.list_sender_policies()[0]["cooldown_until"] == before


def test_scheduled_campaign_is_not_dispatched_before_due_and_can_be_authorized(tmp_path):
    current = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
    store, service, campaign_id = _campaign(tmp_path, "future@example.com")
    service.now_fn = lambda: current
    service.policy.now_fn = service.now_fn
    service.prepare_campaign(
        campaign_id,
        template=_template(),
        sender=_sender(),
        schedule=ScheduleSpec(
            mode=ScheduleMode.RECURRING,
            timezone="UTC",
            scheduled_at="2026-10-27T13:00:00",
            recurrence="weekly",
            catch_up_policy=CatchUpPolicy.SKIP,
        ),
    )
    service.authorize_simulation(campaign_id)
    worker_report, worker_transport = service.execute_worker(campaign_id, worker_id="future-worker")
    assert worker_report.campaign_status == "scheduled"
    assert worker_transport.calls == []
    report, transport = service.execute(campaign_id)
    assert report.campaign_status == "scheduled"
    assert transport.calls == []
    assert store.latest_attempt(store.list_intents(campaign_id)[0]["id"]) is None


def test_concurrent_workers_reserve_each_prepared_intent_once(tmp_path):
    store, service, campaign_id = _campaign(
        tmp_path, "one@example.com", "two@example.com", "three@example.com", "four@example.com"
    )
    service.prepare_campaign(campaign_id, template=_template(), sender=_sender(max_concurrency=2))
    service.authorize_simulation(campaign_id)
    transports = []
    lock = Lock()

    def worker(worker_id):
        report, transport = service.execute_worker(campaign_id, worker_id=worker_id, max_items=2)
        with lock:
            transports.append(transport)
        return report

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(worker, ("worker-a", "worker-b")))
    calls = [call for transport in transports for call in transport.calls]
    assert len(calls) == 4
    assert len({call[0] for call in calls}) == 4
    assert all(
        row["state"] == DeliveryState.ACCEPTED.value for row in store.list_intents(campaign_id)
    )


def test_attachment_failure_is_reported_per_recipient(tmp_path):
    store, service, campaign_id = _campaign(tmp_path, "one@example.com", "two@example.com")
    report = service.prepare_campaign(
        campaign_id,
        template=_template(),
        sender=_sender(),
        attachments=(AttachmentSpec("missing.txt"),),
    )
    assert report.prepared == 0
    assert len(report.issues) == 2
    assert all(
        row["state"] == DeliveryState.NOT_ELIGIBLE.value for row in store.list_intents(campaign_id)
    )


def test_outbound_state_is_json_serializable(tmp_path):
    store, service, campaign_id = _campaign(tmp_path, "one@example.com")
    report = service.prepare_campaign(campaign_id, template=_template(), sender=_sender())
    assert json.dumps(report.__dict__, default=str)


def test_ambiguous_attempt_requires_explicit_reconciliation(tmp_path):
    store, service, campaign_id = _campaign(tmp_path, "uncertain@example.com")
    service.prepare_campaign(campaign_id, template=_template(), sender=_sender())
    intent = store.reserve_next(campaign_id)
    assert intent is not None
    assert store.mark_attempt_started(intent["id"], intent["attempt_id"])
    store.record_delivery_result(
        intent_id=intent["id"],
        attempt_id=intent["attempt_id"],
        result=AttemptOutcome.AMBIGUOUS,
        error_code="unknown",
        error_message="synthetic uncertainty",
    )
    assert store.list_intents(campaign_id)[0]["state"] == DeliveryState.AMBIGUOUS.value
    store.reconcile_attempt(intent["attempt_id"], "accepted")
    assert store.list_intents(campaign_id)[0]["state"] == DeliveryState.ACCEPTED.value


def test_schema_v1_database_migrates_to_outbound_schema(tmp_path):
    database = tmp_path / "v1.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(SCHEMA_SQL)
    connection.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    connection.execute("ALTER TABLE delivery_intents ADD COLUMN lease_owner TEXT")
    connection.execute("INSERT INTO schema_migrations VALUES (1, '2026-10-27T00:00:00+00:00')")
    connection.commit()
    connection.close()

    store = CampaignStore(database)
    assert store.schema_version() == 18
    assert store.integrity_check()
    with store.connect() as migrated:
        columns = {row["name"] for row in migrated.execute("PRAGMA table_info(delivery_intents)")}
        attempt_columns = {
            row["name"] for row in migrated.execute("PRAGMA table_info(delivery_attempts)")
        }
        provider_tables = {
            row["name"]
            for row in migrated.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'provider_%'"
            )
        }
    assert {"lease_owner", "lease_expires_at", "not_before_utc"} <= columns
    assert "response_code" in attempt_columns
    assert {
        "provider_profiles",
        "provider_diagnostics",
        "provider_accounts",
        "provider_inbound_settings",
        "provider_operation_grants",
        "provider_test_submissions",
        "provider_oauth_flows",
    } <= provider_tables
