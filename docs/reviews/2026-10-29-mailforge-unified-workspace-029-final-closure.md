# Mailforge unified workspace 029 final closure

Date: 2026-10-29<br>
Branch: `feat/mailforge-unified-workspace-029`<br>
PR: [#17](https://github.com/Ashura2451/mail-forge/pull/17)<br>
Disposition: locally exercised; PR remains open and unmerged

## Scope

This closure review covers the existing 029 shell and workbench integration.
It does not authorize milestone 030, a new frontend architecture, external
provider access, public webhooks, or a merge decision.

## Acceptance matrix

| Criterion | Status | Method and observed result |
| --- | --- | --- |
| Preserve the existing branch, commits, and PR | PASS | `git status`, ancestry, and PR metadata inspected; commits `00c766e` and `e0ec2ef` remain in history; no merge performed. |
| Dashboard reports truthful runtime state | PASS | Current captures show Local mode, schema `v22`, durable-state labels, and both the intentional first-run/zero-count state and a populated state with one contact, one list, and a prepared campaign. |
| Major workbenches have current rendered evidence | PASS | 22 post-closure screenshots cover dashboard, populated dashboard, activity, list tools, contacts, segments, Data Studio, Data Connections, Functions, Message Studio, Campaigns, Providers, Inbound, Automations, Reports, Email Standards, Integration Hub, Maintenance, mobile Message Studio/Campaigns/navigation, and tablet Reports. Capture summary reports no console errors or failed requests. |
| Mobile navigation is usable and accessible | PASS | Playwright verifies the rail is offscreen at 390px, opens the labelled drawer, closes it with Escape, and restores focus to the menu button. Viewport-only evidence is retained. |
| Campaigns mobile is usable | PASS | The permanent rail is replaced by the overlay drawer; the redundant legacy top action strip is hidden at the narrow breakpoint while the visible creation workflow remains. |
| Integration Hub is operational beyond empty state | PASS | Browser operation issued/revoked a scoped client, rejected unauthorized/revoked events, accepted and deduplicated an authorized event, registered/enabled a loopback webhook, processed a contact-import delivery, and verified the received HMAC signature and durable event ID. |
| Data Connections and Data Studio are connected | PASS | Browser uploaded a disposable SQLite source, previewed it, created a snapshot, saved a recipe, and handed the snapshot to Data Studio using the current revision. |
| Email Standards controls have a durable workflow | PASS | Browser saved an identity, recorded domain diagnostics, validated a MIME message, saved an unsubscribe endpoint, and evaluated marketing readiness with the one-click boundary visible. |
| Maintenance recovery is safe and durable | PASS | Browser saved preferences, created a verified backup, and restored it into a staged destination using a basename-only selection. Integrity remained verified. |
| Packaged behavior works across restart | PASS | The arm64 bundle was launched, used for Message Studio create/preview/save/publish and Maintenance backup/restore, stopped, relaunched against the same data directory, and verified to retain the published revision and staged restore artifact. |
| Independent review by a different model/reviewer | NOT VERIFIED | No independent reviewer was available in this execution; this remains a PR-gate obligation. |
| External provider, public callback, signing, and production qualification | SKIPPED | Explicitly outside the approved local synthetic scope. |

## Validation commands

Successful commands executed during closure:

- `PYTHONPATH=src .venv/bin/pytest -q --ignore=tests/e2e` — 248 passed.
- `PYTHONPATH=src .venv/bin/pytest -q tests/e2e` — 6 passed (5 browser workflow tests plus the axe accessibility route sweep).
- `uv run ruff check src tests` — passed.
- TypeScript project build and Vite production build — passed.
- Oxlint — passed with the repository's existing React effect warnings.
- `scripts/check-secrets.sh` — passed.
- `git diff --check` — passed.
- `scripts/package-macos.sh` — arm64 bundle built.
- `shasum -c Resources/BUILD-METADATA.sha256` before launching the bundle — passed.

`uv run ruff format --check src tests` still reports 12 pre-existing files with
format drift outside this scoped closure; no mass-formatting change was made.

## Evidence and limits

The local-only screenshot index is `.orchestra/evidence/029/index.html` and
the current capture set is `.orchestra/evidence/029/current-after-closure/`.
The fixture receiver is loopback-only and enabled through an explicit test
environment flag. The package is unsigned/unnotarized. The PR is not merged,
and this document is evidence for Planner/Auditor review rather than a claim
of Business Owner acceptance.
