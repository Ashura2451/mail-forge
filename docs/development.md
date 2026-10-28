# Development and troubleshooting

## Supported setup

Use CPython 3.15.2. The exact version is recorded in `.python-version` and
`pyproject.toml`; `uv.lock` is the reproducible dependency resolution.
`uv sync --locked --extra dev` must be used in CI and is the preferred local
setup.

The functional local browser console is built with React/TypeScript/Vite and
served by the canonical Python package. Build and launch it with:

```bash
cd web && npm install && npm run build && cd ..
uv run mailforge serve --database state/mailforge.sqlite3
```

See [`docs/local-web-application.md`](local-web-application.md) for the API,
session boundary, worker lifecycle, and browser acceptance contract. Normal
runtime does not require a Vite server or a hosted account.

```bash
uv sync --locked --extra dev
uv run --locked pytest
uv run ruff check src tests
uv run ruff format --check src tests
```

An isolated virtual environment using `python -m venv .venv` and
`python -m pip install -e '.[dev]'` is the fallback when `uv` is unavailable.
Do not install Mailforge dependencies into the system interpreter.

### Local MAILER credentials

Copy `MAILER/.env.example` to `MAILER/.env` and replace its placeholders only
on the local machine. `.env` files are ignored by Git, while examples remain
trackable. The existing `MAILER/.env` is intentionally preserved locally but
is no longer tracked by the release branch. Do not print or paste its values
into tickets, logs, CI output, or review packets.

Run the offline guard before creating a release commit:

```bash
bash scripts/check-secrets.sh
```

This guard checks tracked environment filenames and high-confidence private-key
or token patterns without printing matched values. It is a safeguard, not proof
that a credential is valid or absent from old history. If a credential may have
been committed, identify its service, revoke or rotate it with the provider,
then verify the replacement locally before authorizing publication.

The host-level Python upgrade is separate from the project environment. In a
fresh login shell, verify `python --version` and `python3 --version` both report
3.15.2, while `uv run --locked python --version` reports the project 3.15.2
environment. The shell change is limited to backed-up zsh PATH entries; restore
`~/.zshrc.mailforge-python-backup-20261027` and
`~/.zprofile.mailforge-python-backup-20261027` to roll it back.

## Sorting workflows

`mailforge sort` accepts one or more TXT, CSV, JSON, or JSONL sources and
deduplicates them in deterministic argument order. Full runs preserve the
legacy provider files and add `master.csv`, `processing.jsonl`, optional
`quarantine.jsonl`, and `summary.json`. `--dry-run --profile report` writes
explainable structured results without legacy output files. Profiles are
`full`, `validation`, `dedupe`, `classify`, `legacy-export`, and `report`.
Structured results contain source provenance, source record position, validation,
classification, deduplication disposition, reason codes, DNS evidence status,
and a rule version; ordinary logs do not contain extracted addresses.

## Safe validation

Tests use synthetic recipients and fake SMTP clients; no test is allowed to
connect to public DNS or SMTP. The provider interoperability tests use only
127.0.0.1/localhost TLS fixtures with generated certificates and synthetic
credentials. The retired MAILER runner has no supported entrypoint.
Use the canonical report/import commands with synthetic paths:

```bash
mailforge campaign translate-legacy --config path/to/synthetic-config.yaml
mailforge campaign import-legacy --database /tmp/mailforge.sqlite3 \
  --config path/to/synthetic-config.yaml --campaign invoice \
  --sender-email sender@example.com
```

The SMTP gate and environment variable are retained as an adapter-level
authorization contract. The compatibility translator intentionally never
loads `.env`; SMTP credentials remain caller-owned inputs and are never copied
to SQLite or printed in reports.

### Provider profiles and diagnostics

Provider profiles are registered with symbolic references such as
`env:MAILFORGE_SMTP_PASSWORD`; the CLI deliberately has no password argument.
Use `provider validate` and `provider credentials` for non-network checks, and
`provider local-diagnostic` only with a loopback fixture. A local protocol pass
does not verify a commercial provider or inbox delivery. See
`docs/provider-interoperability-012.md` for the adapter and recovery contract.

## Troubleshooting

- `uv sync --locked` fails: verify the interpreter is CPython 3.15.2 and rerun
  with the repository's `uv.lock`; do not bypass the lock with ad hoc global
  installs.
- A test attempts network access: stop and fix the test boundary. Use a fake
  DNS/SMTP adapter or a loopback fixture; never add provider credentials.
- The MAILER runner and SORTER/MSORTER Python wrappers have been retired. Use
  the canonical `mailforge` commands below.
- Existing `MAILER/.env`, recipient lists, logs, checkpoints, and delivery
  state are operational artifacts. Do not copy them into fixtures or commit
  their contents.

## Migration boundary

The new package is the authoritative implementation for sorting and former
legacy transformations. The SORTER/MSORTER directories now preserve only
operational data, configuration, checkpoints, and reports; their Python
entrypoints are retired. Use `mailforge sort`, `mailforge transform`, and
`mailforge extract-jsonl` as the migration targets. Operational data remains
owned by its current files until a separately approved migration defines
identity, retention, rollback, and recovery evidence.

### Synthetic campaign core

The durable campaign slice is exercised only with synthetic data and a fake
transport. Use `mailforge campaign demo --database /tmp/mailforge-demo.sqlite3`
to run import, suppression, preparation, preview, pause/resume, and report
semantics without SMTP. `mailforge campaign simulate` requires explicit
simulation authorization and never reads `MAILER/.env`.

### Recipient policy authority

The campaign database is also the authority for recipient restrictions,
purpose-specific preferences, authorization evidence, operator declarations,
and lifecycle plans. An address-only import does not create consent evidence;
its evidence status is `unknown`, while validation, deduplication, preview,
preparation, and synthetic simulation remain available.

Useful inspection and recording commands are:

```bash
mailforge campaign eligibility --database /tmp/mailforge.sqlite3 CAMPAIGN_ID
mailforge campaign suppressions --database /tmp/mailforge.sqlite3
mailforge campaign record-evidence --database /tmp/mailforge.sqlite3 \
  --email alice@example.com --purpose newsletter --source signup --evidence-ref SIGNUP_ID
mailforge campaign preference --database /tmp/mailforge.sqlite3 \
  --email alice@example.com --purpose newsletter --state disabled
mailforge campaign declarations --database /tmp/mailforge.sqlite3 --campaign-id CAMPAIGN_ID
mailforge campaign export-contacts --database /tmp/mailforge.sqlite3
```

Known unsubscribe, complaint, hard-bounce, manual-hold, and other active
suppression records are enforced again immediately before dispatch. Inbound
messages remain evidence: only the existing trusted synthetic exact-correlation
path can apply a consequential suppression automatically.

The bounded legacy inventory and additive mapping are recorded in
`docs/mailer-capability-inventory.md` and
`docs/mailer-migration-contract.md`. Real MAILER files remain compatibility
inputs until identity, consent, retention, rollback, and recovery are approved.
The repository-wide ownership map and capability roadmap are recorded in
`docs/product/capability-atlas.md`; `docs/legacy-retirement-009.md` records
the source-retirement policy and the explicit MAILER blockers.

### Synthetic outbound engine

The outbound preparation path is exercised with synthetic contacts and a
parser-backed fake transport:

```bash
mailforge campaign outbound-demo --database /tmp/mailforge-outbound.sqlite3
```

The command persists prepared MIME bytes and policy/schedule state locally,
prints a preview, and simulates outcomes. It never reads `MAILER/.env`, opens
SMTP, or claims inbox delivery. One-click unsubscribe headers are gated by
HTTPS and an explicit DKIM assertion, while the required endpoint and signing
infrastructure remain outside this repository.

### Synthetic inbound operations

Inbound processing is local-first and uses no network adapter:

```bash
mailforge inbound synthetic-demo --database /tmp/mailforge-inbound.sqlite3
mailforge inbound inspect --database /tmp/mailforge-inbound.sqlite3
mailforge inbound review --database /tmp/mailforge-inbound.sqlite3
mailforge inbound stats --database /tmp/mailforge-inbound.sqlite3
```

Local `.eml` and MBOX imports are read-only. `--retain-raw` is required when
an EML import must remain replayable; otherwise the parser processes the bytes
and releases them after durable metadata/evidence is stored. Real IMAP is
represented only by the simulated endpoint and the disabled adapter. Inbound
classification is evidence, not authentication: untrusted or ambiguous
complaints/unsubscribe/DSN actions enter review, and no automatic reply is
sent.
