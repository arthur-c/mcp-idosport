"""MCP server exposing read-only planned calendar tools."""

from __future__ import annotations

import copy
import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Annotated, Any, Literal

from dotenv import load_dotenv
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import __version__
from .client import IdOClient, IdOClientError, normalize_date_range

load_dotenv(interpolate=False)

logger = logging.getLogger("mcp-idosport")

EventType = Literal["swim", "run", "bike", "race", "autre", "ppg"]
DateValue = Annotated[str | None, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]
LimitValue = Annotated[int, Field(ge=1, le=200)]
EventId = Annotated[str, Field(pattern=r"^[0-9]{1,20}$")]


class CalendarResult(BaseModel):
    """Structured result for a planned calendar query."""

    start: str
    end: str
    count: int
    events: list[dict[str, Any]]


class EventPlanResult(BaseModel):
    """Structured planned-session result."""

    event_id: str
    plan: dict[str, Any]


class IdOService:
    """Serialize access to one authenticated session and cache short-lived reads."""

    def __init__(
        self,
        client_factory: Callable[[], IdOClient] = IdOClient,
        *,
        cache_ttl: float = 30.0,
    ) -> None:
        self._client_factory = client_factory
        self._client: IdOClient | None = None
        self._cache_ttl = max(0.0, min(cache_ttl, 300.0))
        self._cache: dict[tuple[Any, ...], tuple[float, Any]] = {}
        self._lock = threading.RLock()

    def _get_client(self) -> IdOClient:
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def _cached(self, key: tuple[Any, ...], loader: Callable[[], Any]) -> Any:
        with self._lock:
            now = time.monotonic()
            cached = self._cache.get(key)
            if cached and cached[0] > now:
                return copy.deepcopy(cached[1])
            value = loader()
            self._cache[key] = (now + self._cache_ttl, copy.deepcopy(value))
            return value

    def calendar(
        self,
        start: str | None,
        end: str | None,
        event_types: list[str] | None,
        limit: int,
    ) -> CalendarResult:
        normalized_start, normalized_end = normalize_date_range(start, end)
        normalized_types = tuple(sorted(event_types or []))
        key = ("calendar", normalized_start, normalized_end, normalized_types, limit)

        def load() -> CalendarResult:
            events = self._get_client().get_events(
                normalized_start,
                normalized_end,
                list(normalized_types) or None,
                limit,
            )
            return CalendarResult(
                start=normalized_start,
                end=normalized_end,
                count=len(events),
                events=events,
            )

        return self._cached(key, load)

    def event_plan(self, event_id: str) -> EventPlanResult:
        key = ("event-plan", event_id)

        def load() -> EventPlanResult:
            result = self._get_client().get_event_plan(event_id)
            return EventPlanResult.model_validate(result)

        return self._cached(key, load)

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None
            self._cache.clear()


def _cache_ttl() -> float:
    raw = os.environ.get("IDO_CACHE_TTL_SECONDS", "30")
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid IDO_CACHE_TTL_SECONDS; using 30 seconds.")
        return 30.0


service = IdOService(cache_ttl=_cache_ttl())

mcp = MCPServer(
    name="mcp-idosport",
    title="iDO Sport Planned Sessions",
    description="Read-only access to planned calendar sessions and training plans.",
    instructions="Only planned sessions are exposed. No completed activities are returned.",
    version=__version__,
)

READ_ONLY_ANNOTATIONS = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)


def _tool_failure(exc: IdOClientError) -> ToolError:
    logger.info("Tool request failed: %s", exc.__class__.__name__)
    return ToolError(str(exc))


@mcp.tool(
    name="ido_get_calendar",
    title="Get planned iDO calendar sessions",
    annotations=READ_ONLY_ANNOTATIONS,
    structured_output=True,
)
def ido_get_calendar(
    start: DateValue = None,
    end: DateValue = None,
    types: list[EventType] | None = None,
    limit: LimitValue = 50,
) -> CalendarResult:
    """Return newest-first planned sessions for a validated date range."""
    try:
        return service.calendar(start, end, list(types) if types else None, limit)
    except IdOClientError as exc:
        raise _tool_failure(exc) from exc


@mcp.tool(
    name="ido_get_event_plan",
    title="Get a planned iDO session",
    annotations=READ_ONLY_ANNOTATIONS,
    structured_output=True,
)
def ido_get_event_plan(event_id: EventId) -> EventPlanResult:
    """Return the structured plan for an event returned by ido_get_calendar."""
    try:
        return service.event_plan(event_id)
    except IdOClientError as exc:
        raise _tool_failure(exc) from exc


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_request: Request) -> Response:
    return JSONResponse({"status": "ok", "version": __version__})


@mcp.custom_route("/readyz", methods=["GET"])
async def readyz(_request: Request) -> Response:
    configured = bool(os.environ.get("IDO_USERNAME") and os.environ.get("IDO_PASSWORD"))
    status_code = 200 if configured else 503
    status = "ready" if configured else "credentials_not_configured"
    return JSONResponse({"status": status}, status_code=status_code)


def _integer_setting(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer.") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}.")
    return value


def _transport_security() -> TransportSecuritySettings:
    raw_hosts = os.environ.get(
        "MCP_ALLOWED_HOSTS",
        "localhost,localhost:*,127.0.0.1,127.0.0.1:*",
    )
    allowed_hosts = [host.strip() for host in raw_hosts.split(",") if host.strip()]
    if not allowed_hosts:
        raise ValueError("MCP_ALLOWED_HOSTS must contain at least one host in HTTP mode.")
    raw_origins = os.environ.get("MCP_ALLOWED_ORIGINS", "")
    allowed_origins = [origin.strip() for origin in raw_origins.split(",") if origin.strip()]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    transport = os.environ.get("MCP_TRANSPORT", "stdio").strip().lower()
    try:
        if transport == "stdio":
            mcp.run(transport="stdio")
            return 0
        if transport != "streamable-http":
            raise ValueError("MCP_TRANSPORT must be 'stdio' or 'streamable-http'.")

        host = os.environ.get("MCP_HOST", "127.0.0.1")
        port = _integer_setting("MCP_PORT", 8000, 1, 65535)
        path = os.environ.get("MCP_PATH", "/mcp")
        if not path.startswith("/") or ".." in path:
            raise ValueError("MCP_PATH must be an absolute path without '..'.")

        mcp.run(
            transport="streamable-http",
            host=host,
            port=port,
            streamable_http_path=path,
            json_response=True,
            stateless_http=True,
            max_request_body_size=1024 * 1024,
            transport_security=_transport_security(),
        )
        return 0
    except (OSError, ValueError) as exc:
        logger.error("Server configuration error: %s", exc)
        return 2
    finally:
        service.close()


if __name__ == "__main__":
    raise SystemExit(main())
