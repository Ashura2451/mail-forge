# Review, refactor/mailforge-mailer-retirement-010, 2026-10-27

**Reviewed by**: GPT-6 (author model unknown)
**Scope**: 15 files, uncommitted
**Verdict**: Changes requested

## Summary
The change adds a shared eligibility evaluator, durable policy records, lifecycle operations, and CLI commands. The basic synthetic paths have useful coverage, but overlapping scopes can discard restrictions, dispatch can race policy changes, and authorization and audit records are not consistently durable. Preview and secret handling also need attention before the policy authority can be relied on.

## Major
### 🟠 Overlapping scopes discard active restrictions, `src/mailforge/infrastructure/campaign_store.py:1358`
**Problem**: The preference query picks a single newest record across global, list, campaign, and sender scopes. For example, a global marketing disable followed by a list-specific enable makes the recipient eligible in that list, even though the global disable was never revoked. The evidence query at line 1283 has the same issue: a later list-scoped established record hides a global withdrawal or conflict.
**Why it matters**: A narrower update silently erases the effect of a broader applicable restriction and simulation dispatch proceeds despite a known opt-out or withdrawal.
**Suggested fix**: Resolve the latest record independently for each scope and define explicit restrictive precedence for overlapping scopes; test both insertion orders and explicit same-scope reversals.

### 🟠 Policy changes can race the dispatch boundary, `src/mailforge/application/campaigns.py:516`
**Problem**: Eligibility is read through separate connections before the dispatch guard and `mark_attempt_started`, without a transaction or version check tying the decision to the attempt transition. A preference disable, withdrawal, deactivation, or suppression committed after the reads but before the attempt starts is not observed.
**Why it matters**: A recipient can be dispatched after a restriction was durably recorded, including when a guard delays the worker between the check and the send.
**Suggested fix**: Define a serialized dispatch boundary and atomically recheck applicable policy while starting the attempt, or use an equivalent versioned reservation protocol. Add a deterministic concurrency test that commits a restriction between the first evaluation and attempt start.

### 🟠 Authorization and declaration commit independently, `src/mailforge/application/campaigns.py:413`
**Problem**: The campaign authorization is committed before the operator declaration is inserted in a separate transaction. If the process stops or declaration insertion fails in between, the campaign remains authorized with no declaration; execution checks only the campaign flag.
**Why it matters**: An operation advertised as requiring an auditable operator declaration can proceed without its durable evidence, including after restart.
**Suggested fix**: Persist authorization and its declaration together in one transaction, and ensure execution cannot accept an authorization lacking its corresponding declaration. Cover failure and restart between the two writes.

### 🟠 Preference evidence references bypass the secret guard, `src/mailforge/infrastructure/campaign_store.py:1313`
**Problem**: `set_preference` validates source and actor but persists `evidence_ref` without checking it, unlike authorization evidence. The CLI exposes this field directly.
**Why it matters**: A credential-bearing reference rejected by the evidence path can be written into SQLite and copied into backups through the preference path, violating the stated secret-free durable-state boundary.
**Suggested fix**: Apply the same reference validation to preference evidence references and other newly persisted free-form policy fields, and test rejection without persisting the value.

### 🟠 Suppression reaffirmation overwrites historical facts, `src/mailforge/infrastructure/campaign_store.py:1087`
**Problem**: Reaffirming a suppression overwrites its previous reason, effective time, and expiry, while the history row records only action, actor, source, and policy version. Neither the original nor the new reason and expiry are preserved in history.
**Why it matters**: Operators cannot reconstruct why or for how long an earlier suppression applied, despite the new history contract claiming that suppression history is preserved.
**Suggested fix**: Record a complete immutable snapshot or before/after values for each suppression event, including reason and effective/expiry times, and verify history after reaffirmation and backup.

### 🟠 Preview does not re-evaluate current policy, `src/mailforge/application/campaigns.py:389`
**Problem**: Preview returns the stored preparation state and reason without invoking the new eligibility evaluator. Disabling a preference or deactivating a contact after preparation leaves preview showing the recipient as eligible until dispatch, despite the architecture and documentation saying preview uses the shared current policy.
**Why it matters**: Operators reviewing a campaign see an outdated audience and cannot rely on preview to explain the operation they are about to authorize.
**Suggested fix**: Include current eligibility decisions in preview, clearly distinguishing them from the prepared snapshot, and test changes made after preparation.

## Strengths
- Address-only imports do not fabricate recipient evidence, and unknown evidence remains visible in the synthetic workflow.
- The tests exercise late preference changes, preserved deactivation on reimport, backup/restore, and concurrent preference writes.

## Test coverage
The new tests cover basic purpose isolation, suppression, evidence withdrawal, stale prepared audiences, lifecycle operations, and backup. They do not cover overlapping scope conflicts, a policy update racing attempt start, authorization/declaration partial failure, secret-bearing preference references, suppression reason history, or preview after policy changes. No tests were run during this read-only review.
