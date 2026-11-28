"""Secure local web application for the Mailforge operator console."""

from __future__ import annotations

import hashlib
import csv
import io
import json
import os
import secrets
import sqlite3
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime
from email.parser import BytesParser
from email.policy import default as email_default_policy
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from mailforge.application.campaign_operations import (
    CampaignOperationsError,
    CampaignOperationsService,
)
from mailforge.application.campaigns import CampaignService
from mailforge.application.ingest import iter_structured_records
from mailforge.application.list_comparison import ListComparisonError, ListComparisonService
from mailforge.application.list_processing import ListProcessingError, ListProcessingService
from mailforge.application.local_application import (
    DataDirectory,
    application_version,
    backup_database_snapshot,
    complete_onboarding,
    create_backup,
    current_schema_version,
    list_backups,
    read_settings,
    restore_backup,
    utc_now,
    validate_backup,
)
from mailforge.application.message_studio import MessageStudioError, MessageStudioService
from mailforge.application.operational_assurance import OperationalAssuranceService
from mailforge.application.outbound import OutboundService
from mailforge.application.outbound_proof import make_proof, verify_proof
from mailforge.application.provider_operations import ProviderOperationsService
from mailforge.application.providers import ProviderService
from mailforge.application.segmentation import SegmentationError, SegmentationService
from mailforge.domain.campaign import CampaignStatus, ContactCandidate, SuppressionCategory
from mailforge.domain.email import extract_addresses, normalize_email, validate_email
from mailforge.domain.outbound import (
    CatchUpPolicy,
    ScheduleMode,
    ScheduleSpec,
    SenderPolicy,
    TemplateDefinition,
)
from mailforge.domain.policy import EvidenceStatus, PreferenceState
from mailforge.domain.provider import (
    AuthenticationMethod,
    ExternalOperationGrant,
    ProviderKind,
    ProviderOperation,
    ProviderProfile,
    TLSMode,
)
from mailforge.infrastructure.campaign_store import SCHEMA_VERSION, CampaignStore
from mailforge.infrastructure.credentials import CredentialError, LocalCredentialResolver
from mailforge.infrastructure.smtp import LiveSendGate

API_PREFIX = "/api/v1"
SESSION_COOKIE = "mailforge_session"
CSRF_COOKIE = "mailforge_csrf"
CSRF_HEADER = "x-mailforge-csrf"


class ListInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)


class ListHandoffInput(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    provider_keys: list[str] = Field(default_factory=list, max_length=200)
    selection_mode: Literal["all", "selected", "none"] | None = None


class ListSelectionInput(BaseModel):
    provider_keys: list[str] = Field(default_factory=list, max_length=200)
    selection_mode: Literal["all", "selected", "none"] | None = None


class ComparisonHandoffInput(BaseModel):
    results: list[dict[str, Any]] = Field(min_length=1, max_length=250_000)
    name: str | None = Field(default=None, max_length=120)


class ContactUpdateInput(BaseModel):
    display_name: str | None = Field(default=None, max_length=240)
    metadata: dict[str, Any] | None = None
    expected_updated_at: str | None = None


class ContactLifecycleInput(BaseModel):
    reason: str = Field(min_length=1, max_length=500)
    actor: str = Field(default="local-operator", min_length=1, max_length=120)


class ContactEvidenceInput(BaseModel):
    purpose: str = Field(default="marketing", min_length=1, max_length=80)
    status: EvidenceStatus = EvidenceStatus.ESTABLISHED
    source: str = Field(min_length=1, max_length=240)
    actor: str = Field(default="local-operator", min_length=1, max_length=120)
    evidence_ref: str | None = Field(default=None, max_length=240)
    list_id: str | None = None
    policy_version: str = Field(default="1", max_length=32)
    details: dict[str, Any] = Field(default_factory=dict)


class ContactPreferenceInput(BaseModel):
    purpose: str = Field(default="marketing", min_length=1, max_length=80)
    state: PreferenceState
    actor: str = Field(default="local-operator", min_length=1, max_length=120)
    evidence_ref: str | None = Field(default=None, max_length=240)
    list_id: str | None = None
    policy_version: str = Field(default="1", max_length=32)


class ContactSuppressionInput(BaseModel):
    category: SuppressionCategory = SuppressionCategory.MANUAL
    reason: str = Field(min_length=1, max_length=500)
    purpose: str | None = Field(default=None, max_length=80)
    list_id: str | None = None
    actor: str = Field(default="local-operator", min_length=1, max_length=120)


class BulkMembershipInput(BaseModel):
    contact_ids: list[str] = Field(min_length=1, max_length=100)
    action: str = Field(pattern="^(add|remove)$")
    reason: str = Field(default="operator bulk action", max_length=240)


class BulkPolicyInput(BaseModel):
    contact_ids: list[str] = Field(min_length=1, max_length=100)
    purpose: str = Field(default="marketing", min_length=1, max_length=80)
    operation: str = Field(pattern="^(preference|suppression|evidence)$")
    preference_state: PreferenceState | None = None
    suppression_reason: str | None = Field(default=None, max_length=500)
    suppression_category: SuppressionCategory = SuppressionCategory.MANUAL
    evidence_source: str | None = Field(default=None, max_length=240)
    actor: str = Field(default="local-operator", min_length=1, max_length=120)


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


class CampaignExecutionInput(BaseModel):
    mode: Literal["loopback_smtp"] = "loopback_smtp"


class CampaignExternalAuthorizationInput(BaseModel):
    authorization_ref: str = Field(min_length=1, max_length=200)
    purpose: str = Field(min_length=1, max_length=500)
    expires_at: str
    recipient_set_hash: str = Field(min_length=32, max_length=128)
    prepared_set_hash: str = Field(min_length=32, max_length=128)


class CampaignExternalAuthorizationPrepareInput(BaseModel):
    purpose: str = Field(min_length=1, max_length=500)
    expires_at: str
    max_submissions: int = Field(ge=1, le=3)
    prepared_set_hash: str = Field(min_length=32, max_length=128)
    confirm_exact_scope: bool = False


class CampaignGrantInput(BaseModel):
    campaign_id: str = Field(min_length=1, max_length=120)
    purpose: str = Field(min_length=1, max_length=500)
    expires_at: str
    max_submissions: int = Field(ge=1, le=3)
    prepared_set_hash: str = Field(min_length=32, max_length=128)


class CampaignDuplicateInput(BaseModel):
    name: str | None = Field(default=None, max_length=160)


class CampaignReconcileInput(BaseModel):
    decision: str = Field(min_length=1, max_length=40)


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


class MessagePreviewBatchInput(BaseModel):
    revision_id: str
    contact_ids: list[str] = Field(min_length=1, max_length=10)


class MessageAttachmentMapInput(BaseModel):
    revision_id: str
    contact_id: str
    asset_id: str
    required: bool = True


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
    ca_file: str | None = None
    sender_policy_ref: str | None = None
    display_name: str | None = Field(default=None, max_length=160)
    sender_email: str | None = None
    pacing_per_minute: int = Field(default=60, ge=1, le=100_000)
    max_concurrency: int = Field(default=1, ge=1, le=100)
    sandbox_key: str | None = Field(default=None, max_length=80)


class ProviderCredentialInput(BaseModel):
    provider_ref: str = Field(min_length=1, max_length=120)
    credential_ref: str = Field(pattern=r"^keychain:[A-Za-z0-9_.:-]+$", max_length=200)
    secret: str = Field(min_length=1, max_length=4096)


class ProviderAccountUpdateInput(BaseModel):
    status: str | None = Field(default=None, max_length=64)
    enabled: bool | None = None
    outbound_enabled: bool | None = None
    display_name: str | None = Field(default=None, max_length=160)


class ProviderDiagnosticInput(BaseModel):
    operation: ProviderOperation
    authorization_ref: str | None = Field(default=None, max_length=200)
    purpose: str | None = Field(default=None, max_length=500)
    sender: str | None = None
    recipient: str | None = None
    max_submissions: int = Field(default=0, ge=0, le=1)
    expires_at: str | None = None


class ProviderTestSendInput(BaseModel):
    sender: str
    recipient: str
    message: str | None = Field(default=None, max_length=10_000_000)
    idempotency_key: str = Field(min_length=8, max_length=200)
    authorization_ref: str = Field(min_length=1, max_length=200)
    purpose: str = Field(min_length=1, max_length=500)
    expires_at: str
    message_revision_id: str | None = None
    proof_id: str | None = None
    proof_hash: str | None = None


class ProviderGrantInput(BaseModel):
    purpose: str = Field(min_length=1, max_length=500)
    sender: str
    recipient: str
    expires_at: str
    proof_hash: str


class ProviderProofInput(BaseModel):
    sender: str
    recipient: str
    message_revision_id: str


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


class ImportReversalInput(BaseModel):
    reason: str = Field(default="operator import reversal", min_length=1, max_length=500)
    actor: str = Field(default="local-operator", min_length=1, max_length=120)


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
        if (
            request.url.path.startswith(API_PREFIX)
            and request.url.path != f"{API_PREFIX}/session"
            and not one_click_path
        ):
            if request.cookies.get(SESSION_COOKIE) != self.session.session_id:
                return _error(401, "session_required", "a local operator session is required")
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not one_click_path:
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

    def __init__(
        self,
        outbound: OutboundService,
        credential_resolver: LocalCredentialResolver,
        provider_operations: ProviderOperationsService,
    ):
        self.outbound = outbound
        self.credential_resolver = credential_resolver
        self.provider_operations = provider_operations
        self.on_completed: Callable[[str], None] | None = None
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
            completed = False
            while True:
                before_counts = self.outbound.store.counts(campaign_id)
                campaign = self.outbound.store.get_campaign(campaign_id)
                configuration = json.loads(campaign["configuration_json"] or "{}")
                mode = configuration.get("execution_mode", "synthetic")
                if mode == "loopback_smtp":
                    provider_ref = campaign["provider_account_ref"]
                    if not provider_ref or not self.provider_operations.is_capture_only_sandbox(
                        provider_ref
                    ):
                        raise PermissionError("trusted local capture fixture is unavailable")
                    report, transport = self.outbound.execute_provider(
                        campaign_id,
                        provider_ref,
                        self.credential_resolver,
                        gate=LiveSendGate(enabled=True),
                        allow_capture_only=True,
                        retry_deferred=True,
                        stop_after=1,
                        worker_id=f"web-{threading.get_ident()}",
                        allow_pause_clear=False,
                    )
                elif mode == "external_smtp":
                    provider_ref = campaign["provider_account_ref"]
                    authorization_ref = configuration.get("external_authorization_ref")
                    purpose = configuration.get("external_authorization_purpose")
                    expires_at = configuration.get("external_authorization_expires_at")
                    prepared_set_hash = configuration.get("external_prepared_set_hash")
                    if (
                        not provider_ref
                        or not authorization_ref
                        or not purpose
                        or not expires_at
                        or not prepared_set_hash
                    ):
                        raise PermissionError("external campaign authorization is incomplete")
                    grant = self.provider_operations.validate_campaign_grant(
                        provider_ref,
                        authorization_ref=authorization_ref,
                        purpose=purpose,
                        expires_at=expires_at,
                        prepared_set_hash=prepared_set_hash,
                    )

                    def consume_submission(
                        _email: str,
                        _intent_id: str,
                        authorization_ref=grant.authorization_ref,
                        account_ref=provider_ref,
                    ) -> None:
                        self.outbound.store.consume_provider_operation_grant(
                            authorization_ref, account_ref=account_ref
                        )

                    report, transport = self.outbound.execute_provider(
                        campaign_id,
                        provider_ref,
                        self.credential_resolver,
                        gate=LiveSendGate.from_environment(explicit_flag=True),
                        retry_deferred=False,
                        stop_after=None,
                        worker_id=f"web-{threading.get_ident()}",
                        submission_guard=consume_submission,
                        allow_pause_clear=False,
                    )
                else:
                    report, transport = self.outbound.execute_worker(
                        campaign_id,
                        worker_id=f"web-{threading.get_ident()}",
                        max_items=1,
                        retry_deferred=True,
                    )
                if report.campaign_status == "paused":
                    pause_requested, cancel_requested = self.outbound.store.campaign_flags(
                        campaign_id
                    )
                    counts = self.outbound.store.counts(campaign_id)
                    unsafe_pending = any(
                        counts.get(state, 0)
                        for state in ("ambiguous", "reserved", "attempt_started")
                    )
                    last_transition = self.outbound.store.campaign_last_transition(campaign_id)
                    if pause_requested or cancel_requested or unsafe_pending:
                        break
                    # Loopback uses bounded one-message chunks. Continue only
                    # for that explicit resumable boundary; temporary waits,
                    # deferred submissions, and ambiguous outcomes stop here
                    # until the durable scheduler or an operator acts.
                    if last_transition and last_transition["reason"] == "resume_available":
                        continue
                    break
                if report.campaign_status in {"completed", "canceled", "failed"}:
                    completed = report.campaign_status == "completed"
                    break
                after_counts = self.outbound.store.counts(campaign_id)
                if before_counts == after_counts:
                    try:
                        current = CampaignStatus(
                            self.outbound.store.get_campaign(campaign_id)["status"]
                        )
                        if current is CampaignStatus.RUNNING:
                            next_automatic_at = self.outbound.store.campaign_next_automatic_at(
                                campaign_id
                            )
                            self.outbound.store.transition_campaign(
                                campaign_id,
                                CampaignStatus.COMPLETING,
                                "worker_waiting_for_temporary_restriction",
                                {"next_automatic_at": next_automatic_at},
                            )
                            self.outbound.store.transition_campaign(
                                campaign_id,
                                CampaignStatus.PAUSED,
                                "retry_after_temporary_restriction",
                            )
                    except ValueError:
                        pass
                    break
                time.sleep(0.03)
            if completed and self.on_completed is not None:
                self.on_completed(campaign_id)
        except Exception as error:
            try:
                campaign = self.outbound.store.get_campaign(campaign_id)
                with self.outbound.store.transaction(immediate=True) as connection:
                    self.outbound.store.record_event(
                        connection,
                        "campaign",
                        campaign_id,
                        "campaign_worker_failed",
                        {
                            "error": type(error).__name__,
                            "detail": (
                                str(error)[:240]
                                if not any(
                                    token in str(error).lower()
                                    for token in ("secret", "password", "token", "bearer")
                                )
                                else "worker failure detail redacted"
                            ),
                            "status": campaign["status"],
                        },
                    )
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
    """Starts only authorized, due schedules from durable campaign rows."""

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
                        "SELECT c.id, c.status, c.configuration_json, c.pause_requested, "
                        "c.cancel_requested, s.* FROM campaigns c LEFT JOIN campaign_schedules s "
                        "ON s.campaign_id=c.id WHERE c.status IN ('scheduled', 'paused') "
                        "AND c.authorization='simulation_authorized'"
                    ).fetchall()
                for row in rows:
                    configuration = json.loads(row["configuration_json"] or "{}")
                    if (
                        configuration.get("archived_at")
                        or row["pause_requested"]
                        or row["cancel_requested"]
                    ):
                        continue
                    now = self.outbound.now_fn()
                    due = False
                    schedule = None
                    if row["mode"]:
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
                    if row["status"] == CampaignStatus.PAUSED.value:
                        next_at = self.store.campaign_next_automatic_at(row["id"], now=now)
                        due_now = self.outbound.now_fn().isoformat()
                        last_transition = self.store.campaign_last_transition(row["id"])
                        due = (
                            next_at is not None
                            and next_at <= due_now
                            and last_transition is not None
                            and last_transition["reason"]
                            in {
                                "retry_after_temporary_restriction",
                                "worker_waiting_for_temporary_restriction",
                                "unresolved_delivery_state",
                            }
                            and not self.store.campaign_has_unsafe_inflight(row["id"])
                            and (schedule is None or self.outbound.schedules.due(schedule, now))
                        )
                    elif schedule is not None:
                        due = self.outbound.schedules.due(schedule, now)
                    if due:
                        self.workers.start(row["id"])
            except Exception:
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
        self.credential_resolver = LocalCredentialResolver()
        self.segmentation = SegmentationService(self.store)
        self.outbound = OutboundService(self.store)
        self.message_studio = MessageStudioService(self.store, outbound=self.outbound)
        self.providers = ProviderService(self.store)
        self.provider_operations = ProviderOperationsService(self.store)
        self.operational_assurance = OperationalAssuranceService(self.store, self.data_directory)
        self.workers = WorkerSupervisor(
            self.outbound, self.credential_resolver, self.provider_operations
        )
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
            worker_active=self.workers.active,
            worker_start=self.workers.start,
        )
        self.workers.on_completed = self._advance_recurring_campaign
        self.list_processing = ListProcessingService(self.store)
        self.list_comparison = ListComparisonService(self.store)
        self.frontend_dir = Path(frontend_dir) if frontend_dir else None
        self.store.reconcile_abandoned_provider_test_submissions()
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

    def _advance_recurring_campaign(self, campaign_id: str) -> None:
        try:
            self.campaign_operations.create_next_recurring_run(campaign_id)
        except CampaignOperationsError, KeyError, ValueError:
            # The completed run and its evidence remain durable; an operator
            # can inspect the failed recurrence in the campaign workspace.
            with self.store.transaction(immediate=True) as connection:
                self.store.record_event(
                    connection,
                    "campaign",
                    campaign_id,
                    "campaign_recurrence_advance_failed",
                )

    def close(self) -> None:
        self.list_processing.close()
        self.campaign_scheduler.shutdown()
        self.workers.shutdown()

    def worker_status(self) -> dict[str, Any]:
        return {
            "campaigns_active": len(self.workers._running),
            "scheduler": "running" if self.campaign_scheduler.thread.is_alive() else "stopped",
            "credential_store": self.credential_resolver.readiness(),
        }

    def liveness(self) -> dict[str, Any]:
        """Cheap launcher probe; detailed assurance remains an explicit operation."""

        settings = read_settings(self.data_directory)
        status = {
            "application": "Mailforge",
            "version": application_version(),
            "data_directory_id": self.data_directory.identity,
            "database_present": self.data_directory.database.is_file(),
            "schema_version": current_schema_version(self.data_directory.database),
            "supported_schema_version": SCHEMA_VERSION,
            "database_integrity": None,
            "onboarding_complete": bool(settings.get("onboarding_complete")),
            "preferences": settings.get("preferences", {}),
            "backup_count": len(list(self.data_directory.backups.glob("*.zip")))
            if self.data_directory.backups.is_dir()
            else 0,
        }
        status.update(
            {
                "runtime_id": self.runtime_id,
                "started_at": self.started_at,
                "pid": os.getpid(),
                "worker_status": self.worker_status(),
                "loopback_only": True,
                "simulation_mode": True,
            }
        )
        return status

    def status(self) -> dict[str, Any]:
        status = self.liveness()
        status["operational"] = self.operational_assurance.health(
            worker_status=status["worker_status"]
        )
        database_check = next(
            (check for check in status["operational"]["checks"] if check["key"] == "database"),
            None,
        )
        if database_check is not None:
            status["database_integrity"] = database_check["state"] == "healthy"
        return status


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


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


def _contact_csv_safe(value: Any) -> Any:
    if isinstance(value, str) and value[:1] in {"=", "+", "-", "@"}:
        return "'" + value
    return value


def _query(store: CampaignStore, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with store.connect() as connection:
        return [_row(row) for row in connection.execute(sql, params).fetchall()]


def _require_list(store: CampaignStore, list_id: str) -> None:
    with store.connect() as connection:
        if connection.execute("SELECT 1 FROM lists WHERE id = ?", (list_id,)).fetchone() is None:
            raise KeyError(f"list not found: {list_id}")


def _campaign_view(runtime: WebRuntime, row: sqlite3.Row) -> dict[str, Any]:
    campaign = _row(row)
    configuration = json.loads(row["configuration_json"] or "{}")
    campaign.pop("configuration", None)
    campaign["archived"] = bool(configuration.get("archived_at"))
    campaign["recurrence_parent_id"] = configuration.get("recurrence_parent_id")
    campaign["recurrence_index"] = configuration.get("recurrence_index")
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
        for _record_position, value, metadata in iter_structured_records(Path(handle.name)):
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
                        metadata=metadata,
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
        return runtime.liveness()

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

    @app.get(f"{API_PREFIX}/operations/health")
    async def operations_health():
        return runtime.operational_assurance.health(worker_status=runtime.worker_status())

    @app.get(f"{API_PREFIX}/operations/jobs")
    async def operations_jobs(subsystem: str | None = None, status: str | None = None):
        return runtime.operational_assurance.jobs(subsystem=subsystem, status=status)

    @app.post(f"{API_PREFIX}/operations/jobs/{{subsystem}}/{{job_id}}/action")
    async def operational_job_action(subsystem: str, job_id: str, payload: dict[str, str]):
        action = payload.get("action", "")
        try:
            if subsystem == "list_tools" and action in {"pause", "resume", "cancel"}:
                if action == "pause":
                    return runtime.list_processing.pause(job_id)
                if action == "resume":
                    return runtime.list_processing.start(job_id, resume=True)
                return runtime.list_processing.cancel(job_id)
            if subsystem == "campaigns" and action in {"pause", "resume", "cancel"}:
                return runtime.campaign_operations.control(job_id, action)
            raise ValueError("action is not supported for this subsystem")
        except (KeyError, ValueError, RuntimeError, PermissionError) as error:
            return _error(400, "operational_action_failed", str(error))

    @app.get(f"{API_PREFIX}/operations/incidents")
    async def operations_incidents(include_acknowledged: bool = False):
        return runtime.operational_assurance.incidents(
            include_acknowledged=include_acknowledged,
            worker_status=runtime.worker_status(),
        )

    @app.post(f"{API_PREFIX}/operations/health-check")
    async def operations_health_check():
        return runtime.operational_assurance.health_check(worker_status=runtime.worker_status())

    @app.post(f"{API_PREFIX}/operations/incidents/{{incident_id}}/acknowledge")
    async def acknowledge_operational_incident(incident_id: str):
        try:
            return runtime.operational_assurance.acknowledge(incident_id)
        except ValueError as error:
            return _error(400, "incident_invalid", str(error))

    @app.get(f"{API_PREFIX}/operations/diagnostic-export")
    async def operational_diagnostic_export():
        return runtime.operational_assurance.diagnostic_export(
            worker_status=runtime.worker_status()
        )

    @app.get(f"{API_PREFIX}/runtime/backups")
    async def runtime_backups():
        return {"items": list_backups(runtime.data_directory)}

    @app.post(f"{API_PREFIX}/runtime/backup")
    async def runtime_backup():
        backup = create_backup(runtime.data_directory)
        return {"backup": backup.name, "metadata": list_backups(runtime.data_directory)[0]}

    @app.post(f"{API_PREFIX}/runtime/backups/{{backup_name}}/verify")
    async def verify_runtime_backup(backup_name: str):
        if Path(backup_name).name != backup_name:
            return _error(400, "backup_path_invalid", "choose a backup from the local backup list")
        try:
            metadata = validate_backup(runtime.data_directory.backups / backup_name)
            return {
                "backup": backup_name,
                "verified": bool(metadata.get("valid")),
                "metadata": metadata,
            }
        except (FileNotFoundError, OSError, ValueError, KeyError, TypeError) as error:
            return _error(400, "backup_verification_failed", type(error).__name__)

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

    @app.get(f"{API_PREFIX}/dashboard")
    async def dashboard():
        campaigns = _query(
            runtime.store, "SELECT * FROM campaigns ORDER BY updated_at DESC LIMIT 20"
        )
        contacts = _query(runtime.store, "SELECT COUNT(*) AS count FROM contacts")[0]["count"]
        lists = _query(runtime.store, "SELECT COUNT(*) AS count FROM lists")[0]["count"]
        return {
            "counts": {"contacts": contacts, "lists": lists},
            "schema_version": runtime.store.schema_version(),
            "supported_schema_version": SCHEMA_VERSION,
            "database_integrity": runtime.store.integrity_check(),
            "campaigns": [
                _campaign_view(runtime, runtime.store.get_campaign(item["id"]))
                for item in campaigns
            ],
        }

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

    @app.post(f"{API_PREFIX}/message-studio/preview-batch")
    async def preview_message_batch(payload: MessagePreviewBatchInput):
        try:
            return runtime.message_studio.preview_batch(payload.revision_id, payload.contact_ids)
        except (MessageStudioError, KeyError) as error:
            return _error(400, getattr(error, "code", "preview_invalid"), str(error))

    @app.post(f"{API_PREFIX}/message-studio/custom-html/import")
    async def import_custom_html(
        name: str = "Imported HTML template",
        kind: str = "template",
        html_file: UploadFile = File(...),  # noqa: B008
        assets_zip: UploadFile | None = File(default=None),  # noqa: B008
    ):
        try:
            html_data = html_file.file.read(512 * 1024 + 1)
            if len(html_data) > 512 * 1024:
                return _error(413, "html_limit", "custom HTML exceeds the 512 KiB local limit")
            zip_data = None
            if assets_zip is not None:
                zip_data = assets_zip.file.read(25 * 1024 * 1024 + 1)
                if len(zip_data) > 25 * 1024 * 1024:
                    return _error(413, "zip_limit", "asset ZIP exceeds the 25 MiB local limit")
            return runtime.message_studio.import_custom_html(
                name.strip() or "Imported HTML template",
                html_data,
                filename=html_file.filename or "message.html",
                assets_zip=zip_data,
                kind=kind,
            )
        except MessageStudioError as error:
            return _error(400, error.code, str(error))
        except ValueError as error:
            return _error(400, "custom_html_invalid", str(error))

    @app.get(f"{API_PREFIX}/message-studio/documents/{{document_id}}/export")
    async def export_custom_html(document_id: str, revision_id: str | None = None):
        try:
            data, filename = runtime.message_studio.export_custom_html(
                document_id, revision_id=revision_id
            )
            return Response(
                content=data,
                media_type="application/zip",
                headers={
                    "content-disposition": f'attachment; filename="{filename}"',
                    "cache-control": "no-store",
                },
            )
        except MessageStudioError as error:
            return _error(404 if error.code.endswith("_not_found") else 400, error.code, str(error))

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

    @app.post(f"{API_PREFIX}/lists/{{list_id}}/bulk-members")
    async def bulk_list_members(list_id: str, payload: BulkMembershipInput):
        try:
            results = [
                runtime.segmentation.set_membership(
                    list_id,
                    contact_id,
                    payload.action,
                    source="browser-bulk",
                    reason=payload.reason,
                    idempotency_key=f"browser-bulk:{list_id}:{payload.action}:{contact_id}",
                )
                for contact_id in payload.contact_ids
            ]
            return {
                "action": payload.action,
                "requested": len(results),
                "changed": sum(1 for result in results if result["changed"]),
                "results": results,
            }
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

    @app.post(f"{API_PREFIX}/contacts/bulk-policy")
    async def bulk_contact_policy(payload: BulkPolicyInput):
        results = []
        try:
            for contact_id in payload.contact_ids:
                contact = runtime.store.get_contact(contact_id)
                if payload.operation == "preference":
                    if payload.preference_state is None:
                        return _error(
                            400, "preference_state_required", "preference state is required"
                        )
                    value = runtime.campaigns.set_preference(
                        contact["normalized_email"],
                        purpose=payload.purpose,
                        state=payload.preference_state,
                        actor=payload.actor,
                    )
                    results.append({"contact_id": contact_id, "preference_id": value})
                elif payload.operation == "suppression":
                    if not payload.suppression_reason:
                        return _error(
                            400, "suppression_reason_required", "suppression reason is required"
                        )
                    value = runtime.campaigns.add_suppression(
                        contact["normalized_email"],
                        payload.suppression_category,
                        payload.suppression_reason,
                        purpose=payload.purpose,
                        actor=payload.actor,
                    )
                    results.append({"contact_id": contact_id, "suppression_id": value})
                else:
                    if not payload.evidence_source:
                        return _error(
                            400, "evidence_source_required", "evidence source is required"
                        )
                    value = runtime.campaigns.record_authorization_evidence(
                        contact["normalized_email"],
                        purpose=payload.purpose,
                        source=payload.evidence_source,
                        actor=payload.actor,
                    )
                    results.append({"contact_id": contact_id, "evidence_id": value})
        except KeyError:
            return _error(404, "contact_not_found", "contact was not found")
        return {"operation": payload.operation, "requested": len(results), "results": results}

    @app.get(f"{API_PREFIX}/contacts/export")
    async def export_contacts(list_id: str | None = None, q: str = "", format: str = "json"):
        if format not in {"json", "csv"}:
            return _error(400, "format_invalid", "contact export format must be json or csv")
        items = runtime.campaigns.export_contacts(list_id=list_id)
        if q.strip():
            needle = q.strip().lower()
            items = [
                item
                for item in items
                if needle in item["normalized_email"].lower()
                or needle in (item.get("display_name") or "").lower()
            ]
        if format == "json":
            return Response(
                content=json.dumps({"items": items}, ensure_ascii=False, indent=2),
                media_type="application/json",
                headers={"content-disposition": 'attachment; filename="contacts.json"'},
            )
        import csv
        import io

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["email", "display_name", "lifecycle_status", "metadata"])
        for item in items:
            writer.writerow(
                [
                    _contact_csv_safe(item["normalized_email"]),
                    _contact_csv_safe(item.get("display_name") or ""),
                    _contact_csv_safe(item.get("lifecycle_status") or "active"),
                    _contact_csv_safe(
                        json.dumps(item.get("metadata") or {}, ensure_ascii=False, sort_keys=True)
                    ),
                ]
            )
        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={"content-disposition": 'attachment; filename="contacts.csv"'},
        )

    @app.get(f"{API_PREFIX}/contacts/{{contact_id}}")
    async def contact_detail(contact_id: str):
        try:
            contact = runtime.store.get_contact(contact_id)
        except KeyError:
            return _error(404, "contact_not_found", "contact was not found")
        lists = _query(
            runtime.store,
            "SELECT l.id, l.name, l.description FROM lists l "
            "JOIN list_memberships m ON m.list_id = l.id WHERE m.contact_id = ? ORDER BY l.name",
            (contact_id,),
        )
        sources = _query(
            runtime.store,
            "SELECT source_file, source_record_position, original_input, metadata_json, "
            "imported_at "
            "FROM contact_sources WHERE contact_id = ? ORDER BY imported_at DESC LIMIT 100",
            (contact_id,),
        )
        attributes = _query(
            runtime.store,
            "SELECT d.id, d.machine_key, d.label, d.field_type, v.text_value, v.number_value, "
            "v.boolean_value, v.date_value, v.datetime_value, v.source_ref, "
            "v.source_record_position "
            "FROM contact_attribute_values v JOIN contact_field_definitions d ON d.id = v.field_id "
            "WHERE v.contact_id = ? ORDER BY d.machine_key",
            (contact_id,),
        )
        return {
            "contact": _row(contact),
            "lists": lists,
            "sources": sources,
            "attributes": attributes,
            "evidence": [
                _row(row)
                for row in runtime.store.list_authorization_evidence(contact_id=contact_id)
            ],
            "preferences": [
                _row(row) for row in runtime.store.list_preferences(contact_id=contact_id)
            ],
            "suppressions": [
                _row(row)
                for row in runtime.store.list_suppressions(
                    normalized_email=contact["normalized_email"]
                )
            ],
            "lifecycle": [
                _row(row) for row in runtime.store.list_lifecycle_records(contact_id=contact_id)
            ],
        }

    @app.patch(f"{API_PREFIX}/contacts/{{contact_id}}")
    async def update_contact(contact_id: str, payload: ContactUpdateInput):
        try:
            contact = runtime.store.update_contact(
                contact_id,
                display_name=payload.display_name,
                metadata=payload.metadata,
                expected_updated_at=payload.expected_updated_at,
            )
            return {"contact": _row(contact)}
        except KeyError:
            return _error(404, "contact_not_found", "contact was not found")
        except ValueError as error:
            return _error(409, "contact_conflict", str(error))

    @app.post(f"{API_PREFIX}/contacts/{{contact_id}}/deactivate")
    async def deactivate_contact(contact_id: str, payload: ContactLifecycleInput):
        try:
            runtime.store.deactivate_contact(contact_id, reason=payload.reason, actor=payload.actor)
            return {"contact_id": contact_id, "status": "deactivated"}
        except KeyError:
            return _error(404, "contact_not_found", "contact was not found")

    @app.get(f"{API_PREFIX}/contacts/{{contact_id}}/policy")
    async def contact_policy(
        contact_id: str, purpose: str = "marketing", list_id: str | None = None
    ):
        try:
            return runtime.campaigns.inspect_contact_policy(
                contact_id, purpose=purpose, list_id=list_id
            )
        except KeyError:
            return _error(404, "contact_not_found", "contact was not found")

    @app.post(f"{API_PREFIX}/contacts/{{contact_id}}/evidence")
    async def record_contact_evidence(contact_id: str, payload: ContactEvidenceInput):
        try:
            contact = runtime.store.get_contact(contact_id)
            evidence_id = runtime.campaigns.record_authorization_evidence(
                contact["normalized_email"],
                purpose=payload.purpose,
                status=payload.status,
                source=payload.source,
                actor=payload.actor,
                evidence_ref=payload.evidence_ref,
                list_id=payload.list_id,
                policy_version=payload.policy_version,
                details=payload.details,
            )
            return {"evidence_id": evidence_id, "status": payload.status.value}
        except KeyError:
            return _error(404, "contact_not_found", "contact was not found")

    @app.post(f"{API_PREFIX}/contacts/{{contact_id}}/preference")
    async def record_contact_preference(contact_id: str, payload: ContactPreferenceInput):
        try:
            contact = runtime.store.get_contact(contact_id)
            preference_id = runtime.campaigns.set_preference(
                contact["normalized_email"],
                purpose=payload.purpose,
                state=payload.state,
                actor=payload.actor,
                evidence_ref=payload.evidence_ref,
                list_id=payload.list_id,
                policy_version=payload.policy_version,
            )
            return {"preference_id": preference_id, "state": payload.state.value}
        except KeyError:
            return _error(404, "contact_not_found", "contact was not found")

    @app.post(f"{API_PREFIX}/contacts/{{contact_id}}/suppress")
    async def suppress_contact(contact_id: str, payload: ContactSuppressionInput):
        try:
            contact = runtime.store.get_contact(contact_id)
            suppression_id = runtime.campaigns.add_suppression(
                contact["normalized_email"],
                payload.category,
                payload.reason,
                list_id=payload.list_id,
                purpose=payload.purpose,
                actor=payload.actor,
            )
            return {"suppression_id": suppression_id, "category": payload.category.value}
        except KeyError:
            return _error(404, "contact_not_found", "contact was not found")

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
            batch_id = runtime.store.create_import_batch(
                list_id=list_id,
                source_ref=file.filename or "file-import",
                total_rows=len(candidates),
            )
            try:
                result = runtime.campaigns.import_candidates(candidates, list_id, batch_id=batch_id)
                runtime.store.finish_import_batch(batch_id, status="completed")
            except Exception as error:
                runtime.store.finish_import_batch(
                    batch_id, status="failed", error_message=str(error)
                )
                raise
            return {**result, "preview": preview, "batch_id": batch_id}
        except KeyError:
            return _error(404, "list_not_found", "target list was not found")
        except (OSError, ValueError) as error:
            return _error(400, "import_invalid", str(error))

    @app.get(f"{API_PREFIX}/imports/batches/{{batch_id}}")
    async def import_batch(batch_id: str):
        try:
            return runtime.store.get_import_batch(batch_id)
        except KeyError:
            return _error(404, "import_batch_not_found", "import batch was not found")

    @app.post(f"{API_PREFIX}/imports/batches/{{batch_id}}/reverse")
    async def reverse_import_batch(batch_id: str, payload: ImportReversalInput):
        try:
            return runtime.store.reverse_import_batch(
                batch_id, actor=payload.actor, reason=payload.reason
            )
        except KeyError:
            return _error(404, "import_batch_not_found", "import batch was not found")

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

    @app.get(f"{API_PREFIX}/list-comparison/history")
    async def list_comparison_history(limit: int = 100):
        """Return selectable send history without treating prepared rows as sent."""

        return {"items": runtime.list_comparison.history(limit=limit)}

    @app.post(f"{API_PREFIX}/list-comparison/preview")
    @app.post(f"{API_PREFIX}/comparison/preview")
    async def list_comparison_preview(
        main: UploadFile | None = File(default=None),  # noqa: B008
        references: list[UploadFile] = File(default_factory=list),  # noqa: B008
        operation: str = Form(default="subtract"),
        campaign_ids: str = Form(default="[]"),
        main_list_id: str | None = Form(default=None),
        reference_list_ids: str = Form(default="[]"),
    ):
        temporary: list[Path] = []
        try:
            try:
                selected_campaigns = json.loads(campaign_ids or "[]")
                selected_lists = json.loads(reference_list_ids or "[]")
            except json.JSONDecodeError as error:
                raise ListComparisonError(
                    "history and saved-list selections must be JSON arrays", "selection_invalid"
                ) from error
            if not isinstance(selected_campaigns, list) or not all(
                isinstance(item, str) for item in selected_campaigns
            ):
                raise ListComparisonError(
                    "campaign_ids must be a list of identifiers", "selection_invalid"
                )
            if not isinstance(selected_lists, list) or not all(
                isinstance(item, str) for item in selected_lists
            ):
                raise ListComparisonError(
                    "reference_list_ids must be a list of identifiers", "selection_invalid"
                )
            if main_list_id:
                result = runtime.list_comparison.compare_saved_lists(
                    main_list_id,
                    selected_lists,
                    operation=operation,
                    history_campaign_ids=selected_campaigns,
                )
            else:
                if main is None:
                    raise ListComparisonError("choose a main list or upload one", "main_required")
                main_path = _upload_list_tool(main, runtime.list_processing.root)
                temporary.append(main_path)
                reference_paths = []
                for upload in references:
                    path = _upload_list_tool(upload, runtime.list_processing.root)
                    temporary.append(path)
                    reference_paths.append(path)
                result = runtime.list_comparison.compare_files(
                    main_path,
                    reference_paths,
                    operation=operation,
                    history_campaign_ids=selected_campaigns,
                )
            return result
        except ListComparisonError as error:
            return _error(404 if error.code == "list_not_found" else 400, error.code, str(error))
        finally:
            for path in temporary:
                path.unlink(missing_ok=True)

    @app.post(f"{API_PREFIX}/list-comparison/export")
    async def list_comparison_export(payload: dict[str, Any]):
        try:
            content = runtime.list_comparison.export_csv(payload)
        except (TypeError, KeyError) as error:
            return _error(400, "export_invalid", str(error))
        return Response(
            content=content,
            media_type="text/csv",
            headers={"content-disposition": 'attachment; filename="mailforge-comparison.csv"'},
        )

    @app.post(f"{API_PREFIX}/list-comparison/handoff")
    async def list_comparison_handoff(payload: ComparisonHandoffInput):
        try:
            result_hash = hashlib.sha256(
                json.dumps(payload.results, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            description = f"List comparison result {result_hash}"
            existing = _query(
                runtime.store,
                "SELECT id, name, description FROM lists WHERE description = ? LIMIT 1",
                (description,),
            )
            if existing:
                return {"list": existing[0], "reused": True}
            name = payload.name.strip() if payload.name else ""
            name = name or f"Comparison result {result_hash[:8]}"
            list_id = runtime.store.create_list(name, description)
            imported = runtime.campaigns.import_comparison_results(payload.results, list_id)
            return {
                "list": {"id": list_id, "name": name, "description": description},
                "reused": False,
                **imported,
            }
        except (KeyError, OSError, ValueError, sqlite3.IntegrityError) as error:
            return _error(400, "comparison_handoff_failed", str(error))

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
    async def list_tools_job_action(
        job_id: str, action: str, payload: ListHandoffInput | None = None
    ):
        try:
            if action == "handoff":
                result_path = runtime.list_processing.structured_result_path(job_id)
                selection_mode = (
                    payload.selection_mode
                    if payload and payload.selection_mode
                    else "selected"
                    if payload and payload.provider_keys
                    else "all"
                )
                selection_hash = hashlib.sha256(
                    json.dumps(
                        {
                            "mode": selection_mode,
                            "providers": sorted(payload.provider_keys if payload else []),
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()[:16]
                description = f"Sort email result from job {job_id} selection {selection_hash}"
                existing = _query(
                    runtime.store,
                    "SELECT id, name, description FROM lists WHERE description = ? LIMIT 1",
                    (description,),
                )
                if existing:
                    return {"list": existing[0], "reused": True}
                name = payload.name.strip() if payload and payload.name else ""
                name = name or f"Sorted result {job_id[:8]} {selection_hash}"
                list_id = runtime.store.create_list(name, description)
                imported = runtime.campaigns.import_sort_results(
                    result_path,
                    list_id,
                    provider_keys=(payload.provider_keys if payload else None),
                    selection_mode=(
                        payload.selection_mode
                        if payload and payload.selection_mode
                        else "selected"
                        if payload and payload.provider_keys
                        else "all"
                    ),
                )
                return {
                    "list": {"id": list_id, "name": name, "description": description},
                    "reused": False,
                    **imported,
                }
            if action == "export-selected":
                content, filename = runtime.list_processing.export_selected(
                    job_id,
                    payload.provider_keys if payload else [],
                    selection_mode=(
                        payload.selection_mode
                        if payload and payload.selection_mode
                        else "selected"
                        if payload and payload.provider_keys
                        else "all"
                    ),
                )
                return Response(
                    content=content,
                    media_type="text/csv",
                    headers={"content-disposition": f'attachment; filename="{filename}"'},
                )
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
        except (OSError, ValueError, KeyError, sqlite3.IntegrityError) as error:
            return _error(400, "handoff_failed", str(error))

    @app.post(f"{API_PREFIX}/list-tools/jobs/{{job_id}}/export-selected")
    async def export_selected_sort_results(job_id: str, payload: ListSelectionInput):
        """Export the selected canonical sort rows without losing structured fields."""
        try:
            job = runtime.list_processing.get_job(job_id)
            result_path = runtime.list_processing.structured_result_path(job_id)
            selected = {item.strip() for item in payload.provider_keys if item.strip()}
            output = io.StringIO(newline="")
            writer = csv.DictWriter(
                output,
                fieldnames=[
                    "email",
                    "provider",
                    "domain",
                    "registered_domain",
                    "source_file",
                    "source_record_position",
                    "associated_data",
                ],
            )
            writer.writeheader()
            with result_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    record = json.loads(line)
                    if record.get("processing_disposition") != "accepted":
                        continue
                    classification = record.get("classification_result") or {}
                    provider = str(classification.get("provider") or "unknown")
                    if selected and provider not in selected:
                        continue
                    associated = (record.get("metadata") or {}).get("associated_data") or {}
                    writer.writerow(
                        {
                            "email": record.get("normalized_address", ""),
                            "provider": provider,
                            "domain": classification.get("domain", ""),
                            "registered_domain": classification.get("registered_domain", ""),
                            "source_file": record.get("source_file", ""),
                            "source_record_position": record.get("source_record_position", ""),
                            "associated_data": json.dumps(
                                associated, ensure_ascii=False, sort_keys=True
                            ),
                        }
                    )
            suffix = "selected" if selected else "all"
            return Response(
                content=output.getvalue(),
                media_type="text/csv",
                headers={
                    "content-disposition": (
                        f'attachment; filename="{job["source_filename"]}-{suffix}.csv"'
                    )
                },
            )
        except (KeyError, OSError, ValueError, json.JSONDecodeError) as error:
            return _error(404 if isinstance(error, KeyError) else 400, "sort_export_failed", str(error))

    @app.get(f"{API_PREFIX}/list-tools/jobs/{{job_id}}/artifacts/{{artifact_id}}")
    async def list_tools_artifact(job_id: str, artifact_id: str):
        try:
            path, filename = runtime.list_processing.artifact_path(job_id, artifact_id)
            return FileResponse(path, filename=filename, headers={"cache-control": "no-store"})
        except ListProcessingError as error:
            return _error(
                404 if error.code == "artifact_not_found" else 403, error.code, str(error)
            )

    @app.get(f"{API_PREFIX}/campaigns")
    async def campaigns(limit: int = 100, offset: int = 0, include_archived: bool = False):
        safe_limit = max(1, min(limit, 500))
        safe_offset = max(0, offset)
        rows = _query(
            runtime.store,
            "SELECT * FROM campaigns ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (min(500, safe_limit * 3), 0),
        )
        visible = []
        for item in rows:
            view = _campaign_view(runtime, runtime.store.get_campaign(item["id"]))
            if include_archived or not view["archived"]:
                visible.append(view)
        visible = visible[safe_offset : safe_offset + safe_limit]
        return {
            "items": visible,
            "limit": safe_limit,
            "offset": safe_offset,
            "include_archived": include_archived,
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

    @app.post(f"{API_PREFIX}/campaigns/{{campaign_id}}/duplicate")
    async def duplicate_campaign(campaign_id: str, payload: CampaignDuplicateInput | None = None):
        try:
            duplicate_id = runtime.campaign_operations.duplicate_campaign(
                campaign_id, name=payload.name if payload else None
            )
            return {"id": duplicate_id, "status": "draft"}
        except CampaignOperationsError as error:
            return _error(400, error.code, str(error))
        except KeyError:
            return _error(404, "campaign_not_found", "campaign was not found")

    @app.post(f"{API_PREFIX}/campaigns/{{campaign_id}}/archive")
    async def archive_campaign(campaign_id: str):
        try:
            return runtime.campaign_operations.archive_campaign(campaign_id)
        except KeyError:
            return _error(404, "campaign_not_found", "campaign was not found")
        except ValueError as error:
            return _error(409, "campaign_archive_failed", str(error))

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
            runtime.campaign_operations.set_execution_mode(campaign_id, "synthetic")
            runtime.store.clear_pause_request(campaign_id)
            started = runtime.workers.start(campaign_id)
            return {"campaign_id": campaign_id, "started": started, "mode": "synthetic"}
        except (KeyError, ValueError, PermissionError) as error:
            return _error(400, "simulation_failed", str(error))

    @app.post(f"{API_PREFIX}/campaigns/{{campaign_id}}/execute")
    async def execute_campaign(campaign_id: str, payload: CampaignExecutionInput):
        try:
            campaign = runtime.store.get_campaign(campaign_id)
            if campaign[
                "authorization"
            ] != "simulation_authorized" or not runtime.store.has_execution_declaration(
                campaign_id
            ):
                return _error(
                    400,
                    "execution_not_authorized",
                    "authorize the prepared campaign before starting loopback SMTP execution",
                )
            runtime.campaign_operations.set_execution_mode(campaign_id, payload.mode)
            runtime.store.clear_pause_request(campaign_id)
            started = runtime.workers.start(campaign_id)
            return {"campaign_id": campaign_id, "started": started, "mode": payload.mode}
        except (KeyError, ValueError, PermissionError, CampaignOperationsError) as error:
            return _error(400, "execution_failed", str(error))

    @app.get(f"{API_PREFIX}/campaigns/{{campaign_id}}/external-authorization-preview")
    async def external_campaign_authorization_preview(campaign_id: str):
        try:
            return runtime.campaign_operations.external_authorization_preview(campaign_id)
        except (KeyError, ValueError, CampaignOperationsError) as error:
            return _error(
                400, getattr(error, "code", "external_authorization_preview_failed"), str(error)
            )

    @app.post(f"{API_PREFIX}/campaigns/{{campaign_id}}/authorize-external")
    async def authorize_external_campaign(
        campaign_id: str, payload: CampaignExternalAuthorizationInput
    ):
        try:
            return runtime.campaign_operations.bind_external_authorization(
                campaign_id, **payload.model_dump()
            )
        except (KeyError, ValueError, PermissionError, CampaignOperationsError) as error:
            return _error(400, getattr(error, "code", "external_authorization_failed"), str(error))

    @app.post(f"{API_PREFIX}/campaigns/{{campaign_id}}/prepare-external-authorization")
    async def prepare_external_authorization(
        campaign_id: str, payload: CampaignExternalAuthorizationPrepareInput
    ):
        try:
            return runtime.campaign_operations.prepare_external_authorization(
                campaign_id, **payload.model_dump()
            )
        except (KeyError, ValueError, PermissionError, CampaignOperationsError) as error:
            return _error(
                400,
                getattr(error, "code", "external_authorization_prepare_failed"),
                str(error),
            )

    @app.post(f"{API_PREFIX}/campaigns/{{campaign_id}}/execute-external")
    async def execute_external_campaign(campaign_id: str):
        try:
            campaign = runtime.store.get_campaign(campaign_id)
            if campaign[
                "authorization"
            ] != "simulation_authorized" or not runtime.store.has_execution_declaration(
                campaign_id
            ):
                return _error(
                    400,
                    "execution_not_authorized",
                    "authorize the campaign before external execution",
                )
            configuration = json.loads(campaign["configuration_json"] or "{}")
            if configuration.get("execution_mode") != "external_smtp":
                return _error(
                    400,
                    "external_authorization_required",
                    "bind a server-issued external campaign authorization first",
                )
            runtime.store.clear_pause_request(campaign_id)
            started = runtime.workers.start(campaign_id)
            return {
                "campaign_id": campaign_id,
                "started": started,
                "mode": "external_smtp",
                "external_access": False,
            }
        except (KeyError, ValueError, PermissionError) as error:
            return _error(400, "external_execution_failed", str(error))

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

    def _verify_prepared_artifact(prepared: Mapping[str, Any]) -> dict[str, Any]:
        prepared = dict(prepared)
        proof_row = (
            runtime.store.get_outbound_proof(prepared["proof_id"])
            if prepared.get("proof_id")
            else None
        )
        if proof_row is None:
            raise ValueError("prepared message has no verifiable outbound proof")
        proof = make_proof(proof_row["id"], proof_row["proof"])
        verify_proof(proof, bytes(prepared["mime_bytes"]))
        if prepared["content_hash"] != proof.content_hash:
            raise ValueError("prepared message content identity does not match its proof")
        return {
            "proof_id": proof.proof_id,
            "proof_hash": proof.proof_hash,
            "content_hash": proof.content_hash,
        }

    @app.get(f"{API_PREFIX}/campaigns/{{campaign_id}}/prepared-messages/{{intent_id}}")
    async def prepared_message_inspection(campaign_id: str, intent_id: str):
        try:
            with runtime.store.connect() as connection:
                intent = connection.execute(
                    "SELECT id, normalized_email FROM delivery_intents "
                    "WHERE id=? AND campaign_id=?",
                    (intent_id, campaign_id),
                ).fetchone()
            if intent is None:
                return _error(404, "prepared_message_not_found", "prepared message was not found")
            prepared = runtime.store.get_prepared_message(intent_id)
            if prepared is None:
                return _error(404, "prepared_message_not_found", "prepared message was not found")
            message = BytesParser(policy=email_default_policy).parsebytes(prepared["mime_bytes"])
            proof_metadata = _verify_prepared_artifact(prepared)
            parts = []
            for part in message.walk():
                if part.is_multipart():
                    continue
                payload = part.get_payload(decode=True) or b""
                parts.append(
                    {
                        "content_type": part.get_content_type(),
                        "disposition": part.get_content_disposition(),
                        "filename": part.get_filename(),
                        "content_id": part.get("Content-ID"),
                        "size_bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                )
            return {
                "intent_id": intent_id,
                "recipient": intent["normalized_email"],
                "prepared_message_id": prepared["id"],
                "template_name": prepared["template_name"],
                "template_version": prepared["template_version"],
                "message_id": prepared["message_id"],
                "content_hash": prepared["content_hash"],
                "mime_size_bytes": len(prepared["mime_bytes"]),
                "headers": {
                    key: message.get(key)
                    for key in (
                        "From",
                        "To",
                        "Subject",
                        "Date",
                        "Message-ID",
                        "MIME-Version",
                        "Content-Type",
                    )
                    if message.get(key) is not None
                },
                "parts": parts,
                "proof": proof_metadata,
            }
        except ValueError as error:
            return _error(409, "prepared_message_integrity", str(error))
        except KeyError:
            return _error(404, "prepared_message_not_found", "prepared message was not found")

    @app.get(f"{API_PREFIX}/campaigns/{{campaign_id}}/prepared-messages/{{intent_id}}/export")
    async def prepared_message_export(campaign_id: str, intent_id: str):
        try:
            with runtime.store.connect() as connection:
                intent = connection.execute(
                    "SELECT id, normalized_email FROM delivery_intents "
                    "WHERE id=? AND campaign_id=?",
                    (intent_id, campaign_id),
                ).fetchone()
            if intent is None:
                return _error(404, "prepared_message_not_found", "prepared message was not found")
            prepared = runtime.store.get_prepared_message(intent_id)
            if prepared is None:
                return _error(404, "prepared_message_not_found", "prepared message was not found")
            _verify_prepared_artifact(prepared)
            return Response(
                content=prepared["mime_bytes"],
                media_type="message/rfc822",
                headers={
                    "content-disposition": (
                        f'attachment; filename="{intent["normalized_email"]}.eml"'
                    ),
                    "cache-control": "no-store",
                },
            )
        except ValueError as error:
            return _error(409, "prepared_message_integrity", str(error))
        except KeyError:
            return _error(404, "prepared_message_not_found", "prepared message was not found")

    @app.post(f"{API_PREFIX}/campaigns/{{campaign_id}}/attempts/{{attempt_id}}/reconcile")
    async def reconcile_campaign_attempt(
        campaign_id: str, attempt_id: str, payload: CampaignReconcileInput
    ):
        try:
            attempt = runtime.store.latest_attempt_for_campaign(campaign_id, attempt_id)
            runtime.store.reconcile_attempt(attempt_id, payload.decision)
            return {
                "campaign_id": campaign_id,
                "attempt_id": attempt["id"],
                "decision": payload.decision,
            }
        except KeyError:
            return _error(404, "attempt_not_found", "campaign attempt was not found")
        except ValueError as error:
            return _error(409, "reconciliation_failed", str(error))

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
        except KeyError:
            return _error(404, "campaign_not_found", "campaign was not found")

    @app.post(f"{API_PREFIX}/campaigns/{{campaign_id}}/resume")
    async def resume_campaign(campaign_id: str):
        try:
            return runtime.campaign_operations.control(campaign_id, "resume")
        except (KeyError, ValueError, PermissionError, CampaignOperationsError) as error:
            return _error(400, "resume_failed", str(error))

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
            return runtime.providers.credential_status(provider_ref, runtime.credential_resolver)
        except (KeyError, ValueError) as error:
            return _error(400, "provider_credentials_failed", str(error))

    @app.post(f"{API_PREFIX}/providers")
    async def register_provider(payload: ProviderInput):
        try:
            if payload.kind is not ProviderKind.SMTP:
                return _error(
                    410,
                    "provider_scope_retired",
                    "Mailforge is outbound-only; IMAP and POP3 provider accounts are retired",
                )
            values = payload.model_dump()
            sandbox_key = values.pop("sandbox_key")
            account_options = {
                "display_name": values.pop("display_name"),
                "sender_email": values.pop("sender_email"),
                "pacing_per_minute": values.pop("pacing_per_minute"),
                "max_concurrency": values.pop("max_concurrency"),
            }
            if sandbox_key is not None:
                if sandbox_key != "local_capture_only":
                    return _error(400, "provider_sandbox_invalid", "sandbox is not approved")
                values["metadata"] = {"sandbox": {"key": sandbox_key, "mode": "capture_only"}}
            profile = ProviderProfile(**values)
            profile.validate()
            return {
                "account": runtime.provider_operations.register(profile, **account_options),
                "registered": True,
            }
        except (KeyError, ValueError) as error:
            return _error(400, "provider_invalid", str(error))

    @app.post(f"{API_PREFIX}/providers/credentials")
    async def store_provider_credential(payload: ProviderCredentialInput):
        try:
            account = runtime.provider_operations.account(payload.provider_ref)
            if account.get("credential_ref") != payload.credential_ref:
                return _error(
                    409,
                    "credential_reference_mismatch",
                    "credential reference must match the configured SMTP account",
                )
            runtime.credential_resolver.keychain.store(payload.credential_ref, payload.secret)
            return {
                "provider_ref": payload.provider_ref,
                "credential_ref": payload.credential_ref,
                "stored": True,
                "secret_returned": False,
            }
        except (KeyError, ValueError, CredentialError) as error:
            return _error(400, "credential_store_failed", str(error))

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
                provider_ref, payload.operation, resolver=runtime.credential_resolver, grant=grant
            )
        except (KeyError, ValueError, PermissionError) as error:
            return _error(400, "provider_diagnostic_failed", str(error))

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

    @app.post(f"{API_PREFIX}/providers/{{provider_ref}}/grants")
    async def issue_provider_grant(provider_ref: str, payload: ProviderGrantInput):
        try:
            runtime.provider_operations.account(provider_ref)
            return runtime.provider_operations.issue_grant(
                provider_ref,
                ProviderOperation.CONTROLLED_TEST_SEND,
                purpose=payload.purpose,
                sender=payload.sender,
                recipient=payload.recipient,
                expires_at=payload.expires_at,
                proof_hash=payload.proof_hash,
            )
        except (KeyError, ValueError, PermissionError) as error:
            return _error(400, "provider_grant_failed", str(error))

    @app.post(f"{API_PREFIX}/providers/{{provider_ref}}/campaign-grants")
    async def issue_campaign_grant(provider_ref: str, payload: CampaignGrantInput):
        try:
            campaign = runtime.store.get_campaign(payload.campaign_id)
            if campaign["provider_account_ref"] != provider_ref:
                return _error(
                    400, "provider_campaign_mismatch", "campaign is not bound to this provider"
                )
            preview = runtime.campaign_operations.external_authorization_preview(
                payload.campaign_id
            )
            if preview["prepared_set_hash"] != payload.prepared_set_hash:
                return _error(
                    400, "campaign_identity_mismatch", "prepared campaign identity does not match"
                )
            return runtime.provider_operations.issue_campaign_grant(
                provider_ref,
                purpose=payload.purpose,
                expires_at=payload.expires_at,
                max_submissions=payload.max_submissions,
                prepared_set_hash=payload.prepared_set_hash,
            )
        except (KeyError, ValueError, PermissionError, CampaignOperationsError) as error:
            return _error(400, getattr(error, "code", "campaign_grant_failed"), str(error))

    @app.post(f"{API_PREFIX}/providers/{{provider_ref}}/test-proof")
    async def prepare_provider_test_proof(provider_ref: str, payload: ProviderProofInput):
        try:
            account = runtime.provider_operations.account(provider_ref)
            sender_email = account.get("sender_email") or payload.sender
            proof = runtime.message_studio.prepare_controlled_test(
                revision_id=payload.message_revision_id,
                sender=SenderPolicy(
                    ref=f"provider:{provider_ref}",
                    provider="synthetic",
                    email=sender_email,
                ),
                recipient=payload.recipient,
            )
            if proof["sender"].lower() != payload.sender.lower():
                return _error(
                    400, "provider_proof_sender_mismatch", "sender does not match account"
                )
            return proof
        except (KeyError, ValueError, PermissionError, MessageStudioError) as error:
            return _error(400, "provider_proof_failed", str(error))

    @app.post(f"{API_PREFIX}/providers/{{provider_ref}}/test-send")
    async def provider_test_send(provider_ref: str, payload: ProviderTestSendInput):
        try:
            grant = runtime.provider_operations.grant_for_authorization(
                provider_ref,
                ProviderOperation.CONTROLLED_TEST_SEND,
                authorization_ref=payload.authorization_ref,
                purpose=payload.purpose,
                sender=payload.sender,
                recipient=payload.recipient,
                expires_at=payload.expires_at,
                proof_hash=payload.proof_hash,
            )
            return runtime.provider_operations.controlled_test_send(
                ref=provider_ref,
                sender=payload.sender,
                recipient=payload.recipient,
                message=payload.message,
                idempotency_key=payload.idempotency_key,
                grant=grant,
                resolver=runtime.credential_resolver,
                message_revision_id=payload.message_revision_id,
                proof_id=payload.proof_id,
                proof_hash=payload.proof_hash,
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
