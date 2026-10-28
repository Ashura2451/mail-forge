# Mailforge Data Studio

Data Studio is the canonical local structured-data processing workbench. It
uses `DataStudioService` for schema inspection, typed recipes, bounded previews,
pinned revisions, durable jobs, and app-owned export/error artifacts.
The browser, CLI, and versioned API call the same service.

## Implemented slice

- TXT, CSV, TSV, JSON arrays/objects, and JSONL object inputs with source-row
  identity and bounded 1 GiB processing.
- Select, rename, drop, whitespace/case/email normalization, deterministic
  type conversion, fallback values, required/allowed validation, typed filters,
  safe computed fields, duplicate removal, provider classification through the
  existing provider catalog, and grouping/count/sum aggregation.
- Preview is bounded and non-destructive. Validation and transformation errors
  retain source-row and field context.
- Recipes are durable and revisioned. Jobs pin a revision, copy the source
  into an app-owned directory, recover stale running jobs to pending, and
  publish output and error artifacts only after atomic completion. Multi-source
  jobs retain a manifest and provenance for every emitted row.
- Append, compatible union, intersect, subtract, and explicit-key INNER/LEFT
  joins are supported. Joins use temporary indexed SQLite state, reject missing
  keys as non-matches, preserve one-to-many matches, and suffix or reject field
  collisions according to the recipe. Per-key and total-output caps prevent
  accidental Cartesian-product exhaustion; aggregate groups are accumulated
  across processing batches with a bounded group limit.
- TXT, CSV, TSV, JSON, and JSONL exports are UTF-8, deterministic, path-safe,
  atomically published, and CSV/TSV formula-injection protected.
- Contact import is explicit and delegates mapping, typed validation, conflict
  handling, list membership, suppression, and provenance to SegmentationService.
  Contact imports reject artifacts that exceed the explicit row limit rather
  than silently truncating. Message previews delegate rendering to
  MessageStudioService and pin the source artifact hash plus published message
  revision. Workflow actions pin a recipe revision and permitted source
  artifact and execute through the existing AutomationService worker boundary.
- The trusted extension contract is intentionally narrow: the built-in
  `email_domain_normalizer` is versioned, checksummed, local, disabled by
  default, and has no imported-code or subprocess execution path.
- The browser workbench provides multi-source selection, combine configuration,
  export selection, preview, job execution, downloads, contact import, and
  Message Studio personalization preview. CLI commands are under
  `mailforge data-studio`.

## Deliberate boundaries

This milestone does not claim unrestricted extension execution or external
provider qualification. No arbitrary Python/JavaScript expression, subprocess,
SQL, or remote script execution is accepted. Provider classification is local
catalog evidence only; it is not sending-provider selection or consent.
The Message Studio integration currently provides deterministic row previews
and consumer snapshots; full recipient-batch campaign preparation remains a
separate qualification item. Workflow execution is available through the
backend and the browser editor now exposes recipe/job/artifact selection, but
end-to-end recovery and completion-event evidence remain open.

## Local qualification evidence

Synthetic JSONL workload, generation excluded from the timed interval, on the
local development machine:

| Input | Result | Duration | Throughput | Peak RSS | Temporary disk | Output |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 10,000 | completed; 5,000 after dedupe | 5.954 s | 1,679.6 rows/s | ~123 MiB | 3.65 MiB | 820,563 B |
| 100,000 | completed; 50,000 after dedupe | 62.673 s | 1,595.6 rows/s | ~142 MiB | 26.37 MiB | 8,405,564 B |
| 1,000,000 | not qualified | stopped after >90 s without a result | — | — | — | — |

The 1m run was stopped rather than extrapolated. Restart recovery under load,
backup/restore during a live job, and a complete 52-step connected scenario
remain open. No real provider, mailbox, credential, recipient, or private
dataset was accessed.
