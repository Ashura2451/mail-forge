"""SQLite authority for the durable Mailforge campaign core.

The store deliberately contains no SMTP credentials or message bodies. It owns
local contact identity, policy decisions, campaign lifecycle, delivery intent,
attempt evidence, and operational events.
"""

# SQL statements are kept readable as contiguous fragments; their source lines
# can exceed the Python line-length convention without changing the schema.
# ruff: noqa: E501

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mailforge.domain.campaign import (
    AttemptOutcome,
    CampaignStatus,
    DeliveryState,
    ExecutionAuthorization,
    SuppressionCategory,
    utc_now,
    validate_campaign_transition,
)
from mailforge.domain.email import normalize_email, validate_email
from mailforge.domain.inbound import (
    ActionStatus,
    ActionType,
    Correlation,
    InboundKind,
    InboundSourceType,
    ParsedInbound,
    SourceMessage,
    TrustStatus,
)
from mailforge.domain.outbound import (
    PolicyDecision,
    ScheduleSpec,
    SenderPolicy,
    TemplateDefinition,
    TransportPhase,
)
from mailforge.domain.policy import (
    EvidenceStatus,
    OperatorAction,
    PreferenceState,
)
from mailforge.domain.provider import ProviderDiagnostic, ProviderProfile
from mailforge.infrastructure.credentials import validate_credential_reference

SCHEMA_VERSION = 12
SENSITIVE_KEY = re.compile(
    r"(?:password|secret|token|api[_-]?key|private[_-]?key|credential)", re.I
)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS contacts (
    id TEXT PRIMARY KEY,
    normalized_email TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contact_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id TEXT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    source_file TEXT NOT NULL,
    source_record_position INTEGER NOT NULL,
    original_input TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    imported_at TEXT NOT NULL,
    UNIQUE(contact_id, source_file, source_record_position, original_input)
);
CREATE TABLE IF NOT EXISTS lists (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS list_memberships (
    list_id TEXT NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    contact_id TEXT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    added_at TEXT NOT NULL,
    PRIMARY KEY(list_id, contact_id)
);
CREATE TABLE IF NOT EXISTS suppressions (
    id TEXT PRIMARY KEY,
    normalized_email TEXT,
    contact_id TEXT REFERENCES contacts(id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_id TEXT,
    reason TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    effective_at TEXT NOT NULL,
    expires_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(normalized_email, contact_id, category, scope_type, scope_id)
);
CREATE TABLE IF NOT EXISTS campaigns (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    sender_config_ref TEXT NOT NULL,
    template_ref TEXT NOT NULL,
    audience_list_id TEXT NOT NULL REFERENCES lists(id),
    status TEXT NOT NULL,
    authorization TEXT NOT NULL DEFAULT 'unauthorized',
    configuration_json TEXT NOT NULL DEFAULT '{}',
    scheduled_at TEXT,
    started_at TEXT,
    completed_at TEXT,
    failure_reason TEXT,
    pause_requested INTEGER NOT NULL DEFAULT 0 CHECK(pause_requested IN (0, 1)),
    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS campaign_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    from_status TEXT,
    to_status TEXT NOT NULL,
    reason TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS delivery_intents (
    id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    contact_id TEXT NOT NULL REFERENCES contacts(id),
    normalized_email TEXT NOT NULL,
    state TEXT NOT NULL,
    eligibility_reason TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    reserved_at TEXT,
    prepared_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(campaign_id, contact_id)
);
CREATE TABLE IF NOT EXISTS delivery_attempts (
    id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL REFERENCES delivery_intents(id) ON DELETE CASCADE,
    attempt_number INTEGER NOT NULL,
    attempt_state TEXT NOT NULL,
    outcome TEXT,
    transport_id TEXT,
    error_code TEXT,
    error_message TEXT,
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(intent_id, attempt_number)
);
CREATE TABLE IF NOT EXISTS operational_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sources_contact ON contact_sources(contact_id);
CREATE INDEX IF NOT EXISTS idx_memberships_contact ON list_memberships(contact_id);
CREATE INDEX IF NOT EXISTS idx_suppressions_lookup ON suppressions(normalized_email, contact_id, active);
CREATE INDEX IF NOT EXISTS idx_intents_campaign_state ON delivery_intents(campaign_id, state);
CREATE INDEX IF NOT EXISTS idx_attempts_intent ON delivery_attempts(intent_id);
CREATE INDEX IF NOT EXISTS idx_events_aggregate ON operational_events(aggregate_type, aggregate_id);
"""

MIGRATION_2_COLUMNS = (
    ("delivery_intents", "lease_owner", "TEXT"),
    ("delivery_intents", "lease_expires_at", "TEXT"),
    ("delivery_intents", "not_before_utc", "TEXT"),
    ("delivery_attempts", "sender_policy_ref", "TEXT"),
    ("delivery_attempts", "recipient_domain", "TEXT"),
    ("delivery_attempts", "transport_phase", "TEXT NOT NULL DEFAULT 'not_started'"),
    ("delivery_attempts", "worker_id", "TEXT"),
)

MIGRATION_2_SQL = """
CREATE TABLE IF NOT EXISTS outbound_templates (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    subject_template TEXT NOT NULL,
    text_template TEXT NOT NULL,
    html_template TEXT,
    reply_to TEXT,
    max_rendered_bytes INTEGER NOT NULL,
    max_expansion_depth INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(name, version)
);
CREATE TABLE IF NOT EXISTS sender_policies (
    ref TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    email TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    reply_to TEXT,
    max_per_minute INTEGER NOT NULL,
    max_per_hour INTEGER NOT NULL,
    max_per_day INTEGER NOT NULL,
    per_domain_per_minute INTEGER NOT NULL,
    max_concurrency INTEGER NOT NULL,
    cooldown_seconds INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    priority INTEGER NOT NULL DEFAULT 0,
    cooldown_until TEXT,
    configuration_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS campaign_schedules (
    campaign_id TEXT PRIMARY KEY REFERENCES campaigns(id) ON DELETE CASCADE,
    mode TEXT NOT NULL,
    timezone TEXT NOT NULL,
    scheduled_at TEXT,
    window_start TEXT,
    window_end TEXT,
    weekdays_json TEXT NOT NULL,
    pacing_seconds REAL NOT NULL DEFAULT 0,
    cooldown_seconds REAL NOT NULL DEFAULT 0,
    recurrence TEXT,
    catch_up_policy TEXT NOT NULL,
    fold INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prepared_messages (
    id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL UNIQUE REFERENCES delivery_intents(id) ON DELETE CASCADE,
    template_name TEXT NOT NULL,
    template_version TEXT NOT NULL,
    rendering_seed INTEGER NOT NULL,
    message_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    from_email TEXT NOT NULL,
    recipient TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    mime_bytes BLOB NOT NULL,
    attachment_metadata_json TEXT NOT NULL DEFAULT '[]',
    prepared_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS policy_reservations (
    id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL UNIQUE REFERENCES delivery_attempts(id) ON DELETE CASCADE,
    intent_id TEXT NOT NULL REFERENCES delivery_intents(id) ON DELETE CASCADE,
    sender_policy_ref TEXT NOT NULL REFERENCES sender_policies(ref),
    recipient_domain TEXT NOT NULL,
    reserved_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    released_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_policy_reservations_active
ON policy_reservations(sender_policy_ref, recipient_domain, released_at, expires_at);
CREATE INDEX IF NOT EXISTS idx_attempts_policy_time
ON delivery_attempts(sender_policy_ref, created_at);
"""

MIGRATION_3_SQL = """
CREATE TABLE IF NOT EXISTS inbound_sources (
    ref TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    mailbox TEXT,
    trust_status TEXT NOT NULL,
    retention_policy TEXT NOT NULL DEFAULT 'retain',
    configuration_json TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inbound_runs (
    id TEXT PRIMARY KEY,
    source_ref TEXT NOT NULL REFERENCES inbound_sources(ref),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    cursor_before TEXT,
    cursor_after TEXT,
    acquired_count INTEGER NOT NULL DEFAULT 0,
    processed_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT
);
CREATE TABLE IF NOT EXISTS inbound_cursors (
    source_ref TEXT PRIMARY KEY REFERENCES inbound_sources(ref),
    uidvalidity TEXT,
    cursor TEXT,
    generation INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS inbound_messages (
    id TEXT PRIMARY KEY,
    source_ref TEXT NOT NULL REFERENCES inbound_sources(ref),
    source_identity TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    raw_bytes BLOB,
    raw_retained INTEGER NOT NULL DEFAULT 1 CHECK(raw_retained IN (0, 1)),
    size_bytes INTEGER NOT NULL,
    received_at TEXT,
    message_id TEXT,
    in_reply_to TEXT,
    references_json TEXT NOT NULL DEFAULT '[]',
    sender TEXT,
    recipients_json TEXT NOT NULL DEFAULT '[]',
    subject TEXT,
    parse_status TEXT,
    parse_reason TEXT,
    headers_json TEXT NOT NULL DEFAULT '{}',
    text_preview TEXT NOT NULL DEFAULT '',
    html_preview TEXT NOT NULL DEFAULT '',
    acquired_at TEXT NOT NULL,
    processed_at TEXT,
    UNIQUE(source_ref, source_identity)
);
CREATE INDEX IF NOT EXISTS idx_inbound_messages_hash ON inbound_messages(content_hash);
CREATE TABLE IF NOT EXISTS inbound_classifications (
    id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL REFERENCES inbound_messages(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    signals_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    UNIQUE(message_id, kind)
);
CREATE TABLE IF NOT EXISTS inbound_correlations (
    id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL REFERENCES inbound_messages(id) ON DELETE CASCADE,
    entity_type TEXT,
    entity_id TEXT,
    match_kind TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    trust_status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inbound_actions (
    id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL REFERENCES inbound_messages(id) ON DELETE CASCADE,
    action_type TEXT NOT NULL,
    target TEXT,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    proposed_at TEXT NOT NULL,
    applied_at TEXT,
    decided_at TEXT,
    decision_reason TEXT
);
CREATE TABLE IF NOT EXISTS inbound_reviews (
    id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL REFERENCES inbound_messages(id) ON DELETE CASCADE,
    action_id TEXT NOT NULL REFERENCES inbound_actions(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(action_id)
);
CREATE TABLE IF NOT EXISTS inbound_dsn_reports (
    message_id TEXT PRIMARY KEY REFERENCES inbound_messages(id) ON DELETE CASCADE,
    reporting_mta TEXT,
    original_envelope_id TEXT,
    arrival_date TEXT,
    fields_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inbound_dsn_recipients (
    id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL REFERENCES inbound_messages(id) ON DELETE CASCADE,
    recipient_index INTEGER NOT NULL,
    original_recipient TEXT,
    final_recipient TEXT,
    action TEXT NOT NULL,
    status TEXT,
    diagnostic_code TEXT,
    last_attempt_date TEXT,
    retry_until TEXT,
    fields_json TEXT NOT NULL,
    UNIQUE(message_id, recipient_index)
);
CREATE TABLE IF NOT EXISTS inbound_complaints (
    message_id TEXT PRIMARY KEY REFERENCES inbound_messages(id) ON DELETE CASCADE,
    feedback_type TEXT,
    original_message_id TEXT,
    original_recipient TEXT,
    source TEXT,
    abuse_indicator INTEGER NOT NULL DEFAULT 0,
    fields_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inbound_actions_status ON inbound_actions(status);
CREATE INDEX IF NOT EXISTS idx_inbound_correlation_entity ON inbound_correlations(entity_id);
"""

MIGRATION_4_SQL = """
CREATE TABLE IF NOT EXISTS campaign_pacing (
    campaign_id TEXT PRIMARY KEY REFERENCES campaigns(id) ON DELETE CASCADE,
    next_allowed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

MIGRATION_5_SQL = """
CREATE TABLE IF NOT EXISTS authorization_evidence (
    id TEXT PRIMARY KEY,
    contact_id TEXT NOT NULL REFERENCES contacts(id),
    purpose TEXT NOT NULL,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    scope_type TEXT NOT NULL DEFAULT 'global',
    scope_id TEXT,
    recorded_at TEXT NOT NULL,
    recording_actor TEXT NOT NULL,
    evidence_ref TEXT,
    policy_version TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_authorization_evidence_lookup
ON authorization_evidence(contact_id, purpose, scope_type, scope_id, recorded_at);
CREATE TABLE IF NOT EXISTS recipient_preferences (
    id TEXT PRIMARY KEY,
    contact_id TEXT NOT NULL REFERENCES contacts(id),
    purpose TEXT NOT NULL,
    state TEXT NOT NULL,
    source TEXT NOT NULL,
    scope_type TEXT NOT NULL DEFAULT 'global',
    scope_id TEXT,
    recorded_at TEXT NOT NULL,
    recording_actor TEXT NOT NULL,
    evidence_ref TEXT,
    policy_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recipient_preferences_lookup
ON recipient_preferences(contact_id, purpose, scope_type, scope_id, recorded_at);
CREATE TABLE IF NOT EXISTS operator_declarations (
    id TEXT PRIMARY KEY,
    campaign_id TEXT REFERENCES campaigns(id),
    action TEXT NOT NULL,
    declaration TEXT NOT NULL,
    actor TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_operator_declarations_campaign
ON operator_declarations(campaign_id, occurred_at);
CREATE TABLE IF NOT EXISTS suppression_history (
    id TEXT PRIMARY KEY,
    suppression_id TEXT NOT NULL REFERENCES suppressions(id),
    action TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS lifecycle_records (
    id TEXT PRIMARY KEY,
    contact_id TEXT NOT NULL REFERENCES contacts(id),
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    retention_conflict INTEGER NOT NULL DEFAULT 0 CHECK(retention_conflict IN (0, 1)),
    requested_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_lifecycle_contact ON lifecycle_records(contact_id, requested_at);
CREATE TABLE IF NOT EXISTS policy_state (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    revision INTEGER NOT NULL
);
INSERT OR IGNORE INTO policy_state(id, revision) VALUES (1, 0);
"""

MIGRATION_6_SQL = """
CREATE TABLE IF NOT EXISTS provider_profiles (
    ref TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    host TEXT NOT NULL,
    port INTEGER NOT NULL,
    username TEXT,
    credential_ref TEXT,
    tls_mode TEXT NOT NULL,
    auth_method TEXT NOT NULL,
    mailbox TEXT,
    sender_policy_ref TEXT REFERENCES sender_policies(ref),
    timeout_seconds REAL NOT NULL,
    max_message_bytes INTEGER NOT NULL,
    ca_file TEXT,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    suspension_until TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_diagnostics (
    id TEXT PRIMARY KEY,
    profile_ref TEXT NOT NULL REFERENCES provider_profiles(ref) ON DELETE CASCADE,
    status TEXT NOT NULL,
    phase TEXT NOT NULL,
    detail TEXT NOT NULL,
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    observed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_provider_diagnostics_profile
ON provider_diagnostics(profile_ref, observed_at DESC);
"""

MIGRATION_7_SQL = """
CREATE TABLE IF NOT EXISTS contact_field_definitions (
    id TEXT PRIMARY KEY,
    machine_key TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    field_type TEXT NOT NULL CHECK(field_type IN ('TEXT', 'NUMBER', 'BOOLEAN', 'DATE', 'DATETIME')),
    validation_json TEXT NOT NULL DEFAULT '{}',
    controlled_values_json TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'archived')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    updated_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contact_attribute_values (
    contact_id TEXT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    field_id TEXT NOT NULL REFERENCES contact_field_definitions(id),
    value_type TEXT NOT NULL CHECK(value_type IN ('TEXT', 'NUMBER', 'BOOLEAN', 'DATE', 'DATETIME')),
    text_value TEXT,
    number_value INTEGER,
    boolean_value INTEGER CHECK(boolean_value IN (0, 1)),
    date_value TEXT,
    datetime_value TEXT,
    source_kind TEXT NOT NULL,
    source_ref TEXT,
    source_record_position INTEGER,
    provenance_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    PRIMARY KEY(contact_id, field_id),
    CHECK(
        (value_type = 'TEXT' AND text_value IS NOT NULL AND number_value IS NULL AND boolean_value IS NULL AND date_value IS NULL AND datetime_value IS NULL)
        OR (value_type = 'NUMBER' AND text_value IS NULL AND number_value IS NOT NULL AND boolean_value IS NULL AND date_value IS NULL AND datetime_value IS NULL)
        OR (value_type = 'BOOLEAN' AND text_value IS NULL AND number_value IS NULL AND boolean_value IS NOT NULL AND date_value IS NULL AND datetime_value IS NULL)
        OR (value_type = 'DATE' AND text_value IS NULL AND number_value IS NULL AND boolean_value IS NULL AND date_value IS NOT NULL AND datetime_value IS NULL)
        OR (value_type = 'DATETIME' AND text_value IS NULL AND number_value IS NULL AND boolean_value IS NULL AND date_value IS NULL AND datetime_value IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_attribute_field_number
ON contact_attribute_values(field_id, number_value, contact_id);
CREATE INDEX IF NOT EXISTS idx_attribute_field_text
ON contact_attribute_values(field_id, text_value, contact_id);
CREATE TABLE IF NOT EXISTS contact_attribute_conflicts (
    id TEXT PRIMARY KEY,
    contact_id TEXT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    field_id TEXT NOT NULL REFERENCES contact_field_definitions(id),
    source_kind TEXT NOT NULL,
    source_ref TEXT,
    source_record_position INTEGER,
    existing_summary TEXT NOT NULL,
    incoming_summary TEXT NOT NULL,
    disposition TEXT NOT NULL,
    reason TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attribute_conflicts_contact
ON contact_attribute_conflicts(contact_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_attribute_conflict_source_row
ON contact_attribute_conflicts(source_ref, source_record_position, contact_id, field_id);
CREATE TABLE IF NOT EXISTS list_membership_history (
    id TEXT PRIMARY KEY,
    list_id TEXT NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    contact_id TEXT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    action TEXT NOT NULL CHECK(action IN ('add', 'remove')),
    occurred_at TEXT NOT NULL,
    source TEXT NOT NULL,
    actor TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    reason TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_membership_history_list_contact
ON list_membership_history(list_id, contact_id, occurred_at);
CREATE TABLE IF NOT EXISTS segments (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'archived')),
    current_revision_id TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    updated_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS segment_revisions (
    id TEXT PRIMARY KEY,
    segment_id TEXT NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
    revision_number INTEGER NOT NULL,
    definition_version INTEGER NOT NULL,
    definition_json TEXT NOT NULL,
    definition_hash TEXT NOT NULL,
    change_reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    UNIQUE(segment_id, revision_number),
    UNIQUE(segment_id, definition_hash)
);
CREATE INDEX IF NOT EXISTS idx_segment_revisions_segment
ON segment_revisions(segment_id, revision_number DESC);
CREATE TABLE IF NOT EXISTS campaign_audiences (
    id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    run_key TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK(source_type IN ('list', 'segment')),
    source_id TEXT NOT NULL,
    segment_revision_id TEXT,
    definition_hash TEXT,
    evaluated_at TEXT NOT NULL,
    evaluation_clock TEXT NOT NULL,
    matched_count INTEGER NOT NULL DEFAULT 0,
    prepared_count INTEGER NOT NULL DEFAULT 0,
    excluded_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK(status IN ('preparing', 'prepared', 'failed')),
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(campaign_id, run_key)
);
CREATE INDEX IF NOT EXISTS idx_campaign_audiences_source
ON campaign_audiences(source_type, source_id, evaluated_at);
CREATE TABLE IF NOT EXISTS campaign_audience_recipients (
    audience_id TEXT NOT NULL REFERENCES campaign_audiences(id) ON DELETE CASCADE,
    contact_id TEXT NOT NULL REFERENCES contacts(id),
    normalized_email TEXT NOT NULL,
    state TEXT NOT NULL,
    eligibility_reason TEXT NOT NULL,
    prepared_at TEXT NOT NULL,
    PRIMARY KEY(audience_id, contact_id)
);
CREATE INDEX IF NOT EXISTS idx_audience_recipients_contact
ON campaign_audience_recipients(audience_id, normalized_email);
"""

MIGRATION_8_SQL = """
CREATE TABLE IF NOT EXISTS automation_workflows (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK(status IN ('draft', 'active', 'paused', 'archived')),
    reentry_policy TEXT NOT NULL DEFAULT 'once_per_contact',
    current_draft_revision_id TEXT,
    active_revision_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    updated_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS automation_revisions (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES automation_workflows(id) ON DELETE CASCADE,
    revision_number INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft', 'published')),
    graph_json TEXT NOT NULL,
    graph_hash TEXT NOT NULL,
    limits_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    published_at TEXT,
    created_by TEXT NOT NULL,
    UNIQUE(workflow_id, revision_number),
    UNIQUE(workflow_id, graph_hash)
);
CREATE TABLE IF NOT EXISTS automation_triggers (
    id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL REFERENCES automation_revisions(id) ON DELETE CASCADE,
    trigger_key TEXT NOT NULL,
    trigger_kind TEXT NOT NULL,
    config_json TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    UNIQUE(revision_id, trigger_key)
);
CREATE TABLE IF NOT EXISTS automation_nodes (
    id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL REFERENCES automation_revisions(id) ON DELETE CASCADE,
    node_key TEXT NOT NULL,
    node_kind TEXT NOT NULL,
    label TEXT NOT NULL,
    config_json TEXT NOT NULL DEFAULT '{}',
    position_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(revision_id, node_key)
);
CREATE TABLE IF NOT EXISTS automation_edges (
    id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL REFERENCES automation_revisions(id) ON DELETE CASCADE,
    source_key TEXT NOT NULL,
    target_key TEXT NOT NULL,
    branch_key TEXT NOT NULL DEFAULT 'default',
    edge_order INTEGER NOT NULL DEFAULT 0,
    UNIQUE(revision_id, source_key, target_key, branch_key)
);
CREATE TABLE IF NOT EXISTS automation_source_events (
    id TEXT PRIMARY KEY,
    source_identity TEXT NOT NULL,
    event_kind TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    contact_id TEXT REFERENCES contacts(id),
    payload_json TEXT NOT NULL DEFAULT '{}',
    provenance_json TEXT NOT NULL DEFAULT '{}',
    correlation_id TEXT,
    occurred_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS automation_enrollments (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES automation_workflows(id),
    revision_id TEXT NOT NULL REFERENCES automation_revisions(id),
    source_event_id TEXT NOT NULL REFERENCES automation_source_events(id),
    contact_id TEXT REFERENCES contacts(id),
    subject_key TEXT NOT NULL,
    trigger_key TEXT NOT NULL,
    reentry_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'enrolled',
    provenance_json TEXT NOT NULL DEFAULT '{}',
    correlation_id TEXT,
    enrolled_at TEXT NOT NULL,
    UNIQUE(workflow_id, source_event_id, trigger_key),
    UNIQUE(workflow_id, contact_id, reentry_key)
);
CREATE TABLE IF NOT EXISTS automation_runs (
    id TEXT PRIMARY KEY,
    enrollment_id TEXT NOT NULL UNIQUE REFERENCES automation_enrollments(id) ON DELETE CASCADE,
    workflow_id TEXT NOT NULL REFERENCES automation_workflows(id),
    revision_id TEXT NOT NULL REFERENCES automation_revisions(id),
    status TEXT NOT NULL,
    current_node_key TEXT NOT NULL,
    started_at TEXT,
    waiting_since TEXT,
    due_at_utc TEXT,
    completed_at TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    failure_reason TEXT,
    worker_id TEXT,
    lease_expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS automation_step_history (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES automation_runs(id) ON DELETE CASCADE,
    node_key TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    status TEXT NOT NULL,
    side_effect_phase TEXT NOT NULL DEFAULT 'none',
    input_json TEXT NOT NULL DEFAULT '{}',
    output_json TEXT NOT NULL DEFAULT '{}',
    error_code TEXT,
    error_message TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE(run_id, node_key, attempt_number)
);
CREATE TABLE IF NOT EXISTS automation_waits (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES automation_runs(id) ON DELETE CASCADE,
    node_key TEXT NOT NULL,
    due_at_utc TEXT NOT NULL,
    timezone TEXT NOT NULL,
    local_value TEXT NOT NULL,
    catch_up_policy TEXT NOT NULL,
    resumed_at TEXT,
    UNIQUE(run_id, node_key)
);
CREATE TABLE IF NOT EXISTS automation_action_correlations (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES automation_runs(id) ON DELETE CASCADE,
    node_key TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    action_kind TEXT NOT NULL,
    target_key TEXT,
    correlation_id TEXT,
    outcome TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS automation_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT,
    revision_id TEXT,
    run_id TEXT,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_automation_revisions_workflow
ON automation_revisions(workflow_id, revision_number DESC);
CREATE INDEX IF NOT EXISTS idx_automation_runs_status_due
ON automation_runs(status, due_at_utc, updated_at);
CREATE INDEX IF NOT EXISTS idx_automation_events_subject
ON automation_source_events(subject_key, event_kind, occurred_at);
CREATE INDEX IF NOT EXISTS idx_automation_audit_run
ON automation_audit(run_id, occurred_at);
"""

MIGRATION_9_SQL = """
CREATE TABLE IF NOT EXISTS automation_event_cursor (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    last_event_id INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
INSERT OR IGNORE INTO automation_event_cursor(id, last_event_id, updated_at)
VALUES (1, 0, CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS automation_schedule_state (
    workflow_id TEXT NOT NULL REFERENCES automation_workflows(id) ON DELETE CASCADE,
    revision_id TEXT NOT NULL REFERENCES automation_revisions(id) ON DELETE CASCADE,
    trigger_key TEXT NOT NULL,
    next_due_at_utc TEXT NOT NULL,
    last_occurrence_key TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(workflow_id, revision_id, trigger_key)
);
CREATE INDEX IF NOT EXISTS idx_automation_schedule_due
ON automation_schedule_state(next_due_at_utc);
CREATE TABLE IF NOT EXISTS list_processing_jobs (
    id TEXT PRIMARY KEY,
    source_filename TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    profile TEXT NOT NULL,
    dns_mode TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'paused', 'completed', 'failed', 'canceled')),
    processed_count INTEGER NOT NULL DEFAULT 0,
    accepted_count INTEGER NOT NULL DEFAULT 0,
    summary_json TEXT NOT NULL DEFAULT '{}',
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    checkpoint_path TEXT NOT NULL,
    job_dir TEXT NOT NULL,
    correlation_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_list_processing_jobs_status
ON list_processing_jobs(status, updated_at DESC);
CREATE TABLE IF NOT EXISTS list_processing_artifacts (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES list_processing_jobs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    provider_key TEXT,
    filename TEXT NOT NULL,
    path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(job_id, kind, provider_key)
);
CREATE INDEX IF NOT EXISTS idx_list_processing_artifacts_job
ON list_processing_artifacts(job_id, kind, filename);
"""

MIGRATION_10_SQL = """
CREATE TABLE IF NOT EXISTS message_documents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('message', 'template')),
    status TEXT NOT NULL CHECK(status IN ('draft', 'active', 'archived')),
    current_draft_revision_id TEXT,
    current_published_revision_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT 'local-operator',
    updated_by TEXT NOT NULL DEFAULT 'local-operator'
);
CREATE INDEX IF NOT EXISTS idx_message_documents_status
ON message_documents(status, updated_at DESC);
CREATE TABLE IF NOT EXISTS message_revisions (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES message_documents(id) ON DELETE CASCADE,
    revision_number INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft', 'published')),
    document_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    published_at TEXT,
    created_by TEXT NOT NULL DEFAULT 'local-operator',
    UNIQUE(document_id, revision_number)
);
CREATE INDEX IF NOT EXISTS idx_message_revisions_document
ON message_revisions(document_id, revision_number DESC);
CREATE TABLE IF NOT EXISTS message_assets (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('image', 'attachment')),
    filename TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    data_blob BLOB NOT NULL,
    created_at TEXT NOT NULL,
    archived_at TEXT,
    UNIQUE(kind, sha256)
);
CREATE INDEX IF NOT EXISTS idx_message_assets_kind
ON message_assets(kind, created_at DESC);
CREATE TABLE IF NOT EXISTS message_recipient_attachments (
    revision_id TEXT NOT NULL REFERENCES message_revisions(id) ON DELETE CASCADE,
    contact_id TEXT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    asset_id TEXT NOT NULL REFERENCES message_assets(id) ON DELETE RESTRICT,
    required INTEGER NOT NULL DEFAULT 1 CHECK(required IN (0, 1)),
    PRIMARY KEY(revision_id, contact_id, asset_id)
);
CREATE TABLE IF NOT EXISTS message_campaign_snapshots (
    id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    run_key TEXT NOT NULL,
    revision_id TEXT NOT NULL REFERENCES message_revisions(id),
    content_hash TEXT NOT NULL,
    document_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(campaign_id, run_key)
);
CREATE INDEX IF NOT EXISTS idx_message_campaign_snapshots_campaign
ON message_campaign_snapshots(campaign_id, run_key);
"""

MIGRATION_11_SQL = """
CREATE TABLE IF NOT EXISTS provider_accounts (
    ref TEXT PRIMARY KEY REFERENCES provider_profiles(ref) ON DELETE CASCADE,
    display_name TEXT NOT NULL,
    sender_email TEXT,
    status TEXT NOT NULL DEFAULT 'not_configured' CHECK(status IN (
        'not_configured', 'configuration_validated', 'locally_protocol_tested',
        'external_connected', 'authentication_verified', 'ready', 'suspended',
        'failed', 'reauthorization_required', 'external_qualification_pending'
    )),
    pacing_per_minute INTEGER NOT NULL DEFAULT 60 CHECK(pacing_per_minute > 0),
    max_concurrency INTEGER NOT NULL DEFAULT 1 CHECK(max_concurrency > 0),
    outbound_enabled INTEGER NOT NULL DEFAULT 1 CHECK(outbound_enabled IN (0, 1)),
    inbound_enabled INTEGER NOT NULL DEFAULT 0 CHECK(inbound_enabled IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_inbound_settings (
    account_ref TEXT PRIMARY KEY REFERENCES provider_accounts(ref) ON DELETE CASCADE,
    mailbox TEXT NOT NULL DEFAULT 'INBOX',
    read_only INTEGER NOT NULL DEFAULT 1 CHECK(read_only IN (0, 1)),
    max_fetch INTEGER NOT NULL DEFAULT 100 CHECK(max_fetch > 0),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_operation_grants (
    id TEXT PRIMARY KEY,
    account_ref TEXT NOT NULL REFERENCES provider_accounts(ref) ON DELETE CASCADE,
    operation TEXT NOT NULL,
    host TEXT NOT NULL,
    port INTEGER NOT NULL,
    purpose TEXT NOT NULL,
    sender TEXT,
    recipient TEXT,
    max_submissions INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    authorization_ref TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_provider_grants_account
ON provider_operation_grants(account_ref, expires_at DESC);
CREATE TABLE IF NOT EXISTS provider_test_submissions (
    id TEXT PRIMARY KEY,
    account_ref TEXT NOT NULL REFERENCES provider_accounts(ref),
    idempotency_key TEXT NOT NULL UNIQUE,
    sender TEXT NOT NULL,
    recipient TEXT NOT NULL,
    message_revision_id TEXT,
    status TEXT NOT NULL,
    phase TEXT NOT NULL,
    detail TEXT NOT NULL,
    response_code INTEGER,
    external_access INTEGER NOT NULL DEFAULT 0 CHECK(external_access IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_provider_tests_account
ON provider_test_submissions(account_ref, created_at DESC);
CREATE TABLE IF NOT EXISTS provider_oauth_flows (
    id TEXT PRIMARY KEY,
    account_ref TEXT NOT NULL REFERENCES provider_accounts(ref) ON DELETE CASCADE,
    state_hash TEXT NOT NULL UNIQUE,
    pkce_challenge TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    scopes_json TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    identity TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_provider_oauth_flows_expiry
ON provider_oauth_flows(expires_at, status);
INSERT OR IGNORE INTO provider_accounts(
    ref, display_name, sender_email, status, pacing_per_minute, max_concurrency,
    outbound_enabled, inbound_enabled, created_at, updated_at
)
SELECT ref, ref, NULL, 'not_configured', 60, 1, enabled, 0, created_at, updated_at
FROM provider_profiles;
INSERT OR IGNORE INTO provider_inbound_settings(account_ref, mailbox, read_only, max_fetch, updated_at)
SELECT ref, COALESCE(mailbox, 'INBOX'), 1, 100, updated_at FROM provider_profiles;
"""

MIGRATION_12_SQL = """
CREATE INDEX IF NOT EXISTS idx_inbound_messages_sender ON inbound_messages(sender);
CREATE INDEX IF NOT EXISTS idx_inbound_messages_subject ON inbound_messages(subject);
CREATE INDEX IF NOT EXISTS idx_inbound_messages_received ON inbound_messages(received_at DESC);
CREATE TABLE IF NOT EXISTS inbound_attachments (
    id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL REFERENCES inbound_messages(id) ON DELETE CASCADE,
    part_path TEXT NOT NULL,
    filename TEXT,
    media_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    content_id TEXT,
    disposition TEXT,
    sha256 TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(message_id, part_path)
);
CREATE INDEX IF NOT EXISTS idx_inbound_attachments_message
ON inbound_attachments(message_id, filename);
CREATE TABLE IF NOT EXISTS inbound_extracted_values (
    id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL REFERENCES inbound_messages(id) ON DELETE CASCADE,
    field_key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    source_path TEXT NOT NULL,
    provenance_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(message_id, field_key)
);
CREATE TABLE IF NOT EXISTS inbound_rules (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'paused' CHECK(status IN ('active', 'paused', 'archived')),
    priority INTEGER NOT NULL DEFAULT 0,
    definition_json TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inbound_rules_status
ON inbound_rules(status, priority DESC, id);
CREATE TABLE IF NOT EXISTS inbound_rule_runs (
    id TEXT PRIMARY KEY,
    rule_id TEXT NOT NULL REFERENCES inbound_rules(id) ON DELETE CASCADE,
    message_id TEXT NOT NULL REFERENCES inbound_messages(id) ON DELETE CASCADE,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK(status IN ('matched', 'skipped', 'applied', 'failed', 'preview')),
    actions_json TEXT NOT NULL DEFAULT '[]',
    error_message TEXT,
    occurred_at TEXT NOT NULL,
    UNIQUE(rule_id, message_id)
);
CREATE TABLE IF NOT EXISTS inbound_drafts (
    id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL REFERENCES inbound_messages(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('reply', 'forward')),
    document_id TEXT NOT NULL REFERENCES message_documents(id),
    revision_id TEXT NOT NULL REFERENCES message_revisions(id),
    account_ref TEXT,
    subject TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft', 'ready', 'sent', 'canceled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(message_id, kind)
);
CREATE TABLE IF NOT EXISTS inbound_mailbox_actions (
    id TEXT PRIMARY KEY,
    source_ref TEXT NOT NULL REFERENCES inbound_sources(ref),
    message_id TEXT REFERENCES inbound_messages(id) ON DELETE SET NULL,
    action TEXT NOT NULL CHECK(action IN ('mark_read', 'move', 'copy')),
    target_folder TEXT,
    authorization_ref TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'requested' CHECK(status IN ('requested', 'applied', 'failed', 'ambiguous', 'canceled')),
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inbound_mailbox_actions_source
ON inbound_mailbox_actions(source_ref, created_at DESC);
"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _assert_no_secrets(value: Any, path: str = "config") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if SENSITIVE_KEY.search(str(key)):
                raise ValueError(
                    f"secret-bearing field is not allowed in durable state: {path}.{key}"
                )
            _assert_no_secrets(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_secrets(child, f"{path}[{index}]")
    elif isinstance(value, str) and SENSITIVE_KEY.search(value):
        raise ValueError(f"secret-bearing value is not allowed in durable state: {path}")


def _assert_safe_reference(value: str | None, path: str) -> None:
    if value and SENSITIVE_KEY.search(value):
        raise ValueError(f"secret-bearing reference is not allowed in durable state: {path}")


def _assert_credential_reference(value: str | None, path: str) -> None:
    if value is None:
        return
    try:
        validate_credential_reference(value)
    except ValueError as error:
        raise ValueError(f"invalid credential reference at {path}") from error


def _validated_email(value: str) -> str:
    normalized = normalize_email(value)
    if not validate_email(normalized).valid:
        raise ValueError(f"invalid email identity: {value}")
    return normalized


class CampaignStore:
    """Small explicit SQLite adapter with one schema version and safe defaults."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=5.0,
            isolation_level=None,
            check_same_thread=True,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        if str(self.path) != ":memory:":
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    @contextlib.contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def migrate(self) -> int:
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            current = connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
            ).fetchone()["version"]
            if current > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema {current} is newer than supported {SCHEMA_VERSION}"
                )
            if current < 1:
                connection.executescript(SCHEMA_SQL)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (1, utc_now()),
                )
                current = 1
            if current < 2:
                self._apply_migration_2(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (2, utc_now()),
                )
                current = 2
            if current < 3:
                self._apply_migration_3(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (3, utc_now()),
                )
                current = 3
            if current < 4:
                self._apply_migration_4(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (4, utc_now()),
                )
                current = 4
            if current < 5:
                self._apply_migration_5(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (5, utc_now()),
                )
                current = 5
            if current < 6:
                self._apply_migration_6(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (6, utc_now()),
                )
                current = 6
            if current < 7:
                self._apply_migration_7(connection)
                columns = {
                    row["name"] for row in connection.execute("PRAGMA table_info(campaigns)")
                }
                if "audience_source_type" not in columns:
                    connection.execute(
                        "ALTER TABLE campaigns ADD COLUMN audience_source_type TEXT NOT NULL DEFAULT 'list'"
                    )
                if "audience_source_id" not in columns:
                    connection.execute("ALTER TABLE campaigns ADD COLUMN audience_source_id TEXT")
                if "audience_revision_id" not in columns:
                    connection.execute("ALTER TABLE campaigns ADD COLUMN audience_revision_id TEXT")
                if "audience_run_key" not in columns:
                    connection.execute(
                        "ALTER TABLE campaigns ADD COLUMN audience_run_key TEXT NOT NULL DEFAULT 'primary'"
                    )
                connection.execute(
                    "UPDATE campaigns SET audience_source_id = audience_list_id "
                    "WHERE audience_source_id IS NULL"
                )
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (7, utc_now()),
                )
                current = 7
            if current < 8:
                self._apply_migration_8(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (8, utc_now()),
                )
                current = 8
            if current < 9:
                self._apply_migration_9(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (9, utc_now()),
                )
                current = 9
            if current < 10:
                columns = {
                    row["name"] for row in connection.execute("PRAGMA table_info(campaigns)")
                }
                if "message_revision_id" not in columns:
                    connection.execute("ALTER TABLE campaigns ADD COLUMN message_revision_id TEXT")
                self._apply_migration_10(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (10, utc_now()),
                )
                current = 10
            if current < 11:
                columns = {
                    row["name"] for row in connection.execute("PRAGMA table_info(campaigns)")
                }
                if "provider_account_ref" not in columns:
                    connection.execute("ALTER TABLE campaigns ADD COLUMN provider_account_ref TEXT")
                diagnostic_columns = {
                    row["name"]
                    for row in connection.execute("PRAGMA table_info(provider_diagnostics)")
                }
                if "operation" not in diagnostic_columns:
                    connection.execute(
                        "ALTER TABLE provider_diagnostics ADD COLUMN operation TEXT NOT NULL DEFAULT 'local_protocol'"
                    )
                if "external_access" not in diagnostic_columns:
                    connection.execute(
                        "ALTER TABLE provider_diagnostics ADD COLUMN external_access INTEGER NOT NULL DEFAULT 0"
                    )
                if "correlation_id" not in diagnostic_columns:
                    connection.execute(
                        "ALTER TABLE provider_diagnostics ADD COLUMN correlation_id TEXT"
                    )
                if "metadata_json" not in diagnostic_columns:
                    connection.execute(
                        "ALTER TABLE provider_diagnostics ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'"
                    )
                self._apply_migration_11(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (11, utc_now()),
                )
                current = 11
            if current < 12:
                inbound_columns = {
                    row["name"] for row in connection.execute("PRAGMA table_info(inbound_messages)")
                }
                for column, definition in (
                    ("account_ref", "TEXT"),
                    ("folder", "TEXT"),
                    ("thread_key", "TEXT"),
                    ("retention_state", "TEXT NOT NULL DEFAULT 'retained'"),
                ):
                    if column not in inbound_columns:
                        connection.execute(
                            f"ALTER TABLE inbound_messages ADD COLUMN {column} {definition}"
                        )
                self._apply_migration_12(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (12, utc_now()),
                )
            return SCHEMA_VERSION

    @staticmethod
    def _apply_migration_2(connection: sqlite3.Connection) -> None:
        for table, column, definition in MIGRATION_2_COLUMNS:
            columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        connection.executescript(MIGRATION_2_SQL)

    @staticmethod
    def _apply_migration_3(connection: sqlite3.Connection) -> None:
        connection.executescript(MIGRATION_3_SQL)

    @staticmethod
    def _apply_migration_4(connection: sqlite3.Connection) -> None:
        connection.executescript(MIGRATION_4_SQL)

    @staticmethod
    def _apply_migration_5(connection: sqlite3.Connection) -> None:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(contacts)")}
        if "lifecycle_status" not in columns:
            connection.execute(
                "ALTER TABLE contacts ADD COLUMN lifecycle_status TEXT NOT NULL DEFAULT 'active'"
            )
        if "deactivated_at" not in columns:
            connection.execute("ALTER TABLE contacts ADD COLUMN deactivated_at TEXT")
        connection.executescript(MIGRATION_5_SQL)

    @staticmethod
    def _apply_migration_6(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(delivery_attempts)")
        }
        if "response_code" not in columns:
            connection.execute("ALTER TABLE delivery_attempts ADD COLUMN response_code INTEGER")
        connection.executescript(MIGRATION_6_SQL)

    @staticmethod
    def _apply_migration_7(connection: sqlite3.Connection) -> None:
        # sqlite3.executescript() commits any open transaction before running
        # the script. Execute each migration statement on the caller's
        # transaction so a failed upgrade rolls back as one unit.
        for statement in MIGRATION_7_SQL.split(";"):
            statement = statement.strip()
            if statement:
                connection.execute(statement)

    @staticmethod
    def _apply_migration_8(connection: sqlite3.Connection) -> None:
        # Keep migration 8 inside the caller transaction; executescript would
        # implicitly commit and make a failed upgrade difficult to recover.
        for statement in MIGRATION_8_SQL.split(";"):
            statement = statement.strip()
            if statement:
                connection.execute(statement)

    @staticmethod
    def _apply_migration_9(connection: sqlite3.Connection) -> None:
        for statement in MIGRATION_9_SQL.split(";"):
            statement = statement.strip()
            if statement:
                connection.execute(statement)

    @staticmethod
    def _apply_migration_10(connection: sqlite3.Connection) -> None:
        for statement in MIGRATION_10_SQL.split(";"):
            statement = statement.strip()
            if statement:
                connection.execute(statement)

    @staticmethod
    def _apply_migration_11(connection: sqlite3.Connection) -> None:
        for statement in MIGRATION_11_SQL.split(";"):
            statement = statement.strip()
            if statement:
                connection.execute(statement)

    @staticmethod
    def _apply_migration_12(connection: sqlite3.Connection) -> None:
        for statement in MIGRATION_12_SQL.split(";"):
            statement = statement.strip()
            if statement:
                connection.execute(statement)

    def schema_version(self) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
            ).fetchone()
            return int(row["version"])

    def policy_revision(self) -> int:
        with self.connect() as connection:
            row = connection.execute("SELECT revision FROM policy_state WHERE id = 1").fetchone()
        return int(row["revision"])

    @staticmethod
    def _bump_policy_revision(connection: sqlite3.Connection) -> None:
        connection.execute("UPDATE policy_state SET revision = revision + 1 WHERE id = 1")

    def integrity_check(self) -> bool:
        with self.connect() as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"SQLite integrity check failed: {result}")
        return True

    def backup_to(self, destination: str | Path) -> Path:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = self.connect()
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
            target.commit()
        finally:
            target.close()
            source.close()
        CampaignStore(destination).integrity_check()
        return destination

    @staticmethod
    def restore_from(source: str | Path, destination: str | Path) -> Path:
        source = Path(source)
        if not source.is_file():
            raise FileNotFoundError(f"SQLite backup source does not exist: {source}")
        source_store = CampaignStore(source)
        source_store.integrity_check()
        return source_store.backup_to(destination)

    def register_template(self, definition: TemplateDefinition) -> str:
        _assert_no_secrets({"name": definition.name, "version": definition.version}, "template")
        template_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"template:{definition.name}:{definition.version}")
        )
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO outbound_templates "
                "(id, name, version, subject_template, text_template, html_template, reply_to, "
                "max_rendered_bytes, max_expansion_depth, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    template_id,
                    definition.name,
                    definition.version,
                    definition.subject,
                    definition.text,
                    definition.html,
                    definition.reply_to,
                    definition.max_rendered_bytes,
                    definition.max_expansion_depth,
                    utc_now(),
                ),
            )
        return template_id

    def get_template(self, name: str, version: str) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM outbound_templates WHERE name = ? AND version = ?",
                (name, version),
            ).fetchone()
        if row is None:
            raise KeyError(f"template not found: {name}@{version}")
        return row

    def register_sender_policy(
        self, policy: SenderPolicy, configuration: dict[str, Any] | None = None
    ) -> None:
        _assert_safe_reference(policy.ref, "sender_policy.ref")
        configuration = dict(configuration or {})
        configuration["per_domain_per_hour"] = policy.per_domain_per_hour
        _assert_no_secrets(configuration, "sender_policy.configuration")
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO sender_policies "
                "(ref, provider, email, display_name, reply_to, max_per_minute, max_per_hour, "
                "max_per_day, per_domain_per_minute, max_concurrency, cooldown_seconds, enabled, "
                "priority, configuration_json, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(ref) DO UPDATE SET provider = excluded.provider, email = excluded.email, "
                "display_name = excluded.display_name, reply_to = excluded.reply_to, "
                "max_per_minute = excluded.max_per_minute, max_per_hour = excluded.max_per_hour, "
                "max_per_day = excluded.max_per_day, per_domain_per_minute = excluded.per_domain_per_minute, "
                "max_concurrency = excluded.max_concurrency, cooldown_seconds = excluded.cooldown_seconds, "
                "enabled = excluded.enabled, priority = excluded.priority, "
                "configuration_json = excluded.configuration_json, updated_at = excluded.updated_at",
                (
                    policy.ref,
                    policy.provider,
                    policy.email,
                    policy.display_name,
                    policy.reply_to,
                    policy.max_per_minute,
                    policy.max_per_hour,
                    policy.max_per_day,
                    policy.per_domain_per_minute,
                    policy.max_concurrency,
                    policy.cooldown_seconds,
                    int(policy.enabled),
                    policy.priority,
                    _json(configuration),
                    utc_now(),
                ),
            )

    def list_sender_policies(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(connection.execute("SELECT * FROM sender_policies ORDER BY priority, ref"))

    def register_provider_profile(self, profile: ProviderProfile) -> None:
        profile.validate()
        _assert_safe_reference(profile.ref, "provider_profile.ref")
        _assert_safe_reference(profile.host, "provider_profile.host")
        _assert_safe_reference(profile.username, "provider_profile.username")
        _assert_credential_reference(profile.credential_ref, "provider_profile.credential_ref")
        _assert_safe_reference(profile.sender_policy_ref, "provider_profile.sender_policy_ref")
        _assert_safe_reference(profile.ca_file, "provider_profile.ca_file")
        if profile.metadata:
            raise ValueError(
                "provider profile metadata is not persisted; use explicit secret-free fields"
            )
        now = utc_now()
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO provider_profiles "
                "(ref, kind, host, port, username, credential_ref, tls_mode, auth_method, mailbox, "
                "sender_policy_ref, timeout_seconds, max_message_bytes, ca_file, enabled, "
                "metadata_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(ref) DO UPDATE SET kind = excluded.kind, host = excluded.host, "
                "port = excluded.port, username = excluded.username, credential_ref = excluded.credential_ref, "
                "tls_mode = excluded.tls_mode, auth_method = excluded.auth_method, mailbox = excluded.mailbox, "
                "sender_policy_ref = excluded.sender_policy_ref, timeout_seconds = excluded.timeout_seconds, "
                "max_message_bytes = excluded.max_message_bytes, ca_file = excluded.ca_file, "
                "enabled = excluded.enabled, metadata_json = excluded.metadata_json, updated_at = excluded.updated_at",
                (
                    profile.ref,
                    profile.kind.value,
                    profile.host,
                    profile.port,
                    profile.username,
                    profile.credential_ref,
                    profile.tls_mode.value,
                    profile.auth_method.value,
                    profile.mailbox,
                    profile.sender_policy_ref,
                    profile.timeout,
                    profile.max_message_bytes,
                    profile.ca_file,
                    int(profile.enabled),
                    _json(profile.metadata or {}),
                    now,
                    now,
                ),
            )

    def upsert_provider_account(
        self,
        ref: str,
        *,
        display_name: str | None = None,
        sender_email: str | None = None,
        status: str = "not_configured",
        pacing_per_minute: int = 60,
        max_concurrency: int = 1,
        outbound_enabled: bool = True,
        inbound_enabled: bool = False,
    ) -> dict[str, Any]:
        _assert_safe_reference(ref, "provider_account.ref")
        if not display_name or not display_name.strip():
            display_name = ref
        if sender_email is not None:
            sender_email = _validated_email(sender_email)
        if pacing_per_minute <= 0 or max_concurrency <= 0:
            raise ValueError("provider account limits must be positive")
        now = utc_now()
        with self.transaction(immediate=True) as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM provider_profiles WHERE ref = ?", (ref,)
                ).fetchone()
                is None
            ):
                raise KeyError(f"provider profile not found: {ref}")
            connection.execute(
                "INSERT INTO provider_accounts(ref, display_name, sender_email, status, pacing_per_minute, "
                "max_concurrency, outbound_enabled, inbound_enabled, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(ref) DO UPDATE SET display_name=excluded.display_name, "
                "sender_email=excluded.sender_email, status=excluded.status, "
                "pacing_per_minute=excluded.pacing_per_minute, max_concurrency=excluded.max_concurrency, "
                "outbound_enabled=excluded.outbound_enabled, inbound_enabled=excluded.inbound_enabled, "
                "updated_at=excluded.updated_at",
                (
                    ref,
                    display_name.strip(),
                    sender_email,
                    status,
                    pacing_per_minute,
                    max_concurrency,
                    int(outbound_enabled),
                    int(inbound_enabled),
                    now,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO provider_inbound_settings(account_ref, mailbox, read_only, max_fetch, updated_at) "
                "VALUES (?, 'INBOX', 1, 100, ?) ON CONFLICT(account_ref) DO NOTHING",
                (ref, now),
            )
        return self.get_provider_account(ref)

    def get_provider_account(self, ref: str) -> dict[str, Any]:
        _assert_safe_reference(ref, "provider_account.ref")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT a.*, p.kind, p.host, p.port, p.username, p.credential_ref, p.tls_mode, "
                "p.auth_method, p.mailbox, p.sender_policy_ref, p.timeout_seconds, "
                "p.max_message_bytes, p.ca_file, p.enabled AS profile_enabled "
                "FROM provider_accounts a JOIN provider_profiles p ON p.ref = a.ref WHERE a.ref = ?",
                (ref,),
            ).fetchone()
            if row is None:
                raise KeyError(f"provider account not found: {ref}")
            inbound = connection.execute(
                "SELECT * FROM provider_inbound_settings WHERE account_ref = ?", (ref,)
            ).fetchone()
        result = dict(row)
        if inbound is not None:
            result["inbound"] = dict(inbound)
        result["profile_enabled"] = bool(result["profile_enabled"])
        result["outbound_enabled"] = bool(result["outbound_enabled"])
        result["inbound_enabled"] = bool(result["inbound_enabled"])
        return result

    def list_provider_accounts(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            refs = [
                row["ref"]
                for row in connection.execute(
                    "SELECT ref FROM provider_accounts ORDER BY display_name, ref"
                )
            ]
        return [self.get_provider_account(ref) for ref in refs]

    def set_provider_account_status(
        self, ref: str, status: str, *, enabled: bool | None = None
    ) -> dict[str, Any]:
        _assert_safe_reference(ref, "provider_account.ref")
        with self.transaction(immediate=True) as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM provider_accounts WHERE ref = ?", (ref,)
                ).fetchone()
                is None
            ):
                raise KeyError(f"provider account not found: {ref}")
            connection.execute(
                "UPDATE provider_accounts SET status = ?, updated_at = ? WHERE ref = ?",
                (status, utc_now(), ref),
            )
            if enabled is not None:
                connection.execute(
                    "UPDATE provider_profiles SET enabled = ?, updated_at = ? WHERE ref = ?",
                    (int(enabled), utc_now(), ref),
                )
        return self.get_provider_account(ref)

    def upsert_provider_inbound_settings(
        self, ref: str, *, mailbox: str, read_only: bool = True, max_fetch: int = 100
    ) -> dict[str, Any]:
        _assert_safe_reference(ref, "provider_inbound.account_ref")
        if not mailbox.strip() or max_fetch <= 0:
            raise ValueError("inbound mailbox and fetch limit are required")
        with self.transaction(immediate=True) as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM provider_accounts WHERE ref = ?", (ref,)
                ).fetchone()
                is None
            ):
                raise KeyError(f"provider account not found: {ref}")
            connection.execute(
                "INSERT INTO provider_inbound_settings(account_ref, mailbox, read_only, max_fetch, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(account_ref) DO UPDATE SET mailbox=excluded.mailbox, "
                "read_only=excluded.read_only, max_fetch=excluded.max_fetch, updated_at=excluded.updated_at",
                (ref, mailbox.strip(), int(read_only), max_fetch, utc_now()),
            )
        return self.get_provider_account(ref)["inbound"]

    def record_provider_operation_grant(
        self,
        *,
        account_ref: str,
        operation: str,
        host: str,
        port: int,
        purpose: str,
        sender: str | None,
        recipient: str | None,
        max_submissions: int,
        expires_at: str,
        authorization_ref: str,
    ) -> str:
        _assert_safe_reference(account_ref, "provider_grant.account_ref")
        _assert_safe_reference(authorization_ref, "provider_grant.authorization_ref")
        _assert_no_secrets(
            {
                "operation": operation,
                "host": host,
                "purpose": purpose,
                "sender": sender,
                "recipient": recipient,
            },
            "provider_grant",
        )
        grant_id = str(uuid.uuid4())
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO provider_operation_grants "
                "(id, account_ref, operation, host, port, purpose, sender, recipient, "
                "max_submissions, expires_at, created_at, authorization_ref) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    grant_id,
                    account_ref,
                    operation,
                    host,
                    port,
                    purpose,
                    sender,
                    recipient,
                    max_submissions,
                    expires_at,
                    utc_now(),
                    authorization_ref,
                ),
            )
        return grant_id

    def list_provider_operation_grants(
        self, account_ref: str | None = None
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM provider_operation_grants"
        parameters: tuple[Any, ...] = ()
        if account_ref is not None:
            _assert_safe_reference(account_ref, "provider_grant.account_ref")
            query += " WHERE account_ref = ?"
            parameters = (account_ref,)
        query += " ORDER BY created_at DESC, id DESC"
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(query, parameters)]

    def get_provider_profile(self, ref: str) -> sqlite3.Row:
        _assert_safe_reference(ref, "provider_profile.ref")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM provider_profiles WHERE ref = ?", (ref,)
            ).fetchone()
        if row is None:
            raise KeyError(f"provider profile not found: {ref}")
        return row

    def list_provider_profiles(self, *, kind: str | None = None) -> list[sqlite3.Row]:
        query = "SELECT * FROM provider_profiles"
        parameters: tuple[Any, ...] = ()
        if kind is not None:
            query += " WHERE kind = ?"
            parameters = (kind,)
        query += " ORDER BY ref"
        with self.connect() as connection:
            return list(connection.execute(query, parameters))

    def record_provider_diagnostic(self, diagnostic: ProviderDiagnostic) -> str:
        _assert_safe_reference(diagnostic.profile_ref, "provider_diagnostic.profile_ref")
        _assert_no_secrets(
            {"phase": diagnostic.phase, "detail": diagnostic.detail}, "provider_diagnostic"
        )
        diagnostic_id = str(uuid.uuid4())
        occurred_at = diagnostic.observed_at or utc_now()
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO provider_diagnostics "
                "(id, profile_ref, status, phase, detail, capabilities_json, observed_at, operation, "
                "external_access, correlation_id, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    diagnostic_id,
                    diagnostic.profile_ref,
                    diagnostic.status.value,
                    diagnostic.phase,
                    diagnostic.detail,
                    _json(list(diagnostic.capabilities)),
                    occurred_at,
                    diagnostic.operation.value,
                    int(diagnostic.external_access),
                    diagnostic.correlation_id,
                    _json(dict(diagnostic.metadata)),
                ),
            )
        return diagnostic_id

    def list_provider_diagnostics(self, *, profile_ref: str | None = None) -> list[sqlite3.Row]:
        query = "SELECT * FROM provider_diagnostics"
        parameters: tuple[Any, ...] = ()
        if profile_ref is not None:
            _assert_safe_reference(profile_ref, "provider_diagnostic.profile_ref")
            query += " WHERE profile_ref = ?"
            parameters = (profile_ref,)
        query += " ORDER BY observed_at DESC, id DESC"
        with self.connect() as connection:
            return list(connection.execute(query, parameters))

    def record_provider_oauth_flow(
        self,
        *,
        flow_id: str,
        account_ref: str,
        state_hash: str,
        pkce_challenge: str,
        redirect_uri: str,
        scopes: tuple[str, ...],
        expires_at: str,
    ) -> None:
        _assert_safe_reference(account_ref, "provider_oauth.account_ref")
        _assert_no_secrets(
            {
                "state_hash": state_hash,
                "pkce_challenge": pkce_challenge,
                "redirect_uri": redirect_uri,
            },
            "provider_oauth",
        )
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO provider_oauth_flows(id, account_ref, state_hash, pkce_challenge, redirect_uri, "
                "scopes_json, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    flow_id,
                    account_ref,
                    state_hash,
                    pkce_challenge,
                    redirect_uri,
                    _json(list(scopes)),
                    expires_at,
                    utc_now(),
                ),
            )

    def complete_provider_oauth_flow(self, flow_id: str, *, identity: str) -> None:
        _assert_safe_reference(flow_id, "provider_oauth.flow_id")
        _assert_safe_reference(identity, "provider_oauth.identity")
        with self.transaction(immediate=True) as connection:
            result = connection.execute(
                "UPDATE provider_oauth_flows SET status = 'code_received', identity = ?, completed_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (identity, utc_now(), flow_id),
            )
            if result.rowcount != 1:
                raise ValueError("OAuth flow is not pending")

    def create_provider_test_submission(
        self,
        *,
        account_ref: str,
        idempotency_key: str,
        sender: str,
        recipient: str,
        message_revision_id: str | None,
        status: str,
        phase: str,
        detail: str,
        response_code: int | None = None,
        external_access: bool = False,
    ) -> dict[str, Any]:
        _assert_safe_reference(account_ref, "provider_test.account_ref")
        _assert_safe_reference(idempotency_key, "provider_test.idempotency_key")
        _assert_no_secrets({"detail": detail}, "provider_test")
        now = utc_now()
        submission_id = str(uuid.uuid4())
        with self.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM provider_test_submissions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                return dict(existing)
            connection.execute(
                "INSERT INTO provider_test_submissions(id, account_ref, idempotency_key, sender, recipient, "
                "message_revision_id, status, phase, detail, response_code, external_access, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    submission_id,
                    account_ref,
                    idempotency_key,
                    sender,
                    recipient,
                    message_revision_id,
                    status,
                    phase,
                    detail,
                    response_code,
                    int(external_access),
                    now,
                    now,
                ),
            )
        return self.get_provider_test_submission(idempotency_key)

    def get_provider_test_submission(self, idempotency_key: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM provider_test_submissions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        if row is None:
            raise KeyError(f"provider test submission not found: {idempotency_key}")
        return dict(row)

    def list_provider_tests(self, account_ref: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM provider_test_submissions"
        params: tuple[Any, ...] = ()
        if account_ref is not None:
            query += " WHERE account_ref = ?"
            params = (account_ref,)
        query += " ORDER BY created_at DESC, id DESC"
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(query, params)]

    def sender_policy_outcome_counts(
        self, policy_ref: str, *, domain: str | None = None, since: str | None = None
    ) -> dict[str, int]:
        clauses = ["sender_policy_ref = ?", "outcome IS NOT NULL", "outcome != ?"]
        parameters: list[object] = [policy_ref, AttemptOutcome.CANCELED.value]
        if domain is not None:
            clauses.append("recipient_domain = ?")
            parameters.append(domain)
        if since is not None:
            clauses.append("created_at >= ?")
            parameters.append(since)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total, "
                "COALESCE(SUM(CASE WHEN outcome = 'accepted' THEN 1 ELSE 0 END), 0) AS accepted, "
                "COALESCE(SUM(CASE WHEN outcome IN ('transient_failure', 'permanent_failure', 'ambiguous') "
                "THEN 1 ELSE 0 END), 0) AS failures FROM delivery_attempts WHERE "
                + " AND ".join(clauses),
                parameters,
            ).fetchone()
        return {key: int(row[key]) for key in ("total", "accepted", "failures")}

    def upsert_schedule(self, campaign_id: str, schedule: ScheduleSpec) -> None:
        _assert_no_secrets({"timezone": schedule.timezone}, "schedule")
        now = utc_now()
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO campaign_schedules "
                "(campaign_id, mode, timezone, scheduled_at, window_start, window_end, weekdays_json, "
                "pacing_seconds, cooldown_seconds, recurrence, catch_up_policy, fold, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM campaign_schedules WHERE campaign_id = ?), ?), ?)",
                (
                    campaign_id,
                    schedule.mode.value,
                    schedule.timezone,
                    schedule.scheduled_at,
                    schedule.window_start,
                    schedule.window_end,
                    _json(list(schedule.weekdays)),
                    schedule.pacing_seconds,
                    schedule.cooldown_seconds,
                    schedule.recurrence,
                    schedule.catch_up_policy.value,
                    schedule.fold,
                    campaign_id,
                    now,
                    now,
                ),
            )

    def get_schedule(self, campaign_id: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM campaign_schedules WHERE campaign_id = ?", (campaign_id,)
            ).fetchone()

    def set_campaign_scheduled_at(self, campaign_id: str, scheduled_at: str | None) -> None:
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE campaigns SET scheduled_at = ?, updated_at = ? WHERE id = ?",
                (scheduled_at, utc_now(), campaign_id),
            )

    def save_prepared_message(
        self,
        *,
        intent_id: str,
        template_name: str,
        template_version: str,
        rendering_seed: int,
        message_id: str,
        subject: str,
        from_email: str,
        recipient: str,
        content_hash: str,
        mime_bytes: bytes,
        attachment_metadata: list[dict[str, Any]],
    ) -> str:
        message_id_key = str(uuid.uuid5(uuid.NAMESPACE_URL, f"prepared:{intent_id}"))
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO prepared_messages "
                "(id, intent_id, template_name, template_version, rendering_seed, message_id, subject, "
                "from_email, recipient, content_hash, mime_bytes, attachment_metadata_json, prepared_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    message_id_key,
                    intent_id,
                    template_name,
                    template_version,
                    rendering_seed,
                    message_id,
                    subject,
                    from_email,
                    recipient,
                    content_hash,
                    mime_bytes,
                    _json(attachment_metadata),
                    utc_now(),
                ),
            )
        return message_id_key

    def get_prepared_message(self, intent_id: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM prepared_messages WHERE intent_id = ?", (intent_id,)
            ).fetchone()

    def record_event(
        self,
        connection: sqlite3.Connection,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        _assert_no_secrets(payload or {}, "event")
        connection.execute(
            "INSERT INTO operational_events "
            "(aggregate_type, aggregate_id, event_type, payload_json, occurred_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (aggregate_type, aggregate_id, event_type, _json(payload or {}), utc_now()),
        )

    def upsert_contact(
        self,
        normalized_email: str,
        *,
        display_name: str = "",
        metadata: dict[str, Any] | None = None,
        source_file: str,
        source_record_position: int,
        original_input: str,
    ) -> str:
        normalized_email = _validated_email(normalized_email)
        metadata = metadata or {}
        _assert_no_secrets(
            {
                "display_name": display_name,
                "source_file": source_file,
                "original_input": original_input,
            },
            "contact",
        )
        _assert_no_secrets(metadata, "contact.metadata")
        contact_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"mailto:{normalized_email}"))
        now = utc_now()
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT metadata_json, display_name FROM contacts WHERE normalized_email = ?",
                (normalized_email,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO contacts "
                    "(id, normalized_email, display_name, metadata_json, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (contact_id, normalized_email, display_name, _json(metadata), now, now),
                )
            else:
                merged = json.loads(row["metadata_json"])
                merged.update(
                    {key: value for key, value in metadata.items() if value not in (None, "")}
                )
                connection.execute(
                    "UPDATE contacts SET display_name = ?, metadata_json = ?, updated_at = ? "
                    "WHERE normalized_email = ?",
                    (display_name or row["display_name"], _json(merged), now, normalized_email),
                )
                contact_id = connection.execute(
                    "SELECT id FROM contacts WHERE normalized_email = ?", (normalized_email,)
                ).fetchone()["id"]
            source_insert = connection.execute(
                "INSERT OR IGNORE INTO contact_sources "
                "(contact_id, source_file, source_record_position, original_input, metadata_json, imported_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    contact_id,
                    source_file,
                    source_record_position,
                    original_input,
                    _json(metadata),
                    now,
                ),
            )
            if source_insert.rowcount:
                self.record_event(
                    connection,
                    "contact",
                    contact_id,
                    "contact_imported",
                    {
                        "source_file": source_file,
                        "source_record_position": source_record_position,
                    },
                )
        return contact_id

    def create_list(self, name: str, description: str = "", list_id: str | None = None) -> str:
        _assert_no_secrets({"name": name, "description": description}, "list")
        list_id = list_id or str(uuid.uuid4())
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO lists(id, name, description, created_at) VALUES (?, ?, ?, ?)",
                (list_id, name, description, utc_now()),
            )
        return list_id

    def add_to_list(self, list_id: str, contact_id: str) -> None:
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO list_memberships(list_id, contact_id, added_at) VALUES (?, ?, ?)",
                (list_id, contact_id, utc_now()),
            )

    def list_contacts(self, list_id: str) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    "SELECT c.* FROM contacts c JOIN list_memberships m ON m.contact_id = c.id "
                    "WHERE m.list_id = ? ORDER BY c.normalized_email",
                    (list_id,),
                )
            )

    def get_contact(self, contact_id: str) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM contacts WHERE id = ?", (contact_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"contact not found: {contact_id}")
        return row

    def contact_by_email(self, normalized_email: str) -> sqlite3.Row | None:
        normalized_email = _validated_email(normalized_email)
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM contacts WHERE normalized_email = ?", (normalized_email,)
            ).fetchone()

    def update_contact(
        self,
        contact_id: str,
        *,
        display_name: str | None = None,
        metadata: dict[str, Any] | None = None,
        actor: str = "local-operator",
    ) -> None:
        _assert_no_secrets({"display_name": display_name or "", "actor": actor}, "contact_update")
        _assert_no_secrets(metadata or {}, "contact_update.metadata")
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT display_name, metadata_json FROM contacts WHERE id = ?", (contact_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"contact not found: {contact_id}")
            merged = json.loads(row["metadata_json"])
            merged.update(metadata or {})
            connection.execute(
                "UPDATE contacts SET display_name = ?, metadata_json = ?, updated_at = ? WHERE id = ?",
                (
                    display_name if display_name is not None else row["display_name"],
                    _json(merged),
                    utc_now(),
                    contact_id,
                ),
            )
            self.record_event(
                connection,
                "contact",
                contact_id,
                "contact_corrected",
                {"actor": actor},
            )

    def deactivate_contact(self, contact_id: str, *, reason: str, actor: str) -> None:
        _assert_no_secrets({"reason": reason, "actor": actor}, "contact_deactivation")
        with self.transaction(immediate=True) as connection:
            updated = connection.execute(
                "UPDATE contacts SET lifecycle_status = 'deactivated', deactivated_at = ?, updated_at = ? "
                "WHERE id = ?",
                (utc_now(), utc_now(), contact_id),
            ).rowcount
            if updated != 1:
                raise KeyError(f"contact not found: {contact_id}")
            self.record_event(
                connection,
                "contact",
                contact_id,
                "contact_deactivated",
                {"reason": reason, "actor": actor},
            )
            self._bump_policy_revision(connection)

    def export_contacts(self, *, list_id: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as connection:
            if list_id is None:
                rows = connection.execute(
                    "SELECT * FROM contacts ORDER BY normalized_email"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT c.* FROM contacts c JOIN list_memberships m ON m.contact_id = c.id "
                    "WHERE m.list_id = ? ORDER BY c.normalized_email",
                    (list_id,),
                ).fetchall()
        return [
            {
                "id": row["id"],
                "normalized_email": row["normalized_email"],
                "display_name": row["display_name"],
                "metadata": json.loads(row["metadata_json"]),
                "lifecycle_status": row["lifecycle_status"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def add_suppression(
        self,
        *,
        category: SuppressionCategory,
        reason: str,
        normalized_email: str | None = None,
        contact_id: str | None = None,
        scope_type: str = "global",
        scope_id: str | None = None,
        expires_at: str | None = None,
        actor: str = "local-operator",
        source: str = "operator",
        policy_version: str = "1",
    ) -> str:
        _assert_no_secrets({"reason": reason, "actor": actor, "source": source}, "suppression")
        _assert_safe_reference(scope_id, "suppression.scope_id")
        if normalized_email:
            normalized_email = _validated_email(normalized_email)
        if not normalized_email and not contact_id:
            raise ValueError("suppression requires normalized_email or contact_id")
        with self.transaction(immediate=True) as connection:
            return self._record_suppression(
                connection,
                category=category,
                reason=reason,
                normalized_email=normalized_email,
                contact_id=contact_id,
                scope_type=scope_type,
                scope_id=scope_id,
                expires_at=expires_at,
                actor=actor,
                source=source,
                policy_version=policy_version,
            )

    def _record_suppression(
        self,
        connection: sqlite3.Connection,
        *,
        category: SuppressionCategory,
        reason: str,
        normalized_email: str | None,
        contact_id: str | None,
        scope_type: str,
        scope_id: str | None,
        expires_at: str | None = None,
        actor: str = "local-operator",
        source: str = "operator",
        policy_version: str = "1",
        suppression_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Write a suppression, history fact, and policy revision atomically."""

        existing = connection.execute(
            "SELECT * FROM suppressions WHERE normalized_email IS ? AND contact_id IS ? "
            "AND category = ? AND scope_type = ? AND scope_id IS ?",
            (normalized_email, contact_id, category.value, scope_type, scope_id),
        ).fetchone()
        now = utc_now()
        if existing:
            suppression_id = existing["id"]
            before = {
                key: existing[key]
                for key in (
                    "normalized_email",
                    "contact_id",
                    "category",
                    "scope_type",
                    "scope_id",
                    "reason",
                    "effective_at",
                    "expires_at",
                    "active",
                )
            }
            connection.execute(
                "UPDATE suppressions SET active = 1, reason = ?, effective_at = ?, expires_at = ? "
                "WHERE id = ?",
                (reason, now, expires_at, suppression_id),
            )
            action = "reaffirmed"
            history_metadata = {
                "source": source,
                "policy_version": policy_version,
                "before": before,
                "after": {
                    **before,
                    "reason": reason,
                    "effective_at": now,
                    "expires_at": expires_at,
                    "active": 1,
                },
                **(metadata or {}),
            }
        else:
            suppression_id = suppression_id or str(uuid.uuid4())
            connection.execute(
                "INSERT INTO suppressions "
                "(id, normalized_email, contact_id, category, scope_type, scope_id, reason, effective_at, expires_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    suppression_id,
                    normalized_email,
                    contact_id,
                    category.value,
                    scope_type,
                    scope_id,
                    reason,
                    now,
                    expires_at,
                    now,
                ),
            )
            self.record_event(
                connection,
                "suppression",
                suppression_id,
                "suppression_added",
                {
                    "category": category.value,
                    "scope_type": scope_type,
                    "scope_id": scope_id,
                    **(metadata or {}),
                },
            )
            action = "created"
            history_metadata = {
                "source": source,
                "policy_version": policy_version,
                "after": {
                    "normalized_email": normalized_email,
                    "contact_id": contact_id,
                    "category": category.value,
                    "scope_type": scope_type,
                    "scope_id": scope_id,
                    "reason": reason,
                    "effective_at": now,
                    "expires_at": expires_at,
                    "active": 1,
                },
                **(metadata or {}),
            }
        connection.execute(
            "INSERT INTO suppression_history(id, suppression_id, action, occurred_at, actor, metadata_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), suppression_id, action, now, actor, _json(history_metadata)),
        )
        self._bump_policy_revision(connection)
        return suppression_id

    def list_suppressions(self, *, normalized_email: str | None = None) -> list[sqlite3.Row]:
        query = "SELECT * FROM suppressions"
        parameters: tuple[Any, ...] = ()
        if normalized_email:
            query += " WHERE normalized_email = ?"
            parameters = (_validated_email(normalized_email),)
        query += " ORDER BY effective_at DESC, id"
        with self.connect() as connection:
            return list(connection.execute(query, parameters))

    def matching_suppressions(
        self,
        *,
        normalized_email: str,
        contact_id: str,
        list_id: str,
        campaign_id: str,
        sender_config_ref: str,
        communication_purpose: str,
        connection: sqlite3.Connection | None = None,
    ) -> list[sqlite3.Row]:
        now = utc_now()
        query = (
            "SELECT * FROM suppressions WHERE active = 1 "
            "AND (expires_at IS NULL OR expires_at > ?) "
            "AND (normalized_email = ? OR contact_id = ?) "
            "AND (scope_type = 'global' "
            "OR (scope_type = 'sender' AND scope_id = ?) "
            "OR (scope_type = 'list' AND scope_id = ?) "
            "OR (scope_type = 'campaign' AND scope_id = ?) "
            "OR (scope_type = 'purpose' AND scope_id = ?)) "
            "ORDER BY CASE category WHEN 'hard_bounce' THEN 1 WHEN 'complaint' THEN 2 "
            "WHEN 'unsubscribe' THEN 3 WHEN 'manual_hold' THEN 4 ELSE 5 END, "
            "CASE scope_type WHEN 'global' THEN 1 WHEN 'sender' THEN 2 WHEN 'purpose' THEN 3 "
            "WHEN 'list' THEN 4 ELSE 5 END, effective_at, id"
        )
        parameters = (
            now,
            normalized_email,
            contact_id,
            sender_config_ref,
            list_id,
            campaign_id,
            communication_purpose,
        )
        if connection is not None:
            return list(connection.execute(query, parameters))
        with self.connect() as active_connection:
            return list(active_connection.execute(query, parameters))

    def suppression_reason(
        self,
        *,
        normalized_email: str,
        contact_id: str,
        list_id: str,
        campaign_id: str,
        sender_config_ref: str,
        communication_purpose: str = "marketing",
    ) -> str | None:
        row = next(
            iter(
                self.matching_suppressions(
                    normalized_email=normalized_email,
                    contact_id=contact_id,
                    list_id=list_id,
                    campaign_id=campaign_id,
                    sender_config_ref=sender_config_ref,
                    communication_purpose=communication_purpose,
                )
            ),
            None,
        )
        return f"{row['category']}: {row['reason']}" if row else None

    def record_authorization_evidence(
        self,
        *,
        contact_id: str,
        purpose: str,
        status: EvidenceStatus,
        source: str,
        recording_actor: str,
        scope_type: str = "global",
        scope_id: str | None = None,
        evidence_ref: str | None = None,
        policy_version: str = "1",
        details: dict[str, Any] | None = None,
    ) -> str:
        _assert_no_secrets(
            {
                "source": source,
                "recording_actor": recording_actor,
                "evidence_ref": evidence_ref or "",
            },
            "authorization_evidence",
        )
        _assert_no_secrets(details or {}, "authorization_evidence.details")
        evidence_id = str(uuid.uuid4())
        with self.transaction(immediate=True) as connection:
            if (
                connection.execute("SELECT 1 FROM contacts WHERE id = ?", (contact_id,)).fetchone()
                is None
            ):
                raise KeyError(f"contact not found: {contact_id}")
            connection.execute(
                "INSERT INTO authorization_evidence "
                "(id, contact_id, purpose, status, source, scope_type, scope_id, recorded_at, "
                "recording_actor, evidence_ref, policy_version, details_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    evidence_id,
                    contact_id,
                    purpose,
                    status.value,
                    source,
                    scope_type,
                    scope_id,
                    datetime.now(UTC).isoformat(),
                    recording_actor,
                    evidence_ref,
                    policy_version,
                    _json(details or {}),
                ),
            )
            self.record_event(
                connection,
                "contact",
                contact_id,
                "authorization_evidence_recorded",
                {"evidence_id": evidence_id, "purpose": purpose, "status": status.value},
            )
            self._bump_policy_revision(connection)
        return evidence_id

    def latest_authorization_evidence(
        self,
        contact_id: str,
        *,
        purpose: str,
        scope_ids: tuple[tuple[str, str | None], ...],
        connection: sqlite3.Connection | None = None,
    ) -> sqlite3.Row | None:
        clauses = ["(scope_type = 'global' AND scope_id IS NULL)"]
        parameters: list[Any] = [contact_id, purpose]
        for scope_type, scope_id in scope_ids:
            if scope_id is not None:
                clauses.append("(scope_type = ? AND scope_id = ?)")
                parameters.extend((scope_type, scope_id))
        query = (
            "SELECT * FROM authorization_evidence WHERE contact_id = ? AND purpose = ? AND ("
            + " OR ".join(clauses)
            + ") ORDER BY recorded_at DESC, rowid DESC"
        )
        if connection is None:
            with self.connect() as active_connection:
                rows = active_connection.execute(query, parameters).fetchall()
        else:
            rows = connection.execute(query, parameters).fetchall()
        latest_by_scope: dict[tuple[str, str | None], sqlite3.Row] = {}
        for row in rows:
            latest_by_scope.setdefault((row["scope_type"], row["scope_id"]), row)
        restricted = [
            row
            for row in latest_by_scope.values()
            if row["status"] in {EvidenceStatus.WITHDRAWN.value, EvidenceStatus.CONFLICTED.value}
        ]
        if restricted:
            return next(
                (row for row in restricted if row["status"] == EvidenceStatus.CONFLICTED.value),
                restricted[0],
            )
        return next(
            (
                row
                for row in latest_by_scope.values()
                if row["status"] == EvidenceStatus.ESTABLISHED.value
            ),
            None,
        )

    def list_authorization_evidence(self, *, contact_id: str | None = None) -> list[sqlite3.Row]:
        query = "SELECT * FROM authorization_evidence"
        parameters: tuple[Any, ...] = ()
        if contact_id:
            query += " WHERE contact_id = ?"
            parameters = (contact_id,)
        query += " ORDER BY recorded_at DESC, id DESC"
        with self.connect() as connection:
            return list(connection.execute(query, parameters))

    def set_preference(
        self,
        *,
        contact_id: str,
        purpose: str,
        state: PreferenceState,
        source: str,
        recording_actor: str,
        scope_type: str = "global",
        scope_id: str | None = None,
        evidence_ref: str | None = None,
        policy_version: str = "1",
    ) -> str:
        _assert_no_secrets({"source": source, "recording_actor": recording_actor}, "preference")
        _assert_safe_reference(evidence_ref, "preference.evidence_ref")
        preference_id = str(uuid.uuid4())
        with self.transaction(immediate=True) as connection:
            if (
                connection.execute("SELECT 1 FROM contacts WHERE id = ?", (contact_id,)).fetchone()
                is None
            ):
                raise KeyError(f"contact not found: {contact_id}")
            connection.execute(
                "INSERT INTO recipient_preferences "
                "(id, contact_id, purpose, state, source, scope_type, scope_id, recorded_at, "
                "recording_actor, evidence_ref, policy_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    preference_id,
                    contact_id,
                    purpose,
                    state.value,
                    source,
                    scope_type,
                    scope_id,
                    datetime.now(UTC).isoformat(),
                    recording_actor,
                    evidence_ref,
                    policy_version,
                ),
            )
            self.record_event(
                connection,
                "contact",
                contact_id,
                "recipient_preference_recorded",
                {"preference_id": preference_id, "purpose": purpose, "state": state.value},
            )
            self._bump_policy_revision(connection)
        return preference_id

    def latest_preference(
        self,
        contact_id: str,
        *,
        purpose: str,
        scope_ids: tuple[tuple[str, str | None], ...],
        connection: sqlite3.Connection | None = None,
    ) -> sqlite3.Row | None:
        clauses = ["(scope_type = 'global' AND scope_id IS NULL)"]
        parameters: list[Any] = [contact_id, purpose]
        for scope_type, scope_id in scope_ids:
            if scope_id is not None:
                clauses.append("(scope_type = ? AND scope_id = ?)")
                parameters.extend((scope_type, scope_id))
        query = (
            "SELECT * FROM recipient_preferences WHERE contact_id = ? AND purpose = ? AND ("
            + " OR ".join(clauses)
            + ") ORDER BY recorded_at DESC, rowid DESC"
        )
        if connection is None:
            with self.connect() as active_connection:
                rows = active_connection.execute(query, parameters).fetchall()
        else:
            rows = connection.execute(query, parameters).fetchall()
        latest_by_scope: dict[tuple[str, str | None], sqlite3.Row] = {}
        for row in rows:
            latest_by_scope.setdefault((row["scope_type"], row["scope_id"]), row)
        disabled = [
            row
            for row in latest_by_scope.values()
            if row["state"] == PreferenceState.DISABLED.value
        ]
        if disabled:
            return disabled[0]
        return next(
            (
                row
                for row in latest_by_scope.values()
                if row["state"] == PreferenceState.ENABLED.value
            ),
            None,
        )

    def list_preferences(self, *, contact_id: str | None = None) -> list[sqlite3.Row]:
        query = "SELECT * FROM recipient_preferences"
        parameters: tuple[Any, ...] = ()
        if contact_id:
            query += " WHERE contact_id = ?"
            parameters = (contact_id,)
        query += " ORDER BY recorded_at DESC, id DESC"
        with self.connect() as connection:
            return list(connection.execute(query, parameters))

    def record_operator_declaration(
        self,
        *,
        campaign_id: str | None,
        action: OperatorAction,
        declaration: str,
        actor: str,
        idempotency_key: str,
        policy_version: str = "1",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        _assert_no_secrets({"declaration": declaration, "actor": actor}, "operator_declaration")
        _assert_no_secrets(metadata or {}, "operator_declaration.metadata")
        declaration_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"mailforge:operator:{idempotency_key}")
        )
        with self.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT id FROM operator_declarations WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing:
                return existing["id"]
            connection.execute(
                "INSERT INTO operator_declarations "
                "(id, campaign_id, action, declaration, actor, occurred_at, policy_version, idempotency_key, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    declaration_id,
                    campaign_id,
                    action.value,
                    declaration,
                    actor,
                    datetime.now(UTC).isoformat(),
                    policy_version,
                    idempotency_key,
                    _json(metadata or {}),
                ),
            )
            if campaign_id:
                self.record_event(
                    connection,
                    "campaign",
                    campaign_id,
                    "operator_declaration_recorded",
                    {"action": action.value, "actor": actor},
                )
        return declaration_id

    def list_operator_declarations(self, *, campaign_id: str | None = None) -> list[sqlite3.Row]:
        query = "SELECT * FROM operator_declarations"
        parameters: tuple[Any, ...] = ()
        if campaign_id:
            query += " WHERE campaign_id = ?"
            parameters = (campaign_id,)
        query += " ORDER BY occurred_at DESC, id DESC"
        with self.connect() as connection:
            return list(connection.execute(query, parameters))

    def plan_lifecycle_action(
        self,
        *,
        contact_id: str,
        action: str,
        reason: str,
        actor: str,
        idempotency_key: str,
    ) -> str:
        _assert_no_secrets({"reason": reason, "actor": actor}, "lifecycle")
        lifecycle_id = str(uuid.uuid4())
        with self.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT id FROM lifecycle_records WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing:
                return existing["id"]
            if (
                connection.execute("SELECT 1 FROM contacts WHERE id = ?", (contact_id,)).fetchone()
                is None
            ):
                raise KeyError(f"contact not found: {contact_id}")
            conflict = connection.execute(
                "SELECT 1 FROM suppressions WHERE active = 1 AND (contact_id = ? OR "
                "normalized_email = (SELECT normalized_email FROM contacts WHERE id = ?)) LIMIT 1",
                (contact_id, contact_id),
            ).fetchone()
            connection.execute(
                "INSERT INTO lifecycle_records "
                "(id, contact_id, action, status, reason, retention_conflict, requested_at, actor, idempotency_key) "
                "VALUES (?, ?, ?, 'planned', ?, ?, ?, ?, ?)",
                (
                    lifecycle_id,
                    contact_id,
                    action,
                    reason,
                    int(conflict is not None),
                    utc_now(),
                    actor,
                    idempotency_key,
                ),
            )
            self.record_event(
                connection,
                "contact",
                contact_id,
                "lifecycle_action_planned",
                {"action": action, "retention_conflict": bool(conflict), "actor": actor},
            )
        return lifecycle_id

    def list_lifecycle_records(self, *, contact_id: str | None = None) -> list[sqlite3.Row]:
        query = "SELECT * FROM lifecycle_records"
        parameters: tuple[Any, ...] = ()
        if contact_id:
            query += " WHERE contact_id = ?"
            parameters = (contact_id,)
        query += " ORDER BY requested_at DESC, id DESC"
        with self.connect() as connection:
            return list(connection.execute(query, parameters))

    def create_campaign(
        self,
        *,
        name: str,
        description: str,
        sender_config_ref: str,
        template_ref: str,
        audience_list_id: str,
        configuration: dict[str, Any] | None = None,
        campaign_id: str | None = None,
        audience_source_type: str = "list",
        audience_source_id: str | None = None,
        audience_revision_id: str | None = None,
        audience_run_key: str = "primary",
        message_revision_id: str | None = None,
        provider_account_ref: str | None = None,
    ) -> str:
        _assert_no_secrets({"name": name, "description": description}, "campaign")
        _assert_safe_reference(sender_config_ref, "sender_config_ref")
        _assert_safe_reference(template_ref, "template_ref")
        configuration = configuration or {}
        _assert_no_secrets(configuration)
        campaign_id = campaign_id or str(uuid.uuid4())
        now = utc_now()
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO campaigns "
                "(id, name, description, sender_config_ref, template_ref, audience_list_id, "
                "audience_source_type, audience_source_id, audience_revision_id, audience_run_key, "
                "message_revision_id, provider_account_ref, status, authorization, configuration_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    campaign_id,
                    name,
                    description,
                    sender_config_ref,
                    template_ref,
                    audience_list_id,
                    audience_source_type,
                    audience_source_id or audience_list_id,
                    audience_revision_id,
                    audience_run_key,
                    message_revision_id,
                    provider_account_ref,
                    CampaignStatus.DRAFT.value,
                    ExecutionAuthorization.UNAUTHORIZED.value,
                    _json(configuration),
                    now,
                    now,
                ),
            )
            self.record_event(
                connection, "campaign", campaign_id, "campaign_created", {"name": name}
            )
        return campaign_id

    def get_campaign(self, campaign_id: str) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM campaigns WHERE id = ?", (campaign_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"campaign not found: {campaign_id}")
        return row

    def transition_campaign(
        self,
        campaign_id: str,
        target: CampaignStatus,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT status FROM campaigns WHERE id = ?", (campaign_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"campaign not found: {campaign_id}")
            current = CampaignStatus(row["status"])
            validate_campaign_transition(current, target)
            now = utc_now()
            connection.execute(
                "UPDATE campaigns SET status = ?, updated_at = ?, started_at = CASE WHEN ? = 'running' THEN COALESCE(started_at, ?) ELSE started_at END, "
                "completed_at = CASE WHEN ? = 'completed' THEN ? ELSE completed_at END, failure_reason = CASE WHEN ? = 'failed' THEN ? ELSE failure_reason END "
                "WHERE id = ?",
                (
                    target.value,
                    now,
                    target.value,
                    now,
                    target.value,
                    now,
                    target.value,
                    reason,
                    campaign_id,
                ),
            )
            self.record_event(
                connection,
                "campaign",
                campaign_id,
                "campaign_transitioned",
                {"from": current.value, "to": target.value, "reason": reason, **(metadata or {})},
            )
            connection.execute(
                "INSERT INTO campaign_transitions(campaign_id, from_status, to_status, reason, occurred_at, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (campaign_id, current.value, target.value, reason, now, _json(metadata or {})),
            )

    def authorize_simulation(
        self,
        campaign_id: str,
        *,
        declaration: str = (
            "I authorize a synthetic simulation and accept responsibility for the configured "
            "audience and communication purpose; this is not recipient consent evidence."
        ),
        actor: str = "local-operator",
    ) -> None:
        _assert_no_secrets({"declaration": declaration, "actor": actor}, "operator_declaration")
        idempotency_key = f"simulation:{campaign_id}"
        declaration_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"mailforge:operator:{idempotency_key}")
        )
        with self.transaction(immediate=True) as connection:
            updated = connection.execute(
                "UPDATE campaigns SET authorization = ?, updated_at = ? WHERE id = ?",
                (ExecutionAuthorization.SIMULATION_AUTHORIZED.value, utc_now(), campaign_id),
            ).rowcount
            if updated != 1:
                raise KeyError(f"campaign not found: {campaign_id}")
            connection.execute(
                "INSERT OR IGNORE INTO operator_declarations "
                "(id, campaign_id, action, declaration, actor, occurred_at, policy_version, idempotency_key, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    declaration_id,
                    campaign_id,
                    OperatorAction.AUTHORIZE_EXECUTION.value,
                    declaration,
                    actor,
                    datetime.now(UTC).isoformat(),
                    "1",
                    idempotency_key,
                    "{}",
                ),
            )
            self.record_event(connection, "campaign", campaign_id, "simulation_authorized")

    def has_execution_declaration(self, campaign_id: str) -> bool:
        with self.connect() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM operator_declarations WHERE campaign_id = ? AND action = ? LIMIT 1",
                    (campaign_id, OperatorAction.AUTHORIZE_EXECUTION.value),
                ).fetchone()
                is not None
            )

    def prepare_intent(
        self,
        *,
        campaign_id: str,
        contact_id: str,
        normalized_email: str,
        state: DeliveryState,
        eligibility_reason: str,
    ) -> str:
        normalized_email = _validated_email(normalized_email)
        _assert_no_secrets({"eligibility_reason": eligibility_reason}, "delivery_intent")
        intent_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{campaign_id}:{contact_id}"))
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO delivery_intents "
                "(id, campaign_id, contact_id, normalized_email, state, eligibility_reason, prepared_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    intent_id,
                    campaign_id,
                    contact_id,
                    normalized_email,
                    state.value,
                    eligibility_reason,
                    utc_now(),
                    utc_now(),
                ),
            )
        return intent_id

    def import_legacy_attempt(
        self,
        *,
        intent_id: str,
        outcome: AttemptOutcome,
        reason: str,
    ) -> bool:
        state = {
            AttemptOutcome.LEGACY_RECORDED: DeliveryState.AMBIGUOUS,
            AttemptOutcome.TRANSIENT_FAILURE: DeliveryState.DEFERRED,
            AttemptOutcome.PERMANENT_FAILURE: DeliveryState.PERMANENT_FAILURE,
        }[outcome]
        _assert_no_secrets({"reason": reason}, "legacy_attempt")
        with self.transaction(immediate=True) as connection:
            now = utc_now()
            existing = connection.execute(
                "SELECT attempt_number, outcome, error_message FROM delivery_attempts "
                "WHERE intent_id = ? ORDER BY attempt_number DESC LIMIT 1",
                (intent_id,),
            ).fetchone()
            if (
                existing is not None
                and existing["outcome"] == outcome.value
                and existing["error_message"] == reason
            ):
                return False
            if existing is not None:
                if existing["outcome"] is None:
                    return False
                existing_outcome = AttemptOutcome(existing["outcome"])
                if existing_outcome in {
                    AttemptOutcome.ACCEPTED,
                    AttemptOutcome.AMBIGUOUS,
                    AttemptOutcome.PERMANENT_FAILURE,
                    AttemptOutcome.CANCELED,
                }:
                    return False
                if (
                    existing_outcome is AttemptOutcome.LEGACY_RECORDED
                    and outcome is AttemptOutcome.TRANSIENT_FAILURE
                ):
                    return False
            attempt_number = (existing["attempt_number"] if existing else 0) + 1
            connection.execute(
                "UPDATE delivery_intents SET state = ?, eligibility_reason = ?, "
                "attempt_count = MAX(attempt_count, ?), updated_at = ? WHERE id = ?",
                (state.value, reason, attempt_number, now, intent_id),
            )
            attempt_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO delivery_attempts "
                "(id, intent_id, attempt_number, attempt_state, outcome, error_code, error_message, started_at, finished_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'legacy_record', ?, ?, ?, ?)",
                (
                    attempt_id,
                    intent_id,
                    attempt_number,
                    state.value,
                    outcome.value,
                    reason,
                    now,
                    now,
                    now,
                ),
            )
            self.record_event(
                connection,
                "delivery_intent",
                intent_id,
                "legacy_attempt_imported",
                {"outcome": outcome.value, "reason": reason},
            )
        return True

    def list_intents(self, campaign_id: str) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    "SELECT * FROM delivery_intents WHERE campaign_id = ? ORDER BY normalized_email",
                    (campaign_id,),
                )
            )

    def counts(self, campaign_id: str) -> dict[str, int]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT state, COUNT(*) AS count FROM delivery_intents WHERE campaign_id = ? GROUP BY state",
                (campaign_id,),
            ).fetchall()
        return {row["state"]: row["count"] for row in rows}

    def reserve_next(
        self,
        campaign_id: str,
        *,
        retry_deferred: bool = False,
        max_attempts: int = 3,
        worker_id: str = "legacy-worker",
        lease_seconds: int = 300,
        now: datetime | None = None,
    ) -> sqlite3.Row | None:
        now_value = (now or datetime.now(UTC)).astimezone(UTC)
        now_text = now_value.isoformat()
        lease_expires = (now_value + timedelta(seconds=lease_seconds)).isoformat()
        with self.transaction(immediate=True) as connection:
            if retry_deferred:
                row = connection.execute(
                    "SELECT * FROM delivery_intents WHERE campaign_id = ? AND "
                    "(state IN (?, ?) OR (state = ? AND "
                    "(SELECT COUNT(*) FROM delivery_attempts a WHERE a.intent_id = delivery_intents.id "
                    "AND a.outcome IS NOT NULL AND a.outcome != ?) < ?)) "
                    "AND (not_before_utc IS NULL OR not_before_utc <= ?) "
                    "ORDER BY normalized_email LIMIT 1",
                    (
                        campaign_id,
                        DeliveryState.ELIGIBLE_PENDING.value,
                        DeliveryState.PREPARED.value,
                        DeliveryState.DEFERRED.value,
                        AttemptOutcome.CANCELED.value,
                        max_attempts,
                        now_text,
                    ),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM delivery_intents WHERE campaign_id = ? AND state IN (?, ?) "
                    "AND (not_before_utc IS NULL OR not_before_utc <= ?) "
                    "ORDER BY normalized_email LIMIT 1",
                    (
                        campaign_id,
                        DeliveryState.ELIGIBLE_PENDING.value,
                        DeliveryState.PREPARED.value,
                        now_text,
                    ),
                ).fetchone()
            if row is None:
                return None
            now = utc_now()
            connection.execute(
                "UPDATE delivery_intents SET state = ?, reserved_at = ?, attempt_count = attempt_count + 1, "
                "lease_owner = ?, lease_expires_at = ?, not_before_utc = NULL, updated_at = ? WHERE id = ?",
                (DeliveryState.RESERVED.value, now, worker_id, lease_expires, now, row["id"]),
            )
            attempt_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO delivery_attempts "
                "(id, intent_id, attempt_number, attempt_state, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    attempt_id,
                    row["id"],
                    row["attempt_count"] + 1,
                    DeliveryState.RESERVED.value,
                    now,
                ),
            )
            self.record_event(
                connection,
                "delivery_intent",
                row["id"],
                "intent_reserved",
                {"attempt_id": attempt_id},
            )
            return connection.execute(
                "SELECT i.*, a.id AS attempt_id, a.attempt_number FROM delivery_intents i "
                "JOIN delivery_attempts a ON a.intent_id = i.id AND a.attempt_number = i.attempt_count WHERE i.id = ?",
                (row["id"],),
            ).fetchone()

    def try_reserve_policy_slot(
        self,
        *,
        attempt_id: str,
        intent_id: str,
        policy: SenderPolicy,
        domain: str,
        now: datetime,
        warmup_max_per_day: int | None = None,
    ) -> PolicyDecision:
        now = now.astimezone(UTC)
        now_text = now.isoformat()
        expires_text = (
            now + timedelta(seconds=max(60, policy.cooldown_seconds or 300))
        ).isoformat()
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT cooldown_until FROM sender_policies WHERE ref = ?", (policy.ref,)
            ).fetchone()
            if row is None:
                raise KeyError(f"sender policy not found: {policy.ref}")
            if row["cooldown_until"] and row["cooldown_until"] > now_text:
                return PolicyDecision(False, policy.ref, "provider cooldown", row["cooldown_until"])
            active = connection.execute(
                "SELECT COUNT(*) AS count FROM policy_reservations "
                "WHERE sender_policy_ref = ? AND released_at IS NULL AND expires_at > ?",
                (policy.ref, now_text),
            ).fetchone()["count"]
            if active >= policy.max_concurrency:
                return PolicyDecision(False, policy.ref, "concurrency limit", expires_text)
            minute = (now - timedelta(minutes=1)).isoformat()
            hour = (now - timedelta(hours=1)).isoformat()
            day = (now - timedelta(days=1)).isoformat()
            counts = connection.execute(
                "SELECT COUNT(*) AS total, "
                "COALESCE(SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END), 0) AS minute_count, "
                "COALESCE(SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END), 0) AS hour_count, "
                "COALESCE(SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END), 0) AS day_count "
                "FROM delivery_attempts WHERE sender_policy_ref = ? "
                "AND (outcome IS NULL OR outcome != ?)",
                (minute, hour, day, policy.ref, AttemptOutcome.CANCELED.value),
            ).fetchone()
            domain_count = connection.execute(
                "SELECT COUNT(*) AS count FROM delivery_attempts "
                "WHERE sender_policy_ref = ? AND recipient_domain = ? AND created_at >= ? "
                "AND (outcome IS NULL OR outcome != ?)",
                (policy.ref, domain, minute, AttemptOutcome.CANCELED.value),
            ).fetchone()["count"]
            if counts["minute_count"] >= policy.max_per_minute:
                return PolicyDecision(
                    False, policy.ref, "per-minute limit", (now + timedelta(minutes=1)).isoformat()
                )
            if counts["hour_count"] >= policy.max_per_hour:
                return PolicyDecision(
                    False, policy.ref, "per-hour limit", (now + timedelta(hours=1)).isoformat()
                )
            day_limit = min(
                policy.max_per_day,
                warmup_max_per_day if warmup_max_per_day is not None else policy.max_per_day,
            )
            if counts["day_count"] >= day_limit:
                return PolicyDecision(
                    False, policy.ref, "per-day limit", (now + timedelta(days=1)).isoformat()
                )
            if domain_count >= policy.per_domain_per_minute:
                return PolicyDecision(
                    False, policy.ref, "per-domain limit", (now + timedelta(minutes=1)).isoformat()
                )
            hour_domain_count = connection.execute(
                "SELECT COUNT(*) AS count FROM delivery_attempts "
                "WHERE sender_policy_ref = ? AND recipient_domain = ? AND created_at >= ? "
                "AND (outcome IS NULL OR outcome != ?)",
                (policy.ref, domain, hour, AttemptOutcome.CANCELED.value),
            ).fetchone()["count"]
            if policy.per_domain_per_hour and hour_domain_count >= policy.per_domain_per_hour:
                return PolicyDecision(
                    False,
                    policy.ref,
                    "per-domain hourly limit",
                    (now + timedelta(hours=1)).isoformat(),
                )
            connection.execute(
                "INSERT INTO policy_reservations "
                "(id, attempt_id, intent_id, sender_policy_ref, recipient_domain, reserved_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    attempt_id,
                    intent_id,
                    policy.ref,
                    domain,
                    now_text,
                    expires_text,
                ),
            )
            connection.execute(
                "UPDATE delivery_attempts SET sender_policy_ref = ?, recipient_domain = ? WHERE id = ?",
                (policy.ref, domain, attempt_id),
            )
        return PolicyDecision(True, policy.ref, "policy_selected")

    def reserve_campaign_pacing(
        self, campaign_id: str, *, now: datetime, interval_seconds: float
    ) -> tuple[bool, str | None]:
        """Reserve a campaign pacing interval transactionally across workers."""

        now = now.astimezone(UTC)
        now_text = now.isoformat()
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT next_allowed_at FROM campaign_pacing WHERE campaign_id = ?",
                (campaign_id,),
            ).fetchone()
            if row is not None and row["next_allowed_at"] > now_text:
                return False, row["next_allowed_at"]
            next_allowed = (now + timedelta(seconds=max(0.0, interval_seconds))).isoformat()
            connection.execute(
                "INSERT INTO campaign_pacing(campaign_id, next_allowed_at, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(campaign_id) DO UPDATE SET "
                "next_allowed_at = excluded.next_allowed_at, updated_at = excluded.updated_at",
                (campaign_id, next_allowed, now_text),
            )
        return True, next_allowed

    def release_reservation(
        self, intent_id: str, reason: str, *, not_before_utc: str | None = None
    ) -> None:
        _assert_no_secrets({"reason": reason}, "reservation")
        with self.transaction(immediate=True) as connection:
            updated = connection.execute(
                "UPDATE delivery_intents SET state = ?, reserved_at = NULL, lease_owner = NULL, "
                "lease_expires_at = NULL, not_before_utc = ?, eligibility_reason = ?, updated_at = ? "
                "WHERE id = ? AND state = ?",
                (
                    DeliveryState.ELIGIBLE_PENDING.value,
                    not_before_utc,
                    reason,
                    utc_now(),
                    intent_id,
                    DeliveryState.RESERVED.value,
                ),
            )
            if updated.rowcount:
                connection.execute(
                    "UPDATE delivery_attempts SET attempt_state = ?, outcome = ?, error_code = ?, "
                    "finished_at = ?, transport_phase = ? WHERE intent_id = ? "
                    "AND attempt_state = ? AND attempt_number = "
                    "(SELECT MAX(attempt_number) FROM delivery_attempts WHERE intent_id = ?)",
                    (
                        DeliveryState.CANCELED.value,
                        AttemptOutcome.CANCELED.value,
                        "reservation_released_before_send",
                        utc_now(),
                        TransportPhase.NOT_STARTED.value,
                        intent_id,
                        DeliveryState.RESERVED.value,
                        intent_id,
                    ),
                )
            connection.execute(
                "UPDATE policy_reservations SET released_at = ? "
                "WHERE intent_id = ? AND released_at IS NULL",
                (utc_now(), intent_id),
            )

    def release_policy_reservation(self, attempt_id: str) -> None:
        """Release sender capacity while leaving the delivery reservation intact."""

        with self.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE policy_reservations SET released_at = ? "
                "WHERE attempt_id = ? AND released_at IS NULL",
                (utc_now(), attempt_id),
            )

    def set_intent_state(self, intent_id: str, state: DeliveryState, reason: str) -> None:
        _assert_no_secrets({"reason": reason}, "delivery_intent")
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE delivery_intents SET state = ?, eligibility_reason = ?, updated_at = ? WHERE id = ?",
                (state.value, reason, utc_now(), intent_id),
            )

    def reconcile_attempt(self, attempt_id: str, decision: str) -> None:
        from mailforge.domain.campaign import AttemptOutcome
        from mailforge.domain.outbound import ReconciliationDecision

        choice = ReconciliationDecision(decision)
        if choice is ReconciliationDecision.KEEP_AMBIGUOUS:
            return
        mapping = {
            ReconciliationDecision.ACCEPTED: AttemptOutcome.ACCEPTED,
            ReconciliationDecision.PERMANENT_FAILURE: AttemptOutcome.PERMANENT_FAILURE,
            ReconciliationDecision.TRANSIENT_FAILURE: AttemptOutcome.TRANSIENT_FAILURE,
        }
        with self.connect() as connection:
            row = connection.execute(
                "SELECT da.intent_id, da.attempt_number, da.attempt_state, da.outcome, "
                "di.state AS intent_state FROM delivery_attempts da "
                "JOIN delivery_intents di ON di.id = da.intent_id WHERE da.id = ?",
                (attempt_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"attempt not found: {attempt_id}")
        latest = self.latest_attempt(row["intent_id"])
        if (
            row["intent_state"] != DeliveryState.AMBIGUOUS.value
            or row["attempt_state"] != DeliveryState.AMBIGUOUS.value
            or row["outcome"] != AttemptOutcome.AMBIGUOUS.value
            or latest is None
            or latest["id"] != attempt_id
        ):
            raise ValueError("only the latest ambiguous attempt can be reconciled")
        self.record_delivery_result(
            intent_id=row["intent_id"],
            attempt_id=attempt_id,
            result=mapping[choice],
            error_code="operator_reconciled",
            error_message="operator reconciliation decision",
            transport_id=latest["transport_id"] if latest else None,
        )

    def mark_intent_suppressed(self, intent_id: str, reason: str) -> None:
        with self.transaction(immediate=True) as connection:
            updated = connection.execute(
                "UPDATE delivery_intents SET state = ?, eligibility_reason = ?, updated_at = ? "
                "WHERE id = ? AND state = ?",
                (
                    DeliveryState.SUPPRESSED.value,
                    reason,
                    utc_now(),
                    intent_id,
                    DeliveryState.RESERVED.value,
                ),
            )
            if updated.rowcount != 1:
                return
            connection.execute(
                "UPDATE delivery_attempts SET attempt_state = ?, outcome = ?, error_code = ?, "
                "finished_at = ? WHERE intent_id = ? AND attempt_state = ?",
                (
                    DeliveryState.CANCELED.value,
                    AttemptOutcome.CANCELED.value,
                    "suppressed_before_send",
                    utc_now(),
                    intent_id,
                    DeliveryState.RESERVED.value,
                ),
            )
            self.record_event(
                connection, "delivery_intent", intent_id, "intent_suppressed", {"reason": reason}
            )

    def request_pause(self, campaign_id: str) -> None:
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE campaigns SET pause_requested = 1, updated_at = ? WHERE id = ?",
                (utc_now(), campaign_id),
            )
            self.record_event(connection, "campaign", campaign_id, "pause_requested")

    def request_cancel(self, campaign_id: str) -> None:
        with self.transaction(immediate=True) as connection:
            now = utc_now()
            connection.execute(
                "UPDATE campaigns SET cancel_requested = 1, updated_at = ? WHERE id = ?",
                (now, campaign_id),
            )
            connection.execute(
                "UPDATE delivery_intents SET state = ?, updated_at = ? WHERE campaign_id = ? "
                "AND state IN (?, ?, ?, ?, ?)",
                (
                    DeliveryState.CANCELED.value,
                    now,
                    campaign_id,
                    DeliveryState.ELIGIBLE_PENDING.value,
                    DeliveryState.PREPARED.value,
                    DeliveryState.DEFERRED.value,
                    DeliveryState.RESERVED.value,
                    DeliveryState.ATTEMPT_STARTED.value,
                ),
            )
            connection.execute(
                "UPDATE delivery_attempts SET attempt_state = ?, outcome = ?, error_code = ?, "
                "finished_at = ?, transport_phase = ? WHERE intent_id IN "
                "(SELECT id FROM delivery_intents WHERE campaign_id = ? AND state = ?) "
                "AND attempt_state = ?",
                (
                    DeliveryState.CANCELED.value,
                    AttemptOutcome.CANCELED.value,
                    "campaign_canceled_before_send",
                    now,
                    TransportPhase.NOT_STARTED.value,
                    campaign_id,
                    DeliveryState.CANCELED.value,
                    DeliveryState.RESERVED.value,
                ),
            )
            connection.execute(
                "UPDATE policy_reservations SET released_at = ? WHERE intent_id IN "
                "(SELECT id FROM delivery_intents WHERE campaign_id = ? AND state = ?) "
                "AND released_at IS NULL",
                (now, campaign_id, DeliveryState.CANCELED.value),
            )
            self.record_event(connection, "campaign", campaign_id, "cancel_requested")

    def campaign_flags(self, campaign_id: str) -> tuple[bool, bool]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT pause_requested, cancel_requested FROM campaigns WHERE id = ?",
                (campaign_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"campaign not found: {campaign_id}")
        return bool(row["pause_requested"]), bool(row["cancel_requested"])

    def clear_pause_request(self, campaign_id: str) -> None:
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE campaigns SET pause_requested = 0, updated_at = ? WHERE id = ?",
                (utc_now(), campaign_id),
            )

    def mark_attempt_started(
        self,
        intent_id: str,
        attempt_id: str,
        *,
        expected_policy_revision: int | None = None,
    ) -> bool:
        with self.transaction(immediate=True) as connection:
            if expected_policy_revision is not None:
                revision = connection.execute(
                    "SELECT revision FROM policy_state WHERE id = 1"
                ).fetchone()["revision"]
                if revision != expected_policy_revision:
                    return False
            now = utc_now()
            updated = connection.execute(
                "UPDATE delivery_intents SET state = ?, updated_at = ? WHERE id = ? AND state = ?",
                (DeliveryState.ATTEMPT_STARTED.value, now, intent_id, DeliveryState.RESERVED.value),
            ).rowcount
            if updated != 1:
                return False
            connection.execute(
                "UPDATE delivery_attempts SET attempt_state = ?, started_at = ?, transport_phase = ? WHERE id = ?",
                (
                    DeliveryState.ATTEMPT_STARTED.value,
                    now,
                    TransportPhase.BEFORE_DATA.value,
                    attempt_id,
                ),
            )
            self.record_event(
                connection,
                "delivery_intent",
                intent_id,
                "attempt_started",
                {"attempt_id": attempt_id},
            )
            return True

    def record_delivery_result(
        self,
        *,
        intent_id: str,
        attempt_id: str,
        result: AttemptOutcome,
        error_code: str | None = None,
        error_message: str | None = None,
        transport_id: str | None = None,
        transport_phase: str | None = None,
        response_code: int | None = None,
    ) -> None:
        _assert_safe_reference(error_code, "error_code")
        _assert_safe_reference(transport_id, "transport_id")
        _assert_safe_reference(transport_phase, "transport_phase")
        _assert_no_secrets({"error_message": error_message or ""}, "delivery_result")
        mapping = {
            AttemptOutcome.ACCEPTED: DeliveryState.ACCEPTED,
            AttemptOutcome.TRANSIENT_FAILURE: DeliveryState.DEFERRED,
            AttemptOutcome.PERMANENT_FAILURE: DeliveryState.PERMANENT_FAILURE,
            AttemptOutcome.AMBIGUOUS: DeliveryState.AMBIGUOUS,
            AttemptOutcome.CANCELED: DeliveryState.CANCELED,
            AttemptOutcome.LEGACY_RECORDED: DeliveryState.AMBIGUOUS,
        }
        state = mapping[result]
        with self.transaction(immediate=True) as connection:
            now = utc_now()
            connection.execute(
                "UPDATE delivery_intents SET state = ?, lease_owner = NULL, lease_expires_at = NULL, "
                "reserved_at = NULL, updated_at = ? WHERE id = ? "
                "AND state IN (?, ?, ?)",
                (
                    state.value,
                    now,
                    intent_id,
                    DeliveryState.RESERVED.value,
                    DeliveryState.ATTEMPT_STARTED.value,
                    DeliveryState.AMBIGUOUS.value,
                ),
            )
            updated = connection.execute(
                "UPDATE delivery_attempts SET attempt_state = ?, outcome = ?, transport_id = ?, error_code = ?, "
                "error_message = ?, finished_at = ?, transport_phase = ?, response_code = ? WHERE id = ? "
                "AND attempt_state IN (?, ?, ?)",
                (
                    state.value,
                    result.value,
                    transport_id,
                    error_code,
                    error_message,
                    now,
                    transport_phase
                    or (
                        TransportPhase.ACCEPTED.value
                        if result is AttemptOutcome.ACCEPTED
                        else TransportPhase.UNKNOWN.value
                    ),
                    response_code,
                    attempt_id,
                    DeliveryState.RESERVED.value,
                    DeliveryState.ATTEMPT_STARTED.value,
                    DeliveryState.AMBIGUOUS.value,
                ),
            )
            if updated.rowcount != 1:
                raise ValueError("delivery attempt is no longer active or reconcilable")
            connection.execute(
                "UPDATE policy_reservations SET released_at = ? WHERE attempt_id = ? AND released_at IS NULL",
                (now, attempt_id),
            )
            policy_row = connection.execute(
                "SELECT sp.cooldown_seconds FROM sender_policies sp "
                "JOIN delivery_attempts da ON da.sender_policy_ref = sp.ref WHERE da.id = ?",
                (attempt_id,),
            ).fetchone()
            if policy_row and policy_row["cooldown_seconds"]:
                cooldown_until = (
                    datetime.fromisoformat(now) + timedelta(seconds=policy_row["cooldown_seconds"])
                ).isoformat()
                connection.execute(
                    "UPDATE sender_policies SET cooldown_until = ? WHERE ref = "
                    "(SELECT sender_policy_ref FROM delivery_attempts WHERE id = ?)",
                    (cooldown_until, attempt_id),
                )
            self.record_event(
                connection,
                "delivery_intent",
                intent_id,
                "delivery_outcome_recorded",
                {"outcome": result.value, "attempt_id": attempt_id, "error_code": error_code},
            )

    def recover_inflight(self, campaign_id: str, *, force: bool = True) -> dict[str, int]:
        recovered = {"reserved": 0, "ambiguous": 0}
        with self.transaction(immediate=True) as connection:
            if force:
                rows = connection.execute(
                    "SELECT id, state FROM delivery_intents WHERE campaign_id = ? AND state IN (?, ?)",
                    (
                        campaign_id,
                        DeliveryState.RESERVED.value,
                        DeliveryState.ATTEMPT_STARTED.value,
                    ),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT id, state FROM delivery_intents WHERE campaign_id = ? AND "
                    "((state = ? OR state = ?) AND "
                    "(lease_expires_at IS NULL OR lease_expires_at <= ?)) ",
                    (
                        campaign_id,
                        DeliveryState.RESERVED.value,
                        DeliveryState.ATTEMPT_STARTED.value,
                        utc_now(),
                    ),
                ).fetchall()
            for row in rows:
                if row["state"] == DeliveryState.RESERVED.value:
                    connection.execute(
                        "UPDATE delivery_intents SET state = ?, reserved_at = NULL, lease_owner = NULL, "
                        "lease_expires_at = NULL, updated_at = ? WHERE id = ?",
                        (DeliveryState.ELIGIBLE_PENDING.value, utc_now(), row["id"]),
                    )
                    connection.execute(
                        "UPDATE delivery_attempts SET attempt_state = ?, outcome = ?, "
                        "error_code = ?, finished_at = ?, transport_phase = ? WHERE intent_id = ? "
                        "AND attempt_number = (SELECT MAX(attempt_number) FROM delivery_attempts WHERE intent_id = ?)",
                        (
                            DeliveryState.CANCELED.value,
                            AttemptOutcome.CANCELED.value,
                            "reservation_recovered",
                            utc_now(),
                            TransportPhase.NOT_STARTED.value,
                            row["id"],
                            row["id"],
                        ),
                    )
                    recovered["reserved"] += 1
                else:
                    connection.execute(
                        "UPDATE delivery_intents SET state = ?, lease_owner = NULL, lease_expires_at = NULL, "
                        "reserved_at = NULL, updated_at = ? WHERE id = ?",
                        (DeliveryState.AMBIGUOUS.value, utc_now(), row["id"]),
                    )
                    connection.execute(
                        "UPDATE delivery_attempts SET attempt_state = ?, outcome = ?, "
                        "error_code = ?, finished_at = ?, transport_phase = ? WHERE intent_id = ? "
                        "AND attempt_number = (SELECT MAX(attempt_number) FROM delivery_attempts WHERE intent_id = ?)",
                        (
                            DeliveryState.AMBIGUOUS.value,
                            AttemptOutcome.AMBIGUOUS.value,
                            "process_recovery",
                            utc_now(),
                            TransportPhase.UNKNOWN.value,
                            row["id"],
                            row["id"],
                        ),
                    )
                    connection.execute(
                        "UPDATE policy_reservations SET released_at = ? WHERE intent_id = ? AND released_at IS NULL",
                        (utc_now(), row["id"]),
                    )
                    recovered["ambiguous"] += 1
                self.record_event(connection, "delivery_intent", row["id"], "inflight_recovered")
        return recovered

    # Inbound operations share this store so acquisition, outbound evidence,
    # suppression, and operational history remain one transactional authority.
    def register_inbound_source(
        self,
        ref: str,
        source_type: InboundSourceType | str,
        *,
        mailbox: str | None = None,
        trust_status: TrustStatus = TrustStatus.UNKNOWN,
        retention_policy: str = "retain",
        configuration: dict[str, Any] | None = None,
    ) -> None:
        _assert_safe_reference(ref, "inbound_source.ref")
        _assert_no_secrets(configuration or {}, "inbound_source.configuration")
        if retention_policy not in {"retain", "metadata_only"}:
            raise ValueError("unsupported inbound retention policy")
        now = utc_now()
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO inbound_sources(ref, source_type, mailbox, trust_status, retention_policy, configuration_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(ref) DO UPDATE SET source_type=excluded.source_type, mailbox=excluded.mailbox, "
                "trust_status=excluded.trust_status, retention_policy=excluded.retention_policy, configuration_json=excluded.configuration_json, updated_at=excluded.updated_at",
                (
                    ref,
                    str(source_type),
                    mailbox,
                    str(trust_status),
                    retention_policy,
                    _json(configuration or {}),
                    now,
                    now,
                ),
            )

    def get_inbound_source(self, ref: str) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM inbound_sources WHERE ref = ?", (ref,)
            ).fetchone()
        if row is None:
            raise KeyError(f"inbound source not found: {ref}")
        return row

    def start_inbound_run(self, source_ref: str) -> tuple[str, str | None]:
        run_id = str(uuid.uuid4())
        with self.transaction(immediate=True) as connection:
            source = connection.execute(
                "SELECT ref FROM inbound_sources WHERE ref = ? AND enabled = 1", (source_ref,)
            ).fetchone()
            if source is None:
                raise KeyError(f"enabled inbound source not found: {source_ref}")
            cursor = connection.execute(
                "SELECT cursor FROM inbound_cursors WHERE source_ref = ?", (source_ref,)
            ).fetchone()
            connection.execute(
                "INSERT INTO inbound_runs(id, source_ref, started_at, status, cursor_before) VALUES (?, ?, ?, 'running', ?)",
                (run_id, source_ref, utc_now(), cursor["cursor"] if cursor else None),
            )
        return run_id, (cursor["cursor"] if cursor else None)

    def finish_inbound_run(
        self,
        run_id: str,
        *,
        status: str,
        cursor_after: str | None,
        acquired: int,
        processed: int,
        failures: int,
        error_message: str | None = None,
    ) -> None:
        _assert_no_secrets({"error_message": error_message or ""}, "inbound_run")
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE inbound_runs SET finished_at = ?, status = ?, cursor_after = ?, acquired_count = ?, processed_count = ?, failure_count = ?, error_message = ? WHERE id = ?",
                (
                    utc_now(),
                    status,
                    cursor_after,
                    acquired,
                    processed,
                    failures,
                    error_message,
                    run_id,
                ),
            )

    def set_inbound_cursor(
        self, source_ref: str, cursor: str | None, *, uidvalidity: str | None = None
    ) -> None:
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO inbound_cursors(source_ref, uidvalidity, cursor, generation, updated_at) VALUES (?, ?, ?, 0, ?) "
                "ON CONFLICT(source_ref) DO UPDATE SET uidvalidity=excluded.uidvalidity, cursor=excluded.cursor, generation=inbound_cursors.generation + 1, updated_at=excluded.updated_at, last_error=NULL",
                (source_ref, uidvalidity, cursor, utc_now()),
            )

    def invalidate_inbound_cursor(self, source_ref: str, error: str) -> None:
        _assert_no_secrets({"error": error}, "inbound_cursor")
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE inbound_cursors SET cursor = NULL, generation = generation + 1, last_error = ?, updated_at = ? WHERE source_ref = ?",
                (error, utc_now(), source_ref),
            )

    def acquire_inbound_message(
        self, source: SourceMessage, *, retain_raw: bool = True
    ) -> tuple[str, bool]:
        content_hash = hashlib.sha256(source.raw_bytes).hexdigest()
        with self.transaction(immediate=True) as connection:
            source_identity = source.source_identity
            existing = connection.execute(
                "SELECT id FROM inbound_messages WHERE source_ref = ? AND source_identity = ?",
                (source.source_ref, source_identity),
            ).fetchone()
            if existing:
                stored = connection.execute(
                    "SELECT content_hash FROM inbound_messages WHERE id = ?", (existing["id"],)
                ).fetchone()
                if stored["content_hash"] == content_hash:
                    return existing["id"], True
                source_identity = f"{source_identity}:sha256:{content_hash}"
                changed = connection.execute(
                    "SELECT id, content_hash FROM inbound_messages WHERE source_ref = ? AND source_identity = ?",
                    (source.source_ref, source_identity),
                ).fetchone()
                if changed:
                    return changed["id"], changed["content_hash"] == content_hash
            message_id = str(
                uuid.uuid5(uuid.NAMESPACE_URL, f"inbound:{source.source_ref}:{source_identity}")
            )
            connection.execute(
                "INSERT INTO inbound_messages(id, source_ref, source_identity, content_hash, raw_bytes, raw_retained, size_bytes, received_at, acquired_at, account_ref, folder, retention_state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    message_id,
                    source.source_ref,
                    source_identity,
                    content_hash,
                    source.raw_bytes if retain_raw else None,
                    int(retain_raw),
                    len(source.raw_bytes),
                    source.received_at,
                    utc_now(),
                    source.source_ref if source.mailbox else None,
                    source.mailbox,
                    "retained" if retain_raw else "released",
                ),
            )
            self.record_event(
                connection,
                "inbound_message",
                message_id,
                "inbound_message_acquired",
                {"source_ref": source.source_ref, "size_bytes": len(source.raw_bytes)},
            )
        return message_id, False

    def get_inbound_message(self, message_id: str) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM inbound_messages WHERE id = ?", (message_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"inbound message not found: {message_id}")
        return row

    def release_inbound_raw(self, message_id: str) -> None:
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE inbound_messages SET raw_bytes = NULL, raw_retained = 0, retention_state = 'released' WHERE id = ?",
                (message_id,),
            )

    def mark_inbound_processed(self, message_id: str) -> None:
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE inbound_messages SET processed_at = ? WHERE id = ?",
                (utc_now(), message_id),
            )

    def list_inbound_messages(
        self, *, limit: int = 100, message_id: str | None = None
    ) -> list[sqlite3.Row]:
        with self.connect() as connection:
            if message_id:
                rows = connection.execute(
                    "SELECT * FROM inbound_messages WHERE id = ?", (message_id,)
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM inbound_messages ORDER BY acquired_at DESC LIMIT ?", (limit,)
                ).fetchall()
        return list(rows)

    def list_pending_inbound_messages(self, source_ref: str) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    "SELECT * FROM inbound_messages WHERE source_ref = ? AND processed_at IS NULL ORDER BY acquired_at",
                    (source_ref,),
                )
            )

    def save_inbound_parse(self, message_id: str, parsed: ParsedInbound) -> None:
        _assert_no_secrets(
            {
                "subject": parsed.subject or "",
                "text_preview": parsed.text_preview,
                "html_preview": parsed.html_preview,
            },
            "inbound_parse",
        )
        reasons = [issue.code for issue in parsed.issues]
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE inbound_messages SET content_hash = ?, size_bytes = ?, message_id = ?, in_reply_to = ?, references_json = ?, sender = ?, recipients_json = ?, subject = ?, parse_status = ?, parse_reason = ?, headers_json = ?, text_preview = ?, html_preview = ? WHERE id = ?",
                (
                    parsed.content_hash,
                    parsed.size_bytes,
                    parsed.message_id,
                    parsed.in_reply_to,
                    _json(list(parsed.references)),
                    parsed.sender,
                    _json(list(parsed.recipients)),
                    parsed.subject,
                    parsed.status.value,
                    _json(reasons),
                    _json(parsed.headers),
                    parsed.text_preview,
                    parsed.html_preview,
                    message_id,
                ),
            )
            connection.execute(
                "DELETE FROM inbound_attachments WHERE message_id = ?", (message_id,)
            )
            for attachment in parsed.attachments:
                connection.execute(
                    "INSERT OR REPLACE INTO inbound_attachments(id, message_id, part_path, filename, media_type, size_bytes, content_id, disposition, sha256, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(
                            uuid.uuid5(
                                uuid.NAMESPACE_URL,
                                f"inbound-attachment:{message_id}:{attachment.part_path}",
                            )
                        ),
                        message_id,
                        attachment.part_path,
                        attachment.filename,
                        attachment.media_type,
                        attachment.size_bytes,
                        attachment.content_id,
                        attachment.disposition,
                        attachment.sha256,
                        utc_now(),
                    ),
                )
            if parsed.dsn:
                connection.execute(
                    "INSERT OR REPLACE INTO inbound_dsn_reports(message_id, reporting_mta, original_envelope_id, arrival_date, fields_json) VALUES (?, ?, ?, ?, ?)",
                    (
                        message_id,
                        parsed.dsn.reporting_mta,
                        parsed.dsn.original_envelope_id,
                        parsed.dsn.arrival_date,
                        _json(parsed.dsn.fields),
                    ),
                )
                connection.execute(
                    "DELETE FROM inbound_dsn_recipients WHERE message_id = ?", (message_id,)
                )
                for index, recipient in enumerate(parsed.dsn.recipients):
                    connection.execute(
                        "INSERT INTO inbound_dsn_recipients(id, message_id, recipient_index, original_recipient, final_recipient, action, status, diagnostic_code, last_attempt_date, retry_until, fields_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            str(uuid.uuid4()),
                            message_id,
                            index,
                            recipient.original_recipient,
                            recipient.final_recipient,
                            recipient.action.value,
                            recipient.status,
                            recipient.diagnostic_code,
                            recipient.last_attempt_date,
                            recipient.retry_until,
                            _json(recipient.fields),
                        ),
                    )
            if parsed.complaint:
                complaint = parsed.complaint
                connection.execute(
                    "INSERT OR REPLACE INTO inbound_complaints(message_id, feedback_type, original_message_id, original_recipient, source, abuse_indicator, fields_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        message_id,
                        complaint.feedback_type,
                        complaint.original_message_id,
                        complaint.original_recipient,
                        complaint.source,
                        int(complaint.abuse_indicator),
                        _json(complaint.fields),
                    ),
                )
            self.record_event(
                connection,
                "inbound_message",
                message_id,
                "inbound_message_parsed",
                {"status": parsed.status.value, "issue_count": len(parsed.issues)},
            )

    def save_inbound_classification(
        self, message_id: str, kind: InboundKind, reasons: tuple[str, ...], signals: dict[str, str]
    ) -> None:
        _assert_no_secrets(signals, "inbound_signals")
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO inbound_classifications(id, message_id, kind, reasons_json, signals_json, occurred_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(message_id, kind) DO UPDATE SET reasons_json=excluded.reasons_json, signals_json=excluded.signals_json, occurred_at=excluded.occurred_at",
                (
                    str(
                        uuid.uuid5(uuid.NAMESPACE_URL, f"classification:{message_id}:{kind.value}")
                    ),
                    message_id,
                    kind.value,
                    _json(list(reasons)),
                    _json(signals),
                    utc_now(),
                ),
            )

    def save_inbound_correlation(
        self, message_id: str, correlation: Correlation, trust_status: TrustStatus
    ) -> str:
        correlation_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"inbound-correlation:{message_id}:{correlation.kind.value}:{correlation.entity_id}:{_json(list(correlation.evidence))}:{trust_status.value}",
            )
        )
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO inbound_correlations(id, message_id, entity_type, entity_id, match_kind, evidence_json, trust_status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    correlation_id,
                    message_id,
                    correlation.entity_type,
                    correlation.entity_id,
                    correlation.kind.value,
                    _json(list(correlation.evidence)),
                    trust_status.value,
                    utc_now(),
                ),
            )
        return correlation_id

    def propose_inbound_action(
        self,
        message_id: str,
        action_type: ActionType,
        target: str | None,
        reason: str,
        *,
        status: ActionStatus = ActionStatus.PROPOSED,
    ) -> tuple[str, bool]:
        _assert_no_secrets({"reason": reason}, "inbound_action")
        key = f"{message_id}:{action_type.value}:{target or ''}"
        action_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"inbound-action:{key}"))
        with self.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT id FROM inbound_actions WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if existing:
                return existing["id"], True
            connection.execute(
                "INSERT INTO inbound_actions(id, message_id, action_type, target, status, reason, idempotency_key, proposed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    action_id,
                    message_id,
                    action_type.value,
                    target,
                    status.value,
                    reason,
                    key,
                    utc_now(),
                ),
            )
        return action_id, False

    def create_inbound_review(self, message_id: str, action_id: str, reason: str) -> str:
        review_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"inbound-review:{action_id}"))
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO inbound_reviews(id, message_id, action_id, status, reason, created_at, updated_at) VALUES (?, ?, ?, 'pending', ?, ?, ?)",
                (review_id, message_id, action_id, reason, utc_now(), utc_now()),
            )
        return review_id

    def get_inbound_action(self, action_id: str) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM inbound_actions WHERE id = ?", (action_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"inbound action not found: {action_id}")
        return row

    def list_inbound_reviews(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    "SELECT r.*, a.action_type, a.target, a.reason AS action_reason FROM inbound_reviews r JOIN inbound_actions a ON a.id = r.action_id WHERE r.status = 'pending' ORDER BY r.created_at"
                )
            )

    def decide_inbound_action(self, action_id: str, decision: str, reason: str) -> None:
        if decision not in {"approve", "reject"}:
            raise ValueError("decision must be approve or reject")
        _assert_no_secrets({"reason": reason}, "inbound_decision")
        with self.transaction(immediate=True) as connection:
            action = connection.execute(
                "SELECT * FROM inbound_actions WHERE id = ?", (action_id,)
            ).fetchone()
            if action is None:
                raise KeyError(f"inbound action not found: {action_id}")
            if action["status"] in {ActionStatus.APPLIED.value, ActionStatus.REJECTED.value}:
                return
            now = utc_now()
            status = (
                ActionStatus.APPLIED.value if decision == "approve" else ActionStatus.REJECTED.value
            )
            if decision == "approve" and action["target"]:
                category = {
                    ActionType.CREATE_HARD_BOUNCE.value: SuppressionCategory.HARD_BOUNCE,
                    ActionType.CREATE_COMPLAINT.value: SuppressionCategory.COMPLAINT,
                    ActionType.CREATE_UNSUBSCRIBE.value: SuppressionCategory.UNSUBSCRIBE,
                }.get(action["action_type"])
                if category:
                    normalized = _validated_email(action["target"])
                    suppression_id = self._record_suppression(
                        connection,
                        category=category,
                        reason=reason,
                        normalized_email=normalized,
                        contact_id=None,
                        scope_type="global",
                        scope_id=None,
                        actor=f"inbound:{action_id}",
                        source="trusted-inbound",
                        suppression_id=str(
                            uuid.uuid5(
                                uuid.NAMESPACE_URL,
                                f"inbound-suppression:{category.value}:{normalized}",
                            )
                        ),
                        metadata={"action_id": action_id},
                    )
                    changed = connection.execute(
                        "UPDATE delivery_intents SET state = ?, eligibility_reason = ?, updated_at = ? "
                        "WHERE normalized_email = ? AND state IN (?, ?)",
                        (
                            DeliveryState.SUPPRESSED.value,
                            f"{category.value}: {reason}",
                            now,
                            normalized,
                            DeliveryState.ELIGIBLE_PENDING.value,
                            DeliveryState.PREPARED.value,
                        ),
                    ).rowcount
                    if changed:
                        self.record_event(
                            connection,
                            "delivery_intent",
                            normalized,
                            "inbound_suppression_blocked_intent",
                            {"category": category.value, "count": changed},
                        )
                    self.record_event(
                        connection,
                        "suppression",
                        suppression_id,
                        "inbound_suppression_applied",
                        {"action_id": action_id, "category": category.value},
                    )
            connection.execute(
                "UPDATE inbound_actions SET status = ?, applied_at = CASE WHEN ? = 'approve' THEN ? ELSE applied_at END, decided_at = ?, decision_reason = ? WHERE id = ?",
                (status, decision, now, now, reason, action_id),
            )
            connection.execute(
                "UPDATE inbound_reviews SET status = ?, updated_at = ? WHERE action_id = ?",
                ("approved" if decision == "approve" else "rejected", now, action_id),
            )
            self.record_event(
                connection,
                "inbound_action",
                action_id,
                "inbound_action_decided",
                {"decision": decision},
            )

    def find_outbound_candidates(
        self, identifiers: tuple[str, ...], recipients: tuple[str, ...]
    ) -> list[sqlite3.Row]:
        values = tuple(identifier for identifier in identifiers if identifier)
        with self.connect() as connection:
            rows: list[sqlite3.Row] = []
            if values:
                marks = ",".join("?" for _ in values)
                rows.extend(
                    connection.execute(
                        "SELECT pm.message_id, pm.recipient, di.id AS intent_id, da.id AS attempt_id, da.attempt_number, di.normalized_email FROM prepared_messages pm JOIN delivery_intents di ON di.id = pm.intent_id LEFT JOIN delivery_attempts da ON da.intent_id = di.id AND da.attempt_number = (SELECT MAX(attempt_number) FROM delivery_attempts WHERE intent_id = di.id) WHERE pm.message_id IN ("
                        + marks
                        + ")",
                        values,
                    ).fetchall()
                )
            if recipients:
                marks = ",".join("?" for _ in recipients)
                rows.extend(
                    connection.execute(
                        "SELECT pm.message_id, pm.recipient, di.id AS intent_id, da.id AS attempt_id, da.attempt_number, di.normalized_email FROM prepared_messages pm JOIN delivery_intents di ON di.id = pm.intent_id LEFT JOIN delivery_attempts da ON da.intent_id = di.id AND da.attempt_number = (SELECT MAX(attempt_number) FROM delivery_attempts WHERE intent_id = di.id) WHERE lower(pm.recipient) IN ("
                        + marks
                        + ")",
                        tuple(value.lower() for value in recipients),
                    ).fetchall()
                )
        seen: set[tuple[str, str]] = set()
        unique: list[sqlite3.Row] = []
        for row in rows:
            key = (row["intent_id"], row["message_id"])
            if key not in seen:
                seen.add(key)
                unique.append(row)
        return unique

    def inbound_stats(self) -> dict[str, int]:
        with self.connect() as connection:
            result = {
                "messages": connection.execute("SELECT COUNT(*) FROM inbound_messages").fetchone()[
                    0
                ],
                "reviews": connection.execute(
                    "SELECT COUNT(*) FROM inbound_reviews WHERE status = 'pending'"
                ).fetchone()[0],
                "actions_applied": connection.execute(
                    "SELECT COUNT(*) FROM inbound_actions WHERE status = 'applied'"
                ).fetchone()[0],
            }
            for row in connection.execute(
                "SELECT kind, COUNT(*) AS count FROM inbound_classifications GROUP BY kind"
            ):
                result[f"kind_{row['kind']}"] = row["count"]
        return result

    def latest_attempt(self, intent_id: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM delivery_attempts WHERE intent_id = ? ORDER BY attempt_number DESC LIMIT 1",
                (intent_id,),
            ).fetchone()
