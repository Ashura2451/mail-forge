"""Durable inbound acquisition, interpretation, correlation, and review workflow."""

from __future__ import annotations

import json
import re
from typing import Any

from mailforge.application.inbound_parser import SafeInboundParser
from mailforge.application.inbound_sources import CursorInvalidated, InboundSource
from mailforge.application.providers import ProviderService
from mailforge.domain.inbound import (
    ActionStatus,
    ActionType,
    Classification,
    Correlation,
    CorrelationKind,
    DsnAction,
    InboundBatchReport,
    InboundKind,
    InboundProcessResult,
    InboundSourceType,
    ParseStatus,
    PolicyProposal,
    SourceMessage,
    TrustStatus,
)
from mailforge.infrastructure.campaign_store import CampaignStore

_EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)


class InboundService:
    def __init__(self, store: CampaignStore, parser: SafeInboundParser | None = None):
        self.store = store
        self.parser = parser or SafeInboundParser()
        self.operations = None

    def set_operations(self, operations) -> None:
        """Attach the workbench facade without changing inbound authority."""
        self.operations = operations

    def ingest_provider(
        self,
        provider_ref: str,
        credential_resolver,
        *,
        batch_limit: int = 100,
        retain_raw: bool = True,
    ) -> InboundBatchReport:
        """Acquire through a durable IMAP profile and reuse this service's pipeline."""

        source = ProviderService(self.store).imap_source(provider_ref, credential_resolver)
        return self.ingest(source, batch_limit=batch_limit, retain_raw=retain_raw)

    def register_source(
        self,
        source: InboundSource,
        *,
        trusted_synthetic: bool = False,
        retention_policy: str = "retain",
    ) -> None:
        source_type = getattr(source, "source_type", "synthetic")
        if trusted_synthetic and source_type != InboundSourceType.SYNTHETIC:
            raise ValueError("only synthetic sources may be trusted synthetically")
        trust = TrustStatus.TRUSTED_SYNTHETIC if trusted_synthetic else TrustStatus.UNKNOWN
        self.store.register_inbound_source(
            source.source_ref,
            source_type,
            mailbox=getattr(source, "mailbox_name", None),
            trust_status=trust,
            retention_policy=retention_policy,
        )

    def _classification(self, parsed) -> Classification:
        headers = parsed.headers
        if parsed.dsn:
            return Classification(
                InboundKind.DELIVERY_STATUS,
                ("delivery_status_report",),
                {
                    "report_type": "delivery-status",
                    "recipient_count": str(len(parsed.dsn.recipients)),
                },
            )
        if parsed.complaint:
            return Classification(
                InboundKind.COMPLAINT,
                ("feedback_report",),
                {
                    "feedback_type": parsed.complaint.feedback_type or "unknown",
                    "abuse_indicator": str(parsed.complaint.abuse_indicator).lower(),
                },
            )
        auto = (
            (headers.get("auto-submitted", ("no",))[0].lower() != "no")
            or bool(headers.get("x-auto-response-suppress"))
            or headers.get("precedence", ("",))[0].lower() in {"bulk", "list", "junk"}
        )
        if auto:
            return Classification(
                InboundKind.AUTOMATIC_REPLY,
                ("automatic_response_signal",),
                {
                    "auto_submitted": headers.get("auto-submitted", ("",))[0],
                    "precedence": headers.get("precedence", ("",))[0],
                },
            )
        combined = f"{parsed.subject or ''}\n{parsed.text_preview}".lower()
        if (
            "unsubscribe" in combined
            or "remove me" in combined
            or bool(headers.get("list-unsubscribe"))
        ):
            return Classification(
                InboundKind.UNSUBSCRIBE,
                ("unsubscribe_evidence",),
                {"keyword_signal": "unsubscribe" if "unsubscribe" in combined else "remove_me"},
            )
        if (
            parsed.in_reply_to
            or parsed.references
            or (parsed.subject or "").lower().startswith(("re:", "fwd:"))
        ):
            return Classification(
                InboundKind.HUMAN_REPLY, ("reply_signal",), {"header_reply": "true"}
            )
        if parsed.sender and parsed.text_preview:
            return Classification(InboundKind.HUMAN_REPLY, ("sender_and_text",), {})
        return Classification(InboundKind.UNKNOWN, ("no_supported_interpretation",), {})

    def _correlations(self, parsed) -> tuple[Correlation, ...]:
        identifiers = tuple(
            dict.fromkeys(
                (
                    *parsed.references,
                    parsed.in_reply_to or "",
                    parsed.complaint.original_message_id if parsed.complaint else "",
                    parsed.dsn.original_envelope_id if parsed.dsn else "",
                )
            )
        )
        candidates = self.store.find_outbound_candidates(
            identifiers, (parsed.sender,) if parsed.sender else ()
        )
        exact = [row for row in candidates if row["message_id"] in identifiers]
        if exact:
            return tuple(
                Correlation(
                    CorrelationKind.EXACT,
                    "delivery_intent",
                    row["intent_id"],
                    ("outbound_message_id", row["message_id"]),
                )
                for row in exact
            )
        if len(candidates) == 1:
            row = candidates[0]
            return (
                Correlation(
                    CorrelationKind.PROBABLE,
                    "delivery_intent",
                    row["intent_id"],
                    ("sender_recipient_match", row["normalized_email"]),
                ),
            )
        if len(candidates) > 1:
            return (
                Correlation(
                    CorrelationKind.AMBIGUOUS, None, None, ("multiple_recipient_candidates",)
                ),
            )
        return (Correlation(CorrelationKind.UNMATCHED, None, None, ("no_outbound_evidence",)),)

    @staticmethod
    def _target(parsed, correlations: tuple[Correlation, ...]) -> str | None:
        dsn_targets = ()
        if parsed.dsn:
            dsn_targets = tuple(
                recipient.final_recipient or recipient.original_recipient
                for recipient in parsed.dsn.recipients
            )
        values = (*dsn_targets, parsed.complaint.original_recipient if parsed.complaint else None)
        if not parsed.complaint:
            values += (parsed.sender,)
        for value in values:
            if value and _EMAIL_RE.fullmatch(value.strip()):
                return value.strip().lower()
        return None

    def _actions(
        self,
        message_id: str,
        parsed,
        classification: Classification,
        correlations: tuple[Correlation, ...],
        trust: TrustStatus,
    ) -> tuple[tuple[str, ...], tuple[PolicyProposal, ...]]:
        exact = next((item for item in correlations if item.kind is CorrelationKind.EXACT), None)
        trusted_exact = trust is TrustStatus.TRUSTED_SYNTHETIC and exact is not None
        target = self._target(parsed, correlations)
        action_types: list[tuple[ActionType, bool, str, str | None]] = []
        if classification.kind is InboundKind.DELIVERY_STATUS and parsed.dsn:
            for recipient in parsed.dsn.recipients:
                recipient_target = recipient.final_recipient or recipient.original_recipient
                if recipient.action is DsnAction.FAILED and (recipient.status or "").startswith(
                    "5."
                ):
                    action_types.append(
                        (
                            ActionType.CREATE_HARD_BOUNCE,
                            trusted_exact,
                            "permanent DSN failure",
                            recipient_target,
                        )
                    )
                else:
                    action_types.append(
                        (
                            ActionType.RECORD_EVENT,
                            False,
                            "temporary or positive DSN evidence",
                            recipient_target,
                        )
                    )
            if not parsed.dsn.recipients:
                action_types.append((ActionType.RECORD_EVENT, False, "empty DSN evidence", target))
        elif classification.kind is InboundKind.COMPLAINT:
            action_types.append(
                (
                    ActionType.CREATE_COMPLAINT,
                    trusted_exact,
                    "feedback report requires trust and exact correlation",
                    target,
                )
            )
        elif classification.kind is InboundKind.UNSUBSCRIBE:
            action_types.append(
                (
                    ActionType.CREATE_UNSUBSCRIBE,
                    trusted_exact,
                    "unsubscribe evidence requires trust and exact correlation",
                    target,
                )
            )
        else:
            action_types.append((ActionType.RECORD_EVENT, False, "observation only", target))
        action_ids: list[str] = []
        proposals: list[PolicyProposal] = []
        for action_type, auto_apply, reason, action_target in action_types:
            auto_apply = auto_apply and action_target is not None
            requires_review = not auto_apply and action_type in {
                ActionType.CREATE_HARD_BOUNCE,
                ActionType.CREATE_COMPLAINT,
                ActionType.CREATE_UNSUBSCRIBE,
            }
            status = ActionStatus.REVIEW_REQUIRED if requires_review else ActionStatus.PROPOSED
            action_id, _ = self.store.propose_inbound_action(
                message_id, action_type, action_target, reason, status=status
            )
            action_ids.append(action_id)
            proposals.append(
                PolicyProposal(
                    action_type,
                    ActionStatus.REVIEW_REQUIRED
                    if requires_review
                    else (ActionStatus.APPLIED if auto_apply else ActionStatus.PROPOSED),
                    reason,
                    action_target,
                    requires_review,
                )
            )
            if requires_review:
                self.store.create_inbound_review(message_id, action_id, reason)
            elif auto_apply:
                self.store.decide_inbound_action(action_id, "approve", reason)
        return tuple(action_ids), tuple(proposals)

    def process_message(self, message_id: str) -> InboundProcessResult:
        row = self.store.get_inbound_message(message_id)
        if row["raw_bytes"] is None:
            parsed_status = ParseStatus.QUARANTINED
            from mailforge.domain.inbound import Classification

            classification = Classification(InboundKind.MALFORMED, ("raw_not_retained",), {})
            correlations = (
                Correlation(CorrelationKind.UNMATCHED, None, None, ("raw_not_retained",)),
            )
            self.store.save_inbound_classification(
                message_id, classification.kind, classification.reasons, classification.signals
            )
            self.store.mark_inbound_processed(message_id)
            return InboundProcessResult(
                message_id, False, parsed_status, classification, correlations, ()
            )
        source = SourceMessage(
            row["source_ref"],
            row["source_identity"],
            row["raw_bytes"],
            received_at=row["received_at"],
        )
        parsed = self.parser.parse(source)
        self.store.save_inbound_parse(message_id, parsed)
        classification = self._classification(parsed)
        correlations = self._correlations(parsed)
        trust = TrustStatus(self.store.get_inbound_source(row["source_ref"])["trust_status"])
        self.store.save_inbound_classification(
            message_id, classification.kind, classification.reasons, classification.signals
        )
        for correlation in correlations:
            self.store.save_inbound_correlation(message_id, correlation, trust)
        action_ids, proposals = self._actions(
            message_id, parsed, classification, correlations, trust
        )
        self.store.mark_inbound_processed(message_id)
        if self.operations is not None:
            self.operations.apply_active_rules(message_id)
        exact = next((item for item in correlations if item.kind is CorrelationKind.EXACT), None)
        if trust is TrustStatus.TRUSTED_SYNTHETIC and exact is not None:
            target = self._target(parsed, correlations)
            contact_id = None
            if target:
                with self.store.connect() as connection:
                    contact = connection.execute(
                        "SELECT id FROM contacts WHERE normalized_email = ?", (target,)
                    ).fetchone()
                contact_id = contact["id"] if contact else None
            with self.store.transaction(immediate=True) as connection:
                self.store.record_event(
                    connection,
                    "inbound_message",
                    message_id,
                    "trusted_inbound_reconciled",
                    {
                        "message_id": message_id,
                        "contact_id": contact_id,
                        "target": target,
                        "trust_status": trust.value,
                        "correlation_id": exact.entity_id,
                        "classification": classification.kind.value,
                        "provenance": {"source_ref": row["source_ref"], "synthetic": True},
                    },
                )
        reviews = tuple(
            review["id"]
            for review in self.store.list_inbound_reviews()
            if review["message_id"] == message_id
        )
        applied = tuple(
            action_id
            for action_id in action_ids
            if self.store.get_inbound_action(action_id)["status"] == ActionStatus.APPLIED.value
        )
        return InboundProcessResult(
            message_id,
            False,
            parsed.status,
            classification,
            correlations,
            proposals,
            applied,
            reviews,
        )

    def ingest(
        self,
        source: InboundSource,
        *,
        batch_limit: int = 100,
        reprocess: bool = False,
        fault_after: int | None = None,
        retain_raw: bool = True,
        trusted_synthetic: bool = False,
    ) -> InboundBatchReport:
        self.register_source(source, trusted_synthetic=trusted_synthetic)
        run_id, cursor = self.store.start_inbound_run(source.source_ref)
        acquired = duplicates = processed = failures = reviews = applied = 0
        cursor_after = cursor
        try:
            for pending in self.store.list_pending_inbound_messages(source.source_ref):
                try:
                    result = self.process_message(pending["id"])
                    processed += 1
                    reviews += len(result.review_ids)
                    applied += len(result.applied_actions)
                    if not retain_raw:
                        self.store.release_inbound_raw(pending["id"])
                except Exception:
                    failures += 1
            for item in source.messages(cursor=cursor, limit=batch_limit):
                message_id, duplicate = self.store.acquire_inbound_message(item, retain_raw=True)
                acquired += 1
                duplicates += int(duplicate)
                cursor_after = item.source_identity
                self.store.set_inbound_cursor(
                    source.source_ref, cursor_after, uidvalidity=item.uidvalidity
                )
                if fault_after is not None and acquired >= fault_after:
                    raise RuntimeError("inbound fault injection after durable acquisition")
                row = self.store.get_inbound_message(message_id)
                if duplicate and row["processed_at"] and not reprocess:
                    continue
                try:
                    result = self.process_message(message_id)
                    processed += 1
                    reviews += len(result.review_ids)
                    applied += len(result.applied_actions)
                    if not retain_raw:
                        self.store.release_inbound_raw(message_id)
                except Exception:
                    failures += 1
            self.store.finish_inbound_run(
                run_id,
                status="completed",
                cursor_after=cursor_after,
                acquired=acquired,
                processed=processed,
                failures=failures,
            )
        except CursorInvalidated as error:
            self.store.invalidate_inbound_cursor(source.source_ref, str(error))
            self.store.finish_inbound_run(
                run_id,
                status="cursor_invalidated",
                cursor_after=None,
                acquired=acquired,
                processed=processed,
                failures=failures + 1,
                error_message=str(error),
            )
            raise
        except Exception as error:
            self.store.finish_inbound_run(
                run_id,
                status="interrupted",
                cursor_after=cursor_after,
                acquired=acquired,
                processed=processed,
                failures=failures + 1,
                error_message=str(error),
            )
            raise
        return InboundBatchReport(
            run_id,
            source.source_ref,
            acquired,
            duplicates,
            processed,
            failures,
            reviews,
            applied,
            cursor_after,
        )

    def replay(self, source_ref: str) -> int:
        rows = self.store.list_inbound_messages(limit=1_000_000)
        count = 0
        for row in rows:
            if row["source_ref"] == source_ref:
                self.process_message(row["id"])
                count += 1
        return count

    def inspect(self, *, message_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        result = []
        for row in self.store.list_inbound_messages(limit=limit, message_id=message_id):
            item = {
                key: row[key]
                for key in (
                    "id",
                    "source_ref",
                    "source_identity",
                    "content_hash",
                    "message_id",
                    "sender",
                    "subject",
                    "parse_status",
                    "parse_reason",
                    "text_preview",
                    "processed_at",
                )
            }
            with self.store.connect() as connection:
                classification = connection.execute(
                    "SELECT kind, reasons_json, signals_json FROM inbound_classifications "
                    "WHERE message_id = ? ORDER BY occurred_at DESC LIMIT 1",
                    (row["id"],),
                ).fetchone()
                correlations = connection.execute(
                    "SELECT match_kind, entity_type, entity_id, evidence_json, trust_status "
                    "FROM inbound_correlations WHERE message_id = ?",
                    (row["id"],),
                ).fetchall()
            if classification:
                item["classification"] = classification["kind"]
                item["classification_reasons"] = json.loads(classification["reasons_json"])
                item["classification_signals"] = json.loads(classification["signals_json"])
            item["correlations"] = [
                {
                    "match_kind": correlation["match_kind"],
                    "entity_type": correlation["entity_type"],
                    "entity_id": correlation["entity_id"],
                    "evidence": json.loads(correlation["evidence_json"]),
                    "trust_status": correlation["trust_status"],
                }
                for correlation in correlations
            ]
            result.append(item)
        return result

    def review_queue(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.store.list_inbound_reviews()]

    def decide(self, action_id: str, decision: str, reason: str) -> None:
        self.store.decide_inbound_action(action_id, decision, reason)

    def stats(self) -> dict[str, int]:
        return self.store.inbound_stats()
