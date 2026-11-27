# Development

Mailforge is a local-first Python modular monolith with a React/Vite console.
Maintained source is under `src/mailforge/` and `web/src/`; mutable
application data belongs in the platform data directory returned by
`DataDirectory`, not in this checkout.

## Setup

Use CPython 3.15.2 from `.python-version` and the locked environment:

```bash
uv sync --locked --extra dev
uv run --locked pytest
uv run --locked --extra dev ruff check src tests
uv run --locked --extra dev ruff format --check src tests
```

The console requires Node 24 or newer:

```bash
cd web
npm ci
npm run build
npm run lint
```

The build writes served frontend assets to `src/mailforge/web_assets/`.

## Runtime data

Use `mailforge app init --data-dir <directory>` for an explicit local data
directory. The browser, backups, logs, runtime lock, SQLite database, and list
processing artifacts are then kept below that directory. Sorter CLI output also
defaults to the platform Mailforge data directory; use explicit options only
when a separate output location is intentional.

Credentials are symbolic runtime references and must never be written to
fixtures, logs, SQLite records, or ordinary diagnostics. `check-secrets.sh` is
an offline publication guard.

## Compatibility

The retained `campaign translate-legacy`, `campaign import-legacy`, and
`campaign rehearse-legacy-state` commands accept explicitly supplied legacy
files for migration rehearsal. They do not read `.env` files, auto-import
recipient data, or run the retired MAILER/SORTER/MSORTER programs. Migration
guidance is in [`mailer-migration-contract.md`](mailer-migration-contract.md).

The provider boundary is SMTP-only. Loopback qualification uses isolated local
fixtures; no external SMTP operation is part of ordinary development.
