# Resolution, Mailforge Policy Authority 011

The independent review identified three remaining issues. The executor corrected each one and verified the result locally:

1. Trusted inbound suppression writes now share the suppression helper that records immutable history and advances the policy revision in the same transaction.
2. A previously simulation-authorized running campaign can record an explicit post-migration execution declaration without reconstructing historical authorization.
3. A reservation abandoned before dispatch is marked canceled; sender quota queries and deferred retry limits exclude that canceled pre-dispatch attempt.

Verification: 134 tests passed; Ruff checks and formatting passed; the package built successfully; the lockfile check passed; and the repository secret check passed. A requested independent re-review could not complete because the reviewer reached its usage limit, so no independent approval is claimed here.

## Later independent review and closure

An independent review of committed 011 identified three additional issues:

- reserved work canceled before dispatch could leave a phantom attempt;
- SMTP pacing was reserved before sender-policy admission; and
- approved inbound actions did not populate `applied_at`.

All three were corrected locally and covered by focused regression tests. The
milestone remains locally validated, but the review verdict is retained as
"changes requested" because the reviewer did not issue a fresh approval after
the fixes.
