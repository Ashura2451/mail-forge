# Mailforge

Mailforge is a local-first email utility with three independently useful
workflows: sort email lists, compare lists, and prepare controlled sends. The
packaged sorting service and the canonical campaign/outbound services remain
authoritative; narrow legacy input translation is preserved only where it is
needed for compatibility. The browser starts with these
three workflows and keeps account setup, saved lists, history, and recovery as
optional support.

## Quick start

The supported development target is CPython 3.15.2. With `uv` installed:

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

Run `bash scripts/check-secrets.sh` before creating a release commit; it is an
offline, redacted safeguard and does not validate credential activity.

The canonical package CLI is safe to use against synthetic data:

```bash
uv run mailforge --help
uv run mailforge ingest path/to/synthetic-input.txt --output summary.json
uv run mailforge sort first.csv second.jsonl --profile full --progress-every 1000
uv run mailforge sort input.csv --dry-run --profile report
uv run mailforge transform format emails.txt formatted_emails.txt
uv run mailforge transform dedupe formatted_emails.txt
uv run mailforge transform list formatted_emails.txt sorted_mails.txt
uv run mailforge transform count sorted_mails.txt
```

Sort outputs, checkpoints, and disposable-domain refresh data default to the
user-owned Mailforge data directory, not this checkout. Real SMTP delivery remains phase-aware and authorization-bound. Local
simulation and controlled loopback qualification are available; inbox
delivery is never inferred from SMTP acceptance. Explicit legacy translation
and rehearsal commands remain available for approved migration inputs; they do
not load `.env` files or start the retired MAILER runner. See
[`docs/mailer-migration-contract.md`](docs/mailer-migration-contract.md).

Recipient policy is shared by import, campaign preview/preparation, simulated
dispatch, resume, and retry. Use `campaign eligibility`
to inspect current decisions, `campaign record-evidence` and `campaign
preference` to record supplied metadata, and `campaign suppressions` to inspect
known exclusions. An address-only import remains usable and is reported as
`authorization_unknown`; no consent is inferred from the address.

The policy implementation and its explicit non-claims are recorded in
[`docs/policy-authority-011.md`](docs/policy-authority-011.md).

Provider profiles and local-only SMTP diagnostics are available through the
canonical `provider` commands. Profiles store symbolic credential references,
never plaintext credentials; IMAP/POP3 and mailbox operations are retired, and
local diagnostics reject non-loopback endpoints. Controlled loopback execution
is available only through the prepared campaign safety boundary. See
[`docs/provider-interoperability-012.md`](docs/provider-interoperability-012.md).

The current architecture and retained compatibility boundaries are in
[`docs/architecture-audit.md`](docs/architecture-audit.md). The final owner
report is [`docs/MAILFORGE-044-COMPLETION.md`](docs/MAILFORGE-044-COMPLETION.md).
