# Provider interoperability 012 independent review

**Review date:** 2026-10-28
**Task:** `MAILFORGE-PROVIDER-INTEROPERABILITY-012`
**Reviewed branch:** `feat/mailforge-provider-interoperability-012`

## Review method

A fresh read-only reviewer inspected the actual implementation, tests, staged
scope, and local validation evidence. The reviewer did not modify files or
contact external networks. Cross-model review capability was unavailable in
the review session, so independence is documented as degraded rather than
claimed as a multi-model approval.

## Findings and resolution

The first pass identified four major issues and several minor gaps:

- IMAP STARTTLS profiles were accepted despite an implicit-TLS-only adapter;
  profile validation now rejects that mode explicitly and the limitation is
  documented.
- OAuth2 could reach adapter construction before failing; provider construction
  now rejects it before a pool or socket is created, and validation reports
  `authentication_unsupported`.
- `sender_policy_ref` was persisted but not enforced; canonical provider
  execution now requires an exact match with the campaign sender policy.
- Arbitrary provider metadata could bypass heuristic secret scanning; durable
  profile registration now rejects metadata rather than persisting an
  unbounded field.
- Phase propagation, pool cleanup, `ingest_provider`, migration coverage, and
  negative sender-association coverage were strengthened.

The remaining observations are minor: `DATA_START` and
`SESSION_COMPLETION` remain vocabulary values rather than durable terminal
states, because the durable attempt records the most consequential observed
phase through `FINAL_DATA_RESPONSE`; the provider validation status for
unsupported authentication is now explicit.

## Final verdict

**Approve with minor nits.** The reviewer observed 12 provider interoperability
tests and 71 focused regression tests. Its full-suite invocation encountered
the repository's existing import-path issue (146 passed, 1 failed); the
executor then ran the packaged wheel in the installed environment and observed
147 passing tests. Ruff, formatting, `git diff --check`, packaging, and the
offline secret guard passed. Only loopback fixtures were used; no external
provider, mailbox, credential, or real recipient was accessed.
