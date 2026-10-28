# Visual workflow automation

Mailforge workflows are local, revisioned, synthetic-only automations. They
connect trusted local events to conditions, waits, and allowlisted actions while
leaving contact eligibility, segmentation, campaign policy, and inbound trust
with their existing services.

## Operator workflow

1. Open **Automations** in the local console and create a draft.
2. Add nodes and connect them with the keyboard-accessible connection editor.
   The intended reading order is: “When this happens… Check whether… Wait
   until… Then do… Otherwise… End.”
   Condition nodes expose separate TRUE and FALSE connections; segment
   conditions pin the selected segment revision at save time.
3. Validate and save the draft. A graph needs one trigger, a reachable End, no
   cycles, explicit condition branches, and bounded node/configuration counts.
4. Publish the draft, then activate the published revision. Editing an active
   workflow creates a new draft; existing runs stay pinned to their revision.
5. Enroll a synthetic contact and inspect the run history. A run may be
   pending, waiting, paused, completed, failed, canceled, or requires review.

No workflow action sends live email. A campaign action only prepares supported
synthetic campaign work through the canonical campaign service, and the local
console labels the entire runtime as simulation mode.

## Supported graph values

Triggers are `manual_enrollment`, `contact_imported`,
`contact_added_to_list`, `scheduled_occurrence`, and
`trusted_inbound_event`. A list trigger carries a selected `list_id`, and a
trusted inbound trigger requires trusted provenance. Nodes support
`CONDITION`, `WAIT`, `ADD_TO_LIST`, `REMOVE_FROM_LIST`, `PREPARE_CAMPAIGN`,
`NOTE`, and `END` in addition to the single `TRIGGER` entry node.

Waits use UTC durable due times. Relative waits are bounded to seven days and
absolute waits require a timezone-aware ISO timestamp. Missed waits are
reconciled once when the local worker advances; there is no claim of a hosted
always-on scheduler.

## API and CLI

The versioned API is under `/api/v1/automations`. It exposes workflow and
revision CRUD, validation, publication, activation/pause/archive, enrollment,
run pagination, run details, controls, and synthetic advancement. It uses the
same loopback session, origin, CSRF, and bounded-input rules as the rest of the
console. Graph validation rejects credential/command-like configuration keys,
requires configured action references at publication, and blocks workflow
self-enrollment.

The CLI delegates to `AutomationService`:

```bash
mailforge automation create --database /tmp/mailforge.sqlite3 --name "Local nurture"
mailforge automation list --database /tmp/mailforge.sqlite3
mailforge automation validate --database /tmp/mailforge.sqlite3 --graph workflow.json
mailforge automation save --database /tmp/mailforge.sqlite3 WORKFLOW_ID --graph workflow.json
mailforge automation publish --database /tmp/mailforge.sqlite3 WORKFLOW_ID
mailforge automation activate --database /tmp/mailforge.sqlite3 WORKFLOW_ID
mailforge automation enroll --database /tmp/mailforge.sqlite3 WORKFLOW_ID --contact-id CONTACT_ID
mailforge automation advance --database /tmp/mailforge.sqlite3 RUN_ID
```

## Recovery and evidence

Migration 8 stores definitions, immutable revisions, source events,
deduplicated enrollments, runs, waits, action correlations, step history, and
operator audit records in the existing SQLite authority. Backup/restore covers
these tables. Worker claims are bounded in-process; a paused or waiting run can
be inspected and advanced after local restart.

This document is local synthetic evidence. It does not qualify a provider,
recipient consent, inbox delivery, hosted deployment, multi-user authorization,
or production scheduler.
