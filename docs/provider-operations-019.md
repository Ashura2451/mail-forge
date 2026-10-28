# Mailforge provider operations

This guide describes the provider account and qualification boundary added by
`MAILFORGE-PROVIDER-OPERATIONS-019`. It is an implementation guide, not a
claim of commercial-provider readiness or inbox delivery.

## Account setup

Create a named SMTP or IMAP account through the Providers screen, the `/api/v1`
provider routes, or the `mailforge provider` CLI. Store only a symbolic
credential reference such as `env:MAILFORGE_SMTP_PASSWORD` or a synthetic
`test:` reference. Secret values are resolved at operation time and are not
returned to the browser, SQLite, diagnostics, or normal logs.

Configuration validation is local and does not contact the configured host.
Loopback protocol fixtures may be diagnosed locally. A campaign can bind one
explicit account; synthetic campaigns remain available without an account.

The capability registry includes generic SMTP/IMAP, Google Gmail/Workspace,
and Microsoft 365/Outlook presets. Presets provide defaults, scopes, TLS
expectations, restrictions, and official documentation links; they do not
silently authorize a connection.

## Authentication

OAuth authorization-code flows use an opaque state, S256 PKCE, exact loopback
redirect matching, expiry, single-use callbacks, and account identity checks.
Tokens are runtime material only. SMTP and IMAP OAuth accounts use XOAUTH2;
password authentication remains available for generic profiles. Local tests
inject token exchange and refresh behavior, while commercial-provider consent,
tenant configuration, and token endpoints require separate qualification.

Google's documented Gmail scope and XOAUTH2 protocol are represented by the
Google preset. Microsoft SMTP/IMAP scopes and `offline_access` are represented
by the Microsoft preset; SMTP AUTH must be enabled by the tenant/mailbox.
See the [Google SMTP/IMAP documentation](https://developers.google.com/workspace/gmail/imap/imap-smtp),
[Google XOAUTH2 protocol](https://developers.google.com/workspace/gmail/imap/xoauth2-protocol),
[Microsoft OAuth mail authentication](https://learn.microsoft.com/en-us/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth),
and [Microsoft SMTP AUTH guidance](https://learn.microsoft.com/en-us/exchange/clients-and-mobile-in-exchange-online/authenticated-client-smtp-submission).

## Explicit external operations

Connection, authentication, inbound-readiness, and controlled test-send
operations against a non-loopback endpoint require an expiring grant naming:

- the account and exact endpoint;
- the operation and purpose;
- sender and recipient for a test message;
- the maximum submission count; and
- an owner authorization reference and expiry.

The grant is validated at the service boundary and persisted as sanitized
authorization evidence. A controlled test send is one bounded submission with
a stable idempotency key. A repeated key returns the original durable result;
it does not submit again. SMTP acceptance is reported as provider acceptance,
not inbox delivery, and an ambiguous post-DATA result is never retried
automatically.

No external authorization or real credentials were supplied for this
milestone. Therefore `REAL_PROVIDER_QUALIFICATION_NOT_PERFORMED` is the
correct status. No live campaign endpoint was enabled.

## Controlled qualification readiness — not performed

This is a preparation plan only. It does not authorize network contact, token
exchange, message submission, mailbox retrieval, or campaign release. Use one
provider at a time, one owner-controlled destination, one message, and an
expiring authorization reference. Capture the provider response, Mailforge
correlation ID, rendered-message hash, and (only if separately authorized) the
owned mailbox observation. SMTP acceptance is not inbox delivery.

### Preferred order

1. **Mailtrap Email Sandbox** — create a sandbox, copy its SMTP host/port and
   credentials from the Integration tab, and configure only a symbolic
   `env:MAILFORGE_MAILTRAP_PASSWORD` reference. Mailtrap documents the sandbox
   SMTP setup and captured-inbox behavior at [Sandbox SMTP Integration](https://docs.mailtrap.io/email-sandbox/setup/sandbox-smtp-integration).
   The first check is TLS connection and authentication; the one-message check
   is provider acceptance plus appearance in the sandbox inbox.
2. **Dedicated Gmail test account** — create a Google Cloud project, enable
   Gmail API, register a localhost web OAuth client, and use the server-side
   authorization-code flow. Request only the minimum approved scope: use
   `gmail.send` for sending or `gmail.readonly` for inbound retrieval, with
   `access_type=offline` only when the owner approves refresh-token retention.
   Google documents server-side OAuth setup at [Using OAuth 2.0 for
   Web Server Applications](https://developers.google.com/identity/protocols/oauth2/web-server),
   Gmail server authorization at [Implement server-side
   authorization](https://developers.google.com/workspace/gmail/api/auth/web-server),
   and scope sensitivity at [Choose Gmail API scopes](https://developers.google.com/workspace/gmail/api/auth/scopes).
3. **Dedicated Outlook.com test account** — register an application in
   Microsoft Entra, use authorization code or device flow, and request only
   `https://outlook.office.com/SMTP.Send` for SMTP or
   `https://outlook.office.com/IMAP.AccessAsUser.All` for inbound retrieval,
   plus Microsoft `offline_access` only if approved. Microsoft documents the protocol
   OAuth permissions at [Authenticate an IMAP, POP or SMTP connection using
   OAuth](https://learn.microsoft.com/en-us/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth).
4. **Optional SES sandbox** — verify the sender identity and the single
   owner-controlled recipient, then use the sandbox SMTP credentials. AWS
   documents that sandbox accounts require verified identities and recipients
   at [Verified identities in Amazon SES](https://docs.aws.amazon.com/ses/latest/dg/verify-addresses-and-domains.html).

### Operator setup and evidence checklist

- Store OAuth client files, refresh tokens, SMTP passwords, and access tokens
  outside the repository in the operator's approved secret store or environment
  variables. Mailforge durable state receives only symbolic `env:` or
  `keychain:` references; never paste tokens into chat, tests, SQLite, or a
  PR.
- Record the exact test sender, one owner-controlled recipient, provider
  account reference, authorization reference, scope set, message hash, and
  expiry before any connection attempt. Do not use historical MAILER
  credentials or private recipient lists.
- Run in order: local protocol diagnostic, authenticated connection check,
  one bounded SMTP submission, provider response correlation, and—only when
  approved—inbound retrieval of that same message. Capture failures such as
  invalid credentials, consent denial, scope mismatch, TLS/hostname failure,
  SMTP AUTH disabled, rate/size limits, timeout, duplicate correlation, and
  ambiguous post-DATA disconnect.
- Revoke the OAuth grant, rotate or delete the sandbox credential, remove the
  provider profile, and retain only redacted evidence after the test. No bulk
  send, campaign release, or real-recipient operation is part of this plan.

**Readiness status:** `QUALIFICATION_READINESS_PREPARED`; actual external
qualification: `NOT PERFORMED`.

## Operational commands

```text
mailforge provider accounts --database PATH
mailforge provider capabilities --database PATH
mailforge provider validate --database PATH --ref ACCOUNT
mailforge provider diagnostic --database PATH --ref ACCOUNT --operation connection \
  --authorization-ref OWNER-AUTH --purpose "single connection check" --expires-at ISO_TIMESTAMP
mailforge provider test-send --database PATH --ref ACCOUNT --sender SENDER \
  --recipient OWNER_CONTROLLED_RECIPIENT --message MESSAGE.eml \
  --idempotency-key UNIQUE_KEY --authorization-ref OWNER-AUTH \
  --purpose "single qualification message" --expires-at ISO_TIMESTAMP
```

The CLI and browser call the same application service. External commands must
be separately authorized; ordinary account editing and configuration
validation do not contact arbitrary hosts.
