# Mailforge policy authority

This document records the implemented local policy foundation from
`MAILFORGE-POLICY-AUTHORITY-011`. It is an implementation contract and does
not claim legal compliance, recipient consent, or live-provider readiness.

## Ownership

`CampaignStore` remains the only modern SQLite authority. `EligibilityService`
is the only recipient decision service. Campaign preparation, preview,
scheduling, simulation, resume, retry, and the gated SMTP application path
rely on the same campaign service and re-evaluate mutable restrictions before
dispatch.

## Policy model

- A contact import records identity and source provenance only. It creates no
  consent or preference evidence.
- Authorization evidence is append-only and records status, purpose, source,
  scope, actor, timestamp, evidence reference, and policy version.
- Preferences are optional, purpose-specific, scoped, and historical.
- Active suppression always wins over preferences or operator declarations.
  Supported scopes are global, sender, list, campaign, and communication
  purpose. Hard bounce, complaint, unsubscribe, and manual hold categories
  receive deterministic precedence and history is preserved.
- Operator declarations authorize an operation or simulation. They are not
  recipient consent and do not override mandatory restrictions.
- Missing evidence is explicitly `authorization_unknown`; ordinary import,
  deduplication, preview, preparation, and synthetic simulation remain usable
  after an operator declaration.

## Lifecycle and recovery

Schema migration 5 adds evidence, preference, declaration, suppression-history,
and lifecycle-planning tables, plus contact deactivation state. Corrections,
exports, deactivation, deletion planning, backup, restore, and policy events
are local and auditable. Actual contact deletion is intentionally not
automatic; deletion plans expose conflicts with retained suppression records.

Trusted synthetic inbound evidence continues to use the existing inbound
review/action boundary. Untrusted or ambiguous inbound messages cannot mutate
recipient permissions automatically. Reimporting a contact does not reactivate
deactivated state or remove suppression.

## Explicit limits

The implementation does not determine jurisdiction, prove a recipient's
consent from an address, authenticate a real mailbox, provide hosted identity
management, or enable real SMTP delivery. Provider qualification, legal policy,
retention approval, and production operations remain external decisions.
