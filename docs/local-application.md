# Mailforge local application

## Install and launch on macOS

The initial qualified target is macOS arm64. Build a controlled application
bundle from a clean checkout:

```sh
bash scripts/package-macos.sh
open dist/Mailforge.app
```

The bundle includes the locked CPython 3.15.2 runtime, Python dependencies,
Mailforge package, migrations, provider resources, and compiled React assets.
The operator does not need Python, uv, npm, Vite, or a separate frontend
server. The bundle is not signed or notarized by this repository.

The default mutable data root is:

```text
~/Library/Application Support/Mailforge/
```

It contains `mailforge.sqlite3`, `config/settings.json`, `backups/`, `logs/`,
`runtime/`, `artifacts/`, and app-owned processing directories. These files
remain outside the application bundle so replacing the bundle does not remove
operator data. Use `mailforge app --data-dir PATH ...` when a different local
root is explicitly selected.

## Lifecycle controls

The bundled launcher opens the browser and starts the local service if needed.
The Python CLI controls are:

```sh
mailforge app status
mailforge app start
mailforge app open
mailforge app stop
mailforge app restart
```

The installed bundle's launcher is the normal entry point. The service binds
only to `127.0.0.1`, holds the existing database process lock, and returns the
existing runtime when launched twice. A port occupied by another process is a
clear startup failure; Mailforge never terminates an unrelated process.
Closing the browser leaves the service and supervised local workers running.

## First run and maintenance

The first dashboard shows a concise setup card. Completing it records only
local preferences and keeps simulation mode enabled. It does not configure a
provider, infer consent, contact an external service, or send a message.

The Runtime & Maintenance screen shows application version, an opaque data
directory identity, schema compatibility, database integrity, worker state,
and backup metadata. It does not show credential values, full local paths, or
arbitrary shell controls.

## Backup and restore

Create and validate a complete local backup:

```sh
mailforge app backup
mailforge app validate-backup PATH_TO_BACKUP.zip
mailforge app restore --backup PATH_TO_BACKUP.zip --destination PATH_TO_NEW_DATA
```

Archives contain SQLite, settings, and app-owned artifact directories with a
compatibility manifest and SHA-256 hashes. Restore rejects missing, tampered,
unsafe, incompatible, or non-empty destinations unless `--replace` is
explicitly supplied. The active default data directory cannot be destructively
replaced. Compatible startup migrations take a verified SQLite snapshot in
`backups/` before changing schema; a newer database schema fails closed.

## Packaging rationale and limits

The chosen approach is a small application bundle assembled from the existing
runtime and wheel. It keeps the backend authoritative and avoids a second
desktop stack, an embedded Node runtime, or a service installer. PyInstaller,
Electron, and Tauri would add another compatibility and upgrade boundary before
the product needs one; the bundle builder is easier to inspect and reproduce
for the current macOS target.

The script must be rerun for a new build identity. It does not claim Apple
signing, notarization, automatic updates, Intel/Windows/Linux qualification,
real-provider qualification, inbox delivery, or public production readiness.

## Troubleshooting

- `Mailforge did not become healthy`: inspect the private
  `logs/application.log` under the selected data root.
- `port ... is already in use`: choose another `--port` or stop the unrelated
  local process yourself; Mailforge does not replace it.
- `database schema ... newer`: install the newer Mailforge build; do not
  downgrade the database in place.
- Invalid backups remain listed as rejected and are never restored.
