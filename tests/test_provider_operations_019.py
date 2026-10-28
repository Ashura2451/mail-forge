"""Acceptance evidence for provider account operations 019."""

from datetime import UTC, datetime, timedelta

import pytest

from mailforge.application.provider_operations import ProviderOperationsService
from mailforge.domain.provider import (
    AuthenticationMethod,
    ExternalOperationGrant,
    ProviderDiagnostic,
    ProviderDiagnosticStatus,
    ProviderKind,
    ProviderOperation,
    ProviderProfile,
    TLSMode,
)
from mailforge.infrastructure.campaign_store import CampaignStore
from mailforge.infrastructure.credentials import MappingCredentialResolver, OAuthTokenMaterial
from mailforge.infrastructure.smtp import (
    LiveSendGate,
    SMTPConnectionPool,
    SMTPPhase,
    SMTPTransaction,
    SMTPTransportConfig,
)


def _profile(ref: str = "local", host: str = "127.0.0.1") -> ProviderProfile:
    return ProviderProfile(
        ref=ref,
        kind=ProviderKind.SMTP,
        host=host,
        port=465,
        username="sender@example.com",
        credential_ref="test:provider",
        tls_mode=TLSMode.IMPLICIT,
        auth_method=AuthenticationMethod.PASSWORD,
    )


def _grant(
    service: ProviderOperationsService,
    ref: str,
    *,
    operation=ProviderOperation.CONTROLLED_TEST_SEND,
):
    account = service.account(ref)
    return ExternalOperationGrant(
        account_ref=ref,
        operation=operation,
        host=account["host"],
        port=account["port"],
        purpose="owner-controlled local qualification fixture",
        sender="sender@example.com"
        if operation is ProviderOperation.CONTROLLED_TEST_SEND
        else None,
        recipient="owned@example.com"
        if operation is ProviderOperation.CONTROLLED_TEST_SEND
        else None,
        max_submissions=1 if operation is ProviderOperation.CONTROLLED_TEST_SEND else 0,
        expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        authorization_ref="owner-auth-019",
    )


class _FakePool:
    def __init__(self):
        self.calls = []
        self.last_transaction = SMTPTransaction(
            "local",
            SMTPPhase.FINAL_DATA_RESPONSE,
            response_code=250,
            tls_active=True,
            authenticated=True,
        )

    @property
    def last_phase(self):
        return self.last_transaction.phase.value

    def sendmail(self, sender, recipient, message):
        self.calls.append((sender, recipient, message))
        return {}

    def close(self):
        return None


def test_schema_11_accounts_capabilities_and_inbound_settings_are_durable(tmp_path):
    store = CampaignStore(tmp_path / "operations.sqlite3")
    service = ProviderOperationsService(store)
    account = service.register(
        _profile(),
        display_name="Local fixture",
        sender_email="sender@example.com",
        inbound_enabled=True,
        pacing_per_minute=12,
        max_concurrency=2,
    )

    assert store.schema_version() == 14
    assert account["display_name"] == "Local fixture"
    assert account["inbound_enabled"] is True
    assert any(item["key"] == "google_workspace" for item in service.capabilities())
    assert (
        service.configure_inbound("local", mailbox="Qualification", max_fetch=4)["mailbox"]
        == "Qualification"
    )
    assert service.validate("local")["external_access"] is False
    reopened = CampaignStore(tmp_path / "operations.sqlite3")
    assert reopened.get_provider_account("local")["inbound"]["mailbox"] == "Qualification"


def test_oauth_state_pkce_exact_redirect_and_single_use(tmp_path):
    store = CampaignStore(tmp_path / "oauth.sqlite3")
    service = ProviderOperationsService(store)
    service.register(_profile(), sender_email="sender@example.com")
    started = service.oauth.start(
        account_ref="local",
        provider_key="google_workspace",
        client_id="client-id",
        redirect_uri="http://127.0.0.1:8765/oauth/callback",
        scopes=("https://mail.google.com/",),
        authorization_endpoint="https://accounts.example.test/authorize",
        token_endpoint="https://accounts.example.test/token",
    )
    assert "code_verifier" not in started["authorization_url"]
    assert "code_challenge_method=S256" in started["authorization_url"]
    with pytest.raises(ValueError, match="redirect URI"):
        service.oauth.complete(
            state=started["authorization_url"].split("state=", 1)[1].split("&", 1)[0],
            redirect_uri="http://127.0.0.1:8765/wrong",
            code="fixture-code",
        )

    second = service.oauth.start(
        account_ref="local",
        provider_key="google_workspace",
        client_id="client-id",
        redirect_uri="http://127.0.0.1:8765/oauth/callback",
        scopes=("https://mail.google.com/",),
        authorization_endpoint="https://accounts.example.test/authorize",
        token_endpoint="https://accounts.example.test/token",
    )
    from urllib.parse import parse_qs, urlsplit

    state = parse_qs(urlsplit(second["authorization_url"])[3])["state"][0]
    result = service.oauth.complete(
        state=state,
        redirect_uri="http://127.0.0.1:8765/oauth/callback",
        code="fixture-code",
        identity="sender@example.com",
    )
    assert result["status"] == "authorization_code_received"
    with pytest.raises(ValueError, match="unknown"):
        service.oauth.complete(
            state=state,
            redirect_uri="http://127.0.0.1:8765/oauth/callback",
            code="fixture-code",
        )


def test_external_diagnostic_requires_matching_expiring_grant(tmp_path):
    store = CampaignStore(tmp_path / "external.sqlite3")
    service = ProviderOperationsService(store)
    service.register(_profile("external", "smtp.example.test"), sender_email="sender@example.com")
    with pytest.raises(PermissionError, match="authorization"):
        service.diagnostic("external", ProviderOperation.CONNECTION)
    grant = _grant(service, "external", operation=ProviderOperation.CONNECTION)
    result = service.diagnostic(
        "external",
        ProviderOperation.CONNECTION,
        grant=grant,
        resolver=MappingCredentialResolver({"test:provider": "synthetic-password"}),
    )
    assert result["external_access"] is True
    assert result["status"] in {"connection_failed", "tls_failed", "authentication_failed"}
    assert len(store.list_provider_operation_grants("external")) == 1


def test_controlled_test_send_is_once_only_and_preserves_provider_acceptance(tmp_path, monkeypatch):
    store = CampaignStore(tmp_path / "send.sqlite3")
    service = ProviderOperationsService(store)
    service.register(_profile(), sender_email="sender@example.com")
    pool = _FakePool()
    monkeypatch.setattr(service.providers, "smtp_pool", lambda *args, **kwargs: pool)
    resolver = MappingCredentialResolver({"test:provider": "synthetic-password"})
    grant = _grant(service, "local")
    result = service.controlled_test_send(
        ref="local",
        sender="sender@example.com",
        recipient="owned@example.com",
        message="Subject: fixture\r\n\r\nbody",
        idempotency_key="test-send-019",
        grant=grant,
        resolver=resolver,
    )
    repeated = service.controlled_test_send(
        ref="local",
        sender="sender@example.com",
        recipient="owned@example.com",
        message="Subject: fixture\r\n\r\nbody",
        idempotency_key="test-send-019",
        grant=grant,
        resolver=resolver,
    )
    assert result["status"] == "provider_accepted"
    assert repeated["id"] == result["id"]
    assert len(pool.calls) == 1
    assert store.list_provider_tests("local")[0]["external_access"] == 1


def test_diagnostics_reject_secret_bearing_detail(tmp_path):
    store = CampaignStore(tmp_path / "redaction.sqlite3")
    service = ProviderOperationsService(store)
    service.register(_profile(), sender_email="sender@example.com")
    with pytest.raises(ValueError, match="secret-bearing"):
        store.record_provider_diagnostic(
            ProviderDiagnostic(
                "local",
                status=ProviderDiagnosticStatus.CONNECTION_FAILED,
                phase="connect",
                detail="token=secret",
            )
        )


def test_oauth_xoauth2_transport_and_refresh_fixture_do_not_persist_token(tmp_path):
    class Client:
        def __init__(self):
            self.esmtp_features = {"auth": "XOAUTH2"}
            self.auth_payload = None

        def ehlo(self):
            return 250, b"ok"

        def auth(self, mechanism, authobject):
            self.auth_payload = authobject(None)
            return 235, b"ok"

        def noop(self):
            return 250, b"ok"

        def quit(self):
            return None

    client = Client()
    resolver = MappingCredentialResolver(
        {
            "test:oauth": (
                '{"access_token":"fixture-access",'
                '"refresh_token":"fixture-refresh",'
                '"identity":"sender@example.com"}'
            )
        }
    )
    pool = SMTPConnectionPool(
        [
            SMTPTransportConfig(
                host="127.0.0.1",
                port=465,
                username="sender@example.com",
                credential_ref="test:oauth",
                auth_method=AuthenticationMethod.OAUTH2,
                provider_ref="oauth-fixture",
                implicit_tls=True,
            )
        ],
        gate=LiveSendGate(enabled=True),
        connector_factory=lambda _config: client,
        credential_resolver=resolver,
    )
    assert pool.health_check() == {"127.0.0.1": True}
    assert "fixture-access" in client.auth_payload
    assert "fixture-refresh" not in client.auth_payload
    token = resolver.oauth("test:oauth")
    refreshed = ProviderOperationsService(
        CampaignStore(tmp_path / "refresh.sqlite3")
    ).oauth.refresh(
        token,
        lambda refresh: OAuthTokenMaterial(
            access_token="new-access", refresh_token=refresh, identity="sender@example.com"
        ),
        expected_identity="sender@example.com",
    )
    assert refreshed.access_token == "new-access"
