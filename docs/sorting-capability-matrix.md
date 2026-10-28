# Sorting capability traceability

This matrix is the parity evidence register for
`MAILFORGE-SORTING-PARITY-PY315-002`, `MAILFORGE-LOCAL-PYTHON-SORTING-003`,
and the source-retirement decision in `MAILFORGE-LEGACY-RETIREMENT-009`.
“PASS” means the capability is present in
the unified package and covered by synthetic tests; external DNS and SMTP
systems are not exercised. The legacy source rows are provenance records from
the complete inspected contents of `SORTER/` and `MSORTER/`; those Python
wrappers are now retired and their data paths are preserved.

| Source | Capability | Implementation | Test evidence | Result | Compatibility status |
| --- | --- | --- | --- | --- | --- |
| MSORTER/main.py `FileEngine` (retired source) | TXT ingestion and email extraction | `application.ingest`, `application.sorting.InputReader` | `test_supported_inputs_are_normalized_and_deduplicated`, `test_unified_sort_campaign_preserves_outputs_and_summary` | PASS | Canonical owner |
| MSORTER/main.py `FileEngine` (retired source) | CSV ingestion | Same | `test_supported_inputs_are_normalized_and_deduplicated` | PASS | Canonical owner |
| MSORTER/main.py `FileEngine` (retired source) | JSON-array streaming ingestion | `application.ingest` with `ijson` | `test_supported_inputs_are_normalized_and_deduplicated` | PASS | Canonical owner; bounded |
| MSORTER/main.py `FileEngine` (retired source) | JSONL/object-line recursive extraction and malformed-line handling | `application.ingest` | `test_supported_inputs_are_normalized_and_deduplicated`, `test_json_object_lines_and_mixed_unicode_extraction` | PASS | Canonical owner |
| MSORTER/extract_emails.py `extract_emails_from_json` (retired source) | JSONL `e`-field extraction | `application.ingest.iter_jsonl_email_field` | `test_jsonl_field_extractor_preserves_legacy_e_field_contract` | PASS | Canonical owner |
| MSORTER/main.py `EmailValidator` (retired source) | Normalization and syntax validation | `domain.email`, `AddressValidator` | `test_normalization_and_validation_match_legacy_intent`, sorting campaign test | PASS | One authoritative rule set |
| MSORTER/main.py `Deduplicator` | SQLite-backed deduplication | `application.sorting.EmailSorter` | sorting campaign and resume tests | PASS | Preserved |
| MSORTER/main.py `ProviderEngine` (retired source) | Configurable provider routing | `infrastructure.providers.ProviderCatalog` | `test_canonical_sorting_input_validation_checkpoint_and_provider_routing`, sorting campaign test | PASS | Canonical owner |
| MSORTER/main.py `ProviderEngine` (retired source) | Registered-domain and MX provider classification | `ProviderCatalog` and `DNSService` | `test_dns_sync_async_cache_and_failure_paths` | PASS | Canonical owner |
| MSORTER/main.py `EmailValidator` (retired source) | Disposable-domain classification, including subdomains and fallback defaults | `DisposableDomainStore` plus domain classification | sorting campaign test, `test_json_object_lines_and_mixed_unicode_extraction` | PASS | Canonical owner |
| MSORTER/disposable.py (retired source) | Disposable-domain refresh | `infrastructure.disposable.DisposableDomainStore` | `test_disposable_refresh_is_atomic_and_validates_source`, `test_disposable_refresh_redacts_url_and_rolls_back_sidecar_failure` | PASS | Canonical explicit network boundary |
| MSORTER/main.py `DNSEngine` (retired source) | Sync MX lookup and failure mapping | `infrastructure.dns.DNSService` | `test_dns_sync_async_cache_and_failure_paths` | PASS | Mocked canonical adapter |
| MSORTER/main.py `DNSEngine` (retired source) | Async MX lookup and configurable concurrency | `DNSService.resolve_batch_async` | `test_dns_sync_async_cache_and_failure_paths` | PASS | Mocked canonical adapter |
| MSORTER/main.py `DNSEngine` (retired source) | DNS cache, preflight, retry, numeric error mapping, and circuit recovery | `DNSCache`, `DNSService` | `test_dns_sync_async_cache_and_failure_paths`, `test_dns_numeric_errors_and_circuit_recovery` | PASS | No live network claim |
| MSORTER/main.py `FileEngine`/`EmailSorter` (retired source) | Streaming, input bounds, and configurable filesystem paths | `application.ingest`, `SortConfig` | input-limit tests and sorting campaign path-with-spaces test | PASS | Canonical owner; bounded |
| MSORTER/main.py `FileHandleManager`/`EmailSorter` | Provider outputs and master output | `EmailSorter` result writer | `test_unified_sort_campaign_preserves_outputs_and_summary` | PASS | Output contract retained |
| MSORTER/main.py `Stats`/`write_summary_json` | Summary and diagnostics | `SortStats`, `_write_summary`, file logging | sorting campaign test | PASS | JSON summary retained, typed package owner |
| MSORTER/main.py `Checkpoint` | Atomic checkpoint, output-boundary recovery, and resume | `infrastructure.checkpoint`, `application.sorting.Checkpoint` | checkpoint tests, `test_interrupted_campaign_resumes_idempotently`, `test_resume_truncates_uncheckpointed_output_after_write_failure` | PASS | Recovery state preserved |
| MSORTER/main.py CLI (retired source) | Sort, summary, clean, interactive, debug, and DNS controls | `interfaces.cli` | `test_cli_controls_and_cleanup_are_available`, CLI help checks | PASS | Canonical CLI |
| MSORTER/main.py cleanup | Results cleanup with confirmation | `EmailSorter.delete_results` | `test_cli_controls_and_cleanup_are_available` | PASS | Confirmation boundary retained |
| SORTER/email_formatting.py (retired source) | Original formatting transformation | `application.transform.format_email_file` | `test_canonical_transformations_preserve_file_contract` | PASS | Fixed-file data contract retained |
| SORTER/duplicate.py (retired source) | First-occurrence duplicate removal | `application.transform.remove_duplicate_lines` | `test_canonical_transformations_preserve_file_contract` | PASS | Behavior retained |
| SORTER/main.py (retired source) | Python-list conversion | `application.transform.write_legacy_list` | `test_canonical_transformations_preserve_file_contract` | PASS | Representation retained |
| SORTER/email_numbers.py (retired source) | Safe list parsing and counting | `application.transform.load_legacy_list` / `count_legacy_list` | `test_transform_security` | PASS | `eval` removed; feature retained |
| SORTER scripts (retired source) | Sequential invocation and import safety | `interfaces.cli`, canonical transform services | `test_sorting_compatibility`, `test_transform_security` | PASS | Data-only paths preserved |
| Unified sorter | Structured explainable result model and versioned JSONL export | `domain.results`, `processing.jsonl`, summary output | `test_multi_source_structured_results_and_quarantine_reconcile`, `test_structured_resume_restores_dedupe_and_output_boundary` | PASS | NEW CAPABILITY |
| Unified sorter | Deterministic multi-source processing with shared deduplication and provenance | `EmailSorter.sort_files` and CLI multiple inputs | `test_multi_source_structured_results_and_quarantine_reconcile`, `test_single_source_provenance_uses_record_position` | PASS | LEGACY IMPROVED |
| Unified sorter | Optional rejection/quarantine report with reconciled counts | `quarantine.jsonl`, `SortStats.quarantined` | `test_multi_source_structured_results_and_quarantine_reconcile` | PASS | NEW CAPABILITY |
| Unified sorter CLI | Profiles, dry-run preview, progress controls, and structured output paths | `SortConfig.for_profile`, CLI flags | `test_dry_run_profile_writes_explainable_preview_without_legacy_outputs` | PASS | NEW CAPABILITY |
| Unified sorter | Changed-input and configuration fingerprint protection on resume | checkpoint `source_signature`, `configuration_signature` | `test_changed_input_is_not_resumed_silently`, `test_configuration_change_is_not_resumed_silently` | PASS | LEGACY IMPROVED |
| Unified sorter | Spreadsheet formula-injection guard for CSV exports | `_csv_safe` master writer | `test_csv_formula_guard_is_conservative` | PASS | NEW CAPABILITY |
| Disposable dataset | Refresh provenance and safe preservation on failed refresh | `.meta.json` sidecar with redacted source/hash/time | `test_disposable_refresh_is_atomic_and_validates_source`, `test_disposable_refresh_redacts_url_and_rolls_back_sidecar_failure` | PASS | LEGACY IMPROVED |
| Local tooling | Normal shell `python`/`python3` resolve to uv-managed CPython 3.15.2 | uv default shims and backed-up zsh PATH entries | fresh login-shell evidence in task report | PASS | NEW CAPABILITY |
| Unified sorter | Reproducible throughput, memory, duplicate-heavy, and resume measurements | `docs/performance/sorting-2026-10-27.md` | baseline/enhanced synthetic benchmark evidence | PASS | NEW CAPABILITY |

## Explicit non-claims

- No real DNS, disposable-domain HTTP refresh, SMTP, recipient campaign, or
  production filesystem was exercised.
- The matrix validates the sorting implementation and compatibility wrappers;
  it does not claim that the separate MAILER delivery subsystem is migrated.
- Existing user-owned data and the tracked `MAILER/.env` were not read or
  modified. The tracked secret-file exposure remains an owner-led remediation
  item and blocks publication.
