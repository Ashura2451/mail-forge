"""Typed contact attributes, saved segments, and durable audience snapshots."""

# SQL statements and compact validation tables remain readable as fragments.
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from mailforge.domain.campaign import DeliveryState, utc_now
from mailforge.domain.email import normalize_email, validate_email
from mailforge.infrastructure.campaign_store import CampaignStore

NUMBER_SCALE = 1_000_000
NUMBER_MIN = -(2**63)
NUMBER_MAX = 2**63 - 1
FIELD_TYPES = {"TEXT", "NUMBER", "BOOLEAN", "DATE", "DATETIME"}
OPERATORS = {
    "EQUALS",
    "NOT_EQUALS",
    "CONTAINS",
    "STARTS_WITH",
    "ENDS_WITH",
    "IN",
    "GREATER_THAN",
    "LESS_THAN",
    "GREATER_THAN_OR_EQUAL",
    "LESS_THAN_OR_EQUAL",
    "IS_PRESENT",
    "IS_MISSING",
}
MAX_DEPTH = 5
MAX_PREDICATES = 64
MAX_IN_VALUES = 100
MAX_COMPARISON = 512
MAX_DEFINITION = 32 * 1024
MACHINE_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class SegmentationError(ValueError):
    """A safe, actionable segmentation validation or lifecycle error."""

    def __init__(self, message: str, code: str = "segmentation_invalid"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CompiledFilter:
    sql: str
    parameters: tuple[Any, ...]
    definition: dict[str, Any]
    definition_hash: str


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_definition(value: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _bounded_text(value: Any, path: str) -> str:
    if not isinstance(value, str) or len(value) > MAX_COMPARISON:
        raise SegmentationError(f"{path} must be a string of at most {MAX_COMPARISON} characters")
    return value


def _scaled_number(value: Any, path: str = "value") -> int:
    if (
        isinstance(value, bool)
        or isinstance(value, float)
        or not isinstance(value, (str, int, Decimal))
    ):
        raise SegmentationError(f"{path} must be a finite decimal")
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise SegmentationError(f"{path} is not a valid decimal") from error
    if not decimal.is_finite():
        raise SegmentationError(f"{path} must be finite")
    if decimal.as_tuple().exponent < -6:
        raise SegmentationError(f"{path} supports at most six fractional digits")
    normalized = decimal.copy_abs().normalize()
    normalized_tuple = normalized.as_tuple()
    integer_digits = max(len(normalized_tuple.digits) + normalized_tuple.exponent, 0)
    if integer_digits > 12:
        raise SegmentationError(f"{path} supports at most twelve integer digits")
    scaled_decimal = decimal * NUMBER_SCALE
    if scaled_decimal != scaled_decimal.to_integral_value():
        raise SegmentationError(f"{path} would require silent rounding")
    scaled = int(scaled_decimal)
    if not NUMBER_MIN <= scaled <= NUMBER_MAX:
        raise SegmentationError(f"{path} overflows signed 64-bit storage")
    return scaled


def _parse_value(field_type: str, value: Any, path: str = "value") -> Any:
    if field_type not in FIELD_TYPES:
        raise SegmentationError(f"unknown field type: {field_type}")
    if field_type == "TEXT":
        return _bounded_text(value, path)
    if field_type == "NUMBER":
        return _scaled_number(value, path)
    if field_type == "BOOLEAN":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in {"true", "false"}:
            return value.lower() == "true"
        raise SegmentationError(f"{path} must be true or false")
    if field_type == "DATE":
        if not isinstance(value, str):
            raise SegmentationError(f"{path} must be an ISO date")
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError as error:
            raise SegmentationError(f"{path} must be an ISO date") from error
    if not isinstance(value, str):
        raise SegmentationError(f"{path} must be a UTC ISO-8601 datetime")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise SegmentationError(f"{path} must be a UTC ISO-8601 datetime") from error
    if parsed.tzinfo is None:
        raise SegmentationError(f"{path} must include a timezone")
    return parsed.astimezone(UTC).isoformat()


def _value_columns(field_type: str, value: Any) -> dict[str, Any]:
    columns = {
        "text_value": None,
        "number_value": None,
        "boolean_value": None,
        "date_value": None,
        "datetime_value": None,
    }
    column = {
        "TEXT": "text_value",
        "NUMBER": "number_value",
        "BOOLEAN": "boolean_value",
        "DATE": "date_value",
        "DATETIME": "datetime_value",
    }[field_type]
    columns[column] = int(value) if field_type == "BOOLEAN" else value
    return columns


def _row_dict(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return dict(row)


class SegmentationService:
    """The sole audience definition/evaluation service for all interfaces."""

    def __init__(self, store: CampaignStore, *, now_fn=None):
        self.store = store
        self.now_fn = now_fn or (lambda: datetime.now(UTC))

    def list_fields(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM contact_field_definitions"
        if not include_archived:
            query += " WHERE status = 'active'"
        query += " ORDER BY machine_key"
        with self.store.connect() as connection:
            return [_row_dict(row) for row in connection.execute(query)]

    def create_field(
        self,
        machine_key: str,
        label: str,
        field_type: str,
        *,
        validation: dict[str, Any] | None = None,
        controlled_values: list[Any] | None = None,
        actor: str = "local-operator",
    ) -> dict[str, Any]:
        if not MACHINE_KEY.fullmatch(machine_key):
            raise SegmentationError(
                "machine_key must contain lowercase letters, digits, and underscores"
            )
        if field_type not in FIELD_TYPES:
            raise SegmentationError(f"unsupported field type: {field_type}")
        if not label.strip():
            raise SegmentationError("field label is required")
        if validation is not None and not isinstance(validation, dict):
            raise SegmentationError("validation must be an object")
        validation_json = _canonical_json(validation or {})
        if len(validation_json.encode("utf-8")) > 8 * 1024:
            raise SegmentationError("field validation is limited to 8 KiB", "complexity_limit")
        controlled = None
        if controlled_values is not None:
            if len(controlled_values) > 100:
                raise SegmentationError("controlled values are limited to 100 items")
            controlled = [
                _parse_value(field_type, item, "controlled value") for item in controlled_values
            ]
        field_id = str(uuid.uuid4())
        now = utc_now()
        with self.store.transaction(immediate=True) as connection:
            try:
                connection.execute(
                    "INSERT INTO contact_field_definitions "
                    "(id, machine_key, label, field_type, validation_json, controlled_values_json, "
                    "created_at, updated_at, created_by, updated_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        field_id,
                        machine_key,
                        label.strip(),
                        field_type,
                        validation_json,
                        _canonical_json(controlled) if controlled is not None else None,
                        now,
                        now,
                        actor,
                        actor,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise SegmentationError(
                    "a field with this machine_key already exists", "field_exists"
                ) from error
        return self.get_field(field_id)

    def get_field(self, field_id: str) -> dict[str, Any]:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM contact_field_definitions WHERE id = ?", (field_id,)
            ).fetchone()
        if row is None:
            raise SegmentationError("field was not found", "field_not_found")
        return _row_dict(row)

    def archive_field(self, field_id: str, *, actor: str = "local-operator") -> None:
        with self.store.transaction(immediate=True) as connection:
            if (
                connection.execute(
                    "UPDATE contact_field_definitions SET status = 'archived', updated_at = ?, updated_by = ? WHERE id = ?",
                    (utc_now(), actor, field_id),
                ).rowcount
                != 1
            ):
                raise SegmentationError("field was not found", "field_not_found")

    def update_field(
        self,
        field_id: str,
        *,
        label: str | None = None,
        field_type: str | None = None,
        actor: str = "local-operator",
    ) -> dict[str, Any]:
        """Update metadata while refusing unplanned type reinterpretation."""

        if label is not None and not label.strip():
            raise SegmentationError("field label is required")
        with self.store.transaction(immediate=True) as connection:
            current = connection.execute(
                "SELECT * FROM contact_field_definitions WHERE id = ?", (field_id,)
            ).fetchone()
            if current is None:
                raise SegmentationError("field was not found", "field_not_found")
            if field_type is not None and field_type != current["field_type"]:
                raise SegmentationError(
                    "field type changes require a validated migration",
                    "field_type_migration_required",
                )
            connection.execute(
                "UPDATE contact_field_definitions SET label = COALESCE(?, label), updated_at = ?, updated_by = ? WHERE id = ?",
                (label.strip() if label is not None else None, utc_now(), actor, field_id),
            )
        return self.get_field(field_id)

    def _field_map(
        self, connection: sqlite3.Connection, *, include_archived: bool = False
    ) -> dict[str, dict[str, Any]]:
        query = "SELECT * FROM contact_field_definitions"
        if not include_archived:
            query += " WHERE status = 'active'"
        rows = connection.execute(query).fetchall()
        result = {row["id"]: _row_dict(row) for row in rows}
        result.update(
            {
                "contact.email": {
                    "id": "contact.email",
                    "field_type": "TEXT",
                    "kind": "contact",
                    "column": "normalized_email",
                },
                "contact.display_name": {
                    "id": "contact.display_name",
                    "field_type": "TEXT",
                    "kind": "contact",
                    "column": "display_name",
                },
                "contact.lifecycle_status": {
                    "id": "contact.lifecycle_status",
                    "field_type": "TEXT",
                    "kind": "contact",
                    "column": "lifecycle_status",
                },
            }
        )
        for row in connection.execute("SELECT id FROM lists"):
            result[f"list:{row['id']}"] = {
                "id": f"list:{row['id']}",
                "field_type": "BOOLEAN",
                "kind": "list",
            }
        return result

    def _validate_node(
        self,
        node: Any,
        fields: dict[str, dict[str, Any]],
        *,
        depth: int,
        counters: list[int],
        path: str,
        canonical_numbers: bool = False,
    ) -> dict[str, Any]:
        if depth > MAX_DEPTH:
            raise SegmentationError(f"{path} exceeds maximum depth {MAX_DEPTH}", "complexity_limit")
        if not isinstance(node, dict) or not isinstance(node.get("kind"), str):
            raise SegmentationError(f"{path}.kind is required")
        kind = node["kind"]
        if kind in {"ALL", "ANY"}:
            children = node.get("children")
            if not isinstance(children, list) or not children:
                raise SegmentationError(f"{path}.children must be a non-empty list")
            return {
                "kind": kind,
                "children": [
                    self._validate_node(
                        child,
                        fields,
                        depth=depth + 1,
                        counters=counters,
                        path=f"{path}.children[{index}]",
                        canonical_numbers=canonical_numbers,
                    )
                    for index, child in enumerate(children)
                ],
            }
        if kind == "NOT":
            if "child" not in node:
                raise SegmentationError(f"{path}.child is required")
            return {
                "kind": "NOT",
                "child": self._validate_node(
                    node["child"],
                    fields,
                    depth=depth + 1,
                    counters=counters,
                    path=f"{path}.child",
                    canonical_numbers=canonical_numbers,
                ),
            }
        if kind != "PREDICATE":
            raise SegmentationError(f"{path}.kind is unsupported")
        counters[0] += 1
        if counters[0] > MAX_PREDICATES:
            raise SegmentationError(
                f"definition exceeds {MAX_PREDICATES} predicates", "complexity_limit"
            )
        field_id = node.get("field_id")
        operator = node.get("operator")
        if field_id not in fields:
            raise SegmentationError(f"{path}.field_id is not registered")
        if operator not in OPERATORS:
            raise SegmentationError(f"{path}.operator is unsupported")
        field = fields[field_id]
        field_type = field["field_type"]
        allowed = {
            "TEXT": {
                "EQUALS",
                "NOT_EQUALS",
                "CONTAINS",
                "STARTS_WITH",
                "ENDS_WITH",
                "IN",
                "IS_PRESENT",
                "IS_MISSING",
            },
            "NUMBER": {
                "EQUALS",
                "NOT_EQUALS",
                "IN",
                "GREATER_THAN",
                "LESS_THAN",
                "GREATER_THAN_OR_EQUAL",
                "LESS_THAN_OR_EQUAL",
                "IS_PRESENT",
                "IS_MISSING",
            },
            "BOOLEAN": {"EQUALS", "NOT_EQUALS", "IN", "IS_PRESENT", "IS_MISSING"},
            "DATE": {
                "EQUALS",
                "NOT_EQUALS",
                "IN",
                "GREATER_THAN",
                "LESS_THAN",
                "GREATER_THAN_OR_EQUAL",
                "LESS_THAN_OR_EQUAL",
                "IS_PRESENT",
                "IS_MISSING",
            },
            "DATETIME": {
                "EQUALS",
                "NOT_EQUALS",
                "IN",
                "GREATER_THAN",
                "LESS_THAN",
                "GREATER_THAN_OR_EQUAL",
                "LESS_THAN_OR_EQUAL",
                "IS_PRESENT",
                "IS_MISSING",
            },
        }[field_type]
        if operator not in allowed:
            raise SegmentationError(f"{path}.operator {operator} is not valid for {field_type}")
        result: dict[str, Any] = {"kind": "PREDICATE", "field_id": field_id, "operator": operator}
        if operator not in {"IS_PRESENT", "IS_MISSING"}:
            if operator == "IN":
                values = node.get("value")
                if not isinstance(values, list) or not values or len(values) > MAX_IN_VALUES:
                    raise SegmentationError(
                        f"{path}.value must contain 1 to {MAX_IN_VALUES} values"
                    )
                result["value"] = [
                    self._parse_node_value(
                        field_type,
                        value,
                        f"{path}.value[{index}]",
                        canonical_numbers=canonical_numbers,
                    )
                    for index, value in enumerate(values)
                ]
            else:
                result["value"] = self._parse_node_value(
                    field_type,
                    node.get("value"),
                    f"{path}.value",
                    canonical_numbers=canonical_numbers,
                )
        return result

    @staticmethod
    def _parse_node_value(
        field_type: str,
        value: Any,
        path: str,
        *,
        canonical_numbers: bool,
    ) -> Any:
        if field_type == "NUMBER" and canonical_numbers:
            if isinstance(value, bool) or not isinstance(value, int):
                raise SegmentationError(f"{path} must be a canonical scaled integer")
            if not NUMBER_MIN <= value <= NUMBER_MAX:
                raise SegmentationError(f"{path} overflows signed 64-bit storage")
            return value
        return _parse_value(field_type, value, path)

    def canonical_definition(
        self,
        definition: dict[str, Any],
        *,
        canonical_numbers: bool = False,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(definition, dict) or definition.get("version") != 1:
            raise SegmentationError("definition version 1 is required")
        raw = _canonical_json(definition)
        if len(raw.encode("utf-8")) > MAX_DEFINITION:
            raise SegmentationError("serialized definition exceeds 32 KiB", "complexity_limit")
        with self.store.connect() as connection:
            fields = self._field_map(connection, include_archived=include_archived)
            root = self._validate_node(
                definition.get("root"),
                fields,
                depth=1,
                counters=[0],
                path="root",
                canonical_numbers=canonical_numbers,
            )
        return {"version": 1, "root": root}

    def create_segment(
        self,
        name: str,
        definition: dict[str, Any],
        *,
        description: str = "",
        actor: str = "local-operator",
    ) -> dict[str, Any]:
        canonical = self.canonical_definition(definition)
        segment_id = str(uuid.uuid4())
        revision_id = str(uuid.uuid4())
        now = utc_now()
        definition_json = _canonical_json(canonical)
        definition_hash = _hash_definition(canonical)
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO segments(id, name, description, current_revision_id, created_at, updated_at, created_by, updated_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    segment_id,
                    name.strip(),
                    description.strip(),
                    revision_id,
                    now,
                    now,
                    actor,
                    actor,
                ),
            )
            connection.execute(
                "INSERT INTO segment_revisions(id, segment_id, revision_number, definition_version, definition_json, definition_hash, change_reason, created_at, created_by) "
                "VALUES (?, ?, 1, 1, ?, ?, 'created', ?, ?)",
                (revision_id, segment_id, definition_json, definition_hash, now, actor),
            )
        return self.get_segment(segment_id)

    def list_segments(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM segments"
        if not include_archived:
            query += " WHERE status = 'active'"
        query += " ORDER BY name"
        with self.store.connect() as connection:
            return [_row_dict(row) for row in connection.execute(query)]

    def get_segment(self, segment_id: str, revision_id: str | None = None) -> dict[str, Any]:
        with self.store.connect() as connection:
            segment = connection.execute(
                "SELECT * FROM segments WHERE id = ?", (segment_id,)
            ).fetchone()
            if segment is None:
                raise SegmentationError("segment was not found", "segment_not_found")
            selected = revision_id or segment["current_revision_id"]
            revision = connection.execute(
                "SELECT * FROM segment_revisions WHERE id = ? AND segment_id = ?",
                (selected, segment_id),
            ).fetchone()
            revisions = connection.execute(
                "SELECT * FROM segment_revisions WHERE segment_id = ? ORDER BY revision_number DESC",
                (segment_id,),
            ).fetchall()
        if revision is None:
            raise SegmentationError("segment revision was not found", "revision_not_found")
        result = _row_dict(segment)
        result["revision"] = _row_dict(revision)
        result["revisions"] = [_row_dict(item) for item in revisions]
        result["definition"] = json.loads(revision["definition_json"])
        return result

    def edit_segment(
        self,
        segment_id: str,
        definition: dict[str, Any],
        *,
        expected_revision_id: str,
        description: str | None = None,
        actor: str = "local-operator",
        reason: str = "edited",
    ) -> dict[str, Any]:
        canonical = self.canonical_definition(definition)
        definition_json = _canonical_json(canonical)
        definition_hash = _hash_definition(canonical)
        revision_id = str(uuid.uuid4())
        now = utc_now()
        with self.store.transaction(immediate=True) as connection:
            segment = connection.execute(
                "SELECT * FROM segments WHERE id = ?", (segment_id,)
            ).fetchone()
            if segment is None:
                raise SegmentationError("segment was not found", "segment_not_found")
            if segment["status"] != "active":
                raise SegmentationError("archived segments cannot be edited", "segment_archived")
            if segment["current_revision_id"] != expected_revision_id:
                raise SegmentationError(
                    "segment changed; reload before saving", "revision_conflict"
                )
            number = connection.execute(
                "SELECT COALESCE(MAX(revision_number), 0) + 1 AS next_number FROM segment_revisions WHERE segment_id = ?",
                (segment_id,),
            ).fetchone()["next_number"]
            connection.execute(
                "INSERT INTO segment_revisions(id, segment_id, revision_number, definition_version, definition_json, definition_hash, change_reason, created_at, created_by) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)",
                (
                    revision_id,
                    segment_id,
                    number,
                    definition_json,
                    definition_hash,
                    reason,
                    now,
                    actor,
                ),
            )
            connection.execute(
                "UPDATE segments SET current_revision_id = ?, description = COALESCE(?, description), updated_at = ?, updated_by = ? "
                "WHERE id = ? AND current_revision_id = ?",
                (revision_id, description, now, actor, segment_id, expected_revision_id),
            )
        return self.get_segment(segment_id)

    def archive_segment(self, segment_id: str, *, actor: str = "local-operator") -> None:
        with self.store.transaction(immediate=True) as connection:
            if (
                connection.execute(
                    "UPDATE segments SET status = 'archived', updated_at = ?, updated_by = ? WHERE id = ?",
                    (utc_now(), actor, segment_id),
                ).rowcount
                != 1
            ):
                raise SegmentationError("segment was not found", "segment_not_found")

    def _compile_node(
        self,
        node: dict[str, Any],
        fields: dict[str, dict[str, Any]],
    ) -> tuple[str, list[Any]]:
        kind = node["kind"]
        if kind in {"ALL", "ANY"}:
            joiner = " AND " if kind == "ALL" else " OR "
            pieces: list[str] = []
            parameters: list[Any] = []
            for child in node["children"]:
                sql, params = self._compile_node(child, fields)
                pieces.append(f"({sql})")
                parameters.extend(params)
            return joiner.join(pieces), parameters
        if kind == "NOT":
            sql, parameters = self._compile_node(node["child"], fields)
            return f"NOT ({sql})", parameters
        field = fields[node["field_id"]]
        operator = node["operator"]
        value = node.get("value")
        if field.get("kind") == "contact":
            column = field["column"]
            if operator == "IS_PRESENT":
                return "1", []
            if operator == "IS_MISSING":
                return "0", []
            return self._comparison(column, operator, value, field["field_type"])
        if node["field_id"].startswith("list:"):
            list_id = node["field_id"][5:]
            exists = "EXISTS (SELECT 1 FROM list_memberships lm WHERE lm.list_id = ? AND lm.contact_id = c.id)"
            if operator == "IS_PRESENT":
                return exists, [list_id]
            if operator == "IS_MISSING":
                return f"NOT ({exists})", [list_id]
            if operator == "EQUALS":
                return (exists if value else f"NOT ({exists})"), [list_id]
            if operator == "NOT_EQUALS":
                return (f"NOT ({exists})" if value else exists), [list_id]
            raise SegmentationError("list membership supports presence or boolean equality")
        field_id = field["id"]
        column = {
            "TEXT": "av.text_value",
            "NUMBER": "av.number_value",
            "BOOLEAN": "av.boolean_value",
            "DATE": "av.date_value",
            "DATETIME": "av.datetime_value",
        }[field["field_type"]]
        base = "EXISTS (SELECT 1 FROM contact_attribute_values av WHERE av.contact_id = c.id AND av.field_id = ?)"
        if operator == "IS_PRESENT":
            return base, [field_id]
        if operator == "IS_MISSING":
            return f"NOT ({base})", [field_id]
        comparison, values = self._comparison(column, operator, value, field["field_type"])
        match = (
            "EXISTS (SELECT 1 FROM contact_attribute_values av "
            "WHERE av.contact_id = c.id AND av.field_id = ? AND av.value_type = ? AND "
            + comparison
            + ")"
        )
        params = [field_id, field["field_type"], *values]
        return f"CASE WHEN {base} THEN CASE WHEN {match} THEN 1 ELSE 0 END ELSE NULL END", [
            field_id,
            *params,
        ]

    @staticmethod
    def _comparison(
        column: str, operator: str, value: Any, field_type: str
    ) -> tuple[str, list[Any]]:
        if operator == "EQUALS":
            return f"{column} = ?", [value]
        if operator == "NOT_EQUALS":
            return f"{column} <> ?", [value]
        if operator == "CONTAINS":
            if value == "":
                return "1", []
            return f"instr({column}, ?) > 0", [value]
        if operator == "STARTS_WITH":
            if value == "":
                return "1", []
            return f"substr({column}, 1, length(?)) = ?", [value, value]
        if operator == "ENDS_WITH":
            if value == "":
                return "1", []
            return f"substr({column}, -length(?)) = ?", [value, value]
        if operator == "IN":
            placeholders = ", ".join("?" for _ in value)
            return f"{column} IN ({placeholders})", list(value)
        symbols = {
            "GREATER_THAN": ">",
            "LESS_THAN": "<",
            "GREATER_THAN_OR_EQUAL": ">=",
            "LESS_THAN_OR_EQUAL": "<=",
        }
        if operator in symbols:
            return f"{column} {symbols[operator]} ?", [value]
        raise SegmentationError(f"unsupported {field_type} operator")

    def compile(
        self, definition: dict[str, Any], *, canonical_numbers: bool = False
    ) -> CompiledFilter:
        canonical = self.canonical_definition(
            definition,
            canonical_numbers=canonical_numbers,
            include_archived=canonical_numbers,
        )
        with self.store.connect() as connection:
            sql, parameters = self._compile_node(canonical["root"], self._field_map(connection))
        return CompiledFilter(sql, tuple(parameters), canonical, _hash_definition(canonical))

    def preview(
        self,
        segment_id: str,
        *,
        revision_id: str | None = None,
        offset: int = 0,
        limit: int = 100,
        evaluation_time: datetime | None = None,
    ) -> dict[str, Any]:
        limit = max(1, min(limit, 100))
        offset = max(0, offset)
        segment = self.get_segment(segment_id, revision_id)
        compiled = self.compile(segment["definition"], canonical_numbers=True)
        evaluated_at = (evaluation_time or self.now_fn()).astimezone(UTC).isoformat()
        with self.store.connect() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM contacts c WHERE {compiled.sql}", compiled.parameters
            ).fetchone()[0]
            rows = connection.execute(
                f"SELECT c.* FROM contacts c WHERE {compiled.sql} ORDER BY c.normalized_email LIMIT ? OFFSET ?",
                (*compiled.parameters, limit, offset),
            ).fetchall()
        return {
            "segment_id": segment_id,
            "revision_id": segment["revision"]["id"],
            "revision_number": segment["revision"]["revision_number"],
            "definition_hash": compiled.definition_hash,
            "evaluated_at": evaluated_at,
            "evaluation_clock": evaluated_at,
            "matched_count": total,
            "items": [_row_dict(row) for row in rows],
            "offset": offset,
            "limit": limit,
            "next_offset": offset + limit if offset + limit < total else None,
            "explanation": compiled.definition["root"],
        }

    def set_membership(
        self,
        list_id: str,
        contact_id: str,
        action: str,
        *,
        source: str = "segmentation",
        actor: str = "local-operator",
        reason: str = "operator request",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if action not in {"add", "remove"}:
            raise SegmentationError("membership action must be add or remove")
        key = idempotency_key or str(uuid.uuid4())
        with self.store.transaction(immediate=True) as connection:
            if (
                connection.execute("SELECT 1 FROM lists WHERE id = ?", (list_id,)).fetchone()
                is None
            ):
                raise SegmentationError("list was not found", "list_not_found")
            if (
                connection.execute("SELECT 1 FROM contacts WHERE id = ?", (contact_id,)).fetchone()
                is None
            ):
                raise SegmentationError("contact was not found", "contact_not_found")
            if connection.execute(
                "SELECT 1 FROM list_membership_history WHERE idempotency_key = ?", (key,)
            ).fetchone():
                return {"changed": False, "idempotency_key": key}
            exists = (
                connection.execute(
                    "SELECT 1 FROM list_memberships WHERE list_id = ? AND contact_id = ?",
                    (list_id, contact_id),
                ).fetchone()
                is not None
            )
            changed = (action == "add" and not exists) or (action == "remove" and exists)
            if changed:
                if action == "add":
                    connection.execute(
                        "INSERT INTO list_memberships(list_id, contact_id, added_at) VALUES (?, ?, ?)",
                        (list_id, contact_id, utc_now()),
                    )
                else:
                    connection.execute(
                        "DELETE FROM list_memberships WHERE list_id = ? AND contact_id = ?",
                        (list_id, contact_id),
                    )
                connection.execute(
                    "INSERT INTO list_membership_history(id, list_id, contact_id, action, occurred_at, source, actor, idempotency_key, reason) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(uuid.uuid4()),
                        list_id,
                        contact_id,
                        action,
                        utc_now(),
                        source,
                        actor,
                        key,
                        reason,
                    ),
                )
        return {"changed": changed, "idempotency_key": key}

    def validate_import_rows(
        self,
        rows: list[dict[str, Any]],
        *,
        mappings: dict[str, str],
    ) -> dict[str, Any]:
        """Validate a bounded import without mutating contacts or memberships."""

        if len(rows) > 10_000:
            raise SegmentationError("structured imports are limited to 10,000 rows", "import_limit")
        with self.store.connect() as connection:
            fields = {
                row["id"]: _row_dict(row)
                for row in connection.execute(
                    "SELECT * FROM contact_field_definitions WHERE status = 'active'"
                )
            }
        mapped_fields: dict[str, dict[str, Any]] = {}
        for source_key, field_id in mappings.items():
            if field_id == "contact.email":
                mapped_fields[source_key] = {"id": field_id, "field_type": "TEXT"}
            elif field_id in fields:
                mapped_fields[source_key] = fields[field_id]
            else:
                raise SegmentationError(
                    f"mapping for {source_key} references an unknown field", "mapping_invalid"
                )
        email_key = next(
            (key for key, field_id in mappings.items() if field_id == "contact.email"), "email"
        )
        invalid: list[dict[str, Any]] = []
        for position, row in enumerate(rows, start=1):
            email = row.get(email_key) or row.get("email")
            if not isinstance(email, str) or not validate_email(normalize_email(email)).valid:
                invalid.append(
                    {"row": position, "field": email_key, "reason": "valid email is required"}
                )
            for source_key, field in mapped_fields.items():
                if field["id"] == "contact.email" or source_key not in row:
                    continue
                try:
                    value = _parse_value(
                        field["field_type"], row[source_key], f"row {position} {source_key}"
                    )
                    if field.get("controlled_values_json") and value not in json.loads(
                        field["controlled_values_json"]
                    ):
                        raise SegmentationError(
                            f"row {position} {source_key} is not an allowed value"
                        )
                except SegmentationError as error:
                    invalid.append({"row": position, "field": source_key, "reason": str(error)})
        return {"records": len(rows), "invalid": invalid}

    def import_structured(
        self,
        rows: list[dict[str, Any]],
        *,
        mappings: dict[str, str],
        list_id: str,
        source_ref: str = "structured-import",
        overwrite: bool = False,
        actor: str = "local-operator",
    ) -> dict[str, Any]:
        if len(rows) > 10_000:
            raise SegmentationError("structured imports are limited to 10,000 rows", "import_limit")
        with self.store.connect() as connection:
            if (
                connection.execute("SELECT 1 FROM lists WHERE id = ?", (list_id,)).fetchone()
                is None
            ):
                raise SegmentationError("list was not found", "list_not_found")
        if not rows:
            return {
                "records": 0,
                "created": 0,
                "filled": 0,
                "preserved": 0,
                "conflicts": [],
                "invalid": [],
            }
        with self.store.connect() as connection:
            fields = {
                row["id"]: _row_dict(row)
                for row in connection.execute(
                    "SELECT * FROM contact_field_definitions WHERE status = 'active'"
                )
            }
        mapped_fields = {}
        for source_key, field_id in mappings.items():
            if field_id == "contact.email":
                mapped_fields[source_key] = {"id": field_id, "field_type": "TEXT"}
                continue
            if field_id not in fields:
                raise SegmentationError(f"mapping for {source_key} references an unknown field")
            mapped_fields[source_key] = fields[field_id]
        conflicts: list[dict[str, Any]] = []
        invalid: list[dict[str, Any]] = []
        counts = {"records": 0, "created": 0, "filled": 0, "preserved": 0}
        for position, row in enumerate(rows, start=1):
            counts["records"] += 1
            email_key = next(
                (key for key, field_id in mappings.items() if field_id == "contact.email"), "email"
            )
            email = row.get(email_key) or row.get("email")
            if not isinstance(email, str):
                invalid.append(
                    {"row": position, "field": email_key, "reason": "valid email is required"}
                )
                continue
            normalized = normalize_email(email)
            if not validate_email(normalized).valid:
                invalid.append(
                    {"row": position, "field": email_key, "reason": "valid email is required"}
                )
                continue
            contact = self.store.contact_by_email(normalized)
            contact_id = self.store.upsert_contact(
                normalized,
                display_name=str(row.get("display_name", "")),
                source_file=source_ref,
                source_record_position=position,
                original_input=email,
            )
            if contact is None:
                counts["created"] += 1
            self.set_membership(
                list_id,
                contact_id,
                "add",
                source=source_ref,
                actor=actor,
                reason="structured import",
                idempotency_key=f"{source_ref}:{position}:{contact_id}",
            )
            for source_key, field in mapped_fields.items():
                if field["id"] == "contact.email":
                    continue
                raw = row.get(source_key)
                try:
                    value = _parse_value(field["field_type"], raw, f"row {position} {source_key}")
                    if field["controlled_values_json"] and value not in json.loads(
                        field["controlled_values_json"]
                    ):
                        raise SegmentationError(
                            f"row {position} {source_key} is not an allowed value"
                        )
                except SegmentationError as error:
                    invalid.append({"row": position, "field": source_key, "reason": str(error)})
                    continue
                columns = _value_columns(field["field_type"], value)
                with self.store.transaction(immediate=True) as connection:
                    prior_conflict = connection.execute(
                        "SELECT id, disposition FROM contact_attribute_conflicts "
                        "WHERE source_ref = ? AND source_record_position = ? "
                        "AND contact_id = ? AND field_id = ?",
                        (source_ref, position, contact_id, field["id"]),
                    ).fetchone()
                    if prior_conflict is not None:
                        conflicts.append(
                            {
                                "id": prior_conflict["id"],
                                "row": position,
                                "field": field["machine_key"],
                                "disposition": prior_conflict["disposition"],
                            }
                        )
                        continue
                    existing = connection.execute(
                        "SELECT * FROM contact_attribute_values WHERE contact_id = ? AND field_id = ?",
                        (contact_id, field["id"]),
                    ).fetchone()
                    old_value = (
                        self._stored_value(existing, field["field_type"]) if existing else None
                    )
                    if existing and old_value != value:
                        disposition = "overwritten" if overwrite else "preserved"
                        conflict_id = str(uuid.uuid4())
                        connection.execute(
                            "INSERT INTO contact_attribute_conflicts(id, contact_id, field_id, source_kind, source_ref, source_record_position, existing_summary, incoming_summary, disposition, reason, actor, created_at) "
                            "VALUES (?, ?, ?, 'structured', ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                conflict_id,
                                contact_id,
                                field["id"],
                                source_ref,
                                position,
                                str(old_value)[:MAX_COMPARISON],
                                str(value)[:MAX_COMPARISON],
                                disposition,
                                "explicit overwrite" if overwrite else "populated value preserved",
                                actor,
                                utc_now(),
                            ),
                        )
                        conflicts.append(
                            {
                                "id": conflict_id,
                                "row": position,
                                "field": field["machine_key"],
                                "disposition": disposition,
                            }
                        )
                        if not overwrite:
                            counts["preserved"] += 1
                            continue
                    if existing is not None and old_value != value and overwrite:
                        connection.execute(
                            "UPDATE contact_attribute_values SET value_type = ?, text_value = ?, number_value = ?, boolean_value = ?, date_value = ?, datetime_value = ?, source_kind = 'structured', source_ref = ?, source_record_position = ?, provenance_json = '{}', updated_at = ?, updated_by = ? WHERE contact_id = ? AND field_id = ?",
                            (
                                field["field_type"],
                                columns["text_value"],
                                columns["number_value"],
                                columns["boolean_value"],
                                columns["date_value"],
                                columns["datetime_value"],
                                source_ref,
                                position,
                                utc_now(),
                                actor,
                                contact_id,
                                field["id"],
                            ),
                        )
                    elif existing is None:
                        connection.execute(
                            "INSERT INTO contact_attribute_values(contact_id, field_id, value_type, text_value, number_value, boolean_value, date_value, datetime_value, source_kind, source_ref, source_record_position, provenance_json, created_at, updated_at, updated_by) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'structured', ?, ?, '{}', ?, ?, ?)",
                            (
                                contact_id,
                                field["id"],
                                field["field_type"],
                                columns["text_value"],
                                columns["number_value"],
                                columns["boolean_value"],
                                columns["date_value"],
                                columns["datetime_value"],
                                source_ref,
                                position,
                                utc_now(),
                                utc_now(),
                                actor,
                            ),
                        )
                        if existing is None:
                            counts["filled"] += 1
        return {**counts, "conflicts": conflicts, "invalid": invalid}

    @staticmethod
    def _stored_value(row: sqlite3.Row, field_type: str) -> Any:
        column = {
            "TEXT": "text_value",
            "NUMBER": "number_value",
            "BOOLEAN": "boolean_value",
            "DATE": "date_value",
            "DATETIME": "datetime_value",
        }[field_type]
        value = row[column]
        return bool(value) if field_type == "BOOLEAN" else value

    def prepare_campaign(
        self,
        campaign_id: str,
        *,
        source_type: str | None = None,
        source_id: str | None = None,
        revision_id: str | None = None,
        run_key: str = "primary",
        eligibility_service=None,
    ) -> dict[str, Any]:
        campaign = self.store.get_campaign(campaign_id)
        source_type = source_type or campaign["audience_source_type"]
        source_id = source_id or campaign["audience_source_id"]
        revision_id = revision_id or campaign["audience_revision_id"]
        if source_type not in {"list", "segment"} or not source_id:
            raise SegmentationError("campaign audience source is incomplete", "audience_invalid")
        existing = self._audience(campaign_id, run_key)
        if existing and (
            existing["source_type"] != source_type
            or existing["source_id"] != source_id
            or existing["segment_revision_id"] != revision_id
        ):
            raise SegmentationError(
                "campaign audience conflicts with the existing run source or revision",
                "audience_conflict",
            )
        if existing and existing["status"] == "prepared":
            if (
                existing["source_type"] != source_type
                or existing["source_id"] != source_id
                or existing["segment_revision_id"] != revision_id
            ):
                raise SegmentationError(
                    "prepared campaign audience conflicts with requested source or revision",
                    "audience_conflict",
                )
            return existing
        if eligibility_service is None:
            from mailforge.application.eligibility import EligibilityService

            eligibility_service = EligibilityService(self.store)
        now = utc_now()
        audience_id = str(uuid.uuid4())
        try:
            with self.store.transaction(immediate=True) as connection:
                if source_type == "segment":
                    segment = connection.execute(
                        "SELECT * FROM segments WHERE id = ?", (source_id,)
                    ).fetchone()
                    if segment is None:
                        raise SegmentationError("segment was not found", "segment_not_found")
                    selected_revision_id = revision_id or segment["current_revision_id"]
                    revision = connection.execute(
                        "SELECT * FROM segment_revisions WHERE id = ? AND segment_id = ?",
                        (selected_revision_id, source_id),
                    ).fetchone()
                    if revision is None:
                        raise SegmentationError(
                            "segment revision was not found", "revision_not_found"
                        )
                    compiled = self.compile(
                        json.loads(revision["definition_json"]), canonical_numbers=True
                    )
                    contacts = connection.execute(
                        f"SELECT c.* FROM contacts c WHERE {compiled.sql} ORDER BY c.normalized_email",
                        compiled.parameters,
                    ).fetchall()
                    revision_id = revision["id"]
                    definition_hash = compiled.definition_hash
                    matched = len(contacts)
                else:
                    contacts = connection.execute(
                        "SELECT c.* FROM contacts c JOIN list_memberships m ON m.contact_id = c.id "
                        "WHERE m.list_id = ? ORDER BY c.normalized_email",
                        (source_id,),
                    ).fetchall()
                    definition_hash = None
                    matched = len(contacts)
                eligible: list[tuple[sqlite3.Row, DeliveryState, str]] = []
                for contact in contacts:
                    decision = eligibility_service.evaluate_contact(
                        contact, campaign=campaign, connection=connection
                    )
                    state = DeliveryState.ELIGIBLE_PENDING
                    if decision.status.value == "excluded":
                        state = DeliveryState.SUPPRESSED
                    elif decision.status.value == "invalid":
                        state = DeliveryState.NOT_ELIGIBLE
                    eligible.append((contact, state, decision.reason))
                current = connection.execute(
                    "SELECT * FROM campaign_audiences WHERE campaign_id = ? AND run_key = ?",
                    (campaign_id, run_key),
                ).fetchone()
                if current and current["status"] == "prepared":
                    if (
                        current["source_type"] != source_type
                        or current["source_id"] != source_id
                        or current["segment_revision_id"] != revision_id
                    ):
                        raise SegmentationError(
                            "prepared campaign audience conflicts with requested source or revision",
                            "audience_conflict",
                        )
                    return _row_dict(current)
                if current:
                    connection.execute(
                        "UPDATE campaign_audiences SET source_type = ?, source_id = ?, segment_revision_id = ?, definition_hash = ?, evaluated_at = ?, evaluation_clock = ?, matched_count = ?, prepared_count = 0, excluded_count = 0, status = 'preparing', failure_reason = NULL, updated_at = ? WHERE id = ?",
                        (
                            source_type,
                            source_id,
                            revision_id,
                            definition_hash,
                            now,
                            now,
                            matched,
                            now,
                            current["id"],
                        ),
                    )
                    audience_id = current["id"]
                else:
                    connection.execute(
                        "INSERT INTO campaign_audiences(id, campaign_id, run_key, source_type, source_id, segment_revision_id, definition_hash, evaluated_at, evaluation_clock, matched_count, status, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'preparing', ?, ?)",
                        (
                            audience_id,
                            campaign_id,
                            run_key,
                            source_type,
                            source_id,
                            revision_id,
                            definition_hash,
                            now,
                            now,
                            matched,
                            now,
                            now,
                        ),
                    )
                if run_key == "primary":
                    connection.execute(
                        "DELETE FROM delivery_intents WHERE campaign_id = ?", (campaign_id,)
                    )
                else:
                    connection.execute(
                        "DELETE FROM campaign_audience_recipients WHERE audience_id = ?",
                        (audience_id,),
                    )
                excluded = 0
                for contact, state, reason in eligible:
                    if state is not DeliveryState.ELIGIBLE_PENDING:
                        excluded += 1
                    if run_key == "primary":
                        intent_id = str(
                            uuid.uuid5(uuid.NAMESPACE_URL, f"{campaign_id}:{contact['id']}")
                        )
                        connection.execute(
                            "INSERT INTO delivery_intents(id, campaign_id, contact_id, normalized_email, state, eligibility_reason, prepared_at, updated_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                intent_id,
                                campaign_id,
                                contact["id"],
                                contact["normalized_email"],
                                state.value,
                                reason,
                                now,
                                now,
                            ),
                        )
                    else:
                        connection.execute(
                            "INSERT INTO campaign_audience_recipients(audience_id, contact_id, normalized_email, state, eligibility_reason, prepared_at) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (
                                audience_id,
                                contact["id"],
                                contact["normalized_email"],
                                state.value,
                                reason,
                                now,
                            ),
                        )
                connection.execute(
                    "UPDATE campaign_audiences SET prepared_count = ?, excluded_count = ?, status = 'prepared', updated_at = ? WHERE id = ?",
                    (len(eligible), excluded, now, audience_id),
                )
                connection.execute(
                    "UPDATE campaigns SET audience_source_type = ?, audience_source_id = ?, audience_revision_id = ?, audience_run_key = ?, updated_at = ? WHERE id = ?",
                    (source_type, source_id, revision_id, run_key, now, campaign_id),
                )
        except Exception:
            with self.store.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE campaign_audiences SET status = 'failed', failure_reason = ?, updated_at = ? WHERE id = ? AND status = 'preparing'",
                    ("audience preparation failed", utc_now(), audience_id),
                )
            raise
        return self._audience(campaign_id, run_key) or {}

    def _audience(self, campaign_id: str, run_key: str) -> dict[str, Any] | None:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM campaign_audiences WHERE campaign_id = ? AND run_key = ?",
                (campaign_id, run_key),
            ).fetchone()
        return _row_dict(row) if row else None

    def get_audience(self, campaign_id: str, run_key: str = "primary") -> dict[str, Any] | None:
        """Return one durable audience snapshot without exposing storage internals."""

        return self._audience(campaign_id, run_key)

    def audience_recipients(
        self, campaign_id: str, run_key: str = "primary"
    ) -> list[dict[str, Any]]:
        audience = self._audience(campaign_id, run_key)
        if audience is None:
            return []
        if run_key == "primary":
            return [_row_dict(row) for row in self.store.list_intents(campaign_id)]
        with self.store.connect() as connection:
            return [
                _row_dict(row)
                for row in connection.execute(
                    "SELECT * FROM campaign_audience_recipients WHERE audience_id = ? ORDER BY normalized_email",
                    (audience["id"],),
                )
            ]
