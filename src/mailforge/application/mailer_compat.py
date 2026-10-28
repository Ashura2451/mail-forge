"""Bounded compatibility translation for the former MAILER file contract.

This module translates legacy configuration and state into the canonical
CampaignStore/OutboundService boundary. It never loads ``.env`` files, stores
SMTP credentials in SQLite, or sends mail as a side effect of translation.
"""

from __future__ import annotations

import csv
import json
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import yaml

from mailforge.application.campaigns import CampaignService
from mailforge.application.outbound import OutboundService
from mailforge.domain.campaign import (
    AttemptOutcome,
    ContactCandidate,
    DeliveryState,
    SuppressionCategory,
)
from mailforge.domain.email import normalize_email, validate_email
from mailforge.domain.outbound import (
    AttachmentSpec,
    CatchUpPolicy,
    ScheduleMode,
    ScheduleSpec,
    SenderPolicy,
    TemplateDefinition,
)
from mailforge.infrastructure.campaign_store import CampaignStore

_ENV_REFERENCE = re.compile(
    r"\$(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}|(?P<plain>[A-Za-z_][A-Za-z0-9_]*))"
)
_SECRET_KEY = re.compile(r"(?:password|secret|token|api[_-]?key|private[_-]?key|credential)", re.I)
_KNOWN_SECTIONS = {
    "campaigns",
    "smtp_pool",
    "tracking",
    "rate_limiting",
    "domain_throttle",
    "adaptive_domain_throttle",
    "sending_window",
    "smtp_reputation",
    "logging",
    "health_check",
    "targets",
    "files",
    "advanced",
}


@dataclass(frozen=True)
class LegacySmtpAccount:
    name: str
    email: str
    password: str = field(repr=False)
    server: str
    port: int
    rotation_weight: int = 50


@dataclass(frozen=True)
class LegacyCampaign:
    name: str
    template: str
    subject: str
    from_name: str
    reply_to: str = ""
    max_sends_per_day: int = 9_950
    target_duration_hours: float = 21.0
    thread_count: int = 1


@dataclass(frozen=True)
class LegacyPaths:
    base_dir: Path
    target_file: Path | None
    fallback_file: Path
    sent_file: Path
    failed_file: Path
    bounce_file: Path
    event_log_file: Path
    log_file: Path


@dataclass
class LegacyMigrationReport:
    translated: list[str] = field(default_factory=list)
    deprecated: list[str] = field(default_factory=list)
    manual_review: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    defaults_changed: list[str] = field(default_factory=list)
    validation_failures: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, list[str]]:
        return asdict(self)


@dataclass(frozen=True)
class LegacyConfiguration:
    source_path: Path
    campaigns: tuple[LegacyCampaign, ...]
    smtp_accounts: tuple[LegacySmtpAccount, ...]
    paths: LegacyPaths
    policies: dict[str, dict[str, Any]]
    migration: LegacyMigrationReport

    def to_dict(self) -> dict[str, object]:
        return {
            "source_path": str(self.source_path),
            "campaigns": [asdict(item) for item in self.campaigns],
            "smtp_accounts": [
                {
                    "name": item.name,
                    "email": item.email,
                    "server": item.server,
                    "port": item.port,
                    "rotation_weight": item.rotation_weight,
                }
                for item in self.smtp_accounts
            ],
            "paths": {key: str(value) for key, value in asdict(self.paths).items()},
            "policies": _redact_mapping(self.policies),
            "migration": self.migration.to_dict(),
        }


def _redact_mapping(value: object) -> object:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if _SECRET_KEY.search(str(key)) else _redact_mapping(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_redact_mapping(item) for item in value]
    return value


@dataclass
class LegacyTargetReport:
    imported: int = 0
    unique: int = 0
    rejected: list[dict[str, object]] = field(default_factory=list)
    duplicates: int = 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class LegacyStateMigrationReport:
    counts: dict[str, int] = field(
        default_factory=lambda: {
            "sent_ambiguous": 0,
            "failed_deferred": 0,
            "hard_bounces": 0,
            "events_imported": 0,
        }
    )
    rejected: list[dict[str, object]] = field(default_factory=list)
    duplicates: int = 0
    conflicts: int = 0
    already_present: int = 0
    logs_preserved: bool = False

    @property
    def ambiguous(self) -> int:
        return self.counts["sent_ambiguous"]

    def to_dict(self) -> dict[str, object]:
        return asdict(self) | {"ambiguous": self.ambiguous}


class LegacyConfigTranslator:
    """Translate YAML without adopting it as a second runtime authority."""

    def __init__(self, *, environment: Mapping[str, str] | None = None):
        self.environment = dict(os.environ if environment is None else environment)

    def translate(
        self,
        path: str | Path,
        *,
        base_dir: str | Path | None = None,
    ) -> LegacyConfiguration:
        source = Path(path).resolve()
        with source.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
        if not isinstance(raw, dict):
            raise ValueError("legacy MAILER YAML root must be a mapping")
        report = LegacyMigrationReport()
        unknown = sorted(set(raw) - _KNOWN_SECTIONS)
        report.unsupported.extend(f"unknown top-level section: {name}" for name in unknown)
        root = Path(base_dir).resolve() if base_dir is not None else source.parent
        campaigns = tuple(self._campaigns(raw.get("campaigns", []), report))
        accounts = tuple(self._smtp_accounts(raw.get("smtp_pool", []), report))
        paths = self._paths(raw, root, report)
        policies = self._policies(raw, report)
        report.manual_review.append("SMTP credentials remain caller-owned runtime inputs")
        report.manual_review.append(
            "Provider authentication and deliverability require qualification"
        )
        report.manual_review.append("Legacy tracking is limited to deterministic event identity")
        return LegacyConfiguration(source, campaigns, accounts, paths, policies, report)

    def _resolve(self, value: object, field_name: str, report: LegacyMigrationReport) -> str:
        if not isinstance(value, str):
            return str(value)

        def replace(match: re.Match[str]) -> str:
            name = match.group("braced") or match.group("plain")
            resolved = self.environment.get(name)
            if resolved is None:
                report.validation_failures.append(
                    f"{field_name}: environment variable {name} is unset"
                )
                return match.group(0)
            return resolved

        return _ENV_REFERENCE.sub(replace, value)

    @staticmethod
    def _mapping_list(
        value: object, field_name: str, report: LegacyMigrationReport
    ) -> list[dict[str, Any]]:
        if value is None:
            return []
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            report.validation_failures.append(f"{field_name}: expected a list of mappings")
            return []
        return value

    def _campaigns(self, value: object, report: LegacyMigrationReport) -> list[LegacyCampaign]:
        result: list[LegacyCampaign] = []
        for index, item in enumerate(self._mapping_list(value, "campaigns", report), 1):
            try:
                campaign = LegacyCampaign(
                    name=str(item["name"]),
                    template=str(item["template"]),
                    subject=str(item.get("subject", "")),
                    from_name=str(item.get("from_name", "")),
                    reply_to=str(item.get("reply_to", "")),
                    max_sends_per_day=int(item.get("max_sends_per_day", 9_950)),
                    target_duration_hours=float(item.get("target_duration_hours", 21.0)),
                    thread_count=int(item.get("thread_count", 1)),
                )
            except (KeyError, TypeError, ValueError) as error:
                report.validation_failures.append(f"campaigns[{index}]: {error}")
                continue
            if campaign.max_sends_per_day < 1 or campaign.thread_count < 1:
                report.validation_failures.append(f"campaigns[{index}]: limits must be positive")
                continue
            report.translated.append(f"campaigns.{campaign.name}")
            result.append(campaign)
        return result

    def _smtp_accounts(
        self, value: object, report: LegacyMigrationReport
    ) -> list[LegacySmtpAccount]:
        result: list[LegacySmtpAccount] = []
        for index, item in enumerate(self._mapping_list(value, "smtp_pool", report), 1):
            try:
                account = LegacySmtpAccount(
                    name=str(item["name"]),
                    email=self._resolve(item["email"], f"smtp_pool[{index}].email", report),
                    password=self._resolve(
                        item["password"], f"smtp_pool[{index}].password", report
                    ),
                    server=self._resolve(item["server"], f"smtp_pool[{index}].server", report),
                    port=int(self._resolve(item["port"], f"smtp_pool[{index}].port", report)),
                    rotation_weight=int(item.get("rotation_weight", 50)),
                )
            except (KeyError, TypeError, ValueError) as error:
                report.validation_failures.append(f"smtp_pool[{index}]: {error}")
                continue
            if not 1 <= account.port <= 65_535 or account.rotation_weight < 0:
                report.validation_failures.append(
                    f"smtp_pool[{index}]: invalid port or rotation weight"
                )
                continue
            report.translated.append(f"smtp_pool.{account.name}")
            result.append(account)
        return result

    def _paths(self, raw: dict[str, Any], root: Path, report: LegacyMigrationReport) -> LegacyPaths:
        targets = raw.get("targets") if isinstance(raw.get("targets"), dict) else {}
        files = raw.get("files") if isinstance(raw.get("files"), dict) else {}
        tracking = raw.get("tracking") if isinstance(raw.get("tracking"), dict) else {}
        logging_config = raw.get("logging") if isinstance(raw.get("logging"), dict) else {}

        def resolve(value: object, default: str) -> Path:
            candidate = Path(str(value or default))
            return candidate if candidate.is_absolute() else root / candidate

        report.translated.extend(
            ("targets", "files", "tracking.event_log_file", "logging.log_file")
        )
        report.defaults_changed.append("relative paths are resolved from the YAML directory")
        target_value = targets.get("file")
        return LegacyPaths(
            base_dir=root,
            target_file=resolve(target_value, "targets.csv") if target_value else None,
            fallback_file=resolve(targets.get("fallback_file"), "all_mails.txt"),
            sent_file=resolve(files.get("sent_file"), "sent.txt"),
            failed_file=resolve(files.get("failed_file"), "failed.txt"),
            bounce_file=resolve(files.get("bounce_file"), "hard_bounces.txt"),
            event_log_file=resolve(tracking.get("event_log_file"), "events.jsonl"),
            log_file=resolve(logging_config.get("log_file"), "email_campaign.log"),
        )

    def _policies(
        self, raw: dict[str, Any], report: LegacyMigrationReport
    ) -> dict[str, dict[str, Any]]:
        policies: dict[str, dict[str, Any]] = {}
        for name in (
            "rate_limiting",
            "domain_throttle",
            "adaptive_domain_throttle",
            "sending_window",
            "smtp_reputation",
            "health_check",
            "advanced",
            "tracking",
        ):
            value = raw.get(name, {})
            policies[name] = dict(value) if isinstance(value, dict) else {}
            report.translated.append(name)
            advanced_unsupported = set(policies[name]) - {
                "warmup_enabled",
                "warmup_ramp_days",
            }
            if name == "advanced" and advanced_unsupported:
                report.unsupported.append(
                    f"{name}: retained for review; no equivalent provider/runtime "
                    "behavior is enabled"
                )
        tracking = policies["tracking"]
        for deprecated in ("open_tracking", "click_tracking", "tracking_pixel"):
            if deprecated in tracking:
                report.deprecated.append(
                    f"tracking.{deprecated}: not carried into canonical local state"
                )
        return policies


def load_legacy_configuration(
    path: str | Path, *, environment: Mapping[str, str] | None = None
) -> LegacyConfiguration:
    return LegacyConfigTranslator(environment=environment).translate(path)


def load_legacy_targets(
    configuration: LegacyConfiguration, *, max_bytes: int = 16 * 1024 * 1024
) -> tuple[list[ContactCandidate], LegacyTargetReport]:
    path = configuration.paths.target_file
    if path is None or not path.is_file():
        path = configuration.paths.fallback_file
    if not path.is_file():
        raise FileNotFoundError(f"legacy target file does not exist: {path}")
    if path.stat().st_size > max_bytes:
        raise ValueError(f"legacy target file exceeds {max_bytes} bytes")
    report = LegacyTargetReport()
    candidates: list[ContactCandidate] = []
    seen: set[str] = set()
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            records: Iterable[tuple[int, dict[str, str]]] = enumerate(csv.DictReader(handle), 2)
            for position, row in records:
                raw_email = row.get("email", "").strip()
                metadata = {key: value for key, value in row.items() if key != "email" and value}
                _append_target(candidates, report, seen, path, position, raw_email, metadata)
    else:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for position, line in enumerate(handle, 1):
                raw_email, _, comment = line.partition("#")
                _append_target(
                    candidates,
                    report,
                    seen,
                    path,
                    position,
                    raw_email.strip(),
                    {"legacy_comment": comment.strip()} if comment.strip() else {},
                )
    report.unique = len(candidates)
    return candidates, report


def _append_target(
    candidates: list[ContactCandidate],
    report: LegacyTargetReport,
    seen: set[str],
    path: Path,
    position: int,
    raw_email: str,
    metadata: dict[str, str],
) -> None:
    report.imported += 1 if raw_email else 0
    normalized = normalize_email(raw_email)
    if not raw_email or not validate_email(normalized).valid:
        report.rejected.append(
            {
                "source_file": str(path),
                "source_record_position": position,
                "reason": "invalid_email",
            }
        )
        return
    if normalized in seen:
        report.duplicates += 1
        return
    seen.add(normalized)
    candidates.append(ContactCandidate(normalized, raw_email, str(path), position, metadata))


def _legacy_records(
    path: Path, report: LegacyStateMigrationReport
) -> Iterable[tuple[int, str, str]]:
    if not path.is_file():
        return
    for position, line in enumerate(
        path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
    ):
        raw_email, _, reason = line.partition("#")
        email = normalize_email(raw_email.strip())
        if not validate_email(email).valid:
            report.rejected.append(
                {
                    "source_file": str(path),
                    "source_record_position": position,
                    "reason": "invalid_email",
                }
            )
            continue
        yield position, email, reason.strip()


def migrate_legacy_state(
    store: CampaignStore,
    *,
    list_id: str,
    campaign_id: str,
    sent_path: str | Path,
    failed_path: str | Path,
    hard_bounces_path: str | Path,
    events_path: str | Path | None = None,
    log_path: str | Path | None = None,
) -> LegacyStateMigrationReport:
    """Import synthetic legacy records without requeueing uncertain attempts."""

    report = LegacyStateMigrationReport()
    sources = (
        (Path(sent_path), AttemptOutcome.LEGACY_RECORDED, "sent_ambiguous"),
        (Path(failed_path), AttemptOutcome.TRANSIENT_FAILURE, "failed_deferred"),
        (Path(hard_bounces_path), AttemptOutcome.PERMANENT_FAILURE, "hard_bounces"),
    )
    grouped: dict[str, list[tuple[Path, AttemptOutcome, str, int, str]]] = {}
    for path, outcome, count_key in sources:
        for position, email, reason in _legacy_records(path, report):
            grouped.setdefault(email, []).append((path, outcome, count_key, position, reason))
    priority = {
        AttemptOutcome.PERMANENT_FAILURE: 3,
        AttemptOutcome.LEGACY_RECORDED: 2,
        AttemptOutcome.TRANSIENT_FAILURE: 1,
    }
    for email, records in grouped.items():
        if len(records) > 1:
            report.duplicates += len(records) - 1
            if len({record[1] for record in records}) > 1:
                report.conflicts += 1
        path, outcome, count_key, position, reason = max(
            records, key=lambda record: priority[record[1]]
        )
        contact_id = store.upsert_contact(
            email,
            source_file=str(path),
            source_record_position=position,
            original_input=email,
            metadata={"legacy_source": path.name, "legacy_reason": reason},
        )
        store.add_to_list(list_id, contact_id)
        intent_id = store.prepare_intent(
            campaign_id=campaign_id,
            contact_id=contact_id,
            normalized_email=email,
            state={
                AttemptOutcome.LEGACY_RECORDED: DeliveryState.AMBIGUOUS,
                AttemptOutcome.TRANSIENT_FAILURE: DeliveryState.DEFERRED,
                AttemptOutcome.PERMANENT_FAILURE: DeliveryState.PERMANENT_FAILURE,
            }[outcome],
            eligibility_reason=f"legacy_{count_key}",
        )
        imported = store.import_legacy_attempt(
            intent_id=intent_id,
            outcome=outcome,
            reason=reason or f"legacy {count_key} record requires reconciliation",
        )
        report.counts[count_key] += 1
        if not imported:
            report.already_present += 1
        if outcome is AttemptOutcome.PERMANENT_FAILURE:
            store.add_suppression(
                category=SuppressionCategory.HARD_BOUNCE,
                reason="legacy hard bounce record",
                normalized_email=email,
                contact_id=contact_id,
                scope_type="global",
                scope_id=None,
            )
    report.logs_preserved = log_path is None or Path(log_path).exists()
    if events_path is not None:
        report.counts["events_imported"] = _import_legacy_events(store, Path(events_path), report)
    return report


def _import_legacy_events(
    store: CampaignStore, path: Path, report: LegacyStateMigrationReport
) -> int:
    if not path.is_file():
        return 0
    raw = path.read_text(encoding="utf-8", errors="replace")
    try:
        parsed = json.loads(raw)
        raw_records = parsed if isinstance(parsed, list) else [parsed]
        records = list(enumerate(raw_records, 1))
    except json.JSONDecodeError:
        records = []
        for position, line in enumerate(raw.splitlines(), 1):
            if not line.strip():
                continue
            try:
                records.append((position, json.loads(line)))
            except json.JSONDecodeError:
                report.rejected.append(
                    {
                        "source_file": str(path),
                        "source_record_position": position,
                        "reason": "malformed_event_json",
                    }
                )
    imported = 0
    for position, record in records:
        if not isinstance(record, dict):
            report.rejected.append(
                {
                    "source_file": str(path),
                    "source_record_position": position,
                    "reason": "event_not_mapping",
                }
            )
            continue
        event_type = str(record.get("event_type", record.get("type", "legacy_event")))
        payload = {
            "source_file": str(path),
            "source_record_position": position,
            "legacy": True,
            "event_type": event_type,
            "legacy_record": _redact_mapping(record),
        }
        with store.transaction(immediate=True) as connection:
            encoded = json.dumps(payload, sort_keys=True)
            exists = connection.execute(
                "SELECT 1 FROM operational_events WHERE aggregate_type = ? AND aggregate_id = ? "
                "AND event_type = ? AND payload_json = ?",
                ("legacy", str(path), "legacy_event_imported", encoded),
            ).fetchone()
            if exists is None:
                store.record_event(
                    connection, "legacy", str(path), "legacy_event_imported", payload
                )
                imported += 1
    return imported


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        return " ".join(" ".join(self.parts).split())


def _html_text(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(value)
    return parser.text()


def prepare_legacy_campaign(
    store: CampaignStore,
    configuration: LegacyConfiguration,
    *,
    campaign_name: str,
    sender_email: str | None = None,
    attachments: tuple[AttachmentSpec, ...] = (),
    asset_root: str | Path | None = None,
) -> tuple[str, LegacyTargetReport]:
    """Prepare one translated legacy campaign through canonical services."""

    legacy = next((item for item in configuration.campaigns if item.name == campaign_name), None)
    if legacy is None:
        raise KeyError(f"legacy campaign not found: {campaign_name}")
    candidates, target_report = load_legacy_targets(configuration)
    service = CampaignService(store)
    list_id = store.create_list(f"legacy-{legacy.name}", "Translated MAILER audience")
    service.import_candidates(candidates, list_id)
    template_path = configuration.paths.base_dir / legacy.template
    html = template_path.read_text(encoding="utf-8")
    template = TemplateDefinition(
        name=f"legacy-{legacy.name}",
        version="1",
        subject=legacy.subject,
        text=_html_text(html),
        html=html,
        reply_to=legacy.reply_to or None,
    )
    account = configuration.smtp_accounts[0] if configuration.smtp_accounts else None
    email = sender_email or (account.email if account else "sender@example.invalid")
    domain_policy = configuration.policies["domain_throttle"]
    per_domain_limit = domain_policy.get("max_per_domain_per_minute", 10)
    per_domain_hour_limit = int(domain_policy.get("max_per_domain_per_hour", 0))
    sender = SenderPolicy(
        ref=f"legacy-{legacy.name}-sender",
        provider=account.name if account else "legacy-synthetic",
        email=email,
        display_name=legacy.from_name,
        reply_to=legacy.reply_to or None,
        max_per_minute=int(configuration.policies["rate_limiting"].get("max_per_minute", 60)),
        max_per_hour=int(configuration.policies["rate_limiting"].get("max_per_hour", 3_600)),
        max_per_day=legacy.max_sends_per_day,
        per_domain_per_minute=int(per_domain_limit if per_domain_limit is not None else 10),
        per_domain_per_hour=per_domain_hour_limit,
        max_concurrency=legacy.thread_count,
    )
    campaign_id = service.create_campaign(
        legacy.name,
        list_id,
        template_ref=template.name,
        sender_config_ref=sender.ref,
        configuration={
            "legacy_config": str(configuration.source_path),
            "legacy_template": legacy.template,
            "smtp_account": account.name if account else "legacy-synthetic",
        },
    )
    schedule_config = configuration.policies["sending_window"]
    schedule = ScheduleSpec(
        mode=ScheduleMode.WINDOW
        if schedule_config.get("enabled", False)
        else ScheduleMode.IMMEDIATE,
        timezone=str(schedule_config.get("timezone", "UTC")),
        window_start=(
            f"{int(schedule_config.get('start_hour', 9)):02d}:00"
            if schedule_config.get("enabled", False)
            else None
        ),
        window_end=(
            f"{int(schedule_config.get('end_hour', 18)):02d}:00"
            if schedule_config.get("enabled", False)
            else None
        ),
        pacing_seconds=max(0.0, legacy.target_duration_hours * 3600 / legacy.max_sends_per_day),
        catch_up_policy=CatchUpPolicy.NEXT_WINDOW,
    )
    OutboundService(store).prepare_campaign(
        campaign_id,
        template=template,
        sender=sender,
        schedule=schedule,
        attachments=attachments,
        asset_root=asset_root,
        sender_configuration={
            "adaptive_domain_throttle": configuration.policies["adaptive_domain_throttle"],
            "smtp_reputation": configuration.policies["smtp_reputation"],
            "warmup": {
                "enabled": bool(configuration.policies["advanced"].get("warmup_enabled", False)),
                "ramp_days": int(configuration.policies["advanced"].get("warmup_ramp_days", 7)),
                "started_at": datetime.now(UTC).date().isoformat(),
            },
        },
    )
    return campaign_id, target_report
