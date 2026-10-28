# Review, Mailforge inbound operations 020, 2026-10-28

**Task:** `MAILFORGE-INBOUND-OPERATIONS-020`
**Branch:** `feat/mailforge-inbound-operations-020`
**Status:** Local implementation verified; external-provider qualification and
independent review remain open.
**Commit:** `79c4048448434d40070b6ea53a28d532bc135e2b`
**Pull request:** [#3](https://github.com/Ashura2451/mail-forge/pull/3), open and
mergeable against `main`

## Result

The inbound foundation is extended into a durable local workbench while
preserving the existing service authorities. SQLite schema 12 stores bounded
attachment metadata, safe extracted values, versioned rule runs, draft
provenance, retention state, and explicit mailbox-action requests.

`InboundService` remains the acquisition/parser/classifier/correlation/review
authority. `InboundOperations` owns parameterized workbench queries, typed
rules, extraction/export, explicit contact/list additions, and Message Studio
draft preparation. EML, MBOX, synthetic, IMAP, and read-only POP3S UIDL/RETR
adapters share the same parser and durable acquisition path.

## Evidence

- `.venv/bin/pytest -q` — 193 passed after the POP3 and workbench additions,
  one existing Starlette/httpx deprecation warning.
- Focused web, browser, and axe suite — 14 passed.
- `npm run lint` — passed with the existing React set-state-in-effect warnings.
- `npm run build` — passed; packaged assets refreshed.
- `uv run ruff check src tests` and `uv run ruff format --check src tests` —
  passed.
- `git diff --check` — passed.
- Synthetic benchmark: 500 messages processed in 6.257 seconds (79.9
  messages/second), metadata search in 2.85 ms, 2.88 MiB SQLite database.
  This is a local sample, not a production capacity claim.

## Acceptance matrix

| Criterion | Status | Evidence / limitation |
| --- | --- | --- |
| A. Previous milestone integration | PASS | PR #1 and PR #2 merged; 020 starts at verified `origin/main` `d25f467` |
| B. Canonical authorities reused | PASS | Existing `InboundService`, `CampaignStore`, Message Studio, provider, and Automation services |
| C. EML/MBOX import | PASS | Existing CLI plus local web EML/MBOX upload routes and tests |
| D. IMAP acquisition/cursors | PASS | Existing UID/UIDVALIDITY adapter, cursor/recovery tests, explicit authorized sync route |
| E. Identity/deduplication | PASS | Source identity/content hash behavior and replay tests |
| F. Durable inspectable records | PASS | Schema 12 detail API, SQLite backup/restore, rule/action history |
| G. Browser workbench | BROWSER VERIFIED | Search/filter, detail, import, rule controls, extraction, draft controls in Inbound view |
| H. Search/filter/pagination | PASS | Parameterized sender/recipient/subject/body/classification/source/state query with limit/offset |
| I. MIME/HTML/attachment safety | PASS | Bounded parser, no execution/remote fetching, basename-safe metadata, sandboxed browser preview pattern |
| J. DSN/complaint classification | PASS | Existing parser/classifier tests retained |
| K. Trust/correlation | PASS | Existing exact/probable/ambiguous/unmatched and trust separation retained |
| L. Durable rules | PASS | Typed JSON create/validate/activate/pause, persisted rule runs |
| M. Deterministic matching | PASS | Allowlisted fields/operators, stable priority/idempotency, no code/SQL execution |
| N. Extraction/export | PASS | Header/address/date extraction and JSON/formula-safe CSV export |
| O. Reply preparation | PASS | Message Studio document/revision with reply subject and provenance |
| P. Forward preparation | PASS | Message Studio forward draft with sanitized quoted source preview |
| Q. Automatic-response safeguards | LOCALLY ENFORCED | Auto-response/DSN/complaint matches are loop-prevented; no live automatic send |
| R. Workflow integration | PASS | Rule events and existing durable operational events use Automation authority |
| S. Suppression/eligibility | PASS | Existing trusted review pipeline and eligibility tests pass |
| T. Duplicate side effects | PASS | Source and rule replay idempotency tests |
| U. Browser closure | PASS | Existing browser closure/runtime acceptance retained |
| V. Restart/recovery | PASS | Existing cursor/replay/restart tests retained |
| W. Mailbox mutation authorization | LOCALLY SAFE | Requests require authorization/idempotency and are recorded; no remote mutation adapter is executed |
| X. No unauthorized communications | PASS | No live provider or outgoing message used; explicit grants remain required |
| Y. Accessibility/responsive | PASS | Existing axe, keyboard, desktop, and narrow browser suite passed |
| Z. Performance evidence | PASS (LOCAL SAMPLE) | 500-message synthetic benchmark recorded above |
| AA. Existing capabilities | PASS | Full regression suite passed |
| AB. Engineering validation | PASS | Python, Ruff, frontend lint/build, diff checks passed |
| AC. Complete 52-step synthetic acceptance | PARTIAL / NOT CLAIMED | Durable local paths are covered, but no claim is made that every packet step was independently exercised in one run |
| AD. Independent review | NOT AVAILABLE | No independent reviewer was available; none is claimed |
| AE. Documentation synchronization | PASS | Spec 0010, guide, capability atlas, changelog, and local `.aios` observations updated |
| AF. Git publication | PASS | Branch pushed; remote head matches `79c4048448434d40070b6ea53a28d532bc135e2b`; PR #3 is open |
| AG. Main integration | NOT YET INTEGRATED | `origin/main` remains `d25f46795f98d18b3944863f03500549850ba509`; PR #3 is mergeable but not merged |

## Security and operational limitations

No malware scanner is integrated or claimed. Rich arbitrary document
extraction, remote mailbox move/copy/mark execution, live automatic replies,
provider-specific trust, consent qualification, multi-user authorization, and
hosted deployment remain outside this local milestone. POP3 has no folder
semantics and the adapter requires stable UIDL; a missing cursor is treated as
invalid rather than silently replaying an unbounded mailbox.

No real provider, credential, mailbox, recipient data, or external message
submission was accessed. This document is not provider qualification, inbox
delivery evidence, consent evidence, or hosted-service approval.
