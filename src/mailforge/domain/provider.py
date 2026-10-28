"""Provider configuration and diagnostic vocabulary."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import UTC, datetime


class ProviderKind(enum.StrEnum):
    SMTP = "smtp"
    IMAP = "imap"


class TLSMode(enum.StrEnum):
    IMPLICIT = "implicit_tls"
    STARTTLS = "starttls"


class AuthenticationMethod(enum.StrEnum):
    PASSWORD = "password"
    OAUTH2 = "oauth2"


class ProviderDiagnosticStatus(enum.StrEnum):
    CONFIGURATION_VALID = "configuration_valid"
    CREDENTIAL_READY = "credential_ready"
    CREDENTIAL_MISSING = "credential_missing"
    LOCAL_PROTOCOL_PASSED = "local_protocol_passed"
    LOCAL_PROTOCOL_FAILED = "local_protocol_failed"
    AUTHENTICATION_UNSUPPORTED = "authentication_unsupported"
    AUTHENTICATION_FAILED = "authentication_failed"
    AUTHENTICATION_VERIFIED = "authentication_verified"
    TLS_FAILED = "tls_failed"
    CONNECTION_FAILED = "connection_failed"
    EXTERNAL_UNVERIFIED = "external_unverified"
    READY = "ready"
    REAUTHORIZATION_REQUIRED = "reauthorization_required"


class ProviderOperation(enum.StrEnum):
    CONNECTION = "connection"
    AUTHENTICATION = "authentication"
    OUTBOUND_READINESS = "outbound_readiness"
    INBOUND_READINESS = "inbound_readiness"
    CONTROLLED_TEST_SEND = "controlled_test_send"


@dataclass(frozen=True)
class ProviderCapability:
    """Secret-free provider setup and capability guidance."""

    key: str
    label: str
    protocols: tuple[ProviderKind, ...]
    auth_methods: tuple[AuthenticationMethod, ...]
    tls_modes: tuple[TLSMode, ...]
    outbound_defaults: tuple[tuple[str, object], ...] = ()
    inbound_defaults: tuple[tuple[str, object], ...] = ()
    scopes: tuple[str, ...] = ()
    restrictions: tuple[str, ...] = ()
    docs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "protocols": [item.value for item in self.protocols],
            "auth_methods": [item.value for item in self.auth_methods],
            "tls_modes": [item.value for item in self.tls_modes],
            "outbound_defaults": dict(self.outbound_defaults),
            "inbound_defaults": dict(self.inbound_defaults),
            "scopes": list(self.scopes),
            "restrictions": list(self.restrictions),
            "docs": list(self.docs),
        }


@dataclass(frozen=True)
class ExternalOperationGrant:
    """Narrow, expiring authority for one explicitly selected operation."""

    account_ref: str
    operation: ProviderOperation
    host: str
    port: int
    purpose: str
    sender: str | None = None
    recipient: str | None = None
    max_submissions: int = 0
    expires_at: str = ""
    authorization_ref: str = ""

    def validate(self, *, now: datetime | None = None) -> None:
        if (
            not self.account_ref.strip()
            or not self.host.strip()
            or not self.purpose.strip()
            or not self.authorization_ref.strip()
        ):
            raise ValueError(
                "operation grant requires account, host, purpose, and authorization_ref"
            )
        if not 1 <= self.port <= 65_535:
            raise ValueError("operation grant port must be between 1 and 65535")
        if self.max_submissions < 0:
            raise ValueError("operation grant max_submissions must not be negative")
        try:
            expiry = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("operation grant expiry must be an ISO timestamp") from error
        if expiry.tzinfo is None:
            raise ValueError("operation grant expiry must include a timezone")
        if expiry <= (now or datetime.now(UTC)):
            raise ValueError("operation grant has expired")
        if self.operation is ProviderOperation.CONTROLLED_TEST_SEND:
            if not self.sender or not self.recipient or self.max_submissions != 1:
                raise ValueError(
                    "controlled test send requires sender, recipient, and one submission"
                )


@dataclass(frozen=True)
class ProviderProfile:
    """Secret-free provider connection profile.

    Authentication material is resolved at execution time from ``credential_ref``;
    it is never part of this durable value.
    """

    ref: str
    kind: ProviderKind
    host: str
    port: int
    username: str | None
    credential_ref: str | None
    tls_mode: TLSMode
    auth_method: AuthenticationMethod = AuthenticationMethod.PASSWORD
    mailbox: str = "INBOX"
    sender_policy_ref: str | None = None
    timeout: float = 30.0
    max_message_bytes: int = 10 * 1024 * 1024
    ca_file: str | None = None
    enabled: bool = True
    metadata: dict[str, object] | None = None

    def validate(self) -> None:
        if not self.ref.strip():
            raise ValueError("provider profile ref must not be empty")
        if not self.host.strip():
            raise ValueError("provider host must not be empty")
        if not 1 <= self.port <= 65_535:
            raise ValueError("provider port must be between 1 and 65535")
        if self.timeout <= 0:
            raise ValueError("provider timeout must be positive")
        if self.max_message_bytes <= 0:
            raise ValueError("provider message limit must be positive")
        if self.auth_method is AuthenticationMethod.PASSWORD:
            if not self.username or not self.credential_ref:
                raise ValueError("password authentication requires username and credential_ref")
        elif self.auth_method is AuthenticationMethod.OAUTH2:
            if not self.username or not self.credential_ref:
                raise ValueError("OAuth2 authentication requires username and credential_ref")
        if self.kind is ProviderKind.IMAP and self.tls_mode is TLSMode.STARTTLS:
            raise ValueError("IMAP STARTTLS is not implemented; use implicit_tls")
        if self.kind is ProviderKind.IMAP and not self.mailbox.strip():
            raise ValueError("IMAP mailbox must not be empty")


@dataclass(frozen=True)
class ProviderDiagnostic:
    profile_ref: str
    status: ProviderDiagnosticStatus
    phase: str
    detail: str
    capabilities: tuple[str, ...] = ()
    observed_at: str | None = None
    operation: ProviderOperation = ProviderOperation.CONNECTION
    external_access: bool = False
    correlation_id: str | None = None
    metadata: tuple[tuple[str, object], ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "profile_ref": self.profile_ref,
            "status": self.status.value,
            "phase": self.phase,
            "detail": self.detail,
            "capabilities": list(self.capabilities),
            "observed_at": self.observed_at,
            "operation": self.operation.value,
            "external_access": self.external_access,
            "correlation_id": self.correlation_id,
            "metadata": dict(self.metadata),
        }
