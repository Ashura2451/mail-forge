# Review, Mailforge sanitized initial publication 015, 2026-10-28

**Task:** `MAILFORGE-015-SANITIZED-INITIAL-PUBLICATION`
**Source snapshot:** product tree at `845ca6d`, with product implementation
unchanged from validated commit `9b3854e`
**Legacy archive:** `/Users/blac_goody/Desktop/Mailforge`
**Sanitized workspace:** `/Users/blac_goody/Desktop/Mailforge-sanitized`

## Publication boundary

This workspace was initialized independently with `git init -b main`. It has
its own `.git` directory, no alternates, no inherited refs, no copied tags, no
old branches, and no parent relationship to the legacy repository. The legacy
repository remains intact at its original path, including its commits, private
operational files, local databases, logs, credentials, `.aios`, `.orchestra`,
and unrelated dirty work.

The sanitized tree contains only the reviewed application allowlist:

- `src/mailforge/**` and packaged web assets;
- `tests/**` and verified synthetic fixtures;
- `web/**` source/configuration/public assets, excluding starter assets,
  dependencies, and build output;
- `pyproject.toml`, `uv.lock`, `.python-version`, `README.md`, `DESIGN.md`,
  `SYSTEM_CHANGELOG.md`, `scripts/check-secrets.sh`, and safe CI;
- approved `docs/**` specifications, architecture records, capability
  documentation, performance evidence, and review records;
- `MAILER/.env.example` only;
- `MSORTER/config/providers.json` and `MSORTER/config/disposable.json` only,
  because the provider fixture and runtime configuration boundary are required
  by compatibility behavior.

Excluded from the new tree are all historical Git metadata, `.aios`,
`.orchestra`, local executor context, credentials, private recipient lists,
bulk email exports, mailbox content, checkpoints, logs, runtime databases,
legacy Python wrappers, editor state, caches, and machine-local files.

## Security and data scan

- `bash scripts/check-secrets.sh`: passed.
- No file over 10 MiB is reachable from the staged source tree.
- No `.env` file, private key, runtime database, mailbox dump, bulk email
  export, result CSV, checkpoint, or log is present; `.env.example` contains
  placeholders only.
- Email-like values in the tree are limited to synthetic fixtures, example
  addresses, protocol tests, and documentation examples. No personal recipient
  dataset was copied.
- No credential values were printed, sent to external scanning services, or
  used for validation.

## Source completeness

The clean workspace includes the canonical Python package, React/Vite source,
packaged assets, schema-7 persistence implementation, synthetic provider
fixtures, inbound review, suppression, segmentation, campaign, backup/restore,
and browser acceptance tests. The retained provider/disposable configuration is
the only legacy-named runtime resource needed by the test and compatibility
boundary; private legacy data remains an external user-owned input.

## Independent validation

All commands below ran from this workspace, using its own environment:

- `uv sync --locked --extra dev --no-editable`: passed on CPython 3.15.2.
- `.venv/bin/pytest -q`: **167 passed**, one existing Starlette/httpx
  deprecation warning.
- `.venv/bin/pytest -q tests/e2e`: **3 passed**.
- `.venv/bin/ruff check src tests` and `ruff format --check`: passed.
- `uv build`: passed; the wheel contains the complete `mailforge` package and
  packaged browser assets.
- `.venv/bin/mailforge --help` and `python -m mailforge --help`: passed.
- `cd web && npm ci`: passed with the existing optional `fsevents` install
  warning.
- `cd web && npm run lint`: passed with the existing React
  `set-state-in-effect` warning.
- `cd web && npm run build`: passed.
- `git diff --check`: passed.

The first isolated `uv sync --locked --extra dev` attempt used editable mode;
Python 3.15 skipped the generated hidden editable `.pth` path. The documented
`--no-editable` sync was then run successfully, and all Python/CLI checks passed
from the resulting installed artifact. This is an environment/tooling note,
not a product-source failure.

## Publication state

The initial commit is intentionally the only commit in this repository and has
no parent from the legacy archive. The remote is added only after the staged
tree and root-object audit pass. Publication uses `origin/main` only, without
force, mirror, tags, or legacy branches. After publication, `origin/main` must
resolve to this repository's root commit and visibility must remain private.

## Credential disposition

The original archive retains historical credential-like material privately.
Its usage and validity were not tested. Owner-led rotation or revocation is
recommended as a separate operational security action. Those objects are not
reachable from this sanitized repository.

## Workspace transition

The original directory remains the private historical archive. The sanitized
directory is the intended future canonical development workspace after remote
publication is verified. Local `.aios`, `.orchestra`, executor skills, and
other governance context remain outside Git and must be transferred or
reconfigured separately; no product or operational data is migrated.
