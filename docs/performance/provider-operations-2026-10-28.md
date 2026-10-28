# Provider Operations performance evidence — 2026-10-28

This bounded local measurement covers service overhead only. It uses a
temporary SQLite database, one synthetic SMTP account, no network activity,
and no real credentials or recipients. It is not a provider-latency or
production-throughput claim.

| Operation | Method | Mean wall time |
| --- | --- | ---: |
| Configuration validation | 100 calls, each recording a SQLite diagnostic | 2.388 ms/call |
| Capability registry lookup | 100 in-process registry reads | 0.006 ms/call |
| OAuth start | 10 local state/PKCE flow starts with SQLite metadata | 0.711 ms/call |

The measurement was run with Python 3.15.2 from the project virtualenv on
2026-10-28. Network handshake, TLS setup, token exchange, IMAP fetch, provider
rate limits, and commercial service latency were intentionally not measured.
