"""Provider profile orchestration without owning campaign or inbound state."""

from __future__ import annotations

import imaplib
import ipaddress
import json
import smtplib
import ssl

from mailforge.application.inbound_sources import IMAPConnectionConfig, ImapSource
from mailforge.domain.provider import (
    AuthenticationMethod,
    ProviderDiagnostic,
    ProviderDiagnosticStatus,
    ProviderKind,
    ProviderProfile,
    TLSMode,
)
from mailforge.infrastructure.campaign_store import CampaignStore
from mailforge.infrastructure.credentials import (
    CredentialResolver,
    UnsupportedAuthenticationMechanism,
)
from mailforge.infrastructure.smtp import LiveSendGate, SMTPConnectionPool, SMTPTransportConfig


def _profile(row) -> ProviderProfile:
    return ProviderProfile(
        ref=row["ref"],
        kind=ProviderKind(row["kind"]),
        host=row["host"],
        port=row["port"],
        username=row["username"],
        credential_ref=row["credential_ref"],
        tls_mode=TLSMode(row["tls_mode"]),
        auth_method=AuthenticationMethod(row["auth_method"]),
        mailbox=row["mailbox"] or "INBOX",
        sender_policy_ref=row["sender_policy_ref"],
        timeout=row["timeout_seconds"],
        max_message_bytes=row["max_message_bytes"],
        ca_file=row["ca_file"],
        enabled=bool(row["enabled"]),
        metadata=json.loads(row["metadata_json"]),
    )


def _loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class ProviderService:
    """Build explicit runtime adapters from secret-free durable profiles."""

    def __init__(self, store: CampaignStore):
        self.store = store

    def profile(self, ref: str) -> ProviderProfile:
        return _profile(self.store.get_provider_profile(ref))

    def list_profiles(self, *, kind: ProviderKind | None = None) -> list[dict[str, object]]:
        return [
            {
                "ref": row["ref"],
                "kind": row["kind"],
                "host": row["host"],
                "port": row["port"],
                "username": row["username"],
                "credential_ref": row["credential_ref"],
                "tls_mode": row["tls_mode"],
                "auth_method": row["auth_method"],
                "mailbox": row["mailbox"],
                "sender_policy_ref": row["sender_policy_ref"],
                "enabled": bool(row["enabled"]),
            }
            for row in self.store.list_provider_profiles(kind=kind.value if kind else None)
        ]

    def validate(self, ref: str) -> dict[str, object]:
        profile = self.profile(ref)
        profile.validate()
        status = ProviderDiagnosticStatus.CONFIGURATION_VALID
        result = {
            "profile_ref": ref,
            "status": status.value,
            "kind": profile.kind.value,
            "tls_mode": profile.tls_mode.value,
            "auth_method": profile.auth_method.value,
            "external_contact": not _loopback(profile.host),
        }
        self.store.record_provider_diagnostic(
            ProviderDiagnostic(
                ref,
                status,
                "configuration",
                "provider profile is structurally valid; no network contact performed",
            )
        )
        return result

    def credential_status(self, ref: str, resolver: CredentialResolver) -> dict[str, object]:
        profile = self.profile(ref)
        ready = bool(profile.credential_ref and resolver.available(profile.credential_ref))
        status = (
            ProviderDiagnosticStatus.CREDENTIAL_READY
            if ready
            else ProviderDiagnosticStatus.CREDENTIAL_MISSING
        )
        self.store.record_provider_diagnostic(
            ProviderDiagnostic(
                ref,
                status,
                "configuration",
                "symbolic reference is available" if ready else "symbolic reference is unavailable",
            )
        )
        return {
            "profile_ref": ref,
            "status": status.value,
            "credential_ref": profile.credential_ref,
            "available": ready,
        }

    def smtp_pool(
        self,
        ref: str,
        resolver: CredentialResolver,
        *,
        gate: LiveSendGate,
    ) -> SMTPConnectionPool:
        profile = self.profile(ref)
        if profile.kind is not ProviderKind.SMTP:
            raise ValueError(f"provider profile is not SMTP: {ref}")
        if not profile.enabled:
            raise ValueError(f"provider profile is disabled: {ref}")
        return SMTPConnectionPool(
            [
                SMTPTransportConfig(
                    host=profile.host,
                    port=profile.port,
                    username=profile.username,
                    credential_ref=profile.credential_ref,
                    auth_method=profile.auth_method,
                    provider_ref=profile.ref,
                    timeout=profile.timeout,
                    use_tls=True,
                    implicit_tls=profile.tls_mode is TLSMode.IMPLICIT,
                    ca_file=profile.ca_file,
                )
            ],
            gate=gate,
            credential_resolver=resolver,
        )

    def imap_source(self, ref: str, resolver: CredentialResolver) -> ImapSource:
        profile = self.profile(ref)
        if profile.kind is not ProviderKind.IMAP:
            raise ValueError(f"provider profile is not IMAP: {ref}")
        if not profile.enabled:
            raise ValueError(f"provider profile is disabled: {ref}")
        return ImapSource(
            profile.ref,
            IMAPConnectionConfig(
                host=profile.host,
                port=profile.port,
                username=profile.username,
                credential_ref=profile.credential_ref,
                auth_method=profile.auth_method,
                mailbox=profile.mailbox,
                timeout=profile.timeout,
                ca_file=profile.ca_file,
                max_message_bytes=profile.max_message_bytes,
            ),
            credential_resolver=resolver,
        )

    def local_protocol_diagnostic(
        self,
        ref: str,
        resolver: CredentialResolver,
    ) -> dict[str, object]:
        profile = self.profile(ref)
        if not _loopback(profile.host):
            diagnostic = ProviderDiagnostic(
                ref,
                ProviderDiagnosticStatus.EXTERNAL_UNVERIFIED,
                "local_protocol",
                "external provider contact is disabled; use a loopback fixture",
            )
            self.store.record_provider_diagnostic(diagnostic)
            return diagnostic.to_dict()
        pool: SMTPConnectionPool | None = None
        try:
            if profile.kind is ProviderKind.SMTP:
                pool = self.smtp_pool(ref, resolver, gate=LiveSendGate(enabled=True))
                health = pool.health_check()
                if not all(health.values()):
                    raise ConnectionError("loopback SMTP health check failed")
                capabilities = tuple(sorted(pool.last_transaction.capabilities))
                diagnostic = ProviderDiagnostic(
                    ref,
                    ProviderDiagnosticStatus.LOCAL_PROTOCOL_PASSED,
                    "local_protocol",
                    "loopback SMTP TLS/authentication/health check passed",
                    capabilities,
                )
            else:
                source = self.imap_source(ref, resolver)
                list(source.messages(limit=0))
                diagnostic = ProviderDiagnostic(
                    ref,
                    ProviderDiagnosticStatus.LOCAL_PROTOCOL_PASSED,
                    "local_protocol",
                    "loopback IMAP TLS/authentication/read-only selection passed",
                    source.last_capabilities,
                )
        except UnsupportedAuthenticationMechanism as error:
            diagnostic = ProviderDiagnostic(
                ref,
                ProviderDiagnosticStatus.AUTHENTICATION_UNSUPPORTED,
                "local_protocol",
                type(error).__name__,
            )
        except (imaplib.IMAP4.error, smtplib.SMTPAuthenticationError) as error:
            diagnostic = ProviderDiagnostic(
                ref,
                ProviderDiagnosticStatus.AUTHENTICATION_FAILED,
                "local_protocol",
                type(error).__name__,
            )
        except ssl.SSLError as error:
            diagnostic = ProviderDiagnostic(
                ref,
                ProviderDiagnosticStatus.TLS_FAILED,
                "local_protocol",
                type(error).__name__,
            )
        except OSError as error:
            diagnostic = ProviderDiagnostic(
                ref,
                ProviderDiagnosticStatus.CONNECTION_FAILED,
                "local_protocol",
                type(error).__name__,
            )
        except Exception as error:
            diagnostic = ProviderDiagnostic(
                ref,
                ProviderDiagnosticStatus.LOCAL_PROTOCOL_FAILED,
                "local_protocol",
                type(error).__name__,
            )
        finally:
            if pool is not None:
                pool.close()
        self.store.record_provider_diagnostic(diagnostic)
        return diagnostic.to_dict()

    def diagnostics(self, ref: str | None = None) -> list[dict[str, object]]:
        rows = self.store.list_provider_diagnostics(profile_ref=ref)
        return [
            {
                "id": row["id"],
                "profile_ref": row["profile_ref"],
                "status": row["status"],
                "phase": row["phase"],
                "detail": row["detail"],
                "capabilities": json.loads(row["capabilities_json"]),
                "observed_at": row["observed_at"],
            }
            for row in rows
        ]
