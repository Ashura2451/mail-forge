"""Unified sorting workflow owned by the canonical Mailforge package."""

from __future__ import annotations

import asyncio
import copy
import csv
import hashlib
import json
import logging
import os
import resource
import shutil
import signal
import sqlite3
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from mailforge.application.ingest import InputLimitError, iter_raw_values
from mailforge.domain.email import (
    classify_address,
    extract_addresses,
    normalize_email,
    validate_email,
)
from mailforge.domain.results import ProcessingDisposition, ProcessingResult, ReasonCode
from mailforge.domain.sorting import DNSMode, DNSResult, SortStats
from mailforge.infrastructure.checkpoint import read_checkpoint, write_checkpoint
from mailforge.infrastructure.disposable import DisposableDomainStore
from mailforge.infrastructure.dns import DNSCache, DNSService
from mailforge.infrastructure.providers import ProviderCatalog

log = logging.getLogger(__name__)


@dataclass
class SortConfig:
    results_dir: Path = Path("results")
    config_dir: Path = Path("config")
    log_dir: Path = Path("logs")
    checkpoint_path: Path = Path("checkpoint.json")
    providers_path: Path | None = None
    disposable_path: Path | None = None
    dns_mode: DNSMode = DNSMode.OFFLINE
    dns_timeout: float = 5.0
    dns_retry_count: int = 3
    dns_concurrency: int = 25
    max_bytes: int = 1_073_741_824
    checkpoint_interval: int = 10_000
    dns_cache_max_size: int = 100_000
    max_open_files: int = 256
    min_disk_space_gb: float = 0.1
    profile: str = "full"
    structured_output: bool = True
    quarantine_output: bool = True
    formula_safe_csv: bool = True
    progress_every: int = 0

    def __post_init__(self) -> None:
        self.results_dir = Path(self.results_dir)
        self.config_dir = Path(self.config_dir)
        self.log_dir = Path(self.log_dir)
        self.checkpoint_path = Path(self.checkpoint_path)
        self.providers_path = self.providers_path or self.config_dir / "providers.json"
        self.disposable_path = self.disposable_path or self.config_dir / "disposable.json"
        self.providers_path = Path(self.providers_path)
        self.disposable_path = Path(self.disposable_path)
        if self.profile not in {
            "full",
            "validation",
            "dedupe",
            "classify",
            "legacy-export",
            "report",
        }:
            raise ValueError(f"unknown processing profile: {self.profile}")
        if self.checkpoint_interval < 1:
            raise ValueError("checkpoint_interval must be positive")
        if self.progress_every < 0:
            raise ValueError("progress_every cannot be negative")

    @property
    def providers_file(self) -> str:
        return str(self.providers_path)

    @property
    def disposable_file(self) -> str:
        return str(self.disposable_path)

    @classmethod
    def from_environment(cls, **overrides: Any) -> SortConfig:
        values: dict[str, Any] = {}
        if os.getenv("MSORTER_RESULTS_DIR"):
            values["results_dir"] = Path(os.environ["MSORTER_RESULTS_DIR"])
        if os.getenv("MSORTER_CONFIG_DIR"):
            values["config_dir"] = Path(os.environ["MSORTER_CONFIG_DIR"])
        if os.getenv("MSORTER_LOG_DIR"):
            values["log_dir"] = Path(os.environ["MSORTER_LOG_DIR"])
        if os.getenv("MSORTER_CHECKPOINT_PATH"):
            values["checkpoint_path"] = Path(os.environ["MSORTER_CHECKPOINT_PATH"])
        if os.getenv("MSORTER_DNS_MODE"):
            values["dns_mode"] = DNSMode(os.environ["MSORTER_DNS_MODE"].lower())
        if os.getenv("MSORTER_PROFILE"):
            values["profile"] = os.environ["MSORTER_PROFILE"].lower()
        for name, field_name, converter in (
            ("MSORTER_DNS_TIMEOUT", "dns_timeout", float),
            ("MSORTER_DNS_RETRY_COUNT", "dns_retry_count", int),
            ("MSORTER_DNS_CONCURRENCY", "dns_concurrency", int),
            ("MSORTER_CHECKPOINT_INTERVAL", "checkpoint_interval", int),
            ("MSORTER_MAX_BYTES", "max_bytes", int),
        ):
            if os.getenv(name):
                values[field_name] = converter(os.environ[name])
        values.update(overrides)
        return cls(**values)

    @classmethod
    def for_profile(cls, profile: str = "full", **overrides: Any) -> SortConfig:
        profiles = {
            "full": {"structured_output": True, "quarantine_output": True},
            "validation": {
                "structured_output": True,
                "quarantine_output": True,
                "profile": "validation",
            },
            "dedupe": {
                "structured_output": True,
                "quarantine_output": True,
                "profile": "dedupe",
            },
            "classify": {
                "structured_output": True,
                "quarantine_output": True,
                "profile": "classify",
            },
            "legacy-export": {
                "structured_output": False,
                "quarantine_output": False,
                "profile": "legacy-export",
            },
            "report": {
                "structured_output": True,
                "quarantine_output": True,
                "profile": "report",
            },
        }
        if profile not in profiles:
            raise ValueError(f"unknown processing profile: {profile}")
        values = profiles[profile].copy()
        values["profile"] = profile
        values.update(overrides)
        return cls.from_environment(**values)


@dataclass
class SortResult:
    stats: SortStats
    committed: bool
    summary_path: Path | None = None
    dns_results: list[dict[str, Any]] = field(default_factory=list)
    structured_results_path: Path | None = None
    quarantine_path: Path | None = None


class InputReader:
    """Streaming source reader for the canonical sorting workflow."""

    @staticmethod
    def detect_format(path: str | Path) -> str:
        path = Path(path)
        suffix = path.suffix.lower()
        if suffix == ".csv":
            return "csv"
        if suffix == ".jsonl":
            return "jsonl"
        if suffix == ".json":
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                first = next((char for char in handle.read(512) if not char.isspace()), "")
            return "json_array" if first == "[" else "jsonl" if first == "{" else "txt"
        return "txt"

    @staticmethod
    def iter_emails(
        path: str | Path,
        *,
        max_bytes: int = 1_073_741_824,
        with_positions: bool = False,
    ) -> Iterator[str | tuple[int, str]]:
        for position, raw in InputReader.iter_records(path, max_bytes=max_bytes):
            for candidate in extract_addresses(raw):
                yield (position, candidate) if with_positions else candidate

    @staticmethod
    def iter_records(
        path: str | Path, *, max_bytes: int = 1_073_741_824
    ) -> Iterator[tuple[int, str]]:
        yield from enumerate(iter_raw_values(Path(path), max_bytes=max_bytes), 1)

    @staticmethod
    def file_size_mb(path: str | Path) -> float:
        try:
            return Path(path).stat().st_size / (1 << 20)
        except OSError:
            return 0.0


class AddressValidator:
    """Application-facing validation helper over the authoritative domain rules."""

    def __init__(self, disposable_file: str | Path | None = None):
        self.disposable_domains = DisposableDomainStore(
            Path(disposable_file) if disposable_file else None
        ).load()

    @staticmethod
    def normalize(email: str) -> str:
        return normalize_email(email)

    @staticmethod
    def detect_subaddress(local: str) -> tuple[bool, str, str]:
        if "+" in local:
            base, tag = local.split("+", 1)
            return True, base, tag
        return False, local, ""

    def validate(self, email: str) -> tuple[bool, str]:
        result = validate_email(email)
        return result.valid, result.reason

    def is_role_based(self, local: str) -> bool:
        return bool(classify_address(f"{local}@example.com").get("is_role_based"))

    def is_disposable(self, domain: str) -> bool:
        return domain.lower() in self.disposable_domains


class Checkpoint:
    """Resume boundary backed by the atomic package persistence primitive."""

    def __init__(self, checkpoint_path: str | Path = "checkpoint.json"):
        self.checkpoint_path = Path(checkpoint_path)
        self.dns_cache_path = self.checkpoint_path.with_name(
            self.checkpoint_path.name.replace(".json", "_dns_cache.json")
        )
        self.current_state = self._load()

    def _load(self) -> dict[str, Any]:
        return read_checkpoint(self.checkpoint_path) or {
            "file": None,
            "position": 0,
            "processed_count": 0,
            "completed": False,
            "timestamp": time.time(),
            "version": "6.0.0",
        }

    def save(
        self,
        file_path: str,
        position: int,
        processed_count: int,
        completed: bool = False,
        dns_engine: DNSService | None = None,
        stats: SortStats | None = None,
        seen_domains: set[str] | None = None,
        pending_domains: set[str] | None = None,
        output_offsets: dict[str, int] | None = None,
        source_signature: str | None = None,
        configuration_signature: str | None = None,
    ) -> bool:
        self.current_state = {
            "file": file_path,
            "position": position,
            "processed_count": processed_count,
            "completed": completed,
            "timestamp": time.time(),
            "version": "6.0.0",
            "circuit_breaker": None,
        }
        if stats is not None:
            self.current_state["stats"] = stats.__dict__.copy()
        if seen_domains is not None:
            self.current_state["seen_domains"] = sorted(seen_domains)
        if pending_domains is not None:
            self.current_state["pending_domains"] = sorted(pending_domains)
        if output_offsets is not None:
            self.current_state["output_offsets"] = output_offsets.copy()
        if source_signature is not None:
            self.current_state["source_signature"] = source_signature
        if configuration_signature is not None:
            self.current_state["configuration_signature"] = configuration_signature
        write_checkpoint(self.checkpoint_path, self.current_state)
        if dns_engine is not None and not completed:
            dns_engine.cache.save(self.dns_cache_path)
        return True

    def can_resume(
        self,
        file_path: str,
        source_signature: str | None = None,
        configuration_signature: str | None = None,
    ) -> tuple[bool, int, int]:
        state = self.current_state
        if (
            state.get("file") == file_path
            and not state.get("completed", False)
            and state.get("version") == "6.0.0"
            and time.time() - state.get("timestamp", 0) < 86_400
            and (source_signature is None or state.get("source_signature") == source_signature)
            and (
                configuration_signature is None
                or state.get("configuration_signature") == configuration_signature
            )
        ):
            return True, int(state.get("position", 0)), int(state.get("processed_count", 0))
        return False, 0, 0

    def load_dns_cache(self, dns_engine: DNSService) -> bool:
        return dns_engine.cache.load(self.dns_cache_path)

    def clear(self) -> None:
        for path in (self.checkpoint_path, self.dns_cache_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        self.current_state = self._load()


def _write_summary(stats: SortStats, out_dir: Path, dns_results: list[dict[str, Any]]) -> Path:
    total = stats.total_extracted or 1
    payload = {
        "version": "6.0.0",
        "result_schema_version": "1",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "counts": {
            "total_extracted": stats.total_extracted,
            "unique_valid": stats.unique_valid,
            "duplicates_removed": stats.duplicates,
            "invalid": stats.invalid,
            "quarantined": stats.quarantined,
            "role_based": stats.role_based,
            "disposable": stats.disposable,
            "subaddressed": stats.subaddressed,
            "unique_domains": stats.unique_domains,
        },
        "rates": {
            "valid_pct": round(stats.unique_valid / total * 100, 1),
            "duplicate_pct": round(stats.duplicates / total * 100, 1),
            "invalid_pct": round(stats.invalid / total * 100, 1),
        },
        "providers": dict(sorted(stats.provider_counts.items(), key=lambda item: -item[1])),
        "hosting": dict(sorted(stats.hosting_counts.items(), key=lambda item: -item[1])),
        "output_files": dict(sorted(stats.file_counts.items(), key=lambda item: -item[1])),
        "top_100_domains": dict(
            sorted(stats.domain_counts.items(), key=lambda item: -item[1])[:100]
        ),
        "tld_distribution": dict(sorted(stats.tld_counts.items(), key=lambda item: -item[1])),
        "dns_errors": stats.dns_errors,
        "invalid_reasons": stats.invalid_reasons,
        "reason_counts": stats.reason_counts,
        "source_counts": stats.source_counts,
        "dns_results": dns_results,
        "performance": {
            "seconds": round(stats.elapsed, 3),
            "emails_per_second": round(stats.total_extracted / max(stats.elapsed, 0.001), 1),
            "dns_lookups": stats.dns_lookups,
            "dns_resolved_ok": stats.dns_resolved_ok,
            "peak_memory_mb": stats.peak_memory_mb,
            "dns_circuit_breaks": stats.dns_circuit_breaks,
        },
        "outputs": {
            "structured_results": "processing.jsonl",
            "quarantine": "quarantine.jsonl",
            "structured_results_written": (out_dir / "processing.jsonl").exists(),
            "quarantine_written": (out_dir / "quarantine.jsonl").exists(),
            "encoding": "utf-8",
            "line_endings": "lf",
        },
    }
    path = out_dir / "summary.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    if dns_results:
        (out_dir / "dns.json").write_text(
            json.dumps(dns_results, indent=2) + "\n", encoding="utf-8"
        )
    return path


def _csv_safe(value: Any, *, enabled: bool = True) -> Any:
    """Prevent spreadsheet formula interpretation in exported CSV fields."""

    if enabled and isinstance(value, str) and value[:1] in {"=", "+", "-", "@"}:
        return "'" + value
    return value


def _source_signature(sources: Sequence[Path]) -> str:
    digest = hashlib.sha256()
    for source in sources:
        digest.update(str(source).encode("utf-8"))
        with source.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _configuration_signature(config: SortConfig, *, dry_run: bool) -> str:
    """Fingerprint rules and output mode so incompatible resumes are rejected."""

    digest = hashlib.sha256()
    payload = {
        "rule_version": "6.0.0",
        "profile": config.profile,
        "structured_output": config.structured_output,
        "quarantine_output": config.quarantine_output,
        "formula_safe_csv": config.formula_safe_csv,
        "dry_run": dry_run,
        "dns_mode": config.dns_mode.value,
        "providers_path": str(config.providers_path),
        "disposable_path": str(config.disposable_path),
    }
    digest.update(json.dumps(payload, sort_keys=True).encode("utf-8"))
    for config_path in (config.providers_path, config.disposable_path):
        digest.update(str(config_path).encode("utf-8"))
        try:
            with config_path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except FileNotFoundError:
            digest.update(b"<missing>")
    return digest.hexdigest()


class EmailSorter:
    """Streaming, resumable, local-first sorting campaign."""

    def __init__(self, config: SortConfig | None = None, *, dns: DNSService | None = None):
        self.config = config or SortConfig.from_environment()
        self.providers = ProviderCatalog(self.config.providers_path)
        self.validator = AddressValidator(self.config.disposable_path)
        self.dns = dns or DNSService(
            mode=self.config.dns_mode,
            cache=DNSCache(self.config.dns_cache_max_size),
            provider_catalog=self.providers,
            timeout=self.config.dns_timeout,
            retry_count=self.config.dns_retry_count,
            concurrency=self.config.dns_concurrency,
        )
        self.checkpoint = Checkpoint(self.config.checkpoint_path)
        self.stats = SortStats()
        self._interrupted = False
        self._old_sigint: Any = None

    def _signal_handler(self, _signal: int, _frame: Any) -> None:
        self._interrupted = True

    def _install_signal_handler(self) -> None:
        try:
            self._old_sigint = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, self._signal_handler)
        except ValueError, OSError:
            self._old_sigint = None

    def _restore_signal_handler(self) -> None:
        if self._old_sigint is not None:
            signal.signal(signal.SIGINT, self._old_sigint)

    def _flush_outputs(self, handles: dict[str, TextIO], master: TextIO | None = None) -> None:
        for handle in handles.values():
            handle.flush()
            os.fsync(handle.fileno())
        if master is not None and not master.closed:
            master.flush()
            os.fsync(master.fileno())

    def _capture_output_offsets(self) -> dict[str, int]:
        offsets: dict[str, int] = {}
        if not self.config.results_dir.exists():
            return offsets
        for path in self.config.results_dir.rglob("*"):
            if path.is_file() and path.relative_to(self.config.results_dir).parts[0] != "_temp":
                relative = path.relative_to(self.config.results_dir).as_posix()
                offsets[relative] = path.stat().st_size
        return offsets

    def _restore_output_boundary(self) -> None:
        if "output_offsets" not in self.checkpoint.current_state:
            raise RuntimeError(
                "checkpoint lacks output-boundary metadata; start a fresh sort "
                "after reviewing results"
            )
        raw_offsets = self.checkpoint.current_state["output_offsets"]
        if not isinstance(raw_offsets, dict):
            raise RuntimeError("checkpoint output-boundary metadata is invalid")
        offsets = {str(name): int(value) for name, value in raw_offsets.items()}
        for relative, expected in offsets.items():
            path = self.config.results_dir / relative
            if expected and not path.exists():
                raise RuntimeError(f"checkpoint output is missing: {path}")
            if path.exists() and path.stat().st_size < expected:
                raise RuntimeError(f"checkpoint output is shorter than recorded: {path}")
        for path in self.config.results_dir.rglob("*"):
            if not path.is_file() or path.relative_to(self.config.results_dir).parts[0] == "_temp":
                continue
            relative = path.relative_to(self.config.results_dir).as_posix()
            if relative == "summary.json":
                continue
            with path.open("r+b") as handle:
                handle.truncate(offsets.get(relative, 0))

    def _rebuild_seen_index(self, connection: sqlite3.Connection) -> None:
        connection.execute("DELETE FROM seen")
        for path in self.config.results_dir.rglob("*"):
            relative = path.relative_to(self.config.results_dir)
            if (
                not path.is_file()
                or relative.parts[0] == "_temp"
                or relative.as_posix() in {"master.csv", "summary.json", "quarantine.jsonl"}
            ):
                continue
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if relative.as_posix() == "processing.jsonl":
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if (
                            not isinstance(record, dict)
                            or record.get("deduplication_disposition") != "unique"
                        ):
                            continue
                        candidate = record.get("normalized_address")
                        if not isinstance(candidate, str):
                            continue
                        normalized = normalize_email(candidate)
                    else:
                        normalized = normalize_email(line.strip())
                    if validate_email(normalized).valid:
                        connection.execute(
                            "INSERT OR IGNORE INTO seen(email) VALUES (?)", (normalized,)
                        )

    def sort_files(
        self,
        paths: Sequence[str | Path],
        *,
        resume: bool | None = None,
        input_fn: Any = input,
        dry_run: bool = False,
        progress_fn: Callable[[int, int], None] | None = None,
    ) -> SortResult:
        """Process multiple sources as one deterministic, deduplicated job."""

        return self.sort_file(
            list(paths),
            resume=resume,
            input_fn=input_fn,
            dry_run=dry_run,
            progress_fn=progress_fn,
        )

    def sort_file(
        self,
        path: str | Path | Sequence[str | Path],
        *,
        resume: bool | None = None,
        input_fn: Any = input,
        dry_run: bool = False,
        progress_fn: Callable[[int, int], None] | None = None,
    ) -> SortResult:
        sources = [Path(path)] if isinstance(path, (str, Path)) else [Path(item) for item in path]
        if not sources:
            raise ValueError("at least one input source is required")
        for source_path in sources:
            if not source_path.is_file():
                raise FileNotFoundError(source_path)
            size = source_path.stat().st_size
            if size > self.config.max_bytes:
                raise InputLimitError(
                    f"input is {size} bytes; maximum is {self.config.max_bytes}: {source_path}"
                )
        if min(shutil.disk_usage(source_path.parent).free for source_path in sources) < int(
            self.config.min_disk_space_gb * (1024**3)
        ):
            raise OSError("insufficient disk space for sorting")
        source = sources[0]
        job_identity = (
            str(source)
            if len(sources) == 1
            else json.dumps([str(source_path) for source_path in sources], separators=(",", ":"))
        )
        source_signature = _source_signature(sources)
        configuration_signature = _configuration_signature(self.config, dry_run=dry_run)
        started = time.monotonic()
        self.config.log_dir.mkdir(parents=True, exist_ok=True)
        log_handler = logging.FileHandler(
            self.config.log_dir / f"mailforge_{time.strftime('%Y%m%d')}.log", encoding="utf-8"
        )
        log.addHandler(log_handler)
        format_name = InputReader.detect_format(source) if len(sources) == 1 else "multi-source"
        log.info("sort_starting source_count=%d format=%s", len(sources), format_name)
        can_resume, resume_position, resume_count = self.checkpoint.can_resume(
            job_identity, source_signature, configuration_signature
        )
        requested_resume = resume is True
        if requested_resume and not can_resume:
            log.removeHandler(log_handler)
            log_handler.close()
            raise RuntimeError(
                "resume metadata does not match the current input or lacks "
                "required output-boundary metadata; use --fresh after reviewing results"
            )
        if resume is None and can_resume:
            answer = input_fn("Resume from checkpoint? [Y/n]: ").strip().lower()
            resume = answer in {"", "y", "yes"}
        resume = bool(resume and can_resume)
        if resume and isinstance(self.checkpoint.current_state.get("stats"), dict):
            saved_stats = self.checkpoint.current_state["stats"]
            self.stats = SortStats(**saved_stats)
        if not resume:
            self._prepare_results()
            self.checkpoint.clear()
            resume_position = 0
            resume_count = 0
        else:
            self.config.results_dir.mkdir(parents=True, exist_ok=True)
            self.checkpoint.load_dns_cache(self.dns)
            self._restore_output_boundary()

        self._install_signal_handler()
        committed = False
        dns_results: list[dict[str, Any]] = []
        position = resume_position
        committed_position = resume_position
        committed_count = resume_count
        skip_remaining = resume_position if resume else 0
        count = resume_count
        seen_domains: set[str] = set(
            self.checkpoint.current_state.get("seen_domains", []) if resume else []
        )
        pending_domains: set[str] = set(
            self.checkpoint.current_state.get("pending_domains", []) if resume else []
        )
        committed_stats = copy.deepcopy(self.stats)
        committed_seen_domains = set(seen_domains)
        committed_pending_domains = set(pending_domains)
        raw_committed_offsets = self.checkpoint.current_state.get("output_offsets", {})
        committed_offsets = (
            {str(name): int(value) for name, value in raw_committed_offsets.items()}
            if resume and isinstance(raw_committed_offsets, dict)
            else {}
        )
        db_path = self.config.results_dir / "_temp" / "seen.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(db_path, isolation_level=None)
        connection.execute("CREATE TABLE IF NOT EXISTS seen (email TEXT PRIMARY KEY)")
        if resume:
            self._rebuild_seen_index(connection)
        handles: dict[str, TextIO] = {}
        master: TextIO | None = None
        structured_path = (
            self.config.results_dir / "processing.jsonl" if self.config.structured_output else None
        )
        quarantine_path = (
            self.config.results_dir / "quarantine.jsonl" if self.config.quarantine_output else None
        )
        structured_handle: TextIO | None = None
        quarantine_handle: TextIO | None = None
        write_legacy = not dry_run and self.config.profile in {"full", "classify", "legacy-export"}
        if not resume:
            self.checkpoint.save(
                job_identity,
                0,
                0,
                False,
                self.dns,
                committed_stats,
                set(),
                set(),
                {},
                source_signature,
                configuration_signature,
            )
        try:
            writer = None
            if write_legacy:
                master_path = self.config.results_dir / "master.csv"
                master = master_path.open("a", encoding="utf-8", newline="")
                writer = csv.writer(master)
                if master_path.stat().st_size == 0:
                    writer.writerow(
                        [
                            "email",
                            "domain",
                            "registered_domain",
                            "provider",
                            "hosting",
                            "output_file",
                            "valid",
                            "is_role",
                            "is_disposable",
                            "has_subaddress",
                            "processing_disposition",
                            "reason_codes",
                            "source_file",
                            "source_record_position",
                        ]
                    )
            if structured_path is not None:
                structured_handle = structured_path.open("a", encoding="utf-8")
            if quarantine_path is not None:
                quarantine_handle = quarantine_path.open("a", encoding="utf-8")

            def flush_all() -> None:
                auxiliary = dict(handles)
                if structured_handle is not None:
                    auxiliary["__processing.jsonl"] = structured_handle
                if quarantine_handle is not None:
                    auxiliary["__quarantine.jsonl"] = quarantine_handle
                self._flush_outputs(auxiliary, master)

            def emit_result(result: ProcessingResult) -> None:
                payload = json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True)
                if structured_handle is not None:
                    structured_handle.write(payload + "\n")
                if (
                    quarantine_handle is not None
                    and result.processing_disposition is ProcessingDisposition.REJECTED
                ):
                    quarantine_handle.write(payload + "\n")

            def iter_candidates() -> Iterator[tuple[Path, int, str]]:
                if len(sources) == 1:
                    for source_record_position, candidate in InputReader.iter_emails(
                        source, max_bytes=self.config.max_bytes, with_positions=True
                    ):
                        yield source, source_record_position, candidate
                    return
                for source_path in sources:
                    for source_record_position, raw_value in InputReader.iter_records(
                        source_path, max_bytes=self.config.max_bytes
                    ):
                        for candidate in extract_addresses(raw_value):
                            yield source_path, source_record_position, candidate

            for source_path, source_record_position, raw in iter_candidates():
                if skip_remaining > 0:
                    skip_remaining -= 1
                    continue
                if self._interrupted:
                    break
                position += 1
                self.stats.total_extracted += 1
                source_key = str(source_path)
                self.stats.source_counts[source_key] = (
                    self.stats.source_counts.get(source_key, 0) + 1
                )
                normalized = normalize_email(raw)
                validation = validate_email(normalized)
                if not validation.valid:
                    self.stats.invalid += 1
                    self.stats.quarantined += 1
                    self.stats.invalid_reasons[validation.reason] = (
                        self.stats.invalid_reasons.get(validation.reason, 0) + 1
                    )
                    self.stats.reason_counts[ReasonCode.INVALID_SYNTAX.value] = (
                        self.stats.reason_counts.get(ReasonCode.INVALID_SYNTAX.value, 0) + 1
                    )
                    emit_result(
                        ProcessingResult(
                            original_input=raw,
                            normalized_address=normalized,
                            source_file=str(source_path),
                            source_record_position=source_record_position,
                            validation_result={"valid": False, "reason": validation.reason},
                            classification_result={},
                            deduplication_disposition="not_evaluated",
                            diagnostic_reason_codes=(ReasonCode.INVALID_SYNTAX.value,),
                            processing_disposition=ProcessingDisposition.REJECTED,
                        )
                    )
                    continue
                existing = connection.execute(
                    "SELECT 1 FROM seen WHERE email = ?", (normalized,)
                ).fetchone()
                if existing is not None:
                    self.stats.duplicates += 1
                    self.stats.reason_counts[ReasonCode.DUPLICATE.value] = (
                        self.stats.reason_counts.get(ReasonCode.DUPLICATE.value, 0) + 1
                    )
                    emit_result(
                        ProcessingResult(
                            original_input=raw,
                            normalized_address=normalized,
                            source_file=str(source_path),
                            source_record_position=source_record_position,
                            validation_result={"valid": True, "reason": "ok"},
                            classification_result={},
                            deduplication_disposition="duplicate",
                            diagnostic_reason_codes=(ReasonCode.DUPLICATE.value,),
                            processing_disposition=ProcessingDisposition.DUPLICATE,
                        )
                    )
                    continue
                self.stats.unique_valid += 1
                count += 1
                local, domain = normalized.rsplit("@", 1)
                registered = self.providers.registered_domain(domain)
                classification = (
                    classify_address(normalized, self.validator.disposable_domains)
                    if self.config.profile not in {"validation", "dedupe"}
                    else {
                        "is_role_based": False,
                        "is_disposable": False,
                        "has_subaddress": "+" in local,
                    }
                )
                is_role = bool(classification["is_role_based"])
                is_disposable = bool(classification["is_disposable"])
                has_subaddress = bool(classification["has_subaddress"])
                self.stats.role_based += is_role
                self.stats.disposable += is_disposable
                self.stats.subaddressed += has_subaddress
                self.stats.domain_counts[registered] = (
                    self.stats.domain_counts.get(registered, 0) + 1
                )
                self.stats.tld_counts[registered.rsplit(".", 1)[-1]] = (
                    self.stats.tld_counts.get(registered.rsplit(".", 1)[-1], 0) + 1
                )
                seen_domains.add(domain)
                output_file, provider = self.providers.route(normalized)
                reason_codes: list[str] = []
                if is_disposable:
                    reason_codes.append(ReasonCode.DISPOSABLE_DOMAIN.value)
                if is_role:
                    reason_codes.append(ReasonCode.ROLE_ACCOUNT.value)
                if provider != "custom":
                    reason_codes.append(ReasonCode.KNOWN_PROVIDER.value)
                else:
                    reason_codes.append(ReasonCode.CUSTOM_DOMAIN.value)
                if not reason_codes:
                    reason_codes.append(ReasonCode.ACCEPTED.value)
                if output_file == "custom_domains.txt":
                    pending_domains.add(domain)
                dns_evidence_status = "not_needed"
                if output_file == "custom_domains.txt":
                    if self.dns.mode is DNSMode.OFFLINE:
                        reason_codes.append(ReasonCode.DNS_UNAVAILABLE.value)
                        dns_evidence_status = "unavailable"
                    else:
                        reason_codes.append(ReasonCode.DNS_EVIDENCE_PENDING.value)
                        dns_evidence_status = "pending"
                provider_evidence = self.providers.classify(
                    normalized,
                    dns_status=dns_evidence_status,
                )
                for reason_code in reason_codes:
                    self.stats.reason_counts[reason_code] = (
                        self.stats.reason_counts.get(reason_code, 0) + 1
                    )
                self.stats.provider_counts[provider] = (
                    self.stats.provider_counts.get(provider, 0) + 1
                )
                self.stats.file_counts[output_file] = self.stats.file_counts.get(output_file, 0) + 1
                if write_legacy and output_file not in handles:
                    if len(handles) >= self.config.max_open_files:
                        old_name, old_handle = handles.popitem()
                        old_handle.flush()
                        os.fsync(old_handle.fileno())
                        old_handle.close()
                    output_path = self.config.results_dir / output_file
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    handles[output_file] = output_path.open("a", encoding="utf-8")
                if write_legacy:
                    handles[output_file].write(normalized + "\n")
                    writer.writerow(
                        [
                            _csv_safe(normalized, enabled=self.config.formula_safe_csv),
                            _csv_safe(domain, enabled=self.config.formula_safe_csv),
                            _csv_safe(registered, enabled=self.config.formula_safe_csv),
                            _csv_safe(provider, enabled=self.config.formula_safe_csv),
                            "",
                            _csv_safe(output_file, enabled=self.config.formula_safe_csv),
                            "valid",
                            is_role,
                            is_disposable,
                            has_subaddress,
                            ProcessingDisposition.ACCEPTED.value,
                            ";".join(reason_codes),
                            _csv_safe(str(source_path), enabled=self.config.formula_safe_csv),
                            source_record_position,
                        ]
                    )
                connection.execute("INSERT OR IGNORE INTO seen(email) VALUES (?)", (normalized,))
                emit_result(
                    ProcessingResult(
                        original_input=raw,
                        normalized_address=normalized,
                        source_file=str(source_path),
                        source_record_position=source_record_position,
                        validation_result={"valid": True, "reason": "ok"},
                        classification_result={
                            "domain": domain,
                            "registered_domain": registered,
                            "provider": provider,
                            "output_file": output_file,
                            "is_role_based": is_role,
                            "is_disposable": is_disposable,
                            "has_subaddress": has_subaddress,
                            "provider_evidence": provider_evidence,
                        },
                        deduplication_disposition="unique",
                        diagnostic_reason_codes=tuple(reason_codes),
                        dns_evidence_status=dns_evidence_status,
                        processing_disposition=ProcessingDisposition.ACCEPTED,
                        metadata={"profile": self.config.profile, "dry_run": dry_run},
                    )
                )
                if self.config.progress_every and position % self.config.progress_every == 0:
                    if progress_fn is not None:
                        progress_fn(position, count)
                    else:
                        log.info("sort_progress processed=%d accepted=%d", position, count)
                if count % self.config.checkpoint_interval == 0:
                    flush_all()
                    self.stats.peak_memory_mb = max(self.stats.peak_memory_mb, _memory_mb())
                    output_offsets = self._capture_output_offsets()
                    self.checkpoint.save(
                        job_identity,
                        position,
                        count,
                        False,
                        self.dns,
                        self.stats,
                        seen_domains,
                        pending_domains,
                        output_offsets,
                        source_signature,
                        configuration_signature,
                    )
                    committed_position = position
                    committed_count = count
                    committed_stats = copy.deepcopy(self.stats)
                    committed_seen_domains = set(seen_domains)
                    committed_pending_domains = set(pending_domains)
                    committed_offsets = output_offsets
            if self._interrupted:
                flush_all()
                if master is not None:
                    master.close()
                if structured_handle is not None:
                    structured_handle.close()
                if quarantine_handle is not None:
                    quarantine_handle.close()
                self._save_incomplete_checkpoint(
                    job_identity,
                    committed_position,
                    committed_count,
                    committed_stats,
                    committed_seen_domains,
                    committed_pending_domains,
                    committed_offsets,
                    source_signature,
                    configuration_signature,
                )
                return SortResult(
                    self.stats,
                    False,
                    None,
                    structured_results_path=structured_path,
                    quarantine_path=quarantine_path,
                )
            flush_all()
            for handle in handles.values():
                handle.close()
            if master is not None:
                master.close()
            if structured_handle is not None:
                structured_handle.close()
            if quarantine_handle is not None:
                quarantine_handle.close()
            self.stats.unique_domains = len(seen_domains)
            if pending_domains and self.dns.mode is not DNSMode.OFFLINE:
                if not self.dns.preflight_check():
                    responses = []
                else:
                    if self.dns.mode is DNSMode.ASYNC:
                        responses = asyncio.run(
                            self.dns.resolve_batch_async(sorted(pending_domains))
                        )
                    else:
                        responses = self.dns.resolve_batch_sync(sorted(pending_domains))
                for response in responses:
                    dns_results.append(
                        {
                            "domain": response.domain,
                            "result": response.result.value,
                            "provider_id": response.provider_id,
                            "mx_records": [record.__dict__ for record in response.mx_records],
                            "error_msg": response.error_msg,
                            "latency_ms": response.latency_ms,
                        }
                    )
                    if response.provider_id:
                        self.stats.hosting_counts[response.provider_id] = (
                            self.stats.hosting_counts.get(response.provider_id, 0) + 1
                        )
                    if response.result is not DNSResult.OK:
                        self.stats.dns_errors[response.result.value] = (
                            self.stats.dns_errors.get(response.result.value, 0) + 1
                        )
            self.stats.dns_lookups = self.dns.total_lookups
            self.stats.dns_resolved_ok = self.dns.resolved_ok
            self.stats.dns_circuit_breaks = self.dns.error_counts.get(
                DNSResult.CIRCUIT_BROKEN.value, 0
            )
            self.stats.peak_memory_mb = max(self.stats.peak_memory_mb, _memory_mb())
            self.stats.elapsed = time.monotonic() - started
            summary_path = _write_summary(self.stats, self.config.results_dir, dns_results)
            output_offsets = self._capture_output_offsets()
            self.checkpoint.save(
                job_identity,
                position,
                count,
                True,
                self.dns,
                self.stats,
                seen_domains,
                pending_domains,
                output_offsets,
                source_signature,
                configuration_signature,
            )
            committed = True
            shutil.rmtree(self.config.results_dir / "_temp", ignore_errors=True)
            return SortResult(
                self.stats,
                committed,
                summary_path,
                dns_results,
                structured_path,
                quarantine_path,
            )
        except BaseException:
            for handle in handles.values():
                handle.close()
            if master is not None and not master.closed:
                master.close()
            if structured_handle is not None and not structured_handle.closed:
                structured_handle.close()
            if quarantine_handle is not None and not quarantine_handle.closed:
                quarantine_handle.close()
            self._save_incomplete_checkpoint(
                job_identity,
                committed_position,
                committed_count,
                committed_stats,
                committed_seen_domains,
                committed_pending_domains,
                committed_offsets,
                source_signature,
                configuration_signature,
            )
            raise
        finally:
            if master is not None and not master.closed:
                master.close()
            connection.close()
            self._restore_signal_handler()
            log.removeHandler(log_handler)
            log_handler.close()
            if not committed:
                self.stats.elapsed = time.monotonic() - started

    def _prepare_results(self) -> None:
        self.config.results_dir.parent.mkdir(parents=True, exist_ok=True)
        if self.config.results_dir.exists() and any(self.config.results_dir.iterdir()):
            raise RuntimeError(f"'{self.config.results_dir}/' already has files")
        self.config.results_dir.mkdir(parents=True, exist_ok=True)
        (self.config.results_dir / "_temp").mkdir()

    def _save_incomplete_checkpoint(
        self,
        source: str,
        position: int,
        count: int,
        stats: SortStats,
        seen_domains: set[str],
        pending_domains: set[str],
        output_offsets: dict[str, int],
        source_signature: str,
        configuration_signature: str,
    ) -> None:
        self.checkpoint.save(
            source,
            position,
            count,
            False,
            self.dns,
            stats,
            seen_domains,
            pending_domains,
            output_offsets,
            source_signature,
            configuration_signature,
        )

    def delete_results(self, *, confirm: bool = False, input_fn: Any = input) -> bool:
        if not self.config.results_dir.is_dir():
            return False
        if not confirm:
            confirm = (
                input_fn(f"Delete '{self.config.results_dir}/'? (y/N): ").strip().lower() == "y"
            )
        if confirm:
            shutil.rmtree(self.config.results_dir)
            return True
        return False

    def view_summary(self) -> dict[str, Any] | None:
        path = self.config.results_dir / "summary.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))


def print_report(stats: SortStats) -> None:
    print(json.dumps({"counts": stats.__dict__}, indent=2, default=str))


def _memory_mb() -> int:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(usage / (1024 * 1024)) if usage > 10_000_000 else int(usage / 1024)
