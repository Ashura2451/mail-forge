"""Bounded, read-only external data acquisition for Mailforge.

Connection profiles are durable configuration.  Consequential use is always
through a completed, checksummed JSONL snapshot owned by Mailforge.  Adapters
never accept arbitrary SQL and never write to their source.
"""

# SQL and adapter statements remain readable as contiguous fragments.
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
import uuid
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote

from mailforge.infrastructure.campaign_store import CampaignStore
from mailforge.infrastructure.credentials import (
    CredentialResolver,
    EnvironmentCredentialResolver,
    validate_credential_reference,
)

MAX_PREVIEW_ROWS = 500
MAX_SNAPSHOT_ROWS = 100_000
MAX_PREVIEW_BYTES = 8 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 256 * 1024 * 1024
POSTGRES_FETCH_BATCH = 1_000
MAX_SOURCE_BYTES = 1_073_741_824
MAX_XLSX_BYTES = 256 * 1024 * 1024
MAX_SCHEMA_OBJECTS = 500
MAX_SCHEMA_FIELDS = 200
MAX_FILTERS = 20
MAX_FIELD_NAME = 200
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,127}$")
SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
ALLOWED_SOURCE_TYPES = {"sqlite", "postgresql", "xlsx"}
ALLOWED_POSTGRES_CONFIG = {
    "host",
    "port",
    "database",
    "user",
    "sslmode",
    "connect_timeout",
    "statement_timeout_ms",
}
ALLOWED_FILE_CONFIG = {"source_name"}
ALLOWED_FILTERS = {"equals", "contains", "starts_with", "ends_with", "is_null", "not_null"}


class DataConnectionError(ValueError):
    def __init__(self, message: str, code: str = "data_connection_invalid"):
        super().__init__(message)
        self.code = code


class SnapshotCanceled(DataConnectionError):
    def __init__(self) -> None:
        super().__init__("snapshot extraction was canceled", "snapshot_canceled")


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_filename(value: str, fallback: str) -> str:
    name = Path(value or fallback).name
    clean = SAFE_FILENAME.sub("-", name).strip(".-")
    return (clean or fallback)[:160]


def _safe_identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise DataConnectionError(f"{label} is not a safe identifier", "selection_invalid")
    return value


def _safe_field_name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_FIELD_NAME or "\x00" in value:
        raise DataConnectionError(f"{label} is not a valid field name", "selection_invalid")
    return value


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool, float)):
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return None
    return str(value)


def _normalized_type(value: Any, source_type: str | None = None) -> str:
    source = (source_type or "").lower()
    if value is None:
        if source:
            if any(token in source for token in ("date", "time")):
                return "datetime" if "time" in source else "date"
            if any(token in source for token in ("int", "serial")):
                return "integer"
            if any(token in source for token in ("decimal", "numeric", "real", "double", "float")):
                return "number"
            if "bool" in source:
                return "boolean"
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int) and not isinstance(value, bool):
        return "integer"
    if isinstance(value, (float, Decimal)):
        return "number"
    if isinstance(value, datetime):
        return "datetime"
    if isinstance(value, date):
        return "date"
    if isinstance(value, bytes):
        return "binary"
    if any(token in source for token in ("date", "time")):
        return "datetime" if "time" in source else "date"
    if any(token in source for token in ("int", "serial")):
        return "integer"
    if any(token in source for token in ("decimal", "numeric", "real", "double", "float")):
        return "number"
    if any(token in source for token in ("bool",)):
        return "boolean"
    return "text"


def _redact_config(config: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(config)
    path = result.pop("source_path", None)
    if path:
        result["source_name"] = Path(str(path)).name
    result.pop("password", None)
    result.pop("secret", None)
    result.pop("token", None)
    return result


def _assert_no_secret_values(value: Any, path: str = "config") -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            key_text = str(key).lower()
            if any(token in key_text for token in ("password", "secret", "token", "api_key")):
                raise DataConnectionError(f"{path}.{key} must use credential_ref", "secret_input")
            _assert_no_secret_values(nested, f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _assert_no_secret_values(nested, f"{path}[{index}]")


def _validate_limits(limits: Mapping[str, Any] | None) -> dict[str, int]:
    values = dict(limits or {})
    preview = int(values.get("preview_rows", MAX_PREVIEW_ROWS))
    rows = int(values.get("snapshot_rows", MAX_SNAPSHOT_ROWS))
    if not 1 <= preview <= MAX_PREVIEW_ROWS:
        raise DataConnectionError("preview_rows must be between 1 and 500", "limits_invalid")
    if not 1 <= rows <= MAX_SNAPSHOT_ROWS:
        raise DataConnectionError("snapshot_rows must be between 1 and 100000", "limits_invalid")
    return {"preview_rows": preview, "snapshot_rows": rows}


def _validate_filters(selection: Mapping[str, Any], fields: set[str]) -> list[dict[str, Any]]:
    filters = selection.get("filters", [])
    if not isinstance(filters, list) or len(filters) > MAX_FILTERS:
        raise DataConnectionError("filters are limited to 20 entries", "selection_invalid")
    checked: list[dict[str, Any]] = []
    for item in filters:
        if not isinstance(item, Mapping):
            raise DataConnectionError("each filter must be an object", "selection_invalid")
        field = _safe_field_name(item.get("field"), "filter field")
        if field not in fields:
            raise DataConnectionError(f"unknown filter field: {field}", "selection_invalid")
        op = item.get("op")
        if op not in ALLOWED_FILTERS:
            raise DataConnectionError("unsupported filter operator", "selection_invalid")
        value = item.get("value")
        if op not in {"is_null", "not_null"}:
            if isinstance(value, (dict, list)) or len(str(value)) > 1_000:
                raise DataConnectionError("filter values are bounded scalars", "selection_invalid")
        checked.append({"field": field, "op": op, "value": value})
    return checked


def _validate_selection(
    selection: Mapping[str, Any], fields: list[dict[str, Any]]
) -> dict[str, Any]:
    field_names = {item["name"] for item in fields}
    selected = selection.get("columns")
    if selected is None:
        columns = [item["name"] for item in fields]
    elif isinstance(selected, list) and 0 < len(selected) <= 200:
        columns = [_safe_field_name(item, "column") for item in selected]
        if any(item not in field_names for item in columns):
            raise DataConnectionError("selection contains an unknown column", "selection_invalid")
        if len(set(columns)) != len(columns):
            raise DataConnectionError("selection contains duplicate columns", "selection_invalid")
    else:
        raise DataConnectionError(
            "columns must contain between 1 and 200 fields", "selection_invalid"
        )
    filters = _validate_filters(selection, field_names)
    order_by = selection.get("order_by")
    checked_order: dict[str, str] | None = None
    if order_by is not None:
        if not isinstance(order_by, Mapping):
            raise DataConnectionError("order_by must be an object", "selection_invalid")
        field = _safe_field_name(order_by.get("field"), "order field")
        if field not in field_names:
            raise DataConnectionError("order field is not available", "selection_invalid")
        direction = str(order_by.get("direction", "asc")).lower()
        if direction not in {"asc", "desc"}:
            raise DataConnectionError("order direction must be asc or desc", "selection_invalid")
        checked_order = {"field": field, "direction": direction}
    return {"columns": columns, "filters": filters, "order_by": checked_order}


def _filter_sql(filters: list[dict[str, Any]], quote_name) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    values: list[Any] = []
    for item in filters:
        field = quote_name(item["field"])
        op = item["op"]
        if op == "is_null":
            clauses.append(f"{field} IS NULL")
        elif op == "not_null":
            clauses.append(f"{field} IS NOT NULL")
        elif op == "contains":
            clauses.append(f"CAST({field} AS TEXT) LIKE ?")
            values.append(f"%{item['value']}%")
        elif op == "starts_with":
            clauses.append(f"CAST({field} AS TEXT) LIKE ?")
            values.append(f"{item['value']}%")
        elif op == "ends_with":
            clauses.append(f"CAST({field} AS TEXT) LIKE ?")
            values.append(f"%{item['value']}")
        else:
            clauses.append(f"{field} {'!=' if op == 'not_equals' else '='} ?")
            values.append(item.get("value"))
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", values


@dataclass(frozen=True)
class Profile:
    id: str
    name: str
    source_type: str
    config: dict[str, Any]
    credential_ref: str | None
    selection: dict[str, Any]
    limits: dict[str, Any]
    enabled: bool


class _Adapter:
    consistency = "source_read_transaction"

    def schema(self) -> dict[str, Any]:
        raise NotImplementedError

    def test(self) -> dict[str, Any]:
        self.schema()
        return {"reachable": True, "read_only": True}

    def preview(self, selection: Mapping[str, Any], limit: int) -> dict[str, Any]:
        schema = self.schema()
        rows: list[dict[str, Any]] = []
        preview_bytes = 0
        for row in self.iter_records(selection, limit):
            preview_bytes += len(_json(row).encode("utf-8"))
            if preview_bytes > MAX_PREVIEW_BYTES:
                raise DataConnectionError("preview exceeds the 8 MiB limit", "preview_limit")
            rows.append(row)
        return {"schema": schema, "rows": rows, "limit": limit, "read_only": True}

    def iter_records(self, selection: Mapping[str, Any], limit: int) -> Iterator[dict[str, Any]]:
        raise NotImplementedError


class _SQLiteAdapter(_Adapter):
    def __init__(self, path: Path):
        if path.is_symlink() or not path.is_file():
            raise DataConnectionError("SQLite source is not a regular file", "source_missing")
        self.path = path.resolve()

    def _connect(self) -> sqlite3.Connection:
        uri = f"file:{quote(str(self.path), safe='/')}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=3)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=3000")
        return connection

    def _objects(self, connection: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT name, type FROM sqlite_master WHERE type IN ('table','view') "
            f"AND name NOT LIKE 'sqlite_%' ORDER BY name LIMIT {MAX_SCHEMA_OBJECTS + 1}"
        ).fetchall()
        if len(rows) > MAX_SCHEMA_OBJECTS:
            raise DataConnectionError("source has too many tables or views", "schema_limit")
        objects = []
        for row in rows:
            name = row["name"]
            columns = connection.execute(f"PRAGMA table_info({_quote_identifier(name)})").fetchall()
            if len(columns) > MAX_SCHEMA_FIELDS:
                raise DataConnectionError("table has too many fields", "schema_limit")
            fields = [
                {
                    "name": item["name"],
                    "source_type": item["type"] or "text",
                    "normalized_type": _normalized_type(None, item["type"]),
                    "nullable": not bool(item["notnull"]),
                    "primary_key": bool(item["pk"]),
                }
                for item in columns
            ]
            objects.append(
                {
                    "name": name,
                    "kind": row["type"],
                    "fields": fields,
                    "stable_ordering": next(
                        (item["name"] for item in fields if item["primary_key"]), None
                    ),
                }
            )
        return objects

    def schema(self) -> dict[str, Any]:
        try:
            with self._connect() as connection:
                objects = self._objects(connection)
        except sqlite3.Error as error:
            raise DataConnectionError(
                f"SQLite schema discovery failed: {error}", "source_unavailable"
            ) from error
        return {"source_type": "sqlite", "objects": objects}

    def _selection(self, selection: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        with self._connect() as connection:
            objects = self._objects(connection)
        table = selection.get("table")
        if not isinstance(table, str):
            raise DataConnectionError("a SQLite table or view is required", "selection_invalid")
        target = next((item for item in objects if item["name"] == table), None)
        if target is None:
            raise DataConnectionError(
                "the selected SQLite object is unavailable", "selection_invalid"
            )
        return target, _validate_selection(selection, target["fields"])

    def iter_records(self, selection: Mapping[str, Any], limit: int) -> Iterator[dict[str, Any]]:
        target, checked = self._selection(selection)
        columns = checked["columns"]
        where, values = _filter_sql(checked["filters"], _quote_identifier)
        order = checked["order_by"]
        if order is None and target.get("stable_ordering"):
            order = {"field": target["stable_ordering"], "direction": "asc"}
        order_sql = (
            f" ORDER BY {_quote_identifier(order['field'])} {order['direction'].upper()}"
            if order
            else ""
        )
        query = (
            f"SELECT {', '.join(_quote_identifier(item) for item in columns)} "
            f"FROM {_quote_identifier(target['name'])}{where}{order_sql} LIMIT ?"
        )
        try:
            with self._connect() as connection:
                for row_number, row in enumerate(
                    connection.execute(query, (*values, min(limit, MAX_SNAPSHOT_ROWS + 1))), 1
                ):
                    errors: list[dict[str, Any]] = []
                    values_out: dict[str, Any] = {}
                    for field in columns:
                        value = row[field]
                        if isinstance(value, bytes):
                            errors.append({"field": field, "reason": "binary value is unsupported"})
                            value = None
                        values_out[field] = _json_value(value)
                    values_out["__source_row"] = row_number
                    values_out["__source_id"] = (
                        f"sqlite:{self.path.name}:{target['name']}:{row_number}"
                    )
                    values_out["__provenance"] = {
                        "source_type": "sqlite",
                        "source_name": self.path.name,
                        "object": target["name"],
                        "row": row_number,
                    }
                    if errors:
                        values_out["__conversion_errors"] = errors
                    yield values_out
        except sqlite3.Error as error:
            raise DataConnectionError(
                f"SQLite preview failed: {error}", "source_query_failed"
            ) from error


class _PostgresAdapter(_Adapter):
    consistency = "postgresql_read_only_transaction"

    def __init__(
        self, config: Mapping[str, Any], credential_ref: str, resolver: CredentialResolver
    ):
        self.config = dict(config)
        self.credential_ref = credential_ref
        self.resolver = resolver

    def _connect(self):
        try:
            import psycopg
        except ImportError as error:
            raise DataConnectionError(
                "PostgreSQL support is not installed", "driver_missing"
            ) from error
        try:
            password = self.resolver.resolve(self.credential_ref).secret
            return psycopg.connect(
                host=self.config["host"],
                port=int(self.config.get("port", 5432)),
                dbname=self.config["database"],
                user=self.config["user"],
                password=password,
                sslmode=self.config.get("sslmode", "verify-full"),
                connect_timeout=min(int(self.config.get("connect_timeout", 5)), 30),
                options=f"-c statement_timeout={min(int(self.config.get('statement_timeout_ms', 30000)), 120000)} -c default_transaction_read_only=on",
            )
        except Exception as error:
            if isinstance(error, DataConnectionError):
                raise
            raise DataConnectionError(
                "PostgreSQL connection failed", "source_unavailable"
            ) from error

    @staticmethod
    def _type(source_type: str) -> str:
        return _normalized_type(None, source_type)

    def schema(self) -> dict[str, Any]:
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT table_schema, table_name, table_type FROM information_schema.tables "
                    "WHERE table_schema NOT IN ('pg_catalog','information_schema') "
                    "AND table_type IN ('BASE TABLE','VIEW') ORDER BY table_schema, table_name LIMIT %s",
                    (MAX_SCHEMA_OBJECTS + 1,),
                ).fetchmany(MAX_SCHEMA_OBJECTS + 1)
                if len(rows) > MAX_SCHEMA_OBJECTS:
                    raise DataConnectionError("source has too many tables or views", "schema_limit")
                objects = []
                for row in rows:
                    columns = connection.execute(
                        "SELECT column_name, data_type, is_nullable, ordinal_position "
                        "FROM information_schema.columns WHERE table_schema=%s AND table_name=%s "
                        "ORDER BY ordinal_position LIMIT %s",
                        (row[0], row[1], MAX_SCHEMA_FIELDS + 1),
                    ).fetchmany(MAX_SCHEMA_FIELDS + 1)
                    if len(columns) > MAX_SCHEMA_FIELDS:
                        raise DataConnectionError("table has too many fields", "schema_limit")
                    objects.append(
                        {
                            "schema": row[0],
                            "name": row[1],
                            "kind": "view" if row[2] == "VIEW" else "table",
                            "fields": [
                                {
                                    "name": item[0],
                                    "source_type": item[1],
                                    "normalized_type": self._type(item[1]),
                                    "nullable": item[2] == "YES",
                                    "primary_key": False,
                                }
                                for item in columns
                            ],
                            "stable_ordering": None,
                        }
                    )
                return {"source_type": "postgresql", "objects": objects}
        except DataConnectionError:
            raise
        except Exception as error:
            raise DataConnectionError(
                "PostgreSQL schema discovery failed", "source_query_failed"
            ) from error

    def _target(self, selection: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        schema = selection.get("schema", "public")
        table = selection.get("table")
        _safe_identifier(schema, "schema")
        _safe_identifier(table, "table")
        metadata = self.schema()
        target = next(
            (
                item
                for item in metadata["objects"]
                if item["schema"] == schema and item["name"] == table
            ),
            None,
        )
        if target is None:
            raise DataConnectionError(
                "the selected PostgreSQL object is unavailable", "selection_invalid"
            )
        return target, _validate_selection(selection, target["fields"])

    def iter_records(self, selection: Mapping[str, Any], limit: int) -> Iterator[dict[str, Any]]:
        target, checked = self._target(selection)
        try:
            from psycopg import sql

            clauses: list[Any] = []
            values: list[Any] = []
            for item in checked["filters"]:
                identifier = sql.Identifier(item["field"])
                op = item["op"]
                if op == "is_null":
                    clauses.append(sql.SQL("{} IS NULL").format(identifier))
                elif op == "not_null":
                    clauses.append(sql.SQL("{} IS NOT NULL").format(identifier))
                else:
                    operator = "LIKE" if op in {"contains", "starts_with", "ends_with"} else "="
                    value = item.get("value")
                    if op == "contains":
                        value = f"%{value}%"
                    elif op == "starts_with":
                        value = f"{value}%"
                    elif op == "ends_with":
                        value = f"%{value}"
                    clauses.append(sql.SQL("{} {} %s").format(identifier, sql.SQL(operator)))
                    values.append(value)
            where = sql.SQL(" WHERE ") + sql.SQL(" AND ").join(clauses) if clauses else sql.SQL("")
            order = checked.get("order_by")
            order_sql = (
                sql.SQL(" ORDER BY {} {} ").format(
                    sql.Identifier(order["field"]), sql.SQL(order["direction"].upper())
                )
                if order
                else sql.SQL("")
            )
            query = sql.SQL("SELECT {} FROM {}.{}{}").format(
                sql.SQL(", ").join(sql.Identifier(item) for item in checked["columns"]),
                sql.Identifier(target["schema"]),
                sql.Identifier(target["name"]),
                where + order_sql + sql.SQL(" LIMIT %s"),
            )
            values.append(min(limit, MAX_SNAPSHOT_ROWS + 1))
            with self._connect() as connection:
                with connection.cursor(name=f"mailforge_{uuid.uuid4().hex}") as cursor:
                    cursor.execute(query, values)
                    remaining = min(limit, MAX_SNAPSHOT_ROWS + 1)
                    row_number = 0
                    while remaining:
                        batch = cursor.fetchmany(min(POSTGRES_FETCH_BATCH, remaining))
                        if not batch:
                            break
                        for row in batch:
                            row_number += 1
                            values_out = {
                                field: _json_value(value)
                                for field, value in zip(checked["columns"], row, strict=False)
                            }
                            values_out["__source_row"] = row_number
                            values_out["__source_id"] = (
                                f"postgresql:{target['schema']}.{target['name']}:{row_number}"
                            )
                            values_out["__provenance"] = {
                                "source_type": "postgresql",
                                "schema": target["schema"],
                                "object": target["name"],
                                "row": row_number,
                            }
                            yield values_out
                        remaining -= len(batch)
        except DataConnectionError:
            raise
        except Exception as error:
            raise DataConnectionError(
                "PostgreSQL read-only query failed", "source_query_failed"
            ) from error


class _XlsxAdapter(_Adapter):
    consistency = "xlsx_file_snapshot"

    def __init__(self, path: Path):
        if path.is_symlink() or not path.is_file():
            raise DataConnectionError("XLSX source is not a regular file", "source_missing")
        if path.suffix.lower() != ".xlsx":
            raise DataConnectionError("only normal .xlsx workbooks are supported", "source_format")
        if path.stat().st_size > MAX_XLSX_BYTES:
            raise DataConnectionError("workbook exceeds the 256 MiB limit", "source_limit")
        self.path = path.resolve()

    def _workbooks(self):
        try:
            from openpyxl import load_workbook
        except ImportError as error:
            raise DataConnectionError("XLSX support is not installed", "driver_missing") from error
        try:
            formulas = load_workbook(self.path, read_only=True, data_only=False, keep_links=False)
            cached = load_workbook(self.path, read_only=True, data_only=True, keep_links=False)
            return formulas, cached
        except Exception as error:
            raise DataConnectionError(
                "XLSX workbook could not be opened", "source_format"
            ) from error

    @staticmethod
    def _headers(sheet, header_row: int) -> list[str]:
        if not 1 <= header_row <= 10_000:
            raise DataConnectionError("header_row must be between 1 and 10000", "selection_invalid")
        row = next(sheet.iter_rows(min_row=header_row, max_row=header_row, values_only=True), ())
        names: list[str] = []
        seen: dict[str, int] = {}
        for index, value in enumerate(row, 1):
            base = str(value).strip() if value not in {None, ""} else f"column_{index}"
            base = SAFE_FILENAME.sub("_", base)[:MAX_FIELD_NAME] or f"column_{index}"
            seen[base] = seen.get(base, 0) + 1
            names.append(base if seen[base] == 1 else f"{base}__{seen[base]}")
        if not names:
            raise DataConnectionError("worksheet has no header row", "source_schema")
        if len(names) > MAX_SCHEMA_FIELDS:
            raise DataConnectionError("worksheet has too many fields", "schema_limit")
        return names

    def schema(self) -> dict[str, Any]:
        formulas, cached = self._workbooks()
        try:
            worksheets = []
            if len(formulas.sheetnames) > MAX_SCHEMA_OBJECTS:
                raise DataConnectionError("workbook has too many worksheets", "schema_limit")
            for name in formulas.sheetnames:
                headers = self._headers(formulas[name], 1)
                samples = list(
                    self._records(formulas[name], cached[name], headers, 1, headers, 100)
                )
                types: dict[str, str] = {}
                for row in samples:
                    for field in headers:
                        value = row.get(field)
                        if value is not None and field not in types:
                            types[field] = _normalized_type(value)
                worksheets.append(
                    {
                        "name": name,
                        "fields": [
                            {
                                "name": field,
                                "source_type": types.get(field, "empty"),
                                "normalized_type": types.get(field, "null"),
                                "nullable": True,
                                "primary_key": False,
                            }
                            for field in headers
                        ],
                        "stable_ordering": "__source_row",
                    }
                )
            return {"source_type": "xlsx", "worksheets": worksheets}
        finally:
            formulas.close()
            cached.close()

    def _records(self, formula_sheet, cached_sheet, headers, header_row, columns, limit):
        for row_number, (formula_row, cached_row) in enumerate(
            zip(
                formula_sheet.iter_rows(min_row=header_row + 1, values_only=False),
                cached_sheet.iter_rows(min_row=header_row + 1, values_only=False),
                strict=False,
            ),
            header_row + 1,
        ):
            if row_number > header_row + limit:
                break
            values: dict[str, Any] = {}
            errors: list[dict[str, Any]] = []
            formula_fields: list[str] = []
            empty = True
            for index, field in enumerate(headers):
                if field not in columns:
                    continue
                formula_value = formula_row[index].value if index < len(formula_row) else None
                cached_value = cached_row[index].value if index < len(cached_row) else None
                is_formula = formula_row[index].data_type == "f"
                if is_formula:
                    formula_fields.append(field)
                    value = cached_value
                    if value is None:
                        errors.append({"field": field, "reason": "formula has no cached value"})
                else:
                    value = formula_value
                if value not in {None, ""}:
                    empty = False
                values[field] = _json_value(value)
            if empty:
                continue
            values["__source_row"] = row_number
            values["__source_id"] = f"xlsx:{self.path.name}:{formula_sheet.title}:{row_number}"
            values["__provenance"] = {
                "source_type": "xlsx",
                "source_name": self.path.name,
                "worksheet": formula_sheet.title,
                "row": row_number,
            }
            if formula_fields:
                values["__formula_fields"] = formula_fields
            if errors:
                values["__conversion_errors"] = errors
            yield values

    def _target(self, selection: Mapping[str, Any]) -> tuple[str, list[str], dict[str, Any]]:
        worksheet = selection.get("worksheet")
        try:
            header_row = int(selection.get("header_row", 1))
        except (TypeError, ValueError) as error:
            raise DataConnectionError(
                "header_row must be an integer", "selection_invalid"
            ) from error
        formulas, cached = self._workbooks()
        try:
            if not isinstance(worksheet, str) or worksheet not in formulas.sheetnames:
                raise DataConnectionError("a valid worksheet is required", "selection_invalid")
            headers = self._headers(formulas[worksheet], header_row)
            fields = [
                {
                    "name": field,
                    "source_type": "unknown",
                    "normalized_type": "text",
                    "nullable": True,
                    "primary_key": False,
                }
                for field in headers
            ]
            checked = _validate_selection(selection, fields)
            formula_policy = selection.get("formula_policy", "cached_or_null")
            if formula_policy not in {"cached_or_null", "reject"}:
                raise DataConnectionError(
                    "formula_policy must be cached_or_null or reject", "selection_invalid"
                )
            return (
                worksheet,
                checked["columns"],
                {
                    "header_row": header_row,
                    "checked": checked,
                    "formulas": formulas,
                    "cached": cached,
                },
            )
        except Exception:
            formulas.close()
            cached.close()
            raise

    def iter_records(self, selection: Mapping[str, Any], limit: int) -> Iterator[dict[str, Any]]:
        worksheet, columns, state = self._target(selection)
        try:
            rows = self._records(
                state["formulas"][worksheet],
                state["cached"][worksheet],
                self._headers(state["formulas"][worksheet], state["header_row"]),
                state["header_row"],
                columns,
                min(limit, MAX_SNAPSHOT_ROWS + 1),
            )
            formula_policy = selection.get("formula_policy", "cached_or_null")
            for row in rows:
                if formula_policy == "reject" and row.get("__formula_fields"):
                    raise DataConnectionError(
                        "formula cells are not allowed by this policy", "formula_unsupported"
                    )
                yield row
        finally:
            state["formulas"].close()
            state["cached"].close()


class DataConnectionsService:
    """The canonical authority for profiles, source reads, and snapshots."""

    def __init__(
        self,
        store: CampaignStore,
        root: str | Path | None = None,
        *,
        credential_resolver: CredentialResolver | None = None,
        data_studio=None,
    ):
        self.store = store
        self.root = Path(root or store.path.parent / "artifacts" / "data-connections").resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.sources_root = self.root / "sources"
        self.snapshots_root = self.root / "snapshots"
        self.sources_root.mkdir(exist_ok=True, mode=0o700)
        self.snapshots_root.mkdir(exist_ok=True, mode=0o700)
        self.credential_resolver = credential_resolver or EnvironmentCredentialResolver()
        self.data_studio = data_studio
        self.executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="mailforge-connections"
        )
        self._lock = threading.Lock()
        self._running: set[str] = set()
        self._cancel: set[str] = set()
        self._recover_running()

    def close(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=False)

    def _recover_running(self) -> None:
        with self.store.transaction(immediate=True) as connection:
            rows = connection.execute(
                "SELECT id,connection_id FROM data_connection_snapshots WHERE status='running'"
            ).fetchall()
            for row in rows:
                now = _now()
                connection.execute(
                    "UPDATE data_connection_snapshots SET status='failed',worker_token=NULL,error_message=?,updated_at=? WHERE id=? AND status='running'",
                    ("snapshot worker interrupted; start a new refresh", now, row["id"]),
                )
                self._event(
                    connection,
                    row["connection_id"],
                    row["id"],
                    "recovered",
                    "failed",
                    {"reason": "restart"},
                )

    @staticmethod
    def _event(
        connection,
        connection_id: str,
        snapshot_id: str | None,
        event_type: str,
        status: str,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO data_connection_events(id,connection_id,snapshot_id,event_type,status,detail_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (
                str(uuid.uuid4()),
                connection_id,
                snapshot_id,
                event_type,
                status,
                _json(dict(detail or {})),
                _now(),
            ),
        )

    def _row_profile(self, row) -> Profile:
        return Profile(
            id=row["id"],
            name=row["name"],
            source_type=row["source_type"],
            config=json.loads(row["config_json"] or "{}"),
            credential_ref=row["credential_ref"],
            selection=json.loads(row["selection_json"] or "{}"),
            limits=json.loads(row["limits_json"] or "{}"),
            enabled=bool(row["enabled"]),
        )

    def _profile(self, profile_id: str) -> Profile:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM data_connections WHERE id=?", (profile_id,)
            ).fetchone()
        if row is None:
            raise DataConnectionError("data connection was not found", "connection_not_found")
        return self._row_profile(row)

    def _public_profile(self, row) -> dict[str, Any]:
        profile = self._row_profile(row)
        return {
            "id": profile.id,
            "name": profile.name,
            "source_type": profile.source_type,
            "config": _redact_config(profile.config),
            "credential_ref": profile.credential_ref,
            "selection": profile.selection,
            "limits": profile.limits,
            "enabled": profile.enabled,
            "last_test_status": row["last_test_status"],
            "last_test_message": row["last_test_message"],
            "last_tested_at": row["last_tested_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_connections(self, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        safe_limit = max(1, min(int(limit), 100))
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM data_connections ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (safe_limit, max(0, int(offset))),
            ).fetchall()
        return {
            "items": [self._public_profile(row) for row in rows],
            "limit": safe_limit,
            "offset": max(0, int(offset)),
        }

    def retain_source(self, source_path: str | Path, filename: str, source_type: str) -> Path:
        if source_type not in {"sqlite", "xlsx"}:
            raise DataConnectionError(
                "only SQLite and XLSX sources can be uploaded", "source_type_invalid"
            )
        path = Path(source_path).expanduser()
        if path.is_symlink() or not path.is_file():
            raise DataConnectionError("uploaded source is not a regular file", "source_missing")
        if path.stat().st_size > (MAX_XLSX_BYTES if source_type == "xlsx" else MAX_SOURCE_BYTES):
            raise DataConnectionError("source exceeds its local size limit", "source_limit")
        suffix = ".xlsx" if source_type == "xlsx" else ".sqlite3"
        target = (
            self.sources_root / f"{uuid.uuid4().hex}-{_safe_filename(filename, 'source')}{suffix}"
        )
        shutil.copyfile(path.resolve(), target)
        os.chmod(target, 0o600)
        return target

    def create_connection(
        self,
        name: str,
        source_type: str,
        config: Mapping[str, Any] | None = None,
        *,
        credential_ref: str | None = None,
        selection: Mapping[str, Any] | None = None,
        limits: Mapping[str, Any] | None = None,
        source_path: str | Path | None = None,
        source_filename: str | None = None,
    ) -> dict[str, Any]:
        name = str(name).strip()
        if not name or len(name) > 120:
            raise DataConnectionError(
                "connection name is required and bounded", "connection_invalid"
            )
        if source_type not in ALLOWED_SOURCE_TYPES:
            raise DataConnectionError("unsupported data connection type", "source_type_invalid")
        checked_config = dict(config or {})
        _assert_no_secret_values(checked_config)
        if source_type == "postgresql":
            unknown = set(checked_config) - ALLOWED_POSTGRES_CONFIG
            if unknown:
                raise DataConnectionError(
                    "PostgreSQL configuration contains unsupported fields", "connection_invalid"
                )
            for key in ("host", "database", "user"):
                if not isinstance(checked_config.get(key), str) or not checked_config[key].strip():
                    raise DataConnectionError(f"PostgreSQL {key} is required", "connection_invalid")
            port = int(checked_config.get("port", 5432))
            if not 1 <= port <= 65535:
                raise DataConnectionError("PostgreSQL port is invalid", "connection_invalid")
            checked_config["port"] = port
            checked_config.setdefault("sslmode", "verify-full")
            if checked_config["sslmode"] not in {"verify-full", "verify-ca"}:
                raise DataConnectionError("PostgreSQL TLS mode is invalid", "connection_invalid")
            if not credential_ref:
                raise DataConnectionError(
                    "PostgreSQL requires credential_ref", "credential_required"
                )
            validate_credential_reference(credential_ref)
        else:
            unknown = set(checked_config) - ALLOWED_FILE_CONFIG
            if unknown:
                raise DataConnectionError(
                    "file-source configuration contains unsupported fields", "connection_invalid"
                )
            if source_path is None:
                raise DataConnectionError("a selected source file is required", "source_missing")
            retained = Path(source_path).expanduser().resolve()
            if retained.is_symlink() or not retained.is_file():
                raise DataConnectionError("selected source is not a regular file", "source_missing")
            expected = ".xlsx" if source_type == "xlsx" else None
            if expected and retained.suffix.lower() != expected:
                raise DataConnectionError(
                    "only normal .xlsx workbooks are supported", "source_format"
                )
            checked_config["source_path"] = str(retained)
            checked_config["source_name"] = source_filename or retained.name
            credential_ref = None
        checked_limits = _validate_limits(limits)
        checked_selection = dict(selection or {})
        profile_id = str(uuid.uuid4())
        now = _now()
        with self.store.transaction(immediate=True) as connection:
            try:
                connection.execute(
                    "INSERT INTO data_connections(id,name,source_type,config_json,credential_ref,selection_json,limits_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        profile_id,
                        name,
                        source_type,
                        _json(checked_config),
                        credential_ref,
                        _json(checked_selection),
                        _json(checked_limits),
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise DataConnectionError(
                    "a connection with this name already exists", "connection_exists"
                ) from error
            self._event(
                connection,
                profile_id,
                None,
                "profile_created",
                "completed",
                {"source_type": source_type},
            )
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM data_connections WHERE id=?", (profile_id,)
            ).fetchone()
        return self._public_profile(row)

    def disable_connection(self, profile_id: str) -> dict[str, Any]:
        self._profile(profile_id)
        with self.store.transaction(immediate=True) as connection:
            changed = connection.execute(
                "UPDATE data_connections SET enabled=0,updated_at=? WHERE id=?",
                (_now(), profile_id),
            ).rowcount
            if changed != 1:
                raise DataConnectionError("data connection was not found", "connection_not_found")
            self._event(connection, profile_id, None, "profile_disabled", "completed")
            row = connection.execute(
                "SELECT * FROM data_connections WHERE id=?", (profile_id,)
            ).fetchone()
        return self._public_profile(row)

    def _adapter(self, profile: Profile) -> _Adapter:
        if not profile.enabled:
            raise DataConnectionError("data connection is disabled", "connection_disabled")
        if profile.source_type == "sqlite":
            return _SQLiteAdapter(Path(profile.config["source_path"]))
        if profile.source_type == "xlsx":
            return _XlsxAdapter(Path(profile.config["source_path"]))
        return _PostgresAdapter(
            profile.config, profile.credential_ref or "", self.credential_resolver
        )

    def test_connection(self, profile_id: str) -> dict[str, Any]:
        profile = self._profile(profile_id)
        try:
            result = self._adapter(profile).test()
            status, message = "passed", "read-only source is reachable"
        except DataConnectionError as error:
            result = {"reachable": False, "error_code": error.code}
            status, message = "failed", str(error)
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE data_connections SET last_test_status=?,last_test_message=?,last_tested_at=?,updated_at=? WHERE id=?",
                (status, message[:500], _now(), _now(), profile_id),
            )
            self._event(
                connection, profile_id, None, "connection_test", status, {"message": message[:200]}
            )
        if status == "failed":
            raise DataConnectionError(message, result.get("error_code", "source_unavailable"))
        return {"profile_id": profile_id, **result, "status": status, "message": message}

    def schema(self, profile_id: str) -> dict[str, Any]:
        profile = self._profile(profile_id)
        result = self._adapter(profile).schema()
        return {"profile_id": profile_id, **result}

    def preview(
        self, profile_id: str, selection: Mapping[str, Any] | None = None, *, limit: int = 50
    ) -> dict[str, Any]:
        profile = self._profile(profile_id)
        safe_limit = max(1, min(int(limit), MAX_PREVIEW_ROWS))
        chosen = {**profile.selection, **dict(selection or {})}
        result = self._adapter(profile).preview(chosen, safe_limit)
        with self.store.transaction(immediate=True) as connection:
            self._event(
                connection, profile_id, None, "preview", "completed", {"rows": len(result["rows"])}
            )
        return {"profile_id": profile_id, **result}

    def _snapshot(self, snapshot_id: str) -> dict[str, Any]:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM data_connection_snapshots WHERE id=?", (snapshot_id,)
            ).fetchone()
        if row is None:
            raise DataConnectionError("snapshot was not found", "snapshot_not_found")
        result = dict(row)
        for key in ("selection_json", "schema_json", "consistency_json"):
            result[key.removesuffix("_json")] = json.loads(result.pop(key) or "{}")
        artifact_path = result.get("artifact_path")
        result["artifact_available"] = False
        if artifact_path:
            path = Path(artifact_path).resolve()
            try:
                result["artifact_available"] = (
                    result["status"] == "completed"
                    and self.root in path.parents
                    and path.is_file()
                    and bool(result.get("artifact_sha256"))
                    and _sha256(path) == result["artifact_sha256"]
                )
            except OSError:
                result["artifact_available"] = False
        result.pop("artifact_path", None)
        result.pop("worker_token", None)
        return result

    def list_snapshots(
        self, profile_id: str, *, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        self._profile(profile_id)
        safe_limit = max(1, min(int(limit), 100))
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM data_connection_snapshots WHERE connection_id=? ORDER BY revision DESC LIMIT ? OFFSET ?",
                (profile_id, safe_limit, max(0, int(offset))),
            ).fetchall()
        return {
            "items": [self._snapshot(row["id"]) for row in rows],
            "limit": safe_limit,
            "offset": max(0, int(offset)),
        }

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        return self._snapshot(snapshot_id)

    def _artifact_path(self, snapshot_id: str) -> Path:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT artifact_path,artifact_sha256,status FROM data_connection_snapshots WHERE id=?",
                (snapshot_id,),
            ).fetchone()
        if row is None:
            raise DataConnectionError("snapshot was not found", "snapshot_not_found")
        path = Path(row["artifact_path"] or "").resolve()
        if row["status"] != "completed" or not path.is_file() or self.root not in path.parents:
            raise DataConnectionError("snapshot is not a completed artifact", "snapshot_incomplete")
        if not row["artifact_sha256"] or _sha256(path) != row["artifact_sha256"]:
            raise DataConnectionError(
                "snapshot artifact checksum does not match", "snapshot_integrity"
            )
        return path

    def start_snapshot(
        self,
        profile_id: str,
        selection: Mapping[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
        wait: bool = False,
    ) -> dict[str, Any]:
        profile = self._profile(profile_id)
        chosen = {**profile.selection, **dict(selection or {})}
        key = idempotency_key or f"snapshot:{profile_id}:{uuid.uuid4().hex}"
        if len(key) > 200:
            raise DataConnectionError("idempotency_key is too long", "idempotency_invalid")
        now = _now()
        snapshot_id = str(uuid.uuid4())
        with self.store.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT id FROM data_connection_snapshots WHERE connection_id=? AND idempotency_key=?",
                (profile_id, key),
            ).fetchone()
            if existing is not None:
                return self._snapshot(existing["id"])
            revision = connection.execute(
                "SELECT COALESCE(MAX(revision),0)+1 FROM data_connection_snapshots WHERE connection_id=?",
                (profile_id,),
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO data_connection_snapshots(id,connection_id,revision,selection_json,status,idempotency_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (snapshot_id, profile_id, revision, _json(chosen), "pending", key, now, now),
            )
            self._event(
                connection,
                profile_id,
                snapshot_id,
                "snapshot_requested",
                "pending",
                {"revision": revision},
            )
        if wait:
            self._run_snapshot(snapshot_id)
        else:
            with self._lock:
                self._running.add(snapshot_id)
            self.executor.submit(self._run_snapshot, snapshot_id)
        return self._snapshot(snapshot_id)

    def _run_snapshot(self, snapshot_id: str) -> None:
        worker_token = uuid.uuid4().hex
        temporary: Path | None = None
        artifact: Path | None = None
        published = False
        try:
            with self.store.transaction(immediate=True) as connection:
                row = connection.execute(
                    "SELECT * FROM data_connection_snapshots WHERE id=?", (snapshot_id,)
                ).fetchone()
                if row is None or row["status"] != "pending":
                    return
                profile_id = row["connection_id"]
                connection.execute(
                    "UPDATE data_connection_snapshots SET status='running',worker_token=?,started_at=?,updated_at=? WHERE id=? AND status='pending'",
                    (worker_token, _now(), _now(), snapshot_id),
                )
                self._event(connection, profile_id, snapshot_id, "snapshot_started", "running")
            profile = self._profile(profile_id)
            selection = json.loads(row["selection_json"] or "{}")
            adapter = self._adapter(profile)
            schema = adapter.schema()
            source_signature = hashlib.sha256(_json(schema).encode()).hexdigest()
            temporary = self.snapshots_root / f".{snapshot_id}.jsonl.tmp"
            count = 0
            errors = 0
            truncated = False
            artifact_bytes = 0
            with temporary.open("wb") as handle:
                for record in adapter.iter_records(
                    selection, int(profile.limits.get("snapshot_rows", MAX_SNAPSHOT_ROWS)) + 1
                ):
                    if snapshot_id in self._cancel:
                        raise SnapshotCanceled()
                    if count >= int(profile.limits.get("snapshot_rows", MAX_SNAPSHOT_ROWS)):
                        truncated = True
                        break
                    payload = _json(record).encode("utf-8") + b"\n"
                    if artifact_bytes + len(payload) > MAX_SNAPSHOT_BYTES:
                        raise DataConnectionError(
                            "snapshot artifact exceeds the 256 MiB limit", "snapshot_limit"
                        )
                    handle.write(payload)
                    artifact_bytes += len(payload)
                    count += 1
                    errors += len(record.get("__conversion_errors", []))
            os.chmod(temporary, 0o600)
            artifact = self.snapshots_root / f"{snapshot_id}.jsonl"
            os.replace(temporary, artifact)
            temporary = None
            digest = _sha256(artifact)
            with self.store.transaction(immediate=True) as connection:
                changed = connection.execute(
                    "UPDATE data_connection_snapshots SET status='completed',worker_token=NULL,schema_json=?,source_signature=?,consistency_json=?,artifact_path=?,artifact_sha256=?,record_count=?,error_count=?,completed_at=?,updated_at=? WHERE id=? AND status='running' AND worker_token=?",
                    (
                        _json(schema),
                        source_signature,
                        _json(
                            {"mode": adapter.consistency, "bounded": True, "truncated": truncated}
                        ),
                        str(artifact),
                        digest,
                        count,
                        errors,
                        _now(),
                        _now(),
                        snapshot_id,
                        worker_token,
                    ),
                ).rowcount
                if changed:
                    published = True
                    self._event(
                        connection,
                        profile_id,
                        snapshot_id,
                        "snapshot_published",
                        "completed",
                        {"records": count, "sha256": digest},
                    )
            if not published:
                artifact.unlink(missing_ok=True)
        except SnapshotCanceled as error:
            if temporary:
                temporary.unlink(missing_ok=True)
            if artifact and not published:
                artifact.unlink(missing_ok=True)
            with self.store.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE data_connection_snapshots SET status='canceled',worker_token=NULL,error_message=?,updated_at=? WHERE id=? AND status='running'",
                    (str(error), _now(), snapshot_id),
                )
                row = connection.execute(
                    "SELECT connection_id FROM data_connection_snapshots WHERE id=?", (snapshot_id,)
                ).fetchone()
                if row:
                    self._event(
                        connection,
                        row["connection_id"],
                        snapshot_id,
                        "snapshot_canceled",
                        "canceled",
                    )
        except Exception as error:
            if temporary:
                temporary.unlink(missing_ok=True)
            if artifact and not published:
                artifact.unlink(missing_ok=True)
            message = str(error)[:500]
            with self.store.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE data_connection_snapshots SET status='failed',worker_token=NULL,error_message=?,updated_at=? WHERE id=? AND status='running'",
                    (message, _now(), snapshot_id),
                )
                row = connection.execute(
                    "SELECT connection_id FROM data_connection_snapshots WHERE id=?", (snapshot_id,)
                ).fetchone()
                if row:
                    self._event(
                        connection,
                        row["connection_id"],
                        snapshot_id,
                        "snapshot_failed",
                        "failed",
                        {"error": message},
                    )
        finally:
            with self._lock:
                self._running.discard(snapshot_id)
                self._cancel.discard(snapshot_id)

    def cancel_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        with self.store.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT connection_id,status FROM data_connection_snapshots WHERE id=?",
                (snapshot_id,),
            ).fetchone()
            if row is None:
                raise DataConnectionError("snapshot was not found", "snapshot_not_found")
            if row["status"] not in {"pending", "running"}:
                raise DataConnectionError("snapshot is already terminal", "snapshot_state")
            self._cancel.add(snapshot_id)
            connection.execute(
                "UPDATE data_connection_snapshots SET status='canceled',worker_token=NULL,error_message=?,updated_at=? WHERE id=? AND status IN ('pending','running')",
                ("canceled by operator", _now(), snapshot_id),
            )
            self._event(
                connection, row["connection_id"], snapshot_id, "snapshot_canceled", "canceled"
            )
        return self._snapshot(snapshot_id)

    def handoff_to_data_studio(
        self, snapshot_id: str, recipe_revision_id: str, *, correlation_id: str | None = None
    ) -> dict[str, Any]:
        path = self._artifact_path(snapshot_id)
        if self.data_studio is None:
            raise DataConnectionError("Data Studio is unavailable", "handoff_unavailable")
        job = self.data_studio.create_job(
            path,
            f"data-connection-{snapshot_id}.jsonl",
            recipe_revision_id,
            correlation_id=correlation_id or f"data-connection:{snapshot_id}",
        )
        with self.store.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT connection_id FROM data_connection_snapshots WHERE id=?", (snapshot_id,)
            ).fetchone()
            self._event(
                connection,
                row["connection_id"],
                snapshot_id,
                "data_studio_handoff",
                "completed",
                {"job_id": job["id"]},
            )
        return job

    def events(self, profile_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        self._profile(profile_id)
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM data_connection_events WHERE connection_id=? ORDER BY created_at DESC LIMIT ?",
                (profile_id, max(1, min(int(limit), 200))),
            ).fetchall()
        return [{**dict(row), "detail": json.loads(row["detail_json"] or "{}")} for row in rows]
