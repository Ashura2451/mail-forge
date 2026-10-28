# Decision 0008: local web operational acceptance boundary

**Status:** Implemented for the local synthetic browser slice; external
provider qualification and hosted operation remain unapproved.

The local browser application continues to use FastAPI, React/Vite assets,
`CampaignStore`, and the canonical campaign, outbound, inbound, provider, and
eligibility services. Inbound browser acquisition accepts bounded EML bytes,
creates a temporary private file, and delegates parsing and review to the
existing inbound authority. It does not accept arbitrary paths or expose raw
mailbox access to the browser.

The CLI holds an advisory lock adjacent to the configured SQLite database for
the lifetime of `mailforge serve`. This preserves one local runtime owner and
prevents separate local processes from dispatching the same durable work. The
in-process worker remains bounded; browser closure is not a cancellation
signal. Restart recovery uses the existing state machine and transitions stale
`running` work through `pausing` to `paused`.

The acceptance suite uses only disposable databases, synthetic credentials,
loopback TLS SMTP/IMAP fixtures, and a maintained axe audit. No real provider,
recipient, mailbox, or external network operation is part of this decision.
