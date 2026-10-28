# Web operational acceptance 014

Task `MAILFORGE-WEB-OPERATIONAL-ACCEPTANCE-014` closes the verified local
browser gaps around inbound review, browser lifecycle, restart recovery, local
process ownership, accessibility, and the connected synthetic protocol path.

## Evidence

- `tests/test_web.py` covers the bounded EML upload, classification and exact
  correlation, review decision, eligibility change, process lock, and valid
  restart transition.
- `tests/e2e/test_browser_acceptance.py` covers contact import, campaign
  preparation and authorization, synthetic execution after browser closure,
  fresh-session reopening, EML inbound review, approval, and narrow layout.
- `tests/e2e/test_accessibility.py` runs `@axe-core/playwright` WCAG 2A/2AA
  checks on Dashboard, Contacts, Campaigns, Inbound, Providers, and Activity.
- `tests/test_operational_acceptance.py` connects canonical services to local
  TLS SMTP and IMAP fixtures, including DSN review/suppression, ambiguous
  post-DATA protection, no-duplicate retry behavior, and backup/restore.

The evidence is local and synthetic. It does not qualify a real provider,
prove inbox delivery, establish consent, or authorize live sending.

## Operator notes

Run `mailforge serve` against one SQLite database at a time. A second process
using that database exits with an actionable ownership error. A service restart
reconciles stale running work to paused through the canonical state machine;
the operator can then inspect and explicitly resume it.

Manual keyboard checks covered primary navigation, labeled forms, file inputs,
campaign controls, inbound review actions, focus visibility, and the browser
confirm dialog for cancellation. Automated axe checks are supplemental and do
not replace manual accessibility review.
