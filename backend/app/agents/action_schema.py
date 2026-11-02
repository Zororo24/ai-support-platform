"""Typed definition of an action: something an agent can do in a company's own systems.

Everything here is validated when the config is saved, so mistakes (unknown variables, bad
expressions, forbidden headers) are reported to the admin immediately instead of failing in
front of a customer.
"""

import re
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.agents.expressions import ExpressionError, parse, split_path
from app.agents.templating import placeholders

ACTION_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
PARAM_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
FORBIDDEN_HEADERS = {
    "host",
    "authorization",
    "proxy-authorization",
    "cookie",
    "content-length",
    "transfer-encoding",
    "connection",
    "upgrade",
    "te",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-real-ip",
}
ALLOWED_ROOTS = {"args", "context"}  # workflows add `vars` and `last` later


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ParamDef(_Strict):
    type: Literal["string", "integer", "number", "boolean"] = "string"
    description: str = Field(default="", max_length=300)
    required: bool = False
    enum: list[str | int | float] | None = Field(default=None, max_length=50)
    pattern: str | None = Field(default=None, max_length=200)
    min_length: int | None = Field(default=None, ge=0, le=10_000)
    max_length: int | None = Field(default=None, ge=1, le=10_000)
    minimum: float | None = None
    maximum: float | None = None

    @field_validator("pattern")
    @classmethod
    def _valid_regex(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"invalid pattern: {exc}") from exc
        return value


class RequestDef(_Strict):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    path: str = Field(min_length=1, max_length=300)
    query: dict[str, str] = Field(default_factory=dict)
    body: dict[str, Any] | list[Any] | None = None
    headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("path must start with '/'")
        if any(seg in ("..", ".") for seg in value.split("?")[0].split("/")):
            raise ValueError("path must not contain '.' or '..' segments")
        if "?" in value or "#" in value:
            raise ValueError("put query parameters in 'query', not in the path")
        return value

    @field_validator("headers")
    @classmethod
    def _headers(cls, value: dict[str, str]) -> dict[str, str]:
        for name in value:
            if name.lower() in FORBIDDEN_HEADERS:
                raise ValueError(f"header '{name}' is set by the platform and cannot be overridden")
            if not re.fullmatch(r"[A-Za-z0-9\-]{1,60}", name):
                raise ValueError(f"invalid header name '{name}'")
        return value


class GuardDef(_Strict):
    when: str = Field(min_length=1, max_length=500)
    then: Literal["refuse"] = "refuse"
    message: str = Field(default="That request isn't allowed.", max_length=300)


class ActionLimits(_Strict):
    timeout_s: int = Field(default=10, ge=1, le=30)
    max_calls_per_run: int = Field(default=3, ge=1, le=10)
    max_response_bytes: int = Field(default=65_536, ge=1024, le=262_144)
    retries: int = Field(default=0, ge=0, le=2)  # honoured for read actions only


class ResponseDef(_Strict):
    expose: list[str] = Field(default_factory=list, max_length=30)
    max_chars: int = Field(default=4000, ge=200, le=20_000)


class ActionDef(_Strict):
    name: str
    description: str = Field(min_length=1, max_length=500)
    # hosted: we call your API through this connection. handback: we ask your backend to do it.
    execution: Literal["hosted", "handback"] = "hosted"
    connection_id: UUID | None = None
    # Required for hosted actions; a handback action only needs a name, parameters and description.
    request: RequestDef | None = None
    parameters: dict[str, ParamDef] = Field(default_factory=dict, max_length=20)
    effect: Literal["read", "write"] = "read"
    # None = sensible default: writes ask the end user, reads run straight away.
    # user: the end user approves; staff: a person on your team approves in the dashboard.
    confirmation: Literal["none", "user", "staff"] | None = None
    confirm_summary: str | None = Field(default=None, max_length=300)
    guards: list[GuardDef] = Field(default_factory=list, max_length=10)
    limits: ActionLimits = Field(default_factory=ActionLimits)
    response: ResponseDef = Field(default_factory=ResponseDef)
    idempotency_header: str | None = Field(default=None, max_length=60)
    mock: Any = None  # canned response used by the playground and tests
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not ACTION_NAME_RE.match(value):
            raise ValueError("name must be lowercase letters, digits and underscores, starting with a letter")
        return value

    @field_validator("parameters")
    @classmethod
    def _param_names(cls, value: dict[str, ParamDef]) -> dict[str, ParamDef]:
        for name in value:
            if not PARAM_NAME_RE.match(name):
                raise ValueError(f"parameter name '{name}' must be lowercase letters, digits and underscores")
        return value

    @property
    def effective_confirmation(self) -> str:
        return self.confirmation or ("user" if self.effect == "write" else "none")

    @model_validator(mode="after")
    def _check_references(self) -> "ActionDef":
        if self.execution == "hosted":
            if self.connection_id is None:
                raise ValueError("connection_id: choose the connection this action calls")
            if self.request is None:
                raise ValueError("request: describe the call this action makes")
        declared = set(self.parameters)
        templates: list[Any] = [self.confirm_summary or ""]
        if self.request is not None:
            templates += [self.request.path, self.request.query, self.request.body, self.request.headers]
        sources = set()
        for template in templates:
            sources |= placeholders(template)
        for source in sources:
            segments = split_path(source)
            if segments is None:
                raise ValueError(f"'{{{{ {source} }}}}' is not a plain variable path")
            root = segments[0]
            if root not in ALLOWED_ROOTS:
                raise ValueError(f"'{source}': variables must start with args. or context.")
            if root == "args" and (len(segments) < 2 or segments[1] not in declared):
                raise ValueError(f"'{source}' refers to a parameter that is not declared")
        for guard in self.guards:
            try:
                paths = parse(guard.when).paths()
            except ExpressionError as exc:
                raise ValueError(f"guard '{guard.when}': {exc}") from exc
            for path in paths:
                if path[0] not in ALLOWED_ROOTS:
                    raise ValueError(f"guard '{guard.when}': variables must start with args. or context.")
                if path[0] == "args" and (len(path) < 2 or path[1] not in declared):
                    raise ValueError(
                        f"guard '{guard.when}': '{path[1] if len(path) > 1 else 'args'}' is not a declared parameter"  # noqa: E501
                    )
        if self.idempotency_header:
            if self.effect != "write":
                raise ValueError("idempotency_header only applies to write actions")
            if self.idempotency_header.lower() in FORBIDDEN_HEADERS:
                raise ValueError("idempotency_header is a reserved header name")
        if self.request is not None and self.request.method == "GET" and self.request.body is not None:
            raise ValueError("a GET request cannot have a body")
        return self


def to_json_schema(action: ActionDef) -> dict[str, Any]:
    """The model-visible schema of an action's arguments (strict: unknown keys are rejected)."""
    properties: dict[str, Any] = {}
    for name, param in action.parameters.items():
        spec: dict[str, Any] = {"type": param.type}
        if param.description:
            spec["description"] = param.description
        if param.enum is not None:
            spec["enum"] = param.enum
        if param.type == "string":
            if param.pattern:
                spec["pattern"] = param.pattern
            if param.min_length is not None:
                spec["minLength"] = param.min_length
            spec["maxLength"] = param.max_length or 2000
        if param.type in ("integer", "number"):
            if param.minimum is not None:
                spec["minimum"] = param.minimum
            if param.maximum is not None:
                spec["maximum"] = param.maximum
        properties[name] = spec
    return {
        "type": "object",
        "properties": properties,
        "required": [n for n, p in action.parameters.items() if p.required],
        "additionalProperties": False,
    }
