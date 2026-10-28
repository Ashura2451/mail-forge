# Programmable operations

Mailforge’s Functions workbench stores reusable transformations in the local
SQLite authority. Declarative functions are the normal path: they use a small
typed allowlist of text, data, and email operations and are versioned by
immutable revision and content hash.

Every use pins `function_id`, `revision_id`, and the revision hash. Data Studio,
Message Studio, inbound rules, and Automation all call the same
`ProgrammableOperationsService`; none accepts Python, SQL, shell commands, or
provider credentials in a recipe, template, rule, workflow, or HTTP request.

## Trusted local Python

Python extensions are an operator-only escape hatch. Register one with the
local CLI after reviewing its manifest and confirming the exact SHA-256:

```text
mailforge functions install-python \
  --database /path/to/mailforge.sqlite3 \
  --source /path/to/extension.py \
  --manifest /path/to/manifest.json \
  --confirm-checksum <sha256>
```

Installed source is copied into the application data directory, disabled by
default, and enabled separately with `mailforge functions enable NAME`. It is
run in a short-lived subprocess with JSON-only input, a timeout, and bounded
output. The checksum is verified before each run. This is process isolation,
not a sandbox: trusted local Python must be reviewed as privileged code and
may have host capabilities available to Python.

The browser and API can list, preview, version, and enable/disable registered
functions, but cannot upload or register executable source. Backups include
the registry and installed extension files; source paths are relative to the
operator data directory so restore into a new directory remains coherent.

Useful CLI commands are:

```text
mailforge functions list --database PATH
mailforge functions revisions NAME --database PATH
mailforge functions preview NAME --database PATH --records records.json
mailforge functions history --database PATH
```
