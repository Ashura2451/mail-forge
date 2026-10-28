"""Disposable-domain loading and explicit, opt-in refresh."""

from __future__ import annotations

import hashlib
import json
import tempfile
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

DEFAULT_DISPOSABLE_DOMAINS = frozenset(
    {
        "mailinator.com",
        "guerrillamail.com",
        "guerrillamail.net",
        "tempmail.com",
        "throwaway.email",
        "yopmail.com",
        "sharklasers.com",
        "guerrillamailblock.com",
        "grr.la",
        "dispostable.com",
        "mailnesia.com",
        "maildrop.cc",
        "trashmail.com",
        "10minutemail.com",
        "temp-mail.org",
        "fakeinbox.com",
        "getnada.com",
        "mohmal.com",
    }
)


def _redacted_source_url(url: str) -> str:
    """Keep provenance useful without persisting query tokens or credentials."""

    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port is not None else ""
        return urlunsplit((parsed.scheme, hostname + port, parsed.path, "", ""))
    except ValueError:
        return "<redacted>"


class DisposableDomainStore:
    def __init__(self, path: Path | None = None):
        self.path = path

    @property
    def provenance_path(self) -> Path | None:
        return self.path.with_suffix(self.path.suffix + ".meta.json") if self.path else None

    def load(self) -> frozenset[str]:
        if self.path is None or not self.path.exists():
            return DEFAULT_DISPOSABLE_DOMAINS
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError, json.JSONDecodeError:
            return DEFAULT_DISPOSABLE_DOMAINS
        if not isinstance(value, list):
            return DEFAULT_DISPOSABLE_DOMAINS
        return frozenset(item.lower().strip() for item in value if isinstance(item, str))

    def refresh(
        self,
        url: str,
        *,
        opener: Callable[..., object] = urllib.request.urlopen,
        timeout: float = 15.0,
    ) -> int:
        if self.path is None:
            raise ValueError("a destination path is required for refresh")
        with opener(url, timeout=timeout) as response:
            if getattr(response, "status", 200) != 200:
                raise RuntimeError(f"disposable-domain refresh returned {response.status}")
            payload = json.loads(response.read())
        if not isinstance(payload, list) or not all(isinstance(item, str) for item in payload):
            raise ValueError("disposable-domain source must be a list of strings")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        domains = sorted({item.lower().strip() for item in payload})
        old_dataset = self.path.read_bytes() if self.path.exists() else None
        provenance_path = self.provenance_path
        old_provenance = (
            provenance_path.read_bytes()
            if provenance_path is not None and provenance_path.exists()
            else None
        )
        temporary: Path | None = None
        temporary_provenance: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent, delete=False
            ) as handle:
                json.dump(domains, handle, indent=2)
                handle.write("\n")
                temporary = Path(handle.name)
            content_hash = hashlib.sha256(temporary.read_bytes()).hexdigest()
            provenance = {
                "source_url": _redacted_source_url(url),
                "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "domain_count": len(domains),
                "sha256": content_hash,
                "schema_version": "1",
            }
            if provenance_path is not None:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=provenance_path.parent, delete=False
                ) as handle:
                    json.dump(provenance, handle, indent=2)
                    handle.write("\n")
                    temporary_provenance = Path(handle.name)
            temporary.replace(self.path)
            if provenance_path is not None and temporary_provenance is not None:
                temporary_provenance.replace(provenance_path)
        except BaseException:
            for temporary_path in (temporary, temporary_provenance):
                if temporary_path is not None:
                    try:
                        temporary_path.unlink()
                    except FileNotFoundError:
                        pass
            self._restore_bytes(self.path, old_dataset)
            if provenance_path is not None:
                self._restore_bytes(provenance_path, old_provenance)
            raise
        return len(domains)

    @staticmethod
    def _restore_bytes(path: Path, content: bytes | None) -> None:
        if content is None:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as handle:
            handle.write(content)
            temporary = Path(handle.name)
        temporary.replace(path)

    def load_provenance(self) -> dict[str, object] | None:
        if self.provenance_path is None or not self.provenance_path.exists():
            return None
        try:
            value = json.loads(self.provenance_path.read_text(encoding="utf-8"))
        except OSError, json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None
