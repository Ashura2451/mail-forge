from __future__ import annotations

import imaplib
import socketserver
import ssl
import subprocess
import threading
from pathlib import Path

import pytest

from mailforge.application.inbound import InboundService
from mailforge.application.inbound_sources import (
    CursorInvalidated,
    IMAPConnectionConfig,
    ImapSource,
)
from mailforge.application.outbound import OutboundService
from mailforge.application.providers import ProviderService
from mailforge.domain.campaign import DeliveryState
from mailforge.domain.outbound import SenderPolicy, TemplateDefinition
from mailforge.domain.provider import (
    AuthenticationMethod,
    ProviderKind,
    ProviderProfile,
    TLSMode,
)
from mailforge.infrastructure.campaign_store import CampaignStore
from mailforge.infrastructure.credentials import MappingCredentialResolver
from mailforge.infrastructure.smtp import (
    DeliveryStatus,
    LiveSendGate,
    SMTPConnectionPool,
    SMTPPhase,
    SMTPTransportConfig,
    deliver,
)


def _certificate(tmp_path: Path, *, common_name: str = "localhost", san: str = "DNS:localhost"):
    key = tmp_path / f"{common_name}.key.pem"
    cert = tmp_path / f"{common_name}.cert.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            f"/CN={common_name}",
            "-addext",
            f"subjectAltName={san}",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


class _SMTPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, cert: Path, key: Path, *, implicit_tls=False, **scenario):
        self.cert = cert
        self.key = key
        self.implicit_tls = implicit_tls
        self.scenario = scenario
        self.connections = 0
        self.commands: list[str] = []
        super().__init__(("127.0.0.1", 0), _SMTPHandler)


class _SMTPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        server: _SMTPServer = self.server  # type: ignore[assignment]
        server.connections += 1
        connection = self.request
        if server.implicit_tls:
            connection = self._tls(server, connection)
        self._send(connection, b"220 loopback.smtp ESMTP ready\r\n")
        while True:
            line = self._readline(connection)
            if not line:
                return
            command = line.decode("ascii", "replace").strip()
            server.commands.append(command)
            verb = command.split(" ", 1)[0].upper()
            if verb in {"EHLO", "HELO"}:
                if server.scenario.get("no_starttls"):
                    self._send(connection, b"250-loopback.smtp\r\n250 AUTH PLAIN LOGIN\r\n")
                else:
                    self._send(
                        connection, b"250-loopback.smtp\r\n250-STARTTLS\r\n250 AUTH PLAIN LOGIN\r\n"
                    )
            elif verb == "STARTTLS":
                self._send(connection, b"220 2.0.0 ready for TLS\r\n")
                connection = self._tls(server, connection)
            elif verb == "AUTH":
                if server.scenario.get("auth_refused"):
                    self._send(connection, b"535 5.7.8 authentication failed\r\n")
                else:
                    self._send(connection, b"235 2.7.0 authentication successful\r\n")
            elif verb == "MAIL":
                if server.scenario.get("sender_refused"):
                    self._send(connection, b"550 5.1.8 sender refused\r\n")
                else:
                    self._send(connection, b"250 2.1.0 sender accepted\r\n")
            elif verb == "RCPT":
                code = server.scenario.get("recipient_code", 250)
                self._send(connection, f"{code} recipient response\r\n".encode())
            elif verb == "DATA":
                self._send(connection, b"354 end data with <CR><LF>.<CR><LF>\r\n")
                while self._readline(connection) not in {b".\r\n", b"."}:
                    pass
                if server.scenario.get("disconnect_after_data"):
                    connection.close()
                    return
                code = server.scenario.get("data_code", 250)
                self._send(connection, f"{code} final data response\r\n".encode())
            elif verb == "RSET":
                self._send(connection, b"250 reset\r\n")
            elif verb == "QUIT":
                self._send(connection, b"221 bye\r\n")
                return
            else:
                self._send(connection, b"250 ok\r\n")

    @staticmethod
    def _tls(server: _SMTPServer, connection):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(server.cert, server.key)
        return context.wrap_socket(connection, server_side=True)

    @staticmethod
    def _readline(connection):
        data = bytearray()
        while not data.endswith(b"\n"):
            chunk = connection.recv(1)
            if not chunk:
                return bytes(data)
            data.extend(chunk)
        return bytes(data)

    @staticmethod
    def _send(connection, payload: bytes):
        connection.sendall(payload)


class _IMAPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self, cert: Path, key: Path, messages: dict[int, bytes], *, uidvalidity="7", **scenario
    ):
        self.cert = cert
        self.key = key
        self.messages = messages
        self.uidvalidity = uidvalidity
        self.scenario = scenario
        self.folders = {"INBOX": messages, "Archive": {}}
        self.flags: dict[int, set[str]] = {uid: set() for uid in messages}
        super().__init__(("127.0.0.1", 0), _IMAPHandler)


class _IMAPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        server: _IMAPServer = self.server  # type: ignore[assignment]
        mailbox_name = "INBOX"
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(server.cert, server.key)
        connection = context.wrap_socket(self.request, server_side=True)
        self._send(connection, b"* OK loopback.imap ready\r\n")
        while True:
            line = self._readline(connection)
            if not line:
                return
            text = line.decode("ascii", "replace").strip()
            parts = text.split(" ", 2)
            if len(parts) < 2:
                continue
            tag, command = parts[0], parts[1].upper()
            argument = parts[2] if len(parts) > 2 else ""
            if command == "CAPABILITY":
                move = " UIDPLUS MOVE" if server.scenario.get("move") else ""
                self._send(
                    connection,
                    (
                        f"* CAPABILITY IMAP4rev1{move} AUTH=PLAIN\r\n"
                        f"{tag} OK capability\r\n"
                    ).encode(),
                )
            elif command == "LOGIN":
                if server.scenario.get("auth_refused"):
                    self._send(connection, f"{tag} NO [AUTHENTICATIONFAILED] denied\r\n".encode())
                else:
                    self._send(connection, f"{tag} OK login\r\n".encode())
            elif command in {"SELECT", "EXAMINE"}:
                mailbox_name = argument.strip().strip('"') or "INBOX"
                messages = server.folders.setdefault(mailbox_name, {})
                self._send(
                    connection,
                    (
                        f"* FLAGS (\\Seen \\Flagged)\r\n* {len(messages)} EXISTS\r\n"
                        f"* OK [UIDVALIDITY {server.uidvalidity}] valid\r\n"
                        f"{tag} OK select\r\n"
                    ).encode(),
                )
            elif command == "LIST":
                self._send(
                    connection,
                    b'* LIST (\\HasNoChildren) "/" "INBOX"\r\n'
                    b'* LIST (\\HasNoChildren) "/" "Archive"\r\n' + f"{tag} OK list\r\n".encode(),
                )
            elif command == "STATUS":
                selected = argument.rsplit(" ", 1)[0].strip('"') or "INBOX"
                messages = server.folders.setdefault(selected, {})
                unseen = sum(1 for uid in messages if r"\Seen" not in server.flags.get(uid, set()))
                self._send(
                    connection,
                    f'* STATUS "{selected}" (MESSAGES {len(messages)} UNSEEN {unseen} '
                    f"UIDNEXT {max(messages, default=0) + 1} UIDVALIDITY {server.uidvalidity})\r\n"
                    f"{tag} OK status\r\n".encode(),
                )
            elif command == "UID" and argument.upper().startswith("SEARCH"):
                uids = " ".join(str(uid) for uid in sorted(server.folders[mailbox_name]))
                self._send(connection, f"* SEARCH {uids}\r\n{tag} OK search\r\n".encode())
            elif command == "UID" and argument.upper().startswith("FETCH"):
                uid = int(argument.split()[1])
                if server.scenario.get("disconnect_on_fetch"):
                    connection.close()
                    return
                raw = server.folders[mailbox_name][uid]
                flags = " ".join(sorted(server.flags.get(uid, set())))
                fetch_flags = f" FLAGS ({flags})" if "FLAGS" in argument.upper() else ""
                self._send(
                    connection,
                    f"* {uid} FETCH (UID {uid}{fetch_flags} BODY[] {{{len(raw)}}}\r\n".encode()
                    + raw
                    + f")\r\n{tag} OK fetch\r\n".encode(),
                )
            elif command == "UID" and argument.upper().startswith("STORE"):
                parts = argument.split(maxsplit=3)
                uid = int(parts[1])
                operation = parts[2].upper()
                requested = set(parts[3].strip("()\\").split()) if len(parts) > 3 else set()
                flags = server.flags.setdefault(uid, set())
                if operation.startswith("+FLAGS"):
                    flags.update(f"\\{item}" for item in requested)
                elif operation.startswith("-FLAGS"):
                    flags.difference_update(f"\\{item}" for item in requested)
                self._send(connection, f"{tag} OK store\r\n".encode())
            elif command == "UID" and argument.upper().startswith(("COPY", "MOVE")):
                parts = argument.split(maxsplit=2)
                uid = int(parts[1])
                target = parts[2].strip().strip('"')
                target_messages = server.folders.setdefault(target, {})
                target_messages[uid] = server.folders[mailbox_name][uid]
                server.flags.setdefault(uid, set())
                if argument.upper().startswith("MOVE"):
                    del server.folders[mailbox_name][uid]
                self._send(connection, f"{tag} OK {command.lower()}\r\n".encode())
            elif command == "LOGOUT":
                self._send(connection, b"* BYE bye\r\n" + f"{tag} OK logout\r\n".encode())
                return
            else:
                self._send(connection, f"{tag} OK\r\n".encode())

    @staticmethod
    def _readline(connection):
        data = bytearray()
        while not data.endswith(b"\n"):
            chunk = connection.recv(1)
            if not chunk:
                return bytes(data)
            data.extend(chunk)
        return bytes(data)

    @staticmethod
    def _send(connection, payload: bytes):
        connection.sendall(payload)


class _ServerContext:
    def __init__(self, server):
        self.server = server

    def __enter__(self):
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.server

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def _smtp_pool(server: _SMTPServer, cert: Path, *, implicit=False, **overrides):
    config = SMTPTransportConfig(
        host="localhost",
        port=server.server_address[1],
        username="user",
        credential_ref="test:smtp",
        implicit_tls=implicit,
        use_tls=True,
        ca_file=str(cert),
        **overrides,
    )
    return SMTPConnectionPool(
        [config],
        gate=LiveSendGate(enabled=True),
        credential_resolver=MappingCredentialResolver({"test:smtp": "synthetic-password"}),
    )


def test_smtp_starttls_and_implicit_tls_use_real_loopback_protocol(tmp_path):
    cert, key = _certificate(tmp_path)
    starttls_server = _SMTPServer(cert, key)
    with _ServerContext(starttls_server):
        pool = _smtp_pool(starttls_server, cert)
        result = deliver(
            pool, "sender@example.com", "a@example.com", "Subject: test\r\n\r\nbody", gate=pool.gate
        )
        assert result.status is DeliveryStatus.ACCEPTED
        assert pool.last_transaction.phase is SMTPPhase.FINAL_DATA_RESPONSE
        assert "STARTTLS" in " ".join(starttls_server.commands)
        pool.close()

    implicit_server = _SMTPServer(cert, key, implicit_tls=True)
    with _ServerContext(implicit_server):
        pool = _smtp_pool(implicit_server, cert, implicit=True)
        result = deliver(pool, "sender@example.com", "a@example.com", "body", gate=pool.gate)
        assert result.status is DeliveryStatus.ACCEPTED
        assert pool.last_transaction.phase is SMTPPhase.FINAL_DATA_RESPONSE
        pool.close()


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ({"no_starttls": True}, DeliveryStatus.CONNECTION_FAILURE),
        ({"auth_refused": True}, DeliveryStatus.PERMANENT_REJECTION),
        ({"recipient_code": 450}, DeliveryStatus.TRANSIENT_REJECTION),
        ({"recipient_code": 550}, DeliveryStatus.PERMANENT_REJECTION),
        ({"disconnect_after_data": True}, DeliveryStatus.AMBIGUOUS),
    ],
)
def test_smtp_protocol_failures_are_phase_aware_and_not_silently_retried(
    tmp_path, scenario, expected
):
    cert, key = _certificate(tmp_path)
    server = _SMTPServer(cert, key, **scenario)
    with _ServerContext(server):
        pool = _smtp_pool(server, cert)
        result = deliver(pool, "sender@example.com", "a@example.com", "body", gate=pool.gate)
        assert result.status is expected
        if expected is DeliveryStatus.AMBIGUOUS:
            assert result.retryable is False
        pool.close()


def test_smtp_certificate_and_hostname_fail_closed(tmp_path):
    cert, key = _certificate(tmp_path, common_name="wrong-host", san="DNS:wrong-host")
    server = _SMTPServer(cert, key)
    with _ServerContext(server):
        config = SMTPTransportConfig(
            host="localhost",
            port=server.server_address[1],
            username="user",
            credential_ref="test:smtp",
            ca_file=str(cert),
        )
        pool = SMTPConnectionPool(
            [config],
            gate=LiveSendGate(enabled=True),
            credential_resolver=MappingCredentialResolver({"test:smtp": "x"}),
        )
        result = deliver(pool, "sender@example.com", "a@example.com", "body", gate=pool.gate)
        assert result.status is DeliveryStatus.CONNECTION_FAILURE
        pool.close()


def test_imap_uid_acquisition_is_tls_read_only_incremental_and_idempotent(tmp_path):
    cert, key = _certificate(tmp_path)
    messages = {1: b"From: a@example.com\r\nSubject: one\r\n\r\none", 2: b"bad"}
    server = _IMAPServer(cert, key, messages)
    resolver = MappingCredentialResolver({"test:imap": "synthetic-password"})
    with _ServerContext(server):
        source = ImapSource(
            "loopback-imap",
            IMAPConnectionConfig(
                host="localhost",
                port=server.server_address[1],
                username="user",
                credential_ref="test:imap",
                ca_file=str(cert),
            ),
            credential_resolver=resolver,
        )
        first = list(source.messages(limit=1))
        assert first[0].uid == "1"
        assert first[0].uidvalidity == "7"
        second = list(source.messages(cursor=first[0].source_identity))
        assert second[0].uid == "2"
        assert source.last_capabilities == ("AUTH=PLAIN", "IMAP4REV1")

        store = CampaignStore(tmp_path / "imap.sqlite3")
        store.register_provider_profile(
            ProviderProfile(
                ref="loopback-imap",
                kind=ProviderKind.IMAP,
                host="localhost",
                port=server.server_address[1],
                username="user",
                credential_ref="test:imap",
                tls_mode=TLSMode.IMPLICIT,
                ca_file=str(cert),
            )
        )
        report = InboundService(store).ingest_provider("loopback-imap", resolver)
        assert report.acquired == 2
        assert InboundService(store).ingest_provider("loopback-imap", resolver).acquired == 0


def test_imap_uidvalidity_and_authentication_failures_are_explicit(tmp_path):
    cert, key = _certificate(tmp_path)
    server = _IMAPServer(cert, key, {1: b"message"}, uidvalidity="8")
    resolver = MappingCredentialResolver({"test:imap": "synthetic-password"})
    with _ServerContext(server):
        source = ImapSource(
            "loopback-imap",
            IMAPConnectionConfig(
                host="localhost",
                port=server.server_address[1],
                username="user",
                credential_ref="test:imap",
                ca_file=str(cert),
            ),
            credential_resolver=resolver,
        )
        with pytest.raises(CursorInvalidated):
            list(source.messages(cursor="7:1"))


def test_imap_mutation_postconditions_use_message_identity_and_folder_epochs(tmp_path):
    cert, key = _certificate(tmp_path)
    raw = b"Message-ID: <move@example.com>\r\nSubject: move\r\n\r\nbody"
    server = _IMAPServer(cert, key, {1: raw}, move=True)
    resolver = MappingCredentialResolver({"test:imap": "synthetic-password"})
    with _ServerContext(server):
        source = ImapSource(
            "loopback-imap",
            IMAPConnectionConfig(
                host="localhost",
                port=server.server_address[1],
                username="user",
                credential_ref="test:imap",
                ca_file=str(cert),
            ),
            credential_resolver=resolver,
        )
        origin = source.mailbox_state("INBOX")
        target = source.mailbox_state("Archive")
        source.mutate(
            mailbox_name="INBOX",
            uid="1",
            action="move",
            target_folder="Archive",
            expected_uidvalidity=str(origin["uidvalidity"]),
        )
        assert source.message_id_present(
            "Archive",
            "<move@example.com>",
            expected_uidvalidity=str(target["uidvalidity"]),
        )[0]
        assert not source.message_id_present(
            "INBOX",
            "<move@example.com>",
            expected_uidvalidity=str(origin["uidvalidity"]),
        )[0]

    auth_server = _IMAPServer(cert, key, {1: b"message"}, auth_refused=True)
    with _ServerContext(auth_server):
        source = ImapSource(
            "loopback-imap",
            IMAPConnectionConfig(
                host="localhost",
                port=auth_server.server_address[1],
                username="user",
                credential_ref="test:imap",
                ca_file=str(cert),
            ),
            credential_resolver=resolver,
        )
        with pytest.raises(imaplib.IMAP4.error):
            list(source.messages())


def test_provider_profiles_are_secret_free_and_wire_canonical_services(tmp_path):
    cert, key = _certificate(tmp_path)
    server = _SMTPServer(cert, key)
    store = CampaignStore(tmp_path / "provider.sqlite3")
    store.register_sender_policy(
        SenderPolicy(
            ref="sender-policy",
            provider="loopback",
            email="sender@example.com",
            max_per_minute=10,
            max_per_hour=10,
            max_per_day=10,
            per_domain_per_minute=10,
        )
    )
    store.register_provider_profile(
        ProviderProfile(
            ref="loopback-smtp",
            kind=ProviderKind.SMTP,
            host="localhost",
            port=server.server_address[1],
            username="user",
            credential_ref="test:smtp",
            tls_mode=TLSMode.STARTTLS,
            sender_policy_ref="sender-policy",
            ca_file=str(cert),
        )
    )
    with store.connect() as connection:
        row = connection.execute("SELECT * FROM provider_profiles").fetchone()
        assert "synthetic-password" not in str(dict(row))
    provider = ProviderService(store)
    assert provider.validate("loopback-smtp")["external_contact"] is False
    assert provider.credential_status(
        "loopback-smtp", MappingCredentialResolver({"test:smtp": "synthetic-password"})
    )["available"]
    with pytest.raises(ValueError, match="metadata is not persisted"):
        store.register_provider_profile(
            ProviderProfile(
                ref="labeled-profile",
                kind=ProviderKind.SMTP,
                host="localhost",
                port=2525,
                username="user",
                credential_ref="test:smtp",
                tls_mode=TLSMode.STARTTLS,
                metadata={"note": "synthetic-password"},
            )
        )

    list_id = store.create_list("provider-audience")
    store_contact = store.upsert_contact(
        "a@example.com",
        source_file="test",
        source_record_position=0,
        original_input="a@example.com",
    )
    store.add_to_list(list_id, store_contact)
    campaign_id = store.create_campaign(
        name="provider-campaign",
        description="loopback",
        sender_config_ref="sender-policy",
        template_ref="provider-template",
        audience_list_id=list_id,
    )
    service = OutboundService(store)
    service.register_sender_policy(
        SenderPolicy(
            ref="sender-policy",
            provider="loopback",
            email="sender@example.com",
            max_per_minute=10,
            max_per_hour=10,
            max_per_day=10,
            per_domain_per_minute=10,
        )
    )
    service.prepare_campaign(
        campaign_id,
        template=TemplateDefinition("provider-template", "1", "Subject", "Body"),
        sender=SenderPolicy(
            ref="sender-policy",
            provider="loopback",
            email="sender@example.com",
            max_per_minute=10,
            max_per_hour=10,
            max_per_day=10,
            per_domain_per_minute=10,
        ),
    )
    service.authorize_simulation(campaign_id)
    with _ServerContext(server):
        diagnostic = provider.local_protocol_diagnostic(
            "loopback-smtp", MappingCredentialResolver({"test:smtp": "synthetic-password"})
        )
        assert diagnostic["status"] == "local_protocol_passed"
        report, _ = service.execute_provider(
            campaign_id,
            "loopback-smtp",
            MappingCredentialResolver({"test:smtp": "synthetic-password"}),
            gate=LiveSendGate(enabled=True),
        )
    assert report.counts[DeliveryState.ACCEPTED.value] == 1
    attempt = store.latest_attempt(store.list_intents(campaign_id)[0]["id"])
    assert attempt["transport_phase"] == SMTPPhase.FINAL_DATA_RESPONSE.value

    store.register_sender_policy(
        SenderPolicy(
            ref="other-policy",
            provider="loopback",
            email="sender@example.com",
            max_per_minute=10,
            max_per_hour=10,
            max_per_day=10,
            per_domain_per_minute=10,
        )
    )
    store.register_provider_profile(
        ProviderProfile(
            ref="loopback-smtp",
            kind=ProviderKind.SMTP,
            host="localhost",
            port=server.server_address[1],
            username="user",
            credential_ref="test:smtp",
            tls_mode=TLSMode.STARTTLS,
            sender_policy_ref="other-policy",
            ca_file=str(cert),
        )
    )
    with pytest.raises(ValueError, match="not associated"):
        service.execute_provider(
            campaign_id,
            "loopback-smtp",
            MappingCredentialResolver({"test:smtp": "synthetic-password"}),
            gate=LiveSendGate(enabled=True),
        )


def test_unsupported_imap_starttls_profile_is_rejected_explicitly(tmp_path):
    store = CampaignStore(tmp_path / "unsupported.sqlite3")
    with pytest.raises(ValueError, match="IMAP STARTTLS is not implemented"):
        store.register_provider_profile(
            ProviderProfile(
                ref="imap-starttls",
                kind=ProviderKind.IMAP,
                host="localhost",
                port=143,
                username="user",
                credential_ref="test:imap",
                tls_mode=TLSMode.STARTTLS,
            )
        )


def test_oauth2_profile_is_validated_before_smtp_connection(tmp_path):
    store = CampaignStore(tmp_path / "oauth.sqlite3")
    store.register_provider_profile(
        ProviderProfile(
            ref="smtp-oauth",
            kind=ProviderKind.SMTP,
            host="localhost",
            port=2525,
            username="user",
            credential_ref="test:oauth",
            auth_method=AuthenticationMethod.OAUTH2,
            tls_mode=TLSMode.STARTTLS,
        )
    )
    assert ProviderService(store).validate("smtp-oauth")["status"] == "configuration_valid"
    pool = ProviderService(store).smtp_pool(
        "smtp-oauth",
        MappingCredentialResolver({"test:oauth": '{"access_token":"synthetic-token"}'}),
        gate=LiveSendGate(enabled=True),
    )
    assert pool.configs[0].auth_method is AuthenticationMethod.OAUTH2
