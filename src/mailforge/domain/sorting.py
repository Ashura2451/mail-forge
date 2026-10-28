"""Domain models shared by the unified sorting application."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class DNSMode(enum.StrEnum):
    ASYNC = "async"
    SYNC = "sync"
    OFFLINE = "offline"


class DNSResult(enum.StrEnum):
    OK = "ok"
    NXDOMAIN = "nxdomain"
    NO_MX = "no_mx"
    TIMEOUT = "timeout"
    CONNECTION_ERROR = "connection_error"
    REFUSED = "refused"
    SERVER_FAILURE = "server_failure"
    NOT_IMPLEMENTED = "not_implemented"
    FORMAT_ERROR = "format_error"
    GENERIC_ERROR = "dns_error"
    CIRCUIT_BROKEN = "circuit_broken"
    OFFLINE = "offline"


@dataclass(frozen=True)
class MXRecord:
    priority: int
    host: str
    provider_id: str | None = None


@dataclass(frozen=True)
class DNSResponse:
    domain: str
    result: DNSResult
    mx_records: tuple[MXRecord, ...] = ()
    provider_id: str | None = None
    error_msg: str | None = None
    ttl: int | None = None
    latency_ms: int | None = None


@dataclass(frozen=True)
class ProviderDefinition:
    name: str
    output_file: str
    aliases: tuple[str, ...] = ()
    mx_patterns: tuple[str, ...] = ()


@dataclass
class SortStats:
    total_extracted: int = 0
    unique_valid: int = 0
    duplicates: int = 0
    invalid: int = 0
    quarantined: int = 0
    role_based: int = 0
    disposable: int = 0
    subaddressed: int = 0
    unique_domains: int = 0
    dns_lookups: int = 0
    dns_resolved_ok: int = 0
    elapsed: float = 0.0
    dns_circuit_breaks: int = 0
    provider_counts: dict[str, int] = field(default_factory=dict)
    hosting_counts: dict[str, int] = field(default_factory=dict)
    domain_counts: dict[str, int] = field(default_factory=dict)
    tld_counts: dict[str, int] = field(default_factory=dict)
    file_counts: dict[str, int] = field(default_factory=dict)
    dns_errors: dict[str, int] = field(default_factory=dict)
    invalid_reasons: dict[str, int] = field(default_factory=dict)
    reason_counts: dict[str, int] = field(default_factory=dict)
    source_counts: dict[str, int] = field(default_factory=dict)
    peak_memory_mb: int = 0
