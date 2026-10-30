# Mailforge runtime ownership contract

Mailforge is a local single-runtime application. One user installation may
own one active runtime at a time. Separate simultaneous database runtimes are
not a supported product contract for this milestone.

`DatabaseProcessLock` enforces that contract with a stable, user-scoped
application lock directory. The directory advisory lock is held for the
entire protected operation. The database path is diagnostic context only; it
is not the lock identity.

The contract requires that:

- canonical, relative, symlink, and hard-link database aliases contend for
  the same application lock;
- database creation, replacement, and staged restore remain inside the same
  lock lifetime;
- the lock directory and marker are private, non-symlinked paths, and the
  marker is never deleted as part of ordinary release;
- `serve`, application backup/restore, and campaign backup use the same
  coordination authority;
- a second local runtime fails closed with an ownership error; and
- no production guarantee is inferred from the local advisory lock until
  signed/notarized package qualification is separately completed.

This deliberately rejects independent simultaneous databases rather than
pretending that pathname or inode locking can safely support them. Supporting
multiple databases would require a separately ratified ownership protocol,
including lifecycle, replacement, backup/restore, and operator-conflict
semantics.
