# 0011 · Mailforge Data Studio

**Status**: In Progress
**Task**: `MAILFORGE-DATA-STUDIO-021`

## Authority

`DataStudioService` is the single application authority for structured source
inspection, declarative transformation, preview, recipe revisions, and durable
processing jobs. The browser, CLI, and `/api/v1/data-studio` endpoints delegate
to it. `CampaignStore` remains the only modern SQLite authority; contacts,
provider catalogs, segmentation, Message Studio, and automation retain their
existing owners.

## Current contract

The implementation supports bounded TXT, CSV, TSV, JSON, and JSONL input;
typed ordered steps; deterministic previews; immutable revision-pinned jobs;
source-row validation errors and provenance; provider catalog classification;
aggregation; multi-source set operations and bounded INNER/LEFT joins;
atomic TXT/CSV/TSV/JSON/JSONL artifacts; explicit contact imports; Message
Studio preview snapshots; and pinned Automation actions. Standard recipes never
evaluate arbitrary code.

## Remaining acceptance

Full large-workload qualification, restart/cancellation evidence under load,
backup/restore during connected processing, complete keyboard/a11y coverage,
recipient-batch campaign preparation, controlled real-provider qualification,
and trusted imported-code extensions remain open. The built-in trusted
extension is deliberately local, disabled by default, and not a generic plugin
platform. These limits are recorded as partial rather than implied complete.
