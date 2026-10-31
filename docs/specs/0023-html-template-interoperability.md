# 0023. Custom HTML template interoperability

**Date:** 2026-10-30
**Status:** In Progress
**Task:** `MAILFORGE-HTML-TEMPLATE-INTEROPERABILITY-034`

## Summary

Extend the existing Message Studio with a custom HTML composition mode. An
operator may paste source or import a bounded HTML file with local image/CSS
assets, preserve the original source as part of the normal immutable revision,
and use the existing personalization, asset, MIME, campaign, authorization,
and outbound-proof authorities.

## Requirements

- Structured composition remains unchanged and remains the default mode.
- Custom documents preserve `original_source`, its SHA-256 identity, a bounded
  asset manifest, diagnostics, and an explicitly derived prepared
  representation in the same `message_revisions` document JSON.
- HTML/HTM files and a bounded ZIP asset bundle are accepted only after file
  signature, path, size, count, depth, compression-ratio, duplicate, symlink,
  active-content, and relative-reference checks.
- No remote URL is fetched during import, validation, preview, export, or MIME
  preparation. Remote references are visible diagnostics and are removed from
  the safe preview/prepared local-resource representation.
- Script, event-handler, form, frame, object/embed, SVG active content, unsafe
  URLs, CSS imports, and filesystem references are rejected or removed with
  explicit severity diagnostics. Email conditional comments and VML are
  retained as source but reported as client-specific compatibility warnings.
- Simple CSS selectors may be inlined deterministically; media queries and
  Outlook conditional markup remain in the prepared source. The original
  source is never overwritten by transformation.
- Allowed merge markers use the existing bounded syntax and are rendered by
  `SafeTemplateRenderer`; rendered URL values are checked again for safe
  schemes. Plain text is generated with link preservation and may be edited.
- Preview is an opaque, no-origin sandbox frame with a resource-free HTML
  snapshot. The privileged application DOM never receives imported HTML.
- Custom revisions use the existing publish, campaign snapshot, Message Studio
  renderer, `MessageComposer`, eligibility, grant, reservation, idempotency,
  ambiguity, and 033 proof paths. There is no direct-send or caller-MIME path.
- Export writes the preserved original HTML and application-owned assets to a
  bounded ZIP without exposing local paths.

## Decision

Use `composition_mode: "custom_html"` inside the existing Message Studio
document JSON rather than adding a second document/revision authority. The
custom HTML helper is a bounded parser/transformer, not a browser renderer and
not a general-purpose HTML sanitizer. It produces diagnostics and fails closed
for content that cannot be made safe. Existing `message_assets` stores bytes
and hashes; local references are mapped to deterministic CID identities for
MIME and data URLs for preview.

The source contract is:

```json
{
  "version": 1,
  "composition_mode": "custom_html",
  "name": "Newsletter",
  "subject": "...",
  "preheader": "...",
  "plain_text_override": null,
  "custom_html": {
    "original_source": "...",
    "source_hash": "sha256",
    "prepared_source": "...",
    "asset_manifest": [{"reference": "images/logo.png", "asset_id": "...", "kind": "image"}],
    "diagnostics": [],
    "css_mode": "simple-inline-preserve-media"
  },
  "attachments": []
}
```

## Build plan

1. Add bounded custom HTML validation, transformation, text extraction, ZIP
   import, asset resolution, export, and diagnostics using existing services.
2. Extend Message Studio persistence/API with paste/import/export operations,
   preserving optimistic revision and immutable published behavior.
3. Route custom rendering through `SafeTemplateRenderer`, `MessageComposer`,
   and 033 proof preparation; add MIME and security regression tests.
4. Add custom mode/source editor/import diagnostics/preview/revision controls
   to the existing Message Studio route and refresh packaged assets.
5. Run focused, full, browser, packaged, security, performance, and independent
   review checks; update capability and repository truth evidence.

## Consequences

Custom HTML designs retain useful email-specific structure without being forced
through the structured block AST. Browser preview is intentionally not a claim
of identical rendering in Gmail, Outlook, Apple Mail, or other clients.
External resources remain unresolved by policy, so an operator must supply
local assets or accept the diagnostic before any preparation can proceed.

The bounded parser is deliberately conservative and does not promise complete
HTML/CSS compatibility or automatic VML correctness. Full visual editing,
client-specific linting, personalized attachments, native Windows packaging,
and installation/update continuity remain roadmap candidates rather than this
milestone's implementation.

## Security and integrity remediation

The final 034 implementation rejects CSS escape sequences, comments, resource
functions, resource at-rules, and unsupported URL schemes before preparation.
Preview output carries a restrictive `Content-Security-Policy` in addition to
the opaque sandbox frame. Recipient-specific HTML is validated again after
template rendering, so unsafe URL schemes fail closed rather than being merely
HTML-escaped.

Filename references are resolved and pinned to canonical asset IDs and SHA-256
digests before publication. Published revisions do not fall back to the newest
same-named asset. Import rollback archives only assets created by that import;
deduplicated pre-existing assets remain available.

## Rationale

Research and the pre-change capability inventory are recorded in the executor
result packet and in the milestone documentation. Sources include Campaign
Monitor's HTML-plus-ZIP import workflow, OWASP file-upload and XSS guidance,
Gammadyne's HTML editor capability inventory, Gmail CSS guidance, Python's
standard email/MIME documentation, and RFC 5322/RFC 2045.
