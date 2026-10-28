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
    path: str | Path, *, max_bytes: int = MAX_SOURCE_BYTES
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
        yield record


def detect_schema(path: str | Path, *, sample_limit: int = 100) -> dict[str, Any]:
    fields: list[str] = []
    types: dict[str, str] = {}
    samples: list[dict[str, Any]] = []
    for record in iter_source_records(path):
        if len(samples) < sample_limit:
            samples.append(dict(record))
        for field, value in record.items():
            if field == "__source_row":
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
        }
        for step in definition["steps"]:
            if not isinstance(step, dict) or step.get("type") not in allowed:
                raise DataStudioError("recipe contains an unsupported step", "recipe_invalid")
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

    def create_job(
        self, source_path: str | Path, source_filename: str, recipe_revision_id: str
    ) -> dict[str, Any]:
        source = Path(source_path).resolve()
        if not source.is_file():
            raise DataStudioError("source file was not retained", "source_missing")
        with self.store.connect() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM data_studio_recipe_revisions WHERE id=?", (recipe_revision_id,)
                ).fetchone()
                is None
            ):
                raise DataStudioError("recipe revision was not found", "revision_not_found")
        job_id, job_dir = str(uuid.uuid4()), (self.root / str(uuid.uuid4())).resolve()
        job_dir.mkdir(parents=True, exist_ok=False)
        retained = job_dir / f"input-{_safe_filename(source_filename)}"
        shutil.copyfile(source, retained)
        now = _now()
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO data_studio_jobs(id,recipe_revision_id,source_filename,source_path,source_sha256,status,created_at,updated_at,job_dir,correlation_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    recipe_revision_id,
                    _safe_filename(source_filename),
                    str(retained),
                    _sha256(retained),
                    "pending",
                    now,
                    now,
                    str(job_dir),
                    f"data-studio:{job_id}",
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
            output = Path(row["job_dir"]) / "output.jsonl.tmp"
            errors = Path(row["job_dir"]) / "errors.jsonl.tmp"
            count = rejected = processed = 0
            summary = {"removed": 0, "changed_fields": [], "validation_errors": 0}
            dedupe_seen: dict[tuple[str, ...], set[tuple[Any, ...]]] = {}
            with (
                output.open("w", encoding="utf-8") as out,
                errors.open("w", encoding="utf-8") as error_out,
            ):
                batch: list[dict[str, Any]] = []
                for source_row in iter_source_records(row["source_path"]):
                    batch.append(source_row)
                    processed += 1
                    if len(batch) >= 500:
                        result = self._execute_rows(batch, definition, dedupe_seen=dedupe_seen)
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
                    result = self._execute_rows(batch, definition, dedupe_seen=dedupe_seen)
                    for item in result["rows"]:
                        out.write(_json(item) + "\n")
                        count += 1
                    for item in result["errors"]:
                        error_out.write(_json(item) + "\n")
                        rejected += 1
                    summary["removed"] += result["removed"]
                    summary["validation_errors"] += len(result["errors"])
            final = Path(row["job_dir"]) / "output.jsonl"
            error_final = Path(row["job_dir"]) / "errors.jsonl"
            output.replace(final)
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

    def _execute_rows(
        self,
        rows: list[dict[str, Any]],
        definition: dict[str, Any],
        *,
        preview: bool = False,
        dedupe_seen: dict[tuple[str, ...], set[tuple[Any, ...]]] | None = None,
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
                working = self._aggregate(working, step)
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
