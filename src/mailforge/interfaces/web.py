"""Secure local web application for the Mailforge operator console."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from mailforge.application.automation import AutomationError, AutomationService
from mailforge.application.campaign_operations import (
    CampaignOperationsError,
    CampaignOperationsService,
)
from mailforge.application.campaigns import CampaignService
from mailforge.application.data_connections import DataConnectionError, DataConnectionsService
from mailforge.application.data_studio import DataStudioError, DataStudioService, detect_schema
from mailforge.application.email_standards import (
    MAX_REPORT_BYTES,
    EmailStandardsError,
    EmailStandardsService,
)
from mailforge.application.inbound import InboundService
from mailforge.application.inbound_operations import InboundOperations, InboundOperationsError
from mailforge.application.inbound_sources import EmlFileSource, MboxSource
from mailforge.application.ingest import iter_raw_values
from mailforge.application.integration_hub import IntegrationHubError, IntegrationHubService
from mailforge.application.list_processing import ListProcessingError, ListProcessingService
from mailforge.application.local_application import (
    DataDirectory,
    application_status,
    backup_database_snapshot,
    complete_onboarding,
    create_backup,
    current_schema_version,
    list_backups,
    restore_backup,
    utc_now,
)
from mailforge.application.message_studio import MessageStudioError, MessageStudioService
from mailforge.application.outbound import OutboundService
from mailforge.application.programmable_operations import ProgrammableOperationsService
from mailforge.application.provider_operations import ProviderOperationsService
from mailforge.application.providers import ProviderService
from mailforge.application.reporting import ReportFilters, ReportingError, ReportingService
from mailforge.application.segmentation import SegmentationError, SegmentationService
from mailforge.domain.campaign import CampaignStatus, ContactCandidate
from mailforge.domain.email import extract_addresses, normalize_email, validate_email
from mailforge.domain.outbound import (
    CatchUpPolicy,
    ScheduleMode,
    ScheduleSpec,
    SenderPolicy,
    TemplateDefinition,
)
from mailforge.domain.provider import (
    AuthenticationMethod,
    ExternalOperationGrant,
    ProviderKind,
    ProviderOperation,
    ProviderProfile,
    TLSMode,
)
from mailforge.infrastructure.campaign_store import SCHEMA_VERSION, CampaignStore
from mailforge.infrastructure.credentials import EnvironmentCredentialResolver

API_PREFIX = "/api/v1"
SESSION_COOKIE = "mailforge_session"
CSRF_COOKIE = "mailforge_csrf"
CSRF_HEADER = "x-mailforge-csrf"


class ListInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)


class DataStudioRecipeInput(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=500)
    definition: dict[str, Any] = Field(default_factory=lambda: {"steps": []})


class DataConnectionInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    source_type: str
    config: dict[str, Any] = Field(default_factory=dict)
    credential_ref: str | None = Field(default=None, max_length=200)
    selection: dict[str, Any] = Field(default_factory=dict)
    limits: dict[str, Any] = Field(default_factory=dict)


class DataConnectionSelectionInput(BaseModel):
    selection: dict[str, Any] = Field(default_factory=dict)
    limit: int = Field(default=50, ge=1, le=500)


class DataConnectionSnapshotInput(BaseModel):
    selection: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=200)
    wait: bool = False


class DataConnectionHandoffInput(BaseModel):
    recipe_revision_id: str
    correlation_id: str | None = Field(default=None, max_length=240)


class IntegrationClientInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    scopes: list[str] = Field(min_length=1, max_length=5)
    expires_at: str


class IntegrationClientRotateInput(BaseModel):
    expires_at: str | None = None


class IntegrationEventInput(BaseModel):
    event_id: str = Field(min_length=1, max_length=160)
    source: str = Field(min_length=1, max_length=200)
    type: str = Field(min_length=1, max_length=120)
    time: str
    schema_version: str = Field(min_length=1, max_length=32)
    payload: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)


class IntegrationWebhookInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    endpoint_url: str = Field(min_length=8, max_length=1_000)
    event_types: list[str] = Field(min_length=1, max_length=20)
    secret_ref: str = Field(min_length=5, max_length=200)
    payload_version: str = Field(default="1", min_length=1, max_length=20)
    scope: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = False
    local_fixture: bool = False


class IntegrationWebhookStateInput(BaseModel):
    enabled: bool


class DataStudioRevisionInput(BaseModel):
    definition: dict[str, Any]
    expected_revision_id: str | None = None


class ProgrammableFunctionInput(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    version: str = Field(default="1.0", min_length=1, max_length=32)
    definition: dict[str, Any]


class ProgrammablePreviewInput(BaseModel):
    records: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
    parameters: dict[str, Any] = Field(default_factory=dict)
    revision_id: str | None = None


class ProgrammableRevisionInput(BaseModel):
    version: str | None = Field(default=None, max_length=32)
    definition: dict[str, Any]


class SenderIdentityInput(BaseModel):
    identity_ref: str = Field(min_length=1, max_length=120)
    visible_from: str = Field(min_length=3, max_length=320)
    reply_to: str | None = Field(default=None, max_length=320)
    envelope_from: str | None = Field(default=None, max_length=320)
    dkim_domain: str | None = Field(default=None, max_length=253)
    dkim_selector: str | None = Field(default=None, max_length=120)
    provider_account_ref: str | None = Field(default=None, max_length=120)
    provider_signing_status: str = Field(default="unknown", max_length=64)


class DomainDiagnosticInput(BaseModel):
    domain: str = Field(min_length=1, max_length=253)
    sender_ip: str | None = Field(default=None, max_length=64)
    envelope_from: str | None = Field(default=None, max_length=320)
    dkim_selector: str | None = Field(default=None, max_length=120)
    allow_network: bool = False


class MessageDiagnosticInput(BaseModel):
    message: str = Field(min_length=1, max_length=25 * 1024 * 1024)
    communication_purpose: str = Field(default="transactional", max_length=80)
    expected_identity: str | None = Field(default=None, max_length=120)


class UnsubscribeEndpointInput(BaseModel):
    endpoint_ref: str = Field(min_length=1, max_length=120)
    base_url: str = Field(min_length=8, max_length=500)
    verification_status: str = Field(default="not_configured", max_length=64)


class UnsubscribeTokenInput(BaseModel):
    list_id: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=320)
    ttl_days: int = Field(default=30, ge=1, le=365)


class UnsubscribeRequestInput(BaseModel):
    body: str = Field(max_length=100)


class UnsubscribeReadinessInput(BaseModel):
    communication_purpose: str = Field(default="marketing", max_length=80)
    endpoint_ref: str | None = Field(default=None, max_length=120)
    message: str | None = Field(default=None, max_length=25 * 1024 * 1024)


class DataStudioContactImportInput(BaseModel):
    list_id: str
    mappings: dict[str, str]
    overwrite: bool = False
    source_ref: str = Field(default="data-studio-artifact", max_length=200)
    max_rows: int = Field(default=10_000, ge=1, le=100_000)


class DataStudioMessagePreviewInput(BaseModel):
    revision_id: str
    contact_id: str | None = None
    field_mapping: dict[str, str] = Field(default_factory=dict)
    row_number: int = Field(default=1, ge=1, le=100_000)


class DataStudioCampaignPrepareInput(BaseModel):
    source_job_id: str
    source_artifact_id: str
    recipe_revision_id: str
    message_revision_id: str
    field_mapping: dict[str, str]
    run_key: str = Field(default="primary", min_length=1, max_length=120)


class CampaignInput(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=500)
    purpose: str = Field(default="transactional", min_length=1, max_length=80)
    list_id: str | None = None
    segment_id: str | None = None
    segment_revision_id: str | None = None
    sender_email: str = "sender@example.com"
    subject: str = Field(default="Synthetic Mailforge campaign", max_length=200)
    text: str = Field(default="Hello {{ email }}", max_length=100_000)
    unsubscribe_endpoint_ref: str | None = Field(default=None, max_length=120)
    one_click_unsubscribe: bool = False
    dkim_signed: bool = False
    message_revision_id: str | None = None
    provider_account_ref: str | None = None


class CampaignUpdateInput(CampaignInput):
    expected_updated_at: str | None = None


class CampaignScheduleInput(BaseModel):
    mode: ScheduleMode = ScheduleMode.IMMEDIATE
    timezone: str = Field(default="UTC", min_length=1, max_length=80)
    scheduled_at: str | None = Field(default=None, max_length=80)
    window_start: str | None = Field(default=None, max_length=20)
    window_end: str | None = Field(default=None, max_length=20)
    weekdays: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 6], max_length=7)
    pacing_seconds: float = Field(default=0.0, ge=0, le=86_400)
    cooldown_seconds: float = Field(default=0.0, ge=0, le=86_400)
    recurrence: str | None = Field(default=None, max_length=20)
    catch_up_policy: CatchUpPolicy = CatchUpPolicy.SKIP
    fold: int | None = Field(default=None, ge=0, le=1)


class SetupInput(BaseModel):
    timezone: str = Field(default="UTC", min_length=1, max_length=80)
    simulation_mode: bool = True
    sample_data: bool = False


class RestoreInput(BaseModel):
    backup_name: str = Field(min_length=1, max_length=200)
    destination_name: str = Field(min_length=1, max_length=120)


class MessageDocumentInput(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    kind: str = "message"
    document: dict[str, Any] | None = None


class MessageSaveInput(BaseModel):
    document: dict[str, Any]
    expected_revision_id: str | None = None


class MessageRevisionInput(BaseModel):
    expected_revision_id: str | None = None


class MessagePreviewInput(BaseModel):
    document: dict[str, Any] | None = None
    revision_id: str | None = None
    contact_id: str | None = None
    condition_mode: str | None = None


class MessageAttachmentMapInput(BaseModel):
    revision_id: str
    contact_id: str
    asset_id: str
    required: bool = True


class AutomationInput(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=500)


class AutomationRevisionInput(BaseModel):
    graph: dict[str, Any] | None = None
    revision_id: str | None = None


class AutomationEnrollInput(BaseModel):
    contact_id: str | None = None
    event_kind: str = "manual_enrollment"
    source_identity: str | None = Field(default=None, max_length=240)
    subject_key: str | None = Field(default=None, max_length=240)
    payload: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = Field(default=None, max_length=240)


class PrepareInput(BaseModel):
    sender_email: str | None = None
    subject: str | None = Field(default=None, max_length=200)
    text: str | None = Field(default=None, max_length=100_000)
    run_key: str = Field(default="primary", min_length=1, max_length=120)


class ProviderInput(BaseModel):
    ref: str = Field(min_length=1, max_length=120)
    kind: ProviderKind
    host: str
    port: int = Field(ge=1, le=65535)
    username: str
    credential_ref: str
    tls_mode: TLSMode
    auth_method: AuthenticationMethod = AuthenticationMethod.PASSWORD
    mailbox: str = "INBOX"
    sender_policy_ref: str | None = None
    display_name: str | None = Field(default=None, max_length=160)
    sender_email: str | None = None
    pacing_per_minute: int = Field(default=60, ge=1, le=100_000)
    max_concurrency: int = Field(default=1, ge=1, le=100)
    inbound_enabled: bool = False


class ProviderAccountUpdateInput(BaseModel):
    status: str | None = Field(default=None, max_length=64)
    enabled: bool | None = None
    outbound_enabled: bool | None = None
    inbound_enabled: bool | None = None
    display_name: str | None = Field(default=None, max_length=160)


class ProviderDiagnosticInput(BaseModel):
    operation: ProviderOperation
    authorization_ref: str | None = Field(default=None, max_length=200)
    purpose: str | None = Field(default=None, max_length=500)
    sender: str | None = None
    recipient: str | None = None
    max_submissions: int = Field(default=0, ge=0, le=1)
    expires_at: str | None = None


class ProviderInboundInput(BaseModel):
    mailbox: str = Field(min_length=1, max_length=240)
    max_fetch: int = Field(default=100, ge=1, le=10_000)


class ProviderTestSendInput(BaseModel):
    sender: str
    recipient: str
    message: str = Field(min_length=1, max_length=10_000_000)
    idempotency_key: str = Field(min_length=8, max_length=200)
    authorization_ref: str = Field(min_length=1, max_length=200)
    purpose: str = Field(min_length=1, max_length=500)
    expires_at: str
    message_revision_id: str | None = None


class ProviderOAuthStartInput(BaseModel):
    provider_key: str = Field(min_length=1, max_length=80)
    client_id: str = Field(min_length=1, max_length=300)
    redirect_uri: str = Field(min_length=1, max_length=500)
    scopes: list[str] = Field(default_factory=list, max_length=20)
    authorization_endpoint: str = Field(min_length=1, max_length=500)
    token_endpoint: str = Field(min_length=1, max_length=500)


class ProviderOAuthCallbackInput(BaseModel):
    state: str = Field(min_length=1, max_length=300)
    redirect_uri: str = Field(min_length=1, max_length=500)
    code: str = Field(min_length=1, max_length=4096)
    identity: str | None = Field(default=None, max_length=320)


class ReviewDecision(BaseModel):
    decision: str
    reason: str = Field(min_length=1, max_length=500)


class InboundRuleInput(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    definition: dict[str, Any]
    priority: int = Field(default=0, ge=-100_000, le=100_000)


class InboundRuleStatusInput(BaseModel):
    status: str


class InboundExtractInput(BaseModel):
    fields: list[str] | None = Field(default=None, max_length=20)


class InboundDraftInput(BaseModel):
    kind: str
    body: str = Field(default="", max_length=20_000)
    account_ref: str | None = Field(default=None, max_length=120)


class InboundContactInput(BaseModel):
    list_id: str
    field: str = "sender"


class InboundMailboxActionInput(BaseModel):
    action: str
    authorization_ref: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=8, max_length=200)
    target_folder: str | None = Field(default=None, max_length=240)


class InboundSyncInput(BaseModel):
    batch_limit: int = Field(default=100, ge=1, le=10_000)
    retain_raw: bool = True
    authorization_ref: str | None = Field(default=None, max_length=200)
    purpose: str | None = Field(default=None, max_length=500)
    expires_at: str | None = None


class FieldInput(BaseModel):
    machine_key: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=120)
    field_type: str
    validation: dict[str, Any] = Field(default_factory=dict)
    controlled_values: list[Any] | None = None


class FieldUpdateInput(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=120)
    field_type: str | None = None


class SegmentInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    definition: dict[str, Any]
    expected_revision_id: str | None = None


class StructuredImportInput(BaseModel):
    list_id: str
    rows: list[dict[str, Any]] = Field(max_length=10_000)
    mappings: dict[str, str]
    overwrite: bool = False
    source_ref: str = Field(default="browser-structured-import", max_length=200)


class LocalSession:
    def __init__(self, allowed_hosts: set[str]):
        self.session_id = secrets.token_urlsafe(32)
        self.csrf_token = secrets.token_urlsafe(32)
        self.allowed_hosts = allowed_hosts


class LocalSecurityMiddleware(BaseHTTPMiddleware):
    """Host, origin, session, and CSRF boundary for a loopback-only console."""

    def __init__(self, app, session: LocalSession):
        super().__init__(app)
        self.session = session

    async def dispatch(self, request: Request, call_next):
        raw_host = request.headers.get("host", "").lower()
        host = (urlsplit(f"//{raw_host}").hostname or "").lower()
        if host not in self.session.allowed_hosts:
            return _error(400, "invalid_host", "Mailforge only accepts loopback Host headers")
        one_click_path = request.url.path.startswith(f"{API_PREFIX}/unsubscribe/")
        integration_event_path = (
            request.url.path == f"{API_PREFIX}/integrations/events" and request.method == "POST"
        )
        if (
            request.url.path.startswith(API_PREFIX)
            and request.url.path != f"{API_PREFIX}/session"
            and not one_click_path
            and not integration_event_path
        ):
            if request.cookies.get(SESSION_COOKIE) != self.session.session_id:
                return _error(401, "session_required", "a local operator session is required")
        if (
            request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and not one_click_path
            and not integration_event_path
        ):
            origin = request.headers.get("origin")
            if origin:
                parsed = urlsplit(origin)
                origin_host = (parsed.hostname or "").lower()
                host_header = raw_host
                if (
                    parsed.scheme != "http"
                    or origin_host not in self.session.allowed_hosts
                    or parsed.netloc.lower() != host_header
                ):
                    return _error(403, "invalid_origin", "request origin is not accepted")
            if request.cookies.get(SESSION_COOKIE) != self.session.session_id:
                return _error(401, "session_required", "a local operator session is required")
            if request.headers.get(CSRF_HEADER) != self.session.csrf_token:
                return _error(403, "csrf_failed", "the CSRF token is missing or invalid")
        response = await call_next(request)
        response.headers.setdefault(
            "content-security-policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
        )
        response.headers.setdefault("x-content-type-options", "nosniff")
        response.headers.setdefault("referrer-policy", "no-referrer")
        return response


class WorkerSupervisor:
    """Bounded in-process worker supervisor; all state remains durable in SQLite."""

    def __init__(self, outbound: OutboundService):
        self.outbound = outbound
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mailforge-worker")
        self._lock = threading.Lock()
        self._running: set[str] = set()

    def start(self, campaign_id: str) -> bool:
        with self._lock:
            if campaign_id in self._running:
                return False
            self._running.add(campaign_id)
        self.executor.submit(self._run, campaign_id)
        return True

    def _run(self, campaign_id: str) -> None:
        try:
            while True:
                report, _transport = self.outbound.execute_worker(
                    campaign_id,
                    worker_id=f"web-{threading.get_ident()}",
                    max_items=1,
                )
                if report.campaign_status in {"completed", "paused", "canceled", "failed"}:
                    break
                time.sleep(0.03)
        except Exception:
            try:
                campaign = self.outbound.store.get_campaign(campaign_id)
                if campaign["status"] in {"running", "pausing"}:
                    self.outbound.store.transition_campaign(
                        campaign_id, CampaignStatus.FAILED, "worker_failed"
                    )
            except Exception:
                # Preserve the durable attempt evidence if a second transition is unsafe.
                pass
        finally:
            with self._lock:
                self._running.discard(campaign_id)

    def active(self, campaign_id: str) -> bool:
        with self._lock:
            return campaign_id in self._running

    def shutdown(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=False)


class CampaignScheduleSupervisor:
    """Starts only authorized, due synthetic schedules from durable rows."""

    def __init__(self, store: CampaignStore, outbound: OutboundService, workers: WorkerSupervisor):
        self.store = store
        self.outbound = outbound
        self.workers = workers
        self.stop = threading.Event()
        self.thread = threading.Thread(
            target=self._run, name="mailforge-campaign-scheduler", daemon=True
        )
        self.thread.start()

    def _run(self) -> None:
        while not self.stop.is_set():
            try:
                with self.store.connect() as connection:
                    rows = connection.execute(
                        "SELECT c.id, s.* FROM campaigns c JOIN campaign_schedules s "
                        "ON s.campaign_id=c.id WHERE c.status='scheduled' "
                        "AND c.authorization='simulation_authorized'"
                    ).fetchall()
                for row in rows:
                    schedule = ScheduleSpec(
                        mode=ScheduleMode(row["mode"]),
                        timezone=row["timezone"],
                        scheduled_at=row["scheduled_at"],
                        window_start=row["window_start"],
                        window_end=row["window_end"],
                        weekdays=tuple(json.loads(row["weekdays_json"] or "[]")),
                        pacing_seconds=float(row["pacing_seconds"]),
                        cooldown_seconds=float(row["cooldown_seconds"]),
                        recurrence=row["recurrence"],
                        catch_up_policy=CatchUpPolicy(row["catch_up_policy"]),
                        fold=row["fold"],
                    )
                    if self.outbound.schedules.due(schedule, self.outbound.now_fn()):
                        self.workers.start(row["id"])
            except Exception:
                pass
            self.stop.wait(0.1)

    def shutdown(self) -> None:
        self.stop.set()
        self.thread.join(timeout=2)


class AutomationWorkerSupervisor:
    """Bounded synthetic workflow worker; waits and history remain durable."""

    def __init__(self, automation: AutomationService):
        self.automation = automation
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mailforge-automation")
        self._lock = threading.Lock()
        self._running: set[str] = set()

    def start(self, run_id: str) -> bool:
        with self._lock:
            if run_id in self._running:
                return False
            self._running.add(run_id)
        self.executor.submit(self._run, run_id)
        return True

    def _run(self, run_id: str) -> None:
        try:
            for _ in range(50):
                detail = self.automation.advance(
                    run_id, max_steps=20, worker_id=f"web-{threading.get_ident()}"
                )
                status = detail["run"]["status"]
                if status not in {"running", "pending"}:
                    break
                time.sleep(0.03)
        except Exception as error:
            # Never leave a durable run claiming it is running after a worker
            # exception; the operator must see and explicitly recover it.
            try:
                self.automation.fail_run(run_id, "worker_exception", type(error).__name__)
            except Exception:
                pass
        finally:
            with self._lock:
                self._running.discard(run_id)

    def active(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._running

    def shutdown(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=False)


class AutomationRuntimeSupervisor:
    """Polls durable facts and schedules; SQLite remains the source of truth."""

    def __init__(
        self, automation: AutomationService, workers: AutomationWorkerSupervisor, integrations=None
    ):
        self.automation = automation
        self.workers = workers
        self.integrations = integrations
        self.stop = threading.Event()
        self.thread = threading.Thread(
            target=self._run, name="mailforge-automation-supervisor", daemon=True
        )
        self.thread.start()

    def _run(self) -> None:
        while not self.stop.is_set():
            try:
                run_ids = self.automation.reconcile_operational_events(limit=100)
                run_ids.extend(self.automation.reconcile_schedules(limit=25))
                run_ids.extend(self.automation.due_run_ids(limit=25))
                if self.integrations is not None:
                    self.integrations.process_due_deliveries(limit=25)
                for run_id in dict.fromkeys(run_ids):
                    self.workers.start(run_id)
            except Exception:
                # The next bounded poll retries durable work and leaves the
                # operator-visible run/event state intact.
                pass
            self.stop.wait(0.1)

    def shutdown(self) -> None:
        self.stop.set()
        self.thread.join(timeout=2)


class WebRuntime:
    def __init__(
        self,
        database: str | Path,
        frontend_dir: str | Path | None = None,
        data_directory: str | Path | None = None,
    ):
        self.data_directory = DataDirectory.from_path(data_directory or Path(database).parent)
        self.data_directory.initialize()
        self.runtime_id = uuid.uuid4().hex
        self.started_at = utc_now()
        database_path = Path(database)
        current_schema = current_schema_version(database_path)
        if current_schema > SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema {current_schema} is newer than this Mailforge build; "
                "upgrade is refused"
            )
        if 0 < current_schema < SCHEMA_VERSION:
            snapshot = self.data_directory.backups / (
                f"pre-migration-v{current_schema}-{datetime.now().strftime('%Y%m%dT%H%M%S')}.sqlite3"
            )
            if not snapshot.exists():
                backup_database_snapshot(database_path, snapshot)
        self.store = CampaignStore(database)
        if database_path.exists():
            database_path.chmod(0o600)
        self.campaigns = CampaignService(self.store)
        self.segmentation = SegmentationService(self.store)
        self.outbound = OutboundService(self.store)
        self.programmable = ProgrammableOperationsService(
            self.store, extension_root=self.data_directory.root / "extensions"
        )
        self.data_studio = DataStudioService(self.store, programmable=self.programmable)
        self.data_connections = DataConnectionsService(
            self.store,
            root=self.data_directory.artifacts / "data-connections",
            data_studio=self.data_studio,
        )
        self.message_studio = MessageStudioService(
            self.store,
            outbound=self.outbound,
            segmentation=self.segmentation,
            data_studio=self.data_studio,
            programmable=self.programmable,
        )
        self.inbound = InboundService(self.store)
        self.providers = ProviderService(self.store)
        self.provider_operations = ProviderOperationsService(self.store)
        self.reporting = ReportingService(self.store)
        self.email_standards = EmailStandardsService(self.store)
        self.workers = WorkerSupervisor(self.outbound)
        self.campaign_scheduler = CampaignScheduleSupervisor(
            self.store, self.outbound, self.workers
        )
        self.campaign_operations = CampaignOperationsService(
            self.store,
            campaigns=self.campaigns,
            outbound=self.outbound,
            message_studio=self.message_studio,
            segmentation=self.segmentation,
            provider_operations=self.provider_operations,
            reporting=self.reporting,
            email_standards=self.email_standards,
            worker_active=self.workers.active,
            worker_start=self.workers.start,
        )
        self.automation = AutomationService(
            self.store,
            campaigns=self.campaigns,
            message_studio=self.message_studio,
            data_studio=self.data_studio,
            programmable=self.programmable,
        )
        self.integrations = IntegrationHubService(
            self.store,
            automation=self.automation,
        )
        self.inbound_operations = InboundOperations(
            self.store, message_studio=self.message_studio, programmable=self.programmable
        )
        self.inbound.set_operations(self.inbound_operations)
        self.automation_workers = AutomationWorkerSupervisor(self.automation)
        self.automation_runtime = AutomationRuntimeSupervisor(
            self.automation, self.automation_workers, self.integrations
        )
        self.list_processing = ListProcessingService(self.store)
        self.frontend_dir = Path(frontend_dir) if frontend_dir else None
        self._recover_running_campaigns()

    def _recover_running_campaigns(self) -> None:
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM campaigns WHERE status IN ('running', 'pausing')"
            ).fetchall()
        for row in rows:
            campaign_id = row["id"]
            self.store.recover_inflight(campaign_id, force=False)
            try:
                current = CampaignStatus(self.store.get_campaign(campaign_id)["status"])
                if current is CampaignStatus.RUNNING:
                    self.store.transition_campaign(
                        campaign_id, CampaignStatus.PAUSING, "application_restarted"
                    )
                if current in {CampaignStatus.RUNNING, CampaignStatus.PAUSING}:
                    self.store.transition_campaign(
                        campaign_id, CampaignStatus.PAUSED, "application_restarted"
                    )
            except ValueError:
                pass

    def close(self) -> None:
        self.automation_runtime.shutdown()
        self.list_processing.close()
        self.data_connections.close()
        self.data_studio.close()
        self.campaign_scheduler.shutdown()
        self.workers.shutdown()
        self.automation_workers.shutdown()

    def status(self) -> dict[str, Any]:
        status = application_status(self.data_directory)
        status.update(
            {
                "runtime_id": self.runtime_id,
                "started_at": self.started_at,
                "pid": os.getpid(),
                "worker_status": {
                    "campaigns_active": len(self.workers._running),
                    "automations_active": len(self.automation_workers._running),
                    "scheduler": "running"
                    if self.campaign_scheduler.thread.is_alive()
                    else "stopped",
                    "automation_scheduler": (
                        "running" if self.automation_runtime.thread.is_alive() else "stopped"
                    ),
                },
                "loopback_only": True,
                "simulation_mode": True,
            }
        )
        return status


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def _bearer(request: Request) -> str:
    value = request.headers.get("authorization", "")
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise IntegrationHubError("a bearer integration token is required", "unauthorized")
    return token


def _row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for key, value in list(result.items()):
        if isinstance(value, str) and key.endswith("_json"):
            try:
                result[key.removesuffix("_json")] = json.loads(value)
            except json.JSONDecodeError:
                pass
            del result[key]
    return result


def _query(store: CampaignStore, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with store.connect() as connection:
        return [_row(row) for row in connection.execute(sql, params).fetchall()]


def _report_filters(
    *,
    preset: str,
    start: str | None,
    end: str | None,
    timezone: str,
    provider_ref: str | None,
    campaign_id: str | None,
    workflow_id: str | None,
    limit: int,
    offset: int,
    source_ref: str | None = None,
    account_ref: str | None = None,
    folder: str | None = None,
    job_kind: str | None = None,
) -> ReportFilters:
    return ReportFilters(
        preset=preset,
        start=start,
        end=end,
        timezone=timezone,
        provider_ref=provider_ref,
        campaign_id=campaign_id,
        workflow_id=workflow_id,
        source_ref=source_ref,
        account_ref=account_ref,
        folder=folder,
        job_kind=job_kind,
        limit=limit,
        offset=offset,
    )


def _require_list(store: CampaignStore, list_id: str) -> None:
    with store.connect() as connection:
        if connection.execute("SELECT 1 FROM lists WHERE id = ?", (list_id,)).fetchone() is None:
            raise KeyError(f"list not found: {list_id}")


def _campaign_view(runtime: WebRuntime, row: sqlite3.Row) -> dict[str, Any]:
    campaign = _row(row)
    campaign.pop("configuration", None)
    report = runtime.campaigns.report(row["id"])
    campaign["report"] = report.to_dict()
    campaign["worker_active"] = runtime.workers.active(row["id"])
    return campaign


def _upload_preview(upload: UploadFile) -> tuple[list[ContactCandidate], dict[str, Any]]:
    raw = upload.file.read(16 * 1024 * 1024 + 1)
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError("input exceeds the 16 MiB local import limit")
    suffix = Path(upload.filename or "contacts.txt").suffix.lower()
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as handle:
        handle.write(raw)
        handle.flush()
        candidates: list[ContactCandidate] = []
        seen: set[str] = set()
        invalid = 0
        duplicate = 0
        source_position = 0
        for value in iter_raw_values(Path(handle.name)):
            source_position += 1
            extracted = extract_addresses(value)
            if not extracted:
                invalid += 1
                continue
            for item in extracted:
                normalized = normalize_email(item)
                if not validate_email(normalized).valid:
                    invalid += 1
                    continue
                if normalized in seen:
                    duplicate += 1
                    continue
                seen.add(normalized)
                candidates.append(
                    ContactCandidate(
                        normalized_email=normalized,
                        original_input=item,
                        source_file=upload.filename or "browser-import",
                        source_record_position=source_position,
                        metadata={},
                    )
                )
    return candidates, {
        "total_values": source_position,
        "accepted": len(candidates),
        "invalid": invalid,
        "duplicates": duplicate,
        "filename": upload.filename or "browser-import",
        "content_sha256": hashlib.sha256(raw).hexdigest(),
    }


def _upload_inbound(upload: UploadFile) -> tuple[Path, str]:
    """Copy one bounded EML upload to a private temporary file."""

    raw = upload.file.read(10 * 1024 * 1024 + 1)
    if len(raw) > 10 * 1024 * 1024:
        raise ValueError("inbound evidence exceeds the 10 MiB local acquisition limit")
    digest = hashlib.sha256(raw).hexdigest()
    handle = tempfile.NamedTemporaryFile(prefix="mailforge-inbound-", suffix=".eml", delete=False)
    try:
        handle.write(raw)
        handle.close()
    except Exception:
        handle.close()
        Path(handle.name).unlink(missing_ok=True)
        raise
    return Path(handle.name), digest


def _upload_list_tool(upload: UploadFile, directory: Path) -> Path:
    """Stream one bounded upload into an app-owned private directory."""

    directory.mkdir(parents=True, exist_ok=True)
    suffix = Path(upload.filename or "contacts.txt").suffix.lower()
    handle = tempfile.NamedTemporaryFile(
        prefix="list-input-", suffix=suffix, dir=directory, delete=False
    )
    total = 0
    try:
        while True:
            chunk = upload.file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > 1_073_741_824:
                raise ValueError("list input exceeds the 1 GiB local processing limit")
            handle.write(chunk)
        handle.close()
        return Path(handle.name)
    except Exception:
        handle.close()
        Path(handle.name).unlink(missing_ok=True)
        raise


def _upload_data_studio(upload: UploadFile, directory: Path) -> Path:
    """Retain a bounded structured source in the Data Studio app directory."""

    directory.mkdir(parents=True, exist_ok=True)
    suffix = Path(upload.filename or "data.csv").suffix.lower()
    handle = tempfile.NamedTemporaryFile(
        prefix="data-source-", suffix=suffix, dir=directory, delete=False
    )
    total = 0
    try:
        while True:
            chunk = upload.file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > 1_073_741_824:
                raise ValueError("structured input exceeds the 1 GiB local processing limit")
            handle.write(chunk)
        handle.close()
        return Path(handle.name)
    except Exception:
        handle.close()
        Path(handle.name).unlink(missing_ok=True)
        raise


def create_app(
    database: str | Path = "state/mailforge.sqlite3",
    *,
    frontend_dir: str | Path | None = None,
    data_directory: str | Path | None = None,
    allowed_hosts: set[str] | None = None,
) -> FastAPI:
    runtime = WebRuntime(database, frontend_dir, data_directory)
    session = LocalSession(allowed_hosts or {"127.0.0.1", "localhost", "::1"})

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        runtime.close()

    app = FastAPI(
        title="Mailforge Local Console",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.runtime = runtime
    app.state.session = session
    app.add_middleware(LocalSecurityMiddleware, session=session)

    @app.get("/healthz")
    async def healthz():
        return runtime.status()

    @app.get(f"{API_PREFIX}/session")
    async def session_endpoint():
        response = JSONResponse({"session": "local", "csrf_token": session.csrf_token})
        response.set_cookie(SESSION_COOKIE, session.session_id, httponly=True, samesite="strict")
        response.set_cookie(CSRF_COOKIE, session.csrf_token, httponly=False, samesite="strict")
        return response

    @app.get(f"{API_PREFIX}/status")
    async def status():
        payload = runtime.status()
        payload.update(
            {
                "mode": "local-simulation",
                "policy_revision": runtime.store.policy_revision(),
                "database_kind": "sqlite",
                "external_network": False,
            }
        )
        return payload

    @app.get(f"{API_PREFIX}/setup")
    async def setup():
        payload = runtime.status()
        return {
            "complete": payload["onboarding_complete"],
            "data_directory_id": payload["data_directory_id"],
            "preferences": payload.get("preferences", {}),
        }

    @app.post(f"{API_PREFIX}/setup/complete")
    async def setup_complete(payload: SetupInput):
        settings = complete_onboarding(
            runtime.data_directory,
            preferences=payload.model_dump(),
        )
        return {
            "complete": True,
            "preferences": settings["preferences"],
            "data_directory_id": runtime.data_directory.identity,
        }

    @app.get(f"{API_PREFIX}/runtime")
    async def runtime_status():
        return runtime.status()

    @app.get(f"{API_PREFIX}/runtime/backups")
    async def runtime_backups():
        return {"items": list_backups(runtime.data_directory)}

    @app.post(f"{API_PREFIX}/runtime/backup")
    async def runtime_backup():
        backup = create_backup(runtime.data_directory)
        return {"backup": backup.name, "metadata": list_backups(runtime.data_directory)[0]}

    @app.post(f"{API_PREFIX}/runtime/restore")
    async def runtime_restore(payload: RestoreInput):
        if Path(payload.backup_name).name != payload.backup_name:
            return _error(400, "backup_path_invalid", "choose a backup from the local backup list")
        if Path(payload.destination_name).name != payload.destination_name:
            return _error(
                400,
                "restore_destination_invalid",
                "restore destination must be a single local name",
            )
        source = runtime.data_directory.backups / payload.backup_name
        destination = runtime.data_directory.root / "restores" / payload.destination_name
        try:
            restored = restore_backup(source, destination)
        except (FileNotFoundError, FileExistsError, ValueError, RuntimeError) as error:
            return _error(400, "restore_rejected", str(error))
        return {
            "restored": True,
            "destination_id": DataDirectory.from_path(restored).identity,
            "database_schema_version": CampaignStore(
                restored / "mailforge.sqlite3"
            ).schema_version(),
        }

    @app.get(f"{API_PREFIX}/data-connections")
    async def data_connections(limit: int = 50, offset: int = 0):
        return runtime.data_connections.list_connections(limit=limit, offset=offset)

    @app.post(f"{API_PREFIX}/data-connections")
    async def create_data_connection(payload: DataConnectionInput):
        try:
            return runtime.data_connections.create_connection(
                payload.name,
                payload.source_type,
                payload.config,
                credential_ref=payload.credential_ref,
                selection=payload.selection,
                limits=payload.limits,
            )
        except (DataConnectionError, OSError, ValueError) as error:
            code = getattr(error, "code", "connection_invalid")
            return _error(409 if code == "connection_exists" else 400, code, str(error))

    @app.post(f"{API_PREFIX}/data-connections/sources")
    async def upload_data_connection_source(
        profile_name: str = Form(...),
        source_type: str = Form(...),
        selection: str = Form("{}"),
        limits: str = Form("{}"),
        file: UploadFile = File(...),  # noqa: B008
    ):
        temporary: Path | None = None
        retained: Path | None = None
        created = False
        try:
            parsed_selection = json.loads(selection)
            parsed_limits = json.loads(limits)
            if not isinstance(parsed_selection, dict) or not isinstance(parsed_limits, dict):
                raise ValueError("selection and limits must be JSON objects")
            temporary = _upload_data_studio(file, runtime.data_connections.root / "incoming")
            retained = runtime.data_connections.retain_source(
                temporary, file.filename or "source", source_type
            )
            result = runtime.data_connections.create_connection(
                profile_name,
                source_type,
                {"source_name": file.filename or retained.name},
                selection=parsed_selection,
                limits=parsed_limits,
                source_path=retained,
                source_filename=file.filename or retained.name,
            )
            created = True
            return result
        except (DataConnectionError, OSError, ValueError, json.JSONDecodeError) as error:
            code = getattr(error, "code", "source_invalid")
            return _error(409 if code == "connection_exists" else 400, code, str(error))
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
            if retained and not created:
                retained.unlink(missing_ok=True)

    @app.post(f"{API_PREFIX}/data-connections/{{profile_id}}/disable")
    async def disable_data_connection(profile_id: str):
        try:
            return runtime.data_connections.disable_connection(profile_id)
        except DataConnectionError as error:
            return _error(
                404 if error.code == "connection_not_found" else 400, error.code, str(error)
            )

    @app.post(f"{API_PREFIX}/data-connections/{{profile_id}}/test")
    async def test_data_connection(profile_id: str):
        try:
            return runtime.data_connections.test_connection(profile_id)
        except DataConnectionError as error:
            return _error(
                404
                if error.code == "connection_not_found"
                else 409
                if error.code == "connection_disabled"
                else 400,
                error.code,
                str(error),
            )

    @app.get(f"{API_PREFIX}/data-connections/{{profile_id}}/schema")
    async def data_connection_schema(profile_id: str):
        try:
            return runtime.data_connections.schema(profile_id)
        except DataConnectionError as error:
            return _error(
                404 if error.code == "connection_not_found" else 400, error.code, str(error)
            )

    @app.post(f"{API_PREFIX}/data-connections/{{profile_id}}/preview")
    async def data_connection_preview(profile_id: str, payload: DataConnectionSelectionInput):
        try:
            return runtime.data_connections.preview(
                profile_id, payload.selection, limit=payload.limit
            )
        except DataConnectionError as error:
            return _error(
                404
                if error.code == "connection_not_found"
                else 409
                if error.code == "connection_disabled"
                else 400,
                error.code,
                str(error),
            )

    @app.get(f"{API_PREFIX}/data-connections/{{profile_id}}/snapshots")
    async def data_connection_snapshots(profile_id: str, limit: int = 50, offset: int = 0):
        try:
            return runtime.data_connections.list_snapshots(profile_id, limit=limit, offset=offset)
        except DataConnectionError as error:
            return _error(404, error.code, str(error))

    @app.post(f"{API_PREFIX}/data-connections/{{profile_id}}/snapshots")
    async def create_data_connection_snapshot(
        profile_id: str, payload: DataConnectionSnapshotInput
    ):
        try:
            return runtime.data_connections.start_snapshot(
                profile_id,
                payload.selection,
                idempotency_key=payload.idempotency_key,
                wait=payload.wait,
            )
        except DataConnectionError as error:
            return _error(
                404
                if error.code == "connection_not_found"
                else 409
                if error.code in {"connection_exists", "connection_disabled"}
                else 400,
                error.code,
                str(error),
            )

    @app.get(f"{API_PREFIX}/data-connections/snapshots/{{snapshot_id}}")
    async def data_connection_snapshot(snapshot_id: str):
        try:
            return runtime.data_connections.get_snapshot(snapshot_id)
        except DataConnectionError as error:
            return _error(404, error.code, str(error))

    @app.get(f"{API_PREFIX}/data-connections/{{profile_id}}/events")
    async def data_connection_events(profile_id: str, limit: int = 100):
        try:
            return {"items": runtime.data_connections.events(profile_id, limit=limit)}
        except DataConnectionError as error:
            return _error(404, error.code, str(error))

    @app.post(f"{API_PREFIX}/data-connections/snapshots/{{snapshot_id}}/cancel")
    async def cancel_data_connection_snapshot(snapshot_id: str):
        try:
            return runtime.data_connections.cancel_snapshot(snapshot_id)
        except DataConnectionError as error:
            return _error(
                404 if error.code == "snapshot_not_found" else 409, error.code, str(error)
            )

    @app.post(f"{API_PREFIX}/data-connections/snapshots/{{snapshot_id}}/data-studio")
    async def handoff_data_connection_snapshot(
        snapshot_id: str, payload: DataConnectionHandoffInput
    ):
        try:
            return runtime.data_connections.handoff_to_data_studio(
                snapshot_id,
                payload.recipe_revision_id,
                correlation_id=payload.correlation_id,
            )
        except (DataConnectionError, DataStudioError) as error:
            code = getattr(error, "code", "handoff_invalid")
            return _error(
                404
                if code.endswith("_not_found")
                else 409
                if code.startswith("snapshot_")
                else 400,
                code,
                str(error),
            )

    @app.get(f"{API_PREFIX}/integrations")
    async def integrations_summary(limit: int = 50, offset: int = 0):
        try:
            return {
                "catalog": runtime.integrations.event_catalog(),
                "clients": runtime.integrations.list_clients(limit=limit, offset=offset),
                "subscriptions": runtime.integrations.list_subscriptions(
                    limit=limit, offset=offset
                ),
                "events": runtime.integrations.list_events(limit=limit, offset=offset),
                "deliveries": runtime.integrations.deliveries(limit=limit, offset=offset),
                "diagnostics": runtime.integrations.diagnostics(),
            }
        except IntegrationHubError as error:
            return _error(400, error.code, str(error))

    @app.get(f"{API_PREFIX}/integrations/clients")
    async def integration_clients(limit: int = 50, offset: int = 0):
        return runtime.integrations.list_clients(limit=limit, offset=offset)

    @app.post(f"{API_PREFIX}/integrations/clients")
    async def issue_integration_client(payload: IntegrationClientInput):
        try:
            return runtime.integrations.issue_client(**payload.model_dump())
        except IntegrationHubError as error:
            return _error(409 if error.code == "client_exists" else 400, error.code, str(error))

    @app.post(f"{API_PREFIX}/integrations/clients/{{client_ref}}/rotate")
    async def rotate_integration_client(client_ref: str, payload: IntegrationClientRotateInput):
        try:
            return runtime.integrations.rotate_client(client_ref, **payload.model_dump())
        except IntegrationHubError as error:
            return _error(404 if error.code == "client_not_found" else 400, error.code, str(error))

    @app.post(f"{API_PREFIX}/integrations/clients/{{client_ref}}/revoke")
    async def revoke_integration_client(client_ref: str):
        try:
            return runtime.integrations.revoke_client(client_ref)
        except IntegrationHubError as error:
            return _error(404 if error.code == "client_not_found" else 400, error.code, str(error))

    @app.get(f"{API_PREFIX}/integrations/events/catalog")
    async def integration_event_catalog():
        return runtime.integrations.event_catalog()

    @app.post(f"{API_PREFIX}/integrations/events")
    async def receive_integration_event(request: Request, payload: IntegrationEventInput):
        try:
            client = runtime.integrations.authenticate(_bearer(request), "INGEST_LOCAL_EVENT")
            return runtime.integrations.submit_event(client, payload.model_dump())
        except IntegrationHubError as error:
            status = (
                401 if error.code in {"unauthorized", "client_revoked", "client_expired"}
                else 409 if error.code == "event_conflict" else 400
            )
            return _error(status, error.code, str(error))

    @app.get(f"{API_PREFIX}/integrations/events")
    async def list_integration_events(limit: int = 50, offset: int = 0):
        return runtime.integrations.list_events(limit=limit, offset=offset)

    @app.get(f"{API_PREFIX}/integrations/webhooks")
    async def integration_webhooks(limit: int = 50, offset: int = 0):
        return runtime.integrations.list_subscriptions(limit=limit, offset=offset)

    @app.post(f"{API_PREFIX}/integrations/webhooks")
    async def create_integration_webhook(payload: IntegrationWebhookInput):
        try:
            return runtime.integrations.create_subscription(**payload.model_dump())
        except IntegrationHubError as error:
            return _error(
                409 if error.code == "subscription_exists" else 400, error.code, str(error)
            )

    @app.post(f"{API_PREFIX}/integrations/webhooks/{{subscription_ref}}/state")
    async def set_integration_webhook_state(
        subscription_ref: str, payload: IntegrationWebhookStateInput
    ):
        try:
            return runtime.integrations.set_subscription(subscription_ref, enabled=payload.enabled)
        except IntegrationHubError as error:
            return _error(
                404 if error.code == "subscription_not_found" else 400, error.code, str(error)
            )

    @app.get(f"{API_PREFIX}/integrations/webhooks/{{subscription_ref}}/preview")
    async def preview_integration_webhook(subscription_ref: str, event_type: str | None = None):
        try:
            return runtime.integrations.preview_subscription(
                subscription_ref, event_type=event_type
            )
        except IntegrationHubError as error:
            return _error(
                404 if error.code == "subscription_not_found" else 400, error.code, str(error)
            )

    @app.get(f"{API_PREFIX}/integrations/deliveries")
    async def integration_deliveries(
        subscription_ref: str | None = None, limit: int = 100, offset: int = 0
    ):
        return runtime.integrations.deliveries(subscription_ref, limit=limit, offset=offset)

    @app.post(f"{API_PREFIX}/integrations/deliveries/process")
    async def process_integration_deliveries():
        return {"items": runtime.integrations.process_due_deliveries()}

    @app.get(f"{API_PREFIX}/integrations/diagnostics")
    async def integration_diagnostics():
        return runtime.integrations.diagnostics()

    @app.get(f"{API_PREFIX}/dashboard")
    async def dashboard():
        campaigns = _query(
            runtime.store, "SELECT * FROM campaigns ORDER BY updated_at DESC LIMIT 20"
        )
        contacts = _query(runtime.store, "SELECT COUNT(*) AS count FROM contacts")[0]["count"]
        lists = _query(runtime.store, "SELECT COUNT(*) AS count FROM lists")[0]["count"]
        inbound = runtime.inbound.stats()
        return {
            "counts": {"contacts": contacts, "lists": lists, "inbound": inbound},
            "campaigns": [
                _campaign_view(runtime, runtime.store.get_campaign(item["id"]))
                for item in campaigns
            ],
        }

    @app.get(f"{API_PREFIX}/reports/definitions")
    async def report_definitions():
        return runtime.reporting.definitions()

    @app.get(f"{API_PREFIX}/reports/email-standards")
    async def report_email_standards(limit: int = 100):
        return runtime.reporting.email_standards(limit=limit)

    @app.get(f"{API_PREFIX}/reports/integration")
    async def report_integration(limit: int = 100):
        return runtime.reporting.integration(limit=limit)

    @app.get(f"{API_PREFIX}/reports/summary")
    async def report_summary(
        preset: str = "last_7_days",
        start: str | None = None,
        end: str | None = None,
        timezone: str = "UTC",
        provider_ref: str | None = None,
        campaign_id: str | None = None,
        workflow_id: str | None = None,
        source_ref: str | None = None,
        account_ref: str | None = None,
        folder: str | None = None,
        job_kind: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        try:
            return runtime.reporting.summary(
                _report_filters(
                    preset=preset,
                    start=start,
                    end=end,
                    timezone=timezone,
                    provider_ref=provider_ref,
                    campaign_id=campaign_id,
                    workflow_id=workflow_id,
                    limit=limit,
                    offset=offset,
                    source_ref=source_ref,
                    account_ref=account_ref,
                    folder=folder,
                    job_kind=job_kind,
                )
            )
        except ReportingError as error:
            return _error(400, error.code, str(error))

    @app.get(f"{API_PREFIX}/reports/campaigns")
    async def report_campaigns(
        preset: str = "last_7_days",
        start: str | None = None,
        end: str | None = None,
        timezone: str = "UTC",
        provider_ref: str | None = None,
        campaign_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        try:
            return runtime.reporting.campaigns(
                _report_filters(
                    preset=preset,
                    start=start,
                    end=end,
                    timezone=timezone,
                    provider_ref=provider_ref,
                    campaign_id=campaign_id,
                    workflow_id=None,
                    limit=limit,
                    offset=offset,
                )
            )
        except ReportingError as error:
            return _error(400, error.code, str(error))

    @app.get(f"{API_PREFIX}/reports/campaigns/{{campaign_id}}")
    async def report_campaign_detail(
        campaign_id: str,
        preset: str = "last_7_days",
        start: str | None = None,
        end: str | None = None,
        timezone: str = "UTC",
        limit: int = 50,
        offset: int = 0,
    ):
        try:
            return runtime.reporting.campaign_detail(
                campaign_id,
                _report_filters(
                    preset=preset,
                    start=start,
                    end=end,
                    timezone=timezone,
                    provider_ref=None,
                    campaign_id=campaign_id,
                    workflow_id=None,
                    limit=limit,
                    offset=offset,
                ),
            )
        except ReportingError as error:
            return _error(404 if error.code == "not_found" else 400, error.code, str(error))

    @app.get(f"{API_PREFIX}/reports/providers")
    async def report_providers(
        preset: str = "last_7_days",
        start: str | None = None,
        end: str | None = None,
        timezone: str = "UTC",
        provider_ref: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        try:
            return runtime.reporting.providers(
                _report_filters(
                    preset=preset,
                    start=start,
                    end=end,
                    timezone=timezone,
                    provider_ref=provider_ref,
                    campaign_id=None,
                    workflow_id=None,
                    limit=limit,
                    offset=offset,
                )
            )
        except ReportingError as error:
            return _error(400, error.code, str(error))

    @app.get(f"{API_PREFIX}/reports/workflows")
    async def report_workflows(
        preset: str = "last_7_days",
        start: str | None = None,
        end: str | None = None,
        timezone: str = "UTC",
        workflow_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        try:
            return runtime.reporting.workflows(
                _report_filters(
                    preset=preset,
                    start=start,
                    end=end,
                    timezone=timezone,
                    provider_ref=None,
                    campaign_id=None,
                    workflow_id=workflow_id,
                    limit=limit,
                    offset=offset,
                )
            )
        except ReportingError as error:
            return _error(400, error.code, str(error))

    @app.get(f"{API_PREFIX}/reports/inbound")
    async def report_inbound(
        preset: str = "last_7_days",
        start: str | None = None,
        end: str | None = None,
        timezone: str = "UTC",
        source_ref: str | None = None,
        account_ref: str | None = None,
        folder: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        try:
            return runtime.reporting.inbound(
                _report_filters(
                    preset=preset,
                    start=start,
                    end=end,
                    timezone=timezone,
                    provider_ref=None,
                    campaign_id=None,
                    workflow_id=None,
                    limit=limit,
                    offset=offset,
                    source_ref=source_ref,
                    account_ref=account_ref,
                    folder=folder,
                )
            )
        except ReportingError as error:
            return _error(400, error.code, str(error))

    @app.get(f"{API_PREFIX}/reports/jobs")
    async def report_jobs(
        preset: str = "last_7_days",
        start: str | None = None,
        end: str | None = None,
        timezone: str = "UTC",
        job_kind: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        try:
            return runtime.reporting.jobs(
                _report_filters(
                    preset=preset,
                    start=start,
                    end=end,
                    timezone=timezone,
                    provider_ref=None,
                    campaign_id=None,
                    workflow_id=None,
                    limit=limit,
                    offset=offset,
                    job_kind=job_kind,
                )
            )
        except ReportingError as error:
            return _error(400, error.code, str(error))

    @app.get(f"{API_PREFIX}/reports/export")
    async def report_export(
        report_type: str = "summary",
        format: str = "json",
        preset: str = "last_7_days",
        start: str | None = None,
        end: str | None = None,
        timezone: str = "UTC",
        provider_ref: str | None = None,
        campaign_id: str | None = None,
        workflow_id: str | None = None,
        source_ref: str | None = None,
        account_ref: str | None = None,
        folder: str | None = None,
        job_kind: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        try:
            content, media_type, filename = runtime.reporting.export(
                report_type,
                _report_filters(
                    preset=preset,
                    start=start,
                    end=end,
                    timezone=timezone,
                    provider_ref=provider_ref,
                    campaign_id=campaign_id,
                    workflow_id=workflow_id,
                    limit=limit,
                    offset=offset,
                    source_ref=source_ref,
                    account_ref=account_ref,
                    folder=folder,
                    job_kind=job_kind,
                ),
                format=format,
            )
            return Response(
                content=content,
                media_type=media_type,
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )
        except ReportingError as error:
            return _error(400, error.code, str(error))

    @app.get(f"{API_PREFIX}/lists")
    async def lists(limit: int = 100, offset: int = 0):
        safe_limit = max(1, min(limit, 500))
        safe_offset = max(0, offset)
        return {
            "items": _query(
                runtime.store,
                "SELECT l.*, COUNT(m.contact_id) AS contact_count "
                "FROM lists l LEFT JOIN list_memberships m ON m.list_id = l.id "
                "GROUP BY l.id ORDER BY l.created_at DESC LIMIT ? OFFSET ?",
                (safe_limit, safe_offset),
            )
        }

    @app.post(f"{API_PREFIX}/lists")
    async def create_list(payload: ListInput):
        try:
            list_id = runtime.store.create_list(payload.name.strip(), payload.description.strip())
        except sqlite3.IntegrityError:
            return _error(409, "list_exists", "a list with this name already exists")
        return {
            "id": list_id,
            "name": payload.name.strip(),
            "description": payload.description.strip(),
            "contact_count": 0,
        }

    @app.get(f"{API_PREFIX}/fields")
    async def fields(include_archived: bool = False):
        return {"items": runtime.segmentation.list_fields(include_archived=include_archived)}

    @app.get(f"{API_PREFIX}/message-studio/documents")
    async def message_documents(kind: str | None = None, limit: int = 100, offset: int = 0):
        return {
            "items": runtime.message_studio.list_documents(kind=kind, limit=limit, offset=offset)
        }

    @app.post(f"{API_PREFIX}/message-studio/documents")
    async def create_message_document(payload: MessageDocumentInput):
        try:
            return runtime.message_studio.create_document(
                payload.name, kind=payload.kind, document=payload.document
            )
        except MessageStudioError as error:
            return _error(400, error.code, str(error))

    @app.get(f"{API_PREFIX}/message-studio/documents/{{document_id}}")
    async def message_document(document_id: str):
        try:
            return runtime.message_studio.get_document(document_id)
        except MessageStudioError as error:
            return _error(404 if error.code.endswith("_not_found") else 400, error.code, str(error))

    @app.post(f"{API_PREFIX}/message-studio/documents/{{document_id}}/save")
    async def save_message_document(document_id: str, payload: MessageSaveInput):
        try:
            return runtime.message_studio.save_draft(
                document_id, payload.document, expected_revision_id=payload.expected_revision_id
            )
        except MessageStudioError as error:
            return _error(409 if error.code == "revision_conflict" else 400, error.code, str(error))

    @app.post(f"{API_PREFIX}/message-studio/documents/{{document_id}}/publish")
    async def publish_message_document(
        document_id: str, payload: MessageRevisionInput | None = None
    ):
        try:
            return runtime.message_studio.publish(
                document_id, expected_revision_id=payload.expected_revision_id if payload else None
            )
        except MessageStudioError as error:
            return _error(409 if error.code == "revision_conflict" else 400, error.code, str(error))

    @app.post(f"{API_PREFIX}/message-studio/documents/{{document_id}}/archive")
    async def archive_message_document(document_id: str):
        try:
            return runtime.message_studio.archive(document_id)
        except MessageStudioError as error:
            return _error(404 if error.code.endswith("_not_found") else 400, error.code, str(error))

    @app.post(f"{API_PREFIX}/message-studio/documents/{{document_id}}/duplicate")
    async def duplicate_message_document(
        document_id: str, payload: MessageDocumentInput | None = None
    ):
        try:
            return runtime.message_studio.duplicate(document_id, payload.name if payload else None)
        except MessageStudioError as error:
            return _error(400, error.code, str(error))

    @app.get(f"{API_PREFIX}/message-studio/templates")
    async def message_templates(limit: int = 100, offset: int = 0):
        return {
            "items": runtime.message_studio.list_documents(
                kind="template", limit=limit, offset=offset
            )
        }

    @app.post(f"{API_PREFIX}/message-studio/validate")
    async def validate_message_document(payload: MessageSaveInput):
        try:
            document = runtime.message_studio.validate_document(payload.document)
            return {
                "valid": True,
                "content_hash": hashlib.sha256(
                    json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
                "warnings": [],
            }
        except MessageStudioError as error:
            return _error(400, error.code, str(error))

    @app.post(f"{API_PREFIX}/message-studio/preview")
    async def preview_message_document(payload: MessagePreviewInput):
        try:
            return runtime.message_studio.preview(
                payload.document,
                revision_id=payload.revision_id,
                contact_id=payload.contact_id,
                condition_mode=payload.condition_mode,
            )
        except (MessageStudioError, KeyError) as error:
            return _error(400, getattr(error, "code", "preview_invalid"), str(error))

    @app.get(f"{API_PREFIX}/message-studio/assets")
    async def message_assets(kind: str | None = None):
        return {"items": runtime.message_studio.list_assets(kind=kind)}

    @app.get(f"{API_PREFIX}/message-studio/revisions/{{revision_id}}/recipient-attachments")
    async def recipient_attachment_mappings(revision_id: str):
        try:
            return {"items": runtime.message_studio.list_recipient_attachments(revision_id)}
        except MessageStudioError as error:
            return _error(400, error.code, str(error))

    @app.post(f"{API_PREFIX}/message-studio/recipient-attachments")
    async def map_recipient_attachment(payload: MessageAttachmentMapInput):
        try:
            return runtime.message_studio.map_recipient_attachment(**payload.model_dump())
        except MessageStudioError as error:
            status = 404 if error.code.endswith("_not_found") else 400
            return _error(status, error.code, str(error))

    @app.delete(f"{API_PREFIX}/message-studio/recipient-attachments")
    async def remove_recipient_attachment(payload: MessageAttachmentMapInput):
        try:
            runtime.message_studio.remove_recipient_attachment(
                payload.revision_id, payload.contact_id, payload.asset_id
            )
            return {"removed": True}
        except MessageStudioError as error:
            return _error(404, error.code, str(error))

    @app.post(f"{API_PREFIX}/message-studio/assets")
    async def upload_message_asset(file: UploadFile = File(...), kind: str = "attachment"):  # noqa: B008
        try:
            raw = file.file.read(10 * 1024 * 1024 + 1)
            if len(raw) > 10 * 1024 * 1024:
                return _error(413, "asset_limit", "asset exceeds the 10 MiB local limit")
            return runtime.message_studio.create_asset(
                file.filename or "attachment",
                file.content_type or "application/octet-stream",
                raw,
                kind=kind,
            )
        except MessageStudioError as error:
            return _error(400, error.code, str(error))

    @app.delete(f"{API_PREFIX}/message-studio/assets/{{asset_id}}")
    async def archive_message_asset(asset_id: str):
        try:
            runtime.message_studio.archive_asset(asset_id)
            return {"asset_id": asset_id, "status": "archived"}
        except MessageStudioError as error:
            return _error(409 if error.code == "asset_referenced" else 404, error.code, str(error))

    @app.get(f"{API_PREFIX}/message-studio/assets/{{asset_id}}")
    async def download_message_asset(asset_id: str):
        try:
            asset = runtime.message_studio.get_asset(asset_id, include_data=True)
            return Response(
                content=asset["data_blob"],
                media_type=asset["media_type"],
                headers={
                    "content-disposition": f'inline; filename="{asset["filename"]}"',
                    "cache-control": "no-store",
                },
            )
        except MessageStudioError as error:
            return _error(404, error.code, str(error))

    @app.post(f"{API_PREFIX}/fields")
    async def create_field(payload: FieldInput):
        try:
            return runtime.segmentation.create_field(
                payload.machine_key,
                payload.label,
                payload.field_type,
                validation=payload.validation,
                controlled_values=payload.controlled_values,
            )
        except SegmentationError as error:
            status = 409 if error.code == "field_exists" else 400
            return _error(status, error.code, str(error))

    @app.post(f"{API_PREFIX}/fields/{{field_id}}/archive")
    async def archive_field(field_id: str):
        try:
            runtime.segmentation.archive_field(field_id)
            return {"field_id": field_id, "status": "archived"}
        except SegmentationError as error:
            return _error(404 if error.code == "field_not_found" else 400, error.code, str(error))

    @app.patch(f"{API_PREFIX}/fields/{{field_id}}")
    async def update_field(field_id: str, payload: FieldUpdateInput):
        try:
            return runtime.segmentation.update_field(
                field_id, label=payload.label, field_type=payload.field_type
            )
        except SegmentationError as error:
            return _error(404 if error.code == "field_not_found" else 400, error.code, str(error))

    @app.post(f"{API_PREFIX}/imports/structured/preview")
    async def structured_import_preview(payload: StructuredImportInput):
        try:
            _require_list(runtime.store, payload.list_id)
            fields_by_id = {field["id"]: field for field in runtime.segmentation.list_fields()}
            missing = [
                field_id
                for field_id in payload.mappings.values()
                if field_id not in fields_by_id and field_id != "contact.email"
            ]
            if missing:
                return _error(400, "mapping_invalid", f"unknown fields: {', '.join(missing)}")
            checked = runtime.segmentation.validate_import_rows(
                payload.rows, mappings=payload.mappings, overwrite=payload.overwrite
            )
            return {
                "records": checked["records"],
                "mappings": payload.mappings,
                "overwrite": payload.overwrite,
                "source_ref": payload.source_ref,
                "valid": not checked["invalid"],
                "invalid": checked["invalid"],
                "conflicts": checked["conflicts"],
            }
        except (KeyError, SegmentationError) as error:
            return _error(400, "mapping_invalid", str(error))

    @app.post(f"{API_PREFIX}/imports/structured")
    async def structured_import(payload: StructuredImportInput):
        try:
            _require_list(runtime.store, payload.list_id)
            return runtime.segmentation.import_structured(
                payload.rows,
                mappings=payload.mappings,
                list_id=payload.list_id,
                source_ref=payload.source_ref,
                overwrite=payload.overwrite,
            )
        except SegmentationError as error:
            return _error(400, error.code, str(error))

    @app.get(f"{API_PREFIX}/segments")
    async def segments(include_archived: bool = False):
        return {"items": runtime.segmentation.list_segments(include_archived=include_archived)}

    @app.post(f"{API_PREFIX}/segments")
    async def create_segment(payload: SegmentInput):
        try:
            return runtime.segmentation.create_segment(
                payload.name,
                payload.definition,
                description=payload.description,
            )
        except SegmentationError as error:
            return _error(409 if error.code == "revision_conflict" else 400, error.code, str(error))

    @app.get(f"{API_PREFIX}/segments/{{segment_id}}")
    async def segment_detail(segment_id: str, revision_id: str | None = None):
        try:
            return runtime.segmentation.get_segment(segment_id, revision_id)
        except SegmentationError as error:
            return _error(404 if error.code.endswith("_not_found") else 400, error.code, str(error))

    @app.patch(f"{API_PREFIX}/segments/{{segment_id}}")
    async def edit_segment(segment_id: str, payload: SegmentInput):
        try:
            if not payload.expected_revision_id:
                return _error(400, "revision_required", "expected_revision_id is required")
            return runtime.segmentation.edit_segment(
                segment_id,
                payload.definition,
                expected_revision_id=payload.expected_revision_id,
                description=payload.description,
            )
        except SegmentationError as error:
            return _error(409 if error.code == "revision_conflict" else 400, error.code, str(error))

    @app.get(f"{API_PREFIX}/segments/{{segment_id}}/preview")
    async def segment_preview(
        segment_id: str, revision_id: str | None = None, limit: int = 100, offset: int = 0
    ):
        try:
            return runtime.segmentation.preview(
                segment_id, revision_id=revision_id, limit=limit, offset=offset
            )
        except SegmentationError as error:
            return _error(404 if error.code.endswith("_not_found") else 400, error.code, str(error))

    @app.post(f"{API_PREFIX}/segments/{{segment_id}}/archive")
    async def archive_segment(segment_id: str):
        try:
            runtime.segmentation.archive_segment(segment_id)
            return {"segment_id": segment_id, "status": "archived"}
        except SegmentationError as error:
            return _error(404 if error.code == "segment_not_found" else 400, error.code, str(error))

    @app.get(f"{API_PREFIX}/lists/{{list_id}}/history")
    async def list_history(list_id: str, limit: int = 100):
        return {
            "items": _query(
                runtime.store,
                "SELECT * FROM list_membership_history "
                "WHERE list_id = ? ORDER BY occurred_at DESC LIMIT ?",
                (list_id, max(1, min(limit, 500))),
            )
        }

    @app.post(f"{API_PREFIX}/lists/{{list_id}}/members/{{contact_id}}")
    async def add_list_member(list_id: str, contact_id: str):
        try:
            return runtime.segmentation.set_membership(list_id, contact_id, "add")
        except SegmentationError as error:
            return _error(404 if error.code.endswith("_not_found") else 400, error.code, str(error))

    @app.delete(f"{API_PREFIX}/lists/{{list_id}}/members/{{contact_id}}")
    async def remove_list_member(list_id: str, contact_id: str):
        try:
            return runtime.segmentation.set_membership(list_id, contact_id, "remove")
        except SegmentationError as error:
            return _error(404 if error.code.endswith("_not_found") else 400, error.code, str(error))

    @app.get(f"{API_PREFIX}/contacts")
    async def contacts(list_id: str | None = None, q: str = "", limit: int = 100, offset: int = 0):
        safe_limit = max(1, min(limit, 500))
        safe_offset = max(0, offset)
        if list_id:
            rows = _query(
                runtime.store,
                "SELECT c.* FROM contacts c JOIN list_memberships m ON m.contact_id = c.id "
                "WHERE m.list_id = ? AND c.normalized_email LIKE ? "
                "ORDER BY c.normalized_email LIMIT ? OFFSET ?",
                (list_id, f"%{q.strip()}%", safe_limit, safe_offset),
            )
        else:
            rows = _query(
                runtime.store,
                "SELECT * FROM contacts WHERE normalized_email LIKE ? "
                "ORDER BY normalized_email LIMIT ? OFFSET ?",
                (f"%{q.strip()}%", safe_limit, safe_offset),
            )
        items = [_row(item) if isinstance(item, sqlite3.Row) else item for item in rows]
        return {"items": items, "limit": safe_limit, "offset": safe_offset}

    @app.post(f"{API_PREFIX}/imports/preview")
    async def import_preview(file: UploadFile = File(...)):  # noqa: B008
        try:
            _, preview = _upload_preview(file)
            return preview
        except (OSError, ValueError) as error:
            return _error(400, "import_invalid", str(error))

    @app.post(f"{API_PREFIX}/imports")
    async def import_contacts(list_id: str, file: UploadFile = File(...)):  # noqa: B008
        try:
            candidates, preview = _upload_preview(file)
            _require_list(runtime.store, list_id)
            result = runtime.campaigns.import_candidates(candidates, list_id)
            return {**result, "preview": preview}
        except KeyError:
            return _error(404, "list_not_found", "target list was not found")
        except (OSError, ValueError) as error:
            return _error(400, "import_invalid", str(error))

    @app.post(f"{API_PREFIX}/list-tools/preview")
    async def list_tools_preview(file: UploadFile = File(...)):  # noqa: B008
        temporary = _upload_list_tool(file, runtime.list_processing.root)
        try:
            return runtime.list_processing.preview(temporary)
        except ListProcessingError as error:
            return _error(400, error.code, str(error))
        finally:
            temporary.unlink(missing_ok=True)

    @app.get(f"{API_PREFIX}/list-tools/jobs")
    async def list_tools_jobs(limit: int = 50, offset: int = 0):
        safe_limit = max(1, min(limit, 100))
        return {
            "items": _query(
                runtime.store,
                "SELECT id,source_filename,profile,dns_mode,status,processed_count,"
                "accepted_count,summary_json,error_message,created_at,updated_at,"
                "started_at,completed_at,correlation_id FROM list_processing_jobs "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (safe_limit, max(0, offset)),
            )
        }

    @app.post(f"{API_PREFIX}/list-tools/jobs")
    async def create_list_tools_job(
        file: UploadFile = File(...),  # noqa: B008
        profile: str = "full",
        dns_mode: str = "offline",
    ):
        temporary = _upload_list_tool(file, runtime.list_processing.root)
        try:
            job = runtime.list_processing.create_job(
                temporary, file.filename or "input.txt", profile=profile, dns_mode=dns_mode
            )
            temporary.unlink(missing_ok=True)
            return job
        except ListProcessingError as error:
            temporary.unlink(missing_ok=True)
            return _error(400, error.code, str(error))

    @app.get(f"{API_PREFIX}/list-tools/jobs/{{job_id}}")
    async def list_tools_job(job_id: str):
        try:
            return runtime.list_processing.get_job(job_id)
        except ListProcessingError as error:
            return _error(404 if error.code == "job_not_found" else 400, error.code, str(error))

    @app.post(f"{API_PREFIX}/list-tools/jobs/{{job_id}}/{{action}}")
    async def list_tools_job_action(job_id: str, action: str):
        try:
            if action == "start":
                return runtime.list_processing.start(job_id)
            if action == "resume":
                return runtime.list_processing.start(job_id, resume=True)
            if action == "pause":
                return runtime.list_processing.pause(job_id)
            if action == "cancel":
                return runtime.list_processing.cancel(job_id)
            return _error(400, "action_invalid", "unsupported List Tools job action")
        except ListProcessingError as error:
            return _error(
                404 if error.code == "job_not_found" else 409 if error.code == "job_state" else 400,
                error.code,
                str(error),
            )

    @app.get(f"{API_PREFIX}/list-tools/jobs/{{job_id}}/artifacts/{{artifact_id}}")
    async def list_tools_artifact(job_id: str, artifact_id: str):
        try:
            path, filename = runtime.list_processing.artifact_path(job_id, artifact_id)
            return FileResponse(path, filename=filename, headers={"cache-control": "no-store"})
        except ListProcessingError as error:
            return _error(
                404 if error.code == "artifact_not_found" else 403, error.code, str(error)
            )

    @app.get(f"{API_PREFIX}/data-studio/recipes")
    async def data_studio_recipes(include_archived: bool = False):
        return {"items": runtime.data_studio.list_recipes(include_archived=include_archived)}

    @app.post(f"{API_PREFIX}/data-studio/recipes")
    async def create_data_studio_recipe(payload: DataStudioRecipeInput):
        try:
            return runtime.data_studio.create_recipe(
                payload.name, payload.description, payload.definition
            )
        except DataStudioError as error:
            return _error(409 if error.code == "recipe_exists" else 400, error.code, str(error))

    @app.get(f"{API_PREFIX}/data-studio/recipes/{{recipe_id}}")
    async def data_studio_recipe(recipe_id: str, revision_id: str | None = None):
        try:
            return runtime.data_studio.get_recipe(recipe_id, revision_id)
        except DataStudioError as error:
            return _error(404 if error.code.endswith("_not_found") else 400, error.code, str(error))

    @app.post(f"{API_PREFIX}/data-studio/recipes/{{recipe_id}}/revisions")
    async def save_data_studio_revision(recipe_id: str, payload: DataStudioRevisionInput):
        try:
            return runtime.data_studio.save_revision(
                recipe_id,
                payload.definition,
                expected_revision_id=payload.expected_revision_id,
            )
        except DataStudioError as error:
            return _error(409 if error.code == "revision_conflict" else 400, error.code, str(error))

    @app.post(f"{API_PREFIX}/data-studio/validate")
    async def validate_data_studio_recipe(payload: DataStudioRevisionInput):
        try:
            definition = runtime.data_studio.validate_definition(payload.definition)
            return {"valid": True, "definition": definition}
        except DataStudioError as error:
            return _error(400, error.code, str(error))

    @app.post(f"{API_PREFIX}/data-studio/schema")
    async def inspect_data_studio_schema(file: UploadFile = File(...)):  # noqa: B008
        temporary = _upload_data_studio(file, runtime.data_studio.root)
        try:
            return detect_schema(temporary)
        except (DataStudioError, OSError, UnicodeError) as error:
            return _error(400, getattr(error, "code", "source_invalid"), str(error))
        finally:
            temporary.unlink(missing_ok=True)

    @app.post(f"{API_PREFIX}/data-studio/preview")
    async def preview_data_studio(file: UploadFile = File(...), definition: str = Form("{}")):  # noqa: B008
        temporary = _upload_data_studio(file, runtime.data_studio.root)
        try:
            parsed = json.loads(definition)
            return runtime.data_studio.preview(temporary, parsed)
        except (DataStudioError, OSError, UnicodeError, json.JSONDecodeError) as error:
            return _error(400, getattr(error, "code", "preview_invalid"), str(error))
        finally:
            temporary.unlink(missing_ok=True)

    @app.post(f"{API_PREFIX}/data-studio/preview-multi")
    async def preview_data_studio_multi(
        files: list[UploadFile] = File(...),  # noqa: B008
        definition: str = Form("{}"),
    ):
        temporary: list[Path] = []
        try:
            parsed = json.loads(definition)
            sources = []
            for index, file in enumerate(files, start=1):
                path = _upload_data_studio(file, runtime.data_studio.root)
                temporary.append(path)
                sources.append((path, file.filename or f"source-{index}.csv", f"source-{index}"))
            return runtime.data_studio.preview_sources(sources, parsed)
        except (DataStudioError, OSError, UnicodeError, json.JSONDecodeError) as error:
            return _error(400, getattr(error, "code", "preview_invalid"), str(error))
        finally:
            for path in temporary:
                path.unlink(missing_ok=True)

    @app.get(f"{API_PREFIX}/data-studio/jobs")
    async def data_studio_jobs(limit: int = 50, offset: int = 0):
        safe_limit = max(1, min(limit, 100))
        return {
            "items": _query(
                runtime.store,
                "SELECT id,recipe_revision_id,source_filename,status,processed_count,"
                "output_count,rejected_count,error_count,summary_json,error_message,"
                "created_at,updated_at,started_at,completed_at,correlation_id "
                "FROM data_studio_jobs ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (safe_limit, max(0, offset)),
            )
        }

    @app.post(f"{API_PREFIX}/data-studio/jobs")
    async def create_data_studio_job(
        recipe_revision_id: str,
        file: UploadFile = File(...),  # noqa: B008
    ):
        temporary = _upload_data_studio(file, runtime.data_studio.root)
        try:
            job = runtime.data_studio.create_job(
                temporary, file.filename or "data.csv", recipe_revision_id
            )
            temporary.unlink(missing_ok=True)
            return job
        except DataStudioError as error:
            temporary.unlink(missing_ok=True)
            return _error(
                404 if error.code == "revision_not_found" else 400, error.code, str(error)
            )

    @app.post(f"{API_PREFIX}/data-studio/jobs-multi")
    async def create_data_studio_multi_source_job(
        recipe_revision_id: str,
        files: list[UploadFile] = File(...),  # noqa: B008
    ):
        temporary: list[Path] = []
        try:
            sources = []
            for index, file in enumerate(files, start=1):
                path = _upload_data_studio(file, runtime.data_studio.root)
                temporary.append(path)
                sources.append((path, file.filename or f"source-{index}.csv", f"source-{index}"))
            return runtime.data_studio.create_multi_source_job(sources, recipe_revision_id)
        except DataStudioError as error:
            return _error(
                404 if error.code == "revision_not_found" else 400, error.code, str(error)
            )
        finally:
            for path in temporary:
                path.unlink(missing_ok=True)

    @app.get(f"{API_PREFIX}/data-studio/jobs/{{job_id}}")
    async def data_studio_job(job_id: str):
        try:
            return runtime.data_studio.get_job(job_id)
        except DataStudioError as error:
            return _error(404, error.code, str(error))

    @app.post(f"{API_PREFIX}/data-studio/jobs/{{job_id}}/{{action}}")
    async def data_studio_job_action(job_id: str, action: str):
        try:
            if action == "start":
                return runtime.data_studio.start(job_id)
            if action == "cancel":
                return runtime.data_studio.cancel(job_id)
            return _error(400, "action_invalid", "unsupported Data Studio job action")
        except DataStudioError as error:
            return _error(
                404 if error.code == "job_not_found" else 409 if error.code == "job_state" else 400,
                error.code,
                str(error),
            )

    @app.get(f"{API_PREFIX}/data-studio/jobs/{{job_id}}/artifacts/{{artifact_id}}")
    async def data_studio_artifact(job_id: str, artifact_id: str):
        try:
            path, filename = runtime.data_studio.artifact_path(job_id, artifact_id)
            return FileResponse(path, filename=filename, headers={"cache-control": "no-store"})
        except DataStudioError as error:
            return _error(404, error.code, str(error))

    @app.post(
        f"{API_PREFIX}/data-studio/jobs/{{job_id}}/artifacts/{{artifact_id}}/contacts/preview"
    )
    async def preview_data_studio_contact_import(
        job_id: str, artifact_id: str, payload: DataStudioContactImportInput
    ):
        try:
            rows = list(
                runtime.data_studio.iter_artifact_records(
                    job_id, artifact_id, limit=payload.max_rows + 1
                )
            )
            if len(rows) > payload.max_rows:
                raise DataStudioError(
                    "artifact has more rows than max_rows; raise the explicit import limit",
                    "contact_import_limit",
                )
            checked = runtime.segmentation.validate_import_rows(
                rows, mappings=payload.mappings, overwrite=payload.overwrite
            )
            return {
                "records": checked["records"],
                "mappings": payload.mappings,
                "overwrite": payload.overwrite,
                "source_ref": payload.source_ref,
                "valid": not checked["invalid"],
                "invalid": checked["invalid"],
                "conflicts": checked["conflicts"],
            }
        except (DataStudioError, SegmentationError) as error:
            return _error(400, getattr(error, "code", "contact_import_invalid"), str(error))

    @app.post(f"{API_PREFIX}/data-studio/jobs/{{job_id}}/artifacts/{{artifact_id}}/contacts")
    async def import_data_studio_contacts(
        job_id: str, artifact_id: str, payload: DataStudioContactImportInput
    ):
        try:
            rows = list(
                runtime.data_studio.iter_artifact_records(
                    job_id, artifact_id, limit=payload.max_rows + 1
                )
            )
            if len(rows) > payload.max_rows:
                raise DataStudioError(
                    "artifact has more rows than max_rows; raise the explicit import limit",
                    "contact_import_limit",
                )
            result = runtime.segmentation.import_structured(
                rows,
                mappings=payload.mappings,
                list_id=payload.list_id,
                source_ref=payload.source_ref,
                overwrite=payload.overwrite,
            )
            consumer = runtime.data_studio.record_consumer(
                kind="contact_import",
                job_id=job_id,
                artifact_id=artifact_id,
                reference_id=payload.list_id,
                configuration=payload.model_dump(),
            )
            return {**result, "consumer": consumer}
        except (DataStudioError, SegmentationError) as error:
            return _error(400, getattr(error, "code", "contact_import_invalid"), str(error))

    @app.post(f"{API_PREFIX}/data-studio/jobs/{{job_id}}/artifacts/{{artifact_id}}/message-preview")
    async def preview_data_studio_message(
        job_id: str, artifact_id: str, payload: DataStudioMessagePreviewInput
    ):
        try:
            rows = list(
                runtime.data_studio.iter_artifact_records(
                    job_id, artifact_id, limit=payload.row_number
                )
            )
            if len(rows) < payload.row_number:
                return _error(404, "row_not_found", "the requested artifact row was not found")
            row = rows[payload.row_number - 1]
            with runtime.store.connect() as connection:
                revision = connection.execute(
                    "SELECT document_json FROM message_revisions WHERE id=? AND status='published'",
                    (payload.revision_id,),
                ).fetchone()
            if revision is None:
                return _error(
                    400, "revision_not_published", "a published message revision is required"
                )
            metadata: dict[str, Any] = {}
            for source, target in payload.field_mapping.items():
                if source not in row:
                    continue
                key = target.removeprefix("contact.")
                metadata[key] = row[source]
            if payload.contact_id:
                contact = dict(runtime.store.get_contact(payload.contact_id))
                existing = json.loads(contact.get("metadata_json") or "{}")
                contact["metadata"] = {**existing, **metadata}
            else:
                email_field = next(
                    (
                        source
                        for source, target in payload.field_mapping.items()
                        if target == "email"
                    ),
                    "email",
                )
                name_field = next(
                    (
                        source
                        for source, target in payload.field_mapping.items()
                        if target == "name"
                    ),
                    "display_name",
                )
                contact = {
                    "normalized_email": normalize_email(
                        str(row.get(email_field, "sample@example.com"))
                    ),
                    "display_name": str(row.get(name_field, "")),
                    "metadata": metadata,
                }
            document = json.loads(revision["document_json"])
            rendered = runtime.message_studio.render_document(document, contact=contact)
            job = runtime.data_studio.get_job(job_id)
            artifact = next(item for item in job["artifacts"] if item["id"] == artifact_id)
            consumer = runtime.data_studio.record_consumer(
                kind="message_preview",
                job_id=job_id,
                artifact_id=artifact_id,
                reference_id=payload.revision_id,
                configuration={
                    **payload.model_dump(),
                    "row_hash": hashlib.sha256(
                        json.dumps(
                            row, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                        ).encode()
                    ).hexdigest(),
                    "artifact_sha256": artifact["sha256"],
                },
            )
            rendered.pop("_rendered", None)
            rendered.pop("_attachments", None)
            return {
                **rendered,
                "consumer": consumer,
                "snapshot": {
                    "artifact_sha256": artifact["sha256"],
                    "revision_id": payload.revision_id,
                },
            }
        except (DataStudioError, MessageStudioError, KeyError, ValueError) as error:
            return _error(400, getattr(error, "code", "message_preview_invalid"), str(error))

    @app.post(f"{API_PREFIX}/data-studio/extensions/{{name}}/register")
    async def register_data_studio_extension(name: str):
        try:
            return runtime.data_studio.register_extension(name)
        except DataStudioError as error:
            return _error(400, error.code, str(error))

    @app.post(f"{API_PREFIX}/data-studio/extensions/{{name}}/{{action}}")
    async def data_studio_extension_action(name: str, action: str):
        try:
            if action not in {"enable", "disable"}:
                return _error(400, "action_invalid", "unsupported extension action")
            return runtime.data_studio.set_extension_enabled(name, action == "enable")
        except DataStudioError as error:
            return _error(
                404 if error.code == "extension_not_found" else 400, error.code, str(error)
            )

    @app.get(f"{API_PREFIX}/functions")
    async def programmable_functions():
        return {"items": runtime.programmable.list_functions()}

    @app.post(f"{API_PREFIX}/functions")
    async def create_programmable_function(payload: ProgrammableFunctionInput):
        try:
            return runtime.programmable.create_function(
                payload.name, payload.definition, version=payload.version
            )
        except Exception as error:
            return _error(400, getattr(error, "code", "function_invalid"), str(error))

    @app.post(f"{API_PREFIX}/functions/{{name}}/preview")
    async def preview_programmable_function(name: str, payload: ProgrammablePreviewInput):
        try:
            result = runtime.programmable.execute(
                name,
                payload.records,
                revision_id=payload.revision_id,
                parameters=payload.parameters,
                context="data_studio",
                correlation_id=f"preview:{uuid.uuid4()}",
            )
            result.pop("execution_id", None)
            return result
        except Exception as error:
            return _error(400, getattr(error, "code", "function_invalid"), str(error))

    @app.get(f"{API_PREFIX}/functions/{{name}}/revisions")
    async def programmable_function_revisions(name: str):
        try:
            return {"items": runtime.programmable.list_revisions(name)}
        except Exception as error:
            code = getattr(error, "code", "function_invalid")
            return _error(404 if code == "function_not_found" else 400, code, str(error))

    @app.post(f"{API_PREFIX}/functions/{{name}}/revisions")
    async def create_programmable_revision(name: str, payload: ProgrammableRevisionInput):
        try:
            return runtime.programmable.create_revision(
                name, payload.definition, version=payload.version
            )
        except Exception as error:
            code = getattr(error, "code", "function_invalid")
            return _error(404 if code == "function_not_found" else 400, code, str(error))

    @app.get(f"{API_PREFIX}/functions/{{name}}/history")
    async def programmable_function_history(name: str, limit: int = 100):
        try:
            return {"items": runtime.programmable.list_executions(name, limit=limit)}
        except Exception as error:
            code = getattr(error, "code", "function_invalid")
            return _error(404 if code == "function_not_found" else 400, code, str(error))

    @app.post(f"{API_PREFIX}/functions/{{name}}/{{action}}")
    async def programmable_function_action(name: str, action: str):
        if action not in {"enable", "disable"}:
            return _error(400, "action_invalid", "unsupported function action")
        try:
            return runtime.programmable.set_enabled(name, action == "enable")
        except Exception as error:
            code = getattr(error, "code", "function_invalid")
            return _error(404 if code == "function_not_found" else 400, code, str(error))

    @app.get(f"{API_PREFIX}/email-standards")
    async def email_standards():
        return {
            "identities": runtime.email_standards.list_sender_identities(),
            "guidance": runtime.email_standards.provider_guidance(),
            "history": runtime.email_standards.history(limit=25),
            "aggregate": runtime.email_standards.report_summary(),
        }

    @app.post(f"{API_PREFIX}/email-standards/identities")
    async def register_email_sender_identity(payload: SenderIdentityInput):
        try:
            return runtime.email_standards.register_sender_identity(**payload.model_dump())
        except EmailStandardsError as error:
            return _error(400, error.code, str(error))

    @app.get(f"{API_PREFIX}/email-standards/identities/{{identity_ref}}")
    async def email_sender_identity(identity_ref: str):
        try:
            return runtime.email_standards.get_sender_identity(identity_ref)
        except EmailStandardsError as error:
            return _error(404, error.code, str(error))

    @app.post(f"{API_PREFIX}/email-standards/domains/diagnose")
    async def diagnose_email_domain(payload: DomainDiagnosticInput):
        try:
            return runtime.email_standards.inspect_domain(**payload.model_dump())
        except EmailStandardsError as error:
            return _error(400, error.code, str(error))

    @app.post(f"{API_PREFIX}/email-standards/messages/validate")
    async def validate_email_message(payload: MessageDiagnosticInput):
        try:
            return runtime.email_standards.validate_message(
                payload.message.encode("utf-8"),
                communication_purpose=payload.communication_purpose,
                expected_identity=payload.expected_identity,
            )
        except EmailStandardsError as error:
            return _error(400, error.code, str(error))

    @app.get(f"{API_PREFIX}/email-standards/history")
    async def email_standards_history(kind: str | None = None, limit: int = 100):
        return {"items": runtime.email_standards.history(kind=kind, limit=limit)}

    @app.get(f"{API_PREFIX}/email-standards/guidance")
    async def email_standards_guidance():
        return {"items": runtime.email_standards.provider_guidance()}

    @app.post(f"{API_PREFIX}/email-standards/unsubscribe/endpoints")
    async def configure_email_unsubscribe(payload: UnsubscribeEndpointInput):
        try:
            return runtime.email_standards.configure_unsubscribe_endpoint(**payload.model_dump())
        except EmailStandardsError as error:
            return _error(400, error.code, str(error))

    @app.post(f"{API_PREFIX}/email-standards/unsubscribe/endpoints/{{endpoint_ref}}/tokens")
    async def create_email_unsubscribe_token(endpoint_ref: str, payload: UnsubscribeTokenInput):
        try:
            return runtime.email_standards.create_unsubscribe_token(
                endpoint_ref, **payload.model_dump()
            )
        except EmailStandardsError as error:
            return _error(400, error.code, str(error))

    @app.post(f"{API_PREFIX}/email-standards/unsubscribe/readiness")
    async def email_unsubscribe_readiness(payload: UnsubscribeReadinessInput):
        try:
            return runtime.email_standards.unsubscribe_readiness(
                communication_purpose=payload.communication_purpose,
                endpoint_ref=payload.endpoint_ref,
                message=payload.message.encode("utf-8") if payload.message is not None else None,
            )
        except EmailStandardsError as error:
            return _error(400, error.code, str(error))

    @app.post(f"{API_PREFIX}/email-standards/dmarc/import")
    async def import_email_dmarc_report(file: UploadFile = File(...)):  # noqa: B008
        try:
            raw = await file.read(MAX_REPORT_BYTES + 1)
            if len(raw) > MAX_REPORT_BYTES:
                return _error(400, "report_limit", "DMARC aggregate report exceeds the 5 MiB limit")
            return runtime.email_standards.import_dmarc_report(raw, filename=file.filename)
        except (EmailStandardsError, OSError) as error:
            return _error(400, getattr(error, "code", "report_invalid"), str(error))

    @app.get(f"{API_PREFIX}/email-standards/dmarc/summary")
    async def email_dmarc_summary():
        return runtime.email_standards.report_summary()

    @app.post(f"{API_PREFIX}/unsubscribe/{{endpoint_ref}}/{{token}}")
    async def process_email_unsubscribe(request: Request, endpoint_ref: str, token: str):
        try:
            raw_body = await request.body()
            if len(raw_body) > 100:
                return _error(400, "request_invalid", "unsubscribe request body is too large")
            body = raw_body.decode("ascii")
            return runtime.email_standards.process_unsubscribe(
                endpoint_ref, token, method="POST", body=body
            )
        except (EmailStandardsError, UnicodeDecodeError) as error:
            if isinstance(error, UnicodeDecodeError):
                return _error(400, "request_invalid", "unsubscribe request body is not ASCII")
            return _error(
                400 if error.code != "method_not_allowed" else 405, error.code, str(error)
            )

    @app.get(f"{API_PREFIX}/campaigns")
    async def campaigns(limit: int = 100, offset: int = 0):
        safe_limit = max(1, min(limit, 500))
        safe_offset = max(0, offset)
        rows = _query(
            runtime.store,
            "SELECT * FROM campaigns ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (safe_limit, safe_offset),
        )
        return {
            "items": [
                _campaign_view(runtime, runtime.store.get_campaign(item["id"])) for item in rows
            ],
            "limit": safe_limit,
            "offset": safe_offset,
        }

    @app.post(f"{API_PREFIX}/campaigns")
    async def create_campaign(payload: CampaignInput):
        try:
            campaign_id = runtime.campaign_operations.create_draft(**payload.model_dump())
        except CampaignOperationsError as error:
            return _error(404 if error.code.endswith("_not_found") else 400, error.code, str(error))
        return {"id": campaign_id}

    @app.patch(f"{API_PREFIX}/campaigns/{{campaign_id}}")
    async def update_campaign(campaign_id: str, payload: CampaignUpdateInput):
        try:
            values = payload.model_dump(exclude={"expected_updated_at"})
            runtime.campaign_operations.update_draft(
                campaign_id, expected_updated_at=payload.expected_updated_at, **values
            )
            return _campaign_view(runtime, runtime.store.get_campaign(campaign_id))
        except CampaignOperationsError as error:
            return _error(400, error.code, str(error))
        except (KeyError, ValueError) as error:
            return _error(
                409 if "changed" in str(error) else 400, "campaign_update_failed", str(error)
            )

    @app.get(f"{API_PREFIX}/campaigns/{{campaign_id}}")
    async def campaign_detail(campaign_id: str):
        try:
            return _campaign_view(runtime, runtime.store.get_campaign(campaign_id))
        except KeyError:
            return _error(404, "campaign_not_found", "campaign was not found")

    @app.get(f"{API_PREFIX}/campaigns/{{campaign_id}}/workspace")
    async def campaign_workspace(campaign_id: str, preview_limit: int = 10):
        try:
            return runtime.campaign_operations.workspace(campaign_id, preview_limit=preview_limit)
        except KeyError:
            return _error(404, "campaign_not_found", "campaign was not found")

    @app.post(f"{API_PREFIX}/campaigns/{{campaign_id}}/preflight")
    async def campaign_preflight(campaign_id: str):
        try:
            return runtime.campaign_operations.preflight(campaign_id)
        except KeyError:
            return _error(404, "campaign_not_found", "campaign was not found")

    @app.put(f"{API_PREFIX}/campaigns/{{campaign_id}}/schedule")
    async def campaign_schedule(campaign_id: str, payload: CampaignScheduleInput):
        try:
            schedule = ScheduleSpec(
                mode=payload.mode,
                timezone=payload.timezone,
                scheduled_at=payload.scheduled_at,
                window_start=payload.window_start,
                window_end=payload.window_end,
                weekdays=tuple(payload.weekdays),
                pacing_seconds=payload.pacing_seconds,
                cooldown_seconds=payload.cooldown_seconds,
                recurrence=payload.recurrence,
                catch_up_policy=payload.catch_up_policy,
                fold=payload.fold,
            )
            return runtime.campaign_operations.set_schedule(campaign_id, schedule)
        except CampaignOperationsError as error:
            return _error(400, error.code, str(error))
        except KeyError:
            return _error(404, "campaign_not_found", "campaign was not found")

    @app.post(f"{API_PREFIX}/campaigns/{{campaign_id}}/prepare")
    async def prepare_campaign(campaign_id: str, payload: PrepareInput | None = None):
        try:
            if payload is None or (
                payload.sender_email is None
                and payload.subject is None
                and payload.text is None
                and payload.run_key == "primary"
            ):
                return runtime.campaign_operations.prepare(
                    campaign_id, run_key=payload.run_key if payload else "primary"
                )
            campaign = runtime.store.get_campaign(campaign_id)
            preflight = runtime.campaign_operations.preflight(campaign_id)
            if not preflight["ready"]:
                raise CampaignOperationsError(
                    "campaign preflight has blockers", "preflight_blocked"
                )
            configuration = json.loads(campaign["configuration_json"])
            preparation_options = runtime.campaign_operations.preparation_options(campaign)
            sender_email = (payload.sender_email if payload else None) or configuration.get(
                "sender_email", "sender@example.com"
            )
            subject = (payload.subject if payload else None) or configuration.get(
                "subject", "Synthetic Mailforge campaign"
            )
            text = (payload.text if payload else None) or configuration.get(
                "text", "Hello {{ email }}"
            )
            sender = SenderPolicy(
                ref=campaign["sender_config_ref"], provider="synthetic", email=sender_email
            )
            run_key = payload.run_key if payload else "primary"
            if campaign["message_revision_id"]:
                if run_key != "primary":
                    return _error(
                        400,
                        "run_key_unsupported",
                        "Message Studio campaigns use the primary audience run",
                    )
                report = runtime.message_studio.prepare_campaign(
                    campaign_id,
                    sender=sender,
                    revision_id=campaign["message_revision_id"],
                    run_key=run_key,
                    **preparation_options,
                )
                return asdict(report)
            template = TemplateDefinition(
                name=campaign["template_ref"], version="1", subject=subject, text=text
            )
            if run_key != "primary":
                report = runtime.campaigns.prepare(campaign_id, run_key=run_key)
                return {
                    **report.to_dict(),
                    "run_key": run_key,
                    "audience": runtime.segmentation.get_audience(campaign_id, run_key),
                }
            report = runtime.outbound.prepare_campaign(
                campaign_id,
                template=template,
                sender=sender,
                **preparation_options,
            )
            return asdict(report)
        except (KeyError, ValueError, CampaignOperationsError) as error:
            return _error(400, "campaign_not_ready", str(error))

    @app.post(f"{API_PREFIX}/campaigns/{{campaign_id}}/prepare-data-studio")
    async def prepare_data_studio_campaign(
        campaign_id: str, payload: DataStudioCampaignPrepareInput
    ):
        try:
            campaign = runtime.store.get_campaign(campaign_id)
            preflight = runtime.campaign_operations.preflight(campaign_id)
            if not preflight["ready"]:
                raise CampaignOperationsError(
                    "campaign preflight has blockers", "preflight_blocked"
                )
            configuration = json.loads(campaign["configuration_json"] or "{}")
            preparation_options = runtime.campaign_operations.preparation_options(campaign)
            sender = SenderPolicy(
                ref=campaign["sender_config_ref"],
                provider="synthetic",
                email=configuration.get("sender_email", "sender@example.com"),
            )
            snapshot = runtime.message_studio.create_data_studio_snapshot(
                campaign_id,
                source_job_id=payload.source_job_id,
                artifact_id=payload.source_artifact_id,
                recipe_revision_id=payload.recipe_revision_id,
                revision_id=payload.message_revision_id,
                field_mapping=payload.field_mapping,
                run_key=payload.run_key,
            )
            report = runtime.message_studio.prepare_campaign(
                campaign_id,
                sender=sender,
                revision_id=payload.message_revision_id,
                run_key=payload.run_key,
                data_snapshot_id=snapshot["id"],
                **preparation_options,
            )
            return {**asdict(report), "data_snapshot": snapshot}
        except (
            CampaignOperationsError,
            DataStudioError,
            KeyError,
            ValueError,
            MessageStudioError,
        ) as error:
            return _error(400, getattr(error, "code", "campaign_not_ready"), str(error))

    @app.post(f"{API_PREFIX}/campaigns/{{campaign_id}}/authorize")
    async def authorize_campaign(campaign_id: str):
        try:
            return runtime.campaign_operations.authorize(campaign_id)
        except (KeyError, ValueError, PermissionError, CampaignOperationsError) as error:
            return _error(400, "authorization_failed", str(error))

    @app.post(f"{API_PREFIX}/campaigns/{{campaign_id}}/simulate")
    async def simulate_campaign(campaign_id: str):
        try:
            campaign = runtime.store.get_campaign(campaign_id)
            if campaign[
                "authorization"
            ] != "simulation_authorized" or not runtime.store.has_execution_declaration(
                campaign_id
            ):
                return _error(
                    400,
                    "simulation_not_authorized",
                    "authorize the prepared campaign before starting the worker",
                )
            if campaign["status"] in {"completed", "canceled"}:
                return {"campaign_id": campaign_id, "started": False, "mode": "synthetic"}
            started = runtime.workers.start(campaign_id)
            return {"campaign_id": campaign_id, "started": started, "mode": "synthetic"}
        except (KeyError, ValueError, PermissionError) as error:
            return _error(400, "simulation_failed", str(error))

    @app.post(f"{API_PREFIX}/campaigns/{{campaign_id}}/pause")
    async def pause_campaign(campaign_id: str):
        try:
            return runtime.campaign_operations.control(campaign_id, "pause")
        except (KeyError, ValueError, CampaignOperationsError) as error:
            return _error(400, "pause_failed", str(error))

    @app.post(f"{API_PREFIX}/campaigns/{{campaign_id}}/cancel")
    async def cancel_campaign(campaign_id: str):
        try:
            return runtime.campaign_operations.control(campaign_id, "cancel")
        except (KeyError, ValueError, CampaignOperationsError) as error:
            return _error(400, "cancel_failed", str(error))

    @app.get(f"{API_PREFIX}/campaigns/{{campaign_id}}/progress")
    async def campaign_progress(campaign_id: str):
        try:
            return {
                **runtime.campaigns.report(campaign_id).to_dict(),
                "worker_active": runtime.workers.active(campaign_id),
            }
        except KeyError:
            return _error(404, "campaign_not_found", "campaign was not found")

    @app.get(f"{API_PREFIX}/campaigns/{{campaign_id}}/preview")
    async def campaign_preview(campaign_id: str):
        try:
            preview = runtime.campaigns.preview(campaign_id)
            messages = []
            for intent in preview["intents"][:10]:
                if intent["prepared_state"] != "prepared":
                    continue
                try:
                    messages.append(runtime.outbound.preview_message(campaign_id, intent["email"]))
                except KeyError:
                    continue
            return {**preview, "messages": messages}
        except KeyError:
            return _error(404, "campaign_not_found", "campaign was not found")

    @app.get(f"{API_PREFIX}/campaigns/{{campaign_id}}/eligibility")
    async def campaign_eligibility(campaign_id: str):
        try:
            return {"items": runtime.campaigns.inspect_eligibility(campaign_id)}
        except KeyError:
            return _error(404, "campaign_not_found", "campaign was not found")

    @app.get(f"{API_PREFIX}/campaigns/{{campaign_id}}/outcomes")
    async def campaign_outcomes(campaign_id: str):
        try:
            return {
                "campaign_id": campaign_id,
                "report": runtime.campaigns.report(campaign_id).to_dict(),
                "intents": [_row(item) for item in runtime.store.list_intents(campaign_id)],
            }
        except KeyError:
            return _error(404, "campaign_not_found", "campaign was not found")

    @app.get(f"{API_PREFIX}/campaigns/{{campaign_id}}/audience")
    async def campaign_audience(campaign_id: str, run_key: str = "primary"):
        try:
            row = runtime.store.get_campaign(campaign_id)
            audience = runtime.segmentation.get_audience(campaign_id, run_key)
            return {
                "campaign_id": campaign_id,
                "source_type": row["audience_source_type"],
                "source_id": row["audience_source_id"],
                "audience": audience,
                "items": runtime.segmentation.audience_recipients(campaign_id, run_key),
            }
        except KeyError:
            return _error(404, "campaign_not_found", "campaign was not found")

    @app.get(f"{API_PREFIX}/campaigns/{{campaign_id}}/recipients")
    async def campaign_recipients(campaign_id: str, limit: int = 100, offset: int = 0):
        try:
            safe_limit = max(1, min(limit, 500))
            safe_offset = max(0, offset)
            return {
                "items": runtime.campaign_operations.recipients(
                    campaign_id, limit=safe_limit, offset=safe_offset
                ),
                "limit": safe_limit,
                "offset": safe_offset,
            }
        except KeyError:
            return _error(404, "campaign_not_found", "campaign was not found")

    @app.get(f"{API_PREFIX}/campaigns/{{campaign_id}}/attempts")
    async def campaign_attempts(campaign_id: str, limit: int = 100, offset: int = 0):
        try:
            safe_limit = max(1, min(limit, 500))
            safe_offset = max(0, offset)
            return {
                "items": runtime.campaign_operations.attempts(
                    campaign_id, limit=safe_limit, offset=safe_offset
                ),
                "limit": safe_limit,
                "offset": safe_offset,
            }
        except KeyError:
            return _error(404, "campaign_not_found", "campaign was not found")

    @app.get(f"{API_PREFIX}/campaigns/{{campaign_id}}/export")
    async def campaign_export(campaign_id: str, format: str = "json"):
        try:
            body, media_type, filename = runtime.campaign_operations.export(campaign_id, fmt=format)
            return Response(
                body,
                media_type=media_type,
                headers={"content-disposition": f'attachment; filename="{filename}"'},
            )
        except CampaignOperationsError as error:
            return _error(400, error.code, str(error))
        except KeyError, ReportingError:
            return _error(404, "campaign_not_found", "campaign was not found")

    @app.get(f"{API_PREFIX}/automations")
    async def automations(limit: int = 100, offset: int = 0):
        return runtime.automation.list_workflows(limit=limit, offset=offset)

    @app.post(f"{API_PREFIX}/automations")
    async def create_automation(payload: AutomationInput):
        try:
            return runtime.automation.create_workflow(payload.name, payload.description)
        except AutomationError as error:
            return _error(409 if error.code == "workflow_exists" else 400, error.code, str(error))

    @app.get(f"{API_PREFIX}/automations/runs/{{run_id}}")
    async def automation_run_detail(run_id: str):
        try:
            return runtime.automation.run_detail(run_id)
        except AutomationError as error:
            return _error(404, error.code, str(error))

    @app.post(f"{API_PREFIX}/automations/runs/{{run_id}}/{{action}}")
    async def automation_run_control(run_id: str, action: str):
        try:
            result = runtime.automation.control_run(run_id, action)
            if action == "resume":
                runtime.automation_workers.start(run_id)
            return {"run": result, "action": action}
        except AutomationError as error:
            return _error(
                409 if error.code == "run_state" else 404 if error.code == "run_not_found" else 400,
                error.code,
                str(error),
            )

    @app.post(f"{API_PREFIX}/automations/runs/{{run_id}}/advance")
    async def advance_automation_run(run_id: str):
        try:
            return runtime.automation.advance(run_id)
        except AutomationError as error:
            return _error(
                409 if error.code == "run_state" else 404 if error.code == "run_not_found" else 400,
                error.code,
                str(error),
            )

    @app.get(f"{API_PREFIX}/automations/{{automation_id}}")
    async def automation_detail(automation_id: str):
        try:
            return runtime.automation.get_workflow(automation_id)
        except AutomationError as error:
            return _error(404, error.code, str(error))

    @app.get(f"{API_PREFIX}/automations/{{automation_id}}/revisions")
    async def automation_revisions(automation_id: str):
        try:
            return {"items": runtime.automation.list_revisions(automation_id)}
        except AutomationError as error:
            return _error(404, error.code, str(error))

    @app.post(f"{API_PREFIX}/automations/{{automation_id}}/revisions")
    async def save_automation_revision(automation_id: str, payload: AutomationRevisionInput):
        try:
            if payload.graph is None:
                return _error(400, "graph_required", "graph is required when saving a draft")
            return runtime.automation.save_revision(
                automation_id, payload.graph, revision_id=payload.revision_id
            )
        except AutomationError as error:
            return _error(
                409 if error.code in {"revision_state", "workflow_exists"} else 400,
                error.code,
                str(error),
            )

    @app.post(f"{API_PREFIX}/automations/{{automation_id}}/validate")
    async def validate_automation(
        automation_id: str, payload: AutomationRevisionInput | None = None
    ):
        try:
            graph = (
                payload.graph
                if payload and payload.graph is not None
                else runtime.automation.get_workflow(automation_id)["draft"]["graph"]
            )
            return runtime.automation.validate_graph(graph)
        except AutomationError as error:
            return _error(400, error.code, str(error))

    @app.post(f"{API_PREFIX}/automations/{{automation_id}}/enroll")
    async def enroll_automation(automation_id: str, payload: AutomationEnrollInput):
        try:
            result = runtime.automation.enroll(
                automation_id, **payload.model_dump(exclude_none=True)
            )
            if result.get("run_id"):
                runtime.automation_workers.start(result["run_id"])
            return result
        except AutomationError as error:
            return _error(
                409 if error.code in {"workflow_state", "trigger_not_configured"} else 400,
                error.code,
                str(error),
            )

    @app.post(f"{API_PREFIX}/automations/{{automation_id}}/{{action}}")
    async def automation_lifecycle(
        automation_id: str, action: str, payload: AutomationRevisionInput | None = None
    ):
        try:
            if action == "publish":
                return runtime.automation.publish(
                    automation_id, payload.revision_id if payload else None
                )
            if action in {"activate", "pause", "archive"}:
                return runtime.automation.set_lifecycle(
                    automation_id, action, payload.revision_id if payload else None
                )
            raise AutomationError("unsupported workflow lifecycle action")
        except AutomationError as error:
            return _error(
                409
                if error.code in {"revision_state", "workflow_state", "trigger_not_configured"}
                else 400,
                error.code,
                str(error),
            )

    @app.get(f"{API_PREFIX}/automations/{{automation_id}}/runs")
    async def automation_runs(automation_id: str, limit: int = 100, offset: int = 0):
        try:
            return runtime.automation.list_runs(automation_id, limit=limit, offset=offset)
        except AutomationError as error:
            return _error(404, error.code, str(error))

    @app.post(f"{API_PREFIX}/campaigns/{{campaign_id}}/resume")
    async def resume_campaign(campaign_id: str):
        try:
            return runtime.campaign_operations.control(campaign_id, "resume")
        except (KeyError, ValueError, PermissionError, CampaignOperationsError) as error:
            return _error(400, "resume_failed", str(error))

    @app.get(f"{API_PREFIX}/inbound")
    async def inbound(
        q: str | None = None,
        sender: str | None = None,
        recipient: str | None = None,
        subject: str | None = None,
        classification: str | None = None,
        source_ref: str | None = None,
        processed: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ):
        reviews = runtime.inbound.review_queue()
        workbench = runtime.inbound_operations.search(
            q=q,
            sender=sender,
            recipient=recipient,
            subject=subject,
            classification=classification,
            source_ref=source_ref,
            processed=processed,
            limit=limit,
            offset=offset,
        )
        return {
            "items": reviews,
            "messages": workbench["items"],
            "total": workbench["total"],
            "stats": runtime.inbound.stats(),
        }

    @app.get(f"{API_PREFIX}/inbound/messages/{{message_id}}")
    async def inbound_message_detail(message_id: str):
        try:
            return runtime.inbound_operations.detail(message_id)
        except (KeyError, InboundOperationsError) as error:
            return _error(404, getattr(error, "code", "message_not_found"), str(error))

    @app.post(f"{API_PREFIX}/inbound/messages/{{message_id}}/extract")
    async def inbound_extract(message_id: str, payload: InboundExtractInput):
        try:
            return {"items": runtime.inbound_operations.extract_message(message_id, payload.fields)}
        except (KeyError, InboundOperationsError) as error:
            return _error(400, getattr(error, "code", "extract_failed"), str(error))

    @app.get(f"{API_PREFIX}/inbound/export")
    async def inbound_export(message_id: str | None = None, fmt: str = "json"):
        try:
            media_type, content = runtime.inbound_operations.export(message_id=message_id, fmt=fmt)
            return Response(content, media_type=media_type)
        except InboundOperationsError as error:
            return _error(400, error.code, str(error))

    @app.get(f"{API_PREFIX}/inbound/rules")
    async def inbound_rules():
        return {"items": runtime.inbound_operations.list_rules()}

    @app.post(f"{API_PREFIX}/inbound/rules")
    async def create_inbound_rule(payload: InboundRuleInput):
        try:
            return runtime.inbound_operations.create_rule(
                payload.name, payload.definition, priority=payload.priority
            )
        except InboundOperationsError as error:
            return _error(400, error.code, str(error))

    @app.post(f"{API_PREFIX}/inbound/rules/{{rule_id}}/status")
    async def set_inbound_rule_status(rule_id: str, payload: InboundRuleStatusInput):
        try:
            return runtime.inbound_operations.set_rule_status(rule_id, payload.status)
        except InboundOperationsError as error:
            return _error(400, error.code, str(error))

    @app.post(f"{API_PREFIX}/inbound/rules/{{rule_id}}/preview")
    async def preview_inbound_rule(rule_id: str, message_id: str):
        try:
            return runtime.inbound_operations.preview_rule(rule_id, message_id)
        except InboundOperationsError as error:
            return _error(400, error.code, str(error))

    @app.post(f"{API_PREFIX}/inbound/messages/{{message_id}}/draft")
    async def prepare_inbound_draft(message_id: str, payload: InboundDraftInput):
        try:
            return runtime.inbound_operations.prepare_draft(
                message_id,
                payload.kind,
                body=payload.body,
                account_ref=payload.account_ref,
            )
        except (KeyError, InboundOperationsError, MessageStudioError) as error:
            return _error(400, getattr(error, "code", "draft_failed"), str(error))

    @app.post(f"{API_PREFIX}/inbound/messages/{{message_id}}/contact")
    async def add_inbound_contact(message_id: str, payload: InboundContactInput):
        try:
            return runtime.inbound_operations.add_extracted_contact(
                message_id, field=payload.field, list_id=payload.list_id
            )
        except (KeyError, InboundOperationsError, ValueError) as error:
            return _error(400, getattr(error, "code", "contact_add_failed"), str(error))

    @app.post(f"{API_PREFIX}/inbound/sources/{{source_ref}}/actions")
    async def request_inbound_mailbox_action(
        source_ref: str, payload: InboundMailboxActionInput, message_id: str | None = None
    ):
        try:
            return runtime.inbound_operations.request_mailbox_action(
                source_ref,
                action=payload.action,
                authorization_ref=payload.authorization_ref,
                idempotency_key=payload.idempotency_key,
                message_id=message_id,
                target_folder=payload.target_folder,
            )
        except InboundOperationsError as error:
            return _error(400, error.code, str(error))

    @app.post(f"{API_PREFIX}/inbound/sources/{{source_ref}}/sync")
    async def sync_inbound_source(source_ref: str, payload: InboundSyncInput):
        try:
            account = runtime.provider_operations.account(source_ref)
            grant = None
            if payload.authorization_ref:
                if not payload.purpose or not payload.expires_at:
                    return _error(
                        400,
                        "authorization_incomplete",
                        "sync authorization requires purpose and expiry",
                    )
                grant = ExternalOperationGrant(
                    account_ref=source_ref,
                    operation=ProviderOperation.INBOUND_READINESS,
                    host=account["host"],
                    port=account["port"],
                    purpose=payload.purpose,
                    expires_at=payload.expires_at,
                    authorization_ref=payload.authorization_ref,
                )
            source = runtime.provider_operations.inbound_source(
                source_ref, EnvironmentCredentialResolver(), grant=grant
            )
            report = runtime.inbound.ingest(
                source, batch_limit=payload.batch_limit, retain_raw=payload.retain_raw
            )
            return {"report": asdict(report), "stats": runtime.inbound.stats()}
        except (KeyError, ValueError, PermissionError, RuntimeError) as error:
            return _error(400, "inbound_sync_failed", str(error))

    @app.get(f"{API_PREFIX}/inbound/reviews")
    async def inbound_reviews():
        return {"items": runtime.inbound.review_queue()}

    @app.post(f"{API_PREFIX}/inbound/import")
    async def import_inbound(file: UploadFile = File(...)):  # noqa: B008
        path: Path | None = None
        try:
            path, digest = _upload_inbound(file)
            source = EmlFileSource(
                f"browser-{digest[:16]}", path, stable_identity=f"sha256:{digest}"
            )
            report = runtime.inbound.ingest(source, retain_raw=True)
            return {"report": asdict(report), "stats": runtime.inbound.stats()}
        except (OSError, ValueError, RuntimeError) as error:
            return _error(400, "inbound_import_invalid", str(error))
        finally:
            if path is not None:
                path.unlink(missing_ok=True)

    @app.post(f"{API_PREFIX}/inbound/import-mbox")
    async def import_inbound_mbox(file: UploadFile = File(...)):  # noqa: B008
        path: Path | None = None
        try:
            path, digest = _upload_inbound(file)
            source = MboxSource(f"browser-mbox-{digest[:16]}", path)
            report = runtime.inbound.ingest(source, retain_raw=True)
            return {"report": asdict(report), "stats": runtime.inbound.stats()}
        except (OSError, ValueError, RuntimeError) as error:
            return _error(400, "inbound_mbox_invalid", str(error))
        finally:
            if path is not None:
                path.unlink(missing_ok=True)

    @app.post(f"{API_PREFIX}/inbound/reviews/{{action_id}}")
    async def decide_inbound(action_id: str, payload: ReviewDecision):
        try:
            runtime.inbound.decide(action_id, payload.decision, payload.reason)
            return {"action_id": action_id, "decision": payload.decision}
        except (KeyError, ValueError) as error:
            return _error(400, "review_failed", str(error))

    @app.get(f"{API_PREFIX}/providers")
    async def providers():
        return {
            "items": runtime.provider_operations.accounts(),
            "diagnostics": runtime.provider_operations.diagnostics(None),
        }

    @app.get(f"{API_PREFIX}/providers/capabilities")
    async def provider_capabilities():
        return {"items": runtime.provider_operations.capabilities()}

    @app.get(f"{API_PREFIX}/providers/accounts")
    async def provider_accounts():
        return {"items": runtime.provider_operations.accounts()}

    @app.get(f"{API_PREFIX}/providers/{{provider_ref}}/credentials")
    async def provider_credentials(provider_ref: str):
        try:
            return runtime.providers.credential_status(
                provider_ref, EnvironmentCredentialResolver()
            )
        except (KeyError, ValueError) as error:
            return _error(400, "provider_credentials_failed", str(error))

    @app.post(f"{API_PREFIX}/providers")
    async def register_provider(payload: ProviderInput):
        try:
            values = payload.model_dump()
            account_options = {
                "display_name": values.pop("display_name"),
                "sender_email": values.pop("sender_email"),
                "pacing_per_minute": values.pop("pacing_per_minute"),
                "max_concurrency": values.pop("max_concurrency"),
                "inbound_enabled": values.pop("inbound_enabled"),
            }
            profile = ProviderProfile(**values)
            profile.validate()
            return {
                "account": runtime.provider_operations.register(profile, **account_options),
                "registered": True,
            }
        except (KeyError, ValueError) as error:
            return _error(400, "provider_invalid", str(error))

    @app.patch(f"{API_PREFIX}/providers/{{provider_ref}}")
    async def update_provider(provider_ref: str, payload: ProviderAccountUpdateInput):
        try:
            values = payload.model_dump(exclude_none=True)
            account = runtime.provider_operations.account(provider_ref)
            if "status" in values or "enabled" in values:
                account = runtime.store.set_provider_account_status(
                    provider_ref,
                    values.pop("status", account["status"]),
                    enabled=values.pop("enabled", None),
                )
            if values:
                profile = runtime.provider_operations.providers.profile(provider_ref)
                runtime.provider_operations.register(
                    profile,
                    display_name=values.get("display_name", account["display_name"]),
                    sender_email=account.get("sender_email"),
                    pacing_per_minute=account["pacing_per_minute"],
                    max_concurrency=account["max_concurrency"],
                    outbound_enabled=values.get("outbound_enabled", account["outbound_enabled"]),
                    inbound_enabled=values.get("inbound_enabled", account["inbound_enabled"]),
                )
            return runtime.provider_operations.account(provider_ref)
        except (KeyError, ValueError) as error:
            return _error(400, "provider_update_failed", str(error))

    @app.post(f"{API_PREFIX}/providers/{{provider_ref}}/diagnostics")
    async def provider_diagnostic(provider_ref: str, payload: ProviderDiagnosticInput):
        try:
            account = runtime.provider_operations.account(provider_ref)
            grant = None
            if payload.authorization_ref:
                if not payload.expires_at or not payload.purpose:
                    return _error(
                        400,
                        "authorization_incomplete",
                        "external diagnostics require purpose and expiry",
                    )
                grant = ExternalOperationGrant(
                    account_ref=provider_ref,
                    operation=payload.operation,
                    host=account["host"],
                    port=account["port"],
                    purpose=payload.purpose,
                    sender=payload.sender,
                    recipient=payload.recipient,
                    max_submissions=payload.max_submissions,
                    expires_at=payload.expires_at,
                    authorization_ref=payload.authorization_ref,
                )
            return runtime.provider_operations.diagnostic(
                provider_ref, payload.operation, grant=grant
            )
        except (KeyError, ValueError, PermissionError) as error:
            return _error(400, "provider_diagnostic_failed", str(error))

    @app.post(f"{API_PREFIX}/providers/{{provider_ref}}/inbound")
    async def configure_provider_inbound(provider_ref: str, payload: ProviderInboundInput):
        try:
            return runtime.provider_operations.configure_inbound(
                provider_ref, mailbox=payload.mailbox, max_fetch=payload.max_fetch
            )
        except (KeyError, ValueError) as error:
            return _error(400, "provider_inbound_invalid", str(error))

    @app.post(f"{API_PREFIX}/providers/{{provider_ref}}/oauth/start")
    async def provider_oauth_start(provider_ref: str, payload: ProviderOAuthStartInput):
        try:
            runtime.provider_operations.account(provider_ref)
            return runtime.provider_operations.oauth.start(
                account_ref=provider_ref,
                provider_key=payload.provider_key,
                client_id=payload.client_id,
                redirect_uri=payload.redirect_uri,
                scopes=tuple(payload.scopes),
                authorization_endpoint=payload.authorization_endpoint,
                token_endpoint=payload.token_endpoint,
            )
        except (KeyError, ValueError) as error:
            return _error(400, "provider_oauth_start_failed", str(error))

    @app.post(f"{API_PREFIX}/providers/{{provider_ref}}/oauth/callback")
    async def provider_oauth_callback(provider_ref: str, payload: ProviderOAuthCallbackInput):
        try:
            result = runtime.provider_operations.oauth.complete(
                state=payload.state,
                redirect_uri=payload.redirect_uri,
                code=payload.code,
                identity=payload.identity,
            )
            if result["account_ref"] != provider_ref:
                return _error(
                    400, "provider_oauth_identity_mismatch", "OAuth flow belongs to another account"
                )
            return result
        except ValueError as error:
            return _error(400, "provider_oauth_callback_failed", str(error))

    @app.post(f"{API_PREFIX}/providers/{{provider_ref}}/test-send")
    async def provider_test_send(provider_ref: str, payload: ProviderTestSendInput):
        try:
            account = runtime.provider_operations.account(provider_ref)
            grant = ExternalOperationGrant(
                account_ref=provider_ref,
                operation=ProviderOperation.CONTROLLED_TEST_SEND,
                host=account["host"],
                port=account["port"],
                purpose=payload.purpose,
                sender=payload.sender,
                recipient=payload.recipient,
                max_submissions=1,
                expires_at=payload.expires_at,
                authorization_ref=payload.authorization_ref,
            )
            return runtime.provider_operations.controlled_test_send(
                ref=provider_ref,
                sender=payload.sender,
                recipient=payload.recipient,
                message=payload.message,
                idempotency_key=payload.idempotency_key,
                grant=grant,
                message_revision_id=payload.message_revision_id,
            )
        except (KeyError, ValueError, PermissionError) as error:
            return _error(400, "provider_test_send_failed", str(error))

    @app.get(f"{API_PREFIX}/providers/{{provider_ref}}/tests")
    async def provider_tests(provider_ref: str):
        try:
            runtime.provider_operations.account(provider_ref)
            return {"items": runtime.store.list_provider_tests(provider_ref)}
        except KeyError as error:
            return _error(404, "provider_not_found", str(error))

    @app.post(f"{API_PREFIX}/providers/{{provider_ref}}/validate")
    async def validate_provider(provider_ref: str):
        try:
            return runtime.provider_operations.validate(provider_ref)
        except (KeyError, ValueError) as error:
            return _error(400, "provider_invalid", str(error))

    @app.get(f"{API_PREFIX}/activity")
    async def activity(limit: int = 100, offset: int = 0, aggregate_type: str | None = None):
        safe_limit = max(1, min(limit, 500))
        safe_offset = max(0, offset)
        if aggregate_type:
            return {
                "items": _query(
                    runtime.store,
                    "SELECT * FROM operational_events WHERE aggregate_type = ? "
                    "ORDER BY occurred_at DESC, id DESC LIMIT ? OFFSET ?",
                    (aggregate_type, safe_limit, safe_offset),
                ),
                "limit": safe_limit,
                "offset": safe_offset,
            }
        return {
            "items": _query(
                runtime.store,
                "SELECT * FROM operational_events ORDER BY occurred_at DESC, id DESC "
                "LIMIT ? OFFSET ?",
                (safe_limit, safe_offset),
            ),
            "limit": safe_limit,
            "offset": safe_offset,
        }

    frontend = runtime.frontend_dir
    if frontend is not None and frontend.is_dir():
        assets = frontend / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{path:path}")
        async def frontend_route(path: str):
            candidate = frontend / path
            if path and candidate.is_file() and frontend in candidate.resolve().parents:
                return FileResponse(candidate)
            index = frontend / "index.html"
            if index.is_file():
                return FileResponse(index)
            return _error(404, "frontend_missing", "compiled frontend assets are unavailable")

    return app
