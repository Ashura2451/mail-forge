"""SMTP policy boundary and deterministic fake transport.

SMTP acceptance means the provider accepted the message for processing. It is
not proof of inbox delivery. Ambiguous post-DATA failures are never retried by
this module because the remote server may already have accepted the message.
"""

from __future__ import annotations

import os
import smtplib
import ssl
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from threading import RLock, local
from typing import Protocol

from mailforge.domain.provider import AuthenticationMethod
from mailforge.infrastructure.credentials import (
    CredentialError,
    CredentialResolver,
    EnvironmentCredentialResolver,
    UnsupportedAuthenticationMechanism,
)


class DeliveryStatus(StrEnum):
    ACCEPTED = "accepted"
    PERMANENT_REJECTION = "permanent_rejection"
    TRANSIENT_REJECTION = "transient_rejection"
    CONNECTION_FAILURE = "connection_failure"
    AMBIGUOUS = "ambiguous"


class SMTPPhase(StrEnum):
    NOT_STARTED = "not_started"
    CONNECT = "connect"
    TLS_NEGOTIATION = "tls_negotiation"
    AUTHENTICATION = "authentication"
    MAIL_FROM = "mail_from"
    RCPT_TO = "rcpt_to"
    DATA_START = "data_start"
    DATA_TRANSMISSION = "data_transmission"
    FINAL_DATA_RESPONSE = "final_data_response"
    SESSION_COMPLETION = "session_completion"
    CONNECTION_ERROR = "connection_error"


@dataclass(frozen=True)
class DeliveryResult:
    status: DeliveryStatus
    detail: str
    retryable: bool
    phase: SMTPPhase = SMTPPhase.NOT_STARTED
    response_code: int | None = None


class SMTPTransport(Protocol):
    def sendmail(self, sender: str, recipient: str, message: str) -> object: ...


@dataclass(frozen=True)
class SMTPTransportConfig:
    """Connection settings supplied by an explicitly trusted caller."""

    host: str
    port: int
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    credential_ref: str | None = None
    auth_method: AuthenticationMethod = AuthenticationMethod.PASSWORD
    provider_ref: str = "smtp"
    timeout: float = 30.0
    use_tls: bool = True
    implicit_tls: bool = False
    ca_file: str | None = None
    allowed_sender_domains: tuple[str, ...] = ()
    rotation_weight: int = 50

    def __post_init__(self) -> None:
        if not self.host.strip():
            raise ValueError("SMTP host must not be empty")
        if not 1 <= self.port <= 65_535:
            raise ValueError("SMTP port must be between 1 and 65535")
        if self.timeout <= 0:
            raise ValueError("SMTP timeout must be positive")
        if self.password is not None and self.username is None:
            raise ValueError("SMTP password requires a username")
        if self.credential_ref is not None and self.username is None:
            raise ValueError("SMTP credential_ref requires a username")
        if self.auth_method is AuthenticationMethod.OAUTH2 and self.username is None:
            raise ValueError("OAuth2 authentication requires a username")
        if self.username is not None and not (self.use_tls or self.implicit_tls):
            raise ValueError("SMTP authentication requires TLS")
        if self.implicit_tls and not self.use_tls:
            raise ValueError("implicit TLS requires use_tls=True")
        if any(not domain.strip() for domain in self.allowed_sender_domains):
            raise ValueError("allowed sender domains must not be empty")
        if self.rotation_weight < 0:
            raise ValueError("SMTP rotation weight must not be negative")
        if not self.provider_ref.strip():
            raise ValueError("SMTP provider_ref must not be empty")


@dataclass(frozen=True)
class SMTPTransaction:
    provider_ref: str
    phase: SMTPPhase
    detail: str = ""
    response_code: int | None = None
    capabilities: tuple[str, ...] = ()
    tls_active: bool = False
    authenticated: bool = False


class LiveSendAuthorizationRequired(RuntimeError):
    """Raised before any external SMTP operation without explicit authority."""


class SMTPPolicyViolation(ValueError):
    """Raised when a sender is outside the configured SMTP account policy."""


@dataclass(frozen=True)
class LiveSendGate:
    """Require both an explicit caller flag and an out-of-band environment flag."""

    enabled: bool = False
    environment_name: str = "MAILFORGE_ALLOW_LIVE_SEND"

    @classmethod
    def from_environment(cls, explicit_flag: bool = False) -> LiveSendGate:
        return cls(enabled=explicit_flag and os.getenv("MAILFORGE_ALLOW_LIVE_SEND") == "1")

    def require(self) -> None:
        if not self.enabled:
            raise LiveSendAuthorizationRequired(
                "live SMTP delivery is disabled; use a fake transport or provide "
                "the explicit live-send flag and MAILFORGE_ALLOW_LIVE_SEND=1"
            )


def _classify_refusal_codes(codes: list[int]) -> DeliveryResult:
    if codes and all(500 <= code < 600 for code in codes):
        return DeliveryResult(
            DeliveryStatus.PERMANENT_REJECTION,
            "recipient refused",
            retryable=False,
            phase=SMTPPhase.RCPT_TO,
            response_code=codes[0],
        )
    if codes and all(400 <= code < 500 for code in codes):
        return DeliveryResult(
            DeliveryStatus.TRANSIENT_REJECTION,
            "recipient deferred",
            retryable=True,
            phase=SMTPPhase.RCPT_TO,
            response_code=codes[0],
        )
    return DeliveryResult(
        DeliveryStatus.AMBIGUOUS,
        "unclassified recipient refusal",
        retryable=False,
        phase=SMTPPhase.RCPT_TO,
    )


def _classify_refusals(refusals: object) -> DeliveryResult:
    if not isinstance(refusals, Mapping):
        return DeliveryResult(DeliveryStatus.AMBIGUOUS, str(refusals), retryable=False)
    codes = [value[0] for value in refusals.values() if isinstance(value, tuple) and value]
    return _classify_refusal_codes(codes)


def classify_smtp_exception(
    error: BaseException,
    *,
    after_data: bool | None = None,
    phase: SMTPPhase | str | None = None,
) -> DeliveryResult:
    """Classify an SMTP error without claiming what happened at the recipient."""

    try:
        observed_phase = SMTPPhase(phase) if phase is not None else None
    except ValueError:
        observed_phase = None

    if isinstance(error, smtplib.SMTPRecipientsRefused) and after_data is not True:
        return _classify_refusal_codes(
            [value[0] for value in error.recipients.values() if isinstance(value, tuple) and value]
        )
    code = getattr(error, "smtp_code", None)
    if isinstance(error, smtplib.SMTPDataError):
        if isinstance(code, int) and 500 <= code < 600:
            return DeliveryResult(
                DeliveryStatus.PERMANENT_REJECTION,
                str(error),
                retryable=False,
                phase=SMTPPhase.FINAL_DATA_RESPONSE,
                response_code=code,
            )
        if isinstance(code, int) and 400 <= code < 500:
            return DeliveryResult(
                DeliveryStatus.TRANSIENT_REJECTION,
                str(error),
                retryable=True,
                phase=SMTPPhase.FINAL_DATA_RESPONSE,
                response_code=code,
            )
    if after_data is not False:
        return DeliveryResult(
            DeliveryStatus.AMBIGUOUS,
            str(error),
            retryable=False,
            phase=observed_phase or SMTPPhase.CONNECTION_ERROR,
            response_code=code if isinstance(code, int) else None,
        )
    if isinstance(code, int) and 500 <= code < 600:
        return DeliveryResult(
            DeliveryStatus.PERMANENT_REJECTION,
            str(error),
            retryable=False,
            phase=observed_phase or SMTPPhase.RCPT_TO,
            response_code=code,
        )
    if isinstance(code, int) and 400 <= code < 500:
        return DeliveryResult(
            DeliveryStatus.TRANSIENT_REJECTION,
            str(error),
            retryable=True,
            phase=observed_phase or SMTPPhase.RCPT_TO,
            response_code=code,
        )
    if isinstance(error, (smtplib.SMTPServerDisconnected, TimeoutError, ConnectionError, OSError)):
        return DeliveryResult(
            DeliveryStatus.CONNECTION_FAILURE,
            str(error),
            retryable=True,
            phase=observed_phase or SMTPPhase.CONNECTION_ERROR,
        )
    return DeliveryResult(
        DeliveryStatus.AMBIGUOUS,
        str(error),
        retryable=False,
        phase=observed_phase or SMTPPhase.CONNECTION_ERROR,
    )


def deliver(
    transport: SMTPTransport,
    sender: str,
    recipient: str,
    message: str,
    *,
    gate: LiveSendGate,
    after_data: bool | None = None,
) -> DeliveryResult:
    """Send through an explicitly authorized transport or fail closed."""

    gate.require()
    try:
        refusals = transport.sendmail(sender, recipient, message)
    except Exception as error:
        phase = getattr(transport, "last_phase", None)
        if after_data is None:
            if phase in {
                SMTPPhase.DATA_TRANSMISSION.value,
                SMTPPhase.FINAL_DATA_RESPONSE.value,
            }:
                after_data = True
            elif phase in {
                SMTPPhase.CONNECT.value,
                SMTPPhase.TLS_NEGOTIATION.value,
                SMTPPhase.AUTHENTICATION.value,
                SMTPPhase.MAIL_FROM.value,
                SMTPPhase.RCPT_TO.value,
                SMTPPhase.DATA_START.value,
            }:
                after_data = False
        return classify_smtp_exception(error, after_data=after_data, phase=phase)
    if refusals:
        return _classify_refusals(refusals)
    transaction = getattr(transport, "last_transaction", None)
    return DeliveryResult(
        DeliveryStatus.ACCEPTED,
        "SMTP server accepted the message",
        retryable=False,
        phase=getattr(transaction, "phase", SMTPPhase.NOT_STARTED),
        response_code=getattr(transaction, "response_code", None),
    )


class SmtpTransport:
    """Fail-closed stdlib SMTP adapter; callers must supply configuration and a gate."""

    def __init__(
        self,
        config: SMTPTransportConfig,
        *,
        gate: LiveSendGate,
        credential_resolver: CredentialResolver | None = None,
    ):
        self.config = config
        self.gate = gate
        self.credential_resolver = credential_resolver or EnvironmentCredentialResolver()
        self.last_transaction = SMTPTransaction(config.provider_ref, SMTPPhase.NOT_STARTED)

    def _credential(self) -> str | None:
        if self.config.auth_method is AuthenticationMethod.OAUTH2:
            raise UnsupportedAuthenticationMechanism(
                "SMTP OAuth2 authentication is not implemented"
            )
        if self.config.credential_ref:
            try:
                return self.credential_resolver.resolve(self.config.credential_ref).secret
            except CredentialError as error:
                raise CredentialError("SMTP credential is unavailable") from error
        return self.config.password

    def _context(self) -> ssl.SSLContext:
        context = ssl.create_default_context(cafile=self.config.ca_file)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return context

    def sendmail(self, sender: str, recipient: str, message: str) -> object:
        self.gate.require()
        if self.config.auth_method is AuthenticationMethod.OAUTH2:
            raise UnsupportedAuthenticationMechanism(
                "SMTP OAuth2 authentication is not implemented"
            )
        connector = smtplib.SMTP_SSL if self.config.implicit_tls else smtplib.SMTP
        context = self._context()
        self.last_transaction = SMTPTransaction(
            self.config.provider_ref,
            SMTPPhase.CONNECT,
            tls_active=self.config.implicit_tls,
        )
        if self.config.implicit_tls:
            client = connector(
                self.config.host,
                self.config.port,
                timeout=self.config.timeout,
                context=context,
            )
        else:
            client = connector(self.config.host, self.config.port, timeout=self.config.timeout)
        try:
            client.ehlo()
            self.last_transaction = SMTPTransaction(
                self.config.provider_ref,
                SMTPPhase.TLS_NEGOTIATION if self.config.use_tls else SMTPPhase.AUTHENTICATION,
                tls_active=self.config.implicit_tls,
                capabilities=tuple(getattr(client, "esmtp_features", {}).keys()),
            )
            if self.config.use_tls and not self.config.implicit_tls:
                client.starttls(context=context)
                client.ehlo()
            if self.config.username is not None:
                self.last_transaction = SMTPTransaction(
                    self.config.provider_ref,
                    SMTPPhase.AUTHENTICATION,
                    tls_active=True,
                    authenticated=False,
                )
                client.login(self.config.username, self._credential() or "")
            self.last_transaction = SMTPTransaction(
                self.config.provider_ref,
                SMTPPhase.DATA_TRANSMISSION,
                tls_active=self.config.use_tls or self.config.implicit_tls,
                authenticated=self.config.username is not None,
            )
            result = client.sendmail(sender, [recipient], message)
            self.last_transaction = SMTPTransaction(
                self.config.provider_ref,
                SMTPPhase.FINAL_DATA_RESPONSE,
                tls_active=self.config.use_tls or self.config.implicit_tls,
                authenticated=self.config.username is not None,
            )
            return result
        finally:
            try:
                client.quit()
            except OSError, smtplib.SMTPException:
                try:
                    client.close()
                except OSError, smtplib.SMTPException:
                    pass


SMTPConnector = Callable[[SMTPTransportConfig], object]


class SMTPConnectionPool:
    """Small, gated SMTP connection pool with weighted account rotation.

    The pool owns connections only; campaign state, retries, and ambiguity
    handling remain in ``CampaignService``. A failed session is discarded and
    the exception is returned to the caller, so the message is never retried
    implicitly after the server may have accepted it.
    """

    def __init__(
        self,
        configs: list[SMTPTransportConfig] | tuple[SMTPTransportConfig, ...],
        *,
        gate: LiveSendGate,
        connector_factory: SMTPConnector | None = None,
        credential_resolver: CredentialResolver | None = None,
    ) -> None:
        if not configs:
            raise ValueError("SMTP connection pool requires at least one account")
        self.configs = tuple(configs)
        self.gate = gate
        self.connector_factory = connector_factory
        self.credential_resolver = credential_resolver or EnvironmentCredentialResolver()
        self._clients: dict[int, object] = {}
        self._locks = [RLock() for _ in self.configs]
        self._rotation = self._weighted_rotation()
        self._cursor = 0
        self._cursor_lock = RLock()
        self._phase = local()
        self._transaction = local()

    @property
    def last_phase(self) -> str:
        return getattr(self._phase, "value", SMTPPhase.NOT_STARTED.value)

    @last_phase.setter
    def last_phase(self, value: str) -> None:
        self._phase.value = value

    @property
    def last_transaction(self) -> SMTPTransaction:
        return getattr(
            self._transaction,
            "value",
            SMTPTransaction("smtp", SMTPPhase.NOT_STARTED),
        )

    @last_transaction.setter
    def last_transaction(self, value: SMTPTransaction) -> None:
        self._transaction.value = value
        self.last_phase = value.phase.value

    def _weighted_rotation(self) -> tuple[int, ...]:
        rotation = tuple(
            index
            for index, config in enumerate(self.configs)
            for _ in range(
                max(0, config.rotation_weight if hasattr(config, "rotation_weight") else 1)
            )
        )
        return rotation or tuple(range(len(self.configs)))

    def _select_index(self) -> int:
        with self._cursor_lock:
            index = self._rotation[self._cursor % len(self._rotation)]
            self._cursor += 1
            return index

    @staticmethod
    def _context(config: SMTPTransportConfig) -> ssl.SSLContext:
        context = ssl.create_default_context(cafile=config.ca_file)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return context

    def _default_connector(self, config: SMTPTransportConfig) -> object:
        context = self._context(config)
        if config.implicit_tls:
            return smtplib.SMTP_SSL(
                config.host, config.port, timeout=config.timeout, context=context
            )
        return smtplib.SMTP(config.host, config.port, timeout=config.timeout)

    def _connect(self, index: int) -> object:
        self.gate.require()
        config = self.configs[index]
        if config.auth_method is AuthenticationMethod.OAUTH2:
            raise UnsupportedAuthenticationMechanism(
                "SMTP OAuth2 authentication is not implemented"
            )
        self.last_transaction = SMTPTransaction(config.provider_ref, SMTPPhase.CONNECT)
        if config.implicit_tls:
            self.last_transaction = SMTPTransaction(
                config.provider_ref,
                SMTPPhase.TLS_NEGOTIATION,
                tls_active=True,
            )
        client = (
            self.connector_factory(config)
            if self.connector_factory is not None
            else self._default_connector(config)
        )
        try:
            ehlo = getattr(client, "ehlo", None)
            if callable(ehlo):
                ehlo()
            if config.use_tls and not config.implicit_tls:
                self.last_transaction = SMTPTransaction(
                    config.provider_ref,
                    SMTPPhase.TLS_NEGOTIATION,
                    capabilities=tuple(getattr(client, "esmtp_features", {}).keys()),
                )
                starttls = getattr(client, "starttls", None)
                if not callable(starttls):
                    raise RuntimeError("SMTP client does not support STARTTLS")
                features = getattr(client, "esmtp_features", None)
                if features is not None and "starttls" not in {
                    str(key).lower() for key in features
                }:
                    raise smtplib.SMTPNotSupportedError("SMTP server does not advertise STARTTLS")
                starttls(context=self._context(config))
                if callable(ehlo):
                    ehlo()
            if config.username is not None:
                self.last_transaction = SMTPTransaction(
                    config.provider_ref,
                    SMTPPhase.AUTHENTICATION,
                    tls_active=config.use_tls or config.implicit_tls,
                    capabilities=tuple(getattr(client, "esmtp_features", {}).keys()),
                )
                login = getattr(client, "login", None)
                if not callable(login):
                    raise RuntimeError("SMTP client does not support authentication")
                password = config.password
                if config.credential_ref:
                    try:
                        password = self.credential_resolver.resolve(config.credential_ref).secret
                    except CredentialError as error:
                        raise CredentialError("SMTP credential is unavailable") from error
                login(config.username, password or "")
                self.last_transaction = SMTPTransaction(
                    config.provider_ref,
                    SMTPPhase.AUTHENTICATION,
                    tls_active=True,
                    authenticated=True,
                    capabilities=tuple(getattr(client, "esmtp_features", {}).keys()),
                )
        except BaseException as error:
            phase = self.last_transaction
            self.last_transaction = SMTPTransaction(
                config.provider_ref,
                phase.phase,
                detail="connection setup failed",
                response_code=(
                    getattr(error, "smtp_code", None)
                    if isinstance(getattr(error, "smtp_code", None), int)
                    else phase.response_code
                ),
                capabilities=phase.capabilities,
                tls_active=phase.tls_active,
                authenticated=phase.authenticated,
            )
            self._close_client(client)
            raise
        self._clients[index] = client
        return client

    @staticmethod
    def _close_client(client: object) -> None:
        quit_method = getattr(client, "quit", None)
        if callable(quit_method):
            try:
                quit_method()
                return
            except OSError, smtplib.SMTPException:
                pass
        close_method = getattr(client, "close", None)
        if callable(close_method):
            try:
                close_method()
            except OSError, smtplib.SMTPException:
                pass

    def _validate_sender(self, config: SMTPTransportConfig, sender: str) -> None:
        domain = sender.rsplit("@", 1)[-1].lower() if "@" in sender else ""
        allowed = tuple(item.lower().lstrip("@") for item in config.allowed_sender_domains)
        if allowed and domain not in allowed:
            raise SMTPPolicyViolation(
                "sender domain "
                f"{domain or '<missing>'} is not allowed for SMTP account {config.host}"
            )

    def sendmail(self, sender: str, recipient: str, message: str) -> object:
        self.gate.require()
        index = self._select_index()
        config = self.configs[index]
        self._validate_sender(config, sender)
        with self._locks[index]:
            client = self._clients.get(index) or self._connect(index)
            try:
                self.last_transaction = SMTPTransaction(
                    config.provider_ref,
                    SMTPPhase.MAIL_FROM,
                    tls_active=config.use_tls or config.implicit_tls,
                    authenticated=config.username is not None,
                )
                if all(callable(getattr(client, name, None)) for name in ("mail", "rcpt", "data")):
                    code, response = client.mail(sender)
                    if code >= 400:
                        self.last_transaction = SMTPTransaction(
                            config.provider_ref,
                            SMTPPhase.MAIL_FROM,
                            f"MAIL FROM rejected: {code}",
                            response_code=code,
                        )
                        return {sender: (code, response)}
                    self.last_transaction = SMTPTransaction(
                        config.provider_ref,
                        SMTPPhase.RCPT_TO,
                        response_code=code,
                    )
                    code, response = client.rcpt(recipient)
                    if code >= 400:
                        self.last_transaction = SMTPTransaction(
                            config.provider_ref,
                            SMTPPhase.RCPT_TO,
                            f"RCPT TO rejected: {code}",
                            response_code=code,
                        )
                        return {recipient: (code, response)}
                    self.last_transaction = SMTPTransaction(
                        config.provider_ref,
                        SMTPPhase.DATA_TRANSMISSION,
                        response_code=code,
                    )
                    code, response = client.data(message)
                    self.last_transaction = SMTPTransaction(
                        config.provider_ref,
                        SMTPPhase.FINAL_DATA_RESPONSE,
                        response_code=code,
                        tls_active=config.use_tls or config.implicit_tls,
                        authenticated=config.username is not None,
                    )
                    return {} if code < 400 else {recipient: (code, response)}
                self.last_phase = SMTPPhase.DATA_TRANSMISSION.value
                result = client.sendmail(sender, [recipient], message)
                self.last_transaction = SMTPTransaction(
                    config.provider_ref,
                    SMTPPhase.FINAL_DATA_RESPONSE,
                    tls_active=config.use_tls or config.implicit_tls,
                    authenticated=config.username is not None,
                )
                return result
            except Exception as error:
                phase = self.last_transaction
                self.last_transaction = SMTPTransaction(
                    config.provider_ref,
                    phase.phase,
                    detail=type(error).__name__,
                    response_code=(
                        error.smtp_code
                        if isinstance(getattr(error, "smtp_code", None), int)
                        else phase.response_code
                    ),
                    capabilities=phase.capabilities,
                    tls_active=phase.tls_active,
                    authenticated=phase.authenticated,
                )
                self._clients.pop(index, None)
                self._close_client(client)
                raise

    def health_check(self) -> dict[str, bool]:
        self.gate.require()
        results: dict[str, bool] = {}
        for index, config in enumerate(self.configs):
            with self._locks[index]:
                client = self._clients.get(index)
                try:
                    client = client or self._connect(index)
                    noop = getattr(client, "noop", None)
                    if callable(noop):
                        response = noop()
                        code = response[0] if isinstance(response, tuple) else response
                        if isinstance(code, int) and not 200 <= code < 400:
                            raise smtplib.SMTPResponseException(code, b"NOOP failed")
                    results[config.host] = True
                except Exception:
                    self._clients.pop(index, None)
                    if client is not None:
                        self._close_client(client)
                    results[config.host] = False
        return results

    def health_check_transaction(self) -> SMTPTransaction:
        self.health_check()
        return self.last_transaction

    def close(self) -> None:
        for index, lock in enumerate(self._locks):
            with lock:
                client = self._clients.pop(index, None)
                if client is not None:
                    self._close_client(client)


class FakeSMTPTransport:
    """Loopback transport for tests; it never opens a socket."""

    def __init__(self, *, error: BaseException | None = None, refusals: object = None):
        self.error = error
        self.refusals = refusals
        self.calls: list[tuple[str, str, str]] = []

    def sendmail(self, sender: str, recipient: str, message: str) -> object:
        self.calls.append((sender, recipient, message))
        if self.error is not None:
            raise self.error
        return self.refusals
