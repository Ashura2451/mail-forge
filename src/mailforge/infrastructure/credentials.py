"""Runtime-only credential resolution.

Durable provider records contain symbolic references such as
``env:MAILFORGE_SMTP_PASSWORD``. The resolver is the only boundary that turns
that reference into authentication material, and its values are intentionally
not printable or serializable.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
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


class CredentialResolver(Protocol):
    def resolve(self, reference: str) -> CredentialMaterial: ...

    def available(self, reference: str) -> bool: ...


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
