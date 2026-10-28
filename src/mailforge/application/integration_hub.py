"""Local Integration Hub authority for scoped events and durable webhooks.

The service intentionally owns integration identity and delivery state only.
Data acquisition, workflow execution, reporting, and secret resolution remain
owned by their existing canonical services.
"""

# SQL and compact transport contracts stay readable beside their invariants.
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import secrets
import socket
import ssl
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from mailforge.application.automation import AutomationError
from mailforge.domain.campaign import utc_now
from mailforge.infrastructure.campaign_store import CampaignStore
from mailforge.infrastructure.credentials import (
    CredentialResolver,
    EnvironmentCredentialResolver,
    validate_credential_reference,
)

CLIENT_SCOPES = {
    "READ_EXTERNAL_DATA",
    "INGEST_LOCAL_EVENT",
    "DELIVER_WEBHOOK",
    "INSPECT_INTEGRATION",
    "START_APPROVED_JOB",
}
EVENT_TYPES = {
    "external_dataset_available",
    "approved_business_record_changed",
    "approved_local_operation_completed",
    "external_application_status_reported",
}
WEBHOOK_EVENT_TYPES = {
    "contact_imported",
    "contact_added_to_list",
    "list_processing_completed",
    "trusted_inbound_reconciled",
    "inbound_rule_event",
    "inbound_draft_prepared",
    "inbound_mailbox_action_requested",
    "automation_operator_note",
    "data_connection_snapshot_completed",
    "data_connection_snapshot_failed",
}
MAX_EVENT_BYTES = 256 * 1024
MAX_WEBHOOK_BYTES = 64 * 1024
MAX_ATTEMPTS = 6
MAX_DELIVERIES_PER_POLL = 25
MAX_EVENT_SKEW = timedelta(days=90)


class IntegrationHubError(ValueError):
    def __init__(self, message: str, code: str = "integration_invalid") -> None:
        super().__init__(message)
        self.code = code


class WebhookResponse:
    def __init__(self, status: int, body: bytes = b"") -> None:
        self.status = status
        self.body = body[:2_048]


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: object) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _parse_time(value: str, code: str = "event_time_invalid") -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise IntegrationHubError("timestamp must be ISO-8601", code) from error
    if parsed.tzinfo is None:
        raise IntegrationHubError("timestamp must include a timezone", code)
    return parsed.astimezone(UTC)


def _private_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local or address.is_reserved or address.is_multicast or address.is_unspecified


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise IntegrationHubError("webhook redirects are not allowed", "redirect_refused")


def _minimal_payload(value: object, path: str = "payload") -> object:
    """Allow operational references and status/count data, excluding private content."""

    blocked = {"body", "html", "text", "message", "recipient", "recipients", "email", "path", "secret", "token", "password", "credential"}
    if isinstance(value, Mapping):
        output: dict[str, object] = {}
        for key, child in list(value.items())[:40]:
            key_text = str(key).lower()
            if any(marker in key_text for marker in blocked):
                continue
            output[str(key)] = _minimal_payload(child, f"{path}.{key}")
        return output
    if isinstance(value, list):
        return [_minimal_payload(item, f"{path}[{index}]") for index, item in enumerate(value[:20])]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value if not isinstance(value, str) or len(value) <= 500 else value[:500]
    return str(value)[:500]


class IntegrationHubService:
    """Canonical durable authority for local integrations."""

    def __init__(
        self,
        store: CampaignStore,
        *,
        resolver: CredentialResolver | None = None,
        automation=None,
        webhook_transport: Callable[[str, Mapping[str, str], bytes, float], WebhookResponse] | None = None,
        now_fn: Callable[[], datetime] | None = None,
        allow_local_fixtures: bool = False,
    ):
        self.store = store
        self.resolver = resolver or EnvironmentCredentialResolver()
        self.automation = automation
        self.webhook_transport = webhook_transport
        self.now_fn = now_fn or (lambda: datetime.now(UTC))
        self.allow_local_fixtures = allow_local_fixtures

    def _now(self) -> str:
        return self.now_fn().astimezone(UTC).isoformat()

    @staticmethod
    def _audit(connection, client_id: str, action: str, detail: Mapping[str, object] | None = None) -> None:
        connection.execute(
            "INSERT INTO integration_client_audits(id,client_id,action,detail_json,occurred_at) VALUES(?,?,?,?,?)",
            (str(uuid.uuid4()), client_id, action, _json(dict(detail or {})), utc_now()),
        )

    def _client(self, client_ref: str) -> dict[str, object]:
        with self.store.connect() as connection:
            row = connection.execute("SELECT * FROM integration_clients WHERE client_ref=?", (client_ref,)).fetchone()
        if row is None:
            raise IntegrationHubError("integration client was not found", "client_not_found")
        item = dict(row)
        item["scopes"] = json.loads(item.pop("scopes_json"))
        item.pop("token_hash", None)
        return item

    def list_clients(self, *, limit: int = 50, offset: int = 0) -> dict[str, object]:
        safe_limit = max(1, min(int(limit), 100))
        with self.store.connect() as connection:
            rows = connection.execute("SELECT * FROM integration_clients ORDER BY created_at DESC LIMIT ? OFFSET ?", (safe_limit, max(0, int(offset)))).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["scopes"] = json.loads(item.pop("scopes_json"))
            item.pop("token_hash", None)
            items.append(item)
        return {"items": items, "limit": safe_limit, "offset": max(0, int(offset))}

    def issue_client(self, name: str, scopes: list[str], *, expires_at: str) -> dict[str, object]:
        name = str(name).strip()
        if not name or len(name) > 120:
            raise IntegrationHubError("client name is required and bounded", "client_invalid")
        checked = sorted(set(str(scope) for scope in scopes))
        if not checked or any(scope not in CLIENT_SCOPES for scope in checked):
            raise IntegrationHubError("client scopes must be explicit registered capabilities", "scope_invalid")
        expiry = _parse_time(expires_at, "client_expiry_invalid")
        if expiry <= self.now_fn().astimezone(UTC) or expiry > self.now_fn().astimezone(UTC) + timedelta(days=365):
            raise IntegrationHubError("client expiry must be future and within one year", "client_expiry_invalid")
        client_id, client_ref = str(uuid.uuid4()), f"client_{secrets.token_hex(8)}"
        token = f"mf_int_{secrets.token_urlsafe(32)}"
        now = self._now()
        with self.store.transaction(immediate=True) as connection:
            try:
                connection.execute(
                    "INSERT INTO integration_clients(id,client_ref,name,scopes_json,token_hash,token_prefix,expires_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (client_id, client_ref, name, _json(checked), hashlib.sha256(token.encode()).hexdigest(), token[:16], expiry.isoformat(), now, now),
                )
            except Exception as error:
                if "UNIQUE" in str(error).upper():
                    raise IntegrationHubError("an integration client with this name already exists", "client_exists") from error
                raise
            self._audit(connection, client_id, "issued", {"scopes": checked, "expires_at": expiry.isoformat()})
        return {"client_ref": client_ref, "name": name, "scopes": checked, "expires_at": expiry.isoformat(), "token": token, "token_display": "shown_once"}

    def rotate_client(self, client_ref: str, *, expires_at: str | None = None) -> dict[str, object]:
        client = self._client(client_ref)
        expiry = _parse_time(expires_at, "client_expiry_invalid") if expires_at else _parse_time(str(client["expires_at"]), "client_expiry_invalid")
        if expiry <= self.now_fn().astimezone(UTC):
            raise IntegrationHubError("rotated client expiry must be future", "client_expiry_invalid")
        token = f"mf_int_{secrets.token_urlsafe(32)}"
        now = self._now()
        with self.store.transaction(immediate=True) as connection:
            connection.execute("UPDATE integration_clients SET token_hash=?,token_prefix=?,expires_at=?,rotated_at=?,updated_at=?,revoked_at=NULL WHERE id=?", (hashlib.sha256(token.encode()).hexdigest(), token[:16], expiry.isoformat(), now, now, client["id"]))
            self._audit(connection, str(client["id"]), "rotated", {"expires_at": expiry.isoformat()})
        return {"client_ref": client_ref, "scopes": client["scopes"], "expires_at": expiry.isoformat(), "token": token, "token_display": "shown_once"}

    def revoke_client(self, client_ref: str) -> dict[str, object]:
        client = self._client(client_ref)
        now = self._now()
        with self.store.transaction(immediate=True) as connection:
            connection.execute("UPDATE integration_clients SET revoked_at=?,updated_at=? WHERE id=? AND revoked_at IS NULL", (now, now, client["id"]))
            self._audit(connection, str(client["id"]), "revoked")
        return {"client_ref": client_ref, "revoked": True, "revoked_at": now}

    def authenticate(self, token: str, required_scope: str) -> dict[str, object]:
        if not isinstance(token, str) or not token.startswith("mf_int_") or len(token) > 200:
            raise IntegrationHubError("integration bearer token is invalid", "unauthorized")
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.store.connect() as connection:
            row = connection.execute("SELECT * FROM integration_clients WHERE token_hash=?", (digest,)).fetchone()
        if row is None or not hmac.compare_digest(str(row["token_hash"]), digest):
            raise IntegrationHubError("integration bearer token is invalid", "unauthorized")
        if row["revoked_at"]:
            raise IntegrationHubError("integration client is revoked", "client_revoked")
        if _parse_time(row["expires_at"], "client_expiry_invalid") <= self.now_fn().astimezone(UTC):
            raise IntegrationHubError("integration client is expired", "client_expired")
        scopes = set(json.loads(row["scopes_json"]))
        if required_scope not in scopes:
            raise IntegrationHubError("integration client lacks the requested scope", "scope_denied")
        with self.store.transaction(immediate=True) as connection:
            connection.execute("UPDATE integration_clients SET last_used_at=?,updated_at=? WHERE id=?", (self._now(), self._now(), row["id"]))
            self._audit(connection, str(row["id"]), "used", {"scope": required_scope})
        return {"id": row["id"], "client_ref": row["client_ref"], "scopes": sorted(scopes)}

    def event_catalog(self) -> dict[str, object]:
        return {"items": sorted(EVENT_TYPES), "trust": "external_observation", "cloud_events": "inspired_not_claimed_conformant"}

    def submit_event(self, client: Mapping[str, object], envelope: Mapping[str, object]) -> dict[str, object]:
        raw_size = len(_json(envelope).encode())
        if raw_size > MAX_EVENT_BYTES:
            raise IntegrationHubError("event exceeds the 256 KiB limit", "event_limit")
        required = {"event_id", "source", "type", "time", "schema_version", "payload"}
        if not required.issubset(envelope):
            raise IntegrationHubError("event envelope is missing a required field", "event_invalid")
        event_id, source, event_type = str(envelope["event_id"]), str(envelope["source"]), str(envelope["type"])
        if not (1 <= len(event_id) <= 160 and 1 <= len(source) <= 200) or any(char.isspace() for char in event_id):
            raise IntegrationHubError("event identity is invalid", "event_invalid")
        if event_type not in EVENT_TYPES:
            raise IntegrationHubError("event type is not registered", "event_type_unregistered")
        occurred = _parse_time(str(envelope["time"]))
        now = self.now_fn().astimezone(UTC)
        if occurred < now - MAX_EVENT_SKEW or occurred > now + timedelta(minutes=5):
            raise IntegrationHubError("event timestamp is outside the accepted window", "event_expired")
        if not isinstance(envelope["payload"], Mapping) or not isinstance(envelope.get("provenance", {}), Mapping):
            raise IntegrationHubError("event payload and provenance must be objects", "event_invalid")
        payload = _minimal_payload(envelope["payload"])
        provenance = _minimal_payload(envelope.get("provenance", {}))
        payload_hash = _hash({"type": event_type, "schema_version": str(envelope["schema_version"]), "payload": payload, "provenance": provenance})
        client_id = str(client["id"])
        with self.store.transaction(immediate=True) as connection:
            prior = connection.execute("SELECT * FROM integration_events WHERE client_id=? AND source_identity=? AND event_id=?", (client_id, source, event_id)).fetchone()
            if prior is not None:
                if prior["payload_hash"] == payload_hash:
                    self._audit(connection, client_id, "event_duplicate", {"event_id": event_id, "source": source})
                    return {"event_id": event_id, "outcome": "duplicate", "event_record_id": prior["id"], "workflow_run_ids": json.loads(prior["workflow_run_ids_json"])}
                conflict_id = str(uuid.uuid4())
                connection.execute("INSERT INTO integration_event_conflicts(id,client_id,source_identity,event_id,prior_event_id,payload_hash,payload_json,occurred_at) VALUES(?,?,?,?,?,?,?,?)", (conflict_id, client_id, source, event_id, prior["id"], payload_hash, _json(payload), self._now()))
                self._audit(connection, client_id, "event_conflict", {"event_id": event_id, "source": source})
                return {"event_id": event_id, "outcome": "conflict", "conflict_id": conflict_id, "event_record_id": prior["id"]}
            record_id = str(uuid.uuid4())
            connection.execute("INSERT INTO integration_events(id,client_id,source_identity,event_id,event_type,schema_version,occurred_at,received_at,payload_json,provenance_json,auth_result,trust_class,payload_hash,outcome) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (record_id, client_id, source, event_id, event_type, str(envelope["schema_version"]), occurred.isoformat(), self._now(), _json(payload), _json(provenance), "authenticated_client", "external_observation", payload_hash, "accepted"))
            self._audit(connection, client_id, "event_accepted", {"event_id": event_id, "type": event_type})
        workflows = self._enroll_automation(record_id, event_id, source, event_type, payload, provenance, occurred.isoformat())
        with self.store.transaction(immediate=True) as connection:
            connection.execute("UPDATE integration_events SET workflow_run_ids_json=? WHERE id=?", (_json(workflows), record_id))
        return {"event_id": event_id, "outcome": "accepted", "event_record_id": record_id, "workflow_run_ids": workflows, "trust_class": "external_observation"}

    def _enroll_automation(self, record_id: str, event_id: str, source: str, event_type: str, payload: object, provenance: object, occurred_at: str) -> list[str]:
        if self.automation is None:
            return []
        run_ids: list[str] = []
        for workflow in self.automation.list_workflows(limit=250, offset=0)["items"]:
            if workflow["status"] != "active":
                continue
            revision = self.automation.get_revision(workflow["active_revision_id"])
            if not any(node.get("kind") == "TRIGGER" and node.get("config", {}).get("trigger_kind") == "integration_event" and node.get("config", {}).get("event_type") == event_type for node in revision["graph"]["nodes"]):
                continue
            try:
                result = self.automation.enroll(workflow["id"], event_kind="integration_event", source_identity=f"integration:{source}:{event_id}", subject_key=f"integration-event:{record_id}", payload={"event_id": event_id, "event_type": event_type, "payload": payload}, provenance={"trust_class": "external_observation", "source": source, **(provenance if isinstance(provenance, dict) else {})}, correlation_id=f"integration:{record_id}", occurred_at=occurred_at, actor="integration-hub")
                if result.get("run_id"):
                    run_ids.append(str(result["run_id"]))
            except AutomationError as error:
                if error.code not in {"trigger_not_configured", "trigger_mismatch", "workflow_state"}:
                    raise
        return run_ids

    def list_events(self, *, limit: int = 50, offset: int = 0) -> dict[str, object]:
        safe_limit = max(1, min(int(limit), 100))
        with self.store.connect() as connection:
            rows = connection.execute("SELECT id,client_id,source_identity,event_id,event_type,schema_version,occurred_at,received_at,auth_result,trust_class,payload_hash,outcome,workflow_run_ids_json FROM integration_events ORDER BY received_at DESC,id DESC LIMIT ? OFFSET ?", (safe_limit, max(0, int(offset)))).fetchall()
        return {"items": [{**dict(row), "workflow_run_ids": json.loads(row["workflow_run_ids_json"])} for row in rows], "limit": safe_limit, "offset": max(0, int(offset))}

    def _validate_destination(self, endpoint: str, *, local_fixture: bool) -> None:
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"https", "http"} or parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname:
            raise IntegrationHubError("webhook endpoint must be a fixed URL without credentials or query", "destination_invalid")
        local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme != "https" and not (local_fixture and local and self.allow_local_fixtures):
            raise IntegrationHubError("webhook destinations require HTTPS", "tls_required")
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port, type=socket.SOCK_STREAM)}
        except OSError as error:
            raise IntegrationHubError("webhook destination DNS resolution failed", "dns_failed") from error
        if not (local_fixture and local) and any(_private_address(address) for address in addresses):
            raise IntegrationHubError("webhook destination resolves to a private address", "private_destination")

    def create_subscription(self, name: str, endpoint_url: str, event_types: list[str], *, secret_ref: str, payload_version: str = "1", scope: Mapping[str, object] | None = None, enabled: bool = False, local_fixture: bool = False) -> dict[str, object]:
        name = str(name).strip()
        selected = sorted(set(str(item) for item in event_types))
        if not name or len(name) > 120 or not selected or any(item not in WEBHOOK_EVENT_TYPES for item in selected):
            raise IntegrationHubError("webhook name and registered event types are required", "subscription_invalid")
        validate_credential_reference(secret_ref)
        self._validate_destination(endpoint_url, local_fixture=local_fixture)
        item_id, ref, now = str(uuid.uuid4()), f"webhook_{secrets.token_hex(8)}", self._now()
        with self.store.transaction(immediate=True) as connection:
            try:
                connection.execute("INSERT INTO integration_webhook_subscriptions(id,subscription_ref,name,endpoint_url,event_types_json,payload_version,secret_ref,scope_json,enabled,local_fixture,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (item_id, ref, name, endpoint_url, _json(selected), payload_version, secret_ref, _json(dict(scope or {})), int(enabled), int(local_fixture), now, now))
            except Exception as error:
                if "UNIQUE" in str(error).upper():
                    raise IntegrationHubError("a webhook with this name already exists", "subscription_exists") from error
                raise
        return self.subscription(ref)

    def subscription(self, subscription_ref: str) -> dict[str, object]:
        with self.store.connect() as connection:
            row = connection.execute("SELECT * FROM integration_webhook_subscriptions WHERE subscription_ref=?", (subscription_ref,)).fetchone()
        if row is None:
            raise IntegrationHubError("webhook subscription was not found", "subscription_not_found")
        item = dict(row)
        item["event_types"] = json.loads(item.pop("event_types_json"))
        item["scope"] = json.loads(item.pop("scope_json"))
        item.pop("secret_ref", None)
        return item

    def list_subscriptions(self, *, limit: int = 50, offset: int = 0) -> dict[str, object]:
        safe_limit = max(1, min(int(limit), 100))
        with self.store.connect() as connection:
            rows = connection.execute("SELECT subscription_ref,name,endpoint_url,event_types_json,payload_version,scope_json,enabled,local_fixture,created_at,updated_at,last_success_at,last_error FROM integration_webhook_subscriptions ORDER BY updated_at DESC LIMIT ? OFFSET ?", (safe_limit, max(0, int(offset)))).fetchall()
        return {"items": [{**dict(row), "event_types": json.loads(row["event_types_json"]), "scope": json.loads(row["scope_json"])} for row in rows], "limit": safe_limit, "offset": max(0, int(offset))}

    def set_subscription(self, subscription_ref: str, *, enabled: bool) -> dict[str, object]:
        item = self.subscription(subscription_ref)
        now = self._now()
        with self.store.transaction(immediate=True) as connection:
            connection.execute("UPDATE integration_webhook_subscriptions SET enabled=?,updated_at=? WHERE subscription_ref=?", (int(enabled), now, subscription_ref))
            if not enabled:
                connection.execute("UPDATE integration_webhook_deliveries SET status='suspended',updated_at=? WHERE subscription_id=(SELECT id FROM integration_webhook_subscriptions WHERE subscription_ref=?) AND status IN ('pending','retrying')", (now, subscription_ref))
            else:
                connection.execute("UPDATE integration_webhook_deliveries SET status='pending',next_due_at=?,updated_at=? WHERE subscription_id=(SELECT id FROM integration_webhook_subscriptions WHERE subscription_ref=?) AND status='suspended' AND completed_at IS NULL", (now, now, subscription_ref))
        return {**item, "enabled": enabled}

    def _subscription_row(self, subscription_ref: str):
        with self.store.connect() as connection:
            row = connection.execute("SELECT * FROM integration_webhook_subscriptions WHERE subscription_ref=?", (subscription_ref,)).fetchone()
        if row is None:
            raise IntegrationHubError("webhook subscription was not found", "subscription_not_found")
        return row

    def _payload_for_event(self, row) -> dict[str, object]:
        event = dict(row)
        return {"id": f"operational:{event['id']}", "type": event["event_type"], "source": "mailforge.local", "time": event["occurred_at"], "schema_version": "1", "evidence_class": "durable_local_observation", "data": _minimal_payload(json.loads(event["payload_json"] or "{}"))}

    def preview_subscription(self, subscription_ref: str, *, event_type: str | None = None) -> dict[str, object]:
        item = self._subscription_row(subscription_ref)
        selected = json.loads(item["event_types_json"])
        chosen = event_type or selected[0]
        if chosen not in selected:
            raise IntegrationHubError("event type is not selected for this subscription", "event_type_invalid")
        with self.store.connect() as connection:
            event = connection.execute("SELECT * FROM operational_events WHERE event_type=? ORDER BY id DESC LIMIT 1", (chosen,)).fetchone()
        if event is None:
            payload = {"id": "preview:event", "type": chosen, "source": "mailforge.local", "time": self._now(), "schema_version": item["payload_version"], "evidence_class": "preview_not_observed", "data": {"reference": "synthetic-preview"}}
        else:
            payload = self._payload_for_event(event)
        encoded = _json(payload).encode()
        if len(encoded) > MAX_WEBHOOK_BYTES:
            raise IntegrationHubError("webhook payload exceeds the 64 KiB limit", "payload_limit")
        return {"subscription_ref": subscription_ref, "event_type": chosen, "payload": payload, "bytes": len(encoded), "privacy": "minimal_operational_references"}

    def plan_deliveries(self, *, limit: int = MAX_DELIVERIES_PER_POLL) -> int:
        with self.store.transaction(immediate=True) as connection:
            subscriptions = connection.execute("SELECT * FROM integration_webhook_subscriptions WHERE enabled=1").fetchall()
            events = connection.execute("SELECT * FROM operational_events ORDER BY id DESC LIMIT 500").fetchall()
            planned = 0
            for subscription in subscriptions:
                selected = set(json.loads(subscription["event_types_json"]))
                for event in events:
                    if event["event_type"] not in selected:
                        continue
                    payload = self._payload_for_event(event)
                    if len(_json(payload).encode()) > MAX_WEBHOOK_BYTES:
                        continue
                    try:
                        connection.execute("INSERT INTO integration_webhook_deliveries(id,subscription_id,operational_event_id,event_type,event_id,payload_json,status,next_due_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), subscription["id"], event["id"], event["event_type"], payload["id"], _json(payload), "pending", self._now(), self._now(), self._now()))
                        planned += 1
                    except Exception as error:
                        if "UNIQUE" not in str(error).upper():
                            raise
                    if planned >= limit:
                        return planned
            return planned

    def _deliver(self, delivery) -> dict[str, object]:
        with self.store.connect() as connection:
            subscription = connection.execute("SELECT * FROM integration_webhook_subscriptions WHERE id=?", (delivery["subscription_id"],)).fetchone()
        if subscription is None or not subscription["enabled"]:
            return {"status": "suspended", "delivery_id": delivery["id"]}
        payload = delivery["payload_json"].encode()
        timestamp = str(int(self.now_fn().timestamp()))
        secret = self.resolver.resolve(subscription["secret_ref"]).secret.encode()
        signature = hmac.new(secret, f"{timestamp}.".encode() + payload, hashlib.sha256).hexdigest()
        headers = {"content-type": "application/json", "x-mailforge-version": "1", "x-mailforge-event-id": delivery["event_id"], "x-mailforge-delivery-id": delivery["id"], "x-mailforge-timestamp": timestamp, "x-mailforge-signature": f"v1={signature}"}
        try:
            if self.webhook_transport is not None:
                response = self.webhook_transport(subscription["endpoint_url"], headers, payload, 10.0)
            else:
                self._validate_destination(subscription["endpoint_url"], local_fixture=bool(subscription["local_fixture"]))
                request = Request(subscription["endpoint_url"], data=payload, method="POST", headers=headers)
                with build_opener(
                    _NoRedirect(), HTTPSHandler(context=ssl.create_default_context())
                ).open(request, timeout=10) as result:
                    response = WebhookResponse(result.status, result.read(2_048))
            status = int(response.status)
            if 200 <= status < 300:
                return {"status": "completed", "http_status": status, "headers": headers}
            if status in {408, 425, 429} or status >= 500:
                return {"status": "retry", "http_status": status, "error": "destination requested a bounded retry"}
            return {"status": "failed", "http_status": status, "error": "destination rejected the webhook"}
        except (IntegrationHubError, HTTPError, URLError, TimeoutError, OSError) as error:
            return {"status": "ambiguous", "error": str(error)[:300]}

    def process_due_deliveries(self, *, limit: int = MAX_DELIVERIES_PER_POLL) -> list[dict[str, object]]:
        self.plan_deliveries(limit=limit)
        with self.store.connect() as connection:
            rows = connection.execute("SELECT * FROM integration_webhook_deliveries WHERE status IN ('pending','retrying') AND next_due_at<=? ORDER BY next_due_at,id LIMIT ?", (self._now(), max(1, min(int(limit), MAX_DELIVERIES_PER_POLL)))).fetchall()
        results: list[dict[str, object]] = []
        for row in rows:
            attempt = int(row["attempt_number"]) + 1
            outcome = self._deliver(row)
            now = self._now()
            if outcome["status"] == "completed":
                status, next_due, completed = "completed", now, now
            elif attempt >= MAX_ATTEMPTS:
                status = "ambiguous" if outcome["status"] == "ambiguous" else "failed"
                next_due, completed = now, None
            elif outcome["status"] == "failed":
                status, next_due, completed = "failed", now, None
            else:
                status, next_due, completed = "retrying", (self.now_fn() + timedelta(seconds=min(3_600, 2 ** attempt * 5))).astimezone(UTC).isoformat(), None
            with self.store.transaction(immediate=True) as connection:
                connection.execute("UPDATE integration_webhook_deliveries SET status=?,attempt_number=?,next_due_at=?,last_http_status=?,last_error=?,ambiguous=?,updated_at=?,completed_at=? WHERE id=? AND status IN ('pending','retrying')", (status, attempt, next_due, outcome.get("http_status"), outcome.get("error"), int(outcome["status"] == "ambiguous"), now, completed, row["id"]))
                if status == "completed":
                    connection.execute("UPDATE integration_webhook_subscriptions SET last_success_at=?,last_error=NULL WHERE id=?", (now, row["subscription_id"]))
                elif outcome.get("error"):
                    connection.execute("UPDATE integration_webhook_subscriptions SET last_error=?,updated_at=? WHERE id=?", (str(outcome["error"])[:300], now, row["subscription_id"]))
            results.append({"delivery_id": row["id"], "attempt": attempt, **outcome, "state": status})
        return results

    def deliveries(self, subscription_ref: str | None = None, *, limit: int = 100, offset: int = 0) -> dict[str, object]:
        safe_limit = max(1, min(int(limit), 200))
        clause, params = (" WHERE subscription_id=(SELECT id FROM integration_webhook_subscriptions WHERE subscription_ref=?)", [subscription_ref]) if subscription_ref else ("", [])
        with self.store.connect() as connection:
            rows = connection.execute("SELECT id,subscription_id,event_type,event_id,status,attempt_number,next_due_at,last_http_status,last_error,ambiguous,created_at,updated_at,completed_at FROM integration_webhook_deliveries" + clause + " ORDER BY created_at DESC LIMIT ? OFFSET ?", (*params, safe_limit, max(0, int(offset)))).fetchall()
        return {"items": [dict(row) for row in rows], "limit": safe_limit, "offset": max(0, int(offset))}

    def diagnostics(self) -> dict[str, object]:
        with self.store.connect() as connection:
            counts = connection.execute("SELECT outcome,COUNT(*) AS count FROM integration_events GROUP BY outcome").fetchall()
            audit_counts = connection.execute(
                "SELECT action,COUNT(*) AS count FROM integration_client_audits "
                "WHERE action IN ('event_duplicate','event_conflict') GROUP BY action"
            ).fetchall()
            deliveries = connection.execute("SELECT status,COUNT(*) AS count FROM integration_webhook_deliveries GROUP BY status").fetchall()
            sources = connection.execute("SELECT source_type,COUNT(*) AS count FROM data_connections GROUP BY source_type").fetchall()
        events = {row["outcome"]: row["count"] for row in counts}
        events["duplicate"] = next((row["count"] for row in audit_counts if row["action"] == "event_duplicate"), 0)
        events["conflict"] = next((row["count"] for row in audit_counts if row["action"] == "event_conflict"), 0)
        return {"clients": self.list_clients(limit=100)["items"], "sources": [dict(row) for row in sources], "events": events, "deliveries": {row["status"]: row["count"] for row in deliveries}, "evidence_class": "durable_local_observation"}

    @staticmethod
    def verify_signature(secret: str, body: bytes, *, timestamp: str, signature: str, now: datetime | None = None, max_age_seconds: int = 300) -> bool:
        try:
            age = abs((now or datetime.now(UTC)).timestamp() - float(timestamp))
        except (TypeError, ValueError):
            return False
        if age > max_age_seconds or not signature.startswith("v1="):
            return False
        expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature.removeprefix("v1="), expected)
