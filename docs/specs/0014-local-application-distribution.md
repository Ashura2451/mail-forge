# Spec 0014: local application distribution

**Status:** Accepted for MAILFORGE-LOCAL-APPLICATION-024
**Date:** 2026-10-28

## Summary

Provide a controlled macOS arm64 local application around the existing
Mailforge Python/FastAPI/React/SQLite product. The application owns startup,
health, singleton process ownership, first-run preferences, maintenance, and
full local backup/restore boundaries while the existing domain services remain
authoritative.

## Requirements

- A clean macOS arm64 bundle starts without Python, uv, npm, or Vite commands.
- The bundle contains the CPython 3.15.2 runtime, installed wheel dependencies,
  canonical Mailforge package, migrations, provider catalog, and compiled UI.
- Mutable data lives outside the replaceable bundle in a stable user data root.
- One runtime owns one data directory; duplicate launch attempts reuse the live
  runtime and unrelated port occupants are never terminated.
- First-run setup remains local simulation by default and does not contact a
  provider or infer recipient consent.
- Runtime status exposes version, schema, worker, data-directory identity,
  backup, and diagnostic state without credential values or full paths.
- Application backups include SQLite, settings, and app-owned local artifacts,
  use a manifest and SHA-256 integrity checks, and restore only into a new or
  explicitly confirmed destination.
- A database schema newer than the application is refused; compatible startup
  migrations receive a verified pre-migration SQLite snapshot.
- Loopback binding, session authorization, CSRF, CSP, and browser filesystem
  boundaries remain unchanged.

## Decision

Use a reproducible shell packaging process to assemble a native macOS `.app`
bundle from the repository's locked CPython 3.15.2 runtime, a wheel installed
from the locked project, and the already compiled Vite assets. Use a small
Python lifecycle service and CLI (`mailforge app`) for start, stop, restart,
status, open, init, backup, validation, and restore. Keep FastAPI/Uvicorn as
the server and `CampaignStore` as the SQLite authority. Do not add Electron,
Tauri, a Node runtime, a hosted service, or a persistent system daemon.

## Design

`DataDirectory` resolves platform-appropriate mutable storage and creates
private directories for configuration, backups, logs, runtime records, and
artifacts. The server reports a non-reversible directory identity instead of
exposing its path. `LocalApplication` probes an authenticated Mailforge
health marker before spawning; a runtime record contains the PID, port, and
runtime ID and is discarded when stale. The existing database process lock
remains the final ownership boundary.

The browser adds a resumable first-run setup card and a Runtime & Maintenance
surface. Stop/restart remains a launcher action so a browser request cannot
terminate the process serving that request. Restore is staged to a new
destination and never destructively replaces the active directory through the
browser.

## Build plan

Tracer-bullet order: establish data-directory and lifecycle primitives, expose
health/setup/maintenance API, connect the browser surfaces, add the macOS
bundle builder, then exercise clean bundle launch, duplicate launch, port
conflict, backup/restore, and regression validation.

## Consequences

The first qualified distribution is macOS arm64 only and is intentionally not
signed or notarized without an authorized Apple identity. The bundle is
self-contained for its build architecture, while the application data root is
portable and platform-neutral enough for later Windows/Linux packaging. The
existing CLI remains available for technical operators and automation.

## Follow-up

Add signed/notarized release handling only with an authorized distribution
identity. Qualify Intel macOS, Windows, and Linux bundles separately. A native
menu-bar control surface is not part of this milestone; launcher CLI controls
and the browser maintenance surface are the supported controls.

## Rationale

The packaging choice and option comparison are recorded in
`docs/local-application.md`.
