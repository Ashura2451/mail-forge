"""Safe MIME composition for prepared outbound messages."""

from __future__ import annotations

import hashlib
import mimetypes
from dataclasses import dataclass
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formataddr, make_msgid
from pathlib import Path

from mailforge.application.templates import RenderedTemplate
from mailforge.domain.email import validate_email
from mailforge.domain.outbound import AttachmentSpec, SenderPolicy


class MessagePreparationError(ValueError):
    """Raised when a message cannot be composed safely."""


class UnsafeAssetError(MessagePreparationError):
    """Raised when an attachment is outside the configured asset root."""


@dataclass(frozen=True)
class ResolvedAttachment:
    spec: AttachmentSpec
    data: bytes
    content_type: str


@dataclass(frozen=True)
class ComposedMessage:
    message_id: str
    serialized: bytes
    content_hash: str
    subject: str
    from_email: str
    recipient: str
    attachments: tuple[ResolvedAttachment, ...]


class AssetResolver:
    def __init__(self, root: str | Path, *, max_bytes: int = 10 * 1024 * 1024):
        self.root = Path(root).resolve()
        self.max_bytes = max_bytes

    def resolve(self, spec: AttachmentSpec) -> ResolvedAttachment:
        candidate = (self.root / spec.path).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as error:
            raise UnsafeAssetError("attachment path escapes the asset root") from error
        if not candidate.is_file():
            raise FileNotFoundError(f"attachment not found: {spec.path}")
        size = candidate.stat().st_size
        if size > self.max_bytes:
            raise MessagePreparationError("attachment exceeds size limit")
        content_type = spec.content_type or mimetypes.guess_type(candidate.name)[0]
        if not content_type or "/" not in content_type:
            raise MessagePreparationError(f"cannot determine content type: {candidate.name}")
        return ResolvedAttachment(spec, candidate.read_bytes(), content_type)


def _header_text(value: str, field: str) -> str:
    if "\r" in value or "\n" in value:
        raise MessagePreparationError(f"header injection rejected for {field}")
    return value


def _address(value: str, field: str) -> str:
    value = _header_text(value.strip(), field)
    if not validate_email(value).valid:
        raise MessagePreparationError(f"invalid {field} address")
    return value


class MessageComposer:
    """Builds parseable MIME messages without using SMTP or network I/O."""

    def compose(
        self,
        *,
        sender: SenderPolicy,
        recipient: str,
        rendered: RenderedTemplate,
        attachments: tuple[ResolvedAttachment, ...] = (),
        unsubscribe_url: str | None = None,
        one_click_unsubscribe: bool = False,
        dkim_signed: bool = False,
        message_id_domain: str = "mailforge.invalid",
    ) -> ComposedMessage:
        recipient = _address(recipient, "recipient")
        sender_email = _address(sender.email, "sender")
        reply_to = _address(sender.reply_to, "reply-to") if sender.reply_to else None
        if one_click_unsubscribe:
            if not unsubscribe_url or not unsubscribe_url.startswith("https://"):
                raise MessagePreparationError("one-click unsubscribe requires an HTTPS URL")
            if not dkim_signed:
                raise MessagePreparationError(
                    "one-click unsubscribe requires an explicitly DKIM-signed message"
                )
        message = EmailMessage(policy=SMTP)
        message_id = make_msgid(domain=message_id_domain)
        message["Message-ID"] = message_id
        message["From"] = formataddr((_header_text(sender.display_name, "from name"), sender_email))
        message["To"] = recipient
        message["Subject"] = _header_text(rendered.subject, "subject")
        if reply_to:
            message["Reply-To"] = reply_to
        if unsubscribe_url:
            unsubscribe_url = _header_text(unsubscribe_url, "List-Unsubscribe")
            message["List-Unsubscribe"] = f"<{unsubscribe_url}>"
            if one_click_unsubscribe:
                message["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
        message.set_content(rendered.text)
        html_part = None
        if rendered.html is not None:
            message.add_alternative(rendered.html, subtype="html")
            html_part = message.get_payload()[-1]
        for attachment in attachments:
            maintype, subtype = attachment.content_type.split("/", 1)
            filename = _header_text(
                attachment.spec.filename or Path(attachment.spec.path).name, "filename"
            )
            if attachment.spec.content_id:
                if html_part is None:
                    raise MessagePreparationError("inline attachments require an HTML body")
                cid = _header_text(attachment.spec.content_id.strip("<>"), "content-id")
                html_part.add_related(
                    attachment.data,
                    maintype=maintype,
                    subtype=subtype,
                    cid=f"<{cid}>",
                    filename=filename,
                )
            else:
                message.add_attachment(
                    attachment.data,
                    maintype=maintype,
                    subtype=subtype,
                    filename=filename,
                )
        serialized = message.as_bytes(policy=SMTP)
        return ComposedMessage(
            message_id=message_id,
            serialized=serialized,
            content_hash=hashlib.sha256(serialized).hexdigest(),
            subject=rendered.subject,
            from_email=sender_email,
            recipient=recipient,
            attachments=attachments,
        )
