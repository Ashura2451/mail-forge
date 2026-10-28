# Mailforge Data Studio

Data Studio is the canonical local structured-data processing workbench. It
uses `DataStudioService` for schema inspection, typed linear recipes, bounded
previews, pinned revisions, durable jobs, and app-owned JSONL/error artifacts.
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
  publish output and error artifacts only after atomic completion.
- The browser workbench provides source selection, step editing, recipe save,
  preview, and job start. CLI commands are under `mailforge data-studio`.

## Deliberate boundaries

This milestone does not claim the entire packet is complete. CSV/TSV/JSON
export variants, multi-source joins/append/subtract, contact import mapping,
Message Studio snapshot consumption, workflow triggers, and a runnable trusted
extension contract remain follow-up slices. No arbitrary Python/JavaScript
expression or remote script execution is accepted. Provider classification is
local catalog evidence only; it is not sending-provider selection or consent.

Performance evidence for representative 10k/100k/1m workloads and a complete
52-step connected scenario remains unverified. No real provider, mailbox,
credential, recipient, or private dataset was accessed.
