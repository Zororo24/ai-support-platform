"""`{{ path }}` templating for action requests and workflow messages.

Only variable paths are allowed (see expressions.py); there are no filters, loops or calls.
A string that is exactly one placeholder keeps the value's type (so numbers stay numbers in a
JSON body); otherwise values are interpolated as text. A missing value is an error, never a
silent empty string, so a half-built request is never sent.
"""

import json
import re
from typing import Any
from urllib.parse import quote

from app.agents.expressions import lookup, split_path

MAX_RENDERED_CHARS = 10_000
_PLACEHOLDER = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")


class TemplateError(ValueError):
    """The template is malformed or references a value that is not available."""

    def __init__(self, message: str, missing: str | None = None):
        super().__init__(message)
        self.missing = missing


def _lookup(path_source: str, scope: dict) -> Any:
    segments = split_path(path_source)
    if segments is None:
        raise TemplateError(
            f"placeholder '{path_source}' must be a plain variable path like args.order_number"
        )
    value = lookup(scope, segments)
    if value is None:
        raise TemplateError(f"missing value: {path_source}", missing=path_source)
    return value


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


def render(template: str, scope: dict, *, url_quote: bool = False) -> Any:
    """Render one string. With `url_quote`, interpolated values are made safe for a URL path/query."""
    single = _PLACEHOLDER.fullmatch(template.strip()) if template.strip() == template else None
    if single and not url_quote:
        return _lookup(single.group(1), scope)

    def substitute(match: re.Match) -> str:
        text = _as_text(_lookup(match.group(1), scope))
        if url_quote:
            if text in (".", ".."):
                raise TemplateError("a value cannot be '.' or '..' inside a URL")
            return quote(text, safe="")
        return text

    if "{{" in _PLACEHOLDER.sub("", template) or "}}" in _PLACEHOLDER.sub("", template):
        raise TemplateError("unbalanced '{{' or '}}' in template")
    rendered = _PLACEHOLDER.sub(substitute, template)
    if len(rendered) > MAX_RENDERED_CHARS:
        raise TemplateError("rendered text is too long")
    return rendered


def render_json(value: Any, scope: dict) -> Any:
    """Render every string inside a JSON-like structure (dict keys are left alone)."""
    if isinstance(value, str):
        return render(value, scope)
    if isinstance(value, list):
        return [render_json(v, scope) for v in value]
    if isinstance(value, dict):
        return {k: render_json(v, scope) for k, v in value.items()}
    return value


def placeholders(value: Any) -> set[str]:
    """Every `{{ path }}` source string used anywhere inside a template or structure."""
    found: set[str] = set()
    if isinstance(value, str):
        found.update(m.group(1) for m in _PLACEHOLDER.finditer(value))
    elif isinstance(value, list):
        for item in value:
            found |= placeholders(item)
    elif isinstance(value, dict):
        for item in value.values():
            found |= placeholders(item)
    return found
