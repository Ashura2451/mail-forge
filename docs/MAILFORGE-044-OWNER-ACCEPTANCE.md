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

## Limits

This guide does not authorize external SMTP activity, claim inbox delivery,
establish production capacity, or substitute for human participant usability
research. The implementation's local synthetic checks and browser review must
be reported separately from Business Owner acceptance.
