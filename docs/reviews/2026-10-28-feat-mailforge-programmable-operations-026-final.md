# Review, feat/mailforge-programmable-operations-026, 2026-10-28

**Reviewed by**: inline executor plus independent worker review
**Scope**: programmable registry, schema 20 migration, trusted local Python
boundary, Data Studio, Message Studio, Inbound, Automation, API/CLI/browser,
backup/restore, and packaged macOS runtime
**Verdict**: Merged; release caveats recorded below

## Delivered

- Added one SQLite-backed function registry with immutable revisions, content
  hashes, execution history, restart recovery, and relative extension paths.
- Added bounded declarative text/data/email operations and two reviewed local
  reference transformations.
- Added privileged checksum-confirmed Python extension installation. Extensions
  are disabled by default, run in a subprocess with JSON-only inputs, timeout,
  CPU/address-space/file-descriptor limits where supported, bounded output, and
  process-group cleanup. This is not a sandbox.
- Integrated pinned functions into Data Studio recipes and artifact summaries,
  Message Studio function blocks and campaign snapshot hashes, inbound rules,
  and idempotent Automation actions.
- Added CLI/API/browser workbench surfaces. Executable source is not accepted
  through HTTP, browser state, templates, recipes, rules, or workflow graphs.
- Added schema 20 migration, backup/restore coverage for extension files, user
  documentation, feature specification, and package/runtime validation.

## Independent review follow-up

The independent reviewer initially identified seven material risks: mutable
Python revision identity, incomplete interrupted-execution recovery, unpinned
Automation/Inbound references, weak subprocess cleanup, missing Data Studio
provenance, incomplete revision/history surfaces, and missing input byte caps.
The executor addressed the first six in this tree and retained bounded record,
parameter, and output sizes plus subprocess resource limits. Serialized input
is bounded by the request/output contract and record-count caps; arbitrary
large code or nested execution inputs are not accepted by the declarative
validator or extension runner.

## Acceptance matrix

| Criterion | Evidence | Result |
|---|---|---|
| A–E registry, reuse, declarative functions, revisions, preview | `programmable_operations.py`, API, CLI, browser, focused tests | Pass |
| F–I extension registration, explicit trust, code boundary, execution | CLI checksum confirmation, disabled default, subprocess tests | Pass |
| J–K schemas and exact revision/checksum pinning | schema 20 revision columns, consumer content hashes | Pass |
| L–O Data Studio, Message Studio, artifacts, campaigns | integration tests and campaign snapshot hash | Pass |
| P–Q inbound and workflow actions | pinned rule/action paths and existing integration suite | Pass |
| R–U durable errors/recovery, restart, backup/restore, package runtime | recovery code, 241 tests, backup test, arm64 bundle smoke | Pass |
| V–W browser surface, resource and security controls | TypeScript/oxlint/build, API boundary, resource-limited worker | Pass |
| X–AA regression, bounded behavior, connected validation, full suite | 241 Python tests, browser acceptance, package build | Pass with performance evidence limited to bounded tests |
| AB–AC review and documentation truth | this review, spec, operator docs, independent review follow-up | Pass |
| AD–AE publication and main integration | branch push, PR #12 merge, and post-merge verification on `origin/main` at `c3e482f` | Pass |

## Validation executed

- `uvx --from ruff ruff check src`: passed.
- `PYTHONPATH=src .venv/bin/pytest -q`: **241 passed**, one existing
  Starlette/httpx deprecation warning.
- `npm --prefix web run lint`: passed with the existing React effect warnings.
- `npm --prefix web run build`: passed.
- `./scripts/package-macos.sh`: passed; arm64 `Mailforge.app` built at 137M.
- Packaged Python launcher initialized a fresh data directory and reported
  supported schema 20. No checkout or staging path was present in bundle
  resources.
- `git diff --check`: passed.

## Residual caveats

The trusted Python path is intentionally privileged local code, not a security
sandbox. No external provider or cloud execution was added. A separate
performance benchmark beyond bounded synthetic tests was not run; the product
limits are explicit and the worker is single-local-process scoped.
