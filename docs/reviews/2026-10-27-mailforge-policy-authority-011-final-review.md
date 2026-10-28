# Review, refactor/mailforge-mailer-retirement-010, 2026-10-27

**Reviewed by**: gpt-6.1-terra/GPT-6 (author model unknown)
**Scope**: 17 files, uncommitted
**Verdict**: Changes requested

## Summary
The correction implements restrictive scoped precedence, atomic simulation authorization and declaration, preference reference validation, and current eligibility in campaign preview. Operator-created suppression history and policy-revision checks also improve the original findings, but the existing inbound suppression writer bypasses both protections. Existing authorized campaigns and canceled reservations have additional recovery and accounting problems.

## Major
### 🟠 Inbound suppressions bypass the policy revision and history, `src/mailforge/application/campaigns.py:538`
**Problem**: The new dispatch check relies on every policy mutation advancing `policy_state.revision`, but `CampaignStore.decide_inbound_action` writes or reaffirms suppressions directly at `src/mailforge/infrastructure/campaign_store.py:2804` without incrementing the revision or writing `suppression_history`. It updates pending and prepared intents, but not a reserved intent. If an inbound unsubscribe, complaint, or bounce is approved after the eligibility reads and before attempt start, the revision still matches and the reserved recipient proceeds to transport. Reaffirmation also overwrites the old reason without the new before/after history.
**Why it matters**: The original dispatch race and incomplete suppression history remain for the documented trusted inbound path, even though the new operator preference race test passes.
**Suggested fix**: Route inbound mutations through a shared transaction-aware suppression helper that records history and advances the revision in the same transaction. Test an inbound action inserted between eligibility evaluation and attempt start for both dispatch paths, plus inbound reaffirmation history.

### 🟠 Migrated running campaigns cannot obtain the new declaration, `src/mailforge/application/outbound.py:430`
**Problem**: Existing schema-4 campaigns may already be simulation-authorized, but migration 5 creates an empty declaration table. Both execution paths now reject those campaigns without a declaration. Reauthorization is only allowed from ready, scheduled, or paused states; a previously running campaign is rejected before worker recovery and cannot be reauthorized through the service.
**Why it matters**: An interrupted campaign that was recoverable before upgrade becomes stuck after migration, and other existing authorized campaigns unexpectedly require a new declaration without an explicit migration or recovery path.
**Suggested fix**: Provide an explicit auditable reauthorization/recovery workflow for existing campaigns, including running campaigns, without inventing historical declarations. Test migration and restart for authorized ready, paused, and running campaigns.

### 🟠 Revision retries consume sender capacity without sending, `src/mailforge/application/outbound.py:515`
**Problem**: A revision mismatch after `_policy_guard` releases the reservation and immediately retries. The reserved attempt remains in `delivery_attempts` with its sender policy set; sender limit queries count all such rows, including attempts that never started. Because the revision is global, an unrelated contact's policy update can trigger this path.
**Why it matters**: A single unrelated policy update can consume a low per-minute or daily sender quota and defer a campaign even though no message was attempted. Repeated updates can accumulate phantom attempts and distort retry accounting.
**Suggested fix**: Make abandoned pre-dispatch reservations distinguishable and exclude them from send quotas and attempt limits, or restructure reservation so a failed revision check does not consume capacity. Add a test with a low sender limit and an unrelated policy change between the guard and attempt start.

## Strengths
- Scoped preference and evidence resolution now preserves applicable restrictive records across scopes, and the test covers a same-scope evidence renewal.
- A database-trigger failure test verifies that simulation authorization rolls back when declaration insertion fails; preview and secret-reference cases also have focused tests.

## Test coverage
The expanded policy tests address the operator preference race, scoped precedence, declaration rollback, preference reference validation, operator suppression reaffirmation, and current preview. They do not cover inbound suppression racing dispatch, inbound suppression history, migration of already-authorized running campaigns, or sender quota accounting after a revision retry. No tests were run during this read-only review.

## Executor resolution

The three findings above were addressed in the working tree:

- trusted inbound suppressions now use the shared suppression/history/policy-revision transaction;
- already-authorized running campaigns can record an explicit post-migration declaration without fabricating historical approval; and
- pre-dispatch reservation retries are canceled and excluded from sender quota and retry-limit accounting.

Focused regression tests and the full 134-test suite pass after these corrections. A requested independent re-review could not complete because the reviewer reached its usage limit; this file therefore preserves the reviewer’s findings and does not claim an independent approval verdict.
