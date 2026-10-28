"""Modern outbound preparation and synthetic execution orchestration."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from email.parser import BytesParser
from email.policy import SMTP
from pathlib import Path

from mailforge.application.campaigns import (
    CampaignReport,
    CampaignService,
    SimulationTransport,
)
from mailforge.application.composition import (
    AssetResolver,
    MessageComposer,
    MessagePreparationError,
    ResolvedAttachment,
)
from mailforge.application.policy import PolicyEngine
from mailforge.application.providers import ProviderService
from mailforge.application.scheduling import ScheduleEngine, resolve_local_time
from mailforge.application.templates import SafeTemplateRenderer, TemplateRenderError
from mailforge.domain.campaign import (
    AttemptOutcome,
    CampaignStatus,
    ContactCandidate,
    DeliveryResult,
    DeliveryState,
    ExecutionAuthorization,
)
from mailforge.domain.outbound import (
    AttachmentSpec,
    CatchUpPolicy,
    OutboundPreparationReport,
    PreparationIssue,
    ScheduleMode,
    ScheduleSpec,
    SenderPolicy,
    TemplateDefinition,
)
from mailforge.domain.policy import EligibilityStatus
from mailforge.infrastructure.campaign_store import CampaignStore
from mailforge.infrastructure.smtp import (
    DeliveryStatus as SMTPDeliveryStatus,
)
from mailforge.infrastructure.smtp import (
    SMTPConnectionPool,
)
from mailforge.infrastructure.smtp import (
    deliver as deliver_smtp,
)


class PreparedSimulationTransport(SimulationTransport):
    """Fake transport that parses every persisted message before accepting it."""

    def __init__(
        self,
        store: CampaignStore,
        outcomes: Mapping[str, AttemptOutcome | DeliveryResult] | None = None,
    ):
        self.store = store
        self.outcomes = dict(outcomes or {})
        self.calls: list[tuple[str, str, int]] = []
        self.messages: dict[str, bytes] = {}

    def send(self, email: str, intent_id: str, attempt_number: int) -> DeliveryResult:
        prepared = self.store.get_prepared_message(intent_id)
        if prepared is None:
            raise RuntimeError(f"no prepared message for intent {intent_id}")
        parsed = BytesParser(policy=SMTP).parsebytes(prepared["mime_bytes"])
        if parsed["To"] is None or parsed["Message-ID"] is None:
            raise RuntimeError("prepared MIME message is missing mandatory headers")
        if prepared["recipient"] != email:
            raise RuntimeError("prepared recipient does not match delivery intent")
        self.calls.append((email, intent_id, attempt_number))
        self.messages[intent_id] = bytes(prepared["mime_bytes"])
        outcome = self.outcomes.get(email, AttemptOutcome.ACCEPTED)
        return outcome if isinstance(outcome, DeliveryResult) else DeliveryResult(outcome)


class PreparedSMTPTransport(SimulationTransport):
    """Dispatch persisted MIME through the canonical, explicitly gated SMTP pool."""

    requires_policy_guard = True

    def __init__(self, store: CampaignStore, pool: SMTPConnectionPool):
        self.store = store
        self.pool = pool

    def send(self, email: str, intent_id: str, attempt_number: int) -> DeliveryResult:
        prepared = self.store.get_prepared_message(intent_id)
        if prepared is None:
            raise RuntimeError(f"no prepared message for intent {intent_id}")
        if prepared["recipient"] != email:
            raise RuntimeError("prepared recipient does not match delivery intent")
        result = deliver_smtp(
            self.pool,
            prepared["from_email"],
            email,
            prepared["mime_bytes"],
            gate=self.pool.gate,
        )
        outcome = {
            SMTPDeliveryStatus.ACCEPTED: AttemptOutcome.ACCEPTED,
            SMTPDeliveryStatus.PERMANENT_REJECTION: AttemptOutcome.PERMANENT_FAILURE,
            SMTPDeliveryStatus.TRANSIENT_REJECTION: AttemptOutcome.TRANSIENT_FAILURE,
            SMTPDeliveryStatus.CONNECTION_FAILURE: AttemptOutcome.AMBIGUOUS,
            SMTPDeliveryStatus.AMBIGUOUS: AttemptOutcome.AMBIGUOUS,
        }[result.status]
        return DeliveryResult(
            outcome,
            error_code=(
                None
                if outcome is AttemptOutcome.ACCEPTED
                else f"smtp_{result.status.value}_{self.pool.last_phase}"
            ),
            error_message=None if outcome is AttemptOutcome.ACCEPTED else result.detail,
            transport_phase=self.pool.last_transaction.phase.value,
            response_code=self.pool.last_transaction.response_code,
        )


class OutboundService:
    """Coordinates templates, MIME preparation, policy, schedules, and dispatch."""

    def __init__(self, store: CampaignStore, *, now_fn=None):
        self.store = store
        self.renderer = SafeTemplateRenderer()
        self.composer = MessageComposer()
        self.schedules = ScheduleEngine()
        self.policy = PolicyEngine(store, now_fn=now_fn)
        self.now_fn = now_fn or (lambda: datetime.now(UTC))
        self.campaigns = CampaignService(store, now_fn=now_fn)

    def register_template(self, definition: TemplateDefinition) -> str:
        return self.store.register_template(definition)

    def register_sender_policy(self, policy: SenderPolicy, configuration=None) -> None:
        self.policy.validate(policy)
        self.store.register_sender_policy(policy, configuration)

    def prepare_campaign(
        self,
        campaign_id: str,
        *,
        template: TemplateDefinition,
        sender: SenderPolicy,
        campaign_variables: dict[str, object] | None = None,
        asset_root: str | Path | None = None,
        attachments: tuple[AttachmentSpec, ...] = (),
        schedule: ScheduleSpec | None = None,
        unsubscribe_url: str | None = None,
        one_click_unsubscribe: bool = False,
        dkim_signed: bool = False,
        sender_configuration: Mapping[str, object] | None = None,
        resolved_attachments: tuple[ResolvedAttachment, ...] = (),
        resolved_attachments_by_contact: Mapping[str, tuple[ResolvedAttachment, ...]] | None = None,
        render_callback: Callable[[Mapping[str, object], Mapping[str, object], int], object]
        | None = None,
    ) -> OutboundPreparationReport:
        campaign = self.store.get_campaign(campaign_id)
        if campaign["sender_config_ref"] != sender.ref:
            raise ValueError(
                f"sender policy {sender.ref} does not match campaign sender "
                f"{campaign['sender_config_ref']}"
            )
        self.register_template(template)
        self.register_sender_policy(sender, sender_configuration)
        if schedule is not None:
            self.schedules.validate(schedule)
            self.store.upsert_schedule(campaign_id, schedule)
        base_report = self.campaigns.prepare(campaign_id)
        campaign = self.store.get_campaign(campaign_id)
        if campaign["audience_source_type"] == "segment":
            contacts = {
                row["contact_id"]: self.store.get_contact(row["contact_id"])
                for row in self.store.list_intents(campaign_id)
            }
        else:
            contacts = {
                row["id"]: row for row in self.store.list_contacts(campaign["audience_list_id"])
            }
        resolved: tuple = ()
        asset_error: Exception | None = None
        if attachments:
            if asset_root is None:
                asset_error = MessagePreparationError(
                    "an asset_root is required when attachments are configured"
                )
            else:
                try:
                    resolver = AssetResolver(asset_root)
                    resolved = tuple(resolver.resolve(spec) for spec in attachments)
                except (OSError, ValueError) as error:
                    asset_error = error
        issues: list[PreparationIssue] = []
        prepared = 0
        for intent in self.store.list_intents(campaign_id):
            if intent["state"] != DeliveryState.ELIGIBLE_PENDING.value:
                continue
            contact = contacts[intent["contact_id"]]
            metadata = json.loads(contact["metadata_json"])
            context = {
                "email": contact["normalized_email"],
                "name": contact["display_name"],
                "contact": metadata,
                "target": {
                    "email": contact["normalized_email"],
                    "name": contact["display_name"],
                    **metadata,
                },
                **metadata,
                **(campaign_variables or {}),
            }
            seed = int(
                hashlib.sha256(
                    f"{campaign_id}:{intent['normalized_email']}:{template.version}".encode()
                ).hexdigest()[:16],
                16,
            ) % (2**63 - 1)
            try:
                if asset_error is not None:
                    raise asset_error
                callback_attachments: tuple[ResolvedAttachment, ...] = ()
                if render_callback is not None:
                    callback_result = render_callback(contact, intent, seed)
                    if isinstance(callback_result, tuple) and len(callback_result) == 2:
                        rendered, callback_attachments = callback_result
                    else:
                        rendered = callback_result
                else:
                    rendered = self.renderer.render(template, context, seed=seed, now=self.now_fn())
                selected_attachments = resolved_attachments or resolved
                if resolved_attachments_by_contact:
                    selected_attachments = selected_attachments + tuple(
                        resolved_attachments_by_contact.get(intent["contact_id"], ())
                    )
                selected_attachments = selected_attachments + callback_attachments
                composed = self.composer.compose(
                    sender=replace(sender, reply_to=sender.reply_to or template.reply_to),
                    recipient=intent["normalized_email"],
                    rendered=rendered,
                    attachments=selected_attachments,
                    unsubscribe_url=unsubscribe_url,
                    one_click_unsubscribe=one_click_unsubscribe,
                    dkim_signed=dkim_signed,
                )
            except (TemplateRenderError, ValueError, OSError) as error:
                self.store.set_intent_state(
                    intent["id"], DeliveryState.NOT_ELIGIBLE, f"preparation_error: {error}"
                )
                issues.append(
                    PreparationIssue("message_preparation", str(error), intent["normalized_email"])
                )
                continue
            self.store.save_prepared_message(
                intent_id=intent["id"],
                template_name=template.name,
                template_version=template.version,
                rendering_seed=seed,
                message_id=composed.message_id,
                subject=composed.subject,
                from_email=composed.from_email,
                recipient=composed.recipient,
                content_hash=composed.content_hash,
                mime_bytes=composed.serialized,
                attachment_metadata=[
                    {
                        "filename": item.spec.filename or Path(item.spec.path).name,
                        "content_type": item.content_type,
                        "content_id": item.spec.content_id,
                        "bytes": len(item.data),
                    }
                    for item in composed.attachments
                ],
            )
            self.store.set_intent_state(intent["id"], DeliveryState.PREPARED, "message_prepared")
            prepared += 1
        if schedule is not None and not issues:
            if schedule.scheduled_at:
                self.store.set_campaign_scheduled_at(
                    campaign_id,
                    resolve_local_time(
                        schedule.scheduled_at, schedule.timezone, fold=schedule.fold
                    ).isoformat(),
                )
            current = CampaignStatus(self.store.get_campaign(campaign_id)["status"])
            if current is CampaignStatus.READY and schedule.mode.value != "immediate":
                self.store.transition_campaign(
                    campaign_id, CampaignStatus.SCHEDULED, "schedule_configured"
                )
        excluded = base_report.total - base_report.counts.get(
            DeliveryState.ELIGIBLE_PENDING.value, 0
        )
        pacing = schedule.pacing_seconds if schedule else 0.0
        return OutboundPreparationReport(
            campaign_id=campaign_id,
            prepared=prepared,
            excluded=excluded,
            issues=tuple(issues),
            estimated_seconds=prepared * pacing,
            schedule=schedule,
        )

    def preview_message(self, campaign_id: str, email: str) -> dict[str, object]:
        normalized = email.strip().lower()
        intent = next(
            (
                row
                for row in self.store.list_intents(campaign_id)
                if row["normalized_email"] == normalized
            ),
            None,
        )
        if intent is None:
            raise KeyError(f"recipient not prepared: {email}")
        prepared = self.store.get_prepared_message(intent["id"])
        if prepared is None:
            raise KeyError(f"recipient has no prepared message: {email}")
        message = BytesParser(policy=SMTP).parsebytes(prepared["mime_bytes"])
        bodies = {"text": "", "html": ""}
        for part in message.walk():
            if part.get_content_disposition() != "attachment" and part.get_content_type() in {
                "text/plain",
                "text/html",
            }:
                bodies["html" if part.get_content_type() == "text/html" else "text"] = (
                    part.get_content()
                )
        return {
            "email": normalized,
            "message_id": prepared["message_id"],
            "content_hash": prepared["content_hash"],
            "subject": prepared["subject"],
            "from": prepared["from_email"],
            "headers": {
                key: message[key] for key in ("To", "Reply-To", "List-Unsubscribe") if message[key]
            },
            "text": bodies["text"],
            "html": bodies["html"],
        }

    def authorize_simulation(self, campaign_id: str) -> None:
        if not any(
            row["state"] == DeliveryState.PREPARED.value
            for row in self.store.list_intents(campaign_id)
        ):
            raise ValueError("campaign has no prepared messages")
        self.campaigns.authorize_simulation(campaign_id)

    def execute(
        self,
        campaign_id: str,
        *,
        outcomes: Mapping[str, AttemptOutcome | DeliveryResult] | None = None,
        retry_deferred: bool = False,
        stop_after: int | None = None,
        worker_id: str = "outbound-worker",
    ) -> tuple[CampaignReport, PreparedSimulationTransport]:
        self.store.get_campaign(campaign_id)
        schedule = self._stored_schedule(campaign_id)
        if schedule is not None:
            if not self.schedules.due(schedule, self.now_fn()):
                return self.campaigns.report(campaign_id), PreparedSimulationTransport(
                    self.store, outcomes
                )
        transport = PreparedSimulationTransport(self.store, outcomes)
        report = self.campaigns.execute(
            campaign_id,
            transport,
            retry_deferred=retry_deferred,
            stop_after=stop_after,
            dispatch_guard=lambda intent: self._policy_guard(intent),
            worker_id=worker_id,
        )
        return report, transport

    def execute_smtp(
        self,
        campaign_id: str,
        pool: SMTPConnectionPool,
        *,
        retry_deferred: bool = False,
        stop_after: int | None = None,
        worker_id: str = "smtp-worker",
    ) -> tuple[CampaignReport, PreparedSMTPTransport]:
        """Execute prepared MIME through SMTP with the normal policy guard.

        This method is intentionally an application API rather than a CLI
        command. It requires the pool's independent live-send gate and still
        requires campaign simulation authorization, so a campaign flag alone
        can never open a socket.
        """

        pool.gate.require()
        self.store.get_campaign(campaign_id)
        schedule = self._stored_schedule(campaign_id)
        if schedule is not None and not self.schedules.due(schedule, self.now_fn()):
            return self.campaigns.report(campaign_id), PreparedSMTPTransport(self.store, pool)
        transport = PreparedSMTPTransport(self.store, pool)

        smtp_guard = self.campaigns._make_canonical_dispatch_guard(campaign_id, transport)

        report = self.campaigns.execute(
            campaign_id,
            transport,
            retry_deferred=retry_deferred,
            stop_after=stop_after,
            dispatch_guard=smtp_guard,
            worker_id=worker_id,
        )
        return report, transport

    def execute_provider(
        self,
        campaign_id: str,
        provider_ref: str,
        credential_resolver,
        *,
        gate,
        retry_deferred: bool = False,
        stop_after: int | None = None,
        worker_id: str = "smtp-worker",
    ) -> tuple[CampaignReport, PreparedSMTPTransport]:
        """Execute through a durable provider profile and the canonical SMTP path."""

        provider_service = ProviderService(self.store)
        profile = provider_service.profile(provider_ref)
        campaign = self.store.get_campaign(campaign_id)
        if profile.sender_policy_ref != campaign["sender_config_ref"]:
            raise ValueError(
                f"provider profile {provider_ref} is not associated with campaign sender "
                f"policy {campaign['sender_config_ref']}"
            )
        pool = provider_service.smtp_pool(provider_ref, credential_resolver, gate=gate)
        try:
            return self.execute_smtp(
                campaign_id,
                pool,
                retry_deferred=retry_deferred,
                stop_after=stop_after,
                worker_id=worker_id,
            )
        finally:
            pool.close()

    def _policy_guard(self, intent) -> tuple[bool, str, str | None]:
        campaign = self.store.get_campaign(intent["campaign_id"])
        decision = self.policy.reserve(
            attempt_id=intent["attempt_id"],
            intent_id=intent["id"],
            recipient=intent["normalized_email"],
            preferred_ref=campaign["sender_config_ref"],
        )
        return decision.allowed, decision.reason, decision.retry_at

    def _stored_schedule(self, campaign_id: str) -> ScheduleSpec | None:
        row = self.store.get_schedule(campaign_id)
        if row is None:
            return None
        return ScheduleSpec(
            mode=ScheduleMode(row["mode"]),
            timezone=row["timezone"],
            scheduled_at=row["scheduled_at"],
            window_start=row["window_start"],
            window_end=row["window_end"],
            weekdays=tuple(json.loads(row["weekdays_json"])),
            pacing_seconds=row["pacing_seconds"],
            cooldown_seconds=row["cooldown_seconds"],
            recurrence=row["recurrence"],
            catch_up_policy=CatchUpPolicy(row["catch_up_policy"]),
            fold=row["fold"],
        )

    def execute_worker(
        self,
        campaign_id: str,
        *,
        worker_id: str,
        outcomes: Mapping[str, AttemptOutcome | DeliveryResult] | None = None,
        max_items: int | None = None,
    ) -> tuple[CampaignReport, PreparedSimulationTransport]:
        campaign = self.store.get_campaign(campaign_id)
        if (
            campaign["authorization"] != ExecutionAuthorization.SIMULATION_AUTHORIZED.value
            or not self.store.has_execution_declaration(campaign_id)
        ):
            raise PermissionError(
                "simulation authorization and an operator declaration are required; "
                "live SMTP is not available"
            )
        schedule = self._stored_schedule(campaign_id)
        if schedule is not None and not self.schedules.due(schedule, self.now_fn()):
            return self.campaigns.report(campaign_id), PreparedSimulationTransport(
                self.store, outcomes
            )
        status = CampaignStatus(campaign["status"])
        if status in {
            CampaignStatus.READY,
            CampaignStatus.SCHEDULED,
            CampaignStatus.PAUSED,
        }:
            try:
                self.store.transition_campaign(
                    campaign_id,
                    CampaignStatus.RUNNING,
                    "schedule_due" if status is CampaignStatus.SCHEDULED else "worker_started",
                )
            except ValueError:
                if (
                    CampaignStatus(self.store.get_campaign(campaign_id)["status"])
                    is not CampaignStatus.RUNNING
                ):
                    raise
        elif status is not CampaignStatus.RUNNING:
            raise ValueError(f"campaign cannot execute from {status.value}")
        transport = PreparedSimulationTransport(self.store, outcomes)
        recovered = self.store.recover_inflight(campaign_id, force=False)
        processed = 0
        drained = False
        while max_items is None or processed < max_items:
            pause_requested, cancel_requested = self.store.campaign_flags(campaign_id)
            if cancel_requested:
                current = CampaignStatus(self.store.get_campaign(campaign_id)["status"])
                if current is CampaignStatus.RUNNING:
                    self.store.transition_campaign(
                        campaign_id, CampaignStatus.CANCELED, "operator_canceled"
                    )
                break
            if pause_requested:
                current = CampaignStatus(self.store.get_campaign(campaign_id)["status"])
                if current is CampaignStatus.RUNNING:
                    self.store.transition_campaign(
                        campaign_id, CampaignStatus.PAUSING, "pause_requested"
                    )
                    self.store.transition_campaign(
                        campaign_id, CampaignStatus.PAUSED, "paused_by_operator"
                    )
                break
            intent = self.store.reserve_next(campaign_id, worker_id=worker_id)
            if intent is None:
                drained = True
                break
            policy_revision = self.store.policy_revision()
            fresh_campaign = self.store.get_campaign(campaign_id)
            suppression = self.store.suppression_reason(
                normalized_email=intent["normalized_email"],
                contact_id=intent["contact_id"],
                list_id=fresh_campaign["audience_list_id"],
                campaign_id=campaign_id,
                sender_config_ref=fresh_campaign["sender_config_ref"],
                communication_purpose=self.campaigns._eligibility.purpose(fresh_campaign),
            )
            if suppression:
                self.store.mark_intent_suppressed(intent["id"], suppression)
                continue
            decision = self.campaigns._eligibility.evaluate_contact(
                self.store.get_contact(intent["contact_id"]),
                campaign=fresh_campaign,
                operator_authorized=True,
            )
            if decision.status in {EligibilityStatus.EXCLUDED, EligibilityStatus.REQUIRES_REVIEW}:
                self.store.mark_intent_suppressed(intent["id"], decision.reason)
                continue
            allowed, reason, retry_at = self._policy_guard(intent)
            if not allowed:
                self.store.release_reservation(intent["id"], reason, not_before_utc=retry_at)
                break
            if not self.store.mark_attempt_started(
                intent["id"], intent["attempt_id"], expected_policy_revision=policy_revision
            ):
                self.store.release_reservation(intent["id"], "policy_changed_before_dispatch")
                continue
            try:
                result = transport.send(
                    intent["normalized_email"], intent["id"], intent["attempt_number"]
                )
            except Exception as error:
                self.store.record_delivery_result(
                    intent_id=intent["id"],
                    attempt_id=intent["attempt_id"],
                    result=AttemptOutcome.AMBIGUOUS,
                    error_code="worker_exception",
                    error_message=type(error).__name__,
                )
                break
            self.store.record_delivery_result(
                intent_id=intent["id"],
                attempt_id=intent["attempt_id"],
                result=result.outcome,
                error_code=result.error_code,
                error_message=result.error_message,
                transport_id=result.transport_id,
                transport_phase=result.transport_phase,
                response_code=result.response_code,
            )
            processed += 1
        if drained:
            counts = self.store.counts(campaign_id)
            unresolved = any(
                counts.get(state, 0)
                for state in (
                    DeliveryState.ELIGIBLE_PENDING.value,
                    DeliveryState.PREPARED.value,
                    DeliveryState.DEFERRED.value,
                    DeliveryState.AMBIGUOUS.value,
                    DeliveryState.RESERVED.value,
                    DeliveryState.ATTEMPT_STARTED.value,
                )
            )
            current = CampaignStatus(self.store.get_campaign(campaign_id)["status"])
            if current is CampaignStatus.RUNNING:
                self.store.transition_campaign(
                    campaign_id, CampaignStatus.COMPLETING, "worker_drained"
                )
                self.store.transition_campaign(
                    campaign_id,
                    CampaignStatus.PAUSED if unresolved else CampaignStatus.COMPLETED,
                    "unresolved_delivery_state" if unresolved else "worker_completed",
                )
        return self.campaigns.report(campaign_id, recovered=recovered), transport


def run_outbound_demo(database: str | Path) -> dict[str, object]:
    """Run a complete composition, policy, schedule, and fake-dispatch workflow."""

    from tempfile import TemporaryDirectory

    from mailforge.domain.campaign import SuppressionCategory

    store = CampaignStore(database)
    service = OutboundService(store)
    with TemporaryDirectory(prefix="mailforge-outbound-") as directory:
        assets = Path(directory)
        attachment = assets / "notice.txt"
        attachment.write_text("synthetic attachment\n", encoding="utf-8")
        list_id = store.create_list("outbound-demo-audience")
        service.campaigns.import_candidates(
            [
                ContactCandidate(
                    email,
                    email,
                    "synthetic-outbound.txt",
                    position,
                    {"organization": "Mailforge", "position": position},
                )
                for position, email in enumerate(
                    ("alice@example.com", "bob@example.com", "blocked@example.com"), 1
                )
            ],
            list_id,
        )
        service.campaigns.add_suppression(
            "blocked@example.com", SuppressionCategory.UNSUBSCRIBE, "synthetic opt-out"
        )
        campaign_id = service.campaigns.create_campaign(
            "outbound-demo", list_id, template_ref="demo-template", sender_config_ref="demo-sender"
        )
        template = TemplateDefinition(
            name="demo-template",
            version="1",
            subject="Hello {{ name or email }} {from|to} Mailforge",
            text="Hello {{ name or email }}. This is a deterministic preview.",
            html='<p>Hello {{ name or email }}.</p><p><img src="cid:notice"></p>',
        )
        sender = SenderPolicy(
            ref="demo-sender",
            provider="synthetic-provider",
            email="sender@example.com",
            display_name="Mailforge Demo",
            max_per_minute=10,
            per_domain_per_minute=10,
        )
        preparation = service.prepare_campaign(
            campaign_id,
            template=template,
            sender=sender,
            asset_root=assets,
            attachments=(AttachmentSpec("notice.txt", content_id="notice"),),
            schedule=ScheduleSpec(mode=ScheduleMode.IMMEDIATE),
        )
        preview = service.preview_message(campaign_id, "alice@example.com")
        service.authorize_simulation(campaign_id)
        first, first_transport = service.execute(
            campaign_id,
            outcomes={"bob@example.com": AttemptOutcome.TRANSIENT_FAILURE},
            stop_after=1,
        )
        final, final_transport = service.execute(
            campaign_id,
            outcomes={"bob@example.com": AttemptOutcome.TRANSIENT_FAILURE},
        )
        return {
            "campaign_id": campaign_id,
            "preparation": {
                "prepared": preparation.prepared,
                "excluded": preparation.excluded,
                "issues": [issue.__dict__ for issue in preparation.issues],
            },
            "preview": preview,
            "first_run": first.to_dict(),
            "final": final.to_dict(),
            "transport_calls": len(first_transport.calls) + len(final_transport.calls),
        }
