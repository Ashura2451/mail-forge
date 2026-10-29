"""CLI for the unified local-first sorting workflows."""

from __future__ import annotations

import argparse
import json
import logging
import time
import uuid
from pathlib import Path

from mailforge.application.automation import AutomationService
from mailforge.application.campaign_operations import CampaignOperationsService
from mailforge.application.campaigns import (
    CampaignService,
    ScriptedTransport,
    run_synthetic_demo,
)
from mailforge.application.data_connections import DataConnectionError, DataConnectionsService
from mailforge.application.data_studio import DataStudioService, detect_schema
from mailforge.application.email_standards import EmailStandardsService
from mailforge.application.inbound import InboundService
from mailforge.application.inbound_demo import run_inbound_demo
from mailforge.application.inbound_operations import InboundOperations
from mailforge.application.inbound_sources import EmlFileSource, MboxSource
from mailforge.application.ingest import collect_unique_addresses, iter_jsonl_email_field
from mailforge.application.integration_hub import IntegrationHubError, IntegrationHubService
from mailforge.application.legacy_migration import rehearse_legacy_migration
from mailforge.application.local_application import (
    DEFAULT_PORT,
    DataDirectory,
    LocalApplication,
    create_backup,
    default_data_directory,
    restore_backup,
    validate_backup,
)
from mailforge.application.mailbox_operations import MailboxOperationsService
from mailforge.application.mailer_compat import (
    load_legacy_configuration,
    migrate_legacy_state,
    prepare_legacy_campaign,
)
from mailforge.application.message_studio import MessageStudioService
from mailforge.application.outbound import OutboundService, run_outbound_demo
from mailforge.application.programmable_operations import ProgrammableOperationsService
from mailforge.application.provider_operations import ProviderOperationsService
from mailforge.application.providers import ProviderService
from mailforge.application.reporting import ReportFilters, ReportingError, ReportingService
from mailforge.application.segmentation import SegmentationService
from mailforge.application.sorting import EmailSorter, SortConfig
from mailforge.application.transform import (
    count_legacy_list,
    format_email_file,
    remove_duplicate_lines,
    write_legacy_list,
)
from mailforge.domain.campaign import AttemptOutcome, SuppressionCategory
from mailforge.domain.outbound import (
    CatchUpPolicy,
    ReconciliationDecision,
    ScheduleMode,
    ScheduleSpec,
)
from mailforge.domain.policy import CommunicationPurpose, EvidenceStatus, PreferenceState
from mailforge.domain.provider import (
    AuthenticationMethod,
    ExternalOperationGrant,
    ProviderKind,
    ProviderOperation,
    ProviderProfile,
    TLSMode,
)
from mailforge.domain.sorting import DNSMode
from mailforge.infrastructure.disposable import DisposableDomainStore
from mailforge.interfaces.process_lock import DatabaseProcessLock, ProcessLockError

DEFAULT_DISPOSABLE_URL = (
    "https://raw.githubusercontent.com/ivolo/disposable-email-domains/master/index.json"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mailforge", description="Local-first email operations")
    subparsers = parser.add_subparsers(dest="command", required=True)
    ingest = subparsers.add_parser("ingest", help="validate and deduplicate a local input file")
    ingest.add_argument("input", type=Path)
    ingest.add_argument("--output", type=Path, help="write a JSON summary")
    ingest.add_argument("--max-bytes", type=int, default=16 * 1024 * 1024)
    sort = subparsers.add_parser("sort", help="stream, classify, and export addresses")
    sort.add_argument("input", type=Path, nargs="+", help="one or more local input files")
    sort.add_argument("--results-dir", type=Path)
    sort.add_argument("--config-dir", type=Path)
    sort.add_argument("--checkpoint", type=Path)
    sort.add_argument("--dns-mode", choices=[mode.value for mode in DNSMode])
    sort.add_argument("--max-bytes", type=int)
    resume_group = sort.add_mutually_exclusive_group()
    resume_group.add_argument("--resume", dest="resume", action="store_true")
    resume_group.add_argument("--fresh", dest="resume", action="store_false")
    sort.set_defaults(resume=None)
    sort.add_argument("--debug", action="store_true")
    sort.add_argument(
        "--profile",
        choices=["full", "validation", "dedupe", "classify", "legacy-export", "report"],
        default=None,
    )
    sort.add_argument("--dry-run", action="store_true", help="preview without legacy output files")
    sort.add_argument("--progress-every", type=int, default=None)
    summary = subparsers.add_parser("summary", help="print the last sorting summary")
    summary.add_argument("--results-dir", type=Path)
    clean = subparsers.add_parser("clean", help="remove a results directory after confirmation")
    clean.add_argument("--results-dir", type=Path)
    clean.add_argument("--yes", action="store_true")
    interactive = subparsers.add_parser("interactive", help="run the legacy interactive workflow")
    interactive.add_argument("--results-dir", type=Path)
    interactive.add_argument("--config-dir", type=Path)
    interactive.add_argument("--checkpoint", type=Path)
    extract = subparsers.add_parser("extract-jsonl", help="extract the legacy JSONL email field")
    extract.add_argument("input", type=Path)
    extract.add_argument("output", type=Path)
    extract.add_argument("--max-bytes", type=int, default=16 * 1024 * 1024)
    refresh = subparsers.add_parser("refresh-disposable", help="refresh disposable-domain data")
    refresh.add_argument("--path", type=Path, default=Path("config/disposable.json"))
    refresh.add_argument("--url", default=DEFAULT_DISPOSABLE_URL)
    transform = subparsers.add_parser("transform", help="run a legacy file transformation")
    transform.add_argument("operation", choices=["format", "dedupe", "list", "count"])
    transform.add_argument("input", type=Path)
    transform.add_argument("output", type=Path, nargs="?")
    field = subparsers.add_parser("field", help="manage typed contact fields")
    field_commands = field.add_subparsers(dest="field_command", required=True)
    field_list = field_commands.add_parser("list")
    field_list.add_argument("--database", type=Path, required=True)
    field_create = field_commands.add_parser("create")
    field_create.add_argument("--database", type=Path, required=True)
    field_create.add_argument("--machine-key", required=True)
    field_create.add_argument("--label", required=True)
    field_create.add_argument("--type", dest="field_type", required=True)
    field_create.add_argument("--controlled-value", action="append", default=[])
    field_archive = field_commands.add_parser("archive")
    field_archive.add_argument("--database", type=Path, required=True)
    field_archive.add_argument("field_id")
    field_update = field_commands.add_parser("update")
    field_update.add_argument("--database", type=Path, required=True)
    field_update.add_argument("field_id")
    field_update.add_argument("--label")
    field_update.add_argument("--type", dest="field_type")
    segment = subparsers.add_parser("segment", help="manage saved dynamic audiences")
    segment_commands = segment.add_subparsers(dest="segment_command", required=True)
    segment_list = segment_commands.add_parser("list")
    segment_list.add_argument("--database", type=Path, required=True)
    segment_create = segment_commands.add_parser("create")
    segment_create.add_argument("--database", type=Path, required=True)
    segment_create.add_argument("--name", required=True)
    segment_create.add_argument("--definition", type=Path, required=True)
    segment_create.add_argument("--description", default="")
    segment_preview = segment_commands.add_parser("preview")
    segment_preview.add_argument("--database", type=Path, required=True)
    segment_preview.add_argument("segment_id")
    segment_preview.add_argument("--revision-id")
    segment_preview.add_argument("--limit", type=int, default=100)
    segment_preview.add_argument("--offset", type=int, default=0)
    segment_edit = segment_commands.add_parser("edit")
    segment_edit.add_argument("--database", type=Path, required=True)
    segment_edit.add_argument("segment_id")
    segment_edit.add_argument("--definition", type=Path, required=True)
    segment_edit.add_argument("--expected-revision-id", required=True)
    segment_edit.add_argument("--description")
    segment_archive = segment_commands.add_parser("archive")
    segment_archive.add_argument("--database", type=Path, required=True)
    segment_archive.add_argument("segment_id")
    segment_import = segment_commands.add_parser("import", help="import typed audience rows")
    segment_import.add_argument("--database", type=Path, required=True)
    segment_import.add_argument("--list-id", required=True)
    segment_import.add_argument("--rows", type=Path, required=True, help="JSON array of records")
    segment_import.add_argument(
        "--mappings", type=Path, required=True, help="JSON source-to-field map"
    )
    segment_import.add_argument("--source-ref", default="cli-structured-import")
    segment_import.add_argument("--overwrite", action="store_true")
    segment_membership = segment_commands.add_parser(
        "membership", help="change static list membership"
    )
    segment_membership.add_argument("--database", type=Path, required=True)
    segment_membership.add_argument("--list-id", required=True)
    segment_membership.add_argument("--contact-id", required=True)
    segment_membership.add_argument("--action", choices=["add", "remove"], required=True)
    message = subparsers.add_parser(
        "message", help="inspect and render local Message Studio content"
    )
    message_commands = message.add_subparsers(dest="message_command", required=True)
    message_list = message_commands.add_parser("list")
    message_list.add_argument("--database", type=Path, required=True)
    message_validate = message_commands.add_parser("validate")
    message_validate.add_argument("--database", type=Path, required=True)
    message_validate.add_argument("--document", type=Path, required=True)
    message_render = message_commands.add_parser("render")
    message_render.add_argument("--database", type=Path, required=True)
    message_render.add_argument("--revision-id", required=True)
    message_render.add_argument("--contact-id")
    message_render.add_argument("--condition-mode", choices=["true", "false"])
    campaign = subparsers.add_parser("campaign", help="manage durable local campaign state")
    campaign_commands = campaign.add_subparsers(dest="campaign_command", required=True)
    demo = campaign_commands.add_parser("demo", help="run the complete synthetic campaign workflow")
    demo.add_argument("--database", type=Path, default=Path("state/mailforge-demo.sqlite3"))
    outbound_demo = campaign_commands.add_parser(
        "outbound-demo", help="run synthetic composition, policy, schedule, and dispatch"
    )
    outbound_demo.add_argument(
        "--database", type=Path, default=Path("state/mailforge-outbound.sqlite3")
    )
    import_sort = campaign_commands.add_parser(
        "import-sort", help="import structured sorting results"
    )
    import_sort.add_argument("--database", type=Path, required=True)
    import_sort.add_argument("--processing", type=Path, required=True)
    import_sort.add_argument("--list-name", required=True)
    import_sort.add_argument("--description", default="")
    create = campaign_commands.add_parser("create", help="create a durable campaign")
    create.add_argument("--database", type=Path, required=True)
    create.add_argument("--name", required=True)
    audience_group = create.add_mutually_exclusive_group(required=True)
    audience_group.add_argument("--list-id")
    audience_group.add_argument("--segment-id")
    create.add_argument("--segment-revision-id")
    create.add_argument("--template", default="synthetic")
    create.add_argument("--sender", default="synthetic-sender")
    create.add_argument(
        "--purpose",
        default=CommunicationPurpose.MARKETING.value,
        help="communication purpose used by recipient policy",
    )
    prepare = campaign_commands.add_parser(
        "prepare", help="prepare and explain campaign eligibility"
    )
    prepare.add_argument("--database", type=Path, required=True)
    prepare.add_argument("campaign_id")
    prepare.add_argument("--run-key", default="primary")
    workspace = campaign_commands.add_parser(
        "workspace", help="show the campaign operations workspace"
    )
    workspace.add_argument("--database", type=Path, required=True)
    workspace.add_argument("campaign_id")
    workspace.add_argument("--preview-limit", type=int, default=10)
    update = campaign_commands.add_parser("update", help="optimistically update a draft campaign")
    update.add_argument("--database", type=Path, required=True)
    update.add_argument("campaign_id")
    update.add_argument("--expected-updated-at")
    update.add_argument("--name", required=True)
    update.add_argument("--list-id")
    update.add_argument("--segment-id")
    update.add_argument("--segment-revision-id")
    update.add_argument("--sender-email", default="sender@example.com")
    update.add_argument("--subject", default="Synthetic Mailforge campaign")
    update.add_argument("--text", default="Hello {{ email }}")
    update.add_argument("--message-revision-id")
    update.add_argument("--provider-account-ref")
    update.add_argument("--description", default="")
    preflight = campaign_commands.add_parser("preflight", help="run read-only campaign preflight")
    preflight.add_argument("--database", type=Path, required=True)
    preflight.add_argument("campaign_id")
    schedule = campaign_commands.add_parser(
        "schedule", help="save a timezone-aware campaign schedule"
    )
    schedule.add_argument("--database", type=Path, required=True)
    schedule.add_argument("campaign_id")
    schedule.add_argument(
        "--mode", choices=[item.value for item in ScheduleMode], default="immediate"
    )
    schedule.add_argument("--timezone", default="UTC")
    schedule.add_argument("--scheduled-at")
    schedule.add_argument("--window-start")
    schedule.add_argument("--window-end")
    schedule.add_argument("--pacing-seconds", type=float, default=0.0)
    schedule.add_argument("--cooldown-seconds", type=float, default=0.0)
    schedule.add_argument("--recurrence", choices=["daily", "weekly"])
    schedule.add_argument(
        "--catch-up-policy", choices=[item.value for item in CatchUpPolicy], default="skip"
    )
    schedule.add_argument("--fold", type=int, choices=[0, 1])
    for action in ("pause", "resume", "cancel", "recipients", "attempts"):
        action_parser = campaign_commands.add_parser(action, help=f"{action} or inspect a campaign")
        action_parser.add_argument("--database", type=Path, required=True)
        action_parser.add_argument("campaign_id")
        if action in {"recipients", "attempts"}:
            action_parser.add_argument("--limit", type=int, default=100)
            action_parser.add_argument("--offset", type=int, default=0)
    campaign_export = campaign_commands.add_parser(
        "export", help="export bounded campaign evidence"
    )
    campaign_export.add_argument("--database", type=Path, required=True)
    campaign_export.add_argument("campaign_id")
    campaign_export.add_argument("--format", choices=["json", "csv"], default="json")
    preview = campaign_commands.add_parser("preview", help="show prepared eligibility")
    preview.add_argument("--database", type=Path, required=True)
    preview.add_argument("campaign_id")
    audience = campaign_commands.add_parser("audience", help="show a durable audience snapshot")
    audience.add_argument("--database", type=Path, required=True)
    audience.add_argument("campaign_id")
    audience.add_argument("--run-key", default="primary")
    authorize = campaign_commands.add_parser(
        "authorize-simulation", help="authorize fake transport only"
    )
    authorize.add_argument("--database", type=Path, required=True)
    authorize.add_argument("campaign_id")
    simulate = campaign_commands.add_parser(
        "simulate", help="execute with a deterministic fake transport"
    )
    simulate.add_argument("--database", type=Path, required=True)
    simulate.add_argument("campaign_id")
    simulate.add_argument(
        "--outcome",
        action="append",
        default=[],
        help="email=accepted|transient_failure|permanent_failure|ambiguous",
    )
    simulate.add_argument("--retry-deferred", action="store_true")
    simulate.add_argument("--stop-after", type=int)
    report = campaign_commands.add_parser("report", help="show durable campaign report")
    report.add_argument("--database", type=Path, required=True)
    report.add_argument("campaign_id")
    suppress = campaign_commands.add_parser("suppress", help="add an explicit suppression")
    suppress.add_argument("--database", type=Path, required=True)
    suppress.add_argument("--email", required=True)
    suppress.add_argument(
        "--category", choices=[item.value for item in SuppressionCategory], required=True
    )
    suppress.add_argument("--reason", required=True)
    suppress.add_argument("--purpose")
    suppress.add_argument("--actor", default="local-operator")
    evidence = campaign_commands.add_parser(
        "record-evidence", help="record available authorization evidence without inventing consent"
    )
    evidence.add_argument("--database", type=Path, required=True)
    evidence.add_argument("--email", required=True)
    evidence.add_argument("--purpose", default=CommunicationPurpose.MARKETING.value)
    evidence.add_argument(
        "--status", choices=[item.value for item in EvidenceStatus], default="established"
    )
    evidence.add_argument("--source", required=True)
    evidence.add_argument("--evidence-ref")
    evidence.add_argument("--actor", default="local-operator")
    preference = campaign_commands.add_parser("preference", help="record a recipient preference")
    preference.add_argument("--database", type=Path, required=True)
    preference.add_argument("--email", required=True)
    preference.add_argument("--purpose", default=CommunicationPurpose.MARKETING.value)
    preference.add_argument(
        "--state",
        choices=[item.value for item in PreferenceState if item is not PreferenceState.UNKNOWN],
        required=True,
    )
    preference.add_argument("--evidence-ref")
    preference.add_argument("--actor", default="local-operator")
    eligibility = campaign_commands.add_parser(
        "eligibility", help="explain current campaign eligibility"
    )
    eligibility.add_argument("--database", type=Path, required=True)
    eligibility.add_argument("campaign_id")
    declarations = campaign_commands.add_parser("declarations", help="show operator declarations")
    declarations.add_argument("--database", type=Path, required=True)
    declarations.add_argument("--campaign-id")
    suppressions = campaign_commands.add_parser("suppressions", help="show known exclusions")
    suppressions.add_argument("--database", type=Path, required=True)
    suppressions.add_argument("--email")
    export = campaign_commands.add_parser("export-contacts", help="export contact records as JSON")
    export.add_argument("--database", type=Path, required=True)
    export.add_argument("--list-id")
    deactivate = campaign_commands.add_parser(
        "deactivate-contact", help="deactivate a contact without deleting history"
    )
    deactivate.add_argument("--database", type=Path, required=True)
    deactivate.add_argument("--email", required=True)
    deactivate.add_argument("--reason", required=True)
    deactivate.add_argument("--actor", default="local-operator")
    deletion = campaign_commands.add_parser(
        "plan-deletion", help="plan deletion and show retention conflicts"
    )
    deletion.add_argument("--database", type=Path, required=True)
    deletion.add_argument("--email", required=True)
    deletion.add_argument("--reason", required=True)
    deletion.add_argument("--actor", default="local-operator")
    backup = campaign_commands.add_parser("backup", help="backup a campaign database")
    backup.add_argument("--database", type=Path, required=True)
    backup.add_argument("--destination", type=Path, required=True)
    restore = campaign_commands.add_parser("restore", help="restore a campaign database")
    restore.add_argument("--source", type=Path, required=True)
    restore.add_argument("--destination", type=Path, required=True)
    rehearse = campaign_commands.add_parser(
        "rehearse-migration", help="rehearse legacy state mapping with explicit files"
    )
    rehearse.add_argument("--database", type=Path, required=True)
    rehearse.add_argument("--list-id", required=True)
    rehearse.add_argument("--campaign-id", required=True)
    rehearse.add_argument("--sent", type=Path, required=True)
    rehearse.add_argument("--failed", type=Path, required=True)
    rehearse.add_argument("--hard-bounces", type=Path, required=True)
    translate = campaign_commands.add_parser(
        "translate-legacy", help="translate legacy MAILER configuration without sending"
    )
    translate.add_argument("--config", type=Path, required=True)
    translate.add_argument("--output", type=Path)
    import_legacy = campaign_commands.add_parser(
        "import-legacy", help="import legacy targets and prepare a canonical campaign"
    )
    import_legacy.add_argument("--database", type=Path, required=True)
    import_legacy.add_argument("--config", type=Path, required=True)
    import_legacy.add_argument("--campaign", required=True)
    import_legacy.add_argument("--sender-email")
    state = campaign_commands.add_parser(
        "rehearse-legacy-state", help="import legacy state with idempotent dispositions"
    )
    state.add_argument("--database", type=Path, required=True)
    state.add_argument("--list-id", required=True)
    state.add_argument("--campaign-id", required=True)
    state.add_argument("--sent", type=Path, required=True)
    state.add_argument("--failed", type=Path, required=True)
    state.add_argument("--hard-bounces", type=Path, required=True)
    state.add_argument("--events", type=Path)
    state.add_argument("--log", type=Path)
    reconcile = campaign_commands.add_parser(
        "reconcile", help="record an explicit decision for an ambiguous attempt"
    )
    reconcile.add_argument("--database", type=Path, required=True)
    reconcile.add_argument("--attempt-id", required=True)
    reconcile.add_argument(
        "--decision", choices=[item.value for item in ReconciliationDecision], required=True
    )
    inbound = subparsers.add_parser("inbound", help="inspect and operate on local inbound evidence")
    inbound_commands = inbound.add_subparsers(dest="inbound_command", required=True)
    inbound_demo = inbound_commands.add_parser(
        "synthetic-demo", help="run deterministic inbound acceptance"
    )
    inbound_demo.add_argument(
        "--database", type=Path, default=Path("state/mailforge-inbound.sqlite3")
    )
    import_eml = inbound_commands.add_parser("import-eml", help="import one local RFC 5322 message")
    import_eml.add_argument("--database", type=Path, required=True)
    import_eml.add_argument("--source-ref", required=True)
    import_eml.add_argument("--path", type=Path, required=True)
    import_eml.add_argument("--retain-raw", action="store_true")
    import_mbox = inbound_commands.add_parser("import-mbox", help="import a local MBOX read-only")
    import_mbox.add_argument("--database", type=Path, required=True)
    import_mbox.add_argument("--source-ref", required=True)
    import_mbox.add_argument("--path", type=Path, required=True)
    inspect = inbound_commands.add_parser("inspect", help="inspect acquired inbound metadata")
    inspect.add_argument("--database", type=Path, required=True)
    inspect.add_argument("--message-id")
    inspect.add_argument("--limit", type=int, default=100)
    review = inbound_commands.add_parser("review", help="show pending inbound decisions")
    review.add_argument("--database", type=Path, required=True)
    decide = inbound_commands.add_parser("decide", help="approve or reject a reviewed action")
    decide.add_argument("--database", type=Path, required=True)
    decide.add_argument("--action-id", required=True)
    decide.add_argument("--decision", choices=["approve", "reject"], required=True)
    decide.add_argument("--reason", required=True)
    replay = inbound_commands.add_parser("replay", help="reprocess retained evidence")
    replay.add_argument("--database", type=Path, required=True)
    replay.add_argument("--source-ref", required=True)
    stats = inbound_commands.add_parser("stats", help="show inbound operation statistics")
    stats.add_argument("--database", type=Path, required=True)
    search = inbound_commands.add_parser("search", help="search durable inbound metadata")
    search.add_argument("--database", type=Path, required=True)
    search.add_argument("--query")
    search.add_argument("--sender")
    search.add_argument("--subject")
    search.add_argument("--classification")
    search.add_argument("--source-ref")
    search.add_argument("--account-ref")
    search.add_argument("--folder")
    search.add_argument("--recipient")
    search.add_argument("--read", choices=["true", "false"])
    search.add_argument("--flagged", choices=["true", "false"])
    search.add_argument("--has-attachment", choices=["true", "false"])
    search.add_argument("--thread-key")
    search.add_argument("--received-after")
    search.add_argument("--received-before")
    search.add_argument("--processed", choices=["true", "false"])
    search.add_argument("--limit", type=int, default=100)
    search.add_argument("--offset", type=int, default=0)
    extract_values = inbound_commands.add_parser("extract", help="extract safe message metadata")
    extract_values.add_argument("--database", type=Path, required=True)
    extract_values.add_argument("--message-id", required=True)
    extract_values.add_argument("--fields", nargs="*")
    export_values = inbound_commands.add_parser("export", help="export extracted values")
    export_values.add_argument("--database", type=Path, required=True)
    export_values.add_argument("--message-id")
    export_values.add_argument("--format", choices=["json", "csv"], default="json")
    rule = inbound_commands.add_parser("rule", help="manage bounded inbound rules")
    rule.add_argument("--database", type=Path, required=True)
    rule.add_argument("rule_command", choices=["list", "create", "activate", "pause"])
    rule.add_argument("--rule-id")
    rule.add_argument("--name")
    rule.add_argument("--definition", type=Path)
    draft = inbound_commands.add_parser("draft", help="prepare a reply or forward draft")
    draft.add_argument("--database", type=Path, required=True)
    draft.add_argument("--message-id", required=True)
    draft.add_argument("--kind", choices=["reply", "reply_all", "forward"], required=True)
    draft.add_argument("--body", default="")
    mailbox = inbound_commands.add_parser(
        "mailbox", help="browse and operate a loopback IMAP fixture"
    )
    mailbox_commands = mailbox.add_subparsers(dest="mailbox_command", required=True)
    mailbox_list = mailbox_commands.add_parser("list")
    mailbox_list.add_argument("--database", type=Path, required=True)
    mailbox_list.add_argument("--account", required=True)
    mailbox_discover = mailbox_commands.add_parser("discover")
    mailbox_discover.add_argument("--database", type=Path, required=True)
    mailbox_discover.add_argument("--account", required=True)
    mailbox_discover.add_argument("--authorization-ref", required=True)
    mailbox_discover.add_argument("--purpose", required=True)
    mailbox_discover.add_argument("--expires-at", required=True)
    mailbox_sync = mailbox_commands.add_parser("sync")
    mailbox_sync.add_argument("--database", type=Path, required=True)
    mailbox_sync.add_argument("--account", required=True)
    mailbox_sync.add_argument("--mailbox", default="INBOX")
    mailbox_sync.add_argument("--batch-limit", type=int, default=100)
    mailbox_sync.add_argument("--authorization-ref", required=True)
    mailbox_sync.add_argument("--purpose", required=True)
    mailbox_sync.add_argument("--expires-at", required=True)
    mailbox_action = mailbox_commands.add_parser("action")
    mailbox_action.add_argument("--database", type=Path, required=True)
    mailbox_action.add_argument("--message-id", required=True)
    mailbox_action.add_argument("--action", required=True)
    mailbox_action.add_argument("--uidvalidity", required=True)
    mailbox_action.add_argument("--uid", required=True)
    mailbox_action.add_argument("--idempotency-key", required=True)
    mailbox_action.add_argument("--authorization-ref", required=True)
    mailbox_action.add_argument("--purpose", required=True)
    mailbox_action.add_argument("--expires-at", required=True)
    mailbox_action.add_argument("--target-folder")
    mailbox_action.add_argument("--flag", action="append", default=[])
    mailbox_reconcile = mailbox_commands.add_parser("reconcile")
    mailbox_reconcile.add_argument("--database", type=Path, required=True)
    mailbox_reconcile.add_argument("--action-id", required=True)
    reports = subparsers.add_parser(
        "reports", help="inspect local operational intelligence and export bounded reports"
    )
    report_commands = reports.add_subparsers(dest="reports_command", required=True)

    def add_report_filters(command: argparse.ArgumentParser) -> None:
        command.add_argument("--database", type=Path, required=True)
        command.add_argument(
            "--preset",
            choices=["today", "yesterday", "last_7_days", "last_30_days", "custom"],
            default="last_7_days",
        )
        command.add_argument("--start")
        command.add_argument("--end")
        command.add_argument("--timezone", default="UTC")
        command.add_argument("--provider-ref")
        command.add_argument("--campaign-id")
        command.add_argument("--workflow-id")
        command.add_argument("--source-ref")
        command.add_argument("--account-ref")
        command.add_argument("--folder")
        command.add_argument("--job-kind", choices=["data_studio", "list_tools"])
        command.add_argument("--limit", type=int, default=50)
        command.add_argument("--offset", type=int, default=0)

    report_summary = report_commands.add_parser("summary", help="print the operational summary")
    add_report_filters(report_summary)
    report_export = report_commands.add_parser("export", help="write a bounded report export")
    add_report_filters(report_export)
    report_export.add_argument(
        "--report-type",
        choices=["summary", "campaigns", "providers", "workflows", "inbound", "jobs"],
        default="summary",
    )
    report_export.add_argument("--format", choices=["json", "csv", "html"], default="json")
    report_export.add_argument("--output", type=Path, required=True)
    provider = subparsers.add_parser(
        "provider", help="inspect safe provider profiles and diagnostics"
    )
    provider_commands = provider.add_subparsers(dest="provider_command", required=True)
    provider_list = provider_commands.add_parser("list", help="list secret-free provider profiles")
    provider_list.add_argument("--database", type=Path, required=True)
    provider_list.add_argument("--kind", choices=[item.value for item in ProviderKind])
    provider_register = provider_commands.add_parser(
        "register", help="register a secret-free SMTP or IMAP provider profile"
    )
    provider_register.add_argument("--database", type=Path, required=True)
    provider_register.add_argument("--ref", required=True)
    provider_register.add_argument(
        "--kind", choices=[item.value for item in ProviderKind], required=True
    )
    provider_register.add_argument("--host", required=True)
    provider_register.add_argument("--port", type=int, required=True)
    provider_register.add_argument("--username", required=True)
    provider_register.add_argument("--credential-ref", required=True)
    provider_register.add_argument(
        "--tls-mode", choices=[item.value for item in TLSMode], required=True
    )
    provider_register.add_argument(
        "--auth-method", choices=[item.value for item in AuthenticationMethod], default="password"
    )
    provider_register.add_argument("--mailbox", default="INBOX")
    provider_register.add_argument("--sender-policy")
    provider_register.add_argument("--timeout", type=float, default=30.0)
    provider_register.add_argument("--max-message-bytes", type=int, default=10 * 1024 * 1024)
    provider_register.add_argument("--ca-file", type=str)
    provider_validate = provider_commands.add_parser(
        "validate", help="validate without network access"
    )
    provider_validate.add_argument("--database", type=Path, required=True)
    provider_validate.add_argument("--ref", required=True)
    provider_credentials = provider_commands.add_parser(
        "credentials", help="show credential-reference readiness without exposing values"
    )
    provider_credentials.add_argument("--database", type=Path, required=True)
    provider_credentials.add_argument("--ref", required=True)
    provider_local = provider_commands.add_parser(
        "local-diagnostic", help="run a protocol diagnostic against a loopback fixture only"
    )
    provider_local.add_argument("--database", type=Path, required=True)
    provider_local.add_argument("--ref", required=True)
    provider_diagnostics = provider_commands.add_parser(
        "diagnostics", help="show provider diagnostics"
    )
    provider_diagnostics.add_argument("--database", type=Path, required=True)
    provider_diagnostics.add_argument("--ref")
    provider_accounts = provider_commands.add_parser(
        "accounts", help="list named provider accounts"
    )
    provider_accounts.add_argument("--database", type=Path, required=True)
    provider_capabilities = provider_commands.add_parser(
        "capabilities", help="show provider capability presets"
    )
    provider_capabilities.add_argument("--database", type=Path, required=True)
    provider_suspend = provider_commands.add_parser("suspend", help="suspend a provider account")
    provider_suspend.add_argument("--database", type=Path, required=True)
    provider_suspend.add_argument("--ref", required=True)
    provider_diagnostic = provider_commands.add_parser(
        "diagnostic", help="run an explicit provider diagnostic"
    )
    provider_diagnostic.add_argument("--database", type=Path, required=True)
    provider_diagnostic.add_argument("--ref", required=True)
    provider_diagnostic.add_argument(
        "--operation", choices=[item.value for item in ProviderOperation], required=True
    )
    provider_diagnostic.add_argument("--authorization-ref")
    provider_diagnostic.add_argument("--purpose")
    provider_diagnostic.add_argument("--expires-at")
    provider_test_send = provider_commands.add_parser(
        "test-send", help="submit one explicitly authorized test message"
    )
    provider_test_send.add_argument("--database", type=Path, required=True)
    provider_test_send.add_argument("--ref", required=True)
    provider_test_send.add_argument("--sender", required=True)
    provider_test_send.add_argument("--recipient", required=True)
    provider_test_send.add_argument("--message", type=Path, required=True)
    provider_test_send.add_argument("--idempotency-key", required=True)
    provider_test_send.add_argument("--authorization-ref", required=True)
    provider_test_send.add_argument("--purpose", required=True)
    provider_test_send.add_argument("--expires-at", required=True)
    automation = subparsers.add_parser("automation", help="manage durable visual workflows")
    automation_commands = automation.add_subparsers(dest="automation_command", required=True)
    automation_list = automation_commands.add_parser("list")
    automation_list.add_argument("--database", type=Path, required=True)
    automation_create = automation_commands.add_parser("create")
    automation_create.add_argument("--database", type=Path, required=True)
    automation_create.add_argument("--name", required=True)
    automation_create.add_argument("--description", default="")
    automation_save = automation_commands.add_parser("save")
    automation_save.add_argument("--database", type=Path, required=True)
    automation_save.add_argument("workflow_id")
    automation_save.add_argument("--graph", type=Path, required=True)
    automation_save.add_argument("--revision-id")
    automation_validate = automation_commands.add_parser("validate")
    automation_validate.add_argument("--database", type=Path, required=True)
    automation_validate.add_argument("--graph", type=Path, required=True)
    automation_publish = automation_commands.add_parser("publish")
    automation_publish.add_argument("--database", type=Path, required=True)
    automation_publish.add_argument("workflow_id")
    automation_publish.add_argument("--revision-id")
    automation_activate = automation_commands.add_parser("activate")
    automation_activate.add_argument("--database", type=Path, required=True)
    automation_activate.add_argument("workflow_id")
    automation_activate.add_argument("--revision-id")
    automation_enroll = automation_commands.add_parser("enroll")
    automation_enroll.add_argument("--database", type=Path, required=True)
    automation_enroll.add_argument("workflow_id")
    automation_enroll.add_argument("--contact-id", required=True)
    automation_advance = automation_commands.add_parser("advance")
    automation_advance.add_argument("--database", type=Path, required=True)
    automation_advance.add_argument("run_id")
    functions = subparsers.add_parser("functions", help="manage reusable programmable functions")
    function_commands = functions.add_subparsers(dest="function_command", required=True)
    function_list = function_commands.add_parser("list")
    function_list.add_argument("--database", type=Path, required=True)
    function_create = function_commands.add_parser("create")
    function_create.add_argument("--database", type=Path, required=True)
    function_create.add_argument("--name", required=True)
    function_create.add_argument("--version", default="1.0")
    function_create.add_argument("--definition", type=Path, required=True)
    function_preview = function_commands.add_parser("preview")
    function_preview.add_argument("--database", type=Path, required=True)
    function_preview.add_argument("name")
    function_preview.add_argument("--records", type=Path, required=True)
    function_preview.add_argument("--revision-id")
    function_preview.add_argument("--parameters", default="{}")
    function_revisions = function_commands.add_parser("revisions")
    function_revisions.add_argument("--database", type=Path, required=True)
    function_revisions.add_argument("name")
    function_history = function_commands.add_parser("history")
    function_history.add_argument("--database", type=Path, required=True)
    function_history.add_argument("--name")
    function_history.add_argument("--limit", type=int, default=100)
    for action in ("enable", "disable"):
        function_action = function_commands.add_parser(action)
        function_action.add_argument("--database", type=Path, required=True)
        function_action.add_argument("name")
    function_install = function_commands.add_parser(
        "install-python", help="privileged local extension registration"
    )
    function_install.add_argument("--database", type=Path, required=True)
    function_install.add_argument("--source", type=Path, required=True)
    function_install.add_argument("--manifest", type=Path, required=True)
    function_install.add_argument(
        "--confirm-checksum", required=True, help="type the source SHA-256 to confirm local trust"
    )
    standards = subparsers.add_parser(
        "email-standards",
        help="inspect sender identity, email standards, and unsubscribe readiness",
    )
    standards_commands = standards.add_subparsers(dest="standards_command", required=True)
    standards_list = standards_commands.add_parser("identities")
    standards_list.add_argument("--database", type=Path, required=True)
    standards_register = standards_commands.add_parser("register-identity")
    standards_register.add_argument("--database", type=Path, required=True)
    standards_register.add_argument("--ref", required=True)
    standards_register.add_argument("--from", dest="visible_from", required=True)
    standards_register.add_argument("--reply-to")
    standards_register.add_argument("--envelope-from")
    standards_register.add_argument("--dkim-domain")
    standards_register.add_argument("--dkim-selector")
    standards_register.add_argument("--provider-account")
    standards_register.add_argument("--provider-signing-status", default="unknown")
    standards_domain = standards_commands.add_parser("domain")
    standards_domain.add_argument("--database", type=Path, required=True)
    standards_domain.add_argument("domain")
    standards_domain.add_argument("--sender-ip")
    standards_domain.add_argument("--envelope-from")
    standards_domain.add_argument("--dkim-selector")
    standards_domain.add_argument("--network", action="store_true")
    standards_message = standards_commands.add_parser("message")
    standards_message.add_argument("--database", type=Path, required=True)
    standards_message.add_argument("--path", type=Path, required=True)
    standards_message.add_argument("--purpose", default="transactional")
    standards_message.add_argument("--identity")
    standards_history = standards_commands.add_parser("history")
    standards_history.add_argument("--database", type=Path, required=True)
    standards_history.add_argument("--kind")
    standards_history.add_argument("--limit", type=int, default=100)
    standards_guidance = standards_commands.add_parser("guidance")
    standards_guidance.add_argument("--database", type=Path, required=True)
    standards_import = standards_commands.add_parser("dmarc-import")
    standards_import.add_argument("--database", type=Path, required=True)
    standards_import.add_argument("--path", type=Path, required=True)
    standards_summary = standards_commands.add_parser("dmarc-summary")
    standards_summary.add_argument("--database", type=Path, required=True)
    data_studio = subparsers.add_parser("data-studio", help="process structured local data")
    data_commands = data_studio.add_subparsers(dest="data_command", required=True)
    data_schema = data_commands.add_parser("schema", help="inspect source fields")
    data_schema.add_argument("input", type=Path)
    data_schema.add_argument("--sample-limit", type=int, default=100)
    data_create = data_commands.add_parser("create-recipe", help="create a processing recipe")
    data_create.add_argument("--database", type=Path, required=True)
    data_create.add_argument("--name", required=True)
    data_create.add_argument("--description", default="")
    data_create.add_argument("--definition", type=Path, required=True)
    data_list = data_commands.add_parser("list-recipes", help="list saved recipes")
    data_list.add_argument("--database", type=Path, required=True)
    data_preview = data_commands.add_parser("preview", help="preview a recipe")
    data_preview.add_argument("--database", type=Path, required=True)
    data_preview.add_argument("--input", type=Path, required=True)
    data_preview.add_argument("--source", type=Path, action="append", default=[])
    data_preview.add_argument("--definition", type=Path, required=True)
    data_preview.add_argument("--limit", type=int, default=50)
    data_run = data_commands.add_parser("run", help="run a pinned recipe")
    data_run.add_argument("--database", type=Path, required=True)
    data_run.add_argument("--input", type=Path, required=True)
    data_run.add_argument("--source", type=Path, action="append", default=[])
    data_run.add_argument("--filename", default="data.csv")
    data_run.add_argument("--revision-id", required=True)
    connections = subparsers.add_parser(
        "data-connections", help="configure and snapshot read-only external data sources"
    )
    connection_commands = connections.add_subparsers(dest="connection_command", required=True)
    connection_list = connection_commands.add_parser("list")
    connection_list.add_argument("--database", type=Path, required=True)
    connection_create = connection_commands.add_parser("create")
    connection_create.add_argument("--database", type=Path, required=True)
    connection_create.add_argument("--name", required=True)
    connection_create.add_argument(
        "--type", dest="source_type", choices=["sqlite", "postgresql", "xlsx"], required=True
    )
    connection_create.add_argument("--config", default="{}", help="JSON connection configuration")
    connection_create.add_argument("--credential-ref")
    connection_create.add_argument("--source", type=Path)
    connection_create.add_argument("--selection", default="{}")
    connection_test = connection_commands.add_parser("test")
    connection_test.add_argument("--database", type=Path, required=True)
    connection_test.add_argument("profile_id")
    connection_schema = connection_commands.add_parser("schema")
    connection_schema.add_argument("--database", type=Path, required=True)
    connection_schema.add_argument("profile_id")
    connection_preview = connection_commands.add_parser("preview")
    connection_preview.add_argument("--database", type=Path, required=True)
    connection_preview.add_argument("profile_id")
    connection_preview.add_argument("--selection", default="{}")
    connection_preview.add_argument("--limit", type=int, default=50)
    connection_snapshot = connection_commands.add_parser("snapshot")
    connection_snapshot.add_argument("--database", type=Path, required=True)
    connection_snapshot.add_argument("profile_id")
    connection_snapshot.add_argument("--selection", default="{}")
    connection_snapshot.add_argument("--idempotency-key")
    connection_snapshot.add_argument("--wait", action="store_true")
    connection_snapshots = connection_commands.add_parser("list-snapshots")
    connection_snapshots.add_argument("--database", type=Path, required=True)
    connection_snapshots.add_argument("profile_id")
    connection_cancel = connection_commands.add_parser("cancel")
    connection_cancel.add_argument("--database", type=Path, required=True)
    connection_cancel.add_argument("snapshot_id")
    connection_handoff = connection_commands.add_parser("handoff")
    connection_handoff.add_argument("--database", type=Path, required=True)
    connection_handoff.add_argument("snapshot_id")
    connection_handoff.add_argument("--recipe-revision-id", required=True)
    integrations = subparsers.add_parser(
        "integrations", help="manage local integration clients, events, and webhooks"
    )
    integration_commands = integrations.add_subparsers(dest="integration_command", required=True)
    integration_list = integration_commands.add_parser("list")
    integration_list.add_argument("--database", type=Path, required=True)
    integration_issue = integration_commands.add_parser("issue-client")
    integration_issue.add_argument("--database", type=Path, required=True)
    integration_issue.add_argument("--name", required=True)
    integration_issue.add_argument("--scope", action="append", required=True)
    integration_issue.add_argument("--expires-at", required=True)
    integration_rotate = integration_commands.add_parser("rotate-client")
    integration_rotate.add_argument("--database", type=Path, required=True)
    integration_rotate.add_argument("client_ref")
    integration_rotate.add_argument("--expires-at")
    integration_revoke = integration_commands.add_parser("revoke-client")
    integration_revoke.add_argument("--database", type=Path, required=True)
    integration_revoke.add_argument("client_ref")
    integration_catalog = integration_commands.add_parser("event-catalog")
    integration_catalog.add_argument("--database", type=Path, required=True)
    integration_event = integration_commands.add_parser("submit-event")
    integration_event.add_argument("--database", type=Path, required=True)
    integration_event.add_argument("--token", required=True)
    integration_event.add_argument("--event", type=Path, required=True)
    integration_webhooks = integration_commands.add_parser("webhooks")
    integration_webhooks.add_argument("--database", type=Path, required=True)
    integration_webhook_create = integration_commands.add_parser("create-webhook")
    integration_webhook_create.add_argument("--database", type=Path, required=True)
    integration_webhook_create.add_argument("--name", required=True)
    integration_webhook_create.add_argument("--endpoint", required=True)
    integration_webhook_create.add_argument("--event-type", action="append", required=True)
    integration_webhook_create.add_argument("--secret-ref", required=True)
    integration_webhook_create.add_argument("--enable", action="store_true")
    integration_webhook_state = integration_commands.add_parser("webhook-state")
    integration_webhook_state.add_argument("--database", type=Path, required=True)
    integration_webhook_state.add_argument("subscription_ref")
    integration_webhook_state.add_argument("--enable", action="store_true")
    integration_deliveries = integration_commands.add_parser("deliveries")
    integration_deliveries.add_argument("--database", type=Path, required=True)
    integration_diagnostics = integration_commands.add_parser("diagnostics")
    integration_diagnostics.add_argument("--database", type=Path, required=True)
    app = subparsers.add_parser("app", help="start and maintain the local Mailforge application")
    app_subparsers = app.add_subparsers(dest="app_command", required=True)
    for command, help_text in (
        ("init", "initialize the operator data directory"),
        ("start", "start the local application and open its browser interface"),
        ("stop", "stop the local application"),
        ("restart", "restart the local application"),
        ("status", "show runtime health and data-directory status"),
        ("open", "open the running application in the browser"),
    ):
        command_parser = app_subparsers.add_parser(command, help=help_text)
        command_parser.add_argument("--data-dir", type=Path, default=default_data_directory())
        command_parser.add_argument("--port", type=int, default=DEFAULT_PORT)
        if command in {"start", "restart"}:
            command_parser.add_argument("--no-open", action="store_true")
    app_backup = app_subparsers.add_parser("backup", help="create a complete application backup")
    app_backup.add_argument("--data-dir", type=Path, default=default_data_directory())
    app_backup.add_argument("--destination", type=Path)
    app_validate = app_subparsers.add_parser("validate-backup", help="validate a backup archive")
    app_validate.add_argument("backup", type=Path)
    app_restore = app_subparsers.add_parser("restore", help="restore into a new data directory")
    app_restore.add_argument("--backup", type=Path, required=True)
    app_restore.add_argument("--destination", type=Path, required=True)
    app_restore.add_argument("--replace", action="store_true")
    serve = subparsers.add_parser("serve", help="run the local browser operator console")
    serve.add_argument("--database", type=Path)
    serve.add_argument("--data-dir", type=Path)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--frontend-dir", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "app":
        data = DataDirectory.from_path(args.data_dir)
        if args.app_command == "init":
            data.initialize()
            print(json.dumps({"initialized": True, "data_directory_id": data.identity}))
        elif args.app_command == "backup":
            print(json.dumps({"backup": str(create_backup(data, args.destination))}))
        elif args.app_command == "validate-backup":
            print(json.dumps(validate_backup(args.backup), indent=2))
        elif args.app_command == "restore":
            restored = restore_backup(args.backup, args.destination, replace=args.replace)
            print(json.dumps({"restored": str(restored)}))
        else:
            application = LocalApplication(data, port=args.port)
            if args.app_command == "start":
                print(json.dumps(application.start(open_browser=not args.no_open), indent=2))
            elif args.app_command == "stop":
                print(json.dumps(application.stop(), indent=2))
            elif args.app_command == "restart":
                application.stop()
                print(json.dumps(application.start(open_browser=not args.no_open), indent=2))
            elif args.app_command == "status":
                print(json.dumps(application.status(), indent=2))
            elif args.app_command == "open":
                print(json.dumps(application.open(), indent=2))
        return 0
    if args.command == "data-studio":
        if args.data_command == "schema":
            print(json.dumps(detect_schema(args.input, sample_limit=args.sample_limit), indent=2))
        else:
            from mailforge.infrastructure.campaign_store import CampaignStore

            service = DataStudioService(CampaignStore(args.database))
            try:
                if args.data_command == "create-recipe":
                    definition = json.loads(args.definition.read_text(encoding="utf-8"))
                    print(
                        json.dumps(
                            service.create_recipe(args.name, args.description, definition), indent=2
                        )
                    )
                elif args.data_command == "list-recipes":
                    print(json.dumps(service.list_recipes(include_archived=True), indent=2))
                elif args.data_command == "preview":
                    definition = json.loads(args.definition.read_text(encoding="utf-8"))
                    sources = [args.input, *args.source] if args.source else []
                    print(
                        json.dumps(
                            service.preview_sources(
                                [(path, path.name, path.stem) for path in sources],
                                definition,
                                limit=args.limit,
                            )
                            if args.source
                            else service.preview(args.input, definition, limit=args.limit),
                            indent=2,
                        )
                    )
                else:
                    sources = [args.input, *args.source] if args.source else []
                    job = (
                        service.create_multi_source_job(
                            [(path, path.name, path.stem) for path in sources], args.revision_id
                        )
                        if args.source
                        else service.create_job(args.input, args.filename, args.revision_id)
                    )
                    service.start(job["id"])
                    while True:
                        job = service.get_job(job["id"])
                        if job["status"] not in {"pending", "running"}:
                            break
                        time.sleep(0.05)
                    print(json.dumps(job, indent=2))
                    return 0 if job["status"] == "completed" else 2
            finally:
                service.close()
    elif args.command == "functions":
        from mailforge.infrastructure.campaign_store import CampaignStore

        service = ProgrammableOperationsService(CampaignStore(args.database))
        if args.function_command == "list":
            print(json.dumps(service.list_functions(), indent=2))
        elif args.function_command == "create":
            definition = json.loads(args.definition.read_text(encoding="utf-8"))
            print(
                json.dumps(
                    service.create_function(args.name, definition, version=args.version), indent=2
                )
            )
        elif args.function_command == "preview":
            records = json.loads(args.records.read_text(encoding="utf-8"))
            print(
                json.dumps(
                    service.execute(
                        args.name,
                        records,
                        revision_id=args.revision_id,
                        parameters=json.loads(args.parameters),
                        context="data_studio",
                        correlation_id=f"cli-preview:{uuid.uuid4()}",
                    ),
                    indent=2,
                )
            )
        elif args.function_command == "revisions":
            print(json.dumps(service.list_revisions(args.name), indent=2))
        elif args.function_command == "history":
            print(json.dumps(service.list_executions(args.name, limit=args.limit), indent=2))
        elif args.function_command in {"enable", "disable"}:
            print(
                json.dumps(
                    service.set_enabled(args.name, args.function_command == "enable"), indent=2
                )
            )
        else:
            manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
            import hashlib

            digest = hashlib.sha256(args.source.read_bytes()).hexdigest()
            if digest != args.confirm_checksum:
                raise SystemExit("checksum confirmation does not match the local extension source")
            print(
                json.dumps(
                    service.install_python_extension(args.source, manifest, confirm=True), indent=2
                )
            )
    elif args.command == "email-standards":
        from mailforge.infrastructure.campaign_store import CampaignStore

        service = EmailStandardsService(CampaignStore(args.database))
        if args.standards_command == "identities":
            print(json.dumps(service.list_sender_identities(), indent=2))
        elif args.standards_command == "register-identity":
            print(
                json.dumps(
                    service.register_sender_identity(
                        args.ref,
                        visible_from=args.visible_from,
                        reply_to=args.reply_to,
                        envelope_from=args.envelope_from,
                        dkim_domain=args.dkim_domain,
                        dkim_selector=args.dkim_selector,
                        provider_account_ref=args.provider_account,
                        provider_signing_status=args.provider_signing_status,
                    ),
                    indent=2,
                )
            )
        elif args.standards_command == "domain":
            print(
                json.dumps(
                    service.inspect_domain(
                        args.domain,
                        sender_ip=args.sender_ip,
                        envelope_from=args.envelope_from,
                        dkim_selector=args.dkim_selector,
                        allow_network=args.network,
                    ),
                    indent=2,
                )
            )
        elif args.standards_command == "message":
            print(
                json.dumps(
                    service.validate_message(
                        args.path.read_bytes(),
                        communication_purpose=args.purpose,
                        expected_identity=args.identity,
                    ),
                    indent=2,
                )
            )
        elif args.standards_command == "history":
            print(json.dumps(service.history(kind=args.kind, limit=args.limit), indent=2))
        elif args.standards_command == "guidance":
            print(json.dumps(service.provider_guidance(), indent=2))
        elif args.standards_command == "dmarc-import":
            print(json.dumps(service.import_dmarc_report(args.path), indent=2))
        elif args.standards_command == "dmarc-summary":
            print(json.dumps(service.report_summary(), indent=2))
    elif args.command == "serve":
        import uvicorn

        from mailforge.interfaces.web import create_app

        database = args.database or (
            args.data_dir / "mailforge.sqlite3"
            if args.data_dir
            else Path("state/mailforge.sqlite3")
        )
        data_directory = args.data_dir or database.parent
        frontend_dir = args.frontend_dir
        if frontend_dir is None:
            packaged = Path(__file__).resolve().parents[1] / "web_assets"
            source_tree = Path(__file__).resolve().parents[3] / "web" / "dist"
            frontend_dir = packaged if packaged.is_dir() else source_tree
        if not frontend_dir.is_dir():
            raise SystemExit(
                f"compiled frontend assets not found at {frontend_dir}; run `npm run build` first"
            )
        if args.host not in {"127.0.0.1", "localhost", "::1"}:
            raise SystemExit("mailforge serve only binds to a loopback host")
        try:
            with DatabaseProcessLock(database):
                uvicorn.run(
                    create_app(database, frontend_dir=frontend_dir, data_directory=data_directory),
                    host=args.host,
                    port=args.port,
                    log_level="info",
                )
        except ProcessLockError as error:
            raise SystemExit(str(error)) from error
    elif args.command == "ingest":
        addresses = collect_unique_addresses(args.input, max_bytes=args.max_bytes)
        summary = {"count": len(addresses), "addresses": addresses}
        payload = json.dumps(summary, indent=2) + "\n"
        if args.output:
            args.output.write_text(payload, encoding="utf-8")
        else:
            print(payload, end="")
    elif args.command == "sort":
        overrides = {}
        if args.results_dir is not None:
            overrides["results_dir"] = args.results_dir
        if args.config_dir is not None:
            overrides["config_dir"] = args.config_dir
        if args.checkpoint is not None:
            overrides["checkpoint_path"] = args.checkpoint
        if args.dns_mode is not None:
            overrides["dns_mode"] = DNSMode(args.dns_mode)
        if args.max_bytes is not None:
            overrides["max_bytes"] = args.max_bytes
        if args.profile is not None:
            overrides["profile"] = args.profile
        if args.progress_every is not None:
            overrides["progress_every"] = args.progress_every
        if args.debug:
            logging.basicConfig(level=logging.DEBUG, force=True)
        config = SortConfig.from_environment(**overrides)
        inputs = args.input[0] if len(args.input) == 1 else args.input
        result = EmailSorter(config).sort_file(inputs, resume=args.resume, dry_run=args.dry_run)
        print(
            json.dumps(
                {
                    "committed": result.committed,
                    "summary": str(result.summary_path),
                    "structured_results": str(result.structured_results_path),
                    "quarantine": str(result.quarantine_path),
                }
            )
        )
        return 0 if result.committed else 2
    elif args.command == "summary":
        overrides = {"results_dir": args.results_dir} if args.results_dir is not None else {}
        summary = EmailSorter(SortConfig.from_environment(**overrides)).view_summary()
        if summary is None:
            print("No summary found")
            return 1
        print(json.dumps(summary, indent=2))
    elif args.command == "clean":
        overrides = {"results_dir": args.results_dir} if args.results_dir is not None else {}
        config = SortConfig.from_environment(**overrides)
        removed = EmailSorter(config).delete_results(confirm=args.yes)
        return 0 if removed or not config.results_dir.exists() else 1
    elif args.command == "extract-jsonl":
        count = sum(1 for _ in iter_jsonl_email_field(args.input, max_bytes=args.max_bytes))
        args.output.write_text(
            "".join(
                f"{email}\n"
                for email in iter_jsonl_email_field(args.input, max_bytes=args.max_bytes)
            ),
            encoding="utf-8",
        )
        print(f"Wrote {count} unique, validated email(s) to {args.output}")
    elif args.command == "refresh-disposable":
        count = DisposableDomainStore(args.path).refresh(args.url)
        print(f"Saved {count} disposable domains to {args.path}")
    elif args.command == "transform":
        if args.operation == "format":
            if args.output is None:
                raise SystemExit("transform format requires an output path")
            format_email_file(args.input, args.output)
        elif args.operation == "dedupe":
            print(f"Removed {remove_duplicate_lines(args.input)} duplicates")
        elif args.operation == "list":
            if args.output is None:
                raise SystemExit("transform list requires an output path")
            write_legacy_list(args.input, args.output)
        else:
            print(count_legacy_list(args.input))
    elif args.command == "field":
        from mailforge.infrastructure.campaign_store import CampaignStore

        service = SegmentationService(CampaignStore(args.database))
        if args.field_command == "list":
            print(json.dumps(service.list_fields(include_archived=True), indent=2))
        elif args.field_command == "create":
            print(
                json.dumps(
                    service.create_field(
                        args.machine_key,
                        args.label,
                        args.field_type,
                        controlled_values=args.controlled_value or None,
                    ),
                    indent=2,
                )
            )
        elif args.field_command == "update":
            print(
                json.dumps(
                    service.update_field(
                        args.field_id, label=args.label, field_type=args.field_type
                    ),
                    indent=2,
                )
            )
        else:
            service.archive_field(args.field_id)
            print(json.dumps({"field_id": args.field_id, "status": "archived"}))
    elif args.command == "segment":
        from mailforge.infrastructure.campaign_store import CampaignStore

        service = SegmentationService(CampaignStore(args.database))
        if args.segment_command == "list":
            print(json.dumps(service.list_segments(include_archived=True), indent=2))
        elif args.segment_command == "create":
            definition = json.loads(args.definition.read_text(encoding="utf-8"))
            print(
                json.dumps(
                    service.create_segment(args.name, definition, description=args.description),
                    indent=2,
                )
            )
        elif args.segment_command == "preview":
            print(
                json.dumps(
                    service.preview(
                        args.segment_id,
                        revision_id=args.revision_id,
                        limit=args.limit,
                        offset=args.offset,
                    ),
                    indent=2,
                )
            )
        elif args.segment_command == "edit":
            definition = json.loads(args.definition.read_text(encoding="utf-8"))
            print(
                json.dumps(
                    service.edit_segment(
                        args.segment_id,
                        definition,
                        expected_revision_id=args.expected_revision_id,
                        description=args.description,
                    ),
                    indent=2,
                )
            )
        elif args.segment_command == "archive":
            service.archive_segment(args.segment_id)
            print(json.dumps({"segment_id": args.segment_id, "status": "archived"}))
        elif args.segment_command == "import":
            rows = json.loads(args.rows.read_text(encoding="utf-8"))
            mappings = json.loads(args.mappings.read_text(encoding="utf-8"))
            print(
                json.dumps(
                    service.import_structured(
                        rows,
                        mappings=mappings,
                        list_id=args.list_id,
                        source_ref=args.source_ref,
                        overwrite=args.overwrite,
                    ),
                    indent=2,
                )
            )
        else:
            print(
                json.dumps(
                    service.set_membership(
                        args.list_id, args.contact_id, args.action, source="cli"
                    ),
                    indent=2,
                )
            )
    elif args.command == "message":
        from mailforge.infrastructure.campaign_store import CampaignStore

        service = MessageStudioService(CampaignStore(args.database))
        if args.message_command == "list":
            print(json.dumps(service.list_documents(), indent=2))
        elif args.message_command == "validate":
            document = json.loads(args.document.read_text(encoding="utf-8"))
            print(
                json.dumps(
                    {"valid": True, "document": service.validate_document(document)}, indent=2
                )
            )
        else:
            print(
                json.dumps(
                    service.preview(
                        revision_id=args.revision_id,
                        contact_id=args.contact_id,
                        condition_mode=args.condition_mode,
                    ),
                    indent=2,
                )
            )
    elif args.command == "automation":
        from mailforge.infrastructure.campaign_store import CampaignStore

        store = CampaignStore(args.database)
        data_studio = DataStudioService(store)
        service = AutomationService(store, data_studio=data_studio)
        if args.automation_command == "list":
            print(json.dumps(service.list_workflows(), indent=2))
        elif args.automation_command == "create":
            print(json.dumps(service.create_workflow(args.name, args.description), indent=2))
        elif args.automation_command == "validate":
            graph = json.loads(args.graph.read_text(encoding="utf-8"))
            print(json.dumps(service.validate_graph(graph), indent=2))
        elif args.automation_command == "save":
            graph = json.loads(args.graph.read_text(encoding="utf-8"))
            print(
                json.dumps(
                    service.save_revision(args.workflow_id, graph, revision_id=args.revision_id),
                    indent=2,
                )
            )
        elif args.automation_command == "publish":
            print(json.dumps(service.publish(args.workflow_id, args.revision_id), indent=2))
        elif args.automation_command == "activate":
            print(
                json.dumps(
                    service.set_lifecycle(args.workflow_id, "activate", args.revision_id), indent=2
                )
            )
        elif args.automation_command == "enroll":
            print(
                json.dumps(service.enroll(args.workflow_id, contact_id=args.contact_id), indent=2)
            )
        else:
            print(json.dumps(service.advance(args.run_id), indent=2))
    elif args.command == "campaign":
        if args.campaign_command == "demo":
            print(json.dumps(run_synthetic_demo(args.database), indent=2))
        elif args.campaign_command == "outbound-demo":
            print(json.dumps(run_outbound_demo(args.database), indent=2))
        elif args.campaign_command == "restore":
            from mailforge.infrastructure.campaign_store import CampaignStore

            destination = CampaignStore.restore_from(args.source, args.destination)
            print(json.dumps({"restored": str(destination)}))
        elif args.campaign_command == "translate-legacy":
            payload = load_legacy_configuration(args.config).to_dict()
            rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
            if args.output:
                args.output.write_text(rendered, encoding="utf-8")
            else:
                print(rendered, end="")
        elif args.campaign_command == "import-legacy":
            from mailforge.infrastructure.campaign_store import CampaignStore

            configuration = load_legacy_configuration(args.config)
            campaign_id, target_report = prepare_legacy_campaign(
                CampaignStore(args.database),
                configuration,
                campaign_name=args.campaign,
                sender_email=args.sender_email,
            )
            print(
                json.dumps(
                    {
                        "campaign_id": campaign_id,
                        "targets": target_report.to_dict(),
                        "migration": configuration.migration.to_dict(),
                    }
                )
            )
        else:
            from mailforge.infrastructure.campaign_store import CampaignStore

            if args.campaign_command == "backup" and not args.database.is_file():
                raise FileNotFoundError(f"campaign database does not exist: {args.database}")
            service = CampaignService(CampaignStore(args.database))
            outbound = OutboundService(service.store)
            segmentation = SegmentationService(service.store)
            data_studio = DataStudioService(service.store)
            message_studio = MessageStudioService(
                service.store,
                outbound=outbound,
                segmentation=segmentation,
                data_studio=data_studio,
            )
            operations = CampaignOperationsService(
                service.store,
                campaigns=service,
                outbound=outbound,
                message_studio=message_studio,
                segmentation=segmentation,
                provider_operations=ProviderOperationsService(service.store),
                reporting=ReportingService(service.store),
                email_standards=EmailStandardsService(service.store),
            )
            if args.campaign_command == "backup":
                destination = service.store.backup_to(args.destination)
                print(json.dumps({"backup": str(destination)}))
            elif args.campaign_command == "rehearse-migration":
                print(
                    json.dumps(
                        rehearse_legacy_migration(
                            service.store,
                            list_id=args.list_id,
                            campaign_id=args.campaign_id,
                            sent_path=args.sent,
                            failed_path=args.failed,
                            hard_bounces_path=args.hard_bounces,
                        )
                    )
                )
            elif args.campaign_command == "rehearse-legacy-state":
                print(
                    json.dumps(
                        migrate_legacy_state(
                            service.store,
                            list_id=args.list_id,
                            campaign_id=args.campaign_id,
                            sent_path=args.sent,
                            failed_path=args.failed,
                            hard_bounces_path=args.hard_bounces,
                            events_path=args.events,
                            log_path=args.log,
                        ).to_dict()
                    )
                )
            elif args.campaign_command == "reconcile":
                service.store.reconcile_attempt(args.attempt_id, args.decision)
                print(json.dumps({"reconciled": args.attempt_id, "decision": args.decision}))
            elif args.campaign_command == "import-sort":
                store = service.store
                list_id = store.create_list(args.list_name, args.description)
                print(
                    json.dumps(
                        {
                            "list_id": list_id,
                            **service.import_sort_results(args.processing, list_id),
                        }
                    )
                )
            elif args.campaign_command == "create":
                campaign_id = operations.create_draft(
                    name=args.name,
                    list_id=args.list_id,
                    segment_id=args.segment_id,
                    segment_revision_id=args.segment_revision_id,
                    purpose=args.purpose,
                )
                print(json.dumps({"campaign_id": campaign_id}))
            elif args.campaign_command == "workspace":
                print(
                    json.dumps(
                        operations.workspace(args.campaign_id, preview_limit=args.preview_limit),
                        indent=2,
                    )
                )
            elif args.campaign_command == "update":
                operations.update_draft(
                    args.campaign_id,
                    expected_updated_at=args.expected_updated_at,
                    name=args.name,
                    description=args.description,
                    list_id=args.list_id,
                    segment_id=args.segment_id,
                    segment_revision_id=args.segment_revision_id,
                    sender_email=args.sender_email,
                    subject=args.subject,
                    text=args.text,
                    message_revision_id=args.message_revision_id,
                    provider_account_ref=args.provider_account_ref,
                )
                print(json.dumps({"campaign_id": args.campaign_id, "updated": True}))
            elif args.campaign_command == "preflight":
                print(json.dumps(operations.preflight(args.campaign_id), indent=2))
            elif args.campaign_command == "schedule":
                print(
                    json.dumps(
                        operations.set_schedule(
                            args.campaign_id,
                            ScheduleSpec(
                                mode=ScheduleMode(args.mode),
                                timezone=args.timezone,
                                scheduled_at=args.scheduled_at,
                                window_start=args.window_start,
                                window_end=args.window_end,
                                pacing_seconds=args.pacing_seconds,
                                cooldown_seconds=args.cooldown_seconds,
                                recurrence=args.recurrence,
                                catch_up_policy=CatchUpPolicy(args.catch_up_policy),
                                fold=args.fold,
                            ),
                        ),
                        indent=2,
                    )
                )
            elif args.campaign_command in {"pause", "resume", "cancel"}:
                print(
                    json.dumps(
                        operations.control(args.campaign_id, args.campaign_command), indent=2
                    )
                )
            elif args.campaign_command == "recipients":
                print(
                    json.dumps(
                        {
                            "items": operations.recipients(
                                args.campaign_id, limit=args.limit, offset=args.offset
                            )
                        },
                        indent=2,
                    )
                )
            elif args.campaign_command == "attempts":
                print(
                    json.dumps(
                        {
                            "items": operations.attempts(
                                args.campaign_id, limit=args.limit, offset=args.offset
                            )
                        },
                        indent=2,
                    )
                )
            elif args.campaign_command == "export":
                body, _media_type, _filename = operations.export(args.campaign_id, fmt=args.format)
                print(body)
            elif args.campaign_command == "prepare":
                if args.run_key == "primary":
                    print(json.dumps(operations.prepare(args.campaign_id), indent=2))
                else:
                    print(
                        json.dumps(
                            service.prepare(args.campaign_id, run_key=args.run_key).to_dict(),
                            indent=2,
                        )
                    )
            elif args.campaign_command == "audience":
                segmentation = SegmentationService(service.store)
                print(
                    json.dumps(
                        {
                            "audience": segmentation.get_audience(args.campaign_id, args.run_key),
                            "items": segmentation.audience_recipients(
                                args.campaign_id, args.run_key
                            ),
                        },
                        indent=2,
                    )
                )
            elif args.campaign_command == "preview":
                print(json.dumps(service.preview(args.campaign_id), indent=2))
            elif args.campaign_command == "authorize-simulation":
                service.authorize_simulation(args.campaign_id)
                print(json.dumps({"authorized": True, "mode": "simulation"}))
            elif args.campaign_command == "simulate":
                outcomes: dict[str, AttemptOutcome] = {}
                for item in args.outcome:
                    email, outcome = item.split("=", 1)
                    outcomes[email] = AttemptOutcome(outcome)
                report_value = service.execute(
                    args.campaign_id,
                    ScriptedTransport(outcomes),
                    retry_deferred=args.retry_deferred,
                    stop_after=args.stop_after,
                )
                print(json.dumps(report_value.to_dict(), indent=2))
            elif args.campaign_command == "report":
                print(json.dumps(service.report(args.campaign_id).to_dict(), indent=2))
            elif args.campaign_command == "suppress":
                suppression_id = service.add_suppression(
                    args.email,
                    SuppressionCategory(args.category),
                    args.reason,
                    purpose=args.purpose,
                    actor=args.actor,
                )
                print(json.dumps({"suppression_id": suppression_id}))
            elif args.campaign_command == "record-evidence":
                evidence_id = service.record_authorization_evidence(
                    args.email,
                    purpose=args.purpose,
                    status=EvidenceStatus(args.status),
                    source=args.source,
                    actor=args.actor,
                    evidence_ref=args.evidence_ref,
                )
                print(json.dumps({"evidence_id": evidence_id, "status": args.status}))
            elif args.campaign_command == "preference":
                preference_id = service.set_preference(
                    args.email,
                    purpose=args.purpose,
                    state=PreferenceState(args.state),
                    actor=args.actor,
                    evidence_ref=args.evidence_ref,
                )
                print(json.dumps({"preference_id": preference_id, "state": args.state}))
            elif args.campaign_command == "eligibility":
                print(json.dumps(service.inspect_eligibility(args.campaign_id), indent=2))
            elif args.campaign_command == "declarations":
                print(
                    json.dumps(
                        [
                            dict(row)
                            for row in service.store.list_operator_declarations(
                                campaign_id=args.campaign_id
                            )
                        ],
                        indent=2,
                    )
                )
            elif args.campaign_command == "suppressions":
                print(
                    json.dumps(
                        [
                            dict(row)
                            for row in service.store.list_suppressions(normalized_email=args.email)
                        ],
                        indent=2,
                    )
                )
            elif args.campaign_command == "export-contacts":
                print(json.dumps(service.export_contacts(list_id=args.list_id), indent=2))
            elif args.campaign_command == "deactivate-contact":
                service.deactivate_contact(args.email, reason=args.reason, actor=args.actor)
                print(json.dumps({"deactivated": args.email}))
            elif args.campaign_command == "plan-deletion":
                lifecycle_id = service.plan_deletion(
                    args.email, reason=args.reason, actor=args.actor
                )
                row = service.store.list_lifecycle_records()
                record = next(item for item in row if item["id"] == lifecycle_id)
                print(json.dumps(dict(record), indent=2))
    elif args.command == "provider":
        from mailforge.infrastructure.campaign_store import CampaignStore
        from mailforge.infrastructure.credentials import EnvironmentCredentialResolver

        provider_service = ProviderService(CampaignStore(args.database))
        provider_operations = ProviderOperationsService(provider_service.store)
        if args.provider_command == "list":
            kind = ProviderKind(args.kind) if args.kind else None
            print(json.dumps(provider_service.list_profiles(kind=kind), indent=2))
        elif args.provider_command == "register":
            provider_service.store.register_provider_profile(
                ProviderProfile(
                    ref=args.ref,
                    kind=ProviderKind(args.kind),
                    host=args.host,
                    port=args.port,
                    username=args.username,
                    credential_ref=args.credential_ref,
                    tls_mode=TLSMode(args.tls_mode),
                    auth_method=AuthenticationMethod(args.auth_method),
                    mailbox=args.mailbox,
                    sender_policy_ref=args.sender_policy,
                    timeout=args.timeout,
                    max_message_bytes=args.max_message_bytes,
                    ca_file=args.ca_file,
                )
            )
            print(json.dumps({"registered": args.ref}))
        elif args.provider_command == "validate":
            print(json.dumps(provider_service.validate(args.ref), indent=2))
        elif args.provider_command == "credentials":
            print(
                json.dumps(
                    provider_service.credential_status(args.ref, EnvironmentCredentialResolver()),
                    indent=2,
                )
            )
        elif args.provider_command == "local-diagnostic":
            print(
                json.dumps(
                    provider_service.local_protocol_diagnostic(
                        args.ref, EnvironmentCredentialResolver()
                    ),
                    indent=2,
                )
            )
        elif args.provider_command == "diagnostics":
            print(json.dumps(provider_service.diagnostics(args.ref), indent=2))
        elif args.provider_command == "accounts":
            print(json.dumps(provider_operations.accounts(), indent=2))
        elif args.provider_command == "capabilities":
            print(json.dumps(provider_operations.capabilities(), indent=2))
        elif args.provider_command == "suspend":
            print(
                json.dumps(
                    provider_operations.store.set_provider_account_status(
                        args.ref, "suspended", enabled=False
                    ),
                    indent=2,
                )
            )
        elif args.provider_command == "diagnostic":
            operation = ProviderOperation(args.operation)
            grant = None
            if args.authorization_ref:
                account = provider_operations.account(args.ref)
                if not args.purpose or not args.expires_at:
                    raise SystemExit("external diagnostic requires --purpose and --expires-at")
                grant = ExternalOperationGrant(
                    account_ref=args.ref,
                    operation=operation,
                    host=account["host"],
                    port=account["port"],
                    purpose=args.purpose,
                    max_submissions=0,
                    expires_at=args.expires_at,
                    authorization_ref=args.authorization_ref,
                )
            print(
                json.dumps(
                    provider_operations.diagnostic(args.ref, operation, grant=grant), indent=2
                )
            )
        elif args.provider_command == "test-send":
            account = provider_operations.account(args.ref)
            grant = ExternalOperationGrant(
                account_ref=args.ref,
                operation=ProviderOperation.CONTROLLED_TEST_SEND,
                host=account["host"],
                port=account["port"],
                purpose=args.purpose,
                sender=args.sender,
                recipient=args.recipient,
                max_submissions=1,
                expires_at=args.expires_at,
                authorization_ref=args.authorization_ref,
            )
            print(
                json.dumps(
                    provider_operations.controlled_test_send(
                        ref=args.ref,
                        sender=args.sender,
                        recipient=args.recipient,
                        message=args.message.read_text(encoding="utf-8"),
                        idempotency_key=args.idempotency_key,
                        grant=grant,
                    ),
                    indent=2,
                )
            )
    elif args.command == "inbound":
        if args.inbound_command == "synthetic-demo":
            print(json.dumps(run_inbound_demo(args.database), indent=2, default=str))
        else:
            from mailforge.infrastructure.campaign_store import CampaignStore

            service = InboundService(CampaignStore(args.database))
            operations = InboundOperations(service.store)
            service.set_operations(operations)
            provider_operations = ProviderOperationsService(service.store)
            mailbox_operations = MailboxOperationsService(
                service.store,
                inbound=service,
                inbound_operations=operations,
                provider_operations=provider_operations,
            )
            if args.inbound_command == "import-eml":
                report = service.ingest(
                    EmlFileSource(args.source_ref, args.path), retain_raw=args.retain_raw
                )
                print(json.dumps(report.__dict__, indent=2, default=str))
            elif args.inbound_command == "import-mbox":
                report = service.ingest(MboxSource(args.source_ref, args.path))
                print(json.dumps(report.__dict__, indent=2, default=str))
            elif args.inbound_command == "inspect":
                print(
                    json.dumps(
                        service.inspect(message_id=args.message_id, limit=args.limit),
                        indent=2,
                        default=str,
                    )
                )
            elif args.inbound_command == "review":
                print(json.dumps(service.review_queue(), indent=2, default=str))
            elif args.inbound_command == "decide":
                service.decide(args.action_id, args.decision, args.reason)
                print(json.dumps({"action_id": args.action_id, "decision": args.decision}))
            elif args.inbound_command == "replay":
                print(json.dumps({"replayed": service.replay(args.source_ref)}))
            elif args.inbound_command == "stats":
                print(json.dumps(service.stats(), indent=2))
            elif args.inbound_command == "search":
                print(
                    json.dumps(
                        operations.search(
                            q=args.query,
                            sender=args.sender,
                            recipient=args.recipient,
                            subject=args.subject,
                            classification=args.classification,
                            source_ref=args.source_ref,
                            account_ref=args.account_ref,
                            folder=args.folder,
                            read=None if args.read is None else args.read == "true",
                            flagged=None if args.flagged is None else args.flagged == "true",
                            has_attachment=(
                                None
                                if args.has_attachment is None
                                else args.has_attachment == "true"
                            ),
                            thread_key=args.thread_key,
                            received_after=args.received_after,
                            received_before=args.received_before,
                            processed=(
                                None if args.processed is None else args.processed == "true"
                            ),
                            limit=args.limit,
                            offset=args.offset,
                        ),
                        indent=2,
                        default=str,
                    )
                )
            elif args.inbound_command == "extract":
                print(
                    json.dumps(
                        operations.extract_message(args.message_id, args.fields or None),
                        indent=2,
                        default=str,
                    )
                )
            elif args.inbound_command == "export":
                media_type, content = operations.export(message_id=args.message_id, fmt=args.format)
                print(json.dumps({"media_type": media_type, "content": content}, indent=2))
            elif args.inbound_command == "rule":
                if args.rule_command == "list":
                    print(json.dumps(operations.list_rules(), indent=2, default=str))
                elif args.rule_command == "create":
                    if not args.name or not args.definition:
                        raise ValueError("rule create requires --name and --definition")
                    print(
                        json.dumps(
                            operations.create_rule(
                                args.name, json.loads(args.definition.read_text(encoding="utf-8"))
                            ),
                            indent=2,
                            default=str,
                        )
                    )
                else:
                    if not args.rule_id:
                        raise ValueError("rule status changes require --rule-id")
                    print(
                        json.dumps(
                            operations.set_rule_status(
                                args.rule_id,
                                "active" if args.rule_command == "activate" else "paused",
                            ),
                            indent=2,
                            default=str,
                        )
                    )
            elif args.inbound_command == "draft":
                print(
                    json.dumps(
                        operations.prepare_draft(args.message_id, args.kind, body=args.body),
                        indent=2,
                        default=str,
                    )
                )
            elif args.inbound_command == "mailbox":
                from mailforge.infrastructure.credentials import EnvironmentCredentialResolver

                if args.mailbox_command == "list":
                    print(
                        json.dumps(
                            service.store.list_inbound_mailboxes(args.account),
                            indent=2,
                            default=str,
                        )
                    )
                elif args.mailbox_command in {"discover", "sync"}:
                    account = provider_operations.account(args.account)
                    grant = ExternalOperationGrant(
                        account_ref=args.account,
                        operation=ProviderOperation.INBOUND_SYNC,
                        host=account["host"],
                        port=account["port"],
                        purpose=args.purpose,
                        expires_at=args.expires_at,
                        authorization_ref=args.authorization_ref,
                    )
                    if args.mailbox_command == "discover":
                        result = mailbox_operations.discover(
                            args.account,
                            resolver=EnvironmentCredentialResolver(),
                            grant=grant,
                        )
                    else:
                        report = mailbox_operations.sync(
                            args.account,
                            mailbox=args.mailbox,
                            batch_limit=args.batch_limit,
                            resolver=EnvironmentCredentialResolver(),
                            grant=grant,
                        )
                        result = {"report": report.__dict__, "stats": service.stats()}
                    print(json.dumps(result, indent=2, default=str))
                elif args.mailbox_command == "action":
                    row = service.store.get_inbound_message(args.message_id)
                    result = mailbox_operations.execute_action(
                        row["source_ref"],
                        action=args.action,
                        authorization_ref=args.authorization_ref,
                        purpose=args.purpose,
                        expires_at=args.expires_at,
                        idempotency_key=args.idempotency_key,
                        message_id=args.message_id,
                        uidvalidity=args.uidvalidity,
                        uid=args.uid,
                        target_folder=args.target_folder,
                        flags=tuple(args.flag),
                        resolver=EnvironmentCredentialResolver(),
                    )
                    print(json.dumps(result, indent=2, default=str))
                else:
                    result = mailbox_operations.reconcile_action(
                        args.action_id, resolver=EnvironmentCredentialResolver()
                    )
                    print(json.dumps(result, indent=2, default=str))
    elif args.command == "reports":
        from mailforge.infrastructure.campaign_store import CampaignStore

        service = ReportingService(CampaignStore(args.database))
        filters = ReportFilters(
            preset=args.preset,
            start=args.start,
            end=args.end,
            timezone=args.timezone,
            provider_ref=args.provider_ref,
            campaign_id=args.campaign_id,
            workflow_id=args.workflow_id,
            source_ref=args.source_ref,
            account_ref=args.account_ref,
            folder=args.folder,
            job_kind=args.job_kind,
            limit=args.limit,
            offset=args.offset,
        )
        try:
            if args.reports_command == "summary":
                print(json.dumps(service.summary(filters), indent=2, default=str))
            else:
                content, _media_type, _filename = service.export(
                    args.report_type,
                    filters,
                    format=args.format,
                )
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_bytes(content)
                print(json.dumps({"path": str(args.output), "format": args.format}))
        except ReportingError as error:
            raise SystemExit(f"{error.code}: {error}") from error
    elif args.command == "data-connections":
        from mailforge.infrastructure.campaign_store import CampaignStore

        store = CampaignStore(args.database)
        data_studio = DataStudioService(store) if args.connection_command == "handoff" else None
        service = DataConnectionsService(store, data_studio=data_studio)
        try:
            if args.connection_command == "list":
                print(json.dumps(service.list_connections(), indent=2))
            elif args.connection_command == "create":
                config = json.loads(args.config)
                selection = json.loads(args.selection)
                print(
                    json.dumps(
                        service.create_connection(
                            args.name,
                            args.source_type,
                            config,
                            credential_ref=args.credential_ref,
                            selection=selection,
                            source_path=args.source,
                            source_filename=args.source.name if args.source else None,
                        ),
                        indent=2,
                    )
                )
            elif args.connection_command == "test":
                print(json.dumps(service.test_connection(args.profile_id), indent=2))
            elif args.connection_command == "schema":
                print(json.dumps(service.schema(args.profile_id), indent=2))
            elif args.connection_command == "preview":
                print(
                    json.dumps(
                        service.preview(
                            args.profile_id, json.loads(args.selection), limit=args.limit
                        ),
                        indent=2,
                    )
                )
            elif args.connection_command == "snapshot":
                print(
                    json.dumps(
                        service.start_snapshot(
                            args.profile_id,
                            json.loads(args.selection),
                            idempotency_key=args.idempotency_key,
                            wait=args.wait,
                        ),
                        indent=2,
                    )
                )
            elif args.connection_command == "list-snapshots":
                print(json.dumps(service.list_snapshots(args.profile_id), indent=2))
            elif args.connection_command == "cancel":
                print(json.dumps(service.cancel_snapshot(args.snapshot_id), indent=2))
            elif args.connection_command == "handoff":
                print(
                    json.dumps(
                        service.handoff_to_data_studio(args.snapshot_id, args.recipe_revision_id),
                        indent=2,
                    )
                )
        except (DataConnectionError, ValueError, OSError, json.JSONDecodeError) as error:
            raise SystemExit(str(error)) from error
        finally:
            service.close()
            if data_studio is not None:
                data_studio.close()
    elif args.command == "integrations":
        from mailforge.infrastructure.campaign_store import CampaignStore

        service = IntegrationHubService(CampaignStore(args.database))
        try:
            if args.integration_command == "list":
                print(
                    json.dumps(
                        {
                            "clients": service.list_clients(),
                            "subscriptions": service.list_subscriptions(),
                            "events": service.list_events(),
                            "deliveries": service.deliveries(),
                        },
                        indent=2,
                        default=str,
                    )
                )
            elif args.integration_command == "issue-client":
                print(
                    json.dumps(
                        service.issue_client(args.name, args.scope, expires_at=args.expires_at),
                        indent=2,
                    )
                )
            elif args.integration_command == "rotate-client":
                print(
                    json.dumps(
                        service.rotate_client(args.client_ref, expires_at=args.expires_at), indent=2
                    )
                )
            elif args.integration_command == "revoke-client":
                print(json.dumps(service.revoke_client(args.client_ref), indent=2))
            elif args.integration_command == "event-catalog":
                print(json.dumps(service.event_catalog(), indent=2))
            elif args.integration_command == "submit-event":
                envelope = json.loads(args.event.read_text())
                client = service.authenticate(args.token, "INGEST_LOCAL_EVENT")
                print(json.dumps(service.submit_event(client, envelope), indent=2, default=str))
            elif args.integration_command == "webhooks":
                print(json.dumps(service.list_subscriptions(), indent=2, default=str))
            elif args.integration_command == "create-webhook":
                print(
                    json.dumps(
                        service.create_subscription(
                            args.name,
                            args.endpoint,
                            args.event_type,
                            secret_ref=args.secret_ref,
                            enabled=args.enable,
                        ),
                        indent=2,
                        default=str,
                    )
                )
            elif args.integration_command == "webhook-state":
                print(
                    json.dumps(
                        service.set_subscription(args.subscription_ref, enabled=args.enable),
                        indent=2,
                        default=str,
                    )
                )
            elif args.integration_command == "deliveries":
                print(json.dumps(service.deliveries(), indent=2, default=str))
            elif args.integration_command == "diagnostics":
                print(json.dumps(service.diagnostics(), indent=2, default=str))
        except (IntegrationHubError, ValueError, OSError, json.JSONDecodeError) as error:
            raise SystemExit(f"{getattr(error, 'code', 'integration_invalid')}: {error}") from error
    elif args.command == "interactive":
        overrides = {}
        if args.results_dir is not None:
            overrides["results_dir"] = args.results_dir
        if args.config_dir is not None:
            overrides["config_dir"] = args.config_dir
        if args.checkpoint is not None:
            overrides["checkpoint_path"] = args.checkpoint
        config = SortConfig.from_environment(**overrides)
        while True:
            print("0 Sort emails\n1 View summary\n2 Delete results\n3 Exit")
            choice = input("Choice: ").strip()
            try:
                sorter = EmailSorter(config)
                if choice == "0":
                    sorter.sort_file(input("File path: ").strip())
                elif choice == "1":
                    print(json.dumps(sorter.view_summary(), indent=2))
                elif choice == "2":
                    sorter.delete_results()
                elif choice == "3":
                    break
            except KeyboardInterrupt, EOFError:
                raise
            except Exception as error:
                print(f"Operation failed: {error}")
    return 0
