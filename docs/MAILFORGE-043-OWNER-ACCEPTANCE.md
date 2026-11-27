# MAILFORGE-043 · Owner acceptance guide

Use an isolated local Mailforge data directory and simulation mode. Do not use
real provider credentials or execute an external SMTP send during acceptance.

## What to inspect

1. Open Home. Confirm that the first screen presents five common jobs—sending,
   list cleaning, contacts, messages, and account setup—without requiring
   knowledge of Mailforge's internal services. Open and return from two task
   cards, then use More tools to confirm advanced destinations remain available.
2. Open Contacts and import a small synthetic file containing one valid,
   duplicate, and malformed address. Preview the consequences, import the
   accepted records, and confirm that the result explains what happened.
3. Open Send. Create a draft from the imported list, prepare it, inspect the
   checks and message preview, confirm the local send, and start the local
   worker. Confirm that the status changes and the Attempts view remain
   available without leaving the workspace.
4. Configure a disposable loopback capture account in Email accounts and
   repeat the Send flow. Confirm that Start local send uses the controlled
   loopback path and that the captured result is visible after a restart.
5. On a prepared campaign using a non-loopback provider profile in an isolated
   fixture, expand Use an external sending account. Review the exact recipient
   scope, set a purpose and expiry, confirm the scope, and prepare the approved
   send. Confirm that Mailforge issues and binds the grant without asking the
   operator to copy an internal authorization reference. Do not execute against
   a real provider.
6. Visit More tools and open Email Standards, Reports & Insights, Delivery
   Events, Data Studio, and Maintenance. Confirm that each page explains its
   decision or next action in plain language and that populated tables, review
   controls, and recovery/status panels remain reachable.
7. Repeat the primary steps at a narrow viewport. Confirm that navigation can
   be opened and closed, the main content remains readable, and focused
   controls remain visible.

## Evidence and limits

The final candidate was checked with the frontend build and lint, the Python
and browser regression suites, the existing arm64 packaging script, the
bundle's own launcher health/stop path, and the packaged loopback campaign
journey. These are synthetic/local checks. They do not establish real-provider
delivery, signed or notarized distribution, production scale, or human
participant usability validation.
