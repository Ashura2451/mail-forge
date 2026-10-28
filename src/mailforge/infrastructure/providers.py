"""Configurable provider and registered-domain routing."""

from __future__ import annotations

import json
from pathlib import Path

from mailforge.domain.sorting import ProviderDefinition

try:
    import tldextract
except ImportError:  # pragma: no cover - optional fallback
    tldextract = None


class ProviderConfigError(ValueError):
    """Raised when provider configuration is malformed or unsafe."""


class ProviderCatalog:
    """Load provider definitions and route addresses without network access."""

    def __init__(
        self, path: Path | None = None, definitions: list[ProviderDefinition] | None = None
    ):
        self.path = path
        self.definitions = definitions if definitions is not None else self._load(path)
        self.domain_to_file: dict[str, str] = {}
        self.domain_to_name: dict[str, str] = {}
        self.mx_patterns: list[tuple[str, str]] = []
        self._extractor = (
            tldextract.TLDExtract(suffix_list_urls=()) if tldextract is not None else None
        )
        self._build_maps()

    @staticmethod
    def _load(path: Path | None) -> list[ProviderDefinition]:
        if path is None or not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProviderConfigError(f"cannot load providers: {path}") from exc
        values = raw.get("providers", raw) if isinstance(raw, dict) else raw
        if not isinstance(values, dict):
            raise ProviderConfigError("provider configuration must be an object")
        definitions: list[ProviderDefinition] = []
        for name, value in values.items():
            if not isinstance(value, dict):
                raise ProviderConfigError(f"provider {name!r} must be an object")
            output_file = value.get("output_file")
            if (
                not isinstance(output_file, str)
                or not output_file
                or Path(output_file).is_absolute()
            ):
                raise ProviderConfigError(f"unsafe output file for provider {name!r}")
            if ".." in Path(output_file).parts:
                raise ProviderConfigError(f"unsafe output file for provider {name!r}")
            aliases = value.get("aliases", [])
            mx_patterns = value.get("mx_patterns", [])
            if not all(isinstance(item, str) for item in (*aliases, *mx_patterns)):
                raise ProviderConfigError(f"provider {name!r} patterns must be strings")
            definitions.append(
                ProviderDefinition(name, output_file, tuple(aliases), tuple(mx_patterns))
            )
        return definitions

    def _build_maps(self) -> None:
        for definition in self.definitions:
            for alias in definition.aliases:
                domain = alias.lower().strip().rstrip(".")
                self.domain_to_file[domain] = definition.output_file
                self.domain_to_name[domain] = definition.name
            self.mx_patterns.extend(
                (pattern.lower().rstrip("."), definition.name) for pattern in definition.mx_patterns
            )
        if not self.mx_patterns:
            self.mx_patterns = [
                ("aspmx.l.google.com", "google_workspace"),
                ("googlemail.com", "google_workspace"),
                ("mail.protection.outlook.com", "office365"),
                ("outlook.com", "office365"),
                ("zoho.com", "zoho_hosted"),
                ("protonmail.ch", "proton_hosted"),
                ("messagingengine.com", "fastmail_hosted"),
            ]

    def registered_domain(self, domain: str) -> str:
        domain = domain.lower().strip().rstrip(".")
        if self._extractor is not None:
            extracted = self._extractor(domain)
            if extracted.domain and extracted.suffix:
                return f"{extracted.domain}.{extracted.suffix}".lower()
        return domain

    def route(self, email: str) -> tuple[str, str]:
        try:
            domain = email.rsplit("@", 1)[1].lower()
        except IndexError, ValueError:
            return "invalid.txt", "unknown"
        registered = self.registered_domain(domain)
        return (
            self.domain_to_file.get(registered, "custom_domains.txt"),
            self.domain_to_name.get(registered, "custom"),
        )

    def hosting_from_mx(self, mx_host: str) -> str | None:
        normalized = mx_host.lower().rstrip(".")
        for pattern, provider in self.mx_patterns:
            if normalized.endswith(pattern) or pattern in normalized:
                return provider
        return None
