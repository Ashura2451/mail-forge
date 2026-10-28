# Email Standards and Sender Diagnostics

Mailforge's Email Standards workspace is local-first diagnostic evidence. It
does not test provider reputation, inbox placement, consent, or delivery.
DNS access is opt-in (`--network` on the CLI or the equivalent API request),
and normal startup performs no network requests.

## Operator flow

1. Register the visible From, Reply-To, envelope MAIL FROM, sending domain,
   and DKIM selector in Email Standards.
2. Run domain diagnostics with deterministic DNS fixtures or explicitly enabled
   DNS access. Review SPF syntax/evaluation separately from DMARC policy and
   alignment. DKIM record structure is inspected; cryptographic signature
   verification is not claimed unless separately evidenced.
3. Validate the exact prepared MIME. Findings include header/address safety,
   multipart/plain-text structure, links, CID references, attachments, and
   marketing unsubscribe requirements.
4. Configure a provider-neutral HTTPS unsubscribe endpoint. Create opaque,
   recipient/list-scoped tokens; tokens are stored only as hashes. The local
   endpoint accepts the exact RFC 8058 POST body and is idempotent. GET never
   changes suppression state.
5. For marketing campaigns, set `unsubscribe_endpoint_ref`. Campaign
   preparation derives a fresh recipient-scoped URL and delegates MIME
   construction to `MessageComposer`, emitting `List-Unsubscribe`. The local
   campaign boundary blocks the RFC 8058 header pair until a real DKIM signing
   integration can sign the final bytes; the low-level composer still supports
   that pair for an external signing boundary.
6. Import DMARC aggregate XML, gzip, bzip2, or a single safe ZIP member. The
   import is bounded, rejects external entities and traversal, deduplicates by
   report ID, and stores metadata/rows in the canonical SQLite database.

## Interfaces

The browser route is **Email Standards**. The local API exposes:

- `/api/v1/email-standards` for identities, domain/message diagnostics,
  history, guidance, unsubscribe configuration/tokens/readiness, and DMARC
  imports;
- `/api/v1/unsubscribe/{endpoint_ref}/{token}` for the cookie-free RFC 8058
  POST boundary; and
- `/api/v1/reports/email-standards` for aggregate reporting.

The CLI uses `mailforge email-standards` with `identities`,
`register-identity`, `domain`, `message`, `history`, `guidance`,
`dmarc-import`, and `dmarc-summary` subcommands. CLI domain inspection is
offline unless `--network` is supplied.

Google and Yahoo guidance is shown as dated, scoped reference material. The
implementation reviewed [Google sender guidance](https://support.google.com/mail/answer/81126)
and [Yahoo Sender Hub best practices](https://senders.yahooinc.com/best-practices/)
on 2026-10-28. Standards references include [RFC 7208](https://www.rfc-editor.org/rfc/rfc7208),
[RFC 6376](https://www.rfc-editor.org/rfc/rfc6376),
[RFC 7489](https://www.rfc-editor.org/rfc/rfc7489), and
[RFC 8058](https://www.rfc-editor.org/rfc/rfc8058.html).

## Evidence labels and limits

`pass`, `warning`, `blocked`, and `unknown` findings retain their source,
ruleset, and observation time. Imported `Authentication-Results` and
`Received-SPF` headers are untrusted claims unless a trusted receiver boundary
explicitly supplies them. `external_configured` is not `external_verified`;
Mailforge does not invent public endpoint or provider evidence. The current
build does not perform DKIM cryptographic verification, so it does not promote
readiness to `full_one_click_requirements_verified`; a future trusted verifier
must provide that evidence in addition to the tested header pair and endpoint.

The current bounded ruleset limits messages to 25 MiB, reports to 5 MiB input,
DMARC rows to 10,000, DNS SPF recursion to 10 lookups, and diagnostics/history
to bounded durable records. These are local safety limits, not provider quota
claims.
