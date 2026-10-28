"""Provider configuration and diagnostic vocabulary."""

from __future__ import annotations

import enum
from dataclasses import dataclass


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
    TLS_FAILED = "tls_failed"
    CONNECTION_FAILED = "connection_failed"
    EXTERNAL_UNVERIFIED = "external_unverified"


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

    def to_dict(self) -> dict[str, object]:
        return {
            "profile_ref": self.profile_ref,
            "status": self.status.value,
            "phase": self.phase,
            "detail": self.detail,
            "capabilities": list(self.capabilities),
            "observed_at": self.observed_at,
        }
