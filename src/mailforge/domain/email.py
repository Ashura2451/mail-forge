"""Pure email normalization and validation rules.

These rules intentionally do not perform DNS, consent, suppression, or
delivery checks. Syntax and domain evidence are separate from sending policy.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

EMAIL_PATTERN = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9._%+\-]*[A-Za-z0-9])?@"
    r"[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?)*"
    r"\.[A-Za-z]{2,63}"
)
UNICODE_EMAIL_PATTERN = re.compile(
    r"(?<![\w.!#$%&'*+/=?^`{|}~-])"
    r"[\w](?:[\w.!#$%&'*+/=?^`{|}~-]*[\w])?@"
    r"[\w](?:[\w-]*[\w])?(?:\.[\w](?:[\w-]*[\w])?)+"
    r"(?![\w-]|\.(?=[\w-]))",
    re.UNICODE,
)
VALID_LOCAL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._%+\-]*[A-Za-z0-9])?$")
VALID_DOMAIN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?)*\.[A-Za-z]{2,63}$"
)

ROLE_PREFIXES = frozenset(
    {
        "abuse",
        "admin",
        "administrator",
        "billing",
        "compliance",
        "contact",
        "devnull",
        "dns",
        "ftp",
        "help",
        "hostmaster",
        "info",
        "legal",
        "mailer-daemon",
        "marketing",
        "media",
        "news",
        "nobody",
        "noc",
        "noreply",
        "no-reply",
        "null",
        "office",
        "operations",
        "postmaster",
        "privacy",
        "registrar",
        "root",
        "sales",
        "security",
        "spam",
        "support",
        "sysadmin",
        "tech",
        "undisclosed-recipients",
        "unsubscribe",
        "usenet",
        "uucp",
        "webmaster",
        "www",
    }
)


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    reason: str


def normalize_email(value: str) -> str:
    """Normalize an address using the legacy Mailforge lowercase behavior."""

    value = value.strip()
    if "@" not in value:
        return value.lower()
    local, domain = value.rsplit("@", 1)
    return f"{local.lower()}@{domain.lower()}"


def validate_email(value: str, *, min_length: int = 6, max_length: int = 254) -> ValidationResult:
    """Validate syntax without making network or deliverability claims."""

    if not value or len(value) < min_length or len(value) > max_length:
        return ValidationResult(False, "invalid_length")
    try:
        from email_validator import EmailNotValidError
        from email_validator import validate_email as validate_external

        validate_external(value, check_deliverability=False)
        return ValidationResult(True, "ok")
    except ImportError:
        pass
    except EmailNotValidError as error:
        return ValidationResult(False, str(error).lower().replace(" ", "_")[:30])
    if value.count("@") != 1:
        return ValidationResult(False, "bad_at")

    local, domain = value.rsplit("@", 1)
    if not local or not domain:
        return ValidationResult(False, "empty_part")
    if len(local) > 64 or len(domain) > 253:
        return ValidationResult(False, "part_too_long")
    if ".." in local or ".." in domain:
        return ValidationResult(False, "consecutive_dots")
    if not VALID_LOCAL.fullmatch(local) or not VALID_DOMAIN.fullmatch(domain):
        return ValidationResult(False, "invalid_chars")
    if any(len(label) > 63 for label in domain.split(".")):
        return ValidationResult(False, "label_too_long")
    try:
        ipaddress.ip_address(domain.strip("[]"))
    except ValueError:
        pass
    else:
        return ValidationResult(False, "ip_domain")
    return ValidationResult(True, "ok")


def extract_addresses(text: str) -> list[str]:
    """Extract syntactically shaped addresses from arbitrary text."""

    return UNICODE_EMAIL_PATTERN.findall(text)


def classify_address(
    value: str, disposable_domains: set[str] | frozenset[str] = frozenset()
) -> dict:
    """Return local classification; DNS and consent remain intentionally absent."""

    normalized = normalize_email(value)
    result = validate_email(normalized)
    if not result.valid:
        return {"email": normalized, "valid": False, "reason": result.reason}
    local, domain = normalized.rsplit("@", 1)
    base_local = local.split("+", 1)[0]
    return {
        "email": normalized,
        "valid": True,
        "reason": "ok",
        "domain": domain,
        "is_role_based": base_local in ROLE_PREFIXES,
        "is_disposable": any(
            domain == candidate or domain.endswith(f".{candidate}")
            for candidate in {item.lower().strip().rstrip(".") for item in disposable_domains}
        ),
        "has_subaddress": "+" in local,
    }
