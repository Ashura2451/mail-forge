"""Standalone, durable List Tools orchestration over the canonical sorter."""

# SQL statements and compact artifact projections remain readable as fragments.
# ruff: noqa: E501

from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mailforge.application.ingest import InputLimitError, iter_raw_values
from mailforge.application.sorting import EmailSorter, SortConfig
from mailforge.domain.email import extract_addresses, normalize_email, validate_email
from mailforge.domain.sorting import DNSMode
from mailforge.infrastructure.campaign_store import CampaignStore

MAX_UPLOAD_BYTES = 1_073_741_824
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


class ListProcessingError(ValueError):
    def __init__(self, message: str, code: str = "list_processing_invalid"):
        super().__init__(message)
        self.code = code


class _PauseRequested(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _safe_filename(value: str, fallback: str = "input.txt") -> str:
    name = Path(value or fallback).name
    cleaned = _SAFE_FILENAME.sub("-", name).strip(".-")
    return (cleaned or fallback)[:180]


class ListProcessingService:
    """Owns job metadata and artifacts; EmailSorter owns address semantics."""

    def __init__(self, store: CampaignStore, root: str | Path | None = None):
        self.store = store
        self.root = Path(root or self.store.path.parent / "list-tools")
        self.root.mkdir(parents=True, exist_ok=True)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mailforge-list")
        self._running: set[str] = set()
        self._lock = threading.Lock()

    def close(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=False)

    def create_job(
        self,
        source_path: str | Path,
        source_filename: str,
        *,
        profile: str = "full",
        dns_mode: str = "offline",
    ) -> dict[str, Any]:
        path = Path(source_path).resolve()
        if not path.is_file():
            raise ListProcessingError("uploaded input was not retained", "input_missing")
        size = path.stat().st_size
        if size > MAX_UPLOAD_BYTES:
            raise ListProcessingError(
                "input exceeds the 1 GiB local processing limit", "input_limit"
            )
        try:
            mode = DNSMode(dns_mode)
        except ValueError as error:
            raise ListProcessingError("unsupported DNS mode", "dns_mode_invalid") from error
        if profile not in {"full", "validation", "dedupe", "classify", "legacy-export", "report"}:
            raise ListProcessingError("unsupported processing profile", "profile_invalid")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        job_id = str(uuid.uuid4())
        job_dir = (self.root / job_id).resolve()
        job_dir.mkdir(parents=True, exist_ok=False)
        retained_input = job_dir / f"input-{_safe_filename(source_filename)}"
        shutil.copyfile(path, retained_input)
        path = retained_input
        checkpoint = job_dir / "checkpoint.json"
        now = _now()
        correlation_id = f"list-tools:{job_id}"
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO list_processing_jobs(id,source_filename,source_path,source_sha256,profile,dns_mode,status,created_at,updated_at,checkpoint_path,job_dir,correlation_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    _safe_filename(source_filename),
                    str(path),
                    digest.hexdigest(),
                    profile,
                    mode.value,
                    "pending",
                    now,
                    now,
                    str(checkpoint),
                    str(job_dir),
                    correlation_id,
                ),
            )
        return self.get_job(job_id)

    def preview(self, source_path: str | Path, *, sample_limit: int = 20) -> dict[str, Any]:
        path = Path(source_path)
        seen: set[str] = set()
        samples: list[str] = []
        invalid = duplicates = values = 0
        try:
            for raw in iter_raw_values(path, max_bytes=MAX_UPLOAD_BYTES):
                values += 1
                extracted = extract_addresses(raw)
                if not extracted:
                    invalid += 1
                    continue
                for candidate in extracted:
                    normalized = normalize_email(candidate)
                    if not validate_email(normalized).valid:
                        invalid += 1
                    elif normalized in seen:
                        duplicates += 1
                    else:
                        seen.add(normalized)
                        if len(samples) < sample_limit:
                            samples.append(normalized)
        except (OSError, InputLimitError) as error:
            raise ListProcessingError(str(error), "input_invalid") from error
        return {
            "values": values,
            "accepted": len(seen),
            "invalid": invalid,
            "duplicates": duplicates,
            "sample": samples,
        }

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM list_processing_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise ListProcessingError("list processing job was not found", "job_not_found")
            artifacts = connection.execute(
                "SELECT id,kind,provider_key,filename,size_bytes,sha256,created_at FROM list_processing_artifacts WHERE job_id = ? ORDER BY kind, filename",
                (job_id,),
            ).fetchall()
        result = dict(row)
        result["summary"] = json.loads(result.pop("summary_json") or "{}")
        result.pop("source_path", None)
        result.pop("checkpoint_path", None)
        result.pop("job_dir", None)
        result["artifacts"] = [dict(item) for item in artifacts]
        return result

    def start(self, job_id: str, *, resume: bool = False) -> dict[str, Any]:
        self.get_job(job_id)
        with self._lock:
            if job_id in self._running:
                return self.get_job(job_id)
            self._running.add(job_id)
        with self.store.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT status FROM list_processing_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise ListProcessingError("list processing job was not found", "job_not_found")
            if row["status"] in {"completed", "canceled"}:
                raise ListProcessingError("completed jobs cannot be started", "job_state")
            connection.execute(
                "UPDATE list_processing_jobs SET status = 'running', started_at = COALESCE(started_at, ?), updated_at = ?, error_message = NULL WHERE id = ?",
                (_now(), _now(), job_id),
            )
        self.executor.submit(self._run, job_id, resume)
        return self.get_job(job_id)

    def pause(self, job_id: str) -> dict[str, Any]:
        with self.store.transaction(immediate=True) as connection:
            changed = connection.execute(
                "UPDATE list_processing_jobs SET status = 'paused', updated_at = ? WHERE id = ? AND status = 'running'",
                (_now(), job_id),
            ).rowcount
            if not changed:
                row = connection.execute(
                    "SELECT status FROM list_processing_jobs WHERE id = ?", (job_id,)
                ).fetchone()
                if row is None:
                    raise ListProcessingError("list processing job was not found", "job_not_found")
        return self.get_job(job_id)

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self.store.transaction(immediate=True) as connection:
            changed = connection.execute(
                "UPDATE list_processing_jobs SET status = 'canceled', updated_at = ? WHERE id = ? AND status IN ('pending','paused','running')",
                (_now(), job_id),
            ).rowcount
            if (
                not changed
                and connection.execute(
                    "SELECT 1 FROM list_processing_jobs WHERE id = ?", (job_id,)
                ).fetchone()
                is None
            ):
                raise ListProcessingError("list processing job was not found", "job_not_found")
        return self.get_job(job_id)

    def _run(self, job_id: str, resume: bool) -> None:
        try:
            with self.store.connect() as connection:
                row = connection.execute(
                    "SELECT * FROM list_processing_jobs WHERE id = ?", (job_id,)
                ).fetchone()
            if row is None:
                return
            config = SortConfig.for_profile(
                row["profile"],
                results_dir=Path(row["job_dir"]) / "outputs",
                checkpoint_path=Path(row["checkpoint_path"]),
                dns_mode=DNSMode(row["dns_mode"]),
                max_bytes=MAX_UPLOAD_BYTES,
                checkpoint_interval=1_000,
                progress_every=1,
            )
            sorter = EmailSorter(config)

            def progress(processed: int, accepted: int) -> None:
                with self.store.connect() as connection:
                    current = connection.execute(
                        "SELECT status FROM list_processing_jobs WHERE id = ?", (job_id,)
                    ).fetchone()
                if current is None or current["status"] == "canceled":
                    raise _PauseRequested("job canceled")
                if current["status"] == "paused":
                    raise _PauseRequested("job paused")
                with self.store.transaction(immediate=True) as connection:
                    connection.execute(
                        "UPDATE list_processing_jobs SET processed_count = ?, accepted_count = ?, updated_at = ? WHERE id = ?",
                        (processed, accepted, _now(), job_id),
                    )

            result = sorter.sort_file(
                Path(row["source_path"]),
                resume=resume,
                input_fn=lambda _prompt: "y",
                progress_fn=progress,
            )
            output_dir = Path(row["job_dir"]) / "outputs"
            self._write_provider_csvs(output_dir)
            summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
            artifacts = self._artifact_manifest(job_id, output_dir)
            with self.store.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE list_processing_jobs SET status = 'completed', processed_count = ?, accepted_count = ?, summary_json = ?, completed_at = ?, updated_at = ? WHERE id = ?",
                    (
                        result.stats.total_extracted,
                        result.stats.unique_valid,
                        json.dumps(summary, sort_keys=True),
                        _now(),
                        _now(),
                        job_id,
                    ),
                )
                self.store.record_event(
                    connection,
                    "list_processing_job",
                    job_id,
                    "list_processing_completed",
                    {
                        "job_id": job_id,
                        "correlation_id": row["correlation_id"],
                        "source_filename": row["source_filename"],
                        "summary": summary,
                        "artifacts": artifacts,
                    },
                )
        except _PauseRequested:
            with self.store.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE list_processing_jobs SET status = 'paused', updated_at = ? WHERE id = ?",
                    (_now(), job_id),
                )
        except Exception as error:
            with self.store.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE list_processing_jobs SET status = 'failed', error_message = ?, updated_at = ? WHERE id = ?",
                    (str(error)[:500], _now(), job_id),
                )
        finally:
            with self._lock:
                self._running.discard(job_id)

    def _write_provider_csvs(self, job_dir: Path) -> None:
        structured = job_dir / "processing.jsonl"
        if not structured.exists():
            return
        grouped: dict[str, list[dict[str, Any]]] = {}
        with structured.open(encoding="utf-8") as handle:
            for line in handle:
                item = json.loads(line)
                classification = item.get("classification_result", {})
                provider = str(classification.get("provider", "unknown"))
                grouped.setdefault(provider, []).append(
                    {
                        "email": item.get("normalized_address", ""),
                        "provider": provider,
                        "disposition": item.get("processing_disposition", ""),
                    }
                )
        for provider, rows in grouped.items():
            path = job_dir / f"provider-{_safe_filename(provider, 'unknown')}.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["email", "provider", "disposition"])
                writer.writeheader()
                writer.writerows(rows)

    def _artifact_manifest(self, job_id: str, job_dir: Path) -> list[dict[str, Any]]:
        manifest: list[dict[str, Any]] = []
        with self.store.transaction(immediate=True) as connection:
            connection.execute("DELETE FROM list_processing_artifacts WHERE job_id = ?", (job_id,))
            for path in sorted(job_dir.iterdir()):
                if not path.is_file() or path.name.startswith("checkpoint") or path.suffix == ".db":
                    continue
                kind = (
                    "summary_json"
                    if path.name == "summary.json"
                    else "master_csv"
                    if path.name == "master.csv"
                    else "quarantine_jsonl"
                    if path.name == "quarantine.jsonl"
                    else "provider_csv"
                    if path.name.startswith("provider-") and path.suffix == ".csv"
                    else "provider_txt"
                    if path.suffix == ".txt"
                    else "diagnostic"
                )
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                item = {
                    "kind": kind,
                    "filename": path.name,
                    "path": str(path),
                    "size_bytes": path.stat().st_size,
                    "sha256": digest,
                }
                connection.execute(
                    "INSERT INTO list_processing_artifacts(id,job_id,kind,provider_key,filename,path,size_bytes,sha256,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        str(uuid.uuid4()),
                        job_id,
                        kind,
                        None,
                        path.name,
                        str(path),
                        item["size_bytes"],
                        digest,
                        _now(),
                    ),
                )
                manifest.append(
                    {
                        "kind": kind,
                        "filename": path.name,
                        "size_bytes": item["size_bytes"],
                        "sha256": digest,
                    }
                )
        return manifest

    def artifact_path(self, job_id: str, artifact_id: str) -> tuple[Path, str]:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT path, filename FROM list_processing_artifacts WHERE id = ? AND job_id = ?",
                (artifact_id, job_id),
            ).fetchone()
        if row is None:
            raise ListProcessingError("artifact was not found", "artifact_not_found")
        path = Path(row["path"]).resolve()
        root = (self.root / job_id).resolve()
        if root not in path.parents or not path.is_file():
            raise ListProcessingError("artifact path is not allowed", "artifact_forbidden")
        return path, row["filename"]
