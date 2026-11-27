# Mailforge

Mailforge is a local-first email utility with three independently useful
workflows: sort email lists, compare lists, and prepare controlled sends. The
packaged sorting service and the canonical campaign/outbound services remain
authoritative; legacy SORTER/MSORTER and MAILER data contracts are preserved
only where they are needed for compatibility. The browser starts with these
three workflows and keeps account setup, saved lists, history, and recovery as
optional support.

## Quick start

The supported development target is CPython 3.15.2. The normal macOS shell
commands `python` and `python3` are configured to the user-local uv-managed
interpreter; existing Homebrew and framework interpreters remain installed.
With `uv` installed:

```bash
uv sync --locked --extra dev
uv run --locked pytest
uv run ruff check src tests
uv run ruff format --check src tests
```

The React/Vite console uses Node 24 or newer. From `web/`, run `npm run build`
and `npm run lint` with that runtime; the build writes the served assets to
`src/mailforge/web_assets/`.

Without `uv`, use an isolated virtual environment and install the project with
`python -m pip install -e '.[dev]'`. Do not install into the system Python.

For local MAILER configuration, copy `MAILER/.env.example` to `MAILER/.env`
and fill in values only on the local machine. Never commit the copied file.
Run `bash scripts/check-secrets.sh` before creating a release commit; it is an
offline, redacted safeguard and does not validate credential activity.

The canonical package CLI is safe to use against synthetic data:

```bash
uv run mailforge --help
uv run mailforge ingest path/to/synthetic-input.txt --output summary.json
uv run mailforge sort first.csv second.jsonl --results-dir results --profile full --progress-every 1000
uv run mailforge sort input.csv --dry-run --profile report --results-dir preview

# Former SORTER workflow, now owned by the canonical CLI
uv run mailforge transform format emails.txt formatted_emails.txt
uv run mailforge transform dedupe formatted_emails.txt
uv run mailforge transform list formatted_emails.txt sorted_mails.txt
uv run mailforge transform count sorted_mails.txt
```

Real SMTP delivery remains phase-aware and authorization-bound. Local
simulation and controlled loopback qualification are available; inbox
delivery is never inferred from SMTP acceptance. For local MAILER compatibility
use the report-only `campaign translate-legacy`, explicit `campaign
import-legacy`, and `campaign rehearse-legacy-state` commands. See
[`docs/mailer-retirement-010.md`](docs/mailer-retirement-010.md) for the
mapping and rollback boundary.

Recipient policy is shared by import, campaign preview/preparation, simulated
dispatch, resume, retry, and inbound reconciliation. Use `campaign eligibility`
to inspect current decisions, `campaign record-evidence` and `campaign
preference` to record supplied metadata, and `campaign suppressions` to inspect
known exclusions. An address-only import remains usable and is reported as
`authorization_unknown`; no consent is inferred from the address.

The policy implementation and its explicit non-claims are recorded in
[`docs/policy-authority-011.md`](docs/policy-authority-011.md).

Provider profiles and local-only SMTP/IMAP diagnostics are available through
the canonical `provider` commands. Profiles store symbolic credential
references, never plaintext credentials; local diagnostics reject non-loopback
endpoints and no provider send command exists. See
[`docs/provider-interoperability-012.md`](docs/provider-interoperability-012.md).

The product claim ledger and dependency-aware roadmap are in
[`docs/product/capability-atlas.md`](docs/product/capability-atlas.md). The
architecture consolidation audit is in
[`docs/architecture-audit.md`](docs/architecture-audit.md).
