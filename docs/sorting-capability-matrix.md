# Sorting capability matrix

The maintained sorting workflow is owned by `src/mailforge/application/` and
uses packaged resources under `src/mailforge/resources/sorting/`.

| Capability | Status | Current evidence |
| --- | --- | --- |
| TXT, CSV, JSON, JSONL ingestion | PASS | Ingest and sorter regression tests. |
| Validation and duplicate handling | PASS | Domain validation, deterministic dedupe, quarantine, and parity tests. |
| Provider/domain classification | PASS | Packaged provider catalog plus offline MX fallback. |
| Disposable-domain classification | PASS | Packaged disposable-domain dataset with explicit refresh into user data. |
| Flexible export | PASS | Structured JSONL, CSV, provider files, summary, and bounded artifact tests. |
| Large-file behavior | PASS | Representative 13,000-record workload qualification. |

Sort results, logs, checkpoints, and refreshed datasets are written below the
external Mailforge sorting data directory by default. The old MSORTER/SORTER
directory structures are not runtime inputs.
