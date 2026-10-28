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
