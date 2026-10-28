"""Durable, local-only visual workflow orchestration."""

# SQL statements and compact workflow projections remain readable as fragments.
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mailforge.application.campaigns import CampaignService
from mailforge.application.segmentation import SegmentationError, SegmentationService
from mailforge.domain.campaign import utc_now
from mailforge.infrastructure.campaign_store import CampaignStore

WORKFLOW_STATUSES = {"draft", "active", "paused", "archived"}
REVISION_STATUSES = {"draft", "published"}
RUN_STATUSES = {
    "pending",
    "running",
    "waiting",
    "pausing",
    "paused",
    "completed",
    "failed",
    "canceled",
    "requires_review",
}
TRIGGER_KINDS = {
    "manual_enrollment",
    "contact_imported",
    "contact_added_to_list",
    "scheduled_occurrence",
    "trusted_inbound_event",
    "list_processing_completed",
}
NODE_KINDS = {
    "TRIGGER",
    "CONDITION",
    "WAIT",
    "ADD_TO_LIST",
    "REMOVE_FROM_LIST",
    "PREPARE_CAMPAIGN",
    "NOTE",
    "END",
}
ACTION_KINDS = {"ADD_TO_LIST", "REMOVE_FROM_LIST", "PREPARE_CAMPAIGN", "NOTE"}
AUTOMATION_LEASE_SECONDS = 60
MAX_GRAPH_BYTES = 64 * 1024
MAX_NODES = 100
MAX_DEPTH = 50
MAX_CONDITIONS = 20
MAX_WAIT_SECONDS = 7 * 24 * 60 * 60
UNSAFE_GRAPH_KEY_MARKERS = {
    "api_key",
    "command",
    "credential",
    "exec",
    "password",
    "private_key",
    "script",
    "secret",
    "shell",
    "smtp",
    "sql",
    "token",
}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def _unsafe_graph_keys(value: Any, path: str = "graph") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(marker in normalized for marker in UNSAFE_GRAPH_KEY_MARKERS):
                found.append(f"{path}.{key}")
            found.extend(_unsafe_graph_keys(child, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_unsafe_graph_keys(child, f"{path}[{index}]"))
    return found


class AutomationError(ValueError):
    def __init__(self, message: str, code: str = "automation_invalid"):
        super().__init__(message)
        self.code = code


class AutomationService:
    """The sole workflow authority for API, CLI, and in-process workers."""

    def __init__(self, store: CampaignStore, *, campaigns: CampaignService | None = None):
        self.store = store
        self.campaigns = campaigns or CampaignService(store)
        self.segmentation = SegmentationService(store)
        self._event_lock = threading.Lock()

    @staticmethod
    def default_graph() -> dict[str, Any]:
        return {
            "version": 1,
            "nodes": [
                {
                    "id": "trigger",
                    "kind": "TRIGGER",
                    "label": "When this happens",
                    "config": {"trigger_kind": "manual_enrollment"},
                },
                {"id": "end", "kind": "END", "label": "End", "config": {}},
            ],
            "edges": [{"source": "trigger", "target": "end", "branch": "default"}],
        }

    def validate_graph(self, graph: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(graph, dict):
            raise AutomationError("graph must be an object")
        if len(_canonical(graph).encode("utf-8")) > MAX_GRAPH_BYTES:
            raise AutomationError("graph exceeds the 64 KiB limit", "complexity_limit")
        unsafe_keys = _unsafe_graph_keys(graph)
        if unsafe_keys:
            raise AutomationError(
                f"graph contains prohibited configuration keys: {', '.join(unsafe_keys[:5])}",
                "unsafe_configuration",
            )
        nodes = graph.get("nodes")
        edges = graph.get("edges")
        if not isinstance(nodes, list) or not isinstance(edges, list):
            raise AutomationError("graph requires nodes and edges arrays")
        if not 1 <= len(nodes) <= MAX_NODES:
            raise AutomationError("graph must contain between 1 and 100 nodes", "complexity_limit")
        by_id: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        condition_count = 0
        for node in nodes:
            if not isinstance(node, dict) or not isinstance(node.get("id"), str):
                errors.append("every node requires a string id")
                continue
            node_id = node["id"]
            if node_id in by_id:
                errors.append(f"duplicate node id: {node_id}")
            by_id[node_id] = node
            kind = node.get("kind")
            if kind not in NODE_KINDS:
                errors.append(f"unsupported node kind: {kind}")
            if kind == "CONDITION":
                condition_count += 1
            config = node.get("config", {})
            if not isinstance(config, dict) or len(_canonical(config).encode("utf-8")) > 8 * 1024:
                errors.append(f"node {node_id} has invalid or oversized configuration")
            if (
                kind == "TRIGGER"
                and node.get("config", {}).get("trigger_kind") not in TRIGGER_KINDS
            ):
                errors.append(f"trigger {node_id} has an unsupported trigger kind")
            if (
                kind == "TRIGGER"
                and node.get("config", {}).get("trigger_kind") == "contact_added_to_list"
                and not node.get("config", {}).get("list_id")
            ):
                errors.append(f"trigger {node_id} requires a list_id")
            if kind == "TRIGGER" and node.get("config", {}).get("trigger_kind") in {
                "scheduled_occurrence",
            }:
                schedule = node.get("config", {})
                if not any(
                    key in schedule for key in ("due_at_utc", "delay_seconds", "local_time")
                ):
                    errors.append(
                        f"scheduled trigger {node_id} requires a due_at_utc, delay_seconds, or local_time"
                    )
                if "delay_seconds" in schedule and (
                    not isinstance(schedule["delay_seconds"], int)
                    or not 0 <= schedule["delay_seconds"] <= MAX_WAIT_SECONDS
                ):
                    errors.append(f"scheduled trigger {node_id} has an invalid bounded delay")
                if "due_at_utc" in schedule:
                    try:
                        due = datetime.fromisoformat(
                            str(schedule["due_at_utc"]).replace("Z", "+00:00")
                        )
                        if due.tzinfo is None:
                            errors.append(
                                f"scheduled trigger {node_id} due_at_utc must include a timezone"
                            )
                    except ValueError:
                        errors.append(f"scheduled trigger {node_id} has an invalid due_at_utc")
                if "local_time" in schedule:
                    try:
                        hour, minute = (
                            int(value) for value in str(schedule["local_time"]).split(":", 1)
                        )
                        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
                            raise ValueError
                    except TypeError, ValueError:
                        errors.append(f"scheduled trigger {node_id} local_time must be HH:MM")
                    try:
                        ZoneInfo(str(schedule.get("timezone", "UTC")))
                    except ZoneInfoNotFoundError, TypeError:
                        errors.append(f"scheduled trigger {node_id} has an invalid IANA timezone")
            if kind == "CONDITION":
                segment_id = node.get("config", {}).get("segment_id")
                segment_revision_id = node.get("config", {}).get("segment_revision_id")
                if segment_id and not segment_revision_id:
                    errors.append(f"condition {node_id} requires segment_revision_id")
                if segment_id and node.get("config", {}).get("result") is not None:
                    errors.append(f"condition {node_id} cannot mix segment and static result")
            if kind in {"ADD_TO_LIST", "REMOVE_FROM_LIST"} and not node.get("config", {}).get(
                "list_id"
            ):
                errors.append(f"action node {node_id} requires list_id")
            if kind == "PREPARE_CAMPAIGN" and not node.get("config", {}).get("campaign_id"):
                errors.append(f"action node {node_id} requires campaign_id")
            if kind == "NOTE" and len(str(node.get("config", {}).get("note", ""))) > 1000:
                errors.append(f"note node {node_id} exceeds the 1000 character limit")
            if kind in ACTION_KINDS and node.get("config", {}).get("action") not in {
                None,
                kind.lower(),
            }:
                errors.append(f"action node {node_id} has an unsupported action")
            if kind == "WAIT":
                config = node.get("config", {})
                if "seconds" not in config and "due_at_utc" not in config:
                    errors.append(f"wait {node_id} requires seconds or due_at_utc")
                if "seconds" in config and (
                    not isinstance(config["seconds"], int)
                    or not 0 <= config["seconds"] <= MAX_WAIT_SECONDS
                ):
                    errors.append(f"wait {node_id} has an invalid bounded duration")
                if "due_at_utc" in config:
                    try:
                        absolute = datetime.fromisoformat(
                            str(config["due_at_utc"]).replace("Z", "+00:00")
                        )
                        if absolute.tzinfo is None:
                            errors.append(f"wait {node_id} due_at_utc must include a timezone")
                    except ValueError:
                        errors.append(f"wait {node_id} has an invalid due_at_utc")
                timezone = config.get("timezone", "UTC")
                try:
                    ZoneInfo(timezone)
                except ZoneInfoNotFoundError, TypeError:
                    errors.append(f"wait {node_id} has an invalid IANA timezone")
        if condition_count > MAX_CONDITIONS:
            errors.append("graph contains more than 20 condition nodes")
        trigger_nodes = [node for node in nodes if node.get("kind") == "TRIGGER"]
        end_nodes = [node for node in nodes if node.get("kind") == "END"]
        if len(trigger_nodes) != 1:
            errors.append("graph must contain exactly one entry trigger")
        if not end_nodes:
            errors.append("graph must contain an End node")
        adjacency: dict[str, list[tuple[str, str]]] = {node_id: [] for node_id in by_id}
        incoming: dict[str, int] = {node_id: 0 for node_id in by_id}
        seen_edges: set[tuple[str, str, str]] = set()
        for edge in edges:
            if not isinstance(edge, dict):
                errors.append("every edge must be an object")
                continue
            source, target = edge.get("source"), edge.get("target")
            branch = edge.get("branch", "default")
            if source not in by_id or target not in by_id:
                errors.append(f"edge references an unknown node: {source} -> {target}")
                continue
            key = (source, target, branch)
            if key in seen_edges:
                errors.append(f"duplicate edge: {source} -> {target} ({branch})")
            seen_edges.add(key)
            adjacency[source].append((target, branch))
            incoming[target] += 1
        for node in nodes:
            if node.get("kind") == "CONDITION":
                branches = {branch for _, branch in adjacency.get(node["id"], [])}
                if not {"true", "false"}.issubset(branches):
                    errors.append(f"condition {node['id']} requires true and false branches")
        if trigger_nodes:
            start = trigger_nodes[0]["id"]
            visiting: set[str] = set()
            visited: set[str] = set()

            def walk(node_id: str, depth: int) -> None:
                if depth > MAX_DEPTH:
                    errors.append("graph exceeds the 50-node path depth limit")
                    return
                if node_id in visiting:
                    errors.append(f"graph contains a cycle at {node_id}")
                    return
                if node_id in visited:
                    return
                visiting.add(node_id)
                for target, _ in adjacency[node_id]:
                    walk(target, depth + 1)
                visiting.remove(node_id)
                visited.add(node_id)

            walk(start, 1)
            if len(visited) != len(by_id):
                errors.append("every node must be reachable from the entry trigger")
            reachable_end = any(end["id"] in visited for end in end_nodes)
            if not reachable_end:
                errors.append("an End node must be reachable from the entry trigger")
        if errors:
            raise AutomationError("; ".join(dict.fromkeys(errors)), "graph_invalid")
        return {
            "valid": True,
            "node_count": len(nodes),
            "edge_count": len(edges),
            "condition_count": condition_count,
            "graph_hash": _hash(graph),
        }

    def create_workflow(
        self, name: str, description: str = "", *, actor: str = "local-operator"
    ) -> dict[str, Any]:
        if not name.strip() or len(name) > 160:
            raise AutomationError("workflow name is required")
        graph = self.default_graph()
        self.validate_graph(graph)
        workflow_id = str(uuid.uuid4())
        now = utc_now()
        revision_id = str(uuid.uuid4())
        with self.store.transaction(immediate=True) as connection:
            try:
                connection.execute(
                    "INSERT INTO automation_workflows(id,name,description,status,current_draft_revision_id,created_at,updated_at,created_by,updated_by) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        workflow_id,
                        name.strip(),
                        description.strip(),
                        "draft",
                        revision_id,
                        now,
                        now,
                        actor,
                        actor,
                    ),
                )
                self._insert_revision(
                    connection, revision_id, workflow_id, 1, graph, "draft", actor
                )
                self._audit(
                    connection,
                    workflow_id,
                    revision_id,
                    None,
                    "workflow_created",
                    actor,
                    {"name": name.strip()},
                )
            except Exception as error:
                if "UNIQUE" in str(error).upper():
                    raise AutomationError(
                        "a workflow with this name already exists", "workflow_exists"
                    ) from error
                raise
        return self.get_workflow(workflow_id)

    def _insert_revision(self, connection, revision_id, workflow_id, number, graph, status, actor):
        now = utc_now()
        validation = self.validate_graph(graph)
        connection.execute(
            "INSERT INTO automation_revisions(id,workflow_id,revision_number,status,graph_json,graph_hash,limits_json,created_at,published_at,created_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                revision_id,
                workflow_id,
                number,
                status,
                _canonical(graph),
                validation["graph_hash"],
                _canonical(validation),
                now,
                now if status == "published" else None,
                actor,
            ),
        )
        for node in graph["nodes"]:
            connection.execute(
                "INSERT INTO automation_nodes(id,revision_id,node_key,node_kind,label,config_json,position_json) VALUES (?,?,?,?,?,?,?)",
                (
                    str(uuid.uuid4()),
                    revision_id,
                    node["id"],
                    node["kind"],
                    node.get("label", node["id"]),
                    _canonical(node.get("config", {})),
                    _canonical(node.get("position", {})),
                ),
            )
            if node["kind"] == "TRIGGER":
                connection.execute(
                    "INSERT INTO automation_triggers(id,revision_id,trigger_key,trigger_kind,config_json) VALUES (?,?,?,?,?)",
                    (
                        str(uuid.uuid4()),
                        revision_id,
                        node["id"],
                        node["config"]["trigger_kind"],
                        _canonical(node.get("config", {})),
                    ),
                )
        for index, edge in enumerate(graph["edges"]):
            connection.execute(
                "INSERT INTO automation_edges(id,revision_id,source_key,target_key,branch_key,edge_order) VALUES (?,?,?,?,?,?)",
                (
                    str(uuid.uuid4()),
                    revision_id,
                    edge["source"],
                    edge["target"],
                    edge.get("branch", "default"),
                    index,
                ),
            )

    @staticmethod
    def _audit(connection, workflow_id, revision_id, run_id, action, actor, metadata=None):
        connection.execute(
            "INSERT INTO automation_audit(workflow_id,revision_id,run_id,action,actor,metadata_json,occurred_at) VALUES (?,?,?,?,?,?,?)",
            (
                workflow_id,
                revision_id,
                run_id,
                action,
                actor,
                _canonical(metadata or {}),
                utc_now(),
            ),
        )

    def get_workflow(self, workflow_id: str) -> dict[str, Any]:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM automation_workflows WHERE id = ?", (workflow_id,)
            ).fetchone()
        if row is None:
            raise AutomationError("workflow was not found", "workflow_not_found")
        result = dict(row)
        result["draft"] = (
            self.get_revision(row["current_draft_revision_id"])
            if row["current_draft_revision_id"]
            else None
        )
        result["active"] = (
            self.get_revision(row["active_revision_id"]) if row["active_revision_id"] else None
        )
        return result

    def list_workflows(self, *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        with self.store.connect() as connection:
            total = connection.execute("SELECT COUNT(*) FROM automation_workflows").fetchone()[0]
            rows = connection.execute(
                "SELECT * FROM automation_workflows ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "limit": limit,
            "offset": offset,
            "total": total,
        }

    def get_revision(self, revision_id: str) -> dict[str, Any]:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM automation_revisions WHERE id = ?", (revision_id,)
            ).fetchone()
        if row is None:
            raise AutomationError("workflow revision was not found", "revision_not_found")
        result = dict(row)
        result["graph"] = json.loads(row["graph_json"])
        result["limits"] = json.loads(row["limits_json"])
        del result["graph_json"]
        del result["limits_json"]
        return result

    def list_revisions(self, workflow_id: str) -> list[dict[str, Any]]:
        self.get_workflow_base(workflow_id)
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM automation_revisions WHERE workflow_id = ? ORDER BY revision_number DESC",
                (workflow_id,),
            ).fetchall()
        return [
            {
                **dict(row),
                "graph": json.loads(row["graph_json"]),
                "limits": json.loads(row["limits_json"]),
            }
            for row in rows
        ]

    def get_workflow_base(self, workflow_id: str):
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM automation_workflows WHERE id = ?", (workflow_id,)
            ).fetchone()
        if row is None:
            raise AutomationError("workflow was not found", "workflow_not_found")
        return row

    def save_revision(
        self,
        workflow_id: str,
        graph: dict[str, Any],
        *,
        revision_id: str | None = None,
        actor: str = "local-operator",
    ) -> dict[str, Any]:
        validation = self.validate_graph(graph)
        with self.store.transaction(immediate=True) as connection:
            workflow = connection.execute(
                "SELECT * FROM automation_workflows WHERE id = ?", (workflow_id,)
            ).fetchone()
            if workflow is None:
                raise AutomationError("workflow was not found", "workflow_not_found")
            target_id = revision_id or workflow["current_draft_revision_id"]
            revision = (
                connection.execute(
                    "SELECT * FROM automation_revisions WHERE id = ? AND workflow_id = ?",
                    (target_id, workflow_id),
                ).fetchone()
                if target_id
                else None
            )
            if revision is not None and revision["status"] == "published":
                revision = None
            if revision is None:
                next_number = connection.execute(
                    "SELECT COALESCE(MAX(revision_number),0)+1 FROM automation_revisions WHERE workflow_id = ?",
                    (workflow_id,),
                ).fetchone()[0]
                target_id = str(uuid.uuid4())
                self._insert_revision(
                    connection, target_id, workflow_id, next_number, graph, "draft", actor
                )
                connection.execute(
                    "UPDATE automation_workflows SET current_draft_revision_id = ?, status = CASE WHEN status = 'active' THEN 'active' ELSE 'draft' END, updated_at = ?, updated_by = ? WHERE id = ?",
                    (target_id, utc_now(), actor, workflow_id),
                )
            else:
                connection.execute(
                    "DELETE FROM automation_nodes WHERE revision_id = ?", (target_id,)
                )
                connection.execute(
                    "DELETE FROM automation_edges WHERE revision_id = ?", (target_id,)
                )
                connection.execute(
                    "DELETE FROM automation_triggers WHERE revision_id = ?", (target_id,)
                )
                connection.execute(
                    "UPDATE automation_revisions SET graph_json = ?, graph_hash = ?, limits_json = ?, created_at = ?, created_by = ? WHERE id = ?",
                    (
                        _canonical(graph),
                        validation["graph_hash"],
                        _canonical(validation),
                        utc_now(),
                        actor,
                        target_id,
                    ),
                )
                self._insert_materialized(connection, target_id, graph)
            self._audit(
                connection,
                workflow_id,
                target_id,
                None,
                "draft_saved",
                actor,
                {"graph_hash": validation["graph_hash"]},
            )
        return self.get_revision(target_id)

    def _insert_materialized(self, connection, revision_id, graph):
        for node in graph["nodes"]:
            connection.execute(
                "INSERT INTO automation_nodes(id,revision_id,node_key,node_kind,label,config_json,position_json) VALUES (?,?,?,?,?,?,?)",
                (
                    str(uuid.uuid4()),
                    revision_id,
                    node["id"],
                    node["kind"],
                    node.get("label", node["id"]),
                    _canonical(node.get("config", {})),
                    _canonical(node.get("position", {})),
                ),
            )
            if node["kind"] == "TRIGGER":
                connection.execute(
                    "INSERT INTO automation_triggers(id,revision_id,trigger_key,trigger_kind,config_json) VALUES (?,?,?,?,?)",
                    (
                        str(uuid.uuid4()),
                        revision_id,
                        node["id"],
                        node["config"]["trigger_kind"],
                        _canonical(node.get("config", {})),
                    ),
                )
        for index, edge in enumerate(graph["edges"]):
            connection.execute(
                "INSERT INTO automation_edges(id,revision_id,source_key,target_key,branch_key,edge_order) VALUES (?,?,?,?,?,?)",
                (
                    str(uuid.uuid4()),
                    revision_id,
                    edge["source"],
                    edge["target"],
                    edge.get("branch", "default"),
                    index,
                ),
            )

    def publish(
        self, workflow_id: str, revision_id: str | None = None, *, actor: str = "local-operator"
    ) -> dict[str, Any]:
        revision_id = (
            revision_id or self.get_workflow_base(workflow_id)["current_draft_revision_id"]
        )
        revision = self.get_revision(revision_id)
        if revision["workflow_id"] != workflow_id or revision["status"] != "draft":
            raise AutomationError("only the current draft can be published", "revision_state")
        self.validate_graph(revision["graph"])
        self._validate_references(revision["graph"])
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE automation_revisions SET status = 'published', published_at = ? WHERE id = ?",
                (utc_now(), revision_id),
            )
            connection.execute(
                "UPDATE automation_workflows SET updated_at = ?, updated_by = ? WHERE id = ?",
                (utc_now(), actor, workflow_id),
            )
            self._audit(connection, workflow_id, revision_id, None, "revision_published", actor)
        return self.get_revision(revision_id)

    def _validate_references(self, graph: dict[str, Any]) -> None:
        with self.store.connect() as connection:
            for node in graph["nodes"]:
                config = node.get("config", {})
                if node["kind"] in {"ADD_TO_LIST", "REMOVE_FROM_LIST"}:
                    exists = connection.execute(
                        "SELECT 1 FROM lists WHERE id = ?", (config["list_id"],)
                    ).fetchone()
                    if exists is None:
                        raise AutomationError(
                            f"list action references a missing list: {config['list_id']}",
                            "reference_not_found",
                        )
                if node["kind"] == "PREPARE_CAMPAIGN":
                    exists = connection.execute(
                        "SELECT 1 FROM campaigns WHERE id = ?", (config["campaign_id"],)
                    ).fetchone()
                    if exists is None:
                        raise AutomationError(
                            f"campaign action references a missing campaign: {config['campaign_id']}",
                            "reference_not_found",
                        )
                if node["kind"] == "CONDITION" and config.get("segment_id"):
                    try:
                        self.segmentation.get_segment(
                            config["segment_id"], config["segment_revision_id"]
                        )
                    except SegmentationError as error:
                        raise AutomationError(str(error), "reference_not_found") from error

    def set_lifecycle(
        self,
        workflow_id: str,
        action: str,
        revision_id: str | None = None,
        *,
        actor: str = "local-operator",
    ) -> dict[str, Any]:
        if action not in {"activate", "pause", "archive"}:
            raise AutomationError("unsupported workflow lifecycle action")
        with self.store.transaction(immediate=True) as connection:
            workflow = connection.execute(
                "SELECT * FROM automation_workflows WHERE id = ?", (workflow_id,)
            ).fetchone()
            if workflow is None:
                raise AutomationError("workflow was not found", "workflow_not_found")
            if action == "activate":
                revision_id = revision_id or workflow["current_draft_revision_id"]
                revision = connection.execute(
                    "SELECT * FROM automation_revisions WHERE id = ? AND workflow_id = ? AND status = 'published'",
                    (revision_id, workflow_id),
                ).fetchone()
                if revision is None:
                    raise AutomationError(
                        "activate requires a published revision", "revision_state"
                    )
                status = "active"
                connection.execute(
                    "UPDATE automation_workflows SET status = ?, active_revision_id = ?, updated_at = ?, updated_by = ? WHERE id = ?",
                    (status, revision_id, utc_now(), actor, workflow_id),
                )
                self._ensure_schedule_states(connection, workflow_id, revision_id)
            else:
                status = "paused" if action == "pause" else "archived"
                connection.execute(
                    "UPDATE automation_workflows SET status = ?, updated_at = ?, updated_by = ? WHERE id = ?",
                    (status, utc_now(), actor, workflow_id),
                )
            self._audit(
                connection,
                workflow_id,
                revision_id,
                None,
                f"workflow_{action}d" if action != "archive" else "workflow_archived",
                actor,
            )
        return self.get_workflow(workflow_id)

    def _ensure_schedule_states(self, connection, workflow_id: str, revision_id: str) -> None:
        revision = connection.execute(
            "SELECT graph_json FROM automation_revisions WHERE id = ?", (revision_id,)
        ).fetchone()
        if revision is None:
            return
        graph = json.loads(revision["graph_json"])
        now = _now()
        for node in graph.get("nodes", []):
            if (
                node.get("kind") != "TRIGGER"
                or node.get("config", {}).get("trigger_kind") != "scheduled_occurrence"
            ):
                continue
            due = self._schedule_due(node.get("config", {}), now)
            connection.execute(
                "INSERT OR IGNORE INTO automation_schedule_state(workflow_id,revision_id,trigger_key,next_due_at_utc,updated_at) VALUES (?,?,?,?,?)",
                (workflow_id, revision_id, node["id"], due.isoformat(), now.isoformat()),
            )

    @staticmethod
    def _schedule_due(config: dict[str, Any], now: datetime) -> datetime:
        if config.get("due_at_utc"):
            due = datetime.fromisoformat(str(config["due_at_utc"]).replace("Z", "+00:00"))
            return due.astimezone(UTC)
        if config.get("delay_seconds") is not None:
            return now.astimezone(UTC) + timedelta(seconds=int(config["delay_seconds"]))
        return AutomationService._next_local_due(config, now)

    @staticmethod
    def _next_local_due(config: dict[str, Any], now: datetime) -> datetime:
        timezone = ZoneInfo(str(config.get("timezone", "UTC")))
        hour, minute = (
            int(value) for value in str(config.get("local_time", "00:00")).split(":", 1)
        )
        weekdays = {int(day) for day in config.get("weekdays", range(7))}
        local_now = now.astimezone(timezone)
        for day_offset in range(0, 370):
            date = (local_now + timedelta(days=day_offset)).date()
            if date.weekday() not in weekdays:
                continue
            naive = datetime(date.year, date.month, date.day, hour, minute)
            for fold in (0, 1):
                candidate = naive.replace(tzinfo=timezone, fold=fold)
                if candidate.astimezone(timezone).replace(tzinfo=None) != naive:
                    continue
                if candidate.astimezone(UTC) > now.astimezone(UTC):
                    return candidate.astimezone(UTC)
            # A nonexistent spring-forward time is shifted to the first valid
            # minute after the requested wall time.
            for minute_offset in range(1, 181):
                shifted = naive + timedelta(minutes=minute_offset)
                candidate = shifted.replace(tzinfo=timezone, fold=0)
                if candidate.astimezone(timezone).replace(
                    tzinfo=None
                ) == shifted and candidate.astimezone(UTC) > now.astimezone(UTC):
                    return candidate.astimezone(UTC)
        raise AutomationError(
            "scheduled trigger has no calculable future occurrence", "schedule_invalid"
        )

    def reconcile_operational_events(self, *, limit: int = 100) -> list[str]:
        """Consume committed application facts and return newly enrolled runs."""

        run_ids: list[str] = []
        with self._event_lock:
            with self.store.transaction(immediate=True) as connection:
                cursor = connection.execute(
                    "SELECT last_event_id FROM automation_event_cursor WHERE id = 1"
                ).fetchone()["last_event_id"]
                events = connection.execute(
                    "SELECT * FROM operational_events WHERE id > ? ORDER BY id LIMIT ?",
                    (cursor, max(1, min(limit, 250))),
                ).fetchall()
            for event in events:
                payload = json.loads(event["payload_json"] or "{}")
                event_kind = {
                    "contact_imported": "contact_imported",
                    "contact_added_to_list": "contact_added_to_list",
                    "trusted_inbound_reconciled": "trusted_inbound_event",
                    "list_processing_completed": "list_processing_completed",
                }.get(event["event_type"])
                if event_kind:
                    for workflow in self.list_workflows(limit=250, offset=0)["items"]:
                        if workflow["status"] != "active":
                            continue
                        revision = self.get_revision(workflow["active_revision_id"])
                        matching = [
                            node
                            for node in revision["graph"]["nodes"]
                            if node.get("kind") == "TRIGGER"
                            and node.get("config", {}).get("trigger_kind") == event_kind
                        ]
                        for trigger in matching:
                            if event_kind == "contact_added_to_list" and trigger["config"].get(
                                "list_id"
                            ) != payload.get("list_id"):
                                continue
                            if event_kind == "trusted_inbound_event" and payload.get(
                                "trust_status"
                            ) not in {"trusted_synthetic", "trusted"}:
                                continue
                            try:
                                result = self.enroll(
                                    workflow["id"],
                                    contact_id=payload.get("contact_id")
                                    or (
                                        event["aggregate_id"]
                                        if event["aggregate_type"] == "contact"
                                        else None
                                    ),
                                    event_kind=event_kind,
                                    source_identity=f"operational:{event['id']}",
                                    subject_key=payload.get("contact_id")
                                    or payload.get("job_id")
                                    or f"event:{event['id']}",
                                    payload={**payload, "operational_event_id": event["id"]},
                                    provenance={
                                        "source": "operational_events",
                                        "event_id": event["id"],
                                        **payload.get("provenance", {}),
                                    },
                                    correlation_id=payload.get("correlation_id")
                                    or f"operational:{event['id']}",
                                    occurred_at=event["occurred_at"],
                                    actor="automation-supervisor",
                                )
                                if result.get("run_id"):
                                    run_ids.append(result["run_id"])
                            except AutomationError as error:
                                if error.code not in {
                                    "trigger_mismatch",
                                    "trigger_not_configured",
                                    "workflow_state",
                                }:
                                    raise
                with self.store.transaction(immediate=True) as connection:
                    connection.execute(
                        "UPDATE automation_event_cursor SET last_event_id = ?, updated_at = ? WHERE id = 1 AND last_event_id = ?",
                        (event["id"], utc_now(), cursor),
                    )
                    cursor = event["id"]
        return run_ids

    def reconcile_schedules(self, *, now: datetime | None = None, limit: int = 25) -> list[str]:
        clock = (now or _now()).astimezone(UTC)
        run_ids: list[str] = []
        with self.store.connect() as connection:
            states = connection.execute(
                "SELECT * FROM automation_schedule_state WHERE next_due_at_utc <= ? ORDER BY next_due_at_utc LIMIT ?",
                (clock.isoformat(), max(1, min(limit, 100))),
            ).fetchall()
        for state in states:
            workflow = self.get_workflow_base(state["workflow_id"])
            if (
                workflow["status"] != "active"
                or workflow["active_revision_id"] != state["revision_id"]
            ):
                continue
            revision = self.get_revision(state["revision_id"])
            trigger = next(
                (node for node in revision["graph"]["nodes"] if node["id"] == state["trigger_key"]),
                None,
            )
            if trigger is None:
                continue
            due = datetime.fromisoformat(state["next_due_at_utc"]).astimezone(UTC)
            occurrence = due.isoformat()
            result = self.enroll(
                state["workflow_id"],
                contact_id=trigger["config"].get("contact_id"),
                event_kind="scheduled_occurrence",
                source_identity=f"schedule:{state['workflow_id']}:{state['revision_id']}:{state['trigger_key']}:{occurrence}",
                subject_key=trigger["config"].get("subject_key")
                or f"schedule:{state['workflow_id']}",
                payload={
                    "scheduled_for_utc": occurrence,
                    "timezone": trigger["config"].get("timezone", "UTC"),
                },
                provenance={"source": "durable_scheduler", "revision_id": state["revision_id"]},
                correlation_id=f"schedule:{state['workflow_id']}:{occurrence}",
                occurred_at=occurrence,
                actor="automation-scheduler",
            )
            if result.get("run_id"):
                run_ids.append(result["run_id"])
            config = trigger["config"]
            if config.get("local_time") and config.get("recurrence", "daily") in {
                "daily",
                "weekly",
            }:
                next_due = self._next_local_due(config, max(clock, due) + timedelta(seconds=1))
            else:
                next_due = datetime.max.replace(tzinfo=UTC)
            with self.store.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE automation_schedule_state SET next_due_at_utc = ?, last_occurrence_key = ?, updated_at = ? WHERE workflow_id = ? AND revision_id = ? AND trigger_key = ? AND next_due_at_utc = ?",
                    (
                        next_due.isoformat(),
                        occurrence,
                        utc_now(),
                        state["workflow_id"],
                        state["revision_id"],
                        state["trigger_key"],
                        state["next_due_at_utc"],
                    ),
                )
        return run_ids

    def due_run_ids(self, *, now: datetime | None = None, limit: int = 25) -> list[str]:
        clock = (now or _now()).astimezone(UTC).isoformat()
        with self.store.connect() as connection:
            return [
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM automation_runs WHERE status IN ('pending', 'waiting', 'running') AND (due_at_utc IS NULL OR due_at_utc <= ?) ORDER BY updated_at LIMIT ?",
                    (clock, max(1, min(limit, 100))),
                ).fetchall()
            ]

    def enroll(
        self,
        workflow_id: str,
        *,
        contact_id: str | None = None,
        event_kind: str = "manual_enrollment",
        source_identity: str | None = None,
        subject_key: str | None = None,
        payload: dict[str, Any] | None = None,
        provenance: dict[str, Any] | None = None,
        correlation_id: str | None = None,
        occurred_at: str | None = None,
        actor: str = "local-operator",
    ) -> dict[str, Any]:
        workflow = self.get_workflow_base(workflow_id)
        if workflow["status"] != "active":
            raise AutomationError("only active workflows can enroll contacts", "workflow_state")
        revision_id = workflow["active_revision_id"]
        revision = self.get_revision(revision_id)
        if event_kind not in TRIGGER_KINDS:
            raise AutomationError("unsupported source event kind")
        if event_kind == "manual_enrollment" and not contact_id:
            raise AutomationError("manual enrollment requires contact_id")
        subject_key = subject_key or contact_id or source_identity or str(uuid.uuid4())
        source_identity = source_identity or f"manual:{workflow_id}:{subject_key}"
        if source_identity.startswith("automation:"):
            raise AutomationError("workflow actions cannot self-enroll", "recursion_blocked")
        event_key = _hash(
            {
                "source_identity": source_identity,
                "event_kind": event_kind,
                "subject_key": subject_key,
            }
        )
        reentry_key = "once_per_contact" if contact_id else f"event:{event_key}"
        trigger_nodes = [
            node
            for node in revision["graph"]["nodes"]
            if node["kind"] == "TRIGGER" and node["config"].get("trigger_kind") == event_kind
        ]
        if not trigger_nodes:
            raise AutomationError(
                "active revision has no matching trigger", "trigger_not_configured"
            )
        trigger = trigger_nodes[0]
        if event_kind == "contact_added_to_list":
            configured_list_id = trigger["config"].get("list_id")
            if configured_list_id and (payload or {}).get("list_id") != configured_list_id:
                raise AutomationError(
                    "source event list does not match trigger", "trigger_mismatch"
                )
        if (
            event_kind == "trusted_inbound_event"
            and (provenance or {}).get("trust_status") != "trusted"
        ):
            raise AutomationError(
                "trusted inbound trigger requires trusted provenance", "untrusted_event"
            )
        now = utc_now()
        enrollment_id = str(uuid.uuid4())
        run_id = str(uuid.uuid4())
        event_id = str(uuid.uuid4())
        next_node = self._next_nodes(revision["graph"], trigger["id"])[0][0]
        with self.store.transaction(immediate=True) as connection:
            existing_event = connection.execute(
                "SELECT id FROM automation_source_events WHERE dedupe_key = ?", (event_key,)
            ).fetchone()
            if existing_event:
                existing = connection.execute(
                    "SELECT * FROM automation_enrollments WHERE workflow_id = ? AND source_event_id = ?",
                    (workflow_id, existing_event["id"]),
                ).fetchone()
                if existing:
                    return {
                        "enrolled": False,
                        "deduplicated": True,
                        "enrollment_id": existing["id"],
                    }
                event_id = existing_event["id"]
            else:
                connection.execute(
                    "INSERT INTO automation_source_events(id,source_identity,event_kind,subject_key,contact_id,payload_json,provenance_json,correlation_id,occurred_at,received_at,dedupe_key) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        event_id,
                        source_identity,
                        event_kind,
                        subject_key,
                        contact_id,
                        _canonical(payload or {}),
                        _canonical(provenance or {}),
                        correlation_id,
                        occurred_at or now,
                        now,
                        event_key,
                    ),
                )
            if (
                contact_id
                and connection.execute(
                    "SELECT 1 FROM contacts WHERE id = ?", (contact_id,)
                ).fetchone()
                is None
            ):
                raise AutomationError("contact was not found", "contact_not_found")
            if (
                contact_id
                and workflow["reentry_policy"] == "once_per_contact"
                and connection.execute(
                    "SELECT id FROM automation_enrollments WHERE workflow_id = ? AND contact_id = ? AND reentry_key = ?",
                    (workflow_id, contact_id, reentry_key),
                ).fetchone()
            ):
                return {"enrolled": False, "deduplicated": True, "enrollment_id": None}
            connection.execute(
                "INSERT INTO automation_enrollments(id,workflow_id,revision_id,source_event_id,contact_id,subject_key,trigger_key,reentry_key,provenance_json,correlation_id,enrolled_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    enrollment_id,
                    workflow_id,
                    revision_id,
                    event_id,
                    contact_id,
                    subject_key,
                    trigger["id"],
                    reentry_key,
                    _canonical(provenance or {}),
                    correlation_id,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO automation_runs(id,enrollment_id,workflow_id,revision_id,status,current_node_key,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (run_id, enrollment_id, workflow_id, revision_id, "pending", next_node, now, now),
            )
            self._audit(
                connection,
                workflow_id,
                revision_id,
                run_id,
                "enrollment_created",
                actor,
                {"event_kind": event_kind, "contact_id": contact_id},
            )
        return {
            "enrolled": True,
            "deduplicated": False,
            "enrollment_id": enrollment_id,
            "run_id": run_id,
            "revision_id": revision_id,
        }

    @staticmethod
    def _next_nodes(graph, source):
        return [
            (edge["target"], edge.get("branch", "default"))
            for edge in graph["edges"]
            if edge["source"] == source
        ]

    def list_runs(self, workflow_id: str, *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        self.get_workflow_base(workflow_id)
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        with self.store.connect() as connection:
            total = connection.execute(
                "SELECT COUNT(*) FROM automation_runs WHERE workflow_id = ?", (workflow_id,)
            ).fetchone()[0]
            rows = connection.execute(
                "SELECT * FROM automation_runs WHERE workflow_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (workflow_id, limit, offset),
            ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "limit": limit,
            "offset": offset,
            "total": total,
        }

    def run_detail(self, run_id: str) -> dict[str, Any]:
        with self.store.connect() as connection:
            run = connection.execute(
                "SELECT * FROM automation_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise AutomationError("automation run was not found", "run_not_found")
            steps = connection.execute(
                "SELECT * FROM automation_step_history WHERE run_id = ? ORDER BY started_at",
                (run_id,),
            ).fetchall()
            waits = connection.execute(
                "SELECT * FROM automation_waits WHERE run_id = ? ORDER BY due_at_utc", (run_id,)
            ).fetchall()
            actions = connection.execute(
                "SELECT * FROM automation_action_correlations WHERE run_id = ? ORDER BY created_at",
                (run_id,),
            ).fetchall()
            audits = connection.execute(
                "SELECT * FROM automation_audit WHERE run_id = ? ORDER BY occurred_at", (run_id,)
            ).fetchall()
        return {
            "run": dict(run),
            "steps": [dict(row) for row in steps],
            "waits": [dict(row) for row in waits],
            "actions": [dict(row) for row in actions],
            "audit": [dict(row) for row in audits],
        }

    def control_run(
        self, run_id: str, action: str, *, actor: str = "local-operator"
    ) -> dict[str, Any]:
        target = {"pause": "pausing", "resume": "running", "cancel": "canceled"}.get(action)
        if not target:
            raise AutomationError("unsupported run control")
        with self.store.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM automation_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise AutomationError("automation run was not found", "run_not_found")
            if action == "resume" and row["status"] not in {"paused", "waiting", "pausing"}:
                raise AutomationError("run is not paused", "run_state")
            if action == "pause" and row["status"] not in {"pending", "running", "waiting"}:
                raise AutomationError("run cannot be paused", "run_state")
            if action == "cancel" and row["status"] in {"completed", "canceled"}:
                return dict(row)
            if action == "pause":
                target = "paused"
            connection.execute(
                "UPDATE automation_runs SET status = ?, lease_expires_at = NULL, updated_at = ? WHERE id = ?",
                (target, utc_now(), run_id),
            )
            self._audit(
                connection, row["workflow_id"], row["revision_id"], run_id, f"run_{action}d", actor
            )
        return self.run_detail(run_id)["run"]

    def advance(
        self,
        run_id: str,
        *,
        max_steps: int = 20,
        now: datetime | None = None,
        worker_id: str = "automation-worker",
    ) -> dict[str, Any]:
        clock = (now or _now()).astimezone(UTC)
        max_steps = max(1, min(max_steps, 50))
        with self.store.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM automation_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise AutomationError("automation run was not found", "run_not_found")
            if row["status"] in {"completed", "canceled", "failed", "requires_review"}:
                return self.run_detail(run_id)
            if row["status"] == "paused":
                return self.run_detail(run_id)
            if row["worker_id"] and row["worker_id"] != worker_id and row["lease_expires_at"]:
                try:
                    lease_expires = datetime.fromisoformat(row["lease_expires_at"])
                except ValueError:
                    lease_expires = clock
                if lease_expires > clock:
                    return self.run_detail(run_id)
            lease_expires_at = (clock + timedelta(seconds=AUTOMATION_LEASE_SECONDS)).isoformat()
            connection.execute(
                "UPDATE automation_runs SET status = 'running', started_at = COALESCE(started_at, ?), worker_id = ?, lease_expires_at = ?, updated_at = ? WHERE id = ?",
                (clock.isoformat(), worker_id, lease_expires_at, clock.isoformat(), run_id),
            )
        for _ in range(max_steps):
            outcome = self._advance_one(run_id, clock, worker_id)
            if outcome in {
                "waiting",
                "completed",
                "paused",
                "failed",
                "requires_review",
                "canceled",
            }:
                break
        return self.run_detail(run_id)

    def fail_run(self, run_id: str, code: str, message: str) -> dict[str, Any]:
        """Persist an unexpected worker failure instead of leaving a run running."""

        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM automation_runs WHERE id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise AutomationError("automation run was not found", "run_not_found")
        if row["status"] in {"completed", "canceled", "failed", "requires_review"}:
            return self.run_detail(run_id)
        self._fail(dict(row), code, message)
        return self.run_detail(run_id)

    def _advance_one(self, run_id, clock, worker_id):
        detail = self.run_detail(run_id)
        run = detail["run"]
        if run["status"] in {"paused", "canceled", "completed", "failed", "requires_review"}:
            return run["status"]
        workflow = self.get_revision(run["revision_id"])
        graph = workflow["graph"]
        nodes = {node["id"]: node for node in graph["nodes"]}
        node = nodes.get(run["current_node_key"])
        if node is None:
            self._fail(run, "node_missing", "current node is missing")
            return "failed"
        if node["kind"] == "WAIT":
            wait = next((item for item in detail["waits"] if item["node_key"] == node["id"]), None)
            if wait is None:
                due = self._wait_due(node, clock)
                with self.store.transaction(immediate=True) as connection:
                    connection.execute(
                        "INSERT INTO automation_waits(id,run_id,node_key,due_at_utc,timezone,local_value,catch_up_policy) VALUES (?,?,?,?,?,?,?)",
                        (
                            str(uuid.uuid4()),
                            run_id,
                            node["id"],
                            due.isoformat(),
                            node["config"].get("timezone", "UTC"),
                            node["config"].get("local_value", ""),
                            node["config"].get("catch_up_policy", "run_once"),
                        ),
                    )
                    connection.execute(
                        "UPDATE automation_runs SET status = 'waiting', waiting_since = ?, due_at_utc = ?, lease_expires_at = NULL, updated_at = ? WHERE id = ?",
                        (clock.isoformat(), due.isoformat(), clock.isoformat(), run_id),
                    )
                return "waiting"
            if clock < datetime.fromisoformat(wait["due_at_utc"]):
                return "waiting"
            self._finish_step(run, node, "completed", {"due_at_utc": wait["due_at_utc"]})
            self._move(run, self._next_nodes(graph, node["id"])[0][0], clock)
            return "running"
        if node["kind"] == "CONDITION":
            result = self._condition(run, node)
            branch = "true" if result is True else "false" if result is False else "unknown"
            choices = self._next_nodes(graph, node["id"])
            target = next((target for target, key in choices if key == branch), None)
            if target is None and branch == "unknown":
                target = next((target for target, key in choices if key == "false"), None)
            if target is None:
                self._fail(run, "condition_branch_missing", f"no {branch} branch")
                return "failed"
            self._finish_step(run, node, "completed", {"result": branch})
            self._move(run, target, clock)
            return "running"
        if node["kind"] in ACTION_KINDS:
            try:
                output = self._action(run, node)
            except AutomationError as error:
                self._fail(run, error.code, str(error))
                return "failed"
            self._finish_step(run, node, "completed", output, side_effect_phase="confirmed")
            next_nodes = self._next_nodes(graph, node["id"])
            if not next_nodes:
                self._fail(run, "graph_invalid", "action has no next node")
                return "failed"
            self._move(run, next_nodes[0][0], clock)
            return "running"
        if node["kind"] == "END":
            self._finish_step(run, node, "completed", {"ended": True})
            with self.store.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE automation_runs SET status = 'completed', completed_at = ?, lease_expires_at = NULL, updated_at = ? WHERE id = ?",
                    (clock.isoformat(), clock.isoformat(), run_id),
                )
                self._audit(
                    connection,
                    run["workflow_id"],
                    run["revision_id"],
                    run_id,
                    "run_completed",
                    "automation-worker",
                )
            return "completed"
        self._finish_step(run, node, "completed", {})
        next_nodes = self._next_nodes(graph, node["id"])
        if not next_nodes:
            self._fail(run, "graph_invalid", "node has no next node")
            return "failed"
        self._move(run, next_nodes[0][0], clock)
        return "running"

    def _wait_due(self, node, clock):
        config = node.get("config", {})
        if "due_at_utc" in config:
            try:
                due = datetime.fromisoformat(config["due_at_utc"].replace("Z", "+00:00"))
            except ValueError as error:
                raise AutomationError("wait due_at_utc is invalid") from error
            if due.tzinfo is None:
                raise AutomationError("wait due_at_utc must include a timezone")
            return due.astimezone(UTC)
        return clock + timedelta(seconds=int(config.get("seconds", 0)))

    def _condition(self, run, node):
        config = node.get("config", {})
        if config.get("result") in {True, False}:
            return config["result"]
        segment_id = config.get("segment_id")
        if not segment_id:
            return None
        try:
            result = self.segmentation.preview(
                segment_id, revision_id=config.get("segment_revision_id"), limit=200
            )
        except SegmentationError:
            return None
        with self.store.connect() as connection:
            contact = connection.execute(
                "SELECT contact_id FROM automation_enrollments e JOIN automation_runs r ON r.enrollment_id = e.id WHERE r.id = ?",
                (run["id"],),
            ).fetchone()
        return (
            any(item["id"] == contact["contact_id"] for item in result["items"])
            if contact
            else None
        )

    def _action(self, run, node):
        config = node.get("config", {})
        key = f"automation:{run['id']}:{node['id']}"
        with self.store.connect() as connection:
            enrollment = connection.execute(
                "SELECT * FROM automation_enrollments WHERE id = (SELECT enrollment_id FROM automation_runs WHERE id = ?)",
                (run["id"],),
            ).fetchone()
        if enrollment is None:
            raise AutomationError("enrollment is missing", "enrollment_missing")
        existing = self.store.connect()
        try:
            action_row = existing.execute(
                "SELECT * FROM automation_action_correlations WHERE idempotency_key = ?", (key,)
            ).fetchone()
        finally:
            existing.close()
        if action_row:
            return {"idempotent": True, "outcome": action_row["outcome"]}
        if node["kind"] in {"ADD_TO_LIST", "REMOVE_FROM_LIST"}:
            list_id = config.get("list_id")
            if not list_id or not enrollment["contact_id"]:
                raise AutomationError("list action requires list_id and contact")
            from mailforge.application.segmentation import SegmentationError as SError

            try:
                result = self.segmentation.set_membership(
                    list_id,
                    enrollment["contact_id"],
                    "add" if node["kind"] == "ADD_TO_LIST" else "remove",
                    idempotency_key=key,
                    source="automation",
                    actor="automation-worker",
                )
            except SError as error:
                raise AutomationError(str(error), error.code) from error
            output = result
        elif node["kind"] == "NOTE":
            with self.store.transaction(immediate=True) as connection:
                self.store.record_event(
                    connection,
                    "automation_run",
                    run["id"],
                    "automation_operator_note",
                    {"note": str(config.get("note", ""))[:1000], "node": node["id"]},
                )
            output = {"note_recorded": True}
        else:
            campaign_id = config.get("campaign_id")
            if not campaign_id:
                raise AutomationError("prepare campaign action requires campaign_id")
            try:
                report = self.campaigns.prepare(campaign_id)
            except (KeyError, ValueError) as error:
                raise AutomationError(str(error), "campaign_prepare_failed") from error
            output = {"campaign_id": campaign_id, "prepared": report.to_dict()}
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO automation_action_correlations(id,run_id,node_key,idempotency_key,action_kind,target_key,correlation_id,outcome,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    str(uuid.uuid4()),
                    run["id"],
                    node["id"],
                    key,
                    node["kind"],
                    config.get("list_id") or config.get("campaign_id"),
                    enrollment["correlation_id"],
                    "confirmed",
                    utc_now(),
                    utc_now(),
                ),
            )
        return output

    def _finish_step(self, run, node, status, output, *, side_effect_phase="none"):
        with self.store.transaction(immediate=True) as connection:
            attempt = connection.execute(
                "SELECT COALESCE(MAX(attempt_number),0)+1 FROM automation_step_history WHERE run_id = ? AND node_key = ?",
                (run["id"], node["id"]),
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO automation_step_history(id,run_id,node_key,attempt_number,status,side_effect_phase,output_json,started_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    str(uuid.uuid4()),
                    run["id"],
                    node["id"],
                    attempt,
                    status,
                    side_effect_phase,
                    _canonical(output),
                    utc_now(),
                    utc_now(),
                ),
            )

    def _move(self, run, target, clock):
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE automation_runs SET current_node_key = ?, status = 'running', due_at_utc = NULL, waiting_since = NULL, updated_at = ? WHERE id = ?",
                (target, clock.isoformat(), run["id"]),
            )

    def _fail(self, run, code, message):
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE automation_runs SET status = 'failed', failure_reason = ?, retry_count = retry_count + 1, lease_expires_at = NULL, updated_at = ? WHERE id = ?",
                (f"{code}: {message}", utc_now(), run["id"]),
            )
            self._audit(
                connection,
                run["workflow_id"],
                run["revision_id"],
                run["id"],
                "run_failed",
                "automation-worker",
                {"code": code},
            )
