# 0025 · Frontend experience overhaul

**Status**: In Progress
**Outcome**: MAILFORGE-043
**Date**: 2026-11-27

## Summary

Reframe the local operator console around user goals instead of backend service
boundaries. The redesign keeps the existing React/Vite frontend and FastAPI
contracts, but gives first-time users a clear home, a direct path to send,
plain-language account setup, and progressive disclosure for advanced tools.

## Baseline findings

- The rendered shell exposes 17 destinations before a user has chosen a task.
- The home screen leads with SQLite, schema, and “accountable” campaign
  language instead of the five common first-use jobs.
- Provider setup exposes implementation concepts such as account references,
  credential references, capability profiles, and protocol names before asking
  for sender, server, username, or password.
- Related-work chips compete with the page's main action and repeat navigation.
- Most screens are composed in one 1,325-line `App.tsx`, making shared language
  and interaction states difficult to keep consistent.

## Decision

Use a goal-oriented workspace with six primary destinations:

1. Home — choose a task and see only the local status that affects the next
   decision.
2. Send — the campaign workspace, with the existing preparation,
   authorization, preview, pacing, and execution authorities unchanged.
3. Contacts — recipient records, lists, and audience readiness.
4. Messages — reusable message drafts, templates, and personalization.
5. List cleaning — standalone SORTER/MSORTER processing without SMTP setup.
6. Activity — current and historical outcomes.

Place provider accounts, Data Studio, automations, reports, integrations,
delivery evidence, standards, and maintenance under an explicit “More tools”
area. Preserve every existing route as a deep link and retain the current
service components while the shell is migrated.

The first-use home uses task cards and state-aware guidance rather than a
mandatory tutorial. Account setup uses visible labels for sender email, SMTP
server, SMTP username, SMTP password, port, and encryption. The technical
credential reference remains an advanced storage detail and secrets are never
read back.

## Research applied

- Nielsen Norman Group, “10 Usability Heuristics”: recognition rather than
  recall, visibility of system status, user control, and error recovery.
- GOV.UK Design System text-input and error-message guidance: labels above
  controls, persistent help, and field-level actionable errors.
- WCAG 2.2: keyboard access, visible focus, meaningful headings, status
  communication, and responsive reflow.
- Existing repository contract in `DESIGN.md`, `web/AGENTS.md`, and the
  current rendered console at `http://127.0.0.1:8765/`.

## Consequences and guardrails

- No new client-side business logic, persistence authority, scheduler, or SMTP
  path is introduced.
- Existing routes and API calls remain valid while their discoverability is
  improved.
- Technical tools remain available to experienced operators, but are not the
  default onboarding path.
- “Provider accepted” and “inbox delivery” remain distinct states.
- Browser qualification must cover the new home, provider labels, deep links,
  keyboard focus, and narrow-screen layout before this outcome is accepted.

## Build plan

1. Replace the shell and home information architecture while preserving route
   resolution and current view components.
2. Rewrite account setup labels and progressive disclosure without changing the
   provider API payload or credential boundary.
3. Normalize shared visual states and responsive behavior.
4. Build, lint, and run focused rendered browser/accessibility checks; record
   remaining gaps honestly.
