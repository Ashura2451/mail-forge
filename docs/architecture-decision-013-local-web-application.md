# Decision 0007: local web application boundary

**Status:** Implemented as the first local browser slice; hosted identity,
remote workers, and live delivery remain explicitly out of scope.
**Date:** 2026-10-28

Mailforge's browser operator surface is a same-origin FastAPI/Uvicorn boundary
over the existing `CampaignStore` and application services. React/TypeScript
compiled by Vite is served by the Python process. The runtime binds only to
loopback, validates Host and loopback Origin values, establishes an in-memory
local session, and requires a CSRF header for state changes.

Campaign preparation, eligibility, suppression, inbound review, provider
diagnostics, and the operational ledger remain canonical backend behavior. The
browser does not access SQLite, credentials, SMTP, IMAP, or arbitrary files.
Synthetic campaign execution is supervised by a bounded in-process worker;
durable campaign state survives browser closure, while process restart remains
an explicit recovery/operations concern.

The local application is deliberately not a hosted web service. It has no
multi-user identity, tenancy, remote worker, cloud queue, real-send endpoint,
or external provider contact through the browser. Those require separate
architecture, security, and release decisions.
