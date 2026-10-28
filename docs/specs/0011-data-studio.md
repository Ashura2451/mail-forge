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

The implemented slice supports bounded TXT, CSV, TSV, JSON, and JSONL input;
typed ordered steps; deterministic previews; immutable revision-pinned jobs;
source-row validation errors; provider catalog classification; aggregation; and
atomic JSONL/error artifacts. Standard recipes never evaluate arbitrary code.

## Remaining acceptance

Joins, append/intersect/subtract, CSV/TSV/JSON exports, explicit downstream
destinations, workflow consumption, registered trusted extensions, measured
10k/100k/1m workloads, and the complete connected scenario remain open. Their
absence is recorded as partial rather than implied complete.
