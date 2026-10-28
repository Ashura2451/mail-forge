"""Bounded, evidence-oriented email standards diagnostics.

This service is deliberately an inspection authority. It does not send mail,
sign messages, or claim provider acceptance. Network DNS inspection is opt-in
and injectable so tests can remain deterministic and offline.
"""

from __future__ import annotations

# Diagnostic records and XML/SQL fragments remain readable beside their rules.
# ruff: noqa: E501
import base64
import bz2
import gzip
import hashlib
import io
import ipaddress
import json
import re
import secrets
import uuid
import zipfile
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree

from mailforge.domain.campaign import SuppressionCategory
from mailforge.domain.email import normalize_email, validate_email
from mailforge.infrastructure.providers import ProviderCatalog

try:
    import dns.resolver
except ImportError:  # pragma: no cover - dependency is pinned in the package
    dns = None


RULESET_VERSION = "027.1"
MAX_DOMAIN = 253
MAX_RECORDS = 50
MAX_DNS_LOOKUPS = 10
MAX_MESSAGE_BYTES = 25 * 1024 * 1024
MAX_REPORT_BYTES = 5 * 1024 * 1024
MAX_REPORT_EXPANDED_BYTES = 10 * 1024 * 1024
MAX_REPORT_ROWS = 10_000
MARKETING_PURPOSES = {"marketing", "newsletter", "product_announcement", "subscribed"}
HEADER_NAMES = {
    "from",
    "to",
    "cc",
    "reply-to",
    "date",
    "message-id",
    "subject",
    "list-unsubscribe",
    "list-unsubscribe-post",
    "dkim-signature",
    "authentication-results",
    "received-spf",
}
_TAG = re.compile(r"(?P<key>[a-z][a-z0-9_-]*)\s*=\s*(?P<value>[^;\s]+)", re.I)
_CID = re.compile(r"cid:([^\s\"'<>]+)", re.I)
_URL = re.compile(r"(?:href|src)\s*=\s*[\"']([^\"']+)", re.I)
_IMG = re.compile(r"<img\b([^>]*)>", re.I | re.S)
_DKIM_TAG = re.compile(r"(?P<key>[a-z])\s*=\s*(?P<value>[^;]+)", re.I)


class EmailStandardsError(ValueError):
    """A bounded diagnostic or unsubscribe request error."""

    def __init__(self, message: str, code: str = "email_standards_invalid") -> None:
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _severity(value: str, code: str, message: str, source: str, action: str = "") -> dict[str, str]:
    return {
        "severity": value,
        "code": code,
        "message": message,
        "source": source,
        "action": action,
        "rule_version": RULESET_VERSION,
    }


def _tags(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for match in _TAG.finditer(value):
        result.setdefault(match.group("key").lower(), match.group("value").strip())
    return result


def _domain(value: str) -> str:
    value = value.strip().lower().rstrip(".")
    if (
        not value
        or len(value) > MAX_DOMAIN
        or ".." in value
        or any(not (char.isalnum() or char in ".-") for char in value)
    ):
        raise EmailStandardsError("domain is invalid or too long", "domain_invalid")
    return value


def _record_value(answer: object, record_type: str) -> str:
    if record_type == "TXT":
        strings = getattr(answer, "strings", None)
        if strings:
            return "".join(
                item.decode("utf-8", "replace") if isinstance(item, bytes) else str(item)
                for item in strings
            )
    if record_type == "MX":
        return str(getattr(answer, "exchange", answer)).rstrip(".")
    return str(answer).strip('"').rstrip(".")


class EmailStandardsService:
    """Canonical standards inspection, safe unsubscribe, and DMARC authority."""

    def __init__(
        self,
        store,
        *,
        resolver=None,
        now_fn=None,
        max_dns_lookups: int = MAX_DNS_LOOKUPS,
        endpoint_probe: Callable[[str], bool] | None = None,
    ):
        self.store = store
        self.now_fn = now_fn or (lambda: datetime.now(UTC))
        self.resolver = resolver
        self.max_dns_lookups = max(1, min(max_dns_lookups, MAX_DNS_LOOKUPS))
        self.catalog = ProviderCatalog()
        self.endpoint_probe = endpoint_probe

    # ------------------------------------------------------------------ durable runs
    def _run(
        self,
        kind: str,
        subject_ref: str | None,
        result: Mapping[str, object],
    ) -> dict[str, object]:
        findings = list(result.get("findings", []))
        evidence = {key: value for key, value in result.items() if key != "findings"}
        run_id = str(uuid.uuid4())
        observed = self.now_fn().isoformat()
        status = (
            "blocked"
            if any(item.get("severity") == "blocked" for item in findings)
            else (
                "warning" if any(item.get("severity") == "warning" for item in findings) else "pass"
            )
        )
        if status == "pass" and any(item.get("severity") == "unknown" for item in findings):
            status = "unknown"
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO email_standards_diagnostic_runs "
                "(id,kind,subject_ref,status,ruleset_version,findings_json,evidence_json,observed_at,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    kind,
                    subject_ref,
                    status,
                    RULESET_VERSION,
                    json.dumps(findings, sort_keys=True),
                    json.dumps(evidence, sort_keys=True, default=str),
                    observed,
                    observed,
                ),
            )
        return {"id": run_id, "kind": kind, "status": status, "observed_at": observed, **result}

    def history(self, *, kind: str | None = None, limit: int = 100) -> list[dict[str, object]]:
        limit = max(1, min(int(limit), 200))
        query = "SELECT * FROM email_standards_diagnostic_runs"
        params: list[object] = []
        if kind:
            query += " WHERE kind = ?"
            params.append(kind)
        query += " ORDER BY observed_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self.store.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [
            {
                **dict(row),
                "findings": json.loads(row["findings_json"]),
                "evidence": json.loads(row["evidence_json"]),
            }
            for row in rows
        ]

    # ------------------------------------------------------------------ identities
    def register_sender_identity(
        self,
        identity_ref: str,
        *,
        visible_from: str,
        reply_to: str | None = None,
        envelope_from: str | None = None,
        dkim_domain: str | None = None,
        dkim_selector: str | None = None,
        provider_account_ref: str | None = None,
        provider_signing_status: str = "unknown",
    ) -> dict[str, object]:
        if (
            not identity_ref
            or len(identity_ref) > 120
            or any(char in identity_ref for char in "\r\n")
        ):
            raise EmailStandardsError("identity reference is invalid", "identity_invalid")
        self._address(visible_from, "visible From")
        if reply_to:
            self._address(reply_to, "Reply-To")
        if envelope_from:
            self._address(envelope_from, "envelope MAIL FROM")
        sending_domain = visible_from.rsplit("@", 1)[1].lower()
        if dkim_domain:
            dkim_domain = _domain(dkim_domain)
        if dkim_selector and not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", dkim_selector):
            raise EmailStandardsError("DKIM selector is invalid", "selector_invalid")
        now = self.now_fn().isoformat()
        identity_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"email-identity:{identity_ref}"))
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO email_sender_identities "
                "(id,identity_ref,visible_from,reply_to,envelope_from,sending_domain,dkim_domain,"
                "dkim_selector,provider_account_ref,provider_signing_status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,COALESCE((SELECT created_at FROM email_sender_identities WHERE identity_ref=?),?),?) "
                "ON CONFLICT(identity_ref) DO UPDATE SET visible_from=excluded.visible_from,"
                "reply_to=excluded.reply_to,envelope_from=excluded.envelope_from,sending_domain=excluded.sending_domain,"
                "dkim_domain=excluded.dkim_domain,dkim_selector=excluded.dkim_selector,provider_account_ref=excluded.provider_account_ref,"
                "provider_signing_status=excluded.provider_signing_status,updated_at=excluded.updated_at",
                (
                    identity_id,
                    identity_ref,
                    visible_from,
                    reply_to,
                    envelope_from,
                    sending_domain,
                    dkim_domain,
                    dkim_selector,
                    provider_account_ref,
                    provider_signing_status,
                    identity_ref,
                    now,
                    now,
                ),
            )
        return self.get_sender_identity(identity_ref)

    def get_sender_identity(self, identity_ref: str) -> dict[str, object]:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM email_sender_identities WHERE identity_ref=?", (identity_ref,)
            ).fetchone()
        if row is None:
            raise EmailStandardsError("sender identity was not found", "identity_not_found")
        return dict(row)

    def list_sender_identities(self) -> list[dict[str, object]]:
        with self.store.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM email_sender_identities ORDER BY updated_at DESC, identity_ref"
                )
            ]

    # ------------------------------------------------------------------ DNS and SPF
    def _address(self, value: str, label: str) -> str:
        if "\r" in value or "\n" in value or not validate_email(value).valid:
            raise EmailStandardsError(f"{label} address is invalid", "address_invalid")
        return normalize_email(value)

    def _resolver(self):
        if self.resolver is not None:
            return self.resolver
        if dns is None:
            return None
        resolver = dns.resolver.Resolver()
        resolver.timeout = 3.0
        resolver.lifetime = 3.0
        return resolver

    def _query(self, name: str, record_type: str, *, allow_network: bool) -> dict[str, object]:
        if not allow_network:
            return {"status": "not_requested", "records": [], "name": name, "type": record_type}
        resolver = self._resolver()
        if resolver is None:
            return {"status": "unavailable", "records": [], "name": name, "type": record_type}
        try:
            answers = resolver.resolve(name, record_type)
            records = [_record_value(answer, record_type) for answer in list(answers)[:MAX_RECORDS]]
            ttl = getattr(getattr(answers, "rrset", None), "ttl", None)
            return {
                "status": "observed" if records else "no_answer",
                "records": records,
                "ttl": ttl,
                "name": name,
                "type": record_type,
            }
        except Exception as error:
            name_lower = type(error).__name__.lower()
            if "nxdomain" in name_lower:
                status = "nxdomain"
            elif "timeout" in name_lower:
                status = "timeout"
            elif "noanswer" in name_lower:
                status = "no_answer"
            elif "servfail" in name_lower:
                status = "temporary_failure"
            else:
                status = "lookup_error"
            return {"status": status, "records": [], "name": name, "type": record_type}

    def inspect_domain(
        self,
        domain: str,
        *,
        sender_ip: str | None = None,
        envelope_from: str | None = None,
        dkim_selector: str | None = None,
        allow_network: bool = False,
    ) -> dict[str, object]:
        domain = _domain(domain)
        findings: list[dict[str, str]] = []
        observations: dict[str, object] = {}
        for record_type in ("TXT", "MX", "A", "AAAA"):
            observations[record_type] = self._query(
                domain, record_type, allow_network=allow_network
            )
        if sender_ip:
            try:
                ipaddress.ip_address(sender_ip)
                observations["PTR"] = self._query(
                    ipaddress.ip_address(sender_ip).reverse_pointer,
                    "PTR",
                    allow_network=allow_network,
                )
            except ValueError:
                findings.append(
                    _severity("blocked", "sender_ip_invalid", "The sender IP is invalid.", "input")
                )
        observations["DMARC"] = self._query(f"_dmarc.{domain}", "TXT", allow_network=allow_network)
        dkim = self._dkim_record(domain, dkim_selector, allow_network)
        if dkim_selector:
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", dkim_selector):
                findings.append(
                    _severity(
                        "blocked", "selector_invalid", "The DKIM selector is invalid.", "input"
                    )
                )
            else:
                observations["DKIM"] = self._query(
                    f"{dkim_selector}._domainkey.{domain}", "TXT", allow_network=allow_network
                )
        spf = self._spf_diagnostics(
            domain, observations["TXT"], sender_ip, envelope_from, allow_network
        )
        dmarc = self._dmarc_diagnostics(domain, observations["DMARC"], None, None, None)
        findings.extend(spf["findings"])
        findings.extend(dmarc["findings"])
        findings.extend(dkim["findings"])
        if observations["MX"]["status"] == "observed":
            findings.append(
                _severity(
                    "pass",
                    "mx_observed",
                    "MX records were observed; this does not prove a mailbox exists.",
                    "DNS",
                )
            )
        elif observations["MX"]["status"] in {"not_requested", "unavailable"}:
            findings.append(
                _severity(
                    "unknown",
                    "mx_not_observed",
                    "MX records were not inspected.",
                    "DNS",
                    "Run an explicit DNS inspection.",
                )
            )
        return self._run(
            "domain",
            domain,
            {
                "domain": domain,
                "allow_network": allow_network,
                "observations": observations,
                "spf": spf,
                "dkim": dkim,
                "dmarc": dmarc,
                "findings": findings,
            },
        )

    def _spf_diagnostics(
        self,
        domain: str,
        txt_observation: Mapping[str, object],
        sender_ip: str | None,
        envelope_from: str | None,
        allow_network: bool,
    ) -> dict[str, object]:
        records = [
            str(item)
            for item in txt_observation.get("records", [])
            if str(item).lower().startswith("v=spf1")
        ]
        findings: list[dict[str, str]] = []
        if len(records) > 1:
            findings.append(
                _severity(
                    "blocked",
                    "spf_multiple_records",
                    "Multiple SPF records are published.",
                    "SPF",
                    "Publish one SPF record.",
                )
            )
            return {"status": "invalid", "record_count": len(records), "findings": findings}
        if not records:
            if txt_observation.get("status") not in {"observed", "no_answer"}:
                findings.append(
                    _severity(
                        "unknown",
                        "spf_not_observed",
                        "SPF was not observed because the DNS lookup was not performed or failed.",
                        "SPF",
                        "Run an explicit DNS inspection and review its resolver evidence.",
                    )
                )
                return {"status": "unknown", "record_count": 0, "findings": findings}
            findings.append(
                _severity(
                    "warning",
                    "spf_missing",
                    "No SPF record was observed.",
                    "SPF",
                    "Publish one v=spf1 record.",
                )
            )
            return {"status": "missing", "record_count": 0, "findings": findings}
        parsed = self._parse_spf(records[0])
        findings.extend(parsed["findings"])
        status = (
            "configuration_valid"
            if not any(item["severity"] == "blocked" for item in findings)
            else "invalid"
        )
        result: dict[str, object] = {
            "status": status,
            "record": records[0],
            "terms": parsed["terms"],
            "findings": findings,
        }
        if sender_ip and envelope_from:
            try:
                sender_domain = envelope_from.rsplit("@", 1)[1].lower()
                result["evaluation"] = self._evaluate_spf(
                    sender_domain, sender_ip, allow_network=allow_network
                )
                result["findings"].extend(self._spf_evaluation_findings(result["evaluation"]))
            except (IndexError, EmailStandardsError) as error:
                result["evaluation"] = {"status": "unknown", "reason": str(error)}
        else:
            result["evaluation"] = {
                "status": "not_evaluated",
                "reason": "sender IP and envelope identity are required",
            }
            findings.append(
                _severity(
                    "unknown",
                    "spf_not_evaluated",
                    "SPF syntax does not prove a sender passed SPF.",
                    "SPF",
                    "Supply the connecting IP and envelope identity for evaluation.",
                )
            )
        return result

    def _parse_spf(self, record: str) -> dict[str, object]:
        terms = record.split()
        findings: list[dict[str, str]] = []
        if not terms or terms[0].lower() != "v=spf1":
            findings.append(
                _severity("blocked", "spf_syntax", "The SPF version tag is invalid.", "SPF")
            )
            return {"terms": terms, "findings": findings}
        parsed: list[dict[str, str]] = []
        mechanisms = {"all", "include", "a", "mx", "ip4", "ip6", "exists", "ptr"}
        for term in terms[1:]:
            if (
                "=" in term
                and not term.lower().startswith("redirect=")
                and not term.lower().startswith("exp=")
            ):
                key = term.split("=", 1)[0].lower()
                if key not in {"redirect", "exp"}:
                    findings.append(
                        _severity(
                            "warning",
                            "spf_unsupported_modifier",
                            f"Unsupported SPF modifier {key} was found.",
                            "SPF",
                        )
                    )
                    continue
            qualifier = "+"
            if term[:1] in "+-~?":
                qualifier, term = term[0], term[1:]
            key, _, value = term.partition(":")
            key = key.lower()
            if key == "redirect":
                key, value = "redirect", term.split("=", 1)[1] if "=" in term else ""
            if key not in mechanisms and key not in {"redirect", "exp"}:
                findings.append(
                    _severity(
                        "warning",
                        "spf_unsupported_mechanism",
                        f"Unsupported SPF term {term} was found.",
                        "SPF",
                    )
                )
                continue
            if key in {"include", "redirect", "exp", "exists"} and not value:
                findings.append(
                    _severity("blocked", "spf_syntax", f"SPF {key} requires a domain.", "SPF")
                )
            if key in {"ip4", "ip6"}:
                try:
                    ipaddress.ip_network(value, strict=False)
                except ValueError:
                    findings.append(
                        _severity(
                            "blocked",
                            "spf_network_invalid",
                            f"SPF {key} network is invalid.",
                            "SPF",
                        )
                    )
            parsed.append({"qualifier": qualifier, "mechanism": key, "value": value})
        return {"terms": parsed, "findings": findings}

    def _spf_evaluation_findings(self, evaluation: Mapping[str, object]) -> list[dict[str, str]]:
        status = str(evaluation.get("status"))
        if status == "pass":
            return [
                _severity(
                    "pass",
                    "spf_pass",
                    "The supplied IP matched the evaluated SPF identity.",
                    "SPF evaluation",
                )
            ]
        if status in {"permerror", "temperror", "lookup_limit"}:
            return [
                _severity(
                    "blocked",
                    f"spf_{status}",
                    f"SPF evaluation ended with {status}.",
                    "SPF evaluation",
                )
            ]
        if status == "fail":
            return [
                _severity(
                    "blocked",
                    "spf_fail",
                    "The supplied IP did not pass the evaluated SPF policy.",
                    "SPF evaluation",
                )
            ]
        return [
            _severity(
                "warning", "spf_not_pass", f"SPF evaluation result is {status}.", "SPF evaluation"
            )
        ]

    def _evaluate_spf(self, domain: str, ip: str, *, allow_network: bool) -> dict[str, object]:
        try:
            address = ipaddress.ip_address(ip)
        except ValueError:
            return {"status": "permerror", "reason": "invalid sender IP"}
        lookups = [0]
        return self._spf_walk(
            domain, address, allow_network=allow_network, depth=0, seen=set(), lookups=lookups
        )

    def _spf_walk(
        self,
        domain: str,
        address: ipaddress.IPv4Address | ipaddress.IPv6Address,
        *,
        allow_network: bool,
        depth: int,
        seen: set[str],
        lookups: list[int],
    ) -> dict[str, object]:
        if depth > 10 or domain in seen:
            return {"status": "permerror", "reason": "SPF include cycle or recursion limit"}
        seen = {*seen, domain}
        lookup = self._query(domain, "TXT", allow_network=allow_network)
        records = [
            str(item)
            for item in lookup.get("records", [])
            if str(item).lower().startswith("v=spf1")
        ]
        if len(records) != 1:
            return {
                "status": "permerror" if len(records) > 1 else "none",
                "reason": "SPF record count is not one",
            }
        parsed = self._parse_spf(records[0])
        if any(item["severity"] == "blocked" for item in parsed["findings"]):
            return {"status": "permerror", "reason": "invalid SPF syntax"}
        for term in parsed["terms"]:
            mechanism, value, qualifier = term["mechanism"], term["value"], term["qualifier"]
            if mechanism == "all":
                return {
                    "status": {"+": "pass", "-": "fail", "~": "softfail", "?": "neutral"}[qualifier]
                }
            matched = False
            if mechanism in {"ip4", "ip6"}:
                try:
                    matched = address in ipaddress.ip_network(value, strict=False)
                except ValueError:
                    return {"status": "permerror"}
            elif mechanism == "include":
                lookups[0] += 1
                if lookups[0] > self.max_dns_lookups:
                    return {"status": "lookup_limit"}
                nested = self._spf_walk(
                    value,
                    address,
                    allow_network=allow_network,
                    depth=depth + 1,
                    seen=seen,
                    lookups=lookups,
                )
                if nested["status"] == "pass":
                    return {"status": "pass", "matched": f"include:{value}"}
                if nested["status"] in {"permerror", "temperror", "lookup_limit"}:
                    return nested
                continue
            elif mechanism in {"a", "mx", "exists"}:
                lookups[0] += 1
                if lookups[0] > self.max_dns_lookups:
                    return {"status": "lookup_limit"}
                query_domain = value or domain
                if mechanism == "exists":
                    matched = (
                        self._query(query_domain, "A", allow_network=allow_network).get("status")
                        == "observed"
                    )
                else:
                    qtype = "A" if mechanism == "a" else "MX"
                    answer = self._query(query_domain, qtype, allow_network=allow_network)
                    for item in answer.get("records", []):
                        host = str(item).rstrip(".") if qtype == "MX" else query_domain
                        addresses = self._query(
                            host,
                            "A" if address.version == 4 else "AAAA",
                            allow_network=allow_network,
                        )
                        matched = any(
                            self._address_matches(str(candidate), address)
                            for candidate in addresses.get("records", [])
                        )
                        if matched:
                            break
            if matched:
                return {
                    "status": {"+": "pass", "-": "fail", "~": "softfail", "?": "neutral"}[
                        qualifier
                    ],
                    "matched": mechanism,
                }
        redirect = next(
            (term["value"] for term in parsed["terms"] if term["mechanism"] == "redirect"), None
        )
        if redirect:
            lookups[0] += 1
            if lookups[0] > self.max_dns_lookups:
                return {"status": "lookup_limit"}
            return self._spf_walk(
                redirect,
                address,
                allow_network=allow_network,
                depth=depth + 1,
                seen=seen,
                lookups=lookups,
            )
        return {"status": "neutral"}

    @staticmethod
    def _address_matches(value: str, address: object) -> bool:
        try:
            return (
                ipaddress.ip_address(value).version == address.version
                and ipaddress.ip_address(value) == address
            )
        except ValueError:
            return False

    # ------------------------------------------------------------------ DKIM, DMARC, and MIME
    def _dkim_record(
        self, domain: str, selector: str | None, allow_network: bool
    ) -> dict[str, object]:
        if not selector:
            return {"status": "not_configured", "findings": []}
        observation = self._query(
            f"{selector}._domainkey.{domain}", "TXT", allow_network=allow_network
        )
        records = observation.get("records", [])
        findings: list[dict[str, str]] = []
        if not records:
            if observation.get("status") not in {"observed", "no_answer"}:
                findings.append(
                    _severity(
                        "unknown",
                        "dkim_not_observed",
                        "The DKIM key was not observed because the DNS lookup was not performed or failed.",
                        "DKIM",
                        "Run an explicit DNS inspection and review its resolver evidence.",
                    )
                )
                return {
                    "status": "unknown",
                    "selector": selector,
                    "observation": observation,
                    "findings": findings,
                }
            findings.append(
                _severity(
                    "warning",
                    "dkim_key_missing",
                    "No DKIM public key record was observed.",
                    "DKIM",
                    "Publish the selector key or confirm provider-managed signing.",
                )
            )
            return {
                "status": "missing",
                "selector": selector,
                "observation": observation,
                "findings": findings,
            }
        tags = _tags(str(records[0]))
        if tags.get("v", "DKIM1").upper() != "DKIM1" or not tags.get("p"):
            findings.append(
                _severity(
                    "blocked",
                    "dkim_record_invalid",
                    "The DKIM public-key record is invalid or revoked.",
                    "DKIM",
                )
            )
        else:
            try:
                base64.b64decode(tags["p"], validate=True)
                findings.append(
                    _severity(
                        "pass",
                        "dkim_record_valid",
                        "The DKIM public-key record has valid syntax.",
                        "DKIM DNS",
                    )
                )
            except ValueError, base64.binascii.Error:
                findings.append(
                    _severity(
                        "blocked",
                        "dkim_key_invalid",
                        "The DKIM public key is not valid base64.",
                        "DKIM",
                    )
                )
        return {
            "status": "valid"
            if not any(item["severity"] == "blocked" for item in findings)
            else "invalid",
            "selector": selector,
            "record": str(records[0]),
            "tags": tags,
            "findings": findings,
        }

    def _dmarc_diagnostics(
        self,
        domain: str,
        observation: Mapping[str, object],
        message,
        spf_domain: str | None,
        dkim_domain: str | None,
    ) -> dict[str, object]:
        records = [
            str(item)
            for item in observation.get("records", [])
            if str(item).lower().startswith("v=dmarc1")
        ]
        findings: list[dict[str, str]] = []
        if len(records) > 1:
            findings.append(
                _severity(
                    "blocked",
                    "dmarc_multiple_records",
                    "Multiple DMARC records were observed.",
                    "DMARC",
                    "Publish one _dmarc record.",
                )
            )
            return {"status": "invalid", "findings": findings}
        if not records:
            if observation.get("status") not in {"observed", "no_answer"}:
                findings.append(
                    _severity(
                        "unknown",
                        "dmarc_not_observed",
                        "DMARC was not observed because the DNS lookup was not performed or failed.",
                        "DMARC",
                        "Run an explicit DNS inspection and review its resolver evidence.",
                    )
                )
                return {"status": "unknown", "findings": findings}
            findings.append(
                _severity(
                    "warning",
                    "dmarc_missing",
                    "No DMARC policy was observed.",
                    "DMARC",
                    "Publish a DMARC policy for the sending domain.",
                )
            )
            return {"status": "missing", "findings": findings}
        tags = _tags(records[0])
        if tags.get("v", "").upper() != "DMARC1" or tags.get("p") not in {
            "none",
            "quarantine",
            "reject",
        }:
            findings.append(
                _severity(
                    "blocked",
                    "dmarc_syntax",
                    "The DMARC record has an invalid version or policy.",
                    "DMARC",
                )
            )
        for key in ("adkim", "aspf"):
            if tags.get(key, "r") not in {"r", "s"}:
                findings.append(
                    _severity(
                        "blocked",
                        "dmarc_alignment_mode",
                        f"DMARC {key} alignment mode is invalid.",
                        "DMARC",
                    )
                )
        result: dict[str, object] = {
            "status": "valid"
            if not any(item["severity"] == "blocked" for item in findings)
            else "invalid",
            "record": records[0],
            "tags": tags,
            "findings": findings,
        }
        if message is not None:
            result["alignment"] = self._message_alignment(message, tags, spf_domain, dkim_domain)
            if result["alignment"]["result"] == "pass":
                findings.append(
                    _severity(
                        "pass",
                        "dmarc_pass",
                        "The supplied authentication evidence is aligned with the From domain.",
                        "DMARC alignment",
                    )
                )
            else:
                findings.append(
                    _severity(
                        "unknown",
                        "dmarc_not_proven",
                        "A published DMARC policy does not prove this message passed DMARC.",
                        "DMARC alignment",
                    )
                )
        return result

    def _message_alignment(
        self, message, tags: Mapping[str, str], spf_domain: str | None, dkim_domain: str | None
    ) -> dict[str, object]:
        from_values = getaddresses(message.get_all("From", []))
        author_domain = (
            from_values[0][1].rsplit("@", 1)[1].lower()
            if from_values and "@" in from_values[0][1]
            else None
        )
        mode_spf, mode_dkim = tags.get("aspf", "r"), tags.get("adkim", "r")
        registered = self.catalog.registered_domain(author_domain or "")
        return {
            "author_domain": author_domain,
            "spf": bool(
                author_domain
                and spf_domain
                and (
                    author_domain == spf_domain
                    if mode_spf == "s"
                    else registered == self.catalog.registered_domain(spf_domain)
                )
            ),
            "dkim": bool(
                author_domain
                and dkim_domain
                and (
                    author_domain == dkim_domain
                    if mode_dkim == "s"
                    else registered == self.catalog.registered_domain(dkim_domain)
                )
            ),
            "spf_mode": mode_spf,
            "dkim_mode": mode_dkim,
            "result": "pass"
            if (
                (
                    author_domain
                    and spf_domain
                    and (
                        author_domain == spf_domain
                        if mode_spf == "s"
                        else registered == self.catalog.registered_domain(spf_domain)
                    )
                )
                or (
                    author_domain
                    and dkim_domain
                    and (
                        author_domain == dkim_domain
                        if mode_dkim == "s"
                        else registered == self.catalog.registered_domain(dkim_domain)
                    )
                )
            )
            else "fail",
        }

    def validate_message(
        self,
        raw: bytes,
        *,
        communication_purpose: str = "transactional",
        expected_identity: str | None = None,
        trusted_receiver: bool = False,
    ) -> dict[str, object]:
        if not isinstance(raw, bytes) or len(raw) > MAX_MESSAGE_BYTES:
            raise EmailStandardsError(
                "message exceeds the 25 MiB validation limit", "message_limit"
            )
        findings: list[dict[str, str]] = []
        if b"\r\n" not in raw and b"\n" in raw:
            findings.append(
                _severity(
                    "warning",
                    "line_endings",
                    "The message uses LF line endings instead of CRLF.",
                    "MIME",
                )
            )
        if any(len(line) > 998 for line in raw.splitlines()):
            findings.append(
                _severity(
                    "blocked",
                    "line_length",
                    "A header or body line exceeds the RFC line limit.",
                    "MIME",
                )
            )
        try:
            message = BytesParser(policy=policy.SMTP).parsebytes(raw)
        except Exception as error:
            raise EmailStandardsError(
                f"MIME parsing failed: {type(error).__name__}", "mime_invalid"
            ) from error
        for name in ("From", "To", "Date", "Message-ID", "Subject"):
            if message.get(name) is None:
                findings.append(
                    _severity(
                        "warning",
                        f"missing_{name.lower().replace('-', '_')}",
                        f"The {name} header is missing.",
                        "RFC 5322",
                    )
                )
        for name in ("From", "To", "Cc", "Reply-To"):
            for _display, address in getaddresses(message.get_all(name, [])):
                if address and not validate_email(address).valid:
                    findings.append(
                        _severity(
                            "blocked",
                            "address_invalid",
                            f"The {name} header contains an invalid address.",
                            "RFC 5322",
                        )
                    )
        for name in ("Message-ID", "Date"):
            if len(message.get_all(name, [])) > 1:
                findings.append(
                    _severity(
                        "warning",
                        f"duplicate_{name.lower().replace('-', '_')}",
                        f"The {name} header is duplicated.",
                        "RFC 5322",
                    )
                )
        html_parts: list[str] = []
        text_parts: list[str] = []
        cids: set[str] = set()
        for part in message.walk():
            content_type = part.get_content_type()
            if content_type == "text/html":
                html_parts.append(part.get_content())
            elif content_type == "text/plain":
                text_parts.append(part.get_content())
            if part.get("Content-ID"):
                cids.add(part["Content-ID"].strip("<>"))
            if part.get_filename() and any(char in part.get_filename() for char in "\r\n"):
                findings.append(
                    _severity(
                        "blocked",
                        "attachment_name_injection",
                        "An attachment filename contains a header control character.",
                        "MIME",
                    )
                )
        html_body = "\n".join(html_parts)
        if html_parts and not text_parts:
            findings.append(
                _severity(
                    "warning",
                    "missing_plain_text",
                    "The HTML message has no plain-text alternative.",
                    "MIME",
                    "Add a text/plain alternative.",
                )
            )
        for cid in _CID.findall(html_body):
            if cid not in cids:
                findings.append(
                    _severity(
                        "blocked",
                        "cid_missing",
                        f"HTML references missing inline asset {cid}.",
                        "content",
                        "Add the matching Content-ID asset or remove the reference.",
                    )
                )
        for image in _IMG.findall(html_body):
            if not re.search(r"\balt\s*=", image, re.I):
                findings.append(
                    _severity(
                        "warning", "image_alt_missing", "An HTML image has no alt text.", "content"
                    )
                )
        for url in _URL.findall(html_body):
            if urlsplit(url).scheme.lower() not in {"http", "https", "mailto"}:
                findings.append(
                    _severity(
                        "blocked", "unsafe_url", "HTML contains an unsafe URL scheme.", "content"
                    )
                )
        list_unsubscribe = message.get("List-Unsubscribe")
        category = communication_purpose.lower()
        if (
            category in MARKETING_PURPOSES
            and not list_unsubscribe
            and "unsubscribe" not in (html_body + "\n".join(text_parts)).lower()
        ):
            findings.append(
                _severity(
                    "blocked",
                    "unsubscribe_missing",
                    "Marketing or subscribed content has no visible or header unsubscribe path.",
                    "policy",
                    "Configure a stable unsubscribe endpoint and visible content.",
                )
            )
        auth = self._authentication_evidence(message, trusted_receiver=trusted_receiver)
        identity = None
        if expected_identity:
            identity = self.get_sender_identity(expected_identity)
            from_address = getaddresses(message.get_all("From", []))
            if from_address and from_address[0][1].lower() != str(identity["visible_from"]).lower():
                findings.append(
                    _severity(
                        "blocked",
                        "sender_identity_mismatch",
                        "The message From address differs from the selected sender identity.",
                        "sender identity",
                    )
                )
        selected_headers = {
            name.lower(): message.get(name)
            for name in HEADER_NAMES
            if message.get(name) is not None
        }
        result = {
            "size_bytes": len(raw),
            "headers": selected_headers,
            "mime": {
                "multipart": message.is_multipart(),
                "content_type": message.get_content_type(),
                "parts": sum(1 for _ in message.walk()),
            },
            "authentication": auth,
            "findings": findings,
        }
        if identity:
            result["sender_identity"] = identity
        return self._run("message", expected_identity, result)

    @staticmethod
    def _authentication_evidence(message, *, trusted_receiver: bool) -> dict[str, object]:
        signatures = []
        for header in message.get_all("DKIM-Signature", []):
            tags = {
                match.group("key").lower(): match.group("value").strip()
                for match in _DKIM_TAG.finditer(header)
            }
            signatures.append(
                {
                    "d": tags.get("d"),
                    "s": tags.get("s"),
                    "a": tags.get("a"),
                    "claimed": True,
                    "verification": "not_performed",
                }
            )
        return {
            "dkim_signatures": signatures,
            "authentication_results": [
                {
                    "value": value,
                    "evidence": "trusted_receiver_observation"
                    if trusted_receiver
                    else "untrusted_imported_claim",
                }
                for value in message.get_all("Authentication-Results", [])
            ],
            "received_spf": [
                {
                    "value": value,
                    "evidence": "trusted_receiver_observation"
                    if trusted_receiver
                    else "untrusted_imported_claim",
                }
                for value in message.get_all("Received-SPF", [])
            ],
        }

    # ------------------------------------------------------------------ unsubscribe
    def configure_unsubscribe_endpoint(
        self, endpoint_ref: str, base_url: str, *, verification_status: str = "not_configured"
    ) -> dict[str, object]:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or "?" in base_url
            or "#" in base_url
        ):
            raise EmailStandardsError(
                "unsubscribe endpoint must be an absolute HTTP(S) URL without query or fragment",
                "endpoint_invalid",
            )
        if verification_status not in {
            "not_configured",
            "external_configured",
        }:
            raise EmailStandardsError(
                "unsubscribe verification status is invalid", "endpoint_invalid"
            )
        now = self.now_fn().isoformat()
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO email_unsubscribe_endpoints(id,endpoint_ref,base_url,verification_status,configured_at,verified_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(endpoint_ref) DO UPDATE SET base_url=excluded.base_url,verification_status=excluded.verification_status,verified_at=excluded.verified_at",
                (
                    str(uuid.uuid5(uuid.NAMESPACE_URL, f"unsubscribe:{endpoint_ref}")),
                    endpoint_ref,
                    base_url.rstrip("/"),
                    verification_status,
                    now,
                    now if verification_status == "local_fixture_verified" else None,
                ),
            )
        return self.endpoint(endpoint_ref)

    def verify_local_fixture_endpoint(self, endpoint_ref: str) -> dict[str, object]:
        """Record local-fixture verification only after an injected protocol probe."""

        endpoint = self.endpoint(endpoint_ref)
        parsed = urlsplit(str(endpoint["base_url"]))
        if parsed.scheme != "https" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise EmailStandardsError(
                "local fixture verification requires a loopback HTTPS endpoint",
                "endpoint_unverified",
            )
        if self.endpoint_probe is None or not self.endpoint_probe(str(endpoint["base_url"])):
            raise EmailStandardsError(
                "the unsubscribe endpoint has not passed a local protocol probe",
                "endpoint_unverified",
            )
        now = self.now_fn().isoformat()
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE email_unsubscribe_endpoints SET verification_status=?,verified_at=? WHERE endpoint_ref=?",
                ("local_fixture_verified", now, endpoint_ref),
            )
        return self.endpoint(endpoint_ref)

    def endpoint(self, endpoint_ref: str) -> dict[str, object]:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM email_unsubscribe_endpoints WHERE endpoint_ref=?", (endpoint_ref,)
            ).fetchone()
        if row is None:
            raise EmailStandardsError("unsubscribe endpoint was not found", "endpoint_not_found")
        return dict(row)

    def create_unsubscribe_token(
        self, endpoint_ref: str, list_id: str, email: str, *, ttl_days: int = 30
    ) -> dict[str, object]:
        endpoint = self.endpoint(endpoint_ref)
        if not 1 <= ttl_days <= 365:
            raise EmailStandardsError("unsubscribe token lifetime is invalid", "token_invalid")
        email = normalize_email(self._address(email, "unsubscribe recipient"))
        with self.store.connect() as connection:
            contact = connection.execute(
                "SELECT id FROM contacts WHERE normalized_email=?", (email,)
            ).fetchone()
            if connection.execute("SELECT 1 FROM lists WHERE id=?", (list_id,)).fetchone() is None:
                raise EmailStandardsError("unsubscribe list was not found", "list_not_found")
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        expires = (self.now_fn() + timedelta(days=ttl_days)).isoformat()
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO email_unsubscribe_tokens(id,endpoint_ref,token_hash,list_id,normalized_email,contact_id,expires_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    str(uuid.uuid4()),
                    endpoint_ref,
                    digest,
                    list_id,
                    email,
                    contact["id"] if contact else None,
                    expires,
                    self.now_fn().isoformat(),
                ),
            )
        return {
            "endpoint_ref": endpoint_ref,
            "list_id": list_id,
            "expires_at": expires,
            "url": f"{str(endpoint['base_url']).rstrip('/')}/{token}",
            "token": token,
        }

    def create_unsubscribe_url(
        self, endpoint_ref: str, list_id: str, email: str, *, ttl_days: int = 30
    ) -> str:
        """Create a recipient-scoped opaque URL for prepared campaign MIME."""

        return str(
            self.create_unsubscribe_token(endpoint_ref, list_id, email, ttl_days=ttl_days)["url"]
        )

    def unsubscribe_readiness(
        self,
        *,
        communication_purpose: str,
        endpoint_ref: str | None = None,
        message: bytes | None = None,
        dkim_evidence: str = "not_verified",
    ) -> dict[str, object]:
        if communication_purpose.lower() not in MARKETING_PURPOSES:
            return {
                "status": "not_required",
                "communication_purpose": communication_purpose,
                "findings": [
                    _severity(
                        "pass",
                        "unsubscribe_not_required",
                        "This communication category does not require a promotional unsubscribe path.",
                        "policy",
                    )
                ],
            }
        findings: list[dict[str, str]] = []
        endpoint = None
        if endpoint_ref:
            try:
                endpoint = self.endpoint(endpoint_ref)
            except EmailStandardsError:
                findings.append(
                    _severity(
                        "blocked",
                        "endpoint_not_found",
                        "The configured unsubscribe endpoint was not found.",
                        "unsubscribe",
                    )
                )
        else:
            findings.append(
                _severity(
                    "blocked",
                    "endpoint_not_configured",
                    "No unsubscribe endpoint is configured.",
                    "unsubscribe",
                    "Configure a provider-neutral HTTPS endpoint.",
                )
            )
        if message is not None:
            parsed = BytesParser(policy=policy.SMTP).parsebytes(message)
            list_url = parsed.get("List-Unsubscribe", "")
            post = parsed.get("List-Unsubscribe-Post")
            if list_url.startswith("<https://") and post == "List-Unsubscribe=One-Click":
                findings.append(
                    _severity(
                        "pass",
                        "one_click_headers",
                        "The RFC 8058 header pair is present.",
                        "RFC 8058",
                    )
                )
            else:
                findings.append(
                    _severity(
                        "warning",
                        "one_click_headers_missing",
                        "The RFC 8058 header pair is not complete.",
                        "RFC 8058",
                    )
                )
        status = "not_configured" if endpoint is None else "external_endpoint_configured"
        if endpoint and str(endpoint["verification_status"]) == "local_fixture_verified":
            status = "local_fixture_verified"
        headers_ok = any(item.get("code") == "one_click_headers" for item in findings)
        full = bool(
            endpoint
            and endpoint["verification_status"] == "local_fixture_verified"
            and message is not None
            and headers_ok
            and dkim_evidence == "locally_cryptographically_verified"
        )
        if full:
            status = "full_one_click_requirements_verified"
        else:
            findings.append(
                _severity(
                    "unknown",
                    "one_click_not_fully_verified",
                    "Headers alone do not prove a working endpoint and valid DKIM coverage.",
                    "RFC 8058",
                    "Verify the endpoint protocol and DKIM-signed headers.",
                )
            )
        return {
            "status": status,
            "communication_purpose": communication_purpose,
            "endpoint": endpoint,
            "findings": findings,
        }

    def process_unsubscribe(
        self, endpoint_ref: str, token: str, *, method: str, body: str, list_id: str | None = None
    ) -> dict[str, object]:
        if method.upper() != "POST":
            raise EmailStandardsError(
                "unsubscribe changes require POST; GET never changes policy", "method_not_allowed"
            )
        if body != "List-Unsubscribe=One-Click":
            raise EmailStandardsError("unsubscribe POST body is invalid", "request_invalid")
        try:
            digest = hashlib.sha256(token.encode("ascii", "strict")).hexdigest()
        except UnicodeEncodeError as error:
            raise EmailStandardsError("unsubscribe token is invalid", "token_invalid") from error
        now = self.now_fn().isoformat()
        with self.store.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM email_unsubscribe_tokens WHERE endpoint_ref=? AND token_hash=?",
                (endpoint_ref, digest),
            ).fetchone()
            if row is None:
                raise EmailStandardsError("unsubscribe token is invalid", "token_invalid")
            if list_id and list_id != row["list_id"]:
                raise EmailStandardsError(
                    "unsubscribe token is scoped to another list", "token_scope_mismatch"
                )
            if datetime.fromisoformat(row["expires_at"]) <= self.now_fn():
                raise EmailStandardsError("unsubscribe token has expired", "token_expired")
            if row["consumed_at"]:
                return {
                    "status": "already_processed",
                    "list_id": row["list_id"],
                    "normalized_email": row["normalized_email"],
                }
            suppression_id = self.store.add_suppression(
                category=SuppressionCategory.UNSUBSCRIBE,
                reason="RFC 8058 one-click unsubscribe",
                normalized_email=row["normalized_email"],
                contact_id=row["contact_id"],
                scope_type="list",
                scope_id=row["list_id"],
                actor="unsubscribe-endpoint",
                source="rfc8058-one-click",
                connection=connection,
            )
            connection.execute(
                "UPDATE email_unsubscribe_tokens SET consumed_at=? WHERE id=? AND consumed_at IS NULL",
                (now, row["id"]),
            )
        return {
            "status": "processed",
            "suppression_id": suppression_id,
            "list_id": row["list_id"],
            "normalized_email": row["normalized_email"],
        }

    # ------------------------------------------------------------------ DMARC aggregate reports
    def import_dmarc_report(
        self, source: bytes | str | Path, *, filename: str | None = None
    ) -> dict[str, object]:
        if isinstance(source, (str, Path)):
            path = Path(source).resolve()
            raw = path.read_bytes()
            filename = filename or path.name
        else:
            raw = bytes(source)
        if len(raw) > MAX_REPORT_BYTES:
            raise EmailStandardsError(
                "aggregate report exceeds the 5 MiB compressed/input limit", "report_limit"
            )
        source_hash = hashlib.sha256(raw).hexdigest()
        xml_bytes = self._decompress_report(raw, filename or "report.xml")
        try:
            root = ElementTree.fromstring(xml_bytes)
        except ElementTree.ParseError as error:
            raise EmailStandardsError(
                "DMARC aggregate XML is malformed", "report_xml_invalid"
            ) from error
        nodes = list(root.iter())
        if len(nodes) > MAX_REPORT_ROWS * 4:
            raise EmailStandardsError(
                "DMARC aggregate XML is too deeply nested or large", "report_structure_limit"
            )
        report_id = self._xml_text(root, "report_metadata/report_id")
        source_org = self._xml_text(root, "report_metadata/org_name") or "unknown"
        report_domain = self._xml_text(root, "policy_published/domain")
        if not report_id or not report_domain:
            raise EmailStandardsError(
                "DMARC report requires report_id and policy domain", "report_required_field"
            )
        with self.store.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM email_dmarc_aggregate_reports WHERE report_id=?", (report_id,)
            ).fetchone()
        if existing:
            return {
                "status": "duplicate",
                "report_id": report_id,
                "existing_id": existing["id"],
                "source_sha256": existing["source_sha256"],
                "records": json.loads(existing["records_json"]),
            }
        records: list[dict[str, object]] = []
        for record in root.findall(".//record")[:MAX_REPORT_ROWS]:
            row = {
                "source_ip": self._xml_text(record, "row/source_ip"),
                "count": self._bounded_count(self._xml_text(record, "row/count")),
                "disposition": self._xml_text(record, "row/policy_evaluated/disposition"),
                "dkim": self._xml_text(record, "row/policy_evaluated/dkim"),
                "spf": self._xml_text(record, "row/policy_evaluated/spf"),
                "header_from": self._xml_text(record, "identifiers/header_from"),
                "envelope_from": self._xml_text(record, "identifiers/envelope_from"),
            }
            if row["count"] is None or not row["source_ip"]:
                raise EmailStandardsError(
                    "DMARC report contains an invalid record", "report_record_invalid"
                )
            records.append(row)
        now = self.now_fn().isoformat()
        policy_data = {
            key: self._xml_text(root, f"policy_published/{key}")
            for key in ("domain", "adkim", "aspf", "p", "sp", "pct")
        }
        report_id_db = str(uuid.uuid4())
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO email_dmarc_aggregate_reports(id,report_id,source_org,report_domain,period_begin,period_end,policy_json,records_json,source_sha256,imported_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    report_id_db,
                    report_id,
                    source_org,
                    report_domain,
                    self._xml_text(root, "report_metadata/date_range/begin"),
                    self._xml_text(root, "report_metadata/date_range/end"),
                    json.dumps(policy_data, sort_keys=True),
                    json.dumps(records, sort_keys=True),
                    source_hash,
                    now,
                ),
            )
        findings = [
            _severity(
                "pass",
                "report_imported",
                f"Imported {len(records)} DMARC aggregate rows from {source_org}.",
                "DMARC aggregate report",
            )
        ]
        return self._run(
            "dmarc_aggregate",
            report_id,
            {
                "status": "imported",
                "report_id": report_id,
                "source_org": source_org,
                "report_domain": report_domain,
                "records": records,
                "source_sha256": source_hash,
                "findings": findings,
            },
        )

    @staticmethod
    def _decompress_report(raw: bytes, filename: str) -> bytes:
        def safe_xml_bytes(data: bytes) -> bytes:
            if len(data) > MAX_REPORT_EXPANDED_BYTES:
                raise EmailStandardsError(
                    "DMARC report expands beyond the safe limit", "report_limit"
                )
            forbidden = False
            for encoding in ("utf-8", "utf-16", "utf-16-le", "utf-16-be", "utf-32"):
                try:
                    text = data.decode(encoding)
                except UnicodeDecodeError:
                    continue
                upper = text.upper()
                if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
                    forbidden = True
                    break
            if forbidden or b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
                raise EmailStandardsError("DMARC XML entities are not allowed", "report_xml_unsafe")
            return data

        lowered = filename.lower()
        if raw.startswith(b"PK") or lowered.endswith(".zip"):
            try:
                with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                    members = [item for item in archive.infolist() if not item.is_dir()]
                    if len(members) != 1 or Path(members[0].filename).name != members[0].filename:
                        raise EmailStandardsError(
                            "DMARC ZIP must contain one safe file", "report_archive_invalid"
                        )
                    if members[0].file_size > MAX_REPORT_EXPANDED_BYTES:
                        raise EmailStandardsError(
                            "DMARC ZIP expands beyond the safe limit", "report_limit"
                        )
                    with archive.open(members[0], "r") as member:
                        return safe_xml_bytes(member.read(MAX_REPORT_EXPANDED_BYTES + 1))
            except zipfile.BadZipFile as error:
                raise EmailStandardsError(
                    "DMARC ZIP container is malformed", "report_archive_invalid"
                ) from error
        if raw.startswith(b"\x1f\x8b") or lowered.endswith((".gz", ".gzip")):
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
                data = stream.read(MAX_REPORT_EXPANDED_BYTES + 1)
        elif raw.startswith(b"BZh") or lowered.endswith(".bz2"):
            with bz2.BZ2File(io.BytesIO(raw)) as stream:
                data = stream.read(MAX_REPORT_EXPANDED_BYTES + 1)
        else:
            data = raw
        return safe_xml_bytes(data)

    @staticmethod
    def _xml_text(root, path: str) -> str:
        node = root.find(path)
        return (node.text or "").strip() if node is not None else ""

    @staticmethod
    def _bounded_count(value: str) -> int | None:
        try:
            number = int(value)
        except TypeError, ValueError:
            return None
        return number if 0 <= number <= 10_000_000 else None

    def report_summary(self) -> dict[str, object]:
        with self.store.connect() as connection:
            reports = connection.execute(
                "SELECT report_domain, COUNT(*) AS reports, SUM(json_extract(records_json, '$[0].count')) AS sample_count FROM email_dmarc_aggregate_reports GROUP BY report_domain ORDER BY report_domain"
            ).fetchall()
        return {"reports": [dict(row) for row in reports], "history": self.history(limit=25)}

    # ------------------------------------------------------------------ campaign/provider guidance
    def provider_guidance(self) -> list[dict[str, object]]:
        return [
            {
                "provider": "google",
                "reviewed_at": "2026-10-28",
                "scope": "personal Gmail direct mail and bulk sender guidance",
                "requirements": [
                    "SPF or DKIM for all senders",
                    "SPF and DKIM plus DMARC for bulk senders",
                    "From alignment with SPF or DKIM for direct bulk mail",
                    "one-click unsubscribe and visible link for marketing/subscribed mail",
                ],
                "source": "https://support.google.com/mail/answer/81126",
            },
            {
                "provider": "yahoo",
                "reviewed_at": "2026-10-28",
                "scope": "Yahoo sender best practices",
                "requirements": [
                    "SPF or DKIM for all senders",
                    "SPF and DKIM plus DMARC for bulk senders",
                    "forward and reverse DNS for sending IPs",
                    "functioning list-unsubscribe for bulk mail",
                ],
                "source": "https://senders.yahooinc.com/best-practices/",
            },
        ]

    def preflight_campaign(self, campaign: Mapping[str, object]) -> dict[str, object]:
        configuration = json.loads(str(campaign["configuration_json"] or "{}"))
        purpose = str(configuration.get("communication_purpose", "transactional"))
        findings: list[dict[str, str]] = []
        sender_email = str(configuration.get("sender_email", ""))
        if not validate_email(sender_email).valid:
            findings.append(
                _severity(
                    "blocked",
                    "sender_invalid",
                    "Campaign sender identity is invalid.",
                    "campaign configuration",
                )
            )
        else:
            findings.append(
                _severity(
                    "pass",
                    "sender_configured",
                    "Visible From identity is syntactically configured.",
                    "campaign configuration",
                )
            )
            findings.append(
                _severity(
                    "unknown",
                    "authentication_not_observed",
                    "SPF, DKIM, and DMARC are not observed by campaign preflight without an explicit DNS diagnostic.",
                    "email standards",
                    "Run Email Standards domain inspection.",
                )
            )
        if purpose in MARKETING_PURPOSES:
            endpoint_ref = configuration.get("unsubscribe_endpoint_ref")
            audience_list_id = (
                campaign["audience_list_id"]
                if hasattr(campaign, "keys") and "audience_list_id" in campaign.keys()
                else campaign.get("audience_list_id")
                if isinstance(campaign, Mapping)
                else None
            )
            if endpoint_ref and not audience_list_id:
                findings.append(
                    _severity(
                        "blocked",
                        "unsubscribe_scope_missing",
                        "Marketing preparation requires an explicit list scope for unsubscribe tokens.",
                        "campaign configuration",
                        "Select a list or create a durable segment anchor before preparation.",
                    )
                )
            readiness = self.unsubscribe_readiness(
                communication_purpose=purpose,
                endpoint_ref=str(endpoint_ref) if endpoint_ref else None,
            )
            findings.extend(readiness["findings"])
        return {
            "findings": findings,
            "communication_purpose": purpose,
            "ready": not any(item["severity"] == "blocked" for item in findings),
        }


__all__ = ["EmailStandardsError", "EmailStandardsService", "RULESET_VERSION"]
