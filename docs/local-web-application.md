# Local web application

Mailforge's first browser console is a single-machine application. It uses the
existing SQLite campaign authority and canonical Python services; the browser
does not access SQLite, credentials, SMTP, IMAP, or arbitrary local files.

## Launch

For normal use, build the frontend once and run the packaged local server:

```sh
cd /path/to/Mailforge
cd web && npm install && npm run build && cd ..
uv run mailforge serve --database state/mailforge.sqlite3
```

Open `http://127.0.0.1:8765/`. `mailforge serve` rejects non-loopback bind
hosts and serves `src/mailforge/web_assets/` when no explicit frontend path is
provided. The Vite development server is only for frontend iteration:

```sh
cd web && npm run dev
```

The CLI remains supported for advanced workflows. The browser server does not
run while the local process is stopped; durable SQLite state is retained for a
later restart.

## API surface

The same-origin API is versioned under `/api/v1` and includes status/dashboard,
lists/imports/contacts, campaign creation/preview/eligibility/preparation,
simulation authorization and supervised execution controls, progress/outcomes,
bounded local EML acquisition, inbound classification/correlation/trust and
review decisions, provider profiles/credential readiness/diagnostics, and
paginated activity events. Browser EML acquisition uses a temporary private
file and the existing `EmlFileSource`/`InboundService`; it never accepts a
filesystem path.

The session bootstrap endpoint sets an HttpOnly SameSite session cookie and a
CSRF cookie/token. All other API reads require that session; state changes also
require `X-Mailforge-CSRF` and a loopback same-origin `Origin`. Host parsing
supports IPv4 and bracketed IPv6 loopback values. Responses set a restrictive
content-security policy and do not expose the database path or credential
values.

## Safety boundary

Campaign actions use the existing preparation, eligibility, suppression,
authorization, and durable event services. Browser execution is synthetic and
never opens a real provider connection. Provider profiles show secret-free
configuration and local diagnostic/readiness results; local protocol success is
not external provider verification. Unknown consent evidence remains visible
and is never invented by an import.

The in-process worker is bounded and supervised by the Python runtime. A
refresh or browser close does not duplicate or cancel a worker while the local
server remains alive. `mailforge serve` holds an advisory database lock so a
second local process cannot dispatch the same SQLite authority independently.
On startup, stale running/pausing campaigns are reconciled through the existing
lease recovery path, transitioned through the valid `pausing` state, and made
available for explicit local resume. Real sending, hosted identity, LAN access,
remote workers, and cloud infrastructure require separate architecture and
release decisions.

## Verification

```sh
uv run --extra dev pytest -q
cd web && npm run lint && npm run build
```

The repository includes API/security tests in `tests/test_web.py` and a real
Playwright loopback acceptance in `tests/e2e/test_browser_acceptance.py` that
exercises contact import, campaign execution, browser closure, fresh-session
reopening, inbound review, desktop, and narrow viewports. The connected
SMTP/IMAP lifecycle is in `tests/test_operational_acceptance.py`; the maintained
`@axe-core/playwright` dependency drives the route audit in
`tests/e2e/test_accessibility.py`. Chromium can be selected with
`PLAYWRIGHT_CHROMIUM_EXECUTABLE` when it is not installed in the default cache.
