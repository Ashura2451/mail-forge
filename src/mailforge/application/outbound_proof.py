"""Canonical, versioned evidence for a prepared outbound message."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

PROOF_VERSION = 1


def canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_json(value: Mapping[str, Any]) -> str:
    return digest_bytes(canonical_json(value).encode("utf-8"))


@dataclass(frozen=True)
class OutboundMessageProof:
    """The durable identity and evidence binding for one prepared MIME artifact."""

    proof_id: str
    proof_hash: str
    content_hash: str
    payload: dict[str, Any]

    @property
    def proof_version(self) -> int:
        return int(self.payload["proof_version"])

    @property
    def sender(self) -> str:
        return str(self.payload["sender"])

    @property
    def recipient(self) -> str:
        return str(self.payload["recipient"])

    @property
    def message_revision_id(self) -> str | None:
        value = self.payload.get("message_revision_id")
        return str(value) if value else None


def build_payload(
    *,
    operation_kind: str,
    sender: str,
    recipient: str,
    content_hash: str,
    prepared_at: str,
    artifact_kind: str,
    artifact_ref: str,
    message_revision_id: str | None = None,
    source_identity: str | None = None,
    campaign_id: str | None = None,
    intent_id: str | None = None,
    eligibility: Mapping[str, Any] | None = None,
    personalization_hash: str | None = None,
    policy_revision: int | None = None,
    authorization_ref: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    return {
        "proof_version": PROOF_VERSION,
        "operation_kind": operation_kind,
        "message_revision_id": message_revision_id,
        "source_identity": source_identity,
        "campaign_id": campaign_id,
        "intent_id": intent_id,
        "sender": sender,
        "recipient": recipient,
        "content_hash": content_hash,
        "prepared_at": prepared_at,
        "artifact_kind": artifact_kind,
        "artifact_ref": artifact_ref,
        "eligibility": dict(eligibility or {}),
        "personalization_hash": personalization_hash,
        "policy_revision": policy_revision,
        "authorization_ref": authorization_ref,
        "idempotency_key": idempotency_key,
    }


def make_proof(proof_id: str, payload: Mapping[str, Any]) -> OutboundMessageProof:
    normalized = json.loads(canonical_json(dict(payload)))
    if normalized.get("proof_version") != PROOF_VERSION:
        raise ValueError("unsupported outbound proof version")
    proof_hash = digest_json(normalized)
    return OutboundMessageProof(
        proof_id=proof_id,
        proof_hash=proof_hash,
        content_hash=str(normalized["content_hash"]),
        payload=normalized,
    )


def verify_proof(proof: OutboundMessageProof, mime_bytes: bytes) -> None:
    if proof.payload.get("content_hash") != proof.content_hash:
        raise ValueError("outbound proof row does not match its metadata content digest")
    if (
        proof.payload.get("sender") != proof.sender
        or proof.payload.get("recipient") != proof.recipient
    ):
        raise ValueError("outbound proof row does not match its metadata addresses")
    if proof.content_hash != digest_bytes(mime_bytes):
        raise ValueError("outbound proof content digest does not match the MIME artifact")
    if proof.proof_hash != digest_json(proof.payload):
        raise ValueError("outbound proof metadata digest is invalid")
