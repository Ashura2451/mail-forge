# Spec 0005: local web application

**Status:** Assumed for implementation; ratification remains an architecture
follow-up.
**Date:** 2026-10-28

## Summary

Add the first functional local browser application over Mailforge's existing
canonical Python services and SQLite authority. The slice includes a same-origin
FastAPI API, a React/TypeScript/Vite frontend, safe loopback session handling,
supervised local background execution, compiled-asset serving, and browser
acceptance for contacts, campaigns, providers, inbound review, and activity.

## Requirements

- Keep `src/mailforge` as the sole backend service and policy authority.
- Serve compiled frontend assets from the Python runtime in normal operation.
- Bind to loopback, reject untrusted Host/origin values, and protect state
  changes with a local session and CSRF token.
- Expose validated, versioned, paginated API contracts without arbitrary SQL,
  filesystem reads, secret output, or real-send commands.
- Make browser import, campaign preparation/preview, simulation authorization,
  pause/resume/cancel, inbound review, provider readiness, and activity real.
- Keep execution in a supervised Python worker so browser lifecycle does not
  own or duplicate campaign work.
- Add one coherent local acceptance flow and retain explicit unverified gaps.

## Decision

Use FastAPI and Uvicorn for the local HTTP boundary, React with TypeScript and
Vite for the browser application, SQLite through `CampaignStore` for durable
state, and a bounded in-process worker supervised by the local runtime. Use a
same-origin API and static asset serving; do not add a second backend, hosted
identity system, distributed queue, or production send surface.

The implementation is recorded as an assumption because the planner supplied
the preferred stack in the execution packet but no separately ratified web
architecture spec exists. `/architect` should ratify or revise this decision
before the application grows beyond the first local slice.

## Build plan

Use a tracer-bullet order: runtime health and session boundary first, then one
contact-import path through API and UI, then campaign preparation/monitoring,
then provider/inbound/activity surfaces, then compiled serving, worker restart
behavior, security tests, and browser acceptance.

## Consequences

The local runtime remains a single-machine process and does not promise work
while stopped. SQLite contention, process ownership, and restart recovery stay
visible. The frontend is a presentation layer and cannot become a second
eligibility, suppression, scheduling, or reconciliation authority.

## Follow-up

Ratify this assumed architecture decision before adding hosted access,
multi-user identity, remote workers, or production automation.
