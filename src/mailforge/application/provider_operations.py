"""Canonical provider account, OAuth, diagnostics, and controlled-test service."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import secrets
import ssl
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from mailforge.application.outbound_proof import OutboundMessageProof, verify_proof
from mailforge.application.providers import ProviderService
from mailforge.domain.provider import (
    AuthenticationMethod,
    ExternalOperationGrant,
    ProviderCapability,
    ProviderDiagnostic,
    ProviderDiagnosticStatus,
    ProviderKind,
    ProviderOperation,
    ProviderProfile,
    TLSMode,
)
from mailforge.infrastructure.campaign_store import CampaignStore
from mailforge.infrastructure.credentials import (
    CredentialError,
    CredentialResolver,
    EnvironmentCredentialResolver,
    OAuthTokenMaterial,
)
from mailforge.infrastructure.smtp import (
    DeliveryStatus,
    LiveSendGate,
    deliver,
)

GOOGLE_MAIL_SCOPE = "https://mail.google.com/"
MICROSOFT_SMTP_SCOPE = "https://outlook.office.com/SMTP.Send"
MICROSOFT_IMAP_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All"


def _loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _sanitize_detail(value: object) -> str:
    detail = str(value)
    lowered = detail.lower()
    if any(marker in lowered for marker in ("token", "password", "secret", "bearer")):
        return "provider operation failed with redacted sensitive detail"
    return detail[:500]


def provider_capabilities() -> tuple[ProviderCapability, ...]:
    return (
        ProviderCapability(
            "generic_smtp",
            "Custom SMTP",
            (ProviderKind.SMTP,),
            (AuthenticationMethod.PASSWORD, AuthenticationMethod.OAUTH2),
            (TLSMode.IMPLICIT, TLSMode.STARTTLS),
            (("port", 587),),
            scopes=(),
            restrictions=("The server must advertise STARTTLS before authentication.",),
            docs=("https://docs.python.org/3/library/smtplib.html",),
        ),
        ProviderCapability(
            "generic_imap",
            "Custom IMAP",
            (ProviderKind.IMAP,),
            (AuthenticationMethod.PASSWORD, AuthenticationMethod.OAUTH2),
            (TLSMode.IMPLICIT,),
            (),
            (("port", 993), ("mailbox", "INBOX")),
            docs=("https://docs.python.org/3/library/imaplib.html",),
        ),
        ProviderCapability(
            "google_workspace",
            "Google Gmail / Workspace",
            (ProviderKind.SMTP, ProviderKind.IMAP),
            (AuthenticationMethod.OAUTH2,),
            (TLSMode.IMPLICIT, TLSMode.STARTTLS),
            (("host", "smtp.gmail.com"), ("port", 587)),
            (("host", "imap.gmail.com"), ("port", 993), ("mailbox", "INBOX")),
            scopes=(GOOGLE_MAIL_SCOPE,),
            restrictions=(
                "OAuth consent and Google app verification are provider-side requirements.",
            ),
            docs=(
                "https://developers.google.com/workspace/gmail/imap/imap-smtp",
                "https://developers.google.com/workspace/gmail/imap/xoauth2-protocol",
            ),
        ),
        ProviderCapability(
            "microsoft_365",
            "Microsoft 365 / Outlook",
            (ProviderKind.SMTP, ProviderKind.IMAP),
            (AuthenticationMethod.OAUTH2,),
            (TLSMode.IMPLICIT, TLSMode.STARTTLS),
            (("host", "smtp.office365.com"), ("port", 587)),
            (("host", "outlook.office365.com"), ("port", 993), ("mailbox", "INBOX")),
            scopes=(MICROSOFT_SMTP_SCOPE, MICROSOFT_IMAP_SCOPE, "offline_access"),
            restrictions=(
                "SMTP AUTH must be enabled for the tenant/mailbox; "
                "basic auth is not a modern default.",
            ),
            docs=(
                "https://learn.microsoft.com/en-us/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth",
                "https://learn.microsoft.com/en-us/exchange/clients-and-mobile-in-exchange-online/authenticated-client-smtp-submission",
            ),
        ),
    )


@dataclass(frozen=True)
class OAuthFlow:
    flow_id: str
    account_ref: str
    provider_key: str
    client_id: str
    authorization_endpoint: str
    token_endpoint: str
    redirect_uri: str
    scopes: tuple[str, ...]
    state: str
    code_verifier: str
    code_challenge: str
    expires_at: str

    def authorization_url(self) -> str:
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(self.scopes),
            "state": self.state,
            "code_challenge": self.code_challenge,
            "code_challenge_method": "S256",
            "response_mode": "query",
        }
        return f"{self.authorization_endpoint}?{urllib.parse.urlencode(params)}"


class OAuthProtocolService:
    """Provider-neutral authorization-code/PKCE protocol seam for local fixtures."""

    def __init__(self, store: CampaignStore):
        self.store = store
        self._flows: dict[str, OAuthFlow] = {}

    @staticmethod
    def _challenge(verifier: str) -> str:
        return (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )

    @staticmethod
    def _validate_redirect(redirect_uri: str) -> None:
        parsed = urllib.parse.urlsplit(redirect_uri)
        if parsed.scheme not in {"http", "https"} or not _loopback(parsed.hostname or ""):
            raise ValueError("OAuth redirect must be an explicit loopback HTTP(S) URI")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("OAuth redirect must not contain credentials, query, or fragment")

    def start(
        self,
        *,
        account_ref: str,
        provider_key: str,
        client_id: str,
        redirect_uri: str,
        scopes: tuple[str, ...],
        authorization_endpoint: str,
        token_endpoint: str,
        ttl_seconds: int = 600,
    ) -> dict[str, object]:
        self._validate_redirect(redirect_uri)
        if ttl_seconds <= 0:
            raise ValueError("OAuth flow lifetime must be positive")
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        flow = OAuthFlow(
            flow_id=secrets.token_urlsafe(16),
            account_ref=account_ref,
            provider_key=provider_key,
            client_id=client_id,
            authorization_endpoint=authorization_endpoint,
            token_endpoint=token_endpoint,
            redirect_uri=redirect_uri,
            scopes=tuple(dict.fromkeys(scopes)),
            state=state,
            code_verifier=verifier,
            code_challenge=self._challenge(verifier),
            expires_at=(datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat(),
        )
        self._flows[state] = flow
        self.store.record_provider_oauth_flow(
            flow_id=flow.flow_id,
            account_ref=account_ref,
            state_hash=hashlib.sha256(state.encode()).hexdigest(),
            pkce_challenge=flow.code_challenge,
            redirect_uri=redirect_uri,
            scopes=flow.scopes,
            expires_at=flow.expires_at,
        )
        return {
            "flow_id": flow.flow_id,
            "authorization_url": flow.authorization_url(),
            "redirect_uri": redirect_uri,
            "expires_at": flow.expires_at,
            "scopes": list(flow.scopes),
        }

    def complete(
        self, *, state: str, redirect_uri: str, code: str, identity: str | None = None
    ) -> dict[str, object]:
        flow = self._flows.pop(state, None)
        if flow is None:
            raise ValueError("OAuth state is unknown, expired, or already consumed")
        if redirect_uri != flow.redirect_uri:
            raise ValueError("OAuth redirect URI does not match the started flow")
        if not code or len(code) > 4096:
            raise ValueError("OAuth callback code is missing or invalid")
        expiry = datetime.fromisoformat(flow.expires_at)
        if expiry <= datetime.now(UTC):
            raise ValueError("OAuth authorization flow has expired")
        self.store.complete_provider_oauth_flow(
            flow.flow_id, identity=identity or "authorized-account"
        )
        return {
            "flow_id": flow.flow_id,
            "account_ref": flow.account_ref,
            "status": "authorization_code_received",
            "identity": identity,
            "token_exchange_required": True,
        }

    @staticmethod
    def refresh(
        token: OAuthTokenMaterial,
        exchange: Callable[[str], OAuthTokenMaterial],
        *,
        expected_identity: str | None = None,
    ) -> OAuthTokenMaterial:
        """Refresh through an injected token endpoint without persisting token bytes."""

        if not token.refresh_token:
            raise CredentialError("OAuth refresh token is unavailable; reauthorization required")
        refreshed = exchange(token.refresh_token)
        if expected_identity and refreshed.identity and refreshed.identity != expected_identity:
            raise PermissionError("OAuth refresh returned a different account identity")
        return refreshed


class ProviderOperationsService:
    """One application authority for account operations and bounded diagnostics."""

    def __init__(self, store: CampaignStore):
        self.store = store
        self.providers = ProviderService(store)
        self.oauth = OAuthProtocolService(store)

    def capabilities(self) -> list[dict[str, object]]:
        return [item.to_dict() for item in provider_capabilities()]

    def account(self, ref: str) -> dict[str, Any]:
        return self.store.get_provider_account(ref)

    def accounts(self) -> list[dict[str, Any]]:
        return self.store.list_provider_accounts()

    def register(self, profile: ProviderProfile, **account_options: object) -> dict[str, Any]:
        self.store.register_provider_profile(profile)
        return self.store.upsert_provider_account(profile.ref, **account_options)

    def validate(self, ref: str) -> dict[str, object]:
        result = self.providers.validate(ref)
        self.store.set_provider_account_status(ref, "configuration_validated")
        result["readiness"] = "configuration_validated"
        result["external_access"] = False
        return result

    def issue_grant(
        self,
        ref: str,
        operation: ProviderOperation,
        *,
        purpose: str,
        sender: str | None = None,
        recipient: str | None = None,
        expires_at: str,
        proof_hash: str | None = None,
    ) -> dict[str, object]:
        """Issue a server-owned, persisted grant for one bounded operation."""
        profile = self.providers.profile(ref)
        authorization_ref = f"grant-{secrets.token_urlsafe(24)}"
        grant = ExternalOperationGrant(
            account_ref=ref,
            operation=operation,
            host=profile.host,
            port=profile.port,
            purpose=purpose,
            sender=sender,
            recipient=recipient,
            max_submissions=1 if operation is ProviderOperation.CONTROLLED_TEST_SEND else 0,
            expires_at=expires_at,
            authorization_ref=authorization_ref,
            proof_hash=proof_hash,
        )
        grant.validate()
        if operation is ProviderOperation.CONTROLLED_TEST_SEND:
            proof = self.store.get_outbound_proof_by_hash(proof_hash or "")
            if proof is None or proof["operation_kind"] != "controlled_test":
                raise PermissionError("controlled test authorization requires a canonical proof")
            if proof["sender"] != sender or proof["recipient"] != recipient:
                raise PermissionError("authorization does not match the canonical proof")
        self.store.record_provider_operation_grant(
            account_ref=grant.account_ref,
            operation=grant.operation.value,
            host=grant.host,
            port=grant.port,
            purpose=grant.purpose,
            sender=grant.sender,
            recipient=grant.recipient,
            max_submissions=grant.max_submissions,
            expires_at=grant.expires_at,
            authorization_ref=grant.authorization_ref,
            proof_hash=grant.proof_hash,
        )
        return {
            "authorization_ref": authorization_ref,
            "account_ref": ref,
            "operation": operation.value,
            "purpose": purpose,
            "sender": sender,
            "recipient": recipient,
            "expires_at": expires_at,
            "external_access": False,
        }

    def grant_for_authorization(
        self,
        ref: str,
        operation: ProviderOperation,
        *,
        authorization_ref: str,
        purpose: str,
        sender: str | None = None,
        recipient: str | None = None,
        expires_at: str,
        proof_hash: str | None = None,
    ) -> ExternalOperationGrant:
        """Resolve and match a grant issued by the server-side authority."""
        row = self.store.get_provider_operation_grant(authorization_ref, account_ref=ref)
        if row is None:
            raise PermissionError("operation authorization was not issued by Mailforge")
        try:
            granted_operation = ProviderOperation(row["operation"])
        except ValueError as error:
            raise PermissionError("operation authorization is invalid") from error
        grant = ExternalOperationGrant(
            account_ref=row["account_ref"],
            operation=granted_operation,
            host=row["host"],
            port=int(row["port"]),
            purpose=row["purpose"],
            sender=row["sender"],
            recipient=row["recipient"],
            max_submissions=int(row["max_submissions"]),
            expires_at=row["expires_at"],
            authorization_ref=row["authorization_ref"],
            proof_hash=row.get("proof_hash"),
        )
        grant.validate()
        if (
            grant.operation is not operation
            or grant.purpose != purpose
            or grant.sender != sender
            or grant.recipient != recipient
            or grant.expires_at != expires_at
            or grant.proof_hash != proof_hash
        ):
            raise PermissionError("operation request does not match its issued authorization")
        return grant

    def _require_grant(
        self, ref: str, operation: ProviderOperation, grant: ExternalOperationGrant | None
    ) -> ExternalOperationGrant:
        if grant is None:
            raise PermissionError("explicit external operation authorization is required")
        grant.validate()
        if grant.account_ref != ref or grant.operation is not operation:
            raise PermissionError("operation authorization does not match the selected account")
        profile = self.providers.profile(ref)
        if grant.host != profile.host or grant.port != profile.port:
            raise PermissionError("operation authorization endpoint does not match the account")
        stored = self.store.get_provider_operation_grant(grant.authorization_ref, account_ref=ref)
        if stored is None:
            self.store.record_provider_operation_grant(
                account_ref=grant.account_ref,
                operation=grant.operation.value,
                host=grant.host,
                port=grant.port,
                purpose=grant.purpose,
                sender=grant.sender,
                recipient=grant.recipient,
                max_submissions=grant.max_submissions,
                expires_at=grant.expires_at,
                authorization_ref=grant.authorization_ref,
                proof_hash=grant.proof_hash,
            )
        elif (
            stored["operation"] != grant.operation.value
            or stored["host"] != grant.host
            or int(stored["port"]) != grant.port
            or stored["purpose"] != grant.purpose
            or stored["sender"] != grant.sender
            or stored["recipient"] != grant.recipient
            or stored["expires_at"] != grant.expires_at
            or stored.get("proof_hash") != grant.proof_hash
        ):
            raise PermissionError("operation authorization does not match its stored grant")
        return grant

    def diagnostic(
        self,
        ref: str,
        operation: ProviderOperation,
        *,
        resolver: CredentialResolver | None = None,
        grant: ExternalOperationGrant | None = None,
    ) -> dict[str, object]:
        profile = self.providers.profile(ref)
        external = not _loopback(profile.host)
        if external:
            self._require_grant(ref, operation, grant)
        if operation is ProviderOperation.OUTBOUND_READINESS:
            profile.validate()
            ready = profile.kind is ProviderKind.SMTP and bool(profile.sender_policy_ref)
            status = (
                ProviderDiagnosticStatus.READY
                if ready
                else ProviderDiagnosticStatus.CONFIGURATION_VALID
            )
            detail = (
                "outbound configuration is ready for an explicit controlled operation"
                if ready
                else "outbound sender policy is not configured"
            )
            diagnostic = ProviderDiagnostic(
                ref, status, "readiness", detail, operation=operation, external_access=external
            )
        elif operation is ProviderOperation.INBOUND_READINESS:
            profile.validate()
            if profile.kind is not ProviderKind.IMAP:
                raise ValueError("inbound readiness requires an IMAP account")
            diagnostic = ProviderDiagnostic(
                ref,
                ProviderDiagnosticStatus.CONFIGURATION_VALID,
                "readiness",
                "inbound mailbox configuration is valid; no message content fetched",
                operation=operation,
                external_access=external,
            )
        else:
            result = (
                self.providers.local_protocol_diagnostic(
                    ref, resolver or EnvironmentCredentialResolver()
                )
                if not external
                else self._external_diagnostic(
                    ref, resolver or EnvironmentCredentialResolver(), operation
                )
            )
            return result
        self.store.record_provider_diagnostic(diagnostic)
        return diagnostic.to_dict()

    def inbound_source(
        self, ref: str, resolver: CredentialResolver, *, grant: ExternalOperationGrant | None = None
    ):
        """Return the canonical read-only IMAP source after an explicit grant."""
        profile = self.providers.profile(ref)
        if profile.kind is not ProviderKind.IMAP:
            raise ValueError("inbound acquisition requires an IMAP account")
        if not _loopback(profile.host):
            self._require_grant(ref, ProviderOperation.INBOUND_READINESS, grant)
        return self.providers.imap_source(ref, resolver)

    def _external_diagnostic(
        self, ref: str, resolver: CredentialResolver, operation: ProviderOperation
    ) -> dict[str, object]:
        profile = self.providers.profile(ref)
        try:
            if profile.kind is ProviderKind.SMTP:
                pool = self.providers.smtp_pool(ref, resolver, gate=LiveSendGate(enabled=True))
                try:
                    health = pool.health_check()
                    caps = pool.last_transaction.capabilities
                    passed = all(health.values())
                finally:
                    pool.close()
            else:
                source = self.providers.imap_source(ref, resolver)
                list(source.messages(limit=0))
                caps = source.last_capabilities
                passed = True
            status = (
                ProviderDiagnosticStatus.AUTHENTICATION_VERIFIED
                if passed
                else ProviderDiagnosticStatus.CONNECTION_FAILED
            )
            detail = (
                "explicit provider connection and authentication check passed"
                if passed
                else "provider connection health check failed"
            )
        except ssl.SSLError:
            status, detail, caps = (
                ProviderDiagnosticStatus.TLS_FAILED,
                "provider TLS verification failed",
                (),
            )
        except CredentialError:
            status, detail, caps = (
                ProviderDiagnosticStatus.CREDENTIAL_MISSING,
                "provider credential is unavailable or expired",
                (),
            )
        except Exception as error:
            status, detail, caps = (
                ProviderDiagnosticStatus.CONNECTION_FAILED,
                _sanitize_detail(type(error).__name__),
                (),
            )
        diagnostic = ProviderDiagnostic(
            ref,
            status,
            "external_diagnostic",
            detail,
            tuple(caps),
            operation=operation,
            external_access=True,
        )
        self.store.record_provider_diagnostic(diagnostic)
        self.store.set_provider_account_status(
            ref,
            "authentication_verified"
            if status is ProviderDiagnosticStatus.AUTHENTICATION_VERIFIED
            else "failed",
        )
        return diagnostic.to_dict()

    def configure_inbound(
        self,
        ref: str,
        *,
        mailbox: str,
        max_fetch: int = 100,
        sync_enabled: bool = True,
        mutation_enabled: bool = False,
    ) -> dict[str, object]:
        return self.store.upsert_provider_inbound_settings(
            ref,
            mailbox=mailbox,
            max_fetch=max_fetch,
            sync_enabled=sync_enabled,
            mutation_enabled=mutation_enabled,
        )

    def controlled_test_send(
        self,
        *,
        ref: str,
        sender: str,
        recipient: str,
        message: str | None,
        idempotency_key: str,
        grant: ExternalOperationGrant,
        resolver: CredentialResolver | None = None,
        message_revision_id: str | None = None,
        proof_id: str | None = None,
        proof_hash: str | None = None,
    ) -> dict[str, Any]:
        self._require_grant(ref, ProviderOperation.CONTROLLED_TEST_SEND, grant)
        account = self.account(ref)
        if not account["profile_enabled"] or not account["outbound_enabled"]:
            raise PermissionError("provider account is disabled for outbound operations")
        if account.get("sender_email") and sender.lower() != account["sender_email"].lower():
            raise PermissionError("sender does not match the selected provider account")
        if grant.sender and sender.lower() != grant.sender.lower():
            raise PermissionError("sender does not match the operation authorization")
        if grant.recipient and recipient.lower() != grant.recipient.lower():
            raise PermissionError("recipient does not match the operation authorization")
        if not grant.proof_hash or (proof_hash and proof_hash != grant.proof_hash):
            raise PermissionError("controlled test send requires the proof bound to its grant")
        proof_row = self.store.get_outbound_proof(proof_id) if proof_id else None
        if proof_row is None:
            proof_row = self.store.get_outbound_proof_by_hash(grant.proof_hash)
        if proof_row is None or proof_row["proof_hash"] != grant.proof_hash:
            raise PermissionError("controlled test proof is missing or does not match its grant")
        if proof_row["mime_bytes"] is None:
            raise PermissionError("controlled test proof has no canonical MIME artifact")
        proof = OutboundMessageProof(
            proof_id=proof_row["id"],
            proof_hash=proof_row["proof_hash"],
            content_hash=proof_row["content_hash"],
            payload=proof_row["proof"],
        )
        verify_proof(proof, bytes(proof_row["mime_bytes"]))
        if proof.sender != sender or proof.recipient != recipient:
            raise PermissionError("send addresses do not match the canonical proof")
        if message is not None and message.encode() != bytes(proof_row["mime_bytes"]):
            raise PermissionError("caller-supplied content does not match the canonical proof")
        if message_revision_id is not None and message_revision_id != proof.message_revision_id:
            raise PermissionError("message revision does not match the canonical proof")
        message_bytes = bytes(proof_row["mime_bytes"])
        reservation, owns_reservation = self.store.reserve_provider_test_submission(
            account_ref=ref,
            authorization_ref=grant.authorization_ref,
            idempotency_key=idempotency_key,
            sender=sender,
            recipient=recipient,
            message_digest=hashlib.sha256(message_bytes).hexdigest(),
            message_revision_id=proof.message_revision_id,
            proof_id=proof.proof_id,
            sender_scope_ref=account.get("sender_policy_ref") or "",
            communication_purpose="transactional",
        )
        if not owns_reservation:
            return reservation
        pool = None
        try:
            pool = self.providers.smtp_pool(
                ref, resolver or EnvironmentCredentialResolver(), gate=LiveSendGate(enabled=True)
            )
            result = deliver(
                pool, sender, recipient, message_bytes, gate=LiveSendGate(enabled=True)
            )
            transaction = pool.last_transaction
            detail = _sanitize_detail(result.detail)
            row = self.store.finalize_provider_test_submission(
                account_ref=ref,
                authorization_ref=grant.authorization_ref,
                idempotency_key=idempotency_key,
                status=(
                    "provider_accepted"
                    if result.status is DeliveryStatus.ACCEPTED
                    else result.status.value
                ),
                phase=transaction.phase.value if transaction.phase else result.phase.value,
                detail=detail,
                response_code=result.response_code or transaction.response_code,
            )
            self.store.record_provider_diagnostic(
                ProviderDiagnostic(
                    ref,
                    ProviderDiagnosticStatus.AUTHENTICATION_VERIFIED
                    if result.status is DeliveryStatus.ACCEPTED
                    else ProviderDiagnosticStatus.CONNECTION_FAILED,
                    transaction.phase.value,
                    detail,
                    operation=ProviderOperation.CONTROLLED_TEST_SEND,
                    external_access=True,
                    correlation_id=idempotency_key,
                )
            )
            return row
        except Exception as error:
            transaction = getattr(pool, "last_transaction", None)
            self.store.finalize_provider_test_submission(
                account_ref=ref,
                authorization_ref=grant.authorization_ref,
                idempotency_key=idempotency_key,
                status="ambiguous",
                phase=getattr(getattr(transaction, "phase", None), "value", "connection_error"),
                detail=_sanitize_detail(error),
                response_code=getattr(transaction, "response_code", None),
            )
            raise
        finally:
            if pool is not None:
                pool.close()

    def diagnostics(self, ref: str | None = None) -> list[dict[str, object]]:
        return [
            {
                "id": row["id"],
                "profile_ref": row["profile_ref"],
                "status": row["status"],
                "phase": row["phase"],
                "detail": row["detail"],
                "capabilities": json.loads(row["capabilities_json"]),
                "observed_at": row["observed_at"],
                "operation": row["operation"],
                "external_access": bool(row["external_access"]),
                "correlation_id": row["correlation_id"],
                "metadata": json.loads(row["metadata_json"]),
            }
            for row in self.store.list_provider_diagnostics(profile_ref=ref)
        ]
