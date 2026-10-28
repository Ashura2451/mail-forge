"""Canonical registry and bounded evaluator for Mailforge functions."""

# SQL and compact evaluator statements remain readable as fragments.
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mailforge.domain.email import classify_address, normalize_email
from mailforge.infrastructure.campaign_store import CampaignStore

MAX_RECORDS = 10_000
MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_JSON_BYTES = 256 * 1024
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_TEXT = 10_000
MAX_PATTERN = 160
MAX_TIMEOUT_SECONDS = 30
SAFE_ID = re.compile(r"^[a-z][a-z0-9_.-]{1,79}$")
UNSAFE_PATTERN = re.compile(r"(?:\(\?[^)]|\([^)]*[+*][^)]*\)[+*]|\\[1-9])")


class ProgrammableOperationsError(ValueError):
    def __init__(self, message: str, code: str = "function_invalid"):
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _field(record: dict[str, Any], path: str) -> Any:
    current: Any = record
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def email_domain_normalizer(records: list[dict[str, Any]], parameters: dict[str, Any]) -> list[dict[str, Any]]:
    """Reviewed reference extension matching the original Data Studio extension."""

    source = str(parameters.get("field", "email"))
    target = str(parameters.get("output_field", "email_domain"))
    return [{**record, target: normalize_email(_text(record.get(source))).rsplit("@", 1)[-1]} for record in records]


def structured_label_normalizer(records: list[dict[str, Any]], parameters: dict[str, Any]) -> list[dict[str, Any]]:
    """Reviewed reference transformation for structured personalization data."""

    first = str(parameters.get("first_field", "first_name"))
    company = str(parameters.get("company_field", "company"))
    target = str(parameters.get("output_field", "personalization_label"))
    result = []
    for record in records:
        name = " ".join(_text(record.get(first)).strip().split())
        organization = " ".join(_text(record.get(company)).strip().split())
        label = " at ".join(value for value in (name, organization) if value) or "there"
        result.append({**record, target: label[:MAX_TEXT]})
    return result


REFERENCE_EXTENSIONS = {
    "email_domain_normalizer": email_domain_normalizer,
    "structured_label_normalizer": structured_label_normalizer,
}


class ProgrammableOperationsService:
    """Single registry and execution authority shared by product surfaces."""

    def __init__(self, store: CampaignStore, *, extension_root: str | Path | None = None):
        self.store = store
        self.extension_root = Path(extension_root or self.store.path.parent / "extensions").resolve()
        self.extension_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._ensure_reference_functions()
        self._recover_executions()

    def _recover_executions(self) -> None:
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE programmable_executions SET status='failed',error_code='interrupted',"
                "error_message='execution was interrupted before completion',completed_at=? "
                "WHERE status='running'",
                (_now(),),
            )

    def _ensure_reference_functions(self) -> None:
        declarative = {
            "operation": "email.domain",
            "input_schema": {"type": "object", "required": ["email"]},
            "output_schema": {"type": "object", "required": ["email_domain"]},
            "contexts": ["data_studio", "message_studio", "inbound", "automation"],
            "deterministic": True,
            "parameters": {"field": "email", "output_field": "email_domain"},
        }
        structured = {
            "operation": "structured.label",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object", "required": ["personalization_label"]},
            "contexts": ["data_studio", "message_studio"],
            "deterministic": True,
            "parameters": {"first_field": "first_name", "company_field": "company", "output_field": "personalization_label"},
        }
        self._ensure_reference("email_domain_normalizer", "python", "1.0", declarative, "builtin:email_domain_normalizer")
        self._ensure_reference("structured_label_normalizer", "python", "1.0", structured, "builtin:structured_label_normalizer")

    def _ensure_reference(self, name: str, kind: str, version: str, definition: dict[str, Any], source: str) -> None:
        now = _now()
        function_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"mailforge-function:{name}"))
        manifest = {"name": name, "version": version, "entrypoint": "run", "reference": True}
        with self.store.transaction(immediate=True) as connection:
            row = connection.execute("SELECT id FROM programmable_functions WHERE name=?", (name,)).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO programmable_functions(id,name,kind,version,status,enabled,trusted,source_path,source_sha256,manifest_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (function_id, name, kind, version, "active", 0, 1, source, _hash(manifest), _canonical(manifest), now, now),
                )
            revision = connection.execute(
                "SELECT id FROM programmable_function_revisions WHERE function_id=? AND revision_number=1",
                (function_id,),
            ).fetchone()
            if revision is None:
                connection.execute(
                    "INSERT INTO programmable_function_revisions(id,function_id,revision_number,definition_json,content_hash,created_at) VALUES(?,?,?,?,?,?)",
                    (str(uuid.uuid4()), function_id, 1, _canonical(definition), _hash(definition), now),
                )

    def list_functions(self) -> list[dict[str, Any]]:
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM programmable_functions WHERE status='active' ORDER BY name"
            ).fetchall()
        return [self._function(row) for row in rows]

    def _function(self, row: Any) -> dict[str, Any]:
        result = dict(row)
        result["enabled"] = bool(result["enabled"])
        result["trusted"] = bool(result["trusted"])
        result["manifest"] = json.loads(result.pop("manifest_json") or "{}")
        return result

    def get_function(self, name_or_id: str) -> dict[str, Any]:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM programmable_functions WHERE (id=? OR name=?) AND status='active'",
                (name_or_id, name_or_id),
            ).fetchone()
        if row is None:
            raise ProgrammableOperationsError("function was not found", "function_not_found")
        return self._function(row)

    def get_revision(self, revision_id: str) -> dict[str, Any]:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT r.*,f.name,f.kind,f.enabled,f.trusted,f.source_path AS function_source_path,"
                "f.source_sha256 AS function_source_sha256,f.manifest_json AS function_manifest_json "
                "FROM programmable_function_revisions r JOIN programmable_functions f ON f.id=r.function_id WHERE r.id=?",
                (revision_id,),
            ).fetchone()
        if row is None:
            raise ProgrammableOperationsError("function revision was not found", "revision_not_found")
        result = dict(row)
        result["definition"] = json.loads(result.pop("definition_json"))
        revision_manifest = json.loads(result.pop("manifest_json") or "{}")
        function_source_path = result.pop("function_source_path")
        function_source_sha256 = result.pop("function_source_sha256")
        function_manifest = json.loads(result.pop("function_manifest_json") or "{}")
        result["source_path"] = result["source_path"] or function_source_path
        result["source_sha256"] = result["source_sha256"] or function_source_sha256
        result["manifest"] = revision_manifest or function_manifest
        result["enabled"] = bool(result["enabled"])
        result["trusted"] = bool(result["trusted"])
        return result

    def list_revisions(self, name_or_id: str) -> list[dict[str, Any]]:
        function = self.get_function(name_or_id)
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM programmable_function_revisions WHERE function_id=? ORDER BY revision_number DESC",
                (function["id"],),
            ).fetchall()
        return [self.get_revision(row["id"]) for row in rows]

    def list_executions(self, name_or_id: str | None = None, *, limit: int = 100) -> list[dict[str, Any]]:
        params: list[Any] = []
        query = "SELECT e.*,f.name FROM programmable_executions e JOIN programmable_functions f ON f.id=e.function_id"
        if name_or_id:
            query += " WHERE f.name=? OR f.id=?"
            params.extend((name_or_id, name_or_id))
        query += " ORDER BY e.started_at DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        with self.store.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def create_function(self, name: str, definition: dict[str, Any], *, version: str = "1.0") -> dict[str, Any]:
        self._validate_definition(definition)
        if not SAFE_ID.fullmatch(name):
            raise ProgrammableOperationsError("function name is invalid", "function_invalid")
        now = _now()
        function_id = str(uuid.uuid4())
        revision_id = str(uuid.uuid4())
        with self.store.transaction(immediate=True) as connection:
            if connection.execute("SELECT 1 FROM programmable_functions WHERE name=?", (name,)).fetchone():
                raise ProgrammableOperationsError("function name is already registered", "function_conflict")
            connection.execute(
                "INSERT INTO programmable_functions(id,name,kind,version,status,enabled,trusted,manifest_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (function_id, name, "declarative", version, "active", 1, 1, _canonical({"name": name, "version": version}), now, now),
            )
            connection.execute(
                "INSERT INTO programmable_function_revisions(id,function_id,revision_number,definition_json,content_hash,source_path,source_sha256,manifest_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (revision_id, function_id, 1, _canonical(definition), _hash(definition), None, None, "{}", now),
            )
        return self.get_revision(revision_id)

    def create_revision(self, name: str, definition: dict[str, Any], *, version: str | None = None) -> dict[str, Any]:
        function = self.get_function(name)
        self._validate_definition(definition)
        with self.store.transaction(immediate=True) as connection:
            number = connection.execute(
                "SELECT COALESCE(MAX(revision_number),0)+1 FROM programmable_function_revisions WHERE function_id=?",
                (function["id"],),
            ).fetchone()[0]
            revision_id = str(uuid.uuid4())
            now = _now()
            connection.execute(
                "INSERT INTO programmable_function_revisions(id,function_id,revision_number,definition_json,content_hash,source_path,source_sha256,manifest_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (revision_id, function["id"], number, _canonical(definition), _hash(definition), function.get("source_path"), function.get("source_sha256"), _canonical(function.get("manifest", {})), now),
            )
            if version:
                connection.execute("UPDATE programmable_functions SET version=?,updated_at=? WHERE id=?", (version, now, function["id"]))
        return self.get_revision(revision_id)

    def set_enabled(self, name: str, enabled: bool) -> dict[str, Any]:
        function = self.get_function(name)
        if enabled and function["kind"] == "python" and not function["trusted"]:
            raise ProgrammableOperationsError("Python extension is not trusted", "trust_required")
        with self.store.transaction(immediate=True) as connection:
            connection.execute("UPDATE programmable_functions SET enabled=?,updated_at=? WHERE id=?", (int(enabled), _now(), function["id"]))
            # Preserve the legacy Data Studio visibility for existing clients.
            connection.execute("UPDATE data_studio_extensions SET enabled=?,updated_at=? WHERE name=?", (int(enabled), _now(), name))
        return self.get_function(name)

    def install_python_extension(self, source: str | Path, manifest: dict[str, Any], *, confirm: bool = False) -> dict[str, Any]:
        if not confirm:
            raise ProgrammableOperationsError("explicit checksum confirmation is required", "confirmation_required")
        source_path = Path(source).expanduser()
        if not source_path.is_file() or source_path.is_symlink():
            raise ProgrammableOperationsError("extension source must be a regular local file", "source_invalid")
        name = manifest.get("name")
        version = manifest.get("version", "1.0")
        entrypoint = manifest.get("entrypoint", "run")
        definition = manifest.get("definition")
        if not isinstance(name, str) or not SAFE_ID.fullmatch(name) or not isinstance(definition, dict):
            raise ProgrammableOperationsError("extension manifest is invalid", "manifest_invalid")
        if not isinstance(version, str) or not isinstance(entrypoint, str) or not SAFE_ID.fullmatch(entrypoint):
            raise ProgrammableOperationsError("extension manifest is invalid", "manifest_invalid")
        self._validate_definition(definition)
        digest = _file_hash(source_path)
        target_dir = self.extension_root / name / version
        target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = target_dir / "extension.py"
        shutil.copyfile(source_path, target)
        os.chmod(target, 0o600)
        now = _now()
        function_id = str(uuid.uuid4())
        revision_id = str(uuid.uuid4())
        safe_manifest = {"name": name, "version": version, "entrypoint": entrypoint, "definition": definition, "source_sha256": digest}
        with self.store.transaction(immediate=True) as connection:
            existing = connection.execute("SELECT id FROM programmable_functions WHERE name=?", (name,)).fetchone()
            if existing:
                function_id = existing["id"]
                connection.execute(
                    "UPDATE programmable_functions SET kind='python',version=?,trusted=1,enabled=0,source_path=?,source_sha256=?,manifest_json=?,updated_at=? WHERE id=?",
                    (
                        version,
                        str(target.relative_to(self.extension_root.parent)),
                        digest,
                        _canonical(safe_manifest),
                        now,
                        function_id,
                    ),
                )
                number = connection.execute("SELECT COALESCE(MAX(revision_number),0)+1 FROM programmable_function_revisions WHERE function_id=?", (function_id,)).fetchone()[0]
            else:
                connection.execute(
                    "INSERT INTO programmable_functions(id,name,kind,version,status,enabled,trusted,source_path,source_sha256,manifest_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        function_id,
                        name,
                        "python",
                        version,
                        "active",
                        0,
                        1,
                        str(target.relative_to(self.extension_root.parent)),
                        digest,
                        _canonical(safe_manifest),
                        now,
                        now,
                    ),
                )
                number = 1
            connection.execute(
                "INSERT INTO programmable_function_revisions(id,function_id,revision_number,definition_json,content_hash,source_path,source_sha256,manifest_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (revision_id, function_id, number, _canonical(definition), _hash(definition), str(target.relative_to(self.extension_root.parent)), digest, _canonical(safe_manifest), now),
            )
        return self.get_revision(revision_id)

    def execute(
        self,
        name_or_id: str,
        records: list[dict[str, Any]] | dict[str, Any],
        *,
        revision_id: str | None = None,
        parameters: dict[str, Any] | None = None,
        context: str = "data_studio",
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        function = self.get_function(name_or_id)
        rows = [dict(records)] if isinstance(records, dict) else records
        if not isinstance(rows, list) or len(rows) > MAX_RECORDS or any(not isinstance(row, dict) for row in rows):
            raise ProgrammableOperationsError("function input must contain bounded object records", "input_limit")
        params = parameters or {}
        if not isinstance(params, dict) or len(_canonical(params).encode()) > MAX_JSON_BYTES:
            raise ProgrammableOperationsError("function parameters are too large", "input_limit")
        if len(_canonical({"records": rows, "parameters": params}).encode()) > MAX_INPUT_BYTES:
            raise ProgrammableOperationsError("function input exceeds the local byte limit", "input_limit")
        revision = self._current_revision(function, revision_id)
        if context not in revision["definition"].get("contexts", [context]):
            raise ProgrammableOperationsError("function is not enabled for this context", "context_invalid")
        if not function["enabled"]:
            raise ProgrammableOperationsError("function is disabled", "function_disabled")
        correlation = correlation_id or str(uuid.uuid4())
        with self.store.connect() as connection:
            existing = connection.execute("SELECT * FROM programmable_executions WHERE correlation_id=?", (correlation,)).fetchone()
        if existing:
            if (
                existing["function_id"] != function["id"]
                or existing["revision_id"] != revision["id"]
                or existing["context"] != context
            ):
                raise ProgrammableOperationsError(
                    "correlation id is already bound to another function execution",
                    "correlation_conflict",
                )
            if existing["status"] == "completed":
                return {
                    "execution_id": existing["id"],
                    "status": existing["status"],
                    "records": json.loads(existing["output_json"]),
                    "function_id": function["id"],
                    "revision_id": revision["id"],
                    "content_hash": revision["content_hash"],
                }
            execution_id = existing["id"]
            with self.store.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE programmable_executions SET status='running',input_count=?,output_count=0,"
                    "output_json='[]',error_code=NULL,error_message=NULL,started_at=?,completed_at=NULL WHERE id=?",
                    (len(rows), _now(), execution_id),
                )
        else:
            execution_id = str(uuid.uuid4())
            with self.store.transaction(immediate=True) as connection:
                connection.execute(
                    "INSERT INTO programmable_executions(id,function_id,revision_id,correlation_id,context,status,input_count,started_at) VALUES(?,?,?,?,?,?,?,?)",
                    (execution_id, function["id"], revision["id"], correlation, context, "running", len(rows), _now()),
                )
        try:
            output = self._run(function, revision, rows, params)
            encoded = _canonical(output)
            if len(encoded.encode()) > MAX_OUTPUT_BYTES:
                raise ProgrammableOperationsError("function output exceeds the local limit", "output_limit")
            with self.store.transaction(immediate=True) as connection:
                connection.execute("UPDATE programmable_executions SET status='completed',output_count=?,output_json=?,completed_at=? WHERE id=?", (len(output), encoded, _now(), execution_id))
            return {
                "execution_id": execution_id,
                "status": "completed",
                "records": output,
                "function_id": function["id"],
                "revision_id": revision["id"],
                "content_hash": revision["content_hash"],
            }
        except Exception as error:
            code = getattr(error, "code", "execution_failed")
            with self.store.transaction(immediate=True) as connection:
                connection.execute("UPDATE programmable_executions SET status='failed',error_code=?,error_message=?,completed_at=? WHERE id=?", (code, str(error)[:1000], _now(), execution_id))
            if isinstance(error, ProgrammableOperationsError):
                raise
            raise ProgrammableOperationsError(str(error), code) from error

    def _current_revision(self, function: dict[str, Any], revision_id: str | None) -> dict[str, Any]:
        if revision_id:
            revision = self.get_revision(revision_id)
            if revision["function_id"] != function["id"]:
                raise ProgrammableOperationsError("revision does not belong to function", "revision_invalid")
            return revision
        with self.store.connect() as connection:
            row = connection.execute("SELECT id FROM programmable_function_revisions WHERE function_id=? ORDER BY revision_number DESC LIMIT 1", (function["id"],)).fetchone()
        return self.get_revision(row["id"]) if row else (_ for _ in ()).throw(ProgrammableOperationsError("function has no revision", "revision_not_found"))

    def _run(self, function: dict[str, Any], revision: dict[str, Any], rows: list[dict[str, Any]], parameters: dict[str, Any]) -> list[dict[str, Any]]:
        if function["kind"] == "declarative":
            return [self._evaluate(row, revision["definition"], parameters) for row in rows]
        manifest = revision["manifest"]
        source = function.get("source_path")
        if source == "builtin:email_domain_normalizer":
            return email_domain_normalizer(rows, parameters)
        if source == "builtin:structured_label_normalizer":
            return structured_label_normalizer(rows, parameters)
        source_path = (
            Path(source)
            if Path(source).is_absolute()
            else self.extension_root.parent / source
        )
        source_path = source_path.resolve()
        if not source or source_path.resolve().parent.parent.parent != self.extension_root:
            raise ProgrammableOperationsError("extension source is outside the application extension directory", "source_invalid")
        if not source_path.is_file() or _file_hash(source_path) != function.get("source_sha256"):
            raise ProgrammableOperationsError("extension checksum no longer matches", "checksum_mismatch")
        request = json.dumps({"records": rows, "parameters": parameters}, ensure_ascii=False).encode()
        def limit_process() -> None:
            try:
                import resource

                resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
                resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
                resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
            except (ImportError, OSError, ValueError):
                pass

        process = subprocess.Popen(
                [sys.executable, "-m", "mailforge.application.extension_runner", "--source", str(source_path), "--entrypoint", manifest.get("entrypoint", "run")],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                preexec_fn=limit_process if os.name == "posix" else None,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "PYTHONNOUSERSITE": "1",
                    "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
                },
            )
        try:
            stdout, _ = process.communicate(request, timeout=MAX_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as error:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except OSError:
                process.kill()
            process.wait(timeout=2)
            raise ProgrammableOperationsError("trusted extension timed out", "extension_timeout") from error
        if process.returncode != 0:
            raise ProgrammableOperationsError("trusted extension failed", "extension_failed")
        if len(stdout) > MAX_OUTPUT_BYTES:
            raise ProgrammableOperationsError("trusted extension output exceeds the local limit", "output_limit")
        try:
            output = json.loads(stdout)
        except json.JSONDecodeError as error:
            raise ProgrammableOperationsError("trusted extension returned invalid JSON", "extension_output") from error
        if not isinstance(output, list) or any(not isinstance(item, dict) for item in output):
            raise ProgrammableOperationsError("trusted extension must return object records", "extension_output")
        return output

    @classmethod
    def _validate_definition(cls, definition: dict[str, Any]) -> None:
        if not isinstance(definition, dict) or not isinstance(definition.get("operation"), str):
            raise ProgrammableOperationsError("function definition requires an operation", "definition_invalid")
        if len(_canonical(definition).encode()) > MAX_JSON_BYTES:
            raise ProgrammableOperationsError("function definition is too large", "definition_limit")
        contexts = definition.get("contexts", [])
        if not isinstance(contexts, list) or not contexts or any(context not in {"data_studio", "message_studio", "inbound", "automation"} for context in contexts):
            raise ProgrammableOperationsError("function contexts are invalid", "definition_invalid")
        operation = definition["operation"]
        allowed = {"text.trim", "text.lower", "text.upper", "text.whitespace", "text.replace", "text.extract", "text.substring", "text.join", "data.default", "data.coalesce", "data.calculate", "data.map_enum", "email.normalize", "email.domain", "email.classify", "structured.label"}
        if operation not in allowed:
            raise ProgrammableOperationsError("operation is not in the safe declarative allowlist", "operation_invalid")
        if operation == "text.extract":
            pattern = definition.get("parameters", {}).get("pattern", "")
            if not isinstance(pattern, str) or len(pattern) > MAX_PATTERN or UNSAFE_PATTERN.search(pattern):
                raise ProgrammableOperationsError("capture pattern is unsafe or too long", "pattern_invalid")

    @classmethod
    def _evaluate(cls, row: dict[str, Any], definition: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        operation = definition["operation"]
        config = {**definition.get("parameters", {}), **params}
        source = str(config.get("field", "value"))
        target = str(config.get("output_field", source))
        value = _field(row, source)
        if operation == "text.trim":
            result[target] = _text(value).strip()
        elif operation == "text.lower":
            result[target] = _text(value).lower()
        elif operation == "text.upper":
            result[target] = _text(value).upper()
        elif operation == "text.whitespace":
            result[target] = " ".join(_text(value).split())
        elif operation == "text.replace":
            result[target] = _text(value).replace(str(config.get("old", "")), str(config.get("new", "")))[:MAX_TEXT]
        elif operation == "text.substring":
            result[target] = _text(value)[int(config.get("start", 0)):int(config.get("end", MAX_TEXT))]
        elif operation == "text.join":
            result[target] = str(config.get("separator", " ")).join(_text(_field(row, field)) for field in config.get("fields", []))[:MAX_TEXT]
        elif operation == "text.extract":
            pattern = str(config.get("pattern", ""))
            match = re.search(pattern, _text(value))
            result[target] = match.group(1) if match and match.lastindex else (match.group(0) if match else "")
        elif operation == "data.default":
            result[target] = value if value not in (None, "") else config.get("default")
        elif operation == "data.coalesce":
            result[target] = next((item for item in (_field(row, field) for field in config.get("fields", [])) if item not in (None, "")), None)
        elif operation == "data.calculate":
            numbers = [float(_field(row, field) or 0) for field in config.get("fields", [])]
            operator = config.get("operator", "add")
            if operator == "add":
                result[target] = sum(numbers)
            elif operator == "multiply":
                result[target] = __import__("math").prod(numbers)
            elif operator == "subtract":
                result[target] = numbers[0] - sum(numbers[1:])
            else:
                raise ProgrammableOperationsError(
                    "calculation operator is invalid", "operation_invalid"
                )
        elif operation == "data.map_enum":
            result[target] = config.get("mapping", {}).get(str(value), config.get("default"))
        elif operation == "email.normalize":
            result[target] = normalize_email(_text(value))
        elif operation == "email.domain":
            result[target] = normalize_email(_text(value)).rsplit("@", 1)[-1]
        elif operation == "email.classify":
            result[target] = classify_address(normalize_email(_text(value)))
        elif operation == "structured.label":
            result[target] = " at ".join(_text(row.get(field)).strip() for field in (config.get("first_field", "first_name"), config.get("company_field", "company")) if _text(row.get(field)).strip()) or "there"
        else:
            raise ProgrammableOperationsError("operation is not executable", "operation_invalid")
        return result


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
