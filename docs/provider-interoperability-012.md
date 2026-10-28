# Mailforge provider interoperability

This document records the local implementation from
`MAILFORGE-PROVIDER-INTEROPERABILITY-012`. It does not claim a successful
connection to a commercial provider, inbox delivery, provider quota knowledge,
or production-send authorization.

## Implemented boundary

`ProviderProfile` is a secret-free durable record in `CampaignStore` schema 11.
It identifies SMTP or IMAP, endpoint, TLS mode, authentication method,
mailbox, sender-policy reference, limits, and a symbolic credential reference.
`ProviderService` turns that record into runtime adapters; it does not own
campaign, contact, suppression, or inbound-processing state.

Credential values are resolved only at runtime by an injected resolver. The
local resolver supports `env:NAME`; tests use an in-memory `test:NAME` resolver.
SQLite rows, backups, diagnostics, CLI output, campaign state, and provider
profiles contain references and redacted status only. Provider Operations adds
OAuth2 authorization-code/PKCE state handling, token-expiry and refresh seams,
and XOAUTH2 transport support; token exchange and commercial-provider
qualification remain external or fixture-only unless separately authorized.

The CLI exposes safe profile operations:

```text
mailforge provider list --database PATH
mailforge provider register ... --credential-ref env:NAME
mailforge provider validate --database PATH --ref PROFILE
mailforge provider credentials --database PATH --ref PROFILE
mailforge provider local-diagnostic --database PATH --ref PROFILE
mailforge provider diagnostics --database PATH [--ref PROFILE]
```

`local-diagnostic` accepts loopback endpoints only. Provider Operations also
exposes account listing, capability inspection, explicit external diagnostics,
and a separately authorized one-shot `test-send`. The existing campaign
coordinator remains the only route from a prepared campaign to an SMTP
transport and still requires campaign
authorization, eligibility, the provider's exact sender-policy association,
leases, pacing, and the live-send gate. Arbitrary profile metadata is rejected
instead of being persisted, keeping the durable profile schema explicit and
secret-free.

## SMTP

The canonical pool supports implicit TLS and mandatory STARTTLS with standard
library `smtplib`, certificate-chain and hostname verification, TLS 1.2 or
newer, capability negotiation, authenticated submission, connection reuse,
weighted account selection, health checks, teardown, and sender-domain policy.
The recorded transaction vocabulary is:

```text
CONNECT → TLS_NEGOTIATION → AUTHENTICATION → MAIL_FROM → RCPT_TO
→ DATA_START → DATA_TRANSMISSION → FINAL_DATA_RESPONSE
→ SESSION_COMPLETION
```

The durable attempt stores transport phase and response code. Recipient-level
refusals are preserved through the canonical result path. A known 4xx/5xx
response is classified as transient/permanent as appropriate. A disconnect
after possible DATA acceptance is `ambiguous`, has no automatic retry, and
remains available for explicit reconciliation. SMTP acceptance is only
provider acceptance, not inbox delivery.

## IMAP

`ImapSource` uses implicit TLS through `imaplib.IMAP4_SSL` (the only currently
implemented IMAP TLS mode), validates the configured certificate,
authenticates at runtime, negotiates capabilities, selects the mailbox
read-only, searches and fetches by UID using `BODY.PEEK[]`, and applies a
message-size bound. Source identities are `UIDVALIDITY:UID`; cursors are
durable through the existing `InboundService` and `CampaignStore` acquisition
records. A UIDVALIDITY change invalidates the cursor instead of silently
skipping messages. Fetching is bounded and repeated ingestion is idempotent.
The adapter never deletes or marks remote messages processed.

## Local evidence and limits

`tests/test_provider_interoperability.py` runs actual loopback TLS sockets with
generated test certificates and synthetic credentials. It covers successful
implicit TLS and STARTTLS, missing STARTTLS, certificate/hostname failure,
authentication refusal, SMTP response classification, post-DATA disconnect,
IMAP capability/authentication/read-only UID acquisition, cursor and
UIDVALIDITY behavior, profile secrecy, diagnostics, and canonical campaign
execution. No public endpoint, mailbox, credential file, or real recipient was
used.

IMAP STARTTLS is rejected explicitly rather than being treated as implicit TLS.
The following remain external or explicitly pending: commercial-provider OAuth
consent/token exchange, provider-advertised or actual quota discovery,
Gmail/Microsoft 365 qualification,
DKIM signing, SPF/DMARC verification, inbox placement, remote mailbox
acknowledgment, production worker supervision, and controlled real-provider
qualification. Future qualification requires a separately authorized,
single-destination workflow with an approved provider sandbox, credential
source, sender identity, rollback/reconciliation evidence, and operator
approval.

The 019 implementation record is [`docs/provider-operations-019.md`](provider-operations-019.md).

Relevant protocol references are [RFC 6409](https://www.rfc-editor.org/rfc/rfc6409),
[RFC 8314](https://www.rfc-editor.org/rfc/rfc8314),
[RFC 9051](https://www.rfc-editor.org/rfc/rfc9051.html),
[Python `smtplib`](https://docs.python.org/3.15/library/smtplib.html), and
[Python `imaplib`](https://docs.python.org/3.15/library/imaplib.html).
