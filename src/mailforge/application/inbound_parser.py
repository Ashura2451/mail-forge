"""Bounded, non-executing RFC 5322/MIME inspection for inbound evidence."""

from __future__ import annotations

import base64
import hashlib
import time
from collections import defaultdict
from email import policy
from email.header import decode_header, make_header
from email.parser import BytesParser
from email.utils import getaddresses
from html.parser import HTMLParser
from typing import Any

from mailforge.domain.inbound import (
    ComplaintReport,
    DsnAction,
    DsnRecipient,
    DsnReport,
    InboundLimits,
    ParsedInbound,
    ParseIssue,
    ParseStatus,
    SourceMessage,
)


class _PreviewParser(HTMLParser):
    def __init__(self, limit: int):
        super().__init__(convert_charrefs=True)
        self.limit = limit
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript", "template"}:
            self.hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "template"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data: str) -> None:
        if not self.hidden and sum(map(len, self.parts)) < self.limit:
            self.parts.append(data)

    def text(self) -> str:
        return " ".join(" ".join(self.parts).split())[: self.limit]


def _decode(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return str(make_header(decode_header(value)))
    except LookupError, UnicodeError, ValueError:
        return value


def _field_map(message: Any, max_chars: int | None = None) -> dict[str, str]:
    fields: dict[str, str] = {}
    for name, value in message.items():
        decoded = _decode(str(value)) or ""
        fields[name.lower()] = decoded[:max_chars] if max_chars else decoded
    return fields


def _bounded_parts(message: Any, limits: InboundLimits):
    pending = [(message, 0)]
    seen = 0
    while pending and seen < limits.max_part_count:
        part, depth = pending.pop()
        seen += 1
        if depth > limits.max_mime_depth:
            continue
        yield part
        if part.is_multipart() and (
            part.get_content_disposition() != "attachment"
            or part.get_content_type() in {"message/feedback-report", "message/delivery-status"}
        ):
            for child in part.iter_parts():
                if len(pending) + seen >= limits.max_part_count:
                    break
                pending.append((child, depth + 1))


def _embedded_field_map(
    message: Any, limits: InboundLimits, issues: list[ParseIssue]
) -> dict[str, str]:
    fields = _field_map(message, limits.max_preview_chars)
    if any(key in fields for key in ("feedback-type", "original-message-id", "original-rcpt-to")):
        return fields
    decoded_total = 0
    for part in _bounded_parts(message, limits):
        try:
            payload = part.get_payload(decode=True)
        except LookupError, UnicodeError, ValueError, TypeError:
            payload = None
        if not isinstance(payload, bytes):
            continue
        decoded_total += len(payload)
        if decoded_total > limits.max_decoded_payload_bytes:
            issues.append(
                ParseIssue("decoded_payload_limit", "report payload exceeds configured limit")
            )
            break
        decoded = payload.decode("utf-8", errors="replace")
        if ":" not in decoded:
            try:
                decoded = base64.b64decode(payload).decode("utf-8", errors="replace")
            except ValueError, UnicodeError:
                pass
        for line in decoded.splitlines():
            if ":" in line:
                name, value = line.split(":", 1)
                fields[name.strip().lower()] = value.strip()[: limits.max_preview_chars]
    return fields


def _part_text(part: Any, limits: InboundLimits, issues: list[ParseIssue]) -> tuple[str, str]:
    try:
        payload = part.get_payload(decode=True)
    except (LookupError, UnicodeError, ValueError, TypeError) as error:
        issues.append(ParseIssue("payload_decode", type(error).__name__))
        return "", ""
    if not isinstance(payload, bytes):
        return "", ""
    if len(payload) > limits.max_decoded_payload_bytes:
        issues.append(ParseIssue("decoded_payload_limit", "payload exceeds configured limit"))
        return "", ""
    charset = part.get_content_charset() or "utf-8"
    try:
        text = payload.decode(charset, errors="replace")
    except LookupError:
        issues.append(ParseIssue("unknown_charset", charset))
        text = payload.decode("utf-8", errors="replace")
    if part.get_content_type() == "text/html":
        html_parser = _PreviewParser(limits.max_preview_chars)
        try:
            html_parser.feed(text)
            html_parser.close()
        except Exception as error:  # HTMLParser is intentionally best-effort.
            issues.append(ParseIssue("html_preview", type(error).__name__))
        return "", html_parser.text()
    return text[: limits.max_preview_chars], ""


def _parse_status(value: str | None) -> DsnAction:
    if value:
        first = value.lower().split(".", 1)[0]
        if first == "2":
            return DsnAction.DELIVERED
        if first == "4":
            return DsnAction.DELAYED
        if first == "5":
            return DsnAction.FAILED
    return DsnAction.UNKNOWN


def _recipient_value(value: str | None) -> str | None:
    if not value:
        return None
    return value.split(";", 1)[-1].strip()


def _dsn(message: Any, issues: list[ParseIssue], limits: InboundLimits) -> DsnReport | None:
    report_part = None
    if message.get_content_type() == "multipart/report":
        if (message.get_param("report-type") or "").lower() == "delivery-status":
            report_part = next(
                (
                    part
                    for part in _bounded_parts(message, limits)
                    if part.get_content_type() == "message/delivery-status"
                ),
                None,
            )
    elif message.get_content_type() == "message/delivery-status":
        report_part = message
    else:
        report_part = next(
            (
                part
                for part in _bounded_parts(message, limits)
                if part.get_content_type() == "message/delivery-status"
            ),
            None,
        )
    if report_part is None:
        return None
    payload = report_part.get_payload()
    blocks = payload if isinstance(payload, list) else [report_part]
    maps = [
        _field_map(block, limits.max_preview_chars)
        for block in blocks[: limits.max_part_count]
        if hasattr(block, "items")
    ]
    if not maps:
        issues.append(ParseIssue("dsn_empty", "delivery-status report has no field blocks"))
        return DsnReport(None, None, None, ())
    per_message = maps[0]
    recipients: list[DsnRecipient] = []
    for fields in maps[1:]:
        action = fields.get("action", "").lower()
        try:
            action_value = DsnAction(action)
        except ValueError:
            action_value = _parse_status(fields.get("status"))
        recipients.append(
            DsnRecipient(
                original_recipient=_recipient_value(fields.get("original-recipient")),
                final_recipient=_recipient_value(fields.get("final-recipient")),
                action=action_value,
                status=fields.get("status"),
                diagnostic_code=fields.get("diagnostic-code"),
                last_attempt_date=fields.get("last-attempt-date"),
                retry_until=fields.get("retry-until"),
                fields=fields,
            )
        )
    return DsnReport(
        reporting_mta=per_message.get("reporting-mta"),
        original_envelope_id=per_message.get("original-envelope-id"),
        arrival_date=per_message.get("arrival-date"),
        recipients=tuple(recipients),
        fields=per_message,
    )


def _complaint(
    message: Any, limits: InboundLimits, issues: list[ParseIssue]
) -> ComplaintReport | None:
    report_part = None
    if message.get_content_type() == "multipart/report":
        if (message.get_param("report-type") or "").lower() == "feedback-report":
            report_part = next(
                (
                    part
                    for part in _bounded_parts(message, limits)
                    if part.get_content_type() == "message/feedback-report"
                ),
                None,
            )
    elif message.get_content_type() == "message/feedback-report":
        report_part = message
    else:
        report_part = next(
            (
                part
                for part in _bounded_parts(message, limits)
                if part.get_content_type() == "message/feedback-report"
            ),
            None,
        )
    if report_part is None:
        return None
    fields = _embedded_field_map(report_part, limits, issues)
    original_id = fields.get("original-message-id")
    recipient = _recipient_value(fields.get("original-rcpt-to") or fields.get("original-recipient"))
    return ComplaintReport(
        feedback_type=fields.get("feedback-type"),
        original_message_id=original_id,
        original_recipient=recipient,
        source=fields.get("source-ip") or fields.get("reported-domain"),
        abuse_indicator=bool(fields.get("feedback-type")),
        fields=fields,
    )


class SafeInboundParser:
    """Parse metadata and bounded previews; never opens or executes attachments."""

    def __init__(self, limits: InboundLimits | None = None):
        self.limits = limits or InboundLimits()

    def parse(self, source: SourceMessage) -> ParsedInbound:
        started = time.perf_counter()
        raw = source.raw_bytes
        content_hash = hashlib.sha256(raw).hexdigest()
        issues: list[ParseIssue] = []
        if len(raw) > self.limits.max_message_bytes:
            return ParsedInbound(
                ParseStatus.QUARANTINED,
                content_hash,
                len(raw),
                {},
                None,
                None,
                (),
                None,
                (),
                None,
                "",
                "",
                (ParseIssue("message_size_limit", "message exceeds configured limit"),),
            )
        header_bytes = raw.split(b"\r\n\r\n", 1)[0].split(b"\n\n", 1)[0]
        header_issues = []
        if len(header_bytes) > self.limits.max_header_bytes:
            header_issues.append(ParseIssue("header_size_limit", "headers exceed configured limit"))
        if header_bytes.count(b"\n") > self.limits.max_header_count:
            header_issues.append(
                ParseIssue("header_count_limit", "headers exceed configured count")
            )
        if header_issues:
            return ParsedInbound(
                ParseStatus.QUARANTINED,
                content_hash,
                len(raw),
                {},
                None,
                None,
                (),
                None,
                (),
                None,
                "",
                "",
                tuple(header_issues),
            )
        try:
            message = BytesParser(policy=policy.default).parsebytes(raw)
        except Exception as error:
            return ParsedInbound(
                ParseStatus.MALFORMED,
                content_hash,
                len(raw),
                {},
                None,
                None,
                (),
                None,
                (),
                None,
                "",
                "",
                (ParseIssue("parse_error", type(error).__name__),),
            )
        parts = 0
        decoded_total = 0
        text_parts: list[str] = []
        html_parts: list[str] = []
        pending = [(message, 0)]
        while pending:
            if time.perf_counter() - started > self.limits.max_processing_seconds:
                issues.append(ParseIssue("processing_time_limit", "parser time budget exceeded"))
                break
            part, depth = pending.pop()
            if depth > self.limits.max_mime_depth:
                issues.append(
                    ParseIssue("mime_depth_limit", "MIME nesting exceeds configured limit")
                )
                continue
            parts += 1
            if parts > self.limits.max_part_count:
                issues.append(
                    ParseIssue("part_count_limit", "MIME part count exceeds configured limit")
                )
                break
            if part.is_multipart() and part.get_content_disposition() != "attachment":
                pending.extend((child, depth + 1) for child in reversed(tuple(part.iter_parts())))
            if part.is_multipart() or part.get_content_disposition() == "attachment":
                continue
            if part.get_content_type() == "message/rfc822":
                continue
            try:
                bounded_payload = part.get_payload(decode=True)
            except LookupError, UnicodeError, ValueError, TypeError:
                bounded_payload = None
            if isinstance(bounded_payload, bytes):
                decoded_total += len(bounded_payload)
                if decoded_total > self.limits.max_decoded_payload_bytes:
                    issues.append(
                        ParseIssue(
                            "decoded_payload_limit",
                            "decoded payload total exceeds configured limit",
                        )
                    )
                    break
            text, html = _part_text(part, self.limits, issues)
            if text:
                text_parts.append(text)
            if html:
                html_parts.append(html)
        for defect in getattr(message, "defects", ()):
            issues.append(ParseIssue("message_defect", type(defect).__name__))
        headers: dict[str, tuple[str, ...]] = defaultdict(tuple)
        grouped: dict[str, list[str]] = defaultdict(list)
        for name, value in message.raw_items():
            grouped[name.lower()].append(_decode(value) or "")
        headers = {name: tuple(values) for name, values in grouped.items()}
        addresses = getaddresses(
            [value for name in ("from", "to", "cc", "reply-to") for value in headers.get(name, ())]
        )
        sender = next(
            (address for _, address in getaddresses(headers.get("from", ())) if address), None
        )
        recipients = tuple(address for _, address in addresses if address and address != sender)
        dsn = _dsn(message, issues, self.limits)
        complaint = _complaint(message, self.limits, issues)
        status = ParseStatus.PARTIALLY_PARSED if issues else ParseStatus.PARSED
        return ParsedInbound(
            status=status,
            content_hash=content_hash,
            size_bytes=len(raw),
            headers=headers,
            message_id=next(iter(headers.get("message-id", ())), None),
            in_reply_to=next(iter(headers.get("in-reply-to", ())), None),
            references=tuple(" ".join(headers.get("references", ())).split()),
            sender=sender,
            recipients=recipients,
            subject=next(iter(headers.get("subject", ())), None),
            text_preview="\n".join(text_parts)[: self.limits.max_preview_chars],
            html_preview="\n".join(html_parts)[: self.limits.max_preview_chars],
            issues=tuple(issues),
            dsn=dsn,
            complaint=complaint,
        )
