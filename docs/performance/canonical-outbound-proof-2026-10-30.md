# Canonical outbound proof performance evidence

This is a local synthetic measurement, not a production-scale qualification.

| Operation | Workload | Observed |
| --- | ---: | ---: |
| Published Message Studio revision to controlled proof | 100 recipients, fresh SQLite database, one process | 0.596 s total / 5.96 ms per proof |

The run used `MessageStudioService.prepare_controlled_test` with the canonical
renderer, MIME composer, and schema-25 proof persistence. It used no provider,
network, production credential, or inbox. The value is an engineering baseline;
it does not establish throughput, tail latency, multi-process capacity, or
retention behavior.
