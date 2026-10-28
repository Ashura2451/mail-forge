"""Search, rules, extraction, and draft preparation for inbound evidence."""

# SQL statements and compact operator projections remain readable as fragments.
# ruff: noqa: E501

from __future__ import annotations

import csv
import io
import json
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from mailforge.application.message_studio import MessageStudioService
from mailforge.domain.inbound import ActionStatus, ActionType
from mailforge.infrastructure.campaign_store import CampaignStore, _assert_no_secrets

MAX_LIMIT = 200
RULE_FIELDS = {
    "source_ref",
    "folder",
    "sender",
    "recipient",
    "subject",
    "classification",
    "correlation",
}
RULE_OPS = {"equals", "contains", "starts_with", "domain", "in"}
RULE_ACTIONS = {"record_event", "extract", "review", "prepare_reply", "prepare_forward"}
EXTRACT_FIELDS = {
    "sender",
    "recipients",
    "reply_to",
    "message_id",
    "in_reply_to",
    "references",
    "date",
    "subject",
}


class InboundOperationsError(ValueError):
    def __init__(self, message: str, code: str = "inbound_operation_invalid"):
        super().__init__(message)
        self.code = code


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _decode(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for key, value in list(result.items()):
        if key.endswith("_json") and isinstance(value, str):
            try:
                result[key.removesuffix("_json")] = json.loads(value)
            except json.JSONDecodeError:
                pass
    return result


def _safe_export(value: Any) -> Any:
    if isinstance(value, str) and value[:1] in {"=", "+", "-", "@"}:
        return "'" + value
    return value


class InboundOperations:
    """Canonical workbench facade; acquisition remains InboundService-owned."""

    def __init__(self, store: CampaignStore, *, message_studio: MessageStudioService | None = None):
        self.store = store
        self.message_studio = message_studio or MessageStudioService(store)

    @staticmethod
    def validate_rule(definition: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(definition, Mapping) or definition.get("version") != 1:
            raise InboundOperationsError("rule version 1 is required")
        conditions = definition.get("conditions", [])
        actions = definition.get("actions", [])
        if not isinstance(conditions, list) or len(conditions) > 20:
            raise InboundOperationsError("rules allow at most 20 conditions", "rule_limit")
        if not isinstance(actions, list) or not 1 <= len(actions) <= 10:
            raise InboundOperationsError("rules need between 1 and 10 actions", "rule_actions")
        for condition in conditions:
            if not isinstance(condition, Mapping):
                raise InboundOperationsError("rule conditions must be objects")
            if condition.get("field") not in RULE_FIELDS:
                raise InboundOperationsError(f"unsupported rule field: {condition.get('field')}")
            if condition.get("op") not in RULE_OPS:
                raise InboundOperationsError(f"unsupported rule operator: {condition.get('op')}")
            if not isinstance(condition.get("value"), (str, int, float, list, tuple)):
                raise InboundOperationsError("rule values must be scalar or bounded arrays")
            if isinstance(condition.get("value"), (list, tuple)) and len(condition["value"]) > 50:
                raise InboundOperationsError("rule value arrays are bounded", "rule_limit")
        for action in actions:
            if not isinstance(action, Mapping) or action.get("type") not in RULE_ACTIONS:
                raise InboundOperationsError(f"unsupported rule action: {action}")
            if action["type"] == "extract":
                fields = action.get("fields", [])
                if not isinstance(fields, list) or not fields or len(fields) > len(EXTRACT_FIELDS):
                    raise InboundOperationsError("extract actions require bounded fields")
                if any(field not in EXTRACT_FIELDS for field in fields):
                    raise InboundOperationsError("extract action contains an unsupported field")
            if action["type"] in {"prepare_reply", "prepare_forward"}:
                body = action.get("body", "")
                if not isinstance(body, str) or len(body) > 20_000:
                    raise InboundOperationsError("draft body is invalid or too long")
        clean = json.loads(_json(definition))
        _assert_no_secrets(clean, "inbound_rule")
        if len(_json(clean).encode()) > 64 * 1024:
            raise InboundOperationsError("rule exceeds the 64 KiB limit", "rule_limit")
        return clean

    def create_rule(
        self, name: str, definition: Mapping[str, Any], *, priority: int = 0
    ) -> dict[str, Any]:
        if not name.strip() or len(name) > 160 or not -100_000 <= priority <= 100_000:
            raise InboundOperationsError("rule name or priority is invalid")
        clean = self.validate_rule(definition)
        rule_id = str(uuid.uuid4())
        now = _now()
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO inbound_rules(id,name,status,priority,definition_json,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (rule_id, name.strip(), "paused", priority, _json(clean), 1, now, now),
            )
        return self.get_rule(rule_id)

    def get_rule(self, rule_id: str) -> dict[str, Any]:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM inbound_rules WHERE id = ?", (rule_id,)
            ).fetchone()
        if row is None:
            raise InboundOperationsError("inbound rule was not found", "rule_not_found")
        return _decode(row)

    def list_rules(self) -> list[dict[str, Any]]:
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM inbound_rules WHERE status != 'archived' ORDER BY priority DESC, id"
            ).fetchall()
        return [_decode(row) for row in rows]

    def set_rule_status(self, rule_id: str, status: str) -> dict[str, Any]:
        if status not in {"active", "paused", "archived"}:
            raise InboundOperationsError("invalid rule status")
        with self.store.transaction(immediate=True) as connection:
            if (
                connection.execute(
                    "UPDATE inbound_rules SET status = ?, updated_at = ? WHERE id = ?",
                    (status, _now(), rule_id),
                ).rowcount
                != 1
            ):
                raise InboundOperationsError("inbound rule was not found", "rule_not_found")
        return self.get_rule(rule_id)

    def _message_context(self, message_id: str) -> dict[str, Any]:
        row = self.store.get_inbound_message(message_id)
        with self.store.connect() as connection:
            classification = connection.execute(
                "SELECT kind FROM inbound_classifications WHERE message_id = ? ORDER BY occurred_at DESC LIMIT 1",
                (message_id,),
            ).fetchone()
            correlation = connection.execute(
                "SELECT match_kind FROM inbound_correlations WHERE message_id = ? ORDER BY created_at LIMIT 1",
                (message_id,),
            ).fetchone()
        recipients = json.loads(row["recipients_json"] or "[]")
        return {
            "source_ref": row["source_ref"],
            "folder": row["folder"] or "",
            "sender": (row["sender"] or "").lower(),
            "recipient": [str(value).lower() for value in recipients],
            "subject": row["subject"] or "",
            "classification": classification["kind"] if classification else "",
            "correlation": correlation["match_kind"] if correlation else "unmatched",
        }

    @staticmethod
    def _matches(condition: Mapping[str, Any], context: Mapping[str, Any]) -> bool:
        value = context.get(condition["field"], "")
        expected = condition["value"]
        values = value if isinstance(value, list) else [value]
        op = condition["op"]
        if op == "equals":
            return any(str(item).lower() == str(expected).lower() for item in values)
        if op == "contains":
            return any(str(expected).lower() in str(item).lower() for item in values)
        if op == "starts_with":
            return any(str(item).lower().startswith(str(expected).lower()) for item in values)
        if op == "domain":
            return any(
                str(item).lower().rsplit("@", 1)[-1] == str(expected).lower() for item in values
            )
        if op == "in":
            return any(str(item).lower() in {str(x).lower() for x in expected} for item in values)
        return False

    def preview_rule(self, rule_id: str, message_id: str) -> dict[str, Any]:
        rule = self.get_rule(rule_id)
        context = self._message_context(message_id)
        matched = all(
            self._matches(item, context) for item in rule["definition"].get("conditions", [])
        )
        return {
            "rule_id": rule_id,
            "message_id": message_id,
            "matched": matched,
            "context": context,
        }

    def extract_message(
        self, message_id: str, fields: list[str] | None = None
    ) -> list[dict[str, Any]]:
        row = self.store.get_inbound_message(message_id)
        selected = fields or sorted(EXTRACT_FIELDS)
        if any(field not in EXTRACT_FIELDS for field in selected):
            raise InboundOperationsError("unsupported extraction field")
        headers = json.loads(row["headers_json"] or "{}")
        values: dict[str, Any] = {
            "sender": row["sender"],
            "recipients": json.loads(row["recipients_json"] or "[]"),
            "reply_to": (headers.get("reply-to") or [None])[0],
            "message_id": row["message_id"],
            "in_reply_to": row["in_reply_to"],
            "references": json.loads(row["references_json"] or "[]"),
            "date": (headers.get("date") or [None])[0],
            "subject": row["subject"],
        }
        now = _now()
        with self.store.transaction(immediate=True) as connection:
            result = []
            for field in selected:
                value = values[field]
                value_json = _json(value)
                item_id = str(
                    uuid.uuid5(uuid.NAMESPACE_URL, f"inbound-extract:{message_id}:{field}")
                )
                connection.execute(
                    "INSERT INTO inbound_extracted_values(id,message_id,field_key,value_json,source_path,provenance_json,created_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(message_id,field_key) DO UPDATE SET value_json=excluded.value_json, provenance_json=excluded.provenance_json, created_at=excluded.created_at",
                    (
                        item_id,
                        message_id,
                        field,
                        value_json,
                        f"headers.{field}",
                        _json({"message_id": message_id}),
                        now,
                    ),
                )
                result.append(
                    {
                        "id": item_id,
                        "message_id": message_id,
                        "field_key": field,
                        "value": value,
                        "source_path": f"headers.{field}",
                    }
                )
        return result

    def export(self, *, message_id: str | None = None, fmt: str = "json") -> tuple[str, str]:
        query = "SELECT * FROM inbound_extracted_values"
        params: tuple[Any, ...] = ()
        if message_id:
            query += " WHERE message_id = ?"
            params = (message_id,)
        query += " ORDER BY message_id, field_key"
        with self.store.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        values = [
            {
                "message_id": row["message_id"],
                "field": row["field_key"],
                "value": json.loads(row["value_json"]),
            }
            for row in rows
        ]
        if fmt == "json":
            return "application/json", json.dumps(values, ensure_ascii=False, indent=2)
        if fmt != "csv":
            raise InboundOperationsError("format must be json or csv")
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=["message_id", "field", "value"])
        writer.writeheader()
        for value in values:
            writer.writerow(
                {
                    key: _safe_export(
                        json.dumps(item, ensure_ascii=False)
                        if isinstance(item, (list, dict))
                        else item
                    )
                    for key, item in value.items()
                }
            )
        return "text/csv", output.getvalue()

    def apply_active_rules(self, message_id: str) -> list[dict[str, Any]]:
        context = self._message_context(message_id)
        results = []
        for rule in self.list_rules():
            if rule["status"] != "active":
                continue
            key = f"{rule['id']}:{message_id}:{rule['revision']}"
            with self.store.connect() as connection:
                existing = connection.execute(
                    "SELECT * FROM inbound_rule_runs WHERE idempotency_key = ?", (key,)
                ).fetchone()
                if existing:
                    results.append(_decode(existing))
                    continue
            matched = all(
                self._matches(item, context) for item in rule["definition"].get("conditions", [])
            )
            status = "matched" if matched else "skipped"
            actions_done: list[dict[str, Any]] = []
            if matched:
                for action in rule["definition"]["actions"]:
                    kind = action["type"]
                    if kind == "extract":
                        actions_done.append(
                            {
                                "type": kind,
                                "count": len(self.extract_message(message_id, action["fields"])),
                            }
                        )
                    elif kind == "record_event":
                        with self.store.transaction(immediate=True) as connection:
                            self.store.record_event(
                                connection,
                                "inbound_message",
                                message_id,
                                "inbound_rule_event",
                                {"rule_id": rule["id"], "label": action.get("label", rule["name"])},
                            )
                        actions_done.append({"type": kind})
                    elif kind == "review":
                        action_id, _ = self.store.propose_inbound_action(
                            message_id,
                            ActionType.REVIEW_REQUIRED,
                            context.get("sender") or None,
                            action.get("reason", rule["name"]),
                            status=ActionStatus.REVIEW_REQUIRED,
                        )
                        self.store.create_inbound_review(
                            message_id, action_id, action.get("reason", rule["name"])
                        )
                        actions_done.append({"type": kind, "action_id": action_id})
                    elif kind in {"prepare_reply", "prepare_forward"}:
                        if context["classification"] in {
                            "automatic_reply",
                            "delivery_status",
                            "complaint",
                        }:
                            actions_done.append({"type": kind, "status": "loop_prevented"})
                        else:
                            draft = self.prepare_draft(
                                message_id,
                                "reply" if kind == "prepare_reply" else "forward",
                                body=action.get("body", ""),
                            )
                            actions_done.append({"type": kind, "draft_id": draft["id"]})
                status = "applied"
            run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"inbound-rule-run:{key}"))
            with self.store.transaction(immediate=True) as connection:
                connection.execute(
                    "INSERT INTO inbound_rule_runs(id,rule_id,message_id,idempotency_key,status,actions_json,occurred_at) VALUES(?,?,?,?,?,?,?)",
                    (run_id, rule["id"], message_id, key, status, _json(actions_done), _now()),
                )
            results.append(
                {
                    "id": run_id,
                    "rule_id": rule["id"],
                    "message_id": message_id,
                    "status": status,
                    "actions": actions_done,
                }
            )
        return results

    def prepare_draft(
        self, message_id: str, kind: str, *, body: str = "", account_ref: str | None = None
    ) -> dict[str, Any]:
        if kind not in {"reply", "forward"}:
            raise InboundOperationsError("draft kind must be reply or forward")
        row = self.store.get_inbound_message(message_id)
        with self.store.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM inbound_drafts WHERE message_id = ? AND kind = ?", (message_id, kind)
            ).fetchone()
        if existing:
            return self.draft_detail(existing["id"])
        prefix = "Re: " if kind == "reply" else "Fwd: "
        subject = row["subject"] or "No subject"
        if not subject.lower().startswith(prefix.lower()):
            subject = prefix + subject
        source_text = (
            (row["text_preview"] or row["html_preview"] or "").replace("<", "‹").replace(">", "›")
        )
        content = body or (f"\n\n— Original message —\n{source_text}" if kind == "forward" else "")
        document = MessageStudioService.blank_document(f"Inbound {kind}: {subject}")
        document["subject"] = subject
        document["blocks"] = [{"type": "text", "text": content or "Write your reply here."}]
        created = self.message_studio.create_document(document["name"], document=document)
        revision_id = created["current_draft_revision_id"]
        draft_id = str(uuid.uuid4())
        now = _now()
        provenance = {
            "message_id": message_id,
            "message_id_header": row["message_id"],
            "in_reply_to": row["in_reply_to"],
            "references": json.loads(row["references_json"] or "[]"),
        }
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO inbound_drafts(id,message_id,kind,document_id,revision_id,account_ref,subject,provenance_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    draft_id,
                    message_id,
                    kind,
                    created["id"],
                    revision_id,
                    account_ref,
                    subject,
                    _json(provenance),
                    "draft",
                    now,
                    now,
                ),
            )
            self.store.record_event(
                connection,
                "inbound_message",
                message_id,
                "inbound_draft_prepared",
                {"draft_id": draft_id, "kind": kind},
            )
        return self.draft_detail(draft_id)

    def draft_detail(self, draft_id: str) -> dict[str, Any]:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM inbound_drafts WHERE id = ?", (draft_id,)
            ).fetchone()
        if row is None:
            raise InboundOperationsError("inbound draft was not found", "draft_not_found")
        return _decode(row)

    def add_extracted_contact(
        self, message_id: str, *, field: str = "sender", list_id: str
    ) -> dict[str, Any]:
        if field not in {"sender", "reply_to"}:
            raise InboundOperationsError("only sender or reply_to may be added as a contact")
        values = self.extract_message(message_id, [field])
        value = values[0]["value"]
        if not isinstance(value, str) or "@" not in value:
            raise InboundOperationsError("extracted field is not one email address")
        contact_id = self.store.upsert_contact(
            value,
            source_file=f"inbound:{message_id}",
            source_record_position=0,
            original_input=value,
            metadata={"extracted_from_message_id": message_id, "extraction_field": field},
        )
        self.store.add_to_list(list_id, contact_id)
        with self.store.transaction(immediate=True) as connection:
            self.store.record_event(
                connection,
                "contact",
                contact_id,
                "inbound_contact_added_to_list",
                {"message_id": message_id, "list_id": list_id, "field": field},
            )
        return {
            "contact_id": contact_id,
            "list_id": list_id,
            "message_id": message_id,
            "field": field,
            "consent_established": False,
        }

    def request_mailbox_action(
        self,
        source_ref: str,
        *,
        action: str,
        authorization_ref: str,
        idempotency_key: str,
        message_id: str | None = None,
        target_folder: str | None = None,
    ) -> dict[str, Any]:
        if (
            action not in {"mark_read", "move", "copy"}
            or not authorization_ref.strip()
            or not idempotency_key.strip()
        ):
            raise InboundOperationsError(
                "mailbox action, authorization, and idempotency key are required"
            )
        if action in {"move", "copy"} and not target_folder:
            raise InboundOperationsError("move and copy require target_folder")
        _assert_no_secrets(
            {"authorization_ref": authorization_ref, "target_folder": target_folder or ""},
            "mailbox_action",
        )
        action_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"mailbox-action:{idempotency_key}"))
        now = _now()
        with self.store.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM inbound_mailbox_actions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                return _decode(existing)
            if (
                connection.execute(
                    "SELECT 1 FROM inbound_sources WHERE ref = ?", (source_ref,)
                ).fetchone()
                is None
            ):
                raise InboundOperationsError("inbound source was not found", "source_not_found")
            connection.execute(
                "INSERT INTO inbound_mailbox_actions(id,source_ref,message_id,action,target_folder,authorization_ref,idempotency_key,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    action_id,
                    source_ref,
                    message_id,
                    action,
                    target_folder,
                    authorization_ref,
                    idempotency_key,
                    "requested",
                    now,
                    now,
                ),
            )
            self.store.record_event(
                connection,
                "inbound_source",
                source_ref,
                "inbound_mailbox_action_requested",
                {"action_id": action_id, "action": action, "authorization_ref": authorization_ref},
            )
        return self.mailbox_action(action_id)

    def mailbox_action(self, action_id: str) -> dict[str, Any]:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM inbound_mailbox_actions WHERE id = ?", (action_id,)
            ).fetchone()
        if row is None:
            raise InboundOperationsError("mailbox action was not found", "action_not_found")
        return _decode(row)

    def search(
        self,
        *,
        q: str | None = None,
        sender: str | None = None,
        recipient: str | None = None,
        subject: str | None = None,
        classification: str | None = None,
        source_ref: str | None = None,
        processed: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        limit, offset = max(1, min(limit, MAX_LIMIT)), max(0, offset)
        clauses, params = [], []
        if q:
            term = f"%{q[:200]}%"
            clauses.append(
                "(lower(COALESCE(m.sender,'')) LIKE lower(?) OR lower(COALESCE(m.subject,'')) LIKE lower(?) OR lower(COALESCE(m.text_preview,'')) LIKE lower(?))"
            )
            params.extend((term, term, term))
        for column, value in (
            ("m.sender", sender),
            ("m.subject", subject),
            ("m.source_ref", source_ref),
        ):
            if value:
                clauses.append(f"lower(COALESCE({column},'')) LIKE lower(?)")
                params.append(f"%{value[:200]}%")
        if recipient:
            clauses.append("lower(COALESCE(m.recipients_json,'')) LIKE lower(?)")
            params.append(f"%{recipient[:200]}%")
        if classification:
            clauses.append("c.kind = ?")
            params.append(classification)
        if processed is not None:
            clauses.append("m.processed_at IS " + ("NOT NULL" if processed else "NULL"))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        query = f"SELECT m.*, c.kind AS classification, c.reasons_json AS classification_reasons, (SELECT COUNT(*) FROM inbound_reviews r WHERE r.message_id = m.id AND r.status = 'pending') AS pending_reviews FROM inbound_messages m LEFT JOIN inbound_classifications c ON c.message_id = m.id {where} ORDER BY m.acquired_at DESC, m.id LIMIT ? OFFSET ?"
        with self.store.connect() as connection:
            rows = connection.execute(query, (*params, limit, offset)).fetchall()
            total = connection.execute(
                f"SELECT COUNT(*) FROM inbound_messages m LEFT JOIN inbound_classifications c ON c.message_id = m.id {where}",
                params,
            ).fetchone()[0]
            items = []
            for row in rows:
                item = _decode(row)
                correlations = connection.execute(
                    "SELECT match_kind, entity_type, entity_id, evidence_json, trust_status FROM inbound_correlations WHERE message_id = ?",
                    (row["id"],),
                ).fetchall()
                item["correlations"] = [_decode(correlation) for correlation in correlations]
                items.append(item)
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def detail(self, message_id: str) -> dict[str, Any]:
        item = _decode(self.store.get_inbound_message(message_id))
        with self.store.connect() as connection:
            attachments = connection.execute(
                "SELECT * FROM inbound_attachments WHERE message_id = ? ORDER BY part_path",
                (message_id,),
            ).fetchall()
            extracted = connection.execute(
                "SELECT * FROM inbound_extracted_values WHERE message_id = ? ORDER BY field_key",
                (message_id,),
            ).fetchall()
            actions = connection.execute(
                "SELECT * FROM inbound_actions WHERE message_id = ? ORDER BY proposed_at",
                (message_id,),
            ).fetchall()
            runs = connection.execute(
                "SELECT * FROM inbound_rule_runs WHERE message_id = ? ORDER BY occurred_at",
                (message_id,),
            ).fetchall()
            drafts = connection.execute(
                "SELECT * FROM inbound_drafts WHERE message_id = ?", (message_id,)
            ).fetchall()
            correlations = connection.execute(
                "SELECT * FROM inbound_correlations WHERE message_id = ?", (message_id,)
            ).fetchall()
        item["attachments"] = [_decode(row) for row in attachments]
        item["extractions"] = [_decode(row) for row in extracted]
        item["actions"] = [_decode(row) for row in actions]
        item["rule_runs"] = [_decode(row) for row in runs]
        item["drafts"] = [_decode(row) for row in drafts]
        item["correlations"] = [_decode(row) for row in correlations]
        return item
