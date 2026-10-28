# Programmable Operations and Trusted Local Extensions

Status: Complete
Owner: Mailforge local application
Milestone: MAILFORGE-PROGRAMMABLE-OPERATIONS-026

## Outcome

Mailforge will provide one durable, revisioned function registry for reusable
data, text, email, and content transformations. Declarative functions are the
default and are evaluated by a bounded interpreter owned by the application.
Trusted Python is an explicit local-operator extension path: source is
registered from a local file, its manifest and checksum are confirmed, it is
disabled until enabled, and execution occurs in a short-lived subprocess.

The browser, HTTP API, message templates, imported data, workflow graphs, and
inbound payloads never supply executable Python. There is no remote execution,
plugin marketplace, provider credential access, or claim that a trusted local
Python extension is sandboxed.

## Binding architecture

- SQLite remains the operational authority. Registry rows, immutable function
  revisions, execution history, and consumer references are durable.
- `ProgrammableOperationsService` is the canonical registry and evaluator.
  Data Studio, Message Studio, Inbound Operations, and Automation call it;
  they do not maintain their own function catalogs or evaluators.
- Existing `data_studio_extensions` records remain compatible and are migrated
  into the canonical registry. The existing
  `email_domain_normalizer` behavior remains available as a trusted local
  reference extension and stays disabled by default.
- Declarative function revisions are content-addressed by canonical JSON. A
  consumer stores the exact function revision and content hash it used.
- A function execution receives JSON records and bounded parameters only. It
  cannot receive database connections, provider clients, credentials, shell
  commands, or arbitrary application objects.
- The worker subprocess is resource-bounded where the host permits it, has a
  timeout and output cap, and is terminated as a process group on failure.
  This is isolation from the application process, not a security sandbox.

## Declarative contract

Each revision has an input schema, output schema, operation, parameters,
supported contexts, and a deterministic flag. The initial safe operation set
includes:

- text trim, case conversion, whitespace normalization, join, split,
  replacement, substring, formatting, and bounded extraction;
- data default/coalesce, comparison, numeric calculation, enum mapping, and
  date formatting;
- email normalization, domain extraction, provider classification, and
  role/disposable classification through existing domain authorities.

Expressions are field references and typed literals, not Python, SQL, shell,
or template expressions. Extraction accepts bounded literal delimiters and
strictly limited capture patterns; unsafe or oversized patterns are rejected.
Inputs, outputs, nesting, record counts, and result sizes are bounded.

## Trusted extension contract

The privileged local CLI can install a Python file with an explicit manifest,
entrypoint, version, input/output schemas, supported contexts, and checksum
confirmation. The installed source is copied into the application-owned data
directory, made non-writable to ordinary API flows, and is never accepted in
API JSON, browser state, recipe definitions, templates, or workflow graphs.

The extension entrypoint receives a JSON request and returns JSON records. A
manifest mismatch, checksum mismatch, disabled extension, timeout, malformed
output, or resource limit is a durable failed execution and never a partial
success. Two reviewed examples are maintained: the existing email-domain
normalizer and a structured-label normalizer that derives a bounded label from
record fields.

## Product integrations

- Data Studio recipes may invoke a pinned function revision as a transformation
  step. Preview and jobs use the same evaluator; job artifacts include the
  function revision identity in their summary.
- Message Studio may invoke a pinned function from a content block to derive a
  recipient-specific value. Rendered output is deterministic for the same
  revision, input, and seed, and function references are validated when a
  message revision is saved.
- Campaign preparation freezes function references and their hashes together
  with message/data snapshots; later edits cannot change a prepared campaign.
- Inbound extraction may opt into a pinned declarative function after parsing
  untrusted content. It is a local transformation only and cannot perform an
  external action.
- Automation may run a pinned function action. The action is idempotent by the
  existing workflow correlation key and records its output in durable run
  history; it does not gain a new external-effect authority.

The browser workbench lists registry entries, shows schemas and trust state,
creates declarative revisions, previews bounded samples, and exposes enable/
disable state. It cannot upload or register executable code. CLI commands cover
list, create, preview, revisions, enable/disable, execution history, and
privileged local Python installation.

## Durable invariants and recovery

1. A revision is immutable after creation; edits create a new revision.
2. Every reference contains function id, revision id, and content hash.
3. Disabled or untrusted Python cannot execute.
4. Registry writes and execution records are transactional and idempotent.
5. A timeout or restart leaves a failed/recoverable execution record, never a
   misleading completed record.
6. Backups include registry, revisions, extension manifests, and history;
   restore reopens them without source-checkout dependencies.
7. Deleting a function is archive-only while any recipe, message, workflow,
   inbound rule, or prepared campaign references it.

## Validation plan

Tests cover the declarative contract, revision pinning, bounded extraction,
extension registration/confirmation/checksum, disabled-by-default behavior,
subprocess timeout and malformed output, forbidden API/template code paths,
Data Studio and Message Studio integration, inbound and workflow idempotency,
restart and backup/restore, browser/API/CLI parity, package runtime behavior,
security regressions, deterministic rendering, and bounded performance on a
synthetic connected workflow.

The implementation is complete only when the release and independent-check
packets truthfully identify verified criteria and any remaining limits.
