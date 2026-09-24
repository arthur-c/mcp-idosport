from __future__ import annotations

import json
from typing import Any

import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

from mcp_idosport import server
from mcp_idosport.client import IdOValidationError


class FakeClient:
    def __init__(self) -> None:
        self.calendar_calls = 0
        self.plan_calls = 0
        self.closed = False

    def get_events(
        self,
        start: str,
        end: str,
        event_types: list[str] | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        self.calendar_calls += 1
        return [{"id": 42, "type": "run", "start": start, "end": end}][:limit]

    def get_event_plan(self, event_id: str) -> dict[str, Any]:
        self.plan_calls += 1
        return {"event_id": event_id, "plan": {"title": "Tempo run"}}

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def fake_service(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    client = FakeClient()
    service = server.IdOService(lambda: client, cache_ttl=30)
    monkeypatch.setattr(server, "service", service)
    return client


def test_service_reuses_session_and_cache(fake_service: FakeClient) -> None:
    first = server.service.calendar("2026-01-01", "2026-01-31", ["run"], 50)
    second = server.service.calendar("2026-01-01", "2026-01-31", ["run"], 50)

    assert first == second
    assert fake_service.calendar_calls == 1

    server.service.close()
    assert fake_service.closed is True


@pytest.mark.anyio
async def test_server_lists_and_calls_namespaced_tools(fake_service: FakeClient) -> None:
    async with Client(server.mcp) as client:
        tools = await client.list_tools()
        names = {tool.name for tool in tools.tools}
        result = await client.call_tool(
            "ido_get_calendar",
            {"start": "2026-01-01", "end": "2026-01-31", "types": ["run"]},
        )
        plan = await client.call_tool("ido_get_event_plan", {"event_id": "42"})

    assert names == {"ido_get_calendar", "ido_get_event_plan"}
    assert result.is_error is False
    assert result.structured_content == {
        "start": "2026-01-01",
        "end": "2026-01-31",
        "count": 1,
        "events": [
            {
                "id": 42,
                "type": "run",
                "start": "2026-01-01",
                "end": "2026-01-31",
            }
        ],
    }
    assert plan.structured_content == {
        "event_id": "42",
        "plan": {"title": "Tempo run"},
    }


@pytest.mark.anyio
async def test_server_rejects_invalid_event_id(fake_service: FakeClient) -> None:
    async with Client(server.mcp) as client:
        result = await client.call_tool("ido_get_event_plan", {"event_id": "../42"})

    assert result.is_error is True
    assert fake_service.plan_calls == 0


@pytest.mark.anyio
async def test_health_and_readiness(monkeypatch: pytest.MonkeyPatch) -> None:
    health = await server.healthz(None)  # type: ignore[arg-type]
    assert health.status_code == 200

    monkeypatch.delenv("IDO_USERNAME", raising=False)
    monkeypatch.delenv("IDO_PASSWORD", raising=False)
    not_ready = await server.readyz(None)  # type: ignore[arg-type]
    assert not_ready.status_code == 503
    assert json.loads(not_ready.body) == {"status": "credentials_not_configured"}

    monkeypatch.setenv("IDO_USERNAME", "athlete@example.test")
    monkeypatch.setenv("IDO_PASSWORD", "literal-password")
    ready = await server.readyz(None)  # type: ignore[arg-type]
    assert ready.status_code == 200


def test_tool_errors_are_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingService:
        def calendar(self, *_args: object, **_kwargs: object) -> None:
            raise IdOValidationError("safe validation message")

    monkeypatch.setattr(server, "service", FailingService())
    with pytest.raises(ToolError, match="safe validation message"):
        server.ido_get_calendar()


def test_runtime_settings_are_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_INTEGER", "42")
    assert server._integer_setting("TEST_INTEGER", 1, 1, 100) == 42

    monkeypatch.setenv("TEST_INTEGER", "invalid")
    with pytest.raises(ValueError, match="integer"):
        server._integer_setting("TEST_INTEGER", 1, 1, 100)

    monkeypatch.setenv("TEST_INTEGER", "101")
    with pytest.raises(ValueError, match="between"):
        server._integer_setting("TEST_INTEGER", 1, 1, 100)

    monkeypatch.setenv("IDO_CACHE_TTL_SECONDS", "invalid")
    assert server._cache_ttl() == 30


def test_transport_security_allowlists_hosts_and_origins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "gateway.example.test, service:8000")
    monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "https://client.example.test")
    settings = server._transport_security()
    assert settings.allowed_hosts == ["gateway.example.test", "service:8000"]
    assert settings.allowed_origins == ["https://client.example.test"]

    monkeypatch.setenv("MCP_ALLOWED_HOSTS", " , ")
    with pytest.raises(ValueError, match="at least one host"):
        server._transport_security()


def test_main_runs_stdio_and_closes_service(
    monkeypatch: pytest.MonkeyPatch, fake_service: FakeClient
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_run(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(server.mcp, "run", fake_run)
    monkeypatch.setenv("MCP_TRANSPORT", "stdio")

    assert server.main() == 0
    assert calls == [{"transport": "stdio"}]


def test_main_runs_hardened_http_transport(
    monkeypatch: pytest.MonkeyPatch, fake_service: FakeClient
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_run(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(server.mcp, "run", fake_run)
    monkeypatch.setenv("MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("MCP_PORT", "8123")
    monkeypatch.setenv("MCP_PATH", "/mcp")

    assert server.main() == 0
    assert calls[0]["transport"] == "streamable-http"
    assert calls[0]["host"] == "0.0.0.0"
    assert calls[0]["port"] == 8123
    assert calls[0]["streamable_http_path"] == "/mcp"
    assert calls[0]["stateless_http"] is True
    assert calls[0]["max_request_body_size"] == 1024 * 1024


@pytest.mark.parametrize(
    ("transport", "path"),
    [("websocket", "/mcp"), ("streamable-http", "relative")],
)
def test_main_reports_invalid_configuration(
    monkeypatch: pytest.MonkeyPatch,
    fake_service: FakeClient,
    transport: str,
    path: str,
) -> None:
    monkeypatch.setenv("MCP_TRANSPORT", transport)
    monkeypatch.setenv("MCP_PATH", path)
    assert server.main() == 2
