# SMTP provider operations

Provider Operations owns secret-free SMTP account profiles, symbolic credential
references, local diagnostics, OAuth boundary handling, controlled grants, and
loopback qualification evidence. SMTP is the only active provider protocol.

Mailbox synchronization, IMAP/POP3 access, and provider mailbox mutation are
not implemented or registered. Historical provider columns may remain in old
SQLite files solely for preservation and migration.

External provider contact is separately authorized, expiring, profile-bound,
and exact-scope. The normal browser and CLI paths do not perform external SMTP
operations. Local capture fixtures must be loopback-only and explicitly enabled
for qualification.
