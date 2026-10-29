"""Folder-aware mailbox synchronization and fixture-only remote actions."""

from __future__ import annotations

import ipaddress
import json
from datetime import UTC, datetime
from typing import Any

from mailforge.application.inbound import InboundService
from mailforge.application.inbound_operations import InboundOperations
from mailforge.application.inbound_sources import CursorInvalidated, SourceError
from mailforge.application.provider_operations import ProviderOperationsService
from mailforge.domain.provider import ExternalOperationGrant, ProviderOperation
from mailforge.infrastructure.campaign_store import CampaignStore
from mailforge.infrastructure.credentials import CredentialResolver, EnvironmentCredentialResolver


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class MailboxOperationsError(ValueError):
    def __init__(self, message: str, code: str = "mailbox_operation_invalid"):
        super().__init__(message)
        self.code = code


class MailboxOperationsService:
    """Thin mailbox workbench facade over inbound/provider authorities."""

    def __init__(
        self,
        store: CampaignStore,
        *,
        inbound: InboundService,
        inbound_operations: InboundOperations,
        provider_operations: ProviderOperationsService,
    ):
        self.store = store
        self.inbound = inbound
        self.inbound_operations = inbound_operations
        self.provider_operations = provider_operations

    @staticmethod
    def source_ref(account_ref: str, mailbox: str) -> str:
        return account_ref if mailbox == "INBOX" else f"{account_ref}::{mailbox}"

    def _account(self, account_ref: str) -> dict[str, Any]:
        try:
            return self.provider_operations.account(account_ref)
        except KeyError as error:
            raise MailboxOperationsError(
                "provider account was not found", "account_not_found"
            ) from error

    def _fixture_account(self, account_ref: str) -> dict[str, Any]:
        account = self._account(account_ref)
        if not _loopback(account["host"]):
            raise MailboxOperationsError(
                "mailbox network access is limited to loopback fixtures in this milestone",
                "external_mailbox_disabled",
            )
        if account["kind"] != "imap":
            raise MailboxOperationsError(
                "mailbox operations require an IMAP account", "protocol_unsupported"
            )
        return account

    def _source(
        self,
        account_ref: str,
        mailbox: str,
        resolver: CredentialResolver | None = None,
    ):
        self._fixture_account(account_ref)
        return self.provider_operations.providers.imap_source(
            account_ref,
            resolver or EnvironmentCredentialResolver(),
            mailbox=mailbox,
            source_ref=self.source_ref(account_ref, mailbox),
        )

    def _grant(
        self, account: dict[str, Any], grant: ExternalOperationGrant, operation: ProviderOperation
    ) -> None:
        if grant.operation is not operation:
            raise MailboxOperationsError(
                "operation authorization does not match the requested action",
                "authorization_mismatch",
            )
        if (
            grant.account_ref != account["ref"]
            or grant.host != account["host"]
            or grant.port != account["port"]
        ):
            raise MailboxOperationsError(
                "operation authorization does not match the selected account",
                "authorization_mismatch",
            )
        try:
            grant.validate()
        except ValueError as error:
            raise MailboxOperationsError(str(error), "authorization_invalid") from error

    def discover(
        self,
        account_ref: str,
        *,
        resolver: CredentialResolver | None = None,
        grant: ExternalOperationGrant | None = None,
    ) -> dict[str, Any]:
        account = self._fixture_account(account_ref)
        if grant is None:
            raise MailboxOperationsError(
                "folder discovery requires explicit authorization", "authorization_required"
            )
        self._grant(account, grant, ProviderOperation.INBOUND_SYNC)
        source = self._source(account_ref, account["inbound"]["mailbox"], resolver)
        try:
            folders = source.discover_mailboxes()
            capabilities = tuple(source.last_capabilities)
            for folder in folders:
                mailbox = str(folder["mailbox"])
                source_ref = self.source_ref(account_ref, mailbox)
                self.store.register_inbound_source(
                    source_ref,
                    "imap",
                    mailbox=mailbox,
                    configuration={"account_ref": account_ref},
                )
                self.store.upsert_inbound_mailbox(
                    account_ref=account_ref,
                    source_ref=source_ref,
                    mailbox=mailbox,
                    parent_mailbox=folder.get("parent_mailbox"),
                    delimiter=folder.get("delimiter"),
                    flags=tuple(folder.get("flags", ())),
                    capabilities=capabilities,
                    selectable=bool(folder.get("selectable", True)),
                    readonly=not bool(account["inbound"].get("mutation_enabled", False)),
                    mutation_enabled=bool(account["inbound"].get("mutation_enabled", False)),
                )
            self.store.update_provider_inbound_state(account_ref, capabilities=capabilities)
            return {
                "account_ref": account_ref,
                "folders": self.store.list_inbound_mailboxes(account_ref),
                "capabilities": list(capabilities),
            }
        except Exception as error:
            raise MailboxOperationsError(
                "folder discovery failed", "folder_discovery_failed"
            ) from error

    def sync(
        self,
        account_ref: str,
        *,
        mailbox: str,
        batch_limit: int = 100,
        retain_raw: bool = True,
        resolver: CredentialResolver | None = None,
        grant: ExternalOperationGrant | None = None,
    ):
        account = self._fixture_account(account_ref)
        if not account["inbound"].get("sync_enabled", True):
            raise MailboxOperationsError(
                "synchronization is disabled for this account", "sync_disabled"
            )
        if grant is None:
            raise MailboxOperationsError(
                "mailbox synchronization requires explicit authorization", "authorization_required"
            )
        self._grant(account, grant, ProviderOperation.INBOUND_SYNC)
        if not 1 <= batch_limit <= 10_000:
            raise MailboxOperationsError("sync batch is outside the bounded limit", "batch_invalid")
        source = self._source(account_ref, mailbox, resolver)
        source_ref = self.source_ref(account_ref, mailbox)
        self.store.register_inbound_source(
            source_ref,
            "imap",
            mailbox=mailbox,
            configuration={"account_ref": account_ref},
        )
        try:
            state = source.mailbox_state(mailbox)
            self.store.upsert_inbound_mailbox(
                account_ref=account_ref,
                source_ref=source_ref,
                mailbox=mailbox,
                capabilities=tuple(state.get("capabilities", ())),
                readonly=not bool(account["inbound"].get("mutation_enabled", False)),
                mutation_enabled=bool(account["inbound"].get("mutation_enabled", False)),
                uidvalidity=str(state["uidvalidity"]),
                uidnext=str(state["uidnext"]) if state.get("uidnext") is not None else None,
                total_count=state.get("total_count"),
                unseen_count=state.get("unseen_count"),
                last_sync_status="running",
                last_sync_error=None,
            )
            report = self.inbound.ingest(source, batch_limit=batch_limit, retain_raw=retain_raw)
            self.store.upsert_inbound_mailbox(
                account_ref=account_ref,
                source_ref=source_ref,
                mailbox=mailbox,
                capabilities=tuple(source.last_capabilities),
                readonly=not bool(account["inbound"].get("mutation_enabled", False)),
                mutation_enabled=bool(account["inbound"].get("mutation_enabled", False)),
                uidvalidity=str(state["uidvalidity"]),
                last_sync_at=_now(),
                last_sync_status="completed",
                last_sync_error=None,
            )
            self.store.update_provider_inbound_state(
                account_ref,
                last_sync_at=_now(),
                last_sync_status="completed",
                last_sync_error=None,
                capabilities=tuple(source.last_capabilities),
            )
            return report
        except CursorInvalidated:
            self.store.upsert_inbound_mailbox(
                account_ref=account_ref,
                source_ref=source_ref,
                mailbox=mailbox,
                last_sync_at=_now(),
                last_sync_status="uidvalidity_changed",
                last_sync_error="mailbox identity changed; resynchronization is required",
            )
            raise
        except Exception as error:
            detail = type(error).__name__
            self.store.upsert_inbound_mailbox(
                account_ref=account_ref,
                source_ref=source_ref,
                mailbox=mailbox,
                last_sync_at=_now(),
                last_sync_status="failed",
                last_sync_error=detail,
            )
            raise

    def execute_action(
        self,
        source_ref: str,
        *,
        action: str,
        authorization_ref: str,
        purpose: str,
        expires_at: str,
        idempotency_key: str,
        message_id: str,
        uidvalidity: str,
        uid: str,
        target_folder: str | None = None,
        flags: tuple[str, ...] = (),
        resolver: CredentialResolver | None = None,
    ) -> dict[str, Any]:
        if action in {"delete", "expunge"}:
            raise MailboxOperationsError(
                "permanent deletion and EXPUNGE are not supported", "action_refused"
            )
        if action not in {"mark_read", "mark_unread", "flag", "unflag", "copy", "move", "archive"}:
            raise MailboxOperationsError("unsupported mailbox action", "action_unsupported")
        row = self.store.get_inbound_message(message_id)
        account_ref = row["account_ref"] or source_ref.split("::", 1)[0]
        account = self._fixture_account(account_ref)
        if not account["inbound"].get("mutation_enabled", False):
            raise MailboxOperationsError(
                "remote mailbox mutation is disabled for this account", "mutation_disabled"
            )
        grant = ExternalOperationGrant(
            account_ref=account_ref,
            operation=ProviderOperation.INBOUND_MUTATION,
            host=account["host"],
            port=account["port"],
            purpose=purpose,
            expires_at=expires_at,
            authorization_ref=authorization_ref,
        )
        self._grant(account, grant, ProviderOperation.INBOUND_MUTATION)
        if row["source_ref"] != source_ref:
            raise MailboxOperationsError(
                "message does not belong to the selected mailbox", "message_scope_mismatch"
            )
        if not uidvalidity.strip() or not uid.strip():
            raise MailboxOperationsError("UID and UIDVALIDITY are required", "identity_invalid")
        if row["remote_uid"] != uid or row["remote_uidvalidity"] != uidvalidity:
            raise MailboxOperationsError(
                "UID identity does not match the selected message",
                "identity_mismatch",
            )
        with self.store.connect() as connection:
            existing = connection.execute(
                "SELECT id FROM inbound_mailbox_actions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        request = self.inbound_operations.request_mailbox_action(
            source_ref,
            action=action,
            authorization_ref=authorization_ref,
            idempotency_key=idempotency_key,
            message_id=message_id,
            target_folder=target_folder,
        )
        if existing:
            if request.get("action_state") == "requested":
                request = self.store.transition_inbound_mailbox_action(
                    request["id"],
                    action_state="requires_review",
                    status="ambiguous",
                    response_detail=(
                        "existing action was not completed; remote state must be reconciled"
                    ),
                )
            request["events"] = self.store.inbound_action_events(request["id"])
            return request
        if request.get("action_state") in {
            "confirmed",
            "refused",
            "failed",
            "ambiguous",
            "requires_review",
        }:
            request["events"] = self.store.inbound_action_events(request["id"])
            return request
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE inbound_mailbox_actions SET account_ref=?,mailbox=?,uidvalidity=?,"
                "uid=?,expected_state=?,action_state='requested' WHERE id=?",
                (
                    account_ref,
                    row["folder"] or "INBOX",
                    uidvalidity,
                    uid,
                    json.dumps({"flags": list(flags)}),
                    request["id"],
                ),
            )
        self.store.transition_inbound_mailbox_action(request["id"], action_state="validated")
        self.store.transition_inbound_mailbox_action(request["id"], action_state="queued")
        source = self._source(account_ref, row["folder"] or "INBOX", resolver)
        try:
            current = source.mailbox_state(row["folder"] or "INBOX")
            if str(current["uidvalidity"]) != uidvalidity:
                raise CursorInvalidated("UIDVALIDITY changed before mailbox action")
            self.store.transition_inbound_mailbox_action(
                request["id"],
                action_state="in_progress",
                attempt_count=1,
                remote_command=f"UID {action.upper()} {uid}",
            )
            result = source.mutate(
                mailbox_name=row["folder"] or "INBOX",
                uid=uid,
                action=action,
                flags=flags,
                target_folder=target_folder,
            )
            observed_flags = (
                source.flags_for(row["folder"] or "INBOX", uid)
                if action in {"mark_read", "mark_unread", "flag", "unflag"}
                else flags
            )
            self.store.update_inbound_message_remote_state(
                message_id,
                flags=observed_flags,
                state="confirmed"
                if action in {"mark_read", "mark_unread", "flag", "unflag"}
                else action,
                observed_at=_now(),
            )
            result_row = self.store.transition_inbound_mailbox_action(
                request["id"],
                action_state="confirmed",
                status="applied",
                observed_state=json.dumps({"flags": list(observed_flags), "result": result}),
                response_detail="loopback fixture command confirmed",
                reconciled_at=_now(),
            )
        except CursorInvalidated as error:
            result_row = self.store.transition_inbound_mailbox_action(
                request["id"],
                action_state="requires_review",
                status="ambiguous",
                response_detail=str(error),
            )
        except (TimeoutError, ConnectionError, OSError) as error:
            result_row = self.store.transition_inbound_mailbox_action(
                request["id"],
                action_state="ambiguous",
                status="ambiguous",
                response_detail=type(error).__name__,
            )
        except SourceError as error:
            state = "refused" if "not supported" in str(error).lower() else "failed"
            result_row = self.store.transition_inbound_mailbox_action(
                request["id"],
                action_state=state,
                status="failed" if state == "failed" else "canceled",
                response_detail=str(error),
            )
        result = dict(result_row)
        result["events"] = self.store.inbound_action_events(request["id"])
        return result

    def action(self, action_id: str) -> dict[str, Any]:
        result = self.inbound_operations.mailbox_action(action_id)
        result["events"] = self.store.inbound_action_events(action_id)
        return result

    def reconcile_action(
        self, action_id: str, *, resolver: CredentialResolver | None = None
    ) -> dict[str, Any]:
        action = self.action(action_id)
        if action.get("action_state") not in {"ambiguous", "requires_review"}:
            raise MailboxOperationsError(
                "only ambiguous or review-required actions may be reconciled",
                "reconciliation_not_required",
            )
        if not action.get("account_ref") or not action.get("mailbox"):
            raise MailboxOperationsError("action is missing mailbox identity", "identity_invalid")
        self._fixture_account(action["account_ref"])
        source = self._source(action["account_ref"], action["mailbox"], resolver)
        state = source.mailbox_state(action["mailbox"])
        if str(state["uidvalidity"]) != str(action.get("uidvalidity")):
            raise MailboxOperationsError(
                "UIDVALIDITY changed; action remains unresolved", "uidvalidity_changed"
            )
        observed_flags = source.flags_for(action["mailbox"], action["uid"])
        if action["action"] in {"mark_read", "mark_unread", "flag", "unflag"}:
            self.store.update_inbound_message_remote_state(
                action["message_id"],
                flags=observed_flags,
                state="confirmed",
                observed_at=_now(),
            )
            result = self.store.transition_inbound_mailbox_action(
                action_id,
                action_state="confirmed",
                status="applied",
                observed_state=json.dumps({"flags": list(observed_flags)}),
                response_detail="loopback fixture state reconciled",
                reconciled_at=_now(),
            )
        else:
            result = self.store.transition_inbound_mailbox_action(
                action_id,
                action_state="requires_review",
                status="ambiguous",
                observed_state=json.dumps({"source_presence": True, "flags": list(observed_flags)}),
                response_detail="source state observed; target-folder outcome remains unverified",
                reconciled_at=_now(),
            )
        result = dict(result)
        result["events"] = self.store.inbound_action_events(action_id)
        return result
