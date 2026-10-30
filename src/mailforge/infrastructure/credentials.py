"""Runtime-only credential resolution.

Durable provider records contain symbolic references such as
``env:MAILFORGE_SMTP_PASSWORD``. The resolver is the only boundary that turns
that reference into authentication material, and its values are intentionally
not printable or serializable.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from threading import RLock
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


class OAuthCredentialResolver(CredentialResolver, Protocol):
    """Credential resolver that can provide runtime-only OAuth token material."""

    def oauth(self, reference: str) -> OAuthTokenMaterial: ...


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


def resolve_oauth_token(resolver: CredentialResolver, reference: str) -> OAuthTokenMaterial:
    """Resolve OAuth material without exposing a token beyond the adapter boundary."""

    oauth = getattr(resolver, "oauth", None)
    if callable(oauth):
        return oauth(reference)
    return oauth_token_from_material(resolver.resolve(reference))


class RefreshingOAuthCredentialResolver:
    """Runtime-only OAuth refresh adapter with one refresh in flight per reference.

    The wrapper deliberately retains refreshed tokens only in process memory. A
    caller supplies the provider-specific token exchange; no endpoint, client
    secret, refresh token, or token response is persisted by Mailforge.
    """

    def __init__(
        self,
        delegate: CredentialResolver,
        refresh: Callable[[str], OAuthTokenMaterial],
        *,
        expected_identity: str | None = None,
    ) -> None:
        self.delegate = delegate
        self.refresh = refresh
        self.expected_identity = expected_identity
        self._tokens: dict[str, OAuthTokenMaterial] = {}
        self._locks: dict[str, RLock] = {}
        self._guard = RLock()

    def resolve(self, reference: str) -> CredentialMaterial:
        return self.delegate.resolve(reference)

    def available(self, reference: str) -> bool:
        return self.delegate.available(reference)

    def _lock_for(self, reference: str) -> RLock:
        with self._guard:
            return self._locks.setdefault(reference, RLock())

    def oauth(self, reference: str) -> OAuthTokenMaterial:
        token = self._tokens.get(reference) or resolve_oauth_token(self.delegate, reference)
        if not token.expired:
            self._tokens[reference] = token
            return token
        with self._lock_for(reference):
            token = self._tokens.get(reference) or resolve_oauth_token(self.delegate, reference)
            if not token.expired:
                return token
            if not token.refresh_token:
                raise CredentialError(
                    "OAuth refresh token is unavailable; reauthorization required"
                )
            refreshed = self.refresh(token.refresh_token)
            if not isinstance(refreshed, OAuthTokenMaterial):
                raise CredentialError("OAuth refresh did not return token material")
            if (
                self.expected_identity
                and refreshed.identity
                and refreshed.identity != self.expected_identity
            ):
                raise CredentialError("OAuth refresh returned a different account identity")
            if refreshed.refresh_token is None:
                refreshed = replace(refreshed, refresh_token=token.refresh_token)
            self._tokens[reference] = refreshed
            return refreshed


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


class LocalCredentialResolver:
    """Dispatch symbolic local references to the appropriate runtime adapter."""

    def __init__(
        self,
        *,
        environment: EnvironmentCredentialResolver | None = None,
        keychain: KeychainCredentialResolver | None = None,
    ) -> None:
        self.environment = environment or EnvironmentCredentialResolver()
        self.keychain = keychain or KeychainCredentialResolver()

    def _delegate(self, reference: str) -> CredentialResolver:
        kind, _, _name = reference.partition(":")
        if kind == "env":
            return self.environment
        if kind == "keychain":
            return self.keychain
        raise CredentialError("only env: and keychain: references are available here")

    def resolve(self, reference: str) -> CredentialMaterial:
        return self._delegate(reference).resolve(reference)

    def available(self, reference: str) -> bool:
        try:
            return self._delegate(reference).available(reference)
        except CredentialError:
            return False

    def oauth(self, reference: str) -> OAuthTokenMaterial:
        delegate = self._delegate(reference)
        oauth = getattr(delegate, "oauth", None)
        return (
            oauth(reference)
            if callable(oauth)
            else oauth_token_from_material(delegate.resolve(reference))
        )

    def readiness(self) -> dict[str, object]:
        return {
            "environment": "available",
            "keychain": "available" if self.keychain.backend_available() else "unavailable",
            "references": "opaque-runtime-only",
        }


class KeychainCredentialResolver:
    """Resolve ``keychain:`` references through macOS Keychain Services.

    The packaged macOS runtime uses Security.framework directly so secrets
    never enter a child-process argument vector. An injected runner remains
    available for deterministic tests on non-macOS hosts.
    """

    service = "com.mailforge.credential"

    def __init__(self, *, runner: Callable[..., subprocess.CompletedProcess[str]] | None = None):
        self._runner = runner or subprocess.run
        self._injected_runner = runner is not None

    def backend_available(self) -> bool:
        return self._injected_runner or os.uname().sysname == "Darwin"

    @staticmethod
    def _native() -> ctypes.CDLL:
        try:
            library = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        except OSError as error:
            raise CredentialError("macOS Keychain is unavailable") from error
        library.SecKeychainFindGenericPassword.restype = ctypes.c_int32
        library.SecKeychainFindGenericPassword.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        library.SecKeychainAddGenericPassword.restype = ctypes.c_int32
        library.SecKeychainAddGenericPassword.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        library.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
        library.SecKeychainItemModifyAttributesAndData.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        library.SecKeychainItemDelete.restype = ctypes.c_int32
        library.SecKeychainItemDelete.argtypes = [ctypes.c_void_p]
        library.SecKeychainItemFreeContent.restype = ctypes.c_int32
        library.SecKeychainItemFreeContent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        return library

    def _native_find(
        self, account: str
    ) -> tuple[ctypes.CDLL, ctypes.c_void_p, ctypes.c_void_p, int]:
        library = self._native()
        service = self.service.encode()
        account_bytes = account.encode()
        length = ctypes.c_uint32()
        data = ctypes.c_void_p()
        item = ctypes.c_void_p()
        status = library.SecKeychainFindGenericPassword(
            None,
            len(service),
            service,
            len(account_bytes),
            account_bytes,
            ctypes.byref(length),
            ctypes.byref(data),
            ctypes.byref(item),
        )
        return library, item, data, status if status else int(length.value)

    @staticmethod
    def _release_item(item: ctypes.c_void_p) -> None:
        """Release the retained Keychain item reference returned by Security.framework."""

        if not item:
            return
        try:
            core = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            core.CFRelease.argtypes = [ctypes.c_void_p]
            core.CFRelease.restype = None
            core.CFRelease(item)
        except OSError:
            # The native path is only available on macOS; injected runners never
            # reach this helper and should remain fully testable elsewhere.
            return

    @staticmethod
    def _account(reference: str) -> str:
        match = _REFERENCE.fullmatch(reference)
        if match is None or match["kind"] != "keychain":
            raise CredentialError("only keychain: references are available here")
        return match["name"]

    def _run(self, args: list[str], *, input_value: str | None = None) -> str:
        if not self._injected_runner:
            raise CredentialError("macOS Keychain is unavailable")
        try:
            result = self._runner(
                args,
                input=input_value,
                text=True,
                capture_output=True,
                check=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise CredentialError("macOS Keychain operation failed") from error
        # Preserve credential bytes other than the command's trailing line ending.
        return result.stdout.rstrip("\r\n")

    def resolve(self, reference: str) -> CredentialMaterial:
        account = self._account(reference)
        if not self._injected_runner:
            library, item, data, result = self._native_find(account)
            try:
                if result <= 0 or not data:
                    raise CredentialError("macOS Keychain item is unavailable")
                value = ctypes.string_at(data, result).decode()
            except (UnicodeDecodeError, ValueError) as error:
                raise CredentialError("macOS Keychain item is invalid") from error
            finally:
                if data:
                    library.SecKeychainItemFreeContent(None, data)
                self._release_item(item)
            return CredentialMaterial(value)
        value = self._run(
            ["/usr/bin/security", "find-generic-password", "-s", self.service, "-a", account, "-w"]
        )
        try:
            return CredentialMaterial(value)
        except CredentialError as error:
            raise CredentialError("macOS Keychain item is empty") from error

    def available(self, reference: str) -> bool:
        try:
            self.resolve(reference)
        except CredentialError:
            return False
        return True

    def store(self, reference: str, secret: str) -> None:
        account = self._account(reference)
        material = CredentialMaterial(secret)
        if not self._injected_runner:
            library = self._native()
            service = self.service.encode()
            account_bytes = account.encode()
            item = ctypes.c_void_p()
            status = library.SecKeychainAddGenericPassword(
                None,
                len(service),
                service,
                len(account_bytes),
                account_bytes,
                len(material.secret.encode()),
                material.secret.encode(),
                ctypes.byref(item),
            )
            if status == -25299:
                _library, existing, data, found = self._native_find(account)
                try:
                    if found <= 0 or not existing:
                        raise CredentialError("macOS Keychain item cannot be replaced")
                    status = library.SecKeychainItemModifyAttributesAndData(
                        existing, None, len(material.secret.encode()), material.secret.encode()
                    )
                finally:
                    if data:
                        _library.SecKeychainItemFreeContent(None, data)
                    self._release_item(existing)
            self._release_item(item)
            if status:
                raise CredentialError("macOS Keychain item could not be stored")
            return
        self._run(
            [
                "/usr/bin/security",
                "add-generic-password",
                "-U",
                "-s",
                self.service,
                "-a",
                account,
                "-w",
            ],
            input_value=secret,
        )

    def revoke(self, reference: str) -> None:
        account = self._account(reference)
        if not self._injected_runner:
            library, item, data, result = self._native_find(account)
            try:
                if result <= 0 or not item:
                    return
                if library.SecKeychainItemDelete(item):
                    raise CredentialError("macOS Keychain item could not be revoked")
            finally:
                if data:
                    library.SecKeychainItemFreeContent(None, data)
                self._release_item(item)
            return
        self._run(
            ["/usr/bin/security", "delete-generic-password", "-s", self.service, "-a", account]
        )

    def rotate(self, reference: str, secret: str) -> None:
        self.store(reference, secret)

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
