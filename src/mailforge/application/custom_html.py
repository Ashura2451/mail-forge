"""Bounded, non-executing interoperability helpers for email HTML.

This module deliberately does not attempt to be a general web HTML sanitizer.
It accepts the small, useful intersection of email markup and the existing
Message Studio renderer, while retaining the operator's original source.
"""

from __future__ import annotations

import base64
import html
import posixpath
import re
import stat
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse

MAX_CUSTOM_HTML_BYTES = 512 * 1024
MAX_IMPORT_ZIP_BYTES = 25 * 1024 * 1024
MAX_EXTRACTED_BYTES = 50 * 1024 * 1024
MAX_ARCHIVE_FILES = 200
MAX_ARCHIVE_DEPTH = 8
MAX_COMPRESSION_RATIO = 100
ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


class CustomHTMLImportError(ValueError):
    def __init__(self, message: str, code: str = "custom_html_invalid"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AssetCandidate:
    reference: str
    filename: str
    media_type: str
    data: bytes
    kind: str


@dataclass(frozen=True)
class HTMLDiagnostic:
    severity: str
    code: str
    message: str
    reference: str | None = None

    def as_dict(self) -> dict[str, str]:
        result = {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
        }
        if self.reference:
            result["reference"] = self.reference
        return result


_ATTR = re.compile(
    r"(?P<name>[A-Za-z_:][A-Za-z0-9_.:-]*)(?:\s*=\s*(?P<quote>[\"'])(?P<quoted>.*?)\2|\s*=\s*(?P<bare>[^\s>]+))?",
    re.S,
)
_TAG = re.compile(
    r"<(?P<closing>/)?(?P<name>[A-Za-z][A-Za-z0-9:_-]*)(?P<attrs>(?:[^\"'>]|\"[^\"]*\"|'[^']*')*)(?P<self>/?)>",
    re.S,
)
_CSS_RULE = re.compile(r"(?P<selectors>[^{}]+)\{(?P<body>[^{}]*)\}", re.S)
_CSS_URL = re.compile(r"url\(\s*(?P<quote>[\"']?)(?P<url>.*?)(?P=quote)\s*\)", re.I | re.S)
_MERGE = re.compile(
    r"\{\{\s*(?:email|name|contact\.[a-z][a-z0-9_]{0,63})(?:\s*\|\s*(?:capitalize|lower|trim|upper))*\s*\}\}"
)
_UNSAFE_SCHEME = re.compile(r"^(?:javascript|vbscript|data|file|blob):", re.I)
_EXTERNAL = re.compile(r"^(?:https?:)?//", re.I)
_SAFE_PREVIEW_DATA_URL = re.compile(
    r"^data:image/(?:png|gif|jpeg|webp);base64,[A-Za-z0-9+/=]+$", re.I
)
_CSS_RESOURCE_FUNCTIONS = {
    "image-set",
    "-webkit-image-set",
    "cross-fade",
    "element",
    "paint",
    "var",
    "env",
}
_CSS_RESOURCE_AT_RULES = {"import", "font-face", "namespace", "property"}


def local_references(source: str) -> list[str]:
    """Return deterministic local image/CSS references from HTML/CSS source."""

    values = re.findall(r"\b(?:src|background)\s*=\s*[\"']([^\"']+)[\"']", source, re.I)
    values.extend(match.group("url") for match in _CSS_URL.finditer(source))
    result: list[str] = []
    for value in values:
        try:
            ref = _reference(value)
        except CustomHTMLImportError:
            continue
        if ref and ref not in result:
            result.append(ref)
    return result


def _reference(value: str) -> str | None:
    value = html.unescape(value).strip().replace("\\", "/")
    if not value or value.startswith("#") or value.lower().startswith("cid:"):
        return None
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc:
        return None
    normalized = posixpath.normpath("/" + value).lstrip("/")
    if normalized in {"", "."} or normalized.startswith("../") or "/../" in f"/{value}":
        raise CustomHTMLImportError("asset reference escapes the imported bundle", "path_traversal")
    return normalized


def _safe_link(value: str) -> bool:
    value = html.unescape(value).strip()
    if _MERGE.fullmatch(value):
        return True
    if not value or any(char.isspace() or ord(char) < 0x20 for char in value):
        return False
    if value.startswith("//") or _UNSAFE_SCHEME.match(value):
        return False
    parsed = urlparse(value)
    return parsed.scheme.lower() in {"http", "https", "mailto"} and bool(
        parsed.path or parsed.netloc
    )


def _media_type(filename: str) -> str:
    suffix = PurePosixPath(filename).suffix.lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".css": "text/css",
    }.get(suffix, "application/octet-stream")


def _validate_image(filename: str, data: bytes) -> None:
    suffix = PurePosixPath(filename).suffix.lower()
    signatures = {
        ".png": data.startswith(b"\x89PNG\r\n\x1a\n"),
        ".jpg": data.startswith(b"\xff\xd8\xff"),
        ".jpeg": data.startswith(b"\xff\xd8\xff"),
        ".gif": data.startswith((b"GIF87a", b"GIF89a")),
        ".webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP",
    }
    if suffix in signatures and not signatures[suffix]:
        raise CustomHTMLImportError(
            f"asset {filename} does not match its image signature", "asset_signature"
        )


def read_zip_assets(raw: bytes) -> list[AssetCandidate]:
    """Read an asset bundle without extracting it to the application filesystem."""

    if len(raw) > MAX_IMPORT_ZIP_BYTES:
        raise CustomHTMLImportError("asset ZIP exceeds the 25 MiB limit", "zip_limit")
    if not raw.startswith(b"PK"):
        raise CustomHTMLImportError("asset bundle is not a ZIP archive", "zip_signature")
    candidates: list[AssetCandidate] = []
    seen: set[str] = set()
    total = 0
    try:
        archive = zipfile.ZipFile(BytesIO(raw))
        infos = archive.infolist()
        if len(infos) > MAX_ARCHIVE_FILES:
            raise CustomHTMLImportError("asset ZIP contains too many files", "zip_file_count")
        for info in infos:
            raw_name = info.filename.replace("\\", "/")
            if info.is_dir():
                continue
            if raw_name.startswith("/") or raw_name.startswith("\\"):
                raise CustomHTMLImportError("absolute ZIP paths are not allowed", "zip_path")
            parts = [part for part in raw_name.split("/") if part not in {"", "."}]
            if len(parts) > MAX_ARCHIVE_DEPTH or any(part == ".." for part in parts):
                raise CustomHTMLImportError(
                    "ZIP path traversal or nesting is not allowed", "zip_path"
                )
            normalized = "/".join(parts)
            if not normalized or normalized in seen:
                raise CustomHTMLImportError(
                    "duplicate ZIP asset names are not allowed", "zip_duplicate"
                )
            seen.add(normalized)
            if PurePosixPath(normalized).suffix.lower() == ".zip":
                raise CustomHTMLImportError("nested ZIP archives are not allowed", "zip_nested")
            suffix = PurePosixPath(normalized).suffix.lower()
            if suffix not in ALLOWED_IMAGE_EXTENSIONS and suffix != ".css":
                raise CustomHTMLImportError(
                    f"unsupported active or asset file in ZIP: {suffix or 'unknown'}",
                    "zip_file_type",
                )
            if (
                info.file_size > MAX_EXTRACTED_BYTES
                or info.compress_size == 0
                or (info.file_size / max(info.compress_size, 1) > MAX_COMPRESSION_RATIO)
            ):
                raise CustomHTMLImportError(
                    "ZIP entry exceeds decompression safety limits", "zip_bomb"
                )
            total += info.file_size
            if total > MAX_EXTRACTED_BYTES:
                raise CustomHTMLImportError(
                    "extracted ZIP assets exceed the 50 MiB limit", "zip_limit"
                )
            data = archive.read(info)
            if len(data) != info.file_size:
                raise CustomHTMLImportError("ZIP entry size changed while reading", "zip_integrity")
            mode = (info.external_attr >> 16) & 0o170000
            if mode and stat.S_ISLNK(mode):
                raise CustomHTMLImportError("archive symlinks are not allowed", "zip_symlink")
            if suffix in ALLOWED_IMAGE_EXTENSIONS:
                _validate_image(normalized, data)
                kind = "image"
            else:
                data.decode("utf-8")
                kind = "stylesheet"
            candidates.append(
                AssetCandidate(
                    normalized, PurePosixPath(normalized).name, _media_type(normalized), data, kind
                )
            )
    except zipfile.BadZipFile as error:
        raise CustomHTMLImportError(
            "asset bundle is not a valid ZIP archive", "zip_invalid"
        ) from error
    return candidates


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.link: str | None = None
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "template", "head"}:
            self.skip += 1
        if tag == "a":
            self.link = dict(attrs).get("href")
        if tag in {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "section"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "template", "head"} and self.skip:
            self.skip -= 1
        if tag.lower() == "a" and self.link:
            self.parts.append(f" ({self.link})")
            self.link = None
        if tag.lower() in {"p", "div", "li", "tr", "section"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.parts.append(data)


def plain_text_from_html(source: str) -> str:
    parser = _TextExtractor()
    parser.feed(source)
    parser.close()
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in "".join(parser.parts).splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(line for line in lines if line)).strip()


def _sanitize_css(
    css: str, assets: dict[str, dict[str, Any]], diagnostics: list[HTMLDiagnostic], *, preview: bool
) -> str:
    quote: str | None = None
    index = 0
    while index < len(css):
        char = css[index]
        if char == "\\":
            diagnostics.append(
                HTMLDiagnostic("ERROR", "unsafe_css", "CSS escape sequences are not supported")
            )
            return ""
        if quote:
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "/" and index + 1 < len(css) and css[index + 1] == "*":
            diagnostics.append(
                HTMLDiagnostic("ERROR", "unsafe_css", "CSS comments are not supported")
            )
            return ""
        elif char == "@":
            end = index + 1
            while end < len(css) and css[end].isspace():
                end += 1
            start = end
            while end < len(css) and (css[end].isalnum() or css[end] in {"-", "_"}):
                end += 1
            if css[start:end].lower() in _CSS_RESOURCE_AT_RULES:
                diagnostics.append(
                    HTMLDiagnostic("ERROR", "unsafe_css", "CSS resource at-rules are not supported")
                )
                return ""
            index = end
            continue
        elif char.isalpha() or char in {"-", "_"}:
            end = index + 1
            while end < len(css) and (css[end].isalnum() or css[end] in {"-", "_"}):
                end += 1
            if (
                css[index:end].lower() in _CSS_RESOURCE_FUNCTIONS
                and end < len(css)
                and css[end] == "("
            ):
                diagnostics.append(
                    HTMLDiagnostic(
                        "ERROR", "unsafe_css", "CSS resource functions are not supported"
                    )
                )
                return ""
            index = end
            continue
        index += 1
    if quote:
        diagnostics.append(HTMLDiagnostic("ERROR", "unsafe_css", "CSS string is not terminated"))
        return ""

    def replace_url(match: re.Match[str]) -> str:
        value = html.unescape(match.group("url")).strip()
        try:
            ref = _reference(value)
        except CustomHTMLImportError as error:
            diagnostics.append(HTMLDiagnostic("ERROR", error.code, str(error), value))
            return "url("
        if ref:
            asset = assets.get(ref)
            if not asset:
                diagnostics.append(
                    HTMLDiagnostic("ERROR", "missing_asset", "Local CSS asset is missing", ref)
                )
                return "url("
            if asset.get("kind") != "stylesheet":
                replacement = _asset_url(asset, preview=preview)
                return f"url('{replacement}')"
            return "url("
        if _EXTERNAL.match(value):
            diagnostics.append(
                HTMLDiagnostic(
                    "WARNING", "remote_resource", "Remote CSS resource was not fetched", value
                )
            )
            return "url("
        if _UNSAFE_SCHEME.match(value):
            diagnostics.append(
                HTMLDiagnostic("ERROR", "unsafe_css_url", "Unsafe CSS URL was removed", value)
            )
            return "url("
        if value.lower().startswith("cid:") or (
            preview and _SAFE_PREVIEW_DATA_URL.fullmatch(value)
        ):
            return f"url('{value}')"
        if urlparse(value).scheme or urlparse(value).netloc:
            diagnostics.append(
                HTMLDiagnostic("ERROR", "unsafe_css_url", "Unsupported CSS URL was removed", value)
            )
            return "url("
        return f"url('{value}')"

    css = re.sub(r"@import\s+[^;]+;", "", css, flags=re.I)
    return _CSS_URL.sub(replace_url, css)


def _asset_url(asset: dict[str, Any], *, preview: bool) -> str:
    if preview and asset.get("data") is not None:
        encoded = base64.b64encode(asset["data"]).decode("ascii")
        return f"data:{asset['media_type']};base64,{encoded}"
    return f"cid:mailforge-custom-{asset['id']}"


def _inline_simple_css(source: str) -> str:
    style_blocks = list(re.finditer(r"<style[^>]*>(?P<body>.*?)</style>", source, re.I | re.S))
    rules: list[tuple[str, str]] = []
    for block in style_blocks:
        for rule in _CSS_RULE.finditer(block.group("body")):
            if "@media" in rule.group("selectors").lower() or ":" in rule.group("selectors"):
                continue
            declaration = re.sub(r"\s+", " ", rule.group("body").strip())
            for selector in rule.group("selectors").split(","):
                selector = selector.strip()
                if re.fullmatch(r"(?:[A-Za-z][\w-]*|\.[\w-]+|#[\w-]+)", selector):
                    rules.append((selector, declaration))
    for selector, declaration in rules:
        if selector.startswith("."):
            pattern = re.compile(
                rf"(<[A-Za-z][^>]*\bclass=[\"'][^\"']*\b{re.escape(selector[1:])}\b[^\"']*[\"'][^>]*)(>)",
                re.I,
            )
        elif selector.startswith("#"):
            pattern = re.compile(
                rf"(<[A-Za-z][^>]*\bid=[\"']{re.escape(selector[1:])}[\"'][^>]*)(>)", re.I
            )
        else:
            pattern = re.compile(rf"(<{re.escape(selector)}\b[^>]*)(>)", re.I)

        def add_style(match: re.Match[str], declaration: str = declaration) -> str:
            prefix, close = match.group(1), match.group(2)
            existing = re.search(r"\bstyle=[\"']([^\"']*)[\"']", prefix, re.I)
            if existing:
                merged = f"{declaration.rstrip(';')};{existing.group(1)}"
                return prefix[: existing.start(1)] + merged + prefix[existing.end(1) :] + close
            return f'{prefix} style="{html.escape(declaration, quote=True)}"{close}'

        source = pattern.sub(add_style, source)
    return source


def analyze_html(
    source: str,
    assets: dict[str, dict[str, Any]],
    *,
    preview: bool = False,
) -> dict[str, Any]:
    if not isinstance(source, str) or not source.strip():
        raise CustomHTMLImportError("custom HTML source is required", "html_empty")
    if not re.search(r"<\s*[A-Za-z][A-Za-z0-9:_-]*(?:\s|/?>)", source):
        raise CustomHTMLImportError("HTML source contains no valid element tags", "html_signature")
    if len(source.encode("utf-8")) > MAX_CUSTOM_HTML_BYTES:
        raise CustomHTMLImportError("custom HTML exceeds the 512 KiB limit", "html_limit")
    diagnostics: list[HTMLDiagnostic] = []
    if re.search(r"<!--[\s\S]*?\[if\s+(?:mso|!mso|gte|lte|lt|gt)", source, re.I):
        diagnostics.append(
            HTMLDiagnostic(
                "WARNING",
                "conditional_markup",
                "Outlook conditional markup is retained and requires client testing",
            )
        )
    if re.search(r"<\s*v:(?:rect|roundrect|shape|image)\b", source, re.I):
        diagnostics.append(
            HTMLDiagnostic(
                "WARNING", "vml_markup", "VML is retained for source fidelity and is not previewed"
            )
        )

    def replace_style(match: re.Match[str]) -> str:
        body = _sanitize_css(match.group("body"), assets, diagnostics, preview=preview)
        return f"<style>{body}</style>"

    transformed = re.sub(
        r"<style[^>]*>(?P<body>.*?)</style>", replace_style, source, flags=re.I | re.S
    )

    def replace_link(match: re.Match[str]) -> str:
        tag = match.group(0)
        attrs = dict(
            (m.group("name").lower(), m.group("quoted") or m.group("bare") or "")
            for m in _ATTR.finditer(match.group("attrs"))
        )
        href = attrs.get("href", "")
        if attrs.get("rel", "").lower() == "stylesheet":
            ref = _reference(href)
            asset = assets.get(ref or "")
            if asset and asset.get("kind") == "stylesheet":
                stylesheet = _sanitize_css(
                    asset.get("data", b"").decode("utf-8"),
                    assets,
                    diagnostics,
                    preview=preview,
                )
                return f"<style>{stylesheet}</style>"
            diagnostics.append(
                HTMLDiagnostic(
                    "WARNING", "external_stylesheet", "External stylesheet was not fetched", href
                )
            )
            return ""
        return tag

    transformed = re.sub(r"<link(?P<attrs>[^>]*)>", replace_link, transformed, flags=re.I | re.S)

    def resource_value(value: str) -> str:
        try:
            ref = _reference(value)
        except CustomHTMLImportError as error:
            diagnostics.append(HTMLDiagnostic("ERROR", error.code, str(error), value))
            return ""
        if ref:
            asset = assets.get(ref)
            if not asset:
                diagnostics.append(
                    HTMLDiagnostic("ERROR", "missing_asset", "Local asset is missing", ref)
                )
                return ""
            return _asset_url(asset, preview=preview)
        if _EXTERNAL.match(value):
            diagnostics.append(
                HTMLDiagnostic(
                    "WARNING", "remote_resource", "Remote resource was not fetched", value
                )
            )
            return ""
        if value.lower().startswith("cid:") or (
            preview and _SAFE_PREVIEW_DATA_URL.fullmatch(value)
        ):
            return value
        if _UNSAFE_SCHEME.match(value):
            diagnostics.append(
                HTMLDiagnostic("ERROR", "unsafe_url", "Unsafe resource URL was removed", value)
            )
            return ""
        if urlparse(value).scheme or urlparse(value).netloc:
            diagnostics.append(
                HTMLDiagnostic("ERROR", "unsafe_url", "Unsupported resource URL was removed", value)
            )
            return ""
        return value

    def resource_set(value: str) -> str:
        candidates: list[str] = []
        for candidate in value.split(","):
            pieces = candidate.strip().split()
            if not pieces:
                continue
            safe = resource_value(pieces[0])
            if safe:
                candidates.append(" ".join([safe, *pieces[1:]]))
        return ", ".join(candidates)

    def replace_tag(match: re.Match[str]) -> str:
        name = match.group("name").lower()
        if match.group("closing"):
            if name in {"script", "iframe", "object", "embed", "form", "svg", "link", "meta"}:
                return ""
            return match.group(0)
        if name in {
            "script",
            "iframe",
            "object",
            "embed",
            "form",
            "svg",
            "link",
            "meta",
            "base",
            "audio",
            "video",
        }:
            diagnostics.append(
                HTMLDiagnostic(
                    "ERROR", "active_content", f"Unsupported active element <{name}> was removed"
                )
            )
            return ""
        attrs: list[str] = []
        for item in _ATTR.finditer(match.group("attrs")):
            key = item.group("name").lower()
            value = item.group("quoted") if item.group("quoted") is not None else item.group("bare")
            value = value or ""
            if key.startswith("on"):
                diagnostics.append(
                    HTMLDiagnostic(
                        "ERROR", "event_handler", "Event-handler attribute was removed", key
                    )
                )
                continue
            if key in {"src", "background", "poster"}:
                try:
                    ref = _reference(value)
                except CustomHTMLImportError as error:
                    diagnostics.append(HTMLDiagnostic("ERROR", error.code, str(error), value))
                    continue
                if ref:
                    asset = assets.get(ref)
                    if not asset:
                        diagnostics.append(
                            HTMLDiagnostic("ERROR", "missing_asset", "Local asset is missing", ref)
                        )
                        continue
                    value = _asset_url(asset, preview=preview)
                elif _EXTERNAL.match(value):
                    diagnostics.append(
                        HTMLDiagnostic(
                            "WARNING",
                            "remote_resource",
                            "Remote image resource was not fetched",
                            value,
                        )
                    )
                    continue
                elif value.lower().startswith("cid:") or (
                    preview and _SAFE_PREVIEW_DATA_URL.fullmatch(value)
                ):
                    pass
                elif _UNSAFE_SCHEME.match(value):
                    diagnostics.append(
                        HTMLDiagnostic(
                            "ERROR", "unsafe_url", "Unsafe resource URL was removed", value
                        )
                    )
                    continue
                elif urlparse(value).scheme or urlparse(value).netloc:
                    diagnostics.append(
                        HTMLDiagnostic(
                            "ERROR", "unsafe_url", "Unsupported resource URL was removed", value
                        )
                    )
                    continue
            elif key == "srcset":
                value = resource_set(value)
                if not value:
                    continue
            elif key in {"href", "action", "cite"} and not _safe_link(value):
                diagnostics.append(
                    HTMLDiagnostic("ERROR", "unsafe_url", "Unsafe link URL was removed", value)
                )
                continue
            elif key == "style":
                value = _sanitize_css(value, assets, diagnostics, preview=preview)
            if value:
                attrs.append(f'{key}="{html.escape(value, quote=True)}"')
            else:
                attrs.append(key)
        spacing = f" {' '.join(attrs)}" if attrs else ""
        closing = "/" if match.group("self") else ""
        return f"<{name}{spacing}{closing}>"

    transformed = _TAG.sub(replace_tag, transformed)
    if preview:
        transformed = re.sub(r"<!--[\s\S]*?-->", "", transformed)
        transformed = re.sub(r"</?v:[^>]+>", "", transformed, flags=re.I)
    transformed = _inline_simple_css(transformed)
    return {
        "prepared_source": transformed,
        "plain_text": plain_text_from_html(source),
        "diagnostics": [item.as_dict() for item in diagnostics],
        "has_errors": any(item.severity == "ERROR" for item in diagnostics),
    }
