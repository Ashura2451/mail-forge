# MAILFORGE-SECRET-RELEASE-004 security and release review

Date: 2026-10-27
Branch: `feat/mailforge-secret-release-004`

## Redacted exposure assessment

- `MAILER/.env` was present in the current `HEAD` and in the sole reachable
  local history commit (`f095d6a`). Its working copy remains intact; only the
  Git index entry was removed.
- A metadata-only local scan found eight non-empty environment assignments,
  including sender, password, SMTP host, and port classes. Values were not
  printed, logged, hashed, or sent to external tools. A placeholder heuristic
  flagged the values as example-like, but credential validity is not
  established.
- All three reported milestones are in the current local ancestry:
  `3a6f0b6`, `98ea51e`, and `4daac99`. No local tags or remote-tracking refs
  are present. `git ls-remote origin` advertised no heads or tags, so remote
  publication cannot be classified from the available evidence; this is not
  proof that no external copy exists.
- No other tracked path matched the bounded sensitive-file-name audit.

Disposition: treat the file as potentially sensitive with unknown validity.
The Business Owner must identify the affected provider(s), revoke or rotate
any credential that may be active, and verify replacement values before
authorizing publication. No provider-side action was performed.

## Changes

- Removed `MAILER/.env` from Git tracking while preserving its inode, size,
  mode, timestamp, and contents locally.
- Added ignore rules for local `.env` files while allowing safe `.env.example`
  files.
- Added [MAILER/.env.example](../../MAILER/.env.example) with only synthetic
  placeholders.
- Added `scripts/check-secrets.sh`, an offline redacted guard for tracked env
  filenames and high-confidence private-key/token patterns; CI runs it before
  dependency setup. The staged-diff matcher consumes the full diff before
  matching so `pipefail` cannot turn an early-match SIGPIPE into a false
  negative.
- Documented local configuration setup, scan limitations, rotation ownership,
  and publication procedure in `README.md` and `docs/development.md`.

## Acceptance evidence

| Criterion | Status | Evidence |
| --- | --- | --- |
| Local configuration preserved | PASS | `git rm --cached`; before/after metadata and file existence checks matched. |
| Future env tracking protected | PASS | `git check-ignore --no-index` matches `MAILER/.env`; example remains allowed. |
| Safe example available | PASS | `MAILER/.env.example` contains synthetic values only. |
| Secret guard | PASS | `bash scripts/check-secrets.sh` passes without printing matches; CI invokes it. |
| Existing send gate | PASS | Existing gate test plus CLI refusal; no SMTP connection or send performed. |
| History classification | PASS | Reachable local refs/path metadata audited; remote publication is NOT VERIFIED. |
| Credential validity/rotation | BLOCKED | Requires Business Owner/provider evidence and authorization; no external action taken. |
| History rewrite/publication | BLOCKED | No force-push or rewrite authorized; do not publish until rotation/disposition is approved. |

The fresh independent review found the guard pipeline issue above; it was fixed
and the guard, shell syntax, full test suite, lint, formatting, and staged-path
checks were rerun successfully. Follow-up review confirmed no remaining
code-level findings. The review still returns `BLOCKED` for release
authorization because credential validity/rotation and publication authority
remain unverified.

## Independent sorting milestone reconciliation

The prior corrected sorting state was reviewed read-only against the requested
fix list. The earlier findings remain covered by the existing regression tests
and implementation: checkpoint flushing, source/configuration fingerprints,
structured-output resume and duplicate recovery, source-record provenance,
disposable refresh rollback, and sensitive URL redaction. No new sorting defect
was found in this bounded review. The review does not constitute Business Owner
approval for publication.
