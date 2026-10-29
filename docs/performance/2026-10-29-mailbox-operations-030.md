# Mailbox operations 030 bounded performance evidence

Date: 2026-10-29
Environment: macOS arm64, CPython 3.15.2, local SQLite, synthetic fixture only

The measurement used a temporary database and a synthetic IMAP-shaped source
with 250 RFC 5322 messages. Each message had a stable UID/UIDVALIDITY pair;
no provider, credential, recipient dataset, or remote mailbox was contacted.

| Operation | Observed result |
| --- | ---: |
| Bounded acquisition and parse of 250 messages | 3.4868 s |
| Acquisition throughput | 71.7 messages/s |
| Exact bounded search (`Fixture message 249`) | 1.05 ms |
| Durable mailbox-action request | 2.97 ms |
| SQLite file after workload | 2,105,344 bytes |

Command executed:

```text
PYTHONPATH=src .venv/bin/python <temporary synthetic benchmark script>
```

The benchmark is an executor observation, not a production capacity claim.
The workload retained parsed local evidence and used the current default
processing path; it did not measure network latency, provider throttling,
remote mutation confirmation, or concurrent workers. Future provider
qualification should repeat the same workload against an authorized fixture
with explicit timeout and resource limits. Peak process memory was not
measured in this run and is therefore not claimed.
