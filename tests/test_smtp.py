import smtplib
import ssl

import pytest

from mailforge.infrastructure.smtp import (
    DeliveryStatus,
    FakeSMTPTransport,
    LiveSendAuthorizationRequired,
    LiveSendGate,
    SmtpTransport,
    SMTPTransportConfig,
    classify_smtp_exception,
    deliver,
)


def test_gate_blocks_transport_before_any_call():
    transport = FakeSMTPTransport()
    with pytest.raises(LiveSendAuthorizationRequired):
        deliver(
            transport, "sender@example.com", "recipient@example.com", "body", gate=LiveSendGate()
        )
    assert transport.calls == []


def test_fake_transport_models_acceptance_without_network():
    transport = FakeSMTPTransport()
    result = deliver(
        transport,
        "sender@example.com",
        "recipient@example.com",
        "body",
        gate=LiveSendGate(enabled=True),
    )
    assert result.status is DeliveryStatus.ACCEPTED
    assert len(transport.calls) == 1


def test_delivery_failures_are_classified_without_ambiguous_retries():
    permanent = smtplib.SMTPResponseException(550, b"rejected")
    transient = smtplib.SMTPResponseException(421, b"try later")
    assert (
        classify_smtp_exception(permanent, after_data=False).status
        is DeliveryStatus.PERMANENT_REJECTION
    )
    assert not classify_smtp_exception(permanent, after_data=False).retryable
    assert (
        classify_smtp_exception(transient, after_data=False).status
        is DeliveryStatus.TRANSIENT_REJECTION
    )
    assert classify_smtp_exception(transient, after_data=False).retryable
    assert classify_smtp_exception(permanent).status is DeliveryStatus.AMBIGUOUS
    assert not classify_smtp_exception(permanent).retryable
    assert (
        classify_smtp_exception(ConnectionError("socket closed"), after_data=True).status
        is DeliveryStatus.AMBIGUOUS
    )
    assert not classify_smtp_exception(ConnectionError("socket closed"), after_data=True).retryable
    assert (
        classify_smtp_exception(smtplib.SMTPDataError(451, b"try later"), after_data=True).status
        is DeliveryStatus.TRANSIENT_REJECTION
    )


def test_deliver_classifies_transport_refusals_and_unknown_disconnects():
    permanent = FakeSMTPTransport(refusals={"recipient@example.com": (550, b"rejected")})
    transient = FakeSMTPTransport(refusals={"recipient@example.com": (450, b"try later")})
    disconnected = FakeSMTPTransport(error=smtplib.SMTPServerDisconnected("unknown phase"))
    gate = LiveSendGate(enabled=True)
    assert (
        deliver(permanent, "sender@example.com", "recipient@example.com", "body", gate=gate).status
        is DeliveryStatus.PERMANENT_REJECTION
    )
    assert (
        deliver(transient, "sender@example.com", "recipient@example.com", "body", gate=gate).status
        is DeliveryStatus.TRANSIENT_REJECTION
    )
    unknown = deliver(
        disconnected, "sender@example.com", "recipient@example.com", "body", gate=gate
    )
    assert unknown.status is DeliveryStatus.AMBIGUOUS
    assert not unknown.retryable


def test_smtp_recipient_exception_uses_recipient_response_code():
    error = smtplib.SMTPRecipientsRefused({"recipient@example.com": (550, b"rejected")})
    result = classify_smtp_exception(error)
    assert result.status is DeliveryStatus.PERMANENT_REJECTION
    assert not result.retryable


def test_smtp_transport_gate_blocks_connector_creation(monkeypatch):
    def connector(*_args, **_kwargs):
        raise AssertionError("connector must not be created")

    monkeypatch.setattr(smtplib, "SMTP", connector)
    transport = SmtpTransport(
        SMTPTransportConfig(host="smtp.invalid", port=587), gate=LiveSendGate()
    )
    with pytest.raises(LiveSendAuthorizationRequired):
        transport.sendmail("sender@example.com", "recipient@example.com", "body")


def test_smtp_transport_uses_explicit_lifecycle_without_network(monkeypatch):
    calls = []

    class Client:
        def ehlo(self):
            calls.append("ehlo")

        def starttls(self, *, context):
            calls.append(("starttls", context))

        def login(self, username, password):
            calls.append(("login", username, password))

        def sendmail(self, sender, recipients, message):
            calls.append(("sendmail", sender, recipients, message))
            return {}

        def quit(self):
            calls.append("quit")

        def close(self):
            calls.append("close")

    def connect(*args, **kwargs):
        calls.append(("connect", args, kwargs))
        return Client()

    monkeypatch.setattr(smtplib, "SMTP", connect)
    transport = SmtpTransport(
        SMTPTransportConfig(
            host="smtp.invalid",
            port=587,
            username="user",
            password="secret",
        ),
        gate=LiveSendGate(enabled=True),
    )

    assert transport.sendmail("sender@example.com", "recipient@example.com", "body") == {}
    assert calls == [
        ("connect", ("smtp.invalid", 587), {"timeout": 30.0}),
        "ehlo",
        ("starttls", calls[2][1]),
        "ehlo",
        ("login", "user", "secret"),
        ("sendmail", "sender@example.com", ["recipient@example.com"], "body"),
        "quit",
    ]
    assert isinstance(calls[0][2], dict)
    assert calls[0][2] == {"timeout": 30.0}
    assert calls[2][1].verify_mode is ssl.CERT_REQUIRED
    assert calls[2][1].check_hostname


def test_implicit_tls_uses_verified_ssl_context(monkeypatch):
    calls = []

    class Client:
        def ehlo(self):
            calls.append("ehlo")

        def sendmail(self, *_args):
            calls.append("sendmail")
            return {}

        def quit(self):
            calls.append("quit")

        def close(self):
            calls.append("close")

    def connect(*args, **kwargs):
        calls.append((args, kwargs))
        return Client()

    monkeypatch.setattr(smtplib, "SMTP_SSL", connect)
    transport = SmtpTransport(
        SMTPTransportConfig(host="smtp.invalid", port=465, implicit_tls=True),
        gate=LiveSendGate(enabled=True),
    )
    assert transport.sendmail("sender@example.com", "recipient@example.com", "body") == {}
    assert calls[0][0] == ("smtp.invalid", 465)
    assert calls[0][1]["context"].verify_mode is ssl.CERT_REQUIRED
    assert calls[0][1]["context"].check_hostname


def test_smtp_config_hides_password_and_rejects_plaintext_authentication():
    config = SMTPTransportConfig(host="smtp.invalid", port=587, username="user", password="secret")
    assert "secret" not in repr(config)
    with pytest.raises(ValueError, match="requires TLS"):
        SMTPTransportConfig(
            host="smtp.invalid",
            port=25,
            username="user",
            password="secret",
            use_tls=False,
        )


@pytest.mark.parametrize("send_error", [None, smtplib.SMTPServerDisconnected("after send")])
def test_smtp_cleanup_failures_do_not_mask_send_outcome(monkeypatch, send_error):
    calls = []

    class Client:
        def ehlo(self):
            pass

        def starttls(self, *, context):
            pass

        def sendmail(self, *_args):
            calls.append("sendmail")
            if send_error is not None:
                raise send_error
            return {}

        def quit(self):
            calls.append("quit")
            raise smtplib.SMTPServerDisconnected("quit failed")

        def close(self):
            calls.append("close")
            raise OSError("close failed")

    monkeypatch.setattr(smtplib, "SMTP", lambda *_args, **_kwargs: Client())
    transport = SmtpTransport(
        SMTPTransportConfig(host="smtp.invalid", port=587),
        gate=LiveSendGate(enabled=True),
    )
    if send_error is None:
        assert transport.sendmail("sender@example.com", "recipient@example.com", "body") == {}
    else:
        with pytest.raises(smtplib.SMTPServerDisconnected, match="after send"):
            transport.sendmail("sender@example.com", "recipient@example.com", "body")
    assert calls == ["sendmail", "quit", "close"]
