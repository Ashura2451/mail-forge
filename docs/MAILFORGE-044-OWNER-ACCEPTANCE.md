# MAILFORGE-044 · Owner acceptance guide

Use an isolated local database and synthetic address files. Do not use real
credentials or perform an external SMTP operation during acceptance.

1. Open Home. Confirm that the first visible tasks are Sort email, Compare
   lists, and Send, without campaign, CRM, or Data Studio terminology.
2. In Sort email, preview a file containing valid, duplicate, malformed, and
   different-provider addresses. Run sorting, inspect provider groups,
   invalid/duplicate counts, unknown classifications, and download an output.
3. In Compare lists, compare two files using subtraction, intersection, union,
   and duplicate modes. Confirm that neither input is changed and that CSV
   export contains the selected result.
4. Create a small synthetic local send, then compare a later file against the
   selected successful submission history. Confirm that prepared or canceled
   work is not treated as sent.
5. In Send, paste addresses, add plain text and existing HTML, review the
   generated recipient/message records, prepare the exact scope, authorize a
   local simulation, and inspect per-recipient outcomes. Confirm that the UI
   does not require a named campaign, contact database, or saved template.
6. Open Email accounts from More tools. Confirm that sender, SMTP server,
   username, password, port, and encryption have visible labels; save only a
   synthetic or loopback account and confirm the password is never returned.
7. Repeat Home, Sort email, Compare lists, and Send at a narrow viewport.
   Confirm the drawer has labeled controls, focus is visible, tables scroll
   without clipping, and loading/empty/error states remain understandable.
8. For the controlled qualification only, use a fresh loopback TLS SMTP
   fixture and a capture-only provider account. Confirm that Send reaches
   `accepted` through the real SMTP submission path, records the per-recipient
   outcome, survives an application restart, and does not submit a second
   message. No external provider or real recipient is authorized by this guide.
9. In a prepared send, open “Use an external sending account”. Review the
   exact provider, recipient count, expiry, and submission budget. Confirm the
   exact-scope checkbox is required and that the panel does not make an
   external connection by itself.

## Limits

This guide does not authorize external SMTP activity, claim inbox delivery,
establish production capacity, or substitute for human participant usability
research. The implementation's local synthetic checks and browser review must
be reported separately from Business Owner acceptance.
