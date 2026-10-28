"""Runtime-only credential resolution.

Durable provider records contain symbolic references such as
``env:MAILFORGE_SMTP_PASSWORD``. The resolver is the only boundary that turns
that reference into authentication material, and its values are intentionally
not printable or serializable.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol


class CredentialError(RuntimeError):
    """Raised when a credential reference cannot be resolved safely."""


class UnsupportedAuthenticationMechanism(CredentialError):
    """Raised when a provider requests an authentication flow not implemented."""


@dataclass(frozen=True)
class CredentialMaterial:
    secret: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.secret:
            raise CredentialError("credential is empty")


@dataclass(frozen=True)
class OAuthTokenMaterial:
    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    expires_at: str | None = None
    identity: str | None = None

    def __post_init__(self) -> None:
        if not self.access_token:
            raise CredentialError("OAuth access token is empty")

    @property
    def expired(self) -> bool:
        if not self.expires_at:
            return False
        try:
            expiry = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise CredentialError("OAuth token expiry is invalid") from error
        if expiry.tzinfo is None:
            raise CredentialError("OAuth token expiry must include a timezone")
        return expiry <= datetime.now(UTC)


class CredentialResolver(Protocol):
    def resolve(self, reference: str) -> CredentialMaterial: ...

    def available(self, reference: str) -> bool: ...


def oauth_token_from_material(material: CredentialMaterial) -> OAuthTokenMaterial:
    """Decode an externally supplied JSON OAuth credential without persisting it."""

    try:
        payload = json.loads(material.secret)
    except json.JSONDecodeError as error:
        raise CredentialError("OAuth credential reference does not contain JSON") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("access_token"), str):
        raise CredentialError("OAuth credential requires access_token")
    return OAuthTokenMaterial(
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token"),
        expires_at=payload.get("expires_at"),
        identity=payload.get("identity"),
    )


_REFERENCE = re.compile(r"^(?P<kind>env|test|keychain):(?P<name>[A-Za-z0-9_.:-]+)$")


def validate_credential_reference(reference: str) -> None:
    if not _REFERENCE.fullmatch(reference):
        raise ValueError("credential_ref must be a symbolic env:, test:, or keychain: reference")


class EnvironmentCredentialResolver:
    """Resolve only environment references; values never enter durable state."""

    def resolve(self, reference: str) -> CredentialMaterial:
        match = _REFERENCE.fullmatch(reference)
        if match is None or match["kind"] != "env":
            raise CredentialError("only env: credential references are available here")
        value = os.getenv(match["name"])
        if not value:
            raise CredentialError(f"credential reference is unavailable: {reference}")
        return CredentialMaterial(value)

    def available(self, reference: str) -> bool:
        try:
            self.resolve(reference)
        except CredentialError:
            return False
        return True

    def oauth(self, reference: str) -> OAuthTokenMaterial:
        return oauth_token_from_material(self.resolve(reference))


class MappingCredentialResolver:
    """Test-only resolver for injected synthetic secrets."""

    def __init__(self, values: dict[str, str]):
        self._values = dict(values)

    def resolve(self, reference: str) -> CredentialMaterial:
        value = self._values.get(reference)
        if not value:
            raise CredentialError(f"credential reference is unavailable: {reference}")
        return CredentialMaterial(value)

    def available(self, reference: str) -> bool:
        return bool(self._values.get(reference))

    def oauth(self, reference: str) -> OAuthTokenMaterial:
        return oauth_token_from_material(self.resolve(reference))
