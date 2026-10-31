"""Durable, safe message composition for the local Message Studio."""

# SQL statements and compact email markup remain readable as fragments.
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import html
import io
import json
import re
import uuid
import zipfile
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import PurePath
from typing import Any

from mailforge.application.composition import MessageComposer, ResolvedAttachment
from mailforge.application.custom_html import (
    CustomHTMLImportError,
    analyze_html,
    local_references,
    read_zip_assets,
)
from mailforge.application.outbound_proof import (
    build_payload,
    digest_json,
    make_proof,
)
from mailforge.application.programmable_operations import (
    ProgrammableOperationsError,
    ProgrammableOperationsService,
)
from mailforge.application.segmentation import SegmentationError, SegmentationService
from mailforge.application.templates import SafeTemplateRenderer
from mailforge.domain.email import normalize_email, validate_email
from mailforge.domain.outbound import AttachmentSpec, SenderPolicy, TemplateDefinition
from mailforge.infrastructure.campaign_store import CampaignStore

MAX_DOCUMENT_BYTES = 256 * 1024
MAX_BLOCKS = 100
MAX_ASSET_BYTES = 10 * 1024 * 1024
MAX_MESSAGE_ASSET_BYTES = 25 * 1024 * 1024
MAX_FILENAME = 180
ALLOWED_BLOCKS = {
    "text",
    "heading",
    "list",
    "button",
    "image",
    "divider",
    "spacer",
    "columns",
    "conditional",
    "function",
}
ALLOWED_MEDIA = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "application/pdf",
    "text/plain",
    "text/css",
}
MERGE_FIELD = re.compile(
    r"\{\{\s*(?P<name>(?:email|name)|contact\.[a-z][a-z0-9_]{0,63})"
    r"(?:\s*\|\s*(?:capitalize|lower|trim|upper))*\s*\}\}"
)
MERGE_ANY = re.compile(r"\{\{.*?\}\}", re.S)
SAFE_URL = re.compile(r"^(?:https?://|mailto:)[^\s<>\"']+$", re.I)
UNSAFE_MARKUP = re.compile(
    r"<\s*(?:script|style|iframe|object|embed|form|svg|link|meta)\b|\bon[a-z]+\s*=", re.I
)


class MessageStudioError(ValueError):
    """A bounded, operator-facing composition error."""

    def __init__(self, message: str, code: str = "message_invalid"):
        super().__init__(message)
        self.code = code


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _file_hash(path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _row(row: Any) -> dict[str, Any]:
    result = dict(row)
    for key in ("document_json",):
        if key in result:
            result["document"] = json.loads(result[key])
    return result


def _filename(value: str) -> str:
    name = PurePath(value or "attachment").name.strip()
    if not name or name in {".", ".."} or len(name) > MAX_FILENAME:
        raise MessageStudioError("asset filename is invalid or too long", "asset_filename")
    if name != value.strip() or "\\" in value or any(char in name for char in '\x00\r\n"'):
        raise MessageStudioError("asset filename contains prohibited characters", "asset_filename")
    return name


class MessageStudioService:
    """The single composition authority shared by UI, API, CLI, and campaigns."""

    def __init__(
        self,
        store: CampaignStore,
        *,
        outbound=None,
        segmentation=None,
        data_studio=None,
        programmable: ProgrammableOperationsService | None = None,
        now_fn=None,
    ):
        self.store = store
        self.outbound = outbound
        self.segmentation = segmentation or SegmentationService(store)
        self.data_studio = data_studio
        self.programmable = programmable or ProgrammableOperationsService(store)
        self.renderer = SafeTemplateRenderer()
        self.composer = MessageComposer()
        self.now_fn = now_fn or (lambda: datetime.now(UTC))

    @staticmethod
    def blank_document(name: str = "Untitled message") -> dict[str, Any]:
        return {
            "version": 1,
            "name": name,
            "subject": "A thoughtful note from Mailforge",
            "preheader": "",
            "plain_text_override": None,
            "fallbacks": {"name": "there"},
            "blocks": [{"type": "text", "text": "Hello {{ name }},\n\nWrite your message here."}],
            "attachments": [],
        }

    def validate_document(self, document: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(document, Mapping):
            raise MessageStudioError("message document must be an object")
        try:
            candidate = json.loads(_canonical(document))
        except (TypeError, ValueError) as error:
            raise MessageStudioError("message document must contain JSON values") from error
        if len(_canonical(candidate).encode("utf-8")) > MAX_DOCUMENT_BYTES:
            raise MessageStudioError(
                "message document exceeds the 256 KiB limit", "complexity_limit"
            )
        if candidate.get("version") != 1:
            raise MessageStudioError("unsupported message document version")
        if not isinstance(candidate.get("name"), str) or not candidate["name"].strip():
            raise MessageStudioError("message name is required")
        for field in ("subject", "preheader"):
            if not isinstance(candidate.get(field, ""), str) or len(candidate[field]) > 10_000:
                raise MessageStudioError(f"{field} is invalid or too long")
            self._validate_template_text(candidate[field], field)
        if candidate.get("composition_mode", "structured") == "custom_html":
            return self._validate_custom_document(candidate)
        if candidate.get("composition_mode", "structured") != "structured":
            raise MessageStudioError("unsupported composition mode", "composition_mode")
        override = candidate.get("plain_text_override")
        if override is not None:
            if not isinstance(override, str) or len(override) > 100_000:
                raise MessageStudioError("plain text override is invalid or too long")
            self._validate_template_text(override, "plain_text_override")
        fallbacks = candidate.get("fallbacks", {})
        if not isinstance(fallbacks, Mapping) or len(fallbacks) > 100:
            raise MessageStudioError("fallbacks must be a bounded object")
        for key, value in fallbacks.items():
            if not isinstance(key, str) or not re.fullmatch(
                r"(?:name|email|contact\.[a-z][a-z0-9_]*)", key
            ):
                raise MessageStudioError(f"unsupported fallback field: {key}")
            if not isinstance(value, str) or len(value) > 512:
                raise MessageStudioError("fallback values must be short strings")
        blocks = candidate.get("blocks")
        if not isinstance(blocks, list) or not 1 <= len(blocks) <= MAX_BLOCKS:
            raise MessageStudioError("a message needs between 1 and 100 blocks")
        for block in blocks:
            self._validate_block(block, depth=0)
        attachments = candidate.get("attachments", [])
        if not isinstance(attachments, list) or len(attachments) > 50:
            raise MessageStudioError("a message may reference at most 50 attachments")
        for attachment in attachments:
            self._validate_attachment_ref(attachment)
        return candidate

    def _custom_asset_lookup(
        self, manifest: Sequence[Mapping[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        by_reference: dict[str, dict[str, Any]] = {}
        all_assets = self.list_assets()
        by_filename: dict[str, dict[str, Any]] = {}
        for item in all_assets:
            try:
                full = self.get_asset(item["id"], include_data=True)
            except MessageStudioError:
                continue
            by_filename.setdefault(item["filename"], full)
        for item in manifest:
            reference = str(item.get("reference", "")).replace("\\", "/").lstrip("/")
            if not reference:
                continue
            asset = None
            if item.get("asset_id"):
                try:
                    asset = self.get_asset(str(item["asset_id"]), include_data=True)
                except MessageStudioError:
                    asset = None
            if asset is None:
                asset = by_filename.get(PurePath(reference).name)
            if asset is not None:
                by_reference[reference] = {
                    "id": asset["id"],
                    "filename": asset["filename"],
                    "media_type": asset["media_type"],
                    "data": asset["data_blob"],
                    "kind": "stylesheet" if asset["media_type"] == "text/css" else asset["kind"],
                }
        return by_reference

    def _validate_custom_document(self, candidate: dict[str, Any]) -> dict[str, Any]:
        custom = candidate.get("custom_html")
        if not isinstance(custom, Mapping):
            raise MessageStudioError("custom HTML source is required", "html_empty")
        original = custom.get("original_source")
        if not isinstance(original, str):
            raise MessageStudioError("custom HTML source is required", "html_empty")
        manifest = custom.get("asset_manifest", [])
        if not isinstance(manifest, list) or len(manifest) > 200:
            raise MessageStudioError("custom HTML asset manifest is invalid", "asset_manifest")
        assets = self._custom_asset_lookup(manifest)
        for reference in local_references(original):
            if not any(
                str(item.get("reference", "")).replace("\\", "/").lstrip("/") == reference
                for item in manifest
            ):
                manifest.append({"reference": reference})
        assets = self._custom_asset_lookup(manifest)
        try:
            analyzed = analyze_html(original, assets)
        except CustomHTMLImportError as error:
            raise MessageStudioError(str(error), error.code) from error
        blocking = [item for item in analyzed["diagnostics"] if item["severity"] == "ERROR"]
        allowed_draft_errors = {"missing_asset"}
        if any(item["code"] not in allowed_draft_errors for item in blocking):
            first = blocking[0]
            raise MessageStudioError(first["message"], first["code"])
        candidate["composition_mode"] = "custom_html"
        plain_text_generated = custom.get("plain_text_generated") is True or not candidate.get(
            "plain_text_override"
        )
        candidate["custom_html"] = {
            "original_source": original,
            "source_hash": hashlib.sha256(original.encode("utf-8")).hexdigest(),
            "prepared_source": analyzed["prepared_source"],
            "asset_manifest": manifest,
            "diagnostics": analyzed["diagnostics"],
            "css_mode": "simple-inline-preserve-media",
            "plain_text_generated": plain_text_generated,
        }
        if plain_text_generated:
            candidate["plain_text_override"] = analyzed["plain_text"]
        return candidate

    def _validate_template_text(self, value: str, field: str) -> None:
        if UNSAFE_MARKUP.search(value) or "<" in value or ">" in value:
            raise MessageStudioError(f"unsafe markup is not allowed in {field}", "unsafe_markup")
        for match in MERGE_ANY.finditer(value):
            if not MERGE_FIELD.fullmatch(match.group(0)):
                raise MessageStudioError(
                    f"unsupported merge expression in {field}", "unsafe_template"
                )

    def _validate_block(self, block: Any, *, depth: int) -> None:
        if depth > 8 or not isinstance(block, Mapping):
            raise MessageStudioError("message block nesting is invalid", "complexity_limit")
        kind = block.get("type")
        if kind not in ALLOWED_BLOCKS:
            raise MessageStudioError(f"unsupported message block: {kind}")
        if kind in {"text", "heading"}:
            text = block.get("text")
            if not isinstance(text, str) or len(text) > 20_000:
                raise MessageStudioError(f"{kind} text is required and bounded")
            self._validate_template_text(text, kind)
        elif kind == "list":
            items = block.get("items")
            if not isinstance(items, list) or not 1 <= len(items) <= 30:
                raise MessageStudioError("list blocks need between 1 and 30 items")
            for item in items:
                if not isinstance(item, str) or len(item) > 2_000:
                    raise MessageStudioError("list item is invalid")
                self._validate_template_text(item, "list item")
        elif kind == "button":
            label, url = block.get("label"), block.get("url")
            if not isinstance(label, str) or not label.strip() or len(label) > 500:
                raise MessageStudioError("button label is required")
            self._validate_template_text(label, "button label")
            if not isinstance(url, str) or not SAFE_URL.fullmatch(url):
                raise MessageStudioError(
                    "unsafe button URL; use http, https, or mailto", "unsafe_url"
                )
        elif kind == "image":
            if not isinstance(block.get("asset_id"), str):
                raise MessageStudioError("image asset_id is required")
            alt = block.get("alt", "")
            if not isinstance(alt, str) or len(alt) > 500:
                raise MessageStudioError("image alt text is invalid")
            self._validate_template_text(alt, "image alt")
        elif kind == "columns":
            columns = block.get("columns")
            if not isinstance(columns, list) or not 1 < len(columns) <= 3:
                raise MessageStudioError("columns must contain two or three arrays")
            for column in columns:
                if not isinstance(column, list) or len(column) > MAX_BLOCKS:
                    raise MessageStudioError("column blocks are invalid")
                for child in column:
                    self._validate_block(child, depth=depth + 1)
        elif kind == "conditional":
            condition = block.get("condition")
            if not isinstance(condition, Mapping) or not condition.get("segment_id"):
                raise MessageStudioError("conditional blocks require a saved segment")
            for branch_name in ("if_true", "if_false"):
                branch = block.get(branch_name, [])
                if not isinstance(branch, list) or len(branch) > MAX_BLOCKS:
                    raise MessageStudioError("conditional branch is invalid")
                for child in branch:
                    self._validate_block(child, depth=depth + 1)
        elif kind == "function":
            if not isinstance(block.get("function_id"), str) or not block["function_id"]:
                raise MessageStudioError("function blocks require function_id", "function_invalid")
            if not isinstance(block.get("revision_id"), str) or not block["revision_id"]:
                raise MessageStudioError(
                    "function blocks require a pinned revision_id", "function_invalid"
                )
            if not isinstance(block.get("content_hash"), str) or not block["content_hash"]:
                raise MessageStudioError("function blocks require content_hash", "function_invalid")
            mapping = block.get("input_mapping", {})
            if not isinstance(mapping, Mapping) or len(mapping) > 20:
                raise MessageStudioError("function input_mapping is invalid", "function_invalid")
            for key, value in mapping.items():
                if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", str(key)) or not re.fullmatch(
                    r"(?:email|name|contact\.[a-z][a-z0-9_]{0,63})", str(value)
                ):
                    raise MessageStudioError(
                        "function input_mapping contains an unsafe field", "function_invalid"
                    )
            if not isinstance(block.get("parameters", {}), Mapping):
                raise MessageStudioError(
                    "function parameters must be an object", "function_invalid"
                )
            try:
                revision = (
                    self.programmable.get_revision(block["revision_id"])
                    if block.get("revision_id")
                    else self.programmable.get_function(block["function_id"])
                )
            except ProgrammableOperationsError as error:
                raise MessageStudioError(str(error), error.code) from error
            if (
                block.get("revision_id")
                and revision.get("function_id")
                != self.programmable.get_function(block["function_id"])["id"]
            ):
                raise MessageStudioError(
                    "function revision does not belong to function", "function_invalid"
                )
            if revision.get("content_hash") != block["content_hash"]:
                raise MessageStudioError(
                    "function revision content hash does not match", "function_invalid"
                )

    def _validate_attachment_ref(self, attachment: Any) -> None:
        if not isinstance(attachment, Mapping) or not isinstance(attachment.get("asset_id"), str):
            raise MessageStudioError("attachments require an asset_id")
        if attachment.get("content_id") is not None and not re.fullmatch(
            r"[A-Za-z0-9._-]{1,120}", str(attachment["content_id"])
        ):
            raise MessageStudioError("attachment content_id is invalid")

    def create_document(
        self, name: str, *, kind: str = "message", document: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        if kind not in {"message", "template"}:
            raise MessageStudioError("document kind must be message or template")
        source = self.validate_document(document or self.blank_document(name))
        source["name"] = name.strip() or source["name"]
        document_id, revision_id = str(uuid.uuid4()), str(uuid.uuid4())
        now = self.now_fn().isoformat()
        digest = _hash(source)
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO message_documents(id,name,kind,status,current_draft_revision_id,created_at,updated_at,created_by,updated_by) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    document_id,
                    source["name"],
                    kind,
                    "draft",
                    revision_id,
                    now,
                    now,
                    "local-operator",
                    "local-operator",
                ),
            )
            connection.execute(
                "INSERT INTO message_revisions(id,document_id,revision_number,status,document_json,content_hash,created_at,created_by) VALUES(?,?,?,?,?,?,?,?)",
                (
                    revision_id,
                    document_id,
                    1,
                    "draft",
                    _canonical(source),
                    digest,
                    now,
                    "local-operator",
                ),
            )
        return self.get_document(document_id)

    def list_documents(
        self, *, kind: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM message_documents"
        params: list[Any] = []
        if kind:
            query += " WHERE kind = ?"
            params.append(kind)
        query += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        params.extend((max(1, min(limit, 200)), max(0, offset)))
        with self.store.connect() as connection:
            return [_row(row) for row in connection.execute(query, params)]

    def get_document(self, document_id: str) -> dict[str, Any]:
        with self.store.connect() as connection:
            document = connection.execute(
                "SELECT * FROM message_documents WHERE id = ?", (document_id,)
            ).fetchone()
            if document is None:
                raise MessageStudioError("message document was not found", "document_not_found")
            revisions = connection.execute(
                "SELECT * FROM message_revisions WHERE document_id = ? ORDER BY revision_number DESC",
                (document_id,),
            ).fetchall()
        result = _row(document)
        result["revisions"] = [_row(item) for item in revisions]
        result["draft"] = next(
            (
                item
                for item in result["revisions"]
                if item["id"] == result["current_draft_revision_id"]
            ),
            None,
        )
        result["published"] = next(
            (
                item
                for item in result["revisions"]
                if item["id"] == result["current_published_revision_id"]
            ),
            None,
        )
        return result

    def save_draft(
        self,
        document_id: str,
        document: Mapping[str, Any],
        *,
        expected_revision_id: str | None = None,
    ) -> dict[str, Any]:
        source = self.validate_document(document)
        now = self.now_fn().isoformat()
        revision_id = str(uuid.uuid4())
        with self.store.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT current_draft_revision_id FROM message_documents WHERE id = ?",
                (document_id,),
            ).fetchone()
            if row is None:
                raise MessageStudioError("message document was not found", "document_not_found")
            if expected_revision_id and row["current_draft_revision_id"] != expected_revision_id:
                raise MessageStudioError("draft changed in another session", "revision_conflict")
            number = connection.execute(
                "SELECT COALESCE(MAX(revision_number), 0) + 1 FROM message_revisions WHERE document_id = ?",
                (document_id,),
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO message_revisions(id,document_id,revision_number,status,document_json,content_hash,created_at,created_by) VALUES(?,?,?,?,?,?,?,?)",
                (
                    revision_id,
                    document_id,
                    number,
                    "draft",
                    _canonical(source),
                    _hash(source),
                    now,
                    "local-operator",
                ),
            )
            connection.execute(
                "UPDATE message_documents SET name = ?, current_draft_revision_id = ?, updated_at = ?, updated_by = ? WHERE id = ?",
                (source["name"], revision_id, now, "local-operator", document_id),
            )
        return self.get_document(document_id)

    def publish(
        self, document_id: str, *, expected_revision_id: str | None = None
    ) -> dict[str, Any]:
        now = self.now_fn().isoformat()
        with self.store.transaction(immediate=True) as connection:
            document = connection.execute(
                "SELECT * FROM message_documents WHERE id = ?", (document_id,)
            ).fetchone()
            if document is None:
                raise MessageStudioError("message document was not found", "document_not_found")
            revision_id = expected_revision_id or document["current_draft_revision_id"]
            revision = connection.execute(
                "SELECT * FROM message_revisions WHERE id = ? AND document_id = ?",
                (revision_id, document_id),
            ).fetchone()
            if revision is None:
                raise MessageStudioError("draft revision was not found", "revision_not_found")
            self.validate_document(json.loads(revision["document_json"]))
            connection.execute(
                "UPDATE message_revisions SET status = 'published', published_at = ? WHERE id = ?",
                (now, revision_id),
            )
            connection.execute(
                "UPDATE message_documents SET status = 'active', current_published_revision_id = ?, updated_at = ? WHERE id = ?",
                (revision_id, now, document_id),
            )
        return self.get_document(document_id)

    def archive(self, document_id: str) -> dict[str, Any]:
        with self.store.transaction(immediate=True) as connection:
            if (
                connection.execute(
                    "UPDATE message_documents SET status = 'archived', updated_at = ? WHERE id = ?",
                    (self.now_fn().isoformat(), document_id),
                ).rowcount
                != 1
            ):
                raise MessageStudioError("message document was not found", "document_not_found")
        return self.get_document(document_id)

    def duplicate(self, document_id: str, name: str | None = None) -> dict[str, Any]:
        source = self.get_document(document_id)
        revision = source.get("published") or source.get("draft")
        if revision is None:
            raise MessageStudioError("document has no revision")
        document = json.loads(json.dumps(revision["document"]))
        document["name"] = name or f"{document['name']} copy"
        return self.create_document(document["name"], kind=source["kind"], document=document)

    def create_asset(
        self, filename: str, media_type: str, data: bytes, *, kind: str = "attachment"
    ) -> dict[str, Any]:
        filename = _filename(filename)
        if kind not in {"image", "attachment"} or len(data) > MAX_ASSET_BYTES:
            raise MessageStudioError("asset kind or size is invalid", "asset_limit")
        if kind == "image" and not media_type.startswith("image/"):
            raise MessageStudioError("image assets require an image media type", "asset_media_type")
        if media_type not in ALLOWED_MEDIA and not (
            kind == "image" and media_type.startswith("image/")
        ):
            raise MessageStudioError("asset media type is not supported", "asset_media_type")
        digest = hashlib.sha256(data).hexdigest()
        now = self.now_fn().isoformat()
        with self.store.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM message_assets WHERE kind = ? AND sha256 = ?", (kind, digest)
            ).fetchone()
            if existing:
                return _row(existing)
            asset_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO message_assets(id,kind,filename,media_type,size_bytes,sha256,data_blob,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (asset_id, kind, filename, media_type, len(data), digest, data, now),
            )
        return self.get_asset(asset_id)

    def import_custom_html(
        self,
        name: str,
        html_data: bytes,
        *,
        filename: str = "message.html",
        assets_zip: bytes | None = None,
        kind: str = "template",
    ) -> dict[str, Any]:
        """Import source plus optional local assets without filesystem extraction."""

        suffix = PurePath(filename.replace("\\", "/")).suffix.lower()
        if suffix not in {".html", ".htm"}:
            raise MessageStudioError(
                "custom HTML import requires an .html or .htm file", "html_type"
            )
        if len(html_data) > 512 * 1024:
            raise MessageStudioError("custom HTML exceeds the 512 KiB limit", "html_limit")
        try:
            source = html_data.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise MessageStudioError("custom HTML must be valid UTF-8", "html_encoding") from error
        if "\x00" in source or "<" not in source or ">" not in source:
            raise MessageStudioError(
                "HTML file content does not match an HTML document", "html_signature"
            )
        candidates = read_zip_assets(assets_zip) if assets_zip is not None else []
        created: list[str] = []
        manifest: list[dict[str, Any]] = []
        try:
            for item in candidates:
                asset = self.create_asset(
                    item.filename,
                    item.media_type,
                    item.data,
                    kind="attachment" if item.kind == "stylesheet" else "image",
                )
                created.append(asset["id"])
                manifest.append(
                    {"reference": item.reference, "asset_id": asset["id"], "kind": item.kind}
                )
            document = self.blank_document(name)
            document.update(
                {
                    "composition_mode": "custom_html",
                    "plain_text_override": None,
                    "custom_html": {
                        "original_source": source,
                        "asset_manifest": manifest,
                    },
                    "blocks": [{"type": "text", "text": "Imported custom HTML"}],
                }
            )
            result = self.create_document(name, kind=kind, document=document)
        except (
            MessageStudioError,
            CustomHTMLImportError,
        ):
            for asset_id in created:
                try:
                    self.archive_asset(asset_id)
                except MessageStudioError:
                    pass
            raise
        draft = result.get("draft") or {}
        return {
            **result,
            "import": {
                "source_filename": PurePath(filename).name,
                "asset_count": len(manifest),
                "diagnostics": (draft.get("document") or {})
                .get("custom_html", {})
                .get("diagnostics", []),
            },
        }

    def export_custom_html(
        self, document_id: str, *, revision_id: str | None = None
    ) -> tuple[bytes, str]:
        document = self.get_document(document_id)
        revision = (
            next((item for item in document["revisions"] if item["id"] == revision_id), None)
            if revision_id
            else (document.get("published") or document.get("draft"))
        )
        if revision is None:
            raise MessageStudioError("document revision was not found", "revision_not_found")
        source = revision["document"]
        if source.get("composition_mode") != "custom_html":
            raise MessageStudioError(
                "only custom HTML documents can be exported", "composition_mode"
            )
        custom = source["custom_html"]
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("original.html", custom["original_source"])
            for item in custom.get("asset_manifest", []):
                if not item.get("asset_id"):
                    continue
                asset = self.get_asset(item["asset_id"], include_data=True)
                reference = (
                    str(item.get("reference") or asset["filename"]).replace("\\", "/").lstrip("/")
                )
                if reference.startswith("../") or "/../" in reference:
                    raise MessageStudioError("asset export path is invalid", "asset_path")
                archive.writestr(reference, asset["data_blob"])
        return output.getvalue(), f"{document['name'].strip() or 'mailforge-template'}.zip"

    def get_asset(self, asset_id: str, *, include_data: bool = False) -> dict[str, Any]:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM message_assets WHERE id = ? AND archived_at IS NULL", (asset_id,)
            ).fetchone()
        if row is None:
            raise MessageStudioError("asset was not found", "asset_not_found")
        result = _row(row)
        if not include_data:
            result.pop("data_blob", None)
        return result

    def list_assets(self, *, kind: str | None = None) -> list[dict[str, Any]]:
        query, params = (
            "SELECT id,kind,filename,media_type,size_bytes,sha256,created_at FROM message_assets WHERE archived_at IS NULL",
            [],
        )
        if kind:
            query += " AND kind = ?"
            params.append(kind)
        query += " ORDER BY created_at DESC"
        with self.store.connect() as connection:
            return [_row(row) for row in connection.execute(query, params)]

    def archive_asset(self, asset_id: str) -> None:
        with self.store.transaction(immediate=True) as connection:
            asset = connection.execute(
                "SELECT id FROM message_assets WHERE id = ? AND archived_at IS NULL",
                (asset_id,),
            ).fetchone()
            if asset is None:
                raise MessageStudioError("asset was not found", "asset_not_found")
            referenced = connection.execute(
                "SELECT 1 FROM message_revisions WHERE document_json LIKE ? "
                "UNION ALL SELECT 1 FROM message_recipient_attachments WHERE asset_id = ? "
                "UNION ALL SELECT 1 FROM message_campaign_snapshots WHERE document_json LIKE ? LIMIT 1",
                (f'%"asset_id":"{asset_id}"%', asset_id, f'%"asset_id":"{asset_id}"%'),
            ).fetchone()
            if referenced is not None:
                raise MessageStudioError(
                    "asset is still referenced by a revision or campaign snapshot",
                    "asset_referenced",
                )
            connection.execute(
                "UPDATE message_assets SET archived_at = ? WHERE id = ?",
                (self.now_fn().isoformat(), asset_id),
            )

    def map_recipient_attachment(
        self, revision_id: str, contact_id: str, asset_id: str, *, required: bool = True
    ) -> dict[str, Any]:
        with self.store.transaction(immediate=True) as connection:
            revision = connection.execute(
                "SELECT id FROM message_revisions WHERE id = ?", (revision_id,)
            ).fetchone()
            contact = connection.execute(
                "SELECT id FROM contacts WHERE id = ?", (contact_id,)
            ).fetchone()
            asset = connection.execute(
                "SELECT id,kind FROM message_assets WHERE id = ? AND archived_at IS NULL",
                (asset_id,),
            ).fetchone()
            if revision is None:
                raise MessageStudioError("message revision was not found", "revision_not_found")
            if contact is None:
                raise MessageStudioError("contact was not found", "contact_not_found")
            if asset is None or asset["kind"] != "attachment":
                raise MessageStudioError("a reusable attachment asset is required", "asset_invalid")
            connection.execute(
                "INSERT INTO message_recipient_attachments(revision_id,contact_id,asset_id,required) "
                "VALUES(?,?,?,?) ON CONFLICT(revision_id,contact_id,asset_id) DO UPDATE SET required=excluded.required",
                (revision_id, contact_id, asset_id, int(required)),
            )
        return {
            "revision_id": revision_id,
            "contact_id": contact_id,
            "asset_id": asset_id,
            "required": required,
        }

    def list_recipient_attachments(self, revision_id: str) -> list[dict[str, Any]]:
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT m.revision_id,m.contact_id,m.asset_id,m.required,a.filename,a.media_type,a.size_bytes "
                "FROM message_recipient_attachments m JOIN message_assets a ON a.id = m.asset_id "
                "WHERE m.revision_id = ? ORDER BY m.contact_id,a.filename",
                (revision_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def remove_recipient_attachment(self, revision_id: str, contact_id: str, asset_id: str) -> None:
        with self.store.transaction(immediate=True) as connection:
            if (
                connection.execute(
                    "DELETE FROM message_recipient_attachments WHERE revision_id = ? AND contact_id = ? AND asset_id = ?",
                    (revision_id, contact_id, asset_id),
                ).rowcount
                != 1
            ):
                raise MessageStudioError(
                    "recipient attachment mapping was not found", "mapping_not_found"
                )

    def _contact_context(
        self, contact: Mapping[str, Any] | None
    ) -> tuple[dict[str, Any], str | None]:
        if not contact:
            return {"email": "sample@example.com", "name": "Sample recipient", "contact": {}}, None
        contact = dict(contact)
        metadata = contact.get("metadata")
        if metadata is None:
            raw = contact.get("metadata_json", "{}")
            metadata = json.loads(raw) if isinstance(raw, str) else {}
        metadata = dict(metadata or {})
        return {
            "email": contact.get("normalized_email", contact.get("email", "sample@example.com")),
            "name": contact.get("display_name", contact.get("name", "Sample recipient")),
            "contact": metadata,
            "target": {
                "email": contact.get(
                    "normalized_email", contact.get("email", "sample@example.com")
                ),
                "name": contact.get("display_name", contact.get("name", "Sample recipient")),
                **metadata,
            },
            **metadata,
        }, contact.get("id")

    def _with_fallbacks(
        self, context: dict[str, Any], document: Mapping[str, Any], warnings: list[str]
    ) -> dict[str, Any]:
        result = json.loads(json.dumps(context))
        fallbacks = document.get("fallbacks", {})
        for match in MERGE_ANY.finditer(_canonical(document)):
            expression = match.group(0)
            valid = MERGE_FIELD.fullmatch(expression)
            if not valid:
                continue
            name = valid.group("name")
            current: Any = result
            for part in name.split("."):
                current = current.get(part) if isinstance(current, Mapping) else None
            if current in (None, ""):
                fallback = fallbacks.get(name)
                if fallback is not None:
                    if name.startswith("contact."):
                        result.setdefault("contact", {})[name.split(".", 1)[1]] = fallback
                    else:
                        result[name] = fallback
                else:
                    warnings.append(f"missing merge field: {name}")
                    if name.startswith("contact."):
                        result.setdefault("contact", {})[name.split(".", 1)[1]] = ""
                    else:
                        result[name] = ""
        return result

    def _asset_attachment(
        self, asset_id: str, *, content_id: str | None = None
    ) -> ResolvedAttachment:
        asset = self.get_asset(asset_id, include_data=True)
        return ResolvedAttachment(
            AttachmentSpec(asset["filename"], asset["filename"], asset["media_type"], content_id),
            asset["data_blob"],
            asset["media_type"],
        )

    def _condition(
        self,
        block: Mapping[str, Any],
        contact_id: str | None,
        mode: str | None,
        condition_modes: Mapping[str, bool] | None,
        warnings: list[str],
    ) -> bool:
        if mode in {"true", "false"}:
            return mode == "true"
        condition = block["condition"]
        if condition_modes is not None:
            key = _canonical(condition)
            if key in condition_modes:
                return bool(condition_modes[key])
        if not contact_id:
            warnings.append("conditional preview uses the nonmatching branch without a contact")
            return False
        try:
            return self.segmentation.matches_contact(
                condition["segment_id"], contact_id, revision_id=condition.get("revision_id")
            )
        except (KeyError, SegmentationError) as error:
            warnings.append(f"conditional segment could not be evaluated: {error}")
            return False

    def _render_blocks(
        self,
        blocks: Sequence[Mapping[str, Any]],
        *,
        contact_id: str | None,
        mode: str | None,
        condition_modes: Mapping[str, bool] | None,
        warnings: list[str],
        attachments: list[ResolvedAttachment],
        text: list[str],
        context: Mapping[str, Any],
    ) -> str:
        html_parts: list[str] = []
        for block in blocks:
            kind = block["type"]
            if kind == "text":
                value = block["text"].replace("\n", "<br>\n")
                styles = ["margin:0 0 16px", "line-height:1.6"]
                if block.get("color"):
                    color = str(block["color"])
                    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                        raise MessageStudioError(
                            "text color must be a six-digit hex value", "unsafe_style"
                        )
                    styles.append(f"color:{color}")
                if block.get("align") in {"left", "center", "right"}:
                    styles.append(f"text-align:{block['align']}")
                value = f"<strong>{value}</strong>" if block.get("bold") else value
                value = f"<em>{value}</em>" if block.get("italic") else value
                value = f"<u>{value}</u>" if block.get("underline") else value
                html_parts.append(f'<p style="{";".join(styles)}">{value}</p>')
                text.append(block["text"])
            elif kind == "heading":
                level = max(1, min(int(block.get("level", 2)), 3))
                html_parts.append(
                    f'<h{level} style="margin:0 0 16px;color:#102338">{block["text"]}</h{level}>'
                )
                text.append(block["text"])
            elif kind == "list":
                items = "".join(f"<li>{item}</li>" for item in block["items"])
                html_parts.append(f'<ul style="margin:0 0 16px;line-height:1.6">{items}</ul>')
                text.extend(f"• {item}" for item in block["items"])
            elif kind == "button":
                label, url = block["label"], block["url"]
                html_parts.append(
                    f'<table role="presentation" cellpadding="0" cellspacing="0"><tr><td style="background:#05635d;border-radius:5px"><a href="{html.escape(url, quote=True)}" style="display:inline-block;padding:12px 18px;color:#ffffff;text-decoration:none">{label}</a></td></tr></table>'
                )
                text.append(f"{label} ({url})")
            elif kind == "image":
                cid = f"mailforge-{block['asset_id']}"
                attachment = self._asset_attachment(block["asset_id"], content_id=cid)
                attachments.append(attachment)
                width = max(1, min(int(block.get("width", 600)), 1200))
                alt = block.get("alt", "")
                html_parts.append(
                    f'<img src="cid:{cid}" alt="{html.escape(alt, quote=True)}" width="{width}" style="display:block;max-width:100%;height:auto;border:0">'
                )
                text.append(f"[Image: {alt}]" if alt else "[Image]")
            elif kind == "divider":
                html_parts.append(
                    '<hr style="border:0;border-top:1px solid #dfe3df;margin:24px 0">'
                )
                text.append("---")
            elif kind == "spacer":
                height = max(1, min(int(block.get("height", 24)), 200))
                html_parts.append(
                    f'<div aria-hidden="true" style="height:{height}px;line-height:{height}px">&nbsp;</div>'
                )
            elif kind == "function":
                inputs = {}
                for key, source in block.get("input_mapping", {}).items():
                    value: Any = context
                    for part in source.split("."):
                        value = value.get(part) if isinstance(value, Mapping) else None
                    inputs[key] = value
                try:
                    execution = self.programmable.execute(
                        block["function_id"],
                        inputs,
                        revision_id=block.get("revision_id"),
                        parameters=block.get("parameters", {}),
                        context="message_studio",
                        correlation_id=f"message-render:{_hash({'block': dict(block), 'inputs': inputs})}",
                    )
                except ProgrammableOperationsError as error:
                    raise MessageStudioError(str(error), error.code) from error
                output = execution["records"][0] if execution["records"] else {}
                field = str(block.get("output_field", "value"))
                value = output.get(
                    field, output.get("personalization_label", output.get("email_domain", ""))
                )
                value = str(value or "")[:10_000]
                html_parts.append(
                    f'<p style="margin:0 0 16px;line-height:1.6">{html.escape(value)}</p>'
                )
                text.append(value)
            elif kind == "columns":
                cells = []
                for column in block["columns"]:
                    child_text: list[str] = []
                    cells.append(
                        self._render_blocks(
                            column,
                            contact_id=contact_id,
                            mode=mode,
                            condition_modes=condition_modes,
                            warnings=warnings,
                            attachments=attachments,
                            text=child_text,
                            context=context,
                        )
                    )
                    text.extend(child_text)
                html_parts.append(
                    '<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
                    + "".join(
                        f'<td valign="top" style="padding:0 8px">{cell}</td>' for cell in cells
                    )
                    + "</tr></table>"
                )
            elif kind == "conditional":
                matched = self._condition(block, contact_id, mode, condition_modes, warnings)
                branch = block.get("if_true" if matched else "if_false", [])
                html_parts.append(
                    self._render_blocks(
                        branch,
                        contact_id=contact_id,
                        mode=mode,
                        condition_modes=condition_modes,
                        warnings=warnings,
                        attachments=attachments,
                        text=text,
                        context=context,
                    )
                )
                warnings.append(
                    f"conditional {block['condition']['segment_id']}: {'TRUE' if matched else 'FALSE'}"
                )
        return "\n".join(html_parts)

    def render_document(
        self,
        document: Mapping[str, Any],
        *,
        contact: Mapping[str, Any] | None = None,
        condition_mode: str | None = None,
        preview: bool = False,
    ) -> dict[str, Any]:
        source = self.validate_document(document)
        context, contact_id = self._contact_context(contact)
        if source.get("composition_mode", "structured") == "custom_html":
            return self._render_custom_document(source, context, preview=preview)
        condition_modes = (
            contact.get("_data_studio_condition_modes") if isinstance(contact, Mapping) else None
        )
        warnings: list[str] = []
        context = self._with_fallbacks(context, source, warnings)
        attachments: list[ResolvedAttachment] = []
        text_parts: list[str] = []
        body = self._render_blocks(
            source["blocks"],
            contact_id=contact_id,
            mode=condition_mode,
            condition_modes=condition_modes,
            warnings=warnings,
            attachments=attachments,
            text=text_parts,
            context=context,
        )
        text_template = source.get("plain_text_override") or "\n\n".join(text_parts)
        preheader = source.get("preheader", "")
        html_template = f'<div style="display:none;max-height:0;overflow:hidden">{preheader}</div><div style="max-width:600px;margin:0 auto;font-family:Arial,sans-serif;color:#102338">{body}</div>'
        definition = TemplateDefinition(
            name=source["name"],
            version=str(source["version"]),
            subject=source["subject"],
            text=text_template,
            html=html_template,
        )
        rendered = self.renderer.render(definition, context, seed=0, now=self.now_fn())
        preheader_rendered = self.renderer.render(
            TemplateDefinition(name="preheader", version="1", subject="", text=preheader),
            context,
            seed=0,
            now=self.now_fn(),
        ).text
        for reference in source.get("attachments", []):
            attachments.append(
                self._asset_attachment(
                    reference["asset_id"], content_id=reference.get("content_id")
                )
            )
        approximate_bytes = sum(
            len(value.encode("utf-8"))
            for value in (rendered.subject, rendered.text, rendered.html or "")
        ) + sum(len(item.data) for item in attachments)
        if approximate_bytes > MAX_MESSAGE_ASSET_BYTES:
            raise MessageStudioError(
                "rendered message exceeds the 25 MiB local limit", "message_limit"
            )
        manifest = [
            {
                "filename": item.spec.filename,
                "content_type": item.content_type,
                "content_id": item.spec.content_id,
                "bytes": len(item.data),
            }
            for item in attachments
        ]
        return {
            "subject": rendered.subject,
            "preheader": preheader_rendered,
            "html": rendered.html or "",
            "text": rendered.text,
            "warnings": list(dict.fromkeys(warnings)),
            "attachments": manifest,
            "approximate_bytes": approximate_bytes,
            "_rendered": rendered,
            "_attachments": tuple(attachments),
        }

    def _render_custom_document(
        self, source: Mapping[str, Any], context: Mapping[str, Any], *, preview: bool
    ) -> dict[str, Any]:
        custom = source["custom_html"]
        fallback_warnings: list[str] = []
        context = self._with_fallbacks(dict(context), source, fallback_warnings)
        assets = self._custom_asset_lookup(custom.get("asset_manifest", []))
        analyzed = analyze_html(custom["original_source"], assets, preview=preview)
        if analyzed["has_errors"] and any(
            item["severity"] == "ERROR" and item["code"] != "missing_asset"
            for item in analyzed["diagnostics"]
        ):
            first = next(item for item in analyzed["diagnostics"] if item["severity"] == "ERROR")
            raise MessageStudioError(first["message"], first["code"])
        attachments: list[ResolvedAttachment] = []
        for item in custom.get("asset_manifest", []):
            asset = assets.get(str(item.get("reference", "")).replace("\\", "/").lstrip("/"))
            if asset and asset.get("kind") != "stylesheet":
                attachments.append(
                    self._asset_attachment(
                        asset["id"], content_id=f"mailforge-custom-{asset['id']}"
                    )
                )
        html_template = analyzed["prepared_source"]
        text_template = (
            analyzed["plain_text"]
            if custom.get("plain_text_generated")
            else source.get("plain_text_override") or analyzed["plain_text"]
        )
        definition = TemplateDefinition(
            name=source["name"],
            version="custom-html-1",
            subject=source["subject"],
            text=text_template,
            html=html_template,
        )
        rendered = self.renderer.render(definition, context, seed=0, now=self.now_fn())
        rendered_html = rendered.html or ""
        if re.search(
            r"(?:src|background)=[\"'](?:https?:|//|javascript:|data:text/html)",
            rendered_html,
            re.I,
        ):
            raise MessageStudioError("prepared HTML contains an unsafe resource URL", "unsafe_url")
        diagnostics = list(analyzed["diagnostics"])
        if any(item["code"] == "missing_asset" for item in diagnostics):
            raise MessageStudioError("custom HTML has missing local assets", "missing_asset")
        manifest = [
            {
                "filename": item.spec.filename,
                "content_type": item.content_type,
                "content_id": item.spec.content_id,
                "bytes": len(item.data),
            }
            for item in attachments
        ]
        approximate_bytes = sum(
            len(value.encode("utf-8")) for value in (rendered.subject, rendered.text, rendered_html)
        ) + sum(len(item.data) for item in attachments)
        if approximate_bytes > MAX_MESSAGE_ASSET_BYTES:
            raise MessageStudioError(
                "rendered message exceeds the 25 MiB local limit", "message_limit"
            )
        warnings = fallback_warnings + [
            item["message"] for item in diagnostics if item["severity"] != "ERROR"
        ]
        return {
            "subject": rendered.subject,
            "preheader": "",
            "html": rendered_html,
            "text": rendered.text,
            "warnings": list(dict.fromkeys(warnings)),
            "diagnostics": diagnostics,
            "attachments": manifest,
            "approximate_bytes": approximate_bytes,
            "_rendered": rendered,
            "_attachments": tuple(attachments),
        }

    def preview(
        self,
        document: Mapping[str, Any] | None = None,
        *,
        revision_id: str | None = None,
        contact_id: str | None = None,
        condition_mode: str | None = None,
    ) -> dict[str, Any]:
        if revision_id:
            with self.store.connect() as connection:
                row = connection.execute(
                    "SELECT document_json FROM message_revisions WHERE id = ?", (revision_id,)
                ).fetchone()
            if row is None:
                raise MessageStudioError("message revision was not found", "revision_not_found")
            document = json.loads(row["document_json"])
        if document is None:
            raise MessageStudioError("a document or revision is required")
        contact = self.store.get_contact(contact_id) if contact_id else None
        result = self.render_document(
            document, contact=contact, condition_mode=condition_mode, preview=True
        )
        result.pop("_rendered", None)
        result.pop("_attachments", None)
        return result

    def prepare_controlled_test(
        self,
        *,
        revision_id: str,
        sender: SenderPolicy,
        recipient: str,
    ) -> dict[str, Any]:
        """Freeze one published revision for a bounded provider test grant."""

        normalized_recipient = normalize_email(recipient)
        if not validate_email(normalized_recipient).valid:
            raise MessageStudioError("controlled-test recipient is invalid", "recipient_invalid")
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM message_revisions WHERE id = ? AND status = 'published'",
                (revision_id,),
            ).fetchone()
        if row is None:
            raise MessageStudioError(
                "controlled tests require a published message revision",
                "revision_not_published",
            )
        document = json.loads(row["document_json"])
        contact = self.store.contact_by_email(normalized_recipient)
        if contact is None:
            contact = {
                "id": None,
                "normalized_email": normalized_recipient,
                "display_name": "",
                "metadata": {},
            }
        else:
            contact = {
                "id": contact["id"],
                "normalized_email": contact["normalized_email"],
                "display_name": contact["display_name"],
                "metadata": json.loads(contact["metadata_json"] or "{}"),
                "lifecycle_status": contact["lifecycle_status"],
            }
        if contact.get("lifecycle_status", "active") != "active":
            raise MessageStudioError(
                "controlled tests require an active recipient", "recipient_ineligible"
            )
        active_suppressions = self.store.matching_suppressions(
            normalized_email=normalized_recipient,
            contact_id=contact.get("id") or "",
            list_id="",
            campaign_id="",
            sender_config_ref=sender.ref,
            communication_purpose="transactional",
        )
        if active_suppressions:
            raise MessageStudioError(
                "controlled-test recipient is suppressed", "recipient_ineligible"
            )
        rendered = self.render_document(document, contact=contact)
        composed = self.composer.compose(
            sender=sender,
            recipient=normalized_recipient,
            rendered=rendered["_rendered"],
            attachments=rendered["_attachments"],
        )
        prepared_at = self.now_fn().isoformat()
        context_hash = digest_json(
            {
                "email": normalized_recipient,
                "name": contact.get("display_name", ""),
                "metadata": contact.get("metadata", {}),
            }
        )
        proof_id = str(uuid.uuid4())
        source_identity = (
            document.get("custom_html", {}).get("source_hash")
            if document.get("composition_mode") == "custom_html"
            else None
        )
        payload = build_payload(
            operation_kind="controlled_test",
            sender=composed.from_email,
            recipient=normalized_recipient,
            content_hash=composed.content_hash,
            prepared_at=prepared_at,
            artifact_kind="controlled_test",
            artifact_ref=proof_id,
            message_revision_id=revision_id,
            source_identity=source_identity,
            eligibility={
                "status": "controlled_test_scope",
                "reason": "recipient is authorized by the bounded provider grant",
                "contact_id": contact.get("id"),
            },
            personalization_hash=context_hash,
            policy_revision=self.store.policy_revision(),
        )
        proof = make_proof(proof_id, payload)
        self.store.record_outbound_proof(
            proof_id=proof.proof_id,
            proof_version=proof.proof_version,
            proof_hash=proof.proof_hash,
            operation_kind="controlled_test",
            message_revision_id=revision_id,
            campaign_id=None,
            intent_id=None,
            sender=proof.sender,
            recipient=proof.recipient,
            content_hash=proof.content_hash,
            artifact_kind="controlled_test",
            artifact_ref=proof_id,
            proof_json=json.dumps(
                proof.payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
            mime_bytes=composed.serialized,
            created_at=prepared_at,
        )
        return {
            "proof_id": proof.proof_id,
            "proof_hash": proof.proof_hash,
            "proof_version": proof.proof_version,
            "message_revision_id": revision_id,
            "sender": proof.sender,
            "recipient": proof.recipient,
            "content_hash": proof.content_hash,
            "prepared_at": prepared_at,
            "eligibility": proof.payload["eligibility"],
            "preview": {
                "subject": rendered["subject"],
                "text": rendered["text"],
                "html": rendered["html"],
                "attachments": rendered["attachments"],
            },
        }

    def validate_revision(self, revision_id: str) -> dict[str, Any]:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT document_json FROM message_revisions WHERE id = ?", (revision_id,)
            ).fetchone()
        if row is None:
            raise MessageStudioError("message revision was not found", "revision_not_found")
        document = self.validate_document(json.loads(row["document_json"]))
        return {"valid": True, "content_hash": _hash(document), "warnings": []}

    def campaign_snapshot(
        self, campaign_id: str, revision_id: str, *, run_key: str = "primary"
    ) -> dict[str, Any]:
        with self.store.connect() as connection:
            revision = connection.execute(
                "SELECT * FROM message_revisions WHERE id = ?", (revision_id,)
            ).fetchone()
            existing = connection.execute(
                "SELECT * FROM message_campaign_snapshots WHERE campaign_id = ? AND run_key = ?",
                (campaign_id, run_key),
            ).fetchone()
        if revision is None or revision["status"] != "published":
            raise MessageStudioError(
                "campaigns require a published message revision", "revision_not_published"
            )
        if existing:
            if existing["revision_id"] != revision_id:
                raise MessageStudioError(
                    "campaign run already has a different message snapshot", "snapshot_conflict"
                )
            return _row(existing)
        snapshot_id = str(uuid.uuid4())
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO message_campaign_snapshots(id,campaign_id,run_key,revision_id,content_hash,document_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    snapshot_id,
                    campaign_id,
                    run_key,
                    revision_id,
                    revision["content_hash"],
                    revision["document_json"],
                    self.now_fn().isoformat(),
                ),
            )
        with self.store.connect() as connection:
            return _row(
                connection.execute(
                    "SELECT * FROM message_campaign_snapshots WHERE id = ?", (snapshot_id,)
                ).fetchone()
            )

    def create_data_studio_snapshot(
        self,
        campaign_id: str,
        *,
        source_job_id: str,
        artifact_id: str,
        recipe_revision_id: str,
        revision_id: str,
        field_mapping: Mapping[str, str],
        run_key: str = "primary",
    ) -> dict[str, Any]:
        with self.store.campaign_operation_lock():
            return self._create_data_studio_snapshot(
                campaign_id,
                source_job_id=source_job_id,
                artifact_id=artifact_id,
                recipe_revision_id=recipe_revision_id,
                revision_id=revision_id,
                field_mapping=field_mapping,
                run_key=run_key,
            )

    def _create_data_studio_snapshot(
        self,
        campaign_id: str,
        *,
        source_job_id: str,
        artifact_id: str,
        recipe_revision_id: str,
        revision_id: str,
        field_mapping: Mapping[str, str],
        run_key: str = "primary",
    ) -> dict[str, Any]:
        """Freeze processed rows and their rendering context for one campaign run."""

        if self.data_studio is None:
            raise MessageStudioError("Data Studio is not configured", "data_studio_unavailable")
        campaign = self.store.get_campaign(campaign_id)
        job = self.data_studio.get_job(source_job_id)
        if job["status"] != "completed" or job["recipe_revision_id"] != recipe_revision_id:
            raise MessageStudioError(
                "the source job must be completed with the selected recipe revision",
                "data_snapshot_invalid",
            )
        artifact = next((item for item in job["artifacts"] if item["id"] == artifact_id), None)
        if artifact is None or artifact["kind"] != "output":
            raise MessageStudioError(
                "the processed output artifact was not found", "artifact_invalid"
            )
        artifact_path, _ = self.data_studio.artifact_path(source_job_id, artifact_id)
        if _file_hash(artifact_path) != artifact["sha256"]:
            raise MessageStudioError(
                "the processed output artifact checksum changed", "artifact_integrity"
            )
        with self.store.connect() as connection:
            revision = connection.execute(
                "SELECT * FROM message_revisions WHERE id=? AND status='published'", (revision_id,)
            ).fetchone()
            existing = connection.execute(
                "SELECT * FROM data_studio_campaign_snapshots WHERE campaign_id=? AND run_key=?",
                (campaign_id, run_key),
            ).fetchone()
            attachment_rows = connection.execute(
                "SELECT contact_id,asset_id FROM message_recipient_attachments WHERE revision_id=?",
                (revision_id,),
            ).fetchall()
        if revision is None:
            raise MessageStudioError(
                "campaigns require a published message revision", "revision_not_published"
            )
        document = json.loads(revision["document_json"])
        if existing is None:
            with self.store.connect() as connection:
                prepared = connection.execute(
                    "SELECT COUNT(*) FROM delivery_intents "
                    "WHERE campaign_id=? AND state='prepared'",
                    (campaign_id,),
                ).fetchone()[0]
            if prepared:
                raise MessageStudioError(
                    "campaign already has prepared messages; create a new campaign for a Data Studio batch",
                    "snapshot_conflict",
                )
        if not field_mapping:
            raise MessageStudioError(
                "at least one processed field mapping is required", "mapping_invalid"
            )
        allowed_targets = {"email", "name"}
        for target in field_mapping.values():
            if target not in allowed_targets and not re.fullmatch(
                r"contact\.[a-z][a-z0-9_]{0,63}", target
            ):
                raise MessageStudioError(
                    f"unsupported campaign field mapping: {target}", "mapping_invalid"
                )
        rows = list(
            self.data_studio.iter_artifact_records(source_job_id, artifact_id, limit=100_001)
        )
        if len(rows) > 100_000:
            raise MessageStudioError(
                "campaign preparation is limited to 100,000 processed rows", "campaign_limit"
            )
        email_source = next(
            (source for source, target in field_mapping.items() if target == "email"), None
        )
        if not email_source:
            raise MessageStudioError("an email field mapping is required", "mapping_invalid")
        attachment_map: dict[str, list[str]] = {}
        for item in attachment_rows:
            attachment_map.setdefault(item["contact_id"], []).append(item["asset_id"])
        recipients: list[dict[str, Any]] = []
        seen_contacts: set[str] = set()
        for row_number, row in enumerate(rows, start=1):
            if email_source not in row:
                raise MessageStudioError(
                    f"row {row_number} is missing the mapped email field", "mapping_invalid"
                )
            email = normalize_email(str(row[email_source] or ""))
            if not validate_email(email).valid:
                raise MessageStudioError(
                    f"row {row_number} does not contain a valid recipient email", "mapping_invalid"
                )
            contact = self.store.contact_by_email(email)
            if contact is None:
                raise MessageStudioError(
                    f"row {row_number} has no reconciled contact for {email}",
                    "recipient_unreconciled",
                )
            contact_id = contact["id"]
            if contact_id in seen_contacts:
                raise MessageStudioError(
                    f"processed data contains duplicate recipient {email}", "recipient_duplicate"
                )
            seen_contacts.add(contact_id)
            with self.store.connect() as connection:
                member = connection.execute(
                    "SELECT 1 FROM list_memberships WHERE list_id=? AND contact_id=?",
                    (campaign["audience_list_id"], contact_id),
                ).fetchone()
            if member is None:
                raise MessageStudioError(
                    f"recipient {email} is not in the campaign audience list",
                    "recipient_not_in_audience",
                )
            metadata = json.loads(contact["metadata_json"] or "{}")
            name = contact["display_name"]
            for source, target in field_mapping.items():
                if source not in row:
                    continue
                if target == "name":
                    name = str(row[source] or "")
                elif target.startswith("contact."):
                    metadata[target.split(".", 1)[1]] = row[source]
            context_contact = {
                "id": contact_id,
                "normalized_email": email,
                "display_name": name,
                "metadata": metadata,
                "_data_studio_condition_modes": self._condition_modes(
                    document.get("blocks", []), contact_id
                ),
            }
            recipients.append(
                {
                    "row_number": row_number,
                    "source_row_hash": _hash(row),
                    "contact_id": contact_id,
                    "normalized_email": email,
                    "context": context_contact,
                    "attachment_asset_ids": attachment_map.get(contact_id, []),
                    "rendering_seed": int(
                        hashlib.sha256(f"{campaign_id}:{email}:{revision_id}".encode()).hexdigest()[
                            :16
                        ],
                        16,
                    )
                    % (2**63 - 1),
                }
            )
        snapshot_payload = {
            "campaign_id": campaign_id,
            "run_key": run_key,
            "source_job_id": source_job_id,
            "source_artifact_id": artifact_id,
            "artifact_sha256": artifact["sha256"],
            "recipe_revision_id": recipe_revision_id,
            "message_revision_id": revision_id,
            "message_content_hash": revision["content_hash"],
            "function_references": self._function_references(document.get("blocks", [])),
            "field_mapping": dict(field_mapping),
            "recipients": recipients,
        }
        content_hash = _hash(snapshot_payload)
        if existing is not None:
            if existing["content_hash"] != content_hash:
                raise MessageStudioError(
                    "campaign data snapshot already exists with different source or mapping",
                    "snapshot_conflict",
                )
            return self.get_data_studio_snapshot(existing["id"])
        snapshot_id = str(uuid.uuid4())
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO data_studio_campaign_snapshots(id,campaign_id,run_key,source_job_id,source_artifact_id,artifact_sha256,recipe_revision_id,message_revision_id,message_content_hash,field_mapping_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    snapshot_id,
                    campaign_id,
                    run_key,
                    source_job_id,
                    artifact_id,
                    artifact["sha256"],
                    recipe_revision_id,
                    revision_id,
                    revision["content_hash"],
                    _canonical(dict(field_mapping)),
                    content_hash,
                    self.now_fn().isoformat(),
                ),
            )
            for item in recipients:
                connection.execute(
                    "INSERT INTO data_studio_campaign_recipients(snapshot_id,row_number,source_row_hash,contact_id,normalized_email,context_json,attachment_asset_ids_json,rendering_seed) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        snapshot_id,
                        item["row_number"],
                        item["source_row_hash"],
                        item["contact_id"],
                        item["normalized_email"],
                        _canonical(item["context"]),
                        _canonical(item["attachment_asset_ids"]),
                        item["rendering_seed"],
                    ),
                )
        return self.get_data_studio_snapshot(snapshot_id)

    def _condition_modes(
        self, blocks: Sequence[Mapping[str, Any]], contact_id: str
    ) -> dict[str, bool]:
        modes: dict[str, bool] = {}

        def visit(items: Sequence[Mapping[str, Any]]) -> None:
            for block in items:
                if block.get("type") == "conditional":
                    condition = block["condition"]
                    try:
                        modes[_canonical(condition)] = self.segmentation.matches_contact(
                            condition["segment_id"],
                            contact_id,
                            revision_id=condition.get("revision_id"),
                        )
                    except KeyError, SegmentationError:
                        modes[_canonical(condition)] = False
                    visit(block.get("if_true", []))
                    visit(block.get("if_false", []))
                elif block.get("type") == "columns":
                    for column in block.get("columns", []):
                        visit(column)

        visit(blocks)
        return modes

    def _function_references(self, blocks: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
        references: list[dict[str, str]] = []

        def visit(items: Sequence[Mapping[str, Any]]) -> None:
            for block in items:
                if block.get("type") == "function":
                    try:
                        function = self.programmable.get_function(block["function_id"])
                        revision = (
                            self.programmable.get_revision(block["revision_id"])
                            if block.get("revision_id")
                            else self.programmable.list_revisions(function["id"])[0]
                        )
                    except (ProgrammableOperationsError, IndexError) as error:
                        raise MessageStudioError(str(error), "function_invalid") from error
                    references.append(
                        {
                            "function_id": function["id"],
                            "revision_id": revision["id"],
                            "content_hash": revision["content_hash"],
                        }
                    )
                if block.get("type") == "columns":
                    for column in block.get("columns", []):
                        visit(column)
                if block.get("type") == "conditional":
                    visit(block.get("if_true", []))
                    visit(block.get("if_false", []))

        visit(blocks)
        return references

    def get_data_studio_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        with self.store.connect() as connection:
            snapshot = connection.execute(
                "SELECT * FROM data_studio_campaign_snapshots WHERE id=?", (snapshot_id,)
            ).fetchone()
            if snapshot is None:
                raise MessageStudioError(
                    "campaign data snapshot was not found", "snapshot_not_found"
                )
            recipients = connection.execute(
                "SELECT * FROM data_studio_campaign_recipients WHERE snapshot_id=? ORDER BY row_number",
                (snapshot_id,),
            ).fetchall()
        result = dict(snapshot)
        result["field_mapping"] = json.loads(result.pop("field_mapping_json"))
        result["recipients"] = []
        for item in recipients:
            value = dict(item)
            value["context"] = json.loads(value.pop("context_json"))
            value["attachment_asset_ids"] = json.loads(value.pop("attachment_asset_ids_json"))
            result["recipients"].append(value)
        return result

    def prepare_campaign(
        self,
        campaign_id: str,
        *,
        sender: SenderPolicy,
        revision_id: str | None = None,
        run_key: str = "primary",
        campaign_variables: dict[str, Any] | None = None,
        data_snapshot_id: str | None = None,
        unsubscribe_url_callback: Callable[[Mapping[str, object]], str | None] | None = None,
        one_click_unsubscribe: bool = False,
        dkim_signed: bool = False,
    ):
        with self.store.campaign_operation_lock():
            return self._prepare_campaign(
                campaign_id,
                sender=sender,
                revision_id=revision_id,
                run_key=run_key,
                campaign_variables=campaign_variables,
                data_snapshot_id=data_snapshot_id,
                unsubscribe_url_callback=unsubscribe_url_callback,
                one_click_unsubscribe=one_click_unsubscribe,
                dkim_signed=dkim_signed,
            )

    def _prepare_campaign(
        self,
        campaign_id: str,
        *,
        sender: SenderPolicy,
        revision_id: str | None = None,
        run_key: str = "primary",
        campaign_variables: dict[str, Any] | None = None,
        data_snapshot_id: str | None = None,
        unsubscribe_url_callback: Callable[[Mapping[str, object]], str | None] | None = None,
        one_click_unsubscribe: bool = False,
        dkim_signed: bool = False,
    ):
        if self.outbound is None:
            raise MessageStudioError("outbound service is not configured")
        refresh_prepared = data_snapshot_id is not None
        campaign = self.store.get_campaign(campaign_id)
        revision_id = revision_id or campaign["message_revision_id"]
        if not revision_id:
            raise MessageStudioError("campaign has no message revision", "revision_required")
        if data_snapshot_id is None:
            with self.store.connect() as connection:
                saved_snapshot = connection.execute(
                    "SELECT id FROM data_studio_campaign_snapshots "
                    "WHERE campaign_id=? AND run_key=? AND message_revision_id=?",
                    (campaign_id, run_key, revision_id),
                ).fetchone()
                any_snapshot = connection.execute(
                    "SELECT 1 FROM data_studio_campaign_snapshots WHERE campaign_id=? LIMIT 1",
                    (campaign_id,),
                ).fetchone()
            if saved_snapshot is not None:
                data_snapshot_id = saved_snapshot["id"]
            elif any_snapshot is not None:
                raise MessageStudioError(
                    "campaign has a Data Studio snapshot for a different revision or run",
                    "snapshot_conflict",
                )
        snapshot = self.campaign_snapshot(campaign_id, revision_id, run_key=run_key)
        document = json.loads(snapshot["document_json"])
        static = []
        for reference in document.get("attachments", []):
            static.append(
                self._asset_attachment(
                    reference["asset_id"], content_id=reference.get("content_id")
                )
            )
        per_contact: dict[str, tuple[ResolvedAttachment, ...]] = {}
        with self.store.connect() as connection:
            mappings = connection.execute(
                "SELECT contact_id,asset_id FROM message_recipient_attachments WHERE revision_id = ?",
                (revision_id,),
            ).fetchall()
        for mapping in mappings:
            per_contact.setdefault(mapping["contact_id"], tuple())
            per_contact[mapping["contact_id"]] += (self._asset_attachment(mapping["asset_id"]),)
        contact_overrides = None
        recipient_ids = None
        if data_snapshot_id:
            data_snapshot = self.get_data_studio_snapshot(data_snapshot_id)
            if (
                data_snapshot["campaign_id"] != campaign_id
                or data_snapshot["message_revision_id"] != revision_id
                or data_snapshot["run_key"] != run_key
            ):
                raise MessageStudioError(
                    "campaign data snapshot does not match this run", "snapshot_conflict"
                )
            contact_overrides = {
                item["contact_id"]: item["context"] for item in data_snapshot["recipients"]
            }
            recipient_ids = set(contact_overrides)
            per_contact = {
                item["contact_id"]: tuple(
                    self._asset_attachment(asset_id) for asset_id in item["attachment_asset_ids"]
                )
                for item in data_snapshot["recipients"]
            }
        template = TemplateDefinition(
            name=f"studio:{revision_id}", version=revision_id, subject="", text=""
        )

        def render(contact: Mapping[str, Any], _intent: Mapping[str, Any], _seed: int):
            result = self.render_document(document, contact=contact)
            return result["_rendered"], result["_attachments"]

        return self.outbound._prepare_campaign(
            campaign_id,
            template=template,
            sender=sender,
            campaign_variables=campaign_variables,
            resolved_attachments=tuple(static),
            resolved_attachments_by_contact=per_contact,
            contact_overrides=contact_overrides,
            recipient_ids=recipient_ids,
            refresh_prepared=refresh_prepared,
            render_callback=render,
            unsubscribe_url_callback=unsubscribe_url_callback,
            one_click_unsubscribe=one_click_unsubscribe,
            dkim_signed=dkim_signed,
        )
