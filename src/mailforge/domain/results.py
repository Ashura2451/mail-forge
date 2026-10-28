"""Structured, explainable outcomes for local email processing."""

from __future__ import annotations

import enum
from dataclasses import asdict, dataclass, field
from typing import Any


class ProcessingDisposition(enum.StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"
    QUARANTINED = "quarantined"


class ReasonCode(enum.StrEnum):
    ACCEPTED = "accepted"
    INVALID_SYNTAX = "invalid_syntax"
    DUPLICATE = "duplicate"
    DISPOSABLE_DOMAIN = "disposable_domain"
    ROLE_ACCOUNT = "role_account"
    KNOWN_PROVIDER = "known_provider"
    CUSTOM_DOMAIN = "custom_domain"
    DNS_EVIDENCE_PENDING = "dns_evidence_pending"
    MALFORMED_RECORD = "malformed_record"
    DNS_UNAVAILABLE = "dns_unavailable"
    DNS_LOOKUP_FAILED = "dns_lookup_failed"
    INPUT_ERROR = "input_error"


@dataclass(frozen=True)
class ProcessingResult:
    """One candidate's explainable processing outcome.

    ``original_input`` is intentionally the extracted candidate rather than an
    entire source record, limiting incidental personal-data retention in the
    structured export and keeping ordinary logs free of address content.
    """

    original_input: str
    normalized_address: str | None
    source_file: str
    source_record_position: int
    validation_result: dict[str, Any]
    classification_result: dict[str, Any]
    deduplication_disposition: str
    diagnostic_reason_codes: tuple[str, ...] = ()
    dns_evidence_status: str = "not_requested"
    processing_disposition: ProcessingDisposition = ProcessingDisposition.ACCEPTED
    rule_version: str = "6.0.0"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["processing_disposition"] = self.processing_disposition.value
        value["diagnostic_reason_codes"] = list(self.diagnostic_reason_codes)
        return value
