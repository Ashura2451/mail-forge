from email.message import EmailMessage

from mailforge.application.inbound import InboundService
from mailforge.application.inbound_operations import InboundOperations
from mailforge.application.mailbox_operations import (
    MailboxOperationsError,
    MailboxOperationsService,
)
from mailforge.application.provider_operations import ProviderOperationsService
from mailforge.domain.inbound import InboundSourceType, SourceMessage
from mailforge.domain.provider import ProviderKind, ProviderProfile, TLSMode
from mailforge.infrastructure.campaign_store import CampaignStore


class _FixtureMailbox:
    source_type = InboundSourceType.SYNTHETIC

    def __init__(self, source_ref: str, items: list[SourceMessage]):
        self.source_ref = source_ref
        self.items = items

    def messages(self, cursor=None, limit=None):
        yield from self.items[:limit]


def _message(message_id: str, subject: str, *, references: str = "") -> bytes:
    message = EmailMessage()
    message["From"] = "alice@example.com"
    message["To"] = "operator@example.com"
    message["Message-ID"] = message_id
    if references:
        message["References"] = references
    message["Date"] = "Tue, 28 Oct 2026 10:00:00 +0000"
    message["Subject"] = subject
    message.set_content("A safe fixture message.")
    return message.as_bytes()


def test_remote_identity_flags_threads_and_mailbox_action_lifecycle(tmp_path):
    store = CampaignStore(tmp_path / "mailforge.sqlite3")
    inbound = InboundService(store)
    operations = InboundOperations(store)
    inbound.set_operations(operations)
    source = _FixtureMailbox(
        "fixture-account::INBOX",
        [
            SourceMessage(
                "fixture-account::INBOX",
                "9:41",
                _message("<root@example.com>", "Root"),
                mailbox="INBOX",
                uidvalidity="9",
                uid="41",
                flags=(r"\\Seen",),
                account_ref="fixture-account",
            ),
            SourceMessage(
                "fixture-account::INBOX",
                "9:42",
                _message("<reply@example.com>", "Re: Root", references="<root@example.com>"),
                mailbox="INBOX",
                uidvalidity="9",
                uid="42",
                account_ref="fixture-account",
            ),
        ],
    )

    report = inbound.ingest(source, trusted_synthetic=True)
    assert report.processed == 2
    rows = store.list_inbound_messages()
    assert {row["remote_uid"] for row in rows} == {"41", "42"}
    assert rows[0]["remote_uidvalidity"] == "9"
    assert operations.search(read=True)["total"] == 1
    reply = next(row for row in rows if row["message_id"] == "<reply@example.com>")
    conversation = operations.conversation(reply["id"])
    assert conversation["confidence"] == "header_evidence"
    assert len(conversation["items"]) == 2
    draft = operations.prepare_draft(reply["id"], "reply_all")
    assert draft["provenance"]["reply_mode"] == "reply_all"
    assert draft["provenance"]["to"] == ["alice@example.com"]
    document = operations.message_studio.get_document(draft["document_id"])
    assert document["draft"]["document"]["to"] == ["alice@example.com"]

    action = operations.request_mailbox_action(
        source.source_ref,
        action="mark_unread",
        authorization_ref="owner-030",
        idempotency_key="mailbox-action-030",
        message_id=rows[0]["id"],
    )
    assert action["action"] == "mark_unread"
    assert action["action_state"] == "requested"
    assert action["events"]


def test_mailbox_schema_and_permanent_deletion_guard_are_explicit(tmp_path):
    store = CampaignStore(tmp_path / "mailforge.sqlite3")
    with store.connect() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(inbound_messages)")}
        action_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(inbound_mailbox_actions)")
        }
    assert {"remote_uid", "remote_uidvalidity", "remote_flags_json"} <= columns
    assert {"action_state", "uid", "uidvalidity", "observed_state"} <= action_columns

    inbound = InboundService(store)
    operations = InboundOperations(store)
    provider_operations = ProviderOperationsService(store)
    mailbox = MailboxOperationsService(
        store,
        inbound=inbound,
        inbound_operations=operations,
        provider_operations=provider_operations,
    )
    try:
        mailbox.execute_action(
            "fixture-account",
            action="delete",
            authorization_ref="owner-030",
            purpose="fixture verification",
            expires_at="2099-01-01T00:00:00+00:00",
            idempotency_key="delete-action-030",
            message_id="missing",
            uidvalidity="9",
            uid="41",
        )
    except MailboxOperationsError as error:
        assert error.code == "action_refused"
    else:
        raise AssertionError("permanent deletion must be refused")


def test_mailbox_mutations_default_deny_external_and_stale_identities(tmp_path):
    store = CampaignStore(tmp_path / "mailbox-deny.sqlite3")
    inbound = InboundService(store)
    operations = InboundOperations(store)
    inbound.set_operations(operations)
    provider_operations = ProviderOperationsService(store)
    provider_operations.register(
        ProviderProfile(
            ref="external-imap",
            kind=ProviderKind.IMAP,
            host="imap.example.invalid",
            port=993,
            username="user",
            credential_ref="env:MAILFORGE_TEST_IMAP",
            tls_mode=TLSMode.IMPLICIT,
        )
    )
    provider_operations.configure_inbound("external-imap", mailbox="INBOX", mutation_enabled=True)
    source = _FixtureMailbox(
        "external-imap",
        [
            SourceMessage(
                "external-imap",
                "7:1",
                _message("<deny@example.com>", "Deny"),
                mailbox="INBOX",
                uidvalidity="7",
                uid="1",
                account_ref="external-imap",
            )
        ],
    )
    inbound.ingest(source, trusted_synthetic=True)
    message = store.list_inbound_messages()[0]
    mailbox = MailboxOperationsService(
        store,
        inbound=inbound,
        inbound_operations=operations,
        provider_operations=provider_operations,
    )
    try:
        mailbox.execute_action(
            "external-imap",
            action="mark_read",
            authorization_ref="owner-030",
            purpose="fixture verification",
            expires_at="2099-01-01T00:00:00+00:00",
            idempotency_key="external-deny",
            message_id=message["id"],
            uidvalidity="7",
            uid="1",
        )
    except MailboxOperationsError as error:
        assert error.code == "external_mailbox_disabled"
    else:
        raise AssertionError("external mailbox mutation must be refused")

    provider_operations.register(
        ProviderProfile(
            ref="loopback-imap",
            kind=ProviderKind.IMAP,
            host="localhost",
            port=993,
            username="user",
            credential_ref="env:MAILFORGE_TEST_IMAP",
            tls_mode=TLSMode.IMPLICIT,
        )
    )
    provider_operations.configure_inbound("loopback-imap", mailbox="INBOX", mutation_enabled=True)
    loopback_source = _FixtureMailbox(
        "loopback-imap",
        [
            SourceMessage(
                "loopback-imap",
                "7:2",
                _message("<stale@example.com>", "Stale"),
                mailbox="INBOX",
                uidvalidity="7",
                uid="2",
                account_ref="loopback-imap",
            )
        ],
    )
    inbound.ingest(loopback_source, trusted_synthetic=True)
    stale_message = next(
        row for row in store.list_inbound_messages() if row["message_id"] == "<stale@example.com>"
    )
    try:
        mailbox.execute_action(
            "loopback-imap",
            action="mark_read",
            authorization_ref="owner-030",
            purpose="fixture verification",
            expires_at="2099-01-01T00:00:00+00:00",
            idempotency_key="stale-deny",
            message_id=stale_message["id"],
            uidvalidity="7",
            uid="999",
        )
    except MailboxOperationsError as error:
        assert error.code == "identity_mismatch"
    else:
        raise AssertionError("stale UID mutation must be refused")


def test_in_progress_idempotency_requires_reconciliation(tmp_path):
    store = CampaignStore(tmp_path / "mailbox-idempotency.sqlite3")
    inbound = InboundService(store)
    operations = InboundOperations(store)
    inbound.set_operations(operations)
    provider_operations = ProviderOperationsService(store)
    provider_operations.register(
        ProviderProfile(
            ref="loopback-imap",
            kind=ProviderKind.IMAP,
            host="localhost",
            port=993,
            username="user",
            credential_ref="env:MAILFORGE_TEST_IMAP",
            tls_mode=TLSMode.IMPLICIT,
        )
    )
    provider_operations.configure_inbound("loopback-imap", mailbox="INBOX", mutation_enabled=True)
    source = _FixtureMailbox(
        "loopback-imap",
        [
            SourceMessage(
                "loopback-imap",
                "7:3",
                _message("<retry@example.com>", "Retry"),
                mailbox="INBOX",
                uidvalidity="7",
                uid="3",
                account_ref="loopback-imap",
            )
        ],
    )
    inbound.ingest(source, trusted_synthetic=True)
    message = store.list_inbound_messages()[0]
    requested = operations.request_mailbox_action(
        "loopback-imap",
        action="mark_read",
        authorization_ref="owner-030",
        idempotency_key="retry-key",
        message_id=message["id"],
    )
    store.transition_inbound_mailbox_action(requested["id"], action_state="in_progress")
    mailbox = MailboxOperationsService(
        store,
        inbound=inbound,
        inbound_operations=operations,
        provider_operations=provider_operations,
    )
    result = mailbox.execute_action(
        "loopback-imap",
        action="mark_read",
        authorization_ref="owner-030",
        purpose="fixture verification",
        expires_at="2099-01-01T00:00:00+00:00",
        idempotency_key="retry-key",
        message_id=message["id"],
        uidvalidity="7",
        uid="3",
    )
    assert result["action_state"] == "requires_review"
    assert result["status"] == "ambiguous"


def test_mailbox_claim_and_interrupted_sync_recovery_are_atomic(tmp_path):
    store = CampaignStore(tmp_path / "mailbox-recovery.sqlite3")
    inbound = InboundService(store)
    operations = InboundOperations(store)
    inbound.set_operations(operations)
    source = _FixtureMailbox("fixture-account", [])
    inbound.register_source(source, trusted_synthetic=True)

    first_run, cursor = store.start_inbound_run(source.source_ref)
    assert cursor is None
    second_run, _ = store.start_inbound_run(source.source_ref)
    with store.connect() as connection:
        prior = connection.execute(
            "SELECT status, error_message FROM inbound_runs WHERE id = ?", (first_run,)
        ).fetchone()
    assert second_run != first_run
    assert prior["status"] == "interrupted"
    assert "recovered abandoned" in prior["error_message"]

    action = operations.request_mailbox_action(
        source.source_ref,
        action="mark_read",
        authorization_ref="owner-030",
        idempotency_key="atomic-claim",
    )
    assert store.claim_inbound_mailbox_action(action["id"])["action_state"] == "validated"
    assert store.claim_inbound_mailbox_action(action["id"]) is None
