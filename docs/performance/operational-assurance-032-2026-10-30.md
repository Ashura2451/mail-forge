# Operational assurance 032 local qualification measurements

Date: 2026-10-30
Environment: macOS arm64, CPython 3.15.2, local SQLite, synthetic data only.

These are bounded local measurements, not production SLOs or provider-throughput
claims.

| Operation | Workload / method | Observed value |
| --- | --- | ---: |
| Health aggregation | Fresh local runtime, one sample | 14.29 ms |
| Job listing | Ten local calls, empty synthetic state | 0.79 ms average |
| Incident listing | Three local calls | 8.02 ms average |
| Native Keychain cycle | Store, resolve, rotate, revoke | 18.45 ms |
| Backup | SQLite plus 1 MiB owned artifact | 70.29 ms |
| Staged restore | Same backup into a new directory | 9.32 ms |
| SQLite contention | Eight concurrent integrity checks | 42.86 ms total; all passed |
| Active Data Studio workload | 5,000 JSONL rows | 71.85 ms to completion; process max RSS 51,642,368 bytes |

The broader measurement process max RSS was 82,427,904 bytes. The workloads are
small and synthetic; no scale limit, production latency target, or external
provider behavior is inferred.

Qualification evidence also includes process termination/restart recovery for
Campaigns and Data Studio, atomic backup publication with stale temporary-file
cleanup after interruption, staged restore readback, source-failure health
semantics, and persistent incident re-observation after acknowledgement.
