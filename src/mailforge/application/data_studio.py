"""Canonical, local-first structured data processing for Mailforge.

Data Studio deliberately keeps routine transformations declarative.  Sources are
read as bounded record streams, recipes are immutable by revision, and durable
jobs publish artifacts only after the complete output has been written.
"""

# SQL and compact transformation statements remain readable as contiguous fragments.
# ruff: noqa: E501

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import shutil
import sqlite3
import tempfile
import threading
import uuid
from collections import defaultdict
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from mailforge.domain.email import classify_address, normalize_email
from mailforge.infrastructure.campaign_store import CampaignStore
from mailforge.infrastructure.providers import ProviderCatalog

MAX_SOURCE_BYTES = 1_073_741_824
MAX_PREVIEW_ROWS = 500
MAX_ARTIFACT_ROWS = 100_001
MAX_JOIN_MATCHES_PER_ROW = 1_000
MAX_JOIN_OUTPUT_ROWS = 1_000_000
MAX_AGGREGATE_GROUPS = 100_000
MAX_OUTPUT_TEXT = 10_000
SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
MISSING = object()


class DataStudioError(ValueError):
    def __init__(self, message: str, code: str = "data_studio_invalid"):
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _safe_filename(value: str, fallback: str = "data.csv") -> str:
    name = Path(value or fallback).name
    cleaned = SAFE_FILENAME.sub("-", name).strip(".-")
    return (cleaned or fallback)[:180]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _value(record: dict[str, Any], field: str) -> Any:
    value = record.get(field, MISSING)
    return None if value is MISSING else value


def _text(value: Any) -> str:
    if value is None or value is MISSING:
        return ""
    return str(value)


def _is_missing(value: Any) -> bool:
    return value is None or value == ""


def _read_json_records(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", errors="strict") as handle:
        raw = json.load(handle)
    if isinstance(raw, dict):
        raw = raw.get("records", raw.get("data", [raw]))
    if not isinstance(raw, list):
        raise DataStudioError("JSON input must contain an array of records", "source_format")
    for item in raw:
        if not isinstance(item, dict):
            raise DataStudioError("JSON records must be objects", "source_record")
        yield dict(item)


def iter_source_records(
    path: str | Path,
    *,
    max_bytes: int = MAX_SOURCE_BYTES,
    source_id: str | None = None,
    source_name: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield normalized record dictionaries without loading text sources wholesale."""

    source = Path(path)
    if not source.is_file():
        raise DataStudioError("source file was not found", "source_missing")
    if source.stat().st_size > max_bytes:
        raise DataStudioError("source exceeds the 1 GiB local processing limit", "source_limit")
    suffix = source.suffix.lower()
    if suffix in {".json", ".jsonl"}:
        if suffix == ".json":
            iterator = _read_json_records(source)
        else:

            def jsonl() -> Iterator[dict[str, Any]]:
                with source.open("r", encoding="utf-8-sig", errors="strict") as handle:
                    for line_number, line in enumerate(handle, 1):
                        if not line.strip():
                            continue
                        try:
                            item = json.loads(line)
                        except json.JSONDecodeError as error:
                            raise DataStudioError(
                                f"invalid JSONL at row {line_number}", "source_record"
                            ) from error
                        if not isinstance(item, dict):
                            raise DataStudioError(
                                f"JSONL row {line_number} is not an object", "source_record"
                            )
                        yield item

            iterator = jsonl()
    elif suffix in {".csv", ".tsv"}:
        delimiter = "\t" if suffix == ".tsv" else ","

        def delimited() -> Iterator[dict[str, Any]]:
            with source.open("r", encoding="utf-8-sig", newline="", errors="strict") as handle:
                reader = csv.DictReader(handle, delimiter=delimiter)
                if not reader.fieldnames or any(not field for field in reader.fieldnames):
                    raise DataStudioError("delimited input needs a header row", "source_schema")
                for row in reader:
                    yield {str(key): value for key, value in row.items() if key is not None}

        iterator = delimited()
    else:

        def text_records() -> Iterator[dict[str, Any]]:
            with source.open("r", encoding="utf-8-sig", errors="replace") as handle:
                for line in handle:
                    yield {"value": line.rstrip("\r\n")}

        iterator = text_records()
    for row_number, record in enumerate(iterator, 1):
        record["__source_row"] = row_number
        record["__source_id"] = source_id or source.stem
        record["__source_name"] = source_name or source.name
        record["__provenance"] = [
            {
                "source_id": record["__source_id"],
                "source_name": record["__source_name"],
                "row": row_number,
            }
        ]
        yield record


def detect_schema(path: str | Path, *, sample_limit: int = 100) -> dict[str, Any]:
    fields: list[str] = []
    types: dict[str, str] = {}
    samples: list[dict[str, Any]] = []
    for record in iter_source_records(path):
        if len(samples) < sample_limit:
            samples.append(dict(record))
        for field, value in record.items():
            if field.startswith("__"):
                continue
            if field not in fields:
                fields.append(field)
            if field not in types or types[field] == "empty":
                types[field] = _infer_type(value)
        if len(samples) >= sample_limit and len(fields) > 0:
            break
    return {"fields": fields, "types": types, "sample": samples, "sample_count": len(samples)}


def _infer_type(value: Any) -> str:
    if _is_missing(value):
        return "empty"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int) and not isinstance(value, bool):
        return "integer"
    if isinstance(value, float):
        return "number"
    candidate = _text(value).strip()
    if candidate.lower() in {"true", "false"}:
        return "boolean"
    try:
        int(candidate)
        return "integer"
    except ValueError:
        pass
    try:
        float(candidate)
        return "number"
    except ValueError:
        return "text"


def _condition(record: dict[str, Any], condition: dict[str, Any]) -> bool:
    op = condition.get("op")
    if op in {"and", "or"}:
        children = condition.get("conditions", [])
        if not isinstance(children, list):
            raise DataStudioError("filter conditions must be a list", "filter_invalid")
        values = [_condition(record, item) for item in children]
        return all(values) if op == "and" else any(values)
    if op == "not":
        return not _condition(record, condition.get("condition", {}))
    field = condition.get("field")
    if not isinstance(field, str) or not field:
        raise DataStudioError("filter field is required", "filter_invalid")
    left = _value(record, field)
    right = condition.get("value")
    if op == "missing":
        return _is_missing(left)
    if op == "present":
        return not _is_missing(left)
    if op == "in":
        values = condition.get("values", [])
        return left in values if isinstance(values, list) else False
    if op in {"contains", "starts_with", "ends_with"}:
        haystack, needle = _text(left), _text(right)
        return {
            "contains": needle in haystack,
            "starts_with": haystack.startswith(needle),
            "ends_with": haystack.endswith(needle),
        }[op]
    if op in {"equals", "not_equals"}:
        result = left == right
        return not result if op == "not_equals" else result
    if op in {"greater_than", "less_than", "greater_or_equal", "less_or_equal"}:
        try:
            left_number, right_number = float(left), float(right)
        except TypeError, ValueError:
            return False
        return {
            "greater_than": left_number > right_number,
            "less_than": left_number < right_number,
            "greater_or_equal": left_number >= right_number,
            "less_or_equal": left_number <= right_number,
        }[op]
    raise DataStudioError(f"unsupported filter operator: {op}", "filter_invalid")


def _computed(record: dict[str, Any], spec: dict[str, Any]) -> Any:
    kind = spec.get("kind")
    if kind == "concat":
        fields = spec.get("fields", [])
        separator = _text(spec.get("separator", ""))
        return separator.join(_text(_value(record, field)) for field in fields).strip()
    if kind == "email_domain":
        value = _text(_value(record, spec.get("field", ""))).strip().lower()
        return value.rsplit("@", 1)[1] if "@" in value else ""
    if kind == "provider_classify":
        value = normalize_email(_text(_value(record, spec.get("field", "email"))))
        return classify_address(value)
    if kind == "substring":
        value = _text(_value(record, spec.get("field", "")))
        start, end = int(spec.get("start", 0)), spec.get("end")
        return value[start:] if end is None else value[start : int(end)]
    if kind == "number":
        values = [float(_value(record, field) or 0) for field in spec.get("fields", [])]
        operator = spec.get("operator", "add")
        if operator == "add":
            result = sum(values)
        elif operator == "subtract":
            result = values[0] - sum(values[1:]) if values else 0
        elif operator == "multiply":
            result = math.prod(values)
        elif operator == "divide":
            if len(values) != 2 or values[1] == 0:
                raise DataStudioError(
                    "numeric division requires two non-zero values", "computed_error"
                )
            result = values[0] / values[1]
        else:
            raise DataStudioError("unsupported numeric operator", "computed_invalid")
        if not math.isfinite(result):
            raise DataStudioError("computed number is not finite", "computed_error")
        return int(result) if result.is_integer() else result
    if kind == "conditional":
        return (
            spec.get("if_true")
            if _condition(record, spec.get("when", {}))
            else spec.get("if_false")
        )
    if kind == "url":
        template = _text(spec.get("template", ""))
        if len(template) > MAX_OUTPUT_TEXT:
            raise DataStudioError("computed URL is too long", "computed_error")
        for field in spec.get("fields", []):
            template = template.replace(
                "{" + field + "}", quote(_text(_value(record, field)), safe="")
            )
        return template
    raise DataStudioError(f"unsupported computed expression: {kind}", "computed_invalid")


def _cast(value: Any, target: str) -> Any:
    if _is_missing(value):
        return None
    text = _text(value).strip()
    if target == "text":
        return text
    if target == "integer":
        return int(text)
    if target == "number":
        number = float(text)
        if not math.isfinite(number):
            raise ValueError("number is not finite")
        return number
    if target == "boolean":
        if text.lower() in {"true", "1", "yes"}:
            return True
        if text.lower() in {"false", "0", "no"}:
            return False
        raise ValueError("expected true or false")
    if target == "date":
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    raise ValueError(f"unsupported type {target}")


def _public_fields(row: dict[str, Any]) -> list[str]:
    return [field for field in row if not field.startswith("__")]


def _join_key(row: dict[str, Any], fields: list[str], normalization: str) -> str | None:
    values: list[str] = []
    for field in fields:
        value = row.get(field)
        if _is_missing(value):
            return None
        text = _text(value)
        if normalization == "trim":
            text = text.strip()
        elif normalization == "lower":
            text = text.strip().lower()
        elif normalization == "email":
            text = normalize_email(text)
        values.append(text)
    return _json(values)


def _manifest(path: Path) -> dict[str, Any] | None:
    if path.suffix.lower() != ".json":
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except OSError, UnicodeError, json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) and value.get("kind") == "data_studio_sources" else None


def _safe_export_value(value: Any, specification: dict[str, Any]) -> Any:
    if value is None:
        return specification.get("null", "")
    if isinstance(value, (dict, list)):
        value = _json(value)
    text = str(value)
    if text[:1] in {"=", "+", "-", "@"}:
        return "'" + text
    return text


class DataStudioService:
    """Owns recipes, previews, jobs, and safe artifact publication."""

    def __init__(self, store: CampaignStore, root: str | Path | None = None):
        self.store = store
        self.root = Path(root or self.store.path.parent / "data-studio")
        self.root.mkdir(parents=True, exist_ok=True)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mailforge-data")
        self._running: set[str] = set()
        self._lock = threading.Lock()
        self._recover_jobs()

    def close(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=False)

    def _recover_jobs(self) -> None:
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE data_studio_jobs SET status='pending', updated_at=? WHERE status='running'",
                (_now(),),
            )

    def create_recipe(
        self, name: str, description: str = "", definition: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        name = name.strip()
        if not name or len(name) > 160:
            raise DataStudioError("recipe name is required", "recipe_invalid")
        definition = self.validate_definition(definition or {"steps": []})
        recipe_id, revision_id = str(uuid.uuid4()), str(uuid.uuid4())
        now, digest = _now(), hashlib.sha256(_json(definition).encode()).hexdigest()
        try:
            with self.store.transaction(immediate=True) as connection:
                connection.execute(
                    "INSERT INTO data_studio_recipes(id,name,description,current_revision_id,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                    (recipe_id, name, description[:500], revision_id, now, now),
                )
                connection.execute(
                    "INSERT INTO data_studio_recipe_revisions(id,recipe_id,revision_number,definition_json,content_hash,created_at) VALUES (?,?,?,?,?,?)",
                    (revision_id, recipe_id, 1, _json(definition), digest, now),
                )
        except Exception as error:
            if "UNIQUE" in str(error).upper():
                raise DataStudioError(
                    "a recipe with this name already exists", "recipe_exists"
                ) from error
            raise
        return self.get_recipe(recipe_id)

    def list_recipes(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM data_studio_recipes"
        params: tuple[Any, ...] = ()
        if not include_archived:
            query += " WHERE status = 'active'"
        query += " ORDER BY updated_at DESC"
        with self.store.connect() as connection:
            return [dict(row) for row in connection.execute(query, params).fetchall()]

    def get_recipe(self, recipe_id: str, revision_id: str | None = None) -> dict[str, Any]:
        with self.store.connect() as connection:
            recipe = connection.execute(
                "SELECT * FROM data_studio_recipes WHERE id=?", (recipe_id,)
            ).fetchone()
            if recipe is None:
                raise DataStudioError("recipe was not found", "recipe_not_found")
            revision_id = revision_id or recipe["current_revision_id"]
            revision = connection.execute(
                "SELECT * FROM data_studio_recipe_revisions WHERE id=?", (revision_id,)
            ).fetchone()
        if revision is None or revision["recipe_id"] != recipe_id:
            raise DataStudioError("recipe revision was not found", "revision_not_found")
        result = dict(recipe)
        result["revision"] = dict(revision)
        result["revision"]["definition"] = json.loads(revision["definition_json"])
        return result

    def save_revision(
        self, recipe_id: str, definition: dict[str, Any], *, expected_revision_id: str | None = None
    ) -> dict[str, Any]:
        current = self.get_recipe(recipe_id)
        if expected_revision_id and expected_revision_id != current["current_revision_id"]:
            raise DataStudioError("recipe changed; reload before saving", "revision_conflict")
        definition = self.validate_definition(definition)
        now, revision_id = _now(), str(uuid.uuid4())
        digest = hashlib.sha256(_json(definition).encode()).hexdigest()
        with self.store.transaction(immediate=True) as connection:
            number = connection.execute(
                "SELECT COALESCE(MAX(revision_number),0)+1 FROM data_studio_recipe_revisions WHERE recipe_id=?",
                (recipe_id,),
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO data_studio_recipe_revisions(id,recipe_id,revision_number,definition_json,content_hash,created_at) VALUES (?,?,?,?,?,?)",
                (revision_id, recipe_id, number, _json(definition), digest, now),
            )
            connection.execute(
                "UPDATE data_studio_recipes SET current_revision_id=?,updated_at=? WHERE id=?",
                (revision_id, now, recipe_id),
            )
        return self.get_recipe(recipe_id)

    def archive_recipe(self, recipe_id: str) -> dict[str, Any]:
        self.get_recipe(recipe_id)
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE data_studio_recipes SET status='archived',updated_at=? WHERE id=?",
                (_now(), recipe_id),
            )
        return self.get_recipe(recipe_id)

    @staticmethod
    def validate_definition(definition: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(definition, dict) or not isinstance(definition.get("steps"), list):
            raise DataStudioError("recipe steps must be a list", "recipe_invalid")
        if len(definition["steps"]) > 100:
            raise DataStudioError("recipes may contain at most 100 steps", "recipe_limit")
        allowed = {
            "select",
            "rename",
            "drop",
            "normalize",
            "cast",
            "fill",
            "validate",
            "filter",
            "computed",
            "dedupe",
            "provider_classify",
            "aggregate",
            "extension",
        }
        for step in definition["steps"]:
            if not isinstance(step, dict) or step.get("type") not in allowed:
                raise DataStudioError("recipe contains an unsupported step", "recipe_invalid")
            if (
                step.get("type") == "extension"
                and step.get("extension_id") != "email_domain_normalizer"
            ):
                raise DataStudioError(
                    "only the registered email_domain_normalizer extension is available",
                    "extension_invalid",
                )
        aggregate_positions = [
            index for index, step in enumerate(definition["steps"]) if step["type"] == "aggregate"
        ]
        if len(aggregate_positions) > 1 or (
            aggregate_positions and aggregate_positions[0] != len(definition["steps"]) - 1
        ):
            raise DataStudioError(
                "aggregate must be the final recipe step and may appear once",
                "recipe_invalid",
            )
        combine = definition.get("combine")
        if combine is not None:
            if not isinstance(combine, dict) or combine.get("operation") not in {
                "append",
                "union",
                "intersect",
                "subtract",
                "join",
            }:
                raise DataStudioError("combine operation is invalid", "combine_invalid")
            if combine["operation"] in {"intersect", "subtract", "join"} and not isinstance(
                combine.get("keys") or combine.get("left_on"), list
            ):
                raise DataStudioError("combine keys are required", "combine_invalid")
            if combine["operation"] == "join" and combine.get("join_type", "inner") not in {
                "inner",
                "left",
            }:
                raise DataStudioError("only inner and left joins are supported", "combine_invalid")
        output = definition.get("output", {})
        if not isinstance(output, dict) or output.get("format", "jsonl") not in {
            "txt",
            "csv",
            "tsv",
            "json",
            "jsonl",
        }:
            raise DataStudioError("output format is invalid", "output_invalid")
        return json.loads(_json(definition))

    def preview(
        self, source_path: str | Path, definition: dict[str, Any], *, limit: int = 50
    ) -> dict[str, Any]:
        definition = self.validate_definition(definition)
        rows = []
        for row in iter_source_records(source_path):
            rows.append(row)
            if len(rows) >= min(max(1, limit), MAX_PREVIEW_ROWS):
                break
        result = self._execute_rows(rows, definition, preview=True)
        return {"preview": True, "schema": detect_schema(source_path), **result}

    def preview_sources(
        self,
        sources: list[tuple[str | Path, str, str]],
        definition: dict[str, Any],
        *,
        limit: int = 50,
    ) -> dict[str, Any]:
        definition = self.validate_definition(definition)
        rows: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory(prefix="mailforge-data-preview-") as directory:
            manifest = Path(directory) / "sources.json"
            manifest.write_text(
                _json(
                    {
                        "kind": "data_studio_sources",
                        "sources": [
                            {"id": source_id, "path": str(Path(path)), "name": name}
                            for path, name, source_id in sources
                        ],
                    }
                ),
                encoding="utf-8",
            )
            for row in self._iter_job_records(manifest, definition):
                rows.append(row)
                if len(rows) >= min(max(1, limit), MAX_PREVIEW_ROWS):
                    break
        result = self._execute_rows(rows, definition, preview=True)
        return {"preview": True, "sources": [name for _, name, _ in sources], **result}

    def create_job(
        self,
        source_path: str | Path,
        source_filename: str,
        recipe_revision_id: str,
        *,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        return self.create_multi_source_job(
            [(source_path, source_filename, Path(source_path).stem)],
            recipe_revision_id,
            correlation_id=correlation_id,
        )

    def create_multi_source_job(
        self,
        sources: list[tuple[str | Path, str, str]],
        recipe_revision_id: str,
        *,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        if not sources or len(sources) > 16:
            raise DataStudioError("between one and sixteen sources are required", "source_limit")
        for source_path, _, _ in sources:
            if not Path(source_path).resolve().is_file():
                raise DataStudioError("source file was not retained", "source_missing")
        with self.store.connect() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM data_studio_recipe_revisions WHERE id=?", (recipe_revision_id,)
                ).fetchone()
                is None
            ):
                raise DataStudioError("recipe revision was not found", "revision_not_found")
            if correlation_id:
                existing = connection.execute(
                    "SELECT id FROM data_studio_jobs WHERE correlation_id=?", (correlation_id,)
                ).fetchone()
                if existing is not None:
                    return self.get_job(existing["id"])
        job_id, job_dir = str(uuid.uuid4()), (self.root / str(uuid.uuid4())).resolve()
        job_dir.mkdir(parents=True, exist_ok=False)
        manifest_sources = []
        for index, (source_path, source_filename, source_id) in enumerate(sources, start=1):
            retained = job_dir / f"input-{index}-{_safe_filename(source_filename)}"
            shutil.copyfile(Path(source_path).resolve(), retained)
            manifest_sources.append(
                {
                    "id": _safe_filename(source_id, f"source-{index}"),
                    "name": _safe_filename(source_filename),
                    "path": str(retained),
                }
            )
        manifest = job_dir / "sources.json"
        manifest.write_text(
            _json({"kind": "data_studio_sources", "sources": manifest_sources}), encoding="utf-8"
        )
        now = _now()
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO data_studio_jobs(id,recipe_revision_id,source_filename,source_path,source_sha256,status,created_at,updated_at,job_dir,correlation_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    recipe_revision_id,
                    " + ".join(item["name"] for item in manifest_sources),
                    str(manifest),
                    _sha256(manifest),
                    "pending",
                    now,
                    now,
                    str(job_dir),
                    correlation_id or f"data-studio:{job_id}",
                ),
            )
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM data_studio_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise DataStudioError("processing job was not found", "job_not_found")
            artifacts = connection.execute(
                "SELECT id,kind,filename,media_type,size_bytes,sha256,created_at FROM data_studio_artifacts WHERE job_id=?",
                (job_id,),
            ).fetchall()
        result = dict(row)
        result["summary"] = json.loads(result.pop("summary_json") or "{}")
        result.pop("source_path", None)
        result.pop("job_dir", None)
        result["artifacts"] = [dict(item) for item in artifacts]
        return result

    def start(self, job_id: str) -> dict[str, Any]:
        self.get_job(job_id)
        with self._lock:
            if job_id in self._running:
                return self.get_job(job_id)
            self._running.add(job_id)
        with self.store.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT status FROM data_studio_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise DataStudioError("processing job was not found", "job_not_found")
            if row["status"] in {"completed", "canceled"}:
                raise DataStudioError("this job cannot be started again", "job_state")
            connection.execute(
                "UPDATE data_studio_jobs SET status='running',started_at=COALESCE(started_at,?),updated_at=?,error_message=NULL WHERE id=?",
                (_now(), _now(), job_id),
            )
        self.executor.submit(self._run, job_id)
        return self.get_job(job_id)

    def register_extension(self, name: str = "email_domain_normalizer") -> dict[str, Any]:
        if name != "email_domain_normalizer":
            raise DataStudioError(
                "extension is not in the trusted local registry", "extension_invalid"
            )
        contract = {
            "name": name,
            "version": "1.0",
            "input": {"field": "email", "type": "text"},
            "output": {"field": "email_domain", "type": "text"},
            "capabilities": ["deterministic", "local", "no-network"],
        }
        checksum = hashlib.sha256(_json(contract).encode()).hexdigest()
        now = _now()
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO data_studio_extensions(id,name,version,checksum,input_contract_json,output_contract_json,capabilities_json,enabled,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET checksum=excluded.checksum,updated_at=excluded.updated_at",
                (
                    str(uuid.uuid5(uuid.NAMESPACE_URL, f"mailforge-extension:{name}")),
                    name,
                    contract["version"],
                    checksum,
                    _json(contract["input"]),
                    _json(contract["output"]),
                    _json(contract["capabilities"]),
                    0,
                    now,
                    now,
                ),
            )
        return self.get_extension(name)

    def get_extension(self, name: str) -> dict[str, Any]:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM data_studio_extensions WHERE name=?", (name,)
            ).fetchone()
        if row is None:
            raise DataStudioError("extension was not registered", "extension_not_found")
        result = dict(row)
        for key in ("input_contract_json", "output_contract_json", "capabilities_json"):
            result[key.removesuffix("_json")] = json.loads(result.pop(key))
        return result

    def set_extension_enabled(self, name: str, enabled: bool) -> dict[str, Any]:
        self.get_extension(name)
        with self.store.transaction(immediate=True) as connection:
            changed = connection.execute(
                "UPDATE data_studio_extensions SET enabled=?,updated_at=? WHERE name=?",
                (int(enabled), _now(), name),
            ).rowcount
        if changed != 1:
            raise DataStudioError("extension was not found", "extension_not_found")
        return self.get_extension(name)

    def _iter_job_records(
        self, source_path: str | Path, definition: dict[str, Any]
    ) -> Iterator[dict[str, Any]]:
        manifest = _manifest(Path(source_path))
        if manifest is None:
            yield from iter_source_records(source_path)
            return
        sources = manifest.get("sources", [])
        if not isinstance(sources, list) or not sources:
            raise DataStudioError("source manifest is empty", "source_invalid")
        normalized_sources = [
            {
                "id": str(item.get("id") or f"source-{index}"),
                "name": str(item.get("name") or f"source-{index}"),
                "path": str(item.get("path") or ""),
            }
            for index, item in enumerate(sources, start=1)
            if isinstance(item, dict)
        ]
        if any(not Path(item["path"]).is_file() for item in normalized_sources):
            raise DataStudioError("a source in the manifest is unavailable", "source_missing")
        combine = definition.get("combine") or {"operation": "append"}
        yield from self._combine_sources(normalized_sources, combine)

    def _combine_sources(
        self, sources: list[dict[str, str]], combine: dict[str, Any]
    ) -> Iterator[dict[str, Any]]:
        operation = combine.get("operation", "append")
        if operation in {"append", "union"}:
            if operation == "union":
                schemas = [detect_schema(source["path"]) for source in sources]
                types: dict[str, str] = {}
                for schema in schemas:
                    for field, field_type in schema.get("types", {}).items():
                        prior = types.get(field)
                        if prior and field_type not in {prior, "empty"} and prior != "empty":
                            raise DataStudioError(
                                f"union field {field} has incompatible types",
                                "combine_schema",
                            )
                        types[field] = field_type if prior in {None, "empty"} else prior
            field_order: list[str] = []
            for source in sources:
                for row in iter_source_records(
                    source["path"], source_id=source["id"], source_name=source["name"]
                ):
                    if operation == "union":
                        for field in _public_fields(row):
                            if field not in field_order:
                                field_order.append(field)
                    yield row
            return
        if len(sources) < 2:
            raise DataStudioError("this combine operation needs two sources", "combine_invalid")
        left_id = combine.get("left_source") or sources[0]["id"]
        right_id = combine.get("right_source") or sources[1]["id"]
        by_id = {source["id"]: source for source in sources}
        left = by_id.get(left_id)
        right = by_id.get(right_id)
        if left is None or right is None:
            raise DataStudioError("combine source id was not found", "combine_invalid")
        keys = combine.get("keys") or combine.get("left_on") or []
        right_keys = combine.get("right_on") or keys
        if (
            not isinstance(keys, list)
            or not isinstance(right_keys, list)
            or len(keys) != len(right_keys)
        ):
            raise DataStudioError("combine keys must be matching lists", "combine_invalid")
        normalization = combine.get("key_normalization", "trim")
        if normalization not in {"exact", "trim", "lower", "email"}:
            raise DataStudioError("key normalization is invalid", "combine_invalid")
        with tempfile.TemporaryDirectory(prefix="mailforge-data-join-") as directory:
            database = Path(directory) / "index.sqlite3"
            connection = sqlite3.connect(database)
            connection.execute("PRAGMA temp_store=FILE")
            connection.execute(
                "CREATE TABLE right_rows (join_key TEXT, row_json TEXT NOT NULL, row_order INTEGER NOT NULL)"
            )
            connection.execute("CREATE INDEX right_rows_key ON right_rows(join_key)")
            right_fields: list[str] = []
            for order, row in enumerate(
                iter_source_records(
                    right["path"], source_id=right["id"], source_name=right["name"]
                ),
                start=1,
            ):
                for field in _public_fields(row):
                    if field not in right_fields:
                        right_fields.append(field)
                key = _join_key(row, right_keys, normalization)
                if key is not None:
                    connection.execute(
                        "INSERT INTO right_rows(join_key,row_json,row_order) VALUES(?,?,?)",
                        (key, _json(row), order),
                    )
            connection.commit()
            collision = combine.get("collision", "suffix")
            suffix = str(combine.get("right_suffix", "_right"))[:32] or "_right"
            output_rows = 0
            for left_row in iter_source_records(
                left["path"], source_id=left["id"], source_name=left["name"]
            ):
                key = _join_key(left_row, keys, normalization)
                if operation in {"intersect", "subtract"}:
                    has_match = (
                        connection.execute(
                            "SELECT 1 FROM right_rows WHERE join_key=? LIMIT 1", (key,)
                        ).fetchone()
                        if key is not None
                        else None
                    )
                    if (operation == "intersect" and has_match) or (
                        operation == "subtract" and not has_match
                    ):
                        yield left_row
                    continue
                matches = (
                    connection.execute(
                        "SELECT row_json FROM right_rows WHERE join_key=? ORDER BY row_order",
                        (key,),
                    )
                    if key is not None
                    else iter(())
                )
                match_count = 0
                emitted_for_left = False
                for match in matches:
                    match_count += 1
                    if match_count > MAX_JOIN_MATCHES_PER_ROW:
                        raise DataStudioError(
                            "join key has too many matching rows",
                            "combine_limit",
                        )
                    emitted_for_left = True
                    right_row = json.loads(match[0])
                    merged = dict(left_row)
                    for field in right_fields:
                        target = field
                        if target in merged and not field.startswith("__"):
                            if collision == "error":
                                raise DataStudioError(
                                    f"join field collision: {field}", "combine_collision"
                                )
                            target = f"{field}{suffix}"
                            serial = 2
                            while target in merged:
                                target = f"{field}{suffix}{serial}"
                                serial += 1
                        merged[target] = right_row.get(field) if right_row else None
                    merged["__provenance"] = list(left_row.get("__provenance", [])) + list(
                        right_row.get("__provenance", [])
                    )
                    output_rows += 1
                    if output_rows > MAX_JOIN_OUTPUT_ROWS:
                        raise DataStudioError(
                            "join output exceeds the bounded result limit",
                            "combine_limit",
                        )
                    yield merged
                if not emitted_for_left and combine.get("join_type", "inner") == "left":
                    merged = dict(left_row)
                    for field in right_fields:
                        target = field
                        if target in merged:
                            if collision == "error":
                                raise DataStudioError(
                                    f"join field collision: {field}", "combine_collision"
                                )
                            target = f"{field}{suffix}"
                            serial = 2
                            while target in merged:
                                target = f"{field}{suffix}{serial}"
                                serial += 1
                        merged[target] = None
                    merged["__provenance"] = list(left_row.get("__provenance", []))
                    output_rows += 1
                    if output_rows > MAX_JOIN_OUTPUT_ROWS:
                        raise DataStudioError(
                            "join output exceeds the bounded result limit",
                            "combine_limit",
                        )
                    yield merged
            connection.close()

    def iter_artifact_records(
        self, job_id: str, artifact_id: str, *, limit: int = 10_000
    ) -> Iterator[dict[str, Any]]:
        path, _ = self.artifact_path(job_id, artifact_id)
        if limit < 1 or limit > MAX_ARTIFACT_ROWS:
            raise DataStudioError(
                f"artifact row limit is between 1 and {MAX_ARTIFACT_ROWS}", "artifact_limit"
            )
        suffix = path.suffix.lower()
        if suffix in {".jsonl", ".ndjson"}:
            with path.open("r", encoding="utf-8") as handle:
                for index, line in enumerate(handle):
                    if index >= limit:
                        break
                    if line.strip():
                        value = json.loads(line)
                        if not isinstance(value, dict):
                            raise DataStudioError(
                                "artifact rows must be objects", "artifact_invalid"
                            )
                        yield value
        elif suffix == ".json":
            with path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
            if not isinstance(value, list):
                raise DataStudioError("JSON artifact must contain an array", "artifact_invalid")
            for item in value[:limit]:
                if not isinstance(item, dict):
                    raise DataStudioError("artifact rows must be objects", "artifact_invalid")
                yield item
        elif suffix in {".csv", ".tsv"}:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle, delimiter="\t" if suffix == ".tsv" else ",")
                for index, row in enumerate(reader):
                    if index >= limit:
                        break
                    yield {str(key): value for key, value in row.items() if key is not None}
        else:
            with path.open("r", encoding="utf-8") as handle:
                for index, line in enumerate(handle):
                    if index >= limit:
                        break
                    yield {"value": line.rstrip("\r\n")}

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self.store.transaction(immediate=True) as connection:
            changed = connection.execute(
                "UPDATE data_studio_jobs SET status='canceled',updated_at=? WHERE id=? AND status IN ('pending','running')",
                (_now(), job_id),
            ).rowcount
            if (
                not changed
                and connection.execute(
                    "SELECT 1 FROM data_studio_jobs WHERE id=?", (job_id,)
                ).fetchone()
                is None
            ):
                raise DataStudioError("processing job was not found", "job_not_found")
        return self.get_job(job_id)

    def _run(self, job_id: str) -> None:
        try:
            with self.store.connect() as connection:
                row = connection.execute(
                    "SELECT * FROM data_studio_jobs WHERE id=?", (job_id,)
                ).fetchone()
                revision = (
                    connection.execute(
                        "SELECT definition_json FROM data_studio_recipe_revisions WHERE id=?",
                        (row["recipe_revision_id"],),
                    ).fetchone()
                    if row is not None
                    else None
                )
            if row is None or revision is None:
                return
            definition = json.loads(revision["definition_json"])
            output = Path(row["job_dir"]) / "output.rows.jsonl.tmp"
            errors = Path(row["job_dir"]) / "errors.jsonl.tmp"
            count = rejected = processed = 0
            summary = {"removed": 0, "changed_fields": [], "validation_errors": 0}
            dedupe_seen: dict[tuple[str, ...], set[tuple[Any, ...]]] = {}
            with (
                output.open("w", encoding="utf-8") as out,
                errors.open("w", encoding="utf-8") as error_out,
            ):
                batch: list[dict[str, Any]] = []
                aggregate_state: dict[str, Any] | None = None
                if any(step["type"] == "aggregate" for step in definition.get("steps", [])):
                    aggregate_state = {"step": definition["steps"][-1], "groups": {}}
                for source_row in self._iter_job_records(row["source_path"], definition):
                    batch.append(source_row)
                    processed += 1
                    if len(batch) >= 500:
                        result = self._execute_rows(
                            batch,
                            definition,
                            dedupe_seen=dedupe_seen,
                            aggregate_state=aggregate_state,
                        )
                        for item in result["rows"]:
                            out.write(_json(item) + "\n")
                            count += 1
                        for item in result["errors"]:
                            error_out.write(_json(item) + "\n")
                            rejected += 1
                        summary["removed"] += result["removed"]
                        summary["validation_errors"] += len(result["errors"])
                        batch.clear()
                        self._progress(job_id, processed, count, rejected)
                    with self.store.connect() as check:
                        current = check.execute(
                            "SELECT status FROM data_studio_jobs WHERE id=?", (job_id,)
                        ).fetchone()
                    if current is None or current["status"] == "canceled":
                        raise DataStudioError("processing was canceled", "job_canceled")
                if batch:
                    result = self._execute_rows(
                        batch,
                        definition,
                        dedupe_seen=dedupe_seen,
                        aggregate_state=aggregate_state,
                    )
                    for item in result["rows"]:
                        out.write(_json(item) + "\n")
                        count += 1
                    for item in result["errors"]:
                        error_out.write(_json(item) + "\n")
                        rejected += 1
                    summary["removed"] += result["removed"]
                    summary["validation_errors"] += len(result["errors"])
                if aggregate_state is not None:
                    for aggregate_row in self._finalize_aggregate(aggregate_state):
                        out.write(_json(aggregate_row) + "\n")
                        count += 1
            output_spec = definition.get("output", {})
            output_format = output_spec.get("format", "jsonl")
            requested_name = output_spec.get("filename") or f"data-studio-output.{output_format}"
            artifact_dir = Path(row["job_dir"]) / "artifacts"
            artifact_dir.mkdir(parents=True, exist_ok=True)
            final = artifact_dir / _safe_filename(requested_name, f"output.{output_format}")
            if final.suffix.lower() != f".{output_format}":
                final = final.with_suffix(f".{output_format}")
            export_tmp = final.with_suffix(final.suffix + ".tmp")
            self._export_rows(output, export_tmp, output_spec)
            output.unlink(missing_ok=True)
            error_final = Path(row["job_dir"]) / "errors.jsonl"
            export_tmp.replace(final)
            errors.replace(error_final)
            self._publish(
                job_id,
                processed,
                count,
                rejected,
                summary,
                [
                    ("output", final, "application/x-ndjson"),
                    ("errors", error_final, "application/x-ndjson"),
                ],
            )
        except DataStudioError as error:
            with self.store.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE data_studio_jobs SET status=?,error_message=?,updated_at=? WHERE id=?",
                    (
                        "canceled" if error.code == "job_canceled" else "failed",
                        str(error),
                        _now(),
                        job_id,
                    ),
                )
        except Exception as error:  # noqa: BLE001
            with self.store.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE data_studio_jobs SET status='failed',error_message=?,updated_at=? WHERE id=?",
                    (type(error).__name__, _now(), job_id),
                )
        finally:
            with self._lock:
                self._running.discard(job_id)

    def _progress(self, job_id: str, processed: int, output: int, rejected: int) -> None:
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE data_studio_jobs SET processed_count=?,output_count=?,rejected_count=?,updated_at=? WHERE id=? AND status='running'",
                (processed, output, rejected, _now(), job_id),
            )

    @staticmethod
    def _export_rows(source: Path, destination: Path, specification: dict[str, Any]) -> None:
        output_format = specification.get("format", "jsonl")
        fields: list[str] = []
        with source.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                for field in _public_fields(row):
                    if field not in fields:
                        fields.append(field)
                if specification.get("include_provenance", True) and "provenance" not in fields:
                    fields.append("provenance")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if output_format == "jsonl":
            with (
                source.open("r", encoding="utf-8") as input_handle,
                destination.open("w", encoding="utf-8") as output_handle,
            ):
                for line in input_handle:
                    if line.strip():
                        row = json.loads(line)
                        public = {
                            field: (
                                row.get("__provenance", [])
                                if field == "provenance"
                                else row.get(field)
                            )
                            for field in fields
                        }
                        output_handle.write(_json(public) + "\n")
            return
        with destination.open("w", encoding="utf-8", newline="") as output_handle:
            if output_format == "json":
                output_handle.write("[")
            writer = None
            if output_format in {"csv", "tsv"}:
                safe_fields = [_safe_export_value(field, specification) for field in fields]
                writer = csv.DictWriter(
                    output_handle,
                    fieldnames=safe_fields,
                    delimiter="\t" if output_format == "tsv" else ",",
                    lineterminator="\n",
                    extrasaction="ignore",
                )
                if specification.get("include_header", True):
                    writer.writeheader()
            with source.open("r", encoding="utf-8") as input_handle:
                first = True
                for line in input_handle:
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    public = {
                        field: (
                            row.get("__provenance", []) if field == "provenance" else row.get(field)
                        )
                        for field in fields
                    }
                    if output_format == "json":
                        if not first:
                            output_handle.write(",")
                        output_handle.write(_json(public))
                    elif output_format in {"csv", "tsv"}:
                        assert writer is not None
                        writer.writerow(
                            {
                                safe_field: _safe_export_value(public[field], specification)
                                for field, safe_field in zip(fields, writer.fieldnames, strict=True)
                            }
                        )
                    else:
                        value = public.get(specification.get("text_field", "value"))
                        output_handle.write(_text(value) if value is not None else _json(public))
                        output_handle.write("\n")
                    first = False
            if output_format == "json":
                output_handle.write("]\n")

    def record_consumer(
        self,
        *,
        kind: str,
        job_id: str,
        artifact_id: str,
        reference_id: str | None,
        configuration: dict[str, Any],
    ) -> dict[str, Any]:
        payload = _json(configuration)
        consumer_id = str(uuid.uuid4())
        content_hash = hashlib.sha256(
            _json(
                {
                    "artifact_id": artifact_id,
                    "reference_id": reference_id,
                    "configuration": configuration,
                }
            ).encode()
        ).hexdigest()
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO data_studio_consumers(id,kind,job_id,artifact_id,reference_id,configuration_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    consumer_id,
                    kind,
                    job_id,
                    artifact_id,
                    reference_id,
                    payload,
                    content_hash,
                    _now(),
                ),
            )
        return {
            "id": consumer_id,
            "kind": kind,
            "job_id": job_id,
            "artifact_id": artifact_id,
            "reference_id": reference_id,
            "configuration": configuration,
            "content_hash": content_hash,
        }

    def _publish(
        self,
        job_id: str,
        processed: int,
        output_count: int,
        rejected: int,
        summary: dict[str, Any],
        files: list[tuple[str, Path, str]],
    ) -> None:
        now = _now()
        with self.store.transaction(immediate=True) as connection:
            for kind, path, media_type in files:
                connection.execute(
                    "INSERT INTO data_studio_artifacts(id,job_id,kind,filename,path,media_type,size_bytes,sha256,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        str(uuid.uuid4()),
                        job_id,
                        kind,
                        path.name,
                        str(path),
                        media_type,
                        path.stat().st_size,
                        _sha256(path),
                        now,
                    ),
                )
            connection.execute(
                "UPDATE data_studio_jobs SET status='completed',processed_count=?,output_count=?,rejected_count=?,summary_json=?,completed_at=?,updated_at=? WHERE id=?",
                (processed, output_count, rejected, _json(summary), now, now, job_id),
            )

    def artifact_path(self, job_id: str, artifact_id: str) -> tuple[Path, str]:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT path,filename FROM data_studio_artifacts WHERE id=? AND job_id=?",
                (artifact_id, job_id),
            ).fetchone()
        if row is None:
            raise DataStudioError("artifact was not found", "artifact_not_found")
        path = Path(row["path"]).resolve()
        if not path.is_file() or self.root.resolve() not in path.parents:
            raise DataStudioError("artifact is not available", "artifact_not_found")
        return path, row["filename"]

    def create_job_from_artifact(
        self,
        source_job_id: str,
        artifact_id: str,
        recipe_revision_id: str,
        *,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        path, filename = self.artifact_path(source_job_id, artifact_id)
        return self.create_job(path, filename, recipe_revision_id, correlation_id=correlation_id)

    def _execute_rows(
        self,
        rows: list[dict[str, Any]],
        definition: dict[str, Any],
        *,
        preview: bool = False,
        dedupe_seen: dict[tuple[str, ...], set[tuple[Any, ...]]] | None = None,
        aggregate_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        working = [dict(row) for row in rows]
        errors: list[dict[str, Any]] = []
        removed = 0
        for step in definition.get("steps", []):
            kind = step["type"]
            if kind == "select":
                fields = step.get("fields", [])
                working = [
                    {field: row.get(field) for field in fields}
                    | {"__source_row": row.get("__source_row")}
                    for row in working
                ]
            elif kind == "rename":
                mapping = step.get("mapping", {})
                for row in working:
                    for old, new in mapping.items():
                        if old in row:
                            row[new] = row.pop(old)
            elif kind == "drop":
                for row in working:
                    for field in step.get("fields", []):
                        row.pop(field, None)
            elif kind == "normalize":
                for row in working:
                    for field in step.get("fields", []):
                        value = _text(row.get(field))
                        operation = step.get("operation", "whitespace")
                        row[field] = (
                            normalize_email(value)
                            if operation == "email"
                            else value.strip()
                            if operation == "whitespace"
                            else value.lower()
                            if operation == "lower"
                            else value.upper()
                        )
            elif kind == "cast":
                for row in working:
                    try:
                        row[step["field"]] = _cast(row.get(step["field"]), step["target"])
                    except (TypeError, ValueError) as error:
                        errors.append(
                            {
                                "source_row": row.get("__source_row"),
                                "field": step["field"],
                                "message": str(error),
                            }
                        )
            elif kind == "fill":
                for row in working:
                    for field, fallback in step.get("values", {}).items():
                        if _is_missing(row.get(field)):
                            row[field] = fallback
            elif kind == "validate":
                for row in working:
                    for field in step.get("required", []):
                        if _is_missing(row.get(field)):
                            errors.append(
                                {
                                    "source_row": row.get("__source_row"),
                                    "field": field,
                                    "message": "required value is missing",
                                }
                            )
                    for field, allowed in step.get("allowed", {}).items():
                        if not _is_missing(row.get(field)) and row.get(field) not in allowed:
                            errors.append(
                                {
                                    "source_row": row.get("__source_row"),
                                    "field": field,
                                    "message": "value is not allowed",
                                }
                            )
            elif kind == "filter":
                before = len(working)
                working = [row for row in working if _condition(row, step.get("condition", {}))]
                removed += before - len(working)
            elif kind == "computed":
                for row in working:
                    try:
                        row[step["field"]] = _computed(row, step["expression"])
                    except (DataStudioError, TypeError, ValueError, OverflowError) as error:
                        errors.append(
                            {
                                "source_row": row.get("__source_row"),
                                "field": step["field"],
                                "message": str(error),
                            }
                        )
            elif kind == "provider_classify":
                field = step.get("field", "email")
                output_field = step.get("output_field", "provider")
                catalog = ProviderCatalog(Path("MSORTER/config/providers.json"))
                for row in working:
                    email = normalize_email(_text(row.get(field)))
                    row[output_field] = catalog.classify(email, dns_status="offline")
            elif kind == "dedupe":
                fields = step.get("fields", ["email"])
                key_fields = tuple(fields)
                seen = (
                    dedupe_seen.setdefault(key_fields, set()) if dedupe_seen is not None else set()
                )
                unique = []
                for row in working:
                    key = tuple(row.get(field) for field in fields)
                    if key not in seen:
                        seen.add(key)
                        unique.append(row)
                    else:
                        removed += 1
                working = unique
            elif kind == "aggregate":
                if aggregate_state is None:
                    working = self._aggregate(working, step)
                else:
                    self._accumulate_aggregate(working, step, aggregate_state)
                    working = []
            elif kind == "extension":
                extension_id = step.get("extension_id")
                try:
                    extension = self.get_extension(extension_id)
                except DataStudioError:
                    extension = self.register_extension(extension_id)
                if not extension["enabled"]:
                    raise DataStudioError(
                        f"trusted extension is disabled: {extension_id}", "extension_disabled"
                    )
                for row in working:
                    source_field = step.get("field", "email")
                    output_field = step.get("output_field", "email_domain")
                    row[output_field] = _text(row.get(source_field)).rsplit("@", 1)[-1].lower()
        return {
            "rows": working[:MAX_PREVIEW_ROWS] if preview else working,
            "errors": errors,
            "removed": removed,
            "counts": {
                "input": len(rows),
                "output": len(working),
                "errors": len(errors),
                "removed": removed,
            },
        }

    @staticmethod
    def _aggregate(rows: list[dict[str, Any]], step: dict[str, Any]) -> list[dict[str, Any]]:
        group_fields = step.get("group_by", [])
        groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            groups[tuple(row.get(field) for field in group_fields)].append(row)
        result = []
        for key, items in groups.items():
            record = dict(zip(group_fields, key, strict=False))
            record[step.get("count_field", "count")] = len(items)
            for field in step.get("sum", []):
                record[f"{field}_sum"] = sum(float(item.get(field) or 0) for item in items)
            result.append(record)
        return result

    @staticmethod
    def _accumulate_aggregate(
        rows: list[dict[str, Any]], step: dict[str, Any], state: dict[str, Any]
    ) -> None:
        group_fields = step.get("group_by", [])
        groups: dict[tuple[Any, ...], dict[str, Any]] = state["groups"]
        for row in rows:
            key = tuple(row.get(field) for field in group_fields)
            if key not in groups:
                if len(groups) >= MAX_AGGREGATE_GROUPS:
                    raise DataStudioError(
                        "aggregate exceeds the bounded group limit", "aggregate_limit"
                    )
                groups[key] = {
                    "count": 0,
                    "sums": {field: 0.0 for field in step.get("sum", [])},
                }
            group = groups[key]
            group["count"] += 1
            for field in step.get("sum", []):
                group["sums"][field] += float(row.get(field) or 0)

    @staticmethod
    def _finalize_aggregate(state: dict[str, Any]) -> list[dict[str, Any]]:
        step = state["step"]
        group_fields = step.get("group_by", [])
        result = []
        for key, values in state["groups"].items():
            record = dict(zip(group_fields, key, strict=False))
            record[step.get("count_field", "count")] = values["count"]
            for field, total in values["sums"].items():
                record[f"{field}_sum"] = total
            result.append(record)
        return result
