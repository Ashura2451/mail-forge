"""Controlled, deterministic Jinja and legacy spintax rendering."""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from jinja2 import DictLoader, StrictUndefined, TemplateError
from jinja2.sandbox import SandboxedEnvironment

from mailforge.domain.outbound import TemplateDefinition


class TemplateRenderError(ValueError):
    """Raised when a template cannot be safely rendered."""


@dataclass(frozen=True)
class RenderedTemplate:
    subject: str
    text: str
    html: str | None


_SPINTAX = re.compile(r"\{([^{}]*\|[^{}]*)\}")
_SPECIAL = re.compile(r"\{(random_int|date|time)\}")
_ALLOWED_FILTERS = {
    "capitalize": lambda value: str(value).capitalize(),
    "lower": lambda value: str(value).lower(),
    "trim": lambda value: str(value).strip(),
    "upper": lambda value: str(value).upper(),
}


def _primitive(
    value: Any,
    path: str = "context",
    *,
    depth: int = 0,
    state: dict[str, int] | None = None,
    max_depth: int = 8,
    max_items: int = 10_000,
    max_bytes: int = 1_024 * 1_024,
) -> Any:
    state = state or {"items": 0, "bytes": 0}
    state["items"] += 1
    if state["items"] > max_items:
        raise TemplateRenderError("template context item limit exceeded")
    if depth > max_depth:
        raise TemplateRenderError("template context depth limit exceeded")
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, str):
            state["bytes"] += len(value.encode("utf-8"))
            if state["bytes"] > max_bytes:
                raise TemplateRenderError("template context size limit exceeded")
        return value
    if isinstance(value, dict):
        return {
            str(key): _primitive(
                child,
                f"{path}.{key}",
                depth=depth + 1,
                state=state,
                max_depth=max_depth,
                max_items=max_items,
                max_bytes=max_bytes,
            )
            for key, child in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            _primitive(
                child,
                f"{path}[{index}]",
                depth=depth + 1,
                state=state,
                max_depth=max_depth,
                max_items=max_items,
                max_bytes=max_bytes,
            )
            for index, child in enumerate(value)
        ]
    raise TemplateRenderError(f"unsupported template context value at {path}")


class SafeTemplateRenderer:
    """Render only primitive context through a no-I/O sandbox."""

    def __init__(
        self,
        *,
        max_expansions: int = 256,
        max_context_items: int = 10_000,
        max_context_depth: int = 8,
        max_context_bytes: int = 1_024 * 1_024,
    ):
        self.max_expansions = max_expansions
        self.max_context_items = max_context_items
        self.max_context_depth = max_context_depth
        self.max_context_bytes = max_context_bytes

    @staticmethod
    def _render_bounded(template, context: dict[str, Any], max_bytes: int) -> str:
        chunks: list[str] = []
        rendered_bytes = 0
        for chunk in template.generate(**context):
            rendered_bytes += len(chunk.encode("utf-8"))
            if rendered_bytes > max_bytes:
                raise TemplateRenderError("template output exceeds size limit")
            chunks.append(chunk)
        return "".join(chunks)

    def _environment(self, *, html: bool) -> SandboxedEnvironment:
        environment = SandboxedEnvironment(
            loader=DictLoader({"template": ""}),
            undefined=StrictUndefined,
            autoescape=html,
        )
        environment.globals.clear()
        environment.filters = dict(_ALLOWED_FILTERS)
        return environment

    def _spintax(self, value: str, *, seed: int, now: datetime, max_depth: int) -> str:
        generator = random.Random(seed)
        expanded = 0
        result = value
        for _depth in range(max_depth + 1):
            match = _SPINTAX.search(result)
            if not match:
                break
            expanded += 1
            if expanded > self.max_expansions:
                raise TemplateRenderError("spintax expansion limit exceeded")
            options = match.group(1).split("|")
            choice = options[generator.randrange(len(options))].strip()
            result = result[: match.start()] + choice + result[match.end() :]
        else:
            raise TemplateRenderError("spintax nesting limit exceeded")

        def special(match: re.Match[str]) -> str:
            if match.group(1) == "random_int":
                return str(generator.randint(1000, 9999))
            if match.group(1) == "date":
                return now.date().isoformat()
            return now.strftime("%H:%M")

        return _SPECIAL.sub(special, result)

    def render(
        self,
        definition: TemplateDefinition,
        context: dict[str, Any],
        *,
        seed: int = 0,
        now: datetime | None = None,
    ) -> RenderedTemplate:
        now = now or datetime.now(UTC)
        if definition.max_rendered_bytes <= 0:
            raise TemplateRenderError("rendered template size limit must be positive")
        safe_context = _primitive(
            context,
            max_depth=self.max_context_depth,
            max_items=self.max_context_items,
            max_bytes=self.max_context_bytes,
        )
        try:
            subject = self._render_bounded(
                self._environment(html=False).from_string(definition.subject),
                safe_context,
                definition.max_rendered_bytes,
            )
            text = self._render_bounded(
                self._environment(html=False).from_string(definition.text),
                safe_context,
                definition.max_rendered_bytes,
            )
            html = (
                self._render_bounded(
                    self._environment(html=True).from_string(definition.html),
                    safe_context,
                    definition.max_rendered_bytes,
                )
                if definition.html is not None
                else None
            )
        except (TemplateError, TypeError, ValueError) as error:
            raise TemplateRenderError(str(error)) from error
        rendered = RenderedTemplate(
            subject=self._spintax(
                subject, seed=seed, now=now, max_depth=definition.max_expansion_depth
            ),
            text=self._spintax(
                text, seed=seed + 1, now=now, max_depth=definition.max_expansion_depth
            ),
            html=(
                self._spintax(
                    html, seed=seed + 2, now=now, max_depth=definition.max_expansion_depth
                )
                if html is not None
                else None
            ),
        )
        if (
            sum(
                len(value.encode("utf-8"))
                for value in (rendered.subject, rendered.text, rendered.html or "")
            )
            > definition.max_rendered_bytes
        ):
            raise TemplateRenderError("rendered template exceeds size limit")
        return rendered
