from __future__ import annotations

from urllib.parse import parse_qs

import pytest
import requests
import responses

from mcp_idosport.client import (
    BASE_URL,
    MAX_RESPONSE_BYTES,
    IdOAuthenticationError,
    IdOClient,
    IdOParseError,
    IdOUpstreamError,
    IdOValidationError,
    _resolve_login_action,
    normalize_date_range,
)


def authenticated_client() -> IdOClient:
    client = IdOClient("athlete@example.test", "literal-password")
    client._logged_in = True
    return client


def test_credentials_are_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IDO_USERNAME", raising=False)
    monkeypatch.delenv("IDO_PASSWORD", raising=False)
    with pytest.raises(IdOAuthenticationError, match="not configured"):
        IdOClient()


def test_close_resets_session() -> None:
    client = authenticated_client()
    client.close()
    assert client._logged_in is False


def test_response_size_is_bounded() -> None:
    response = requests.Response()
    response.headers["Content-Length"] = str(MAX_RESPONSE_BYTES + 1)
    response._content = b"small"
    with pytest.raises(IdOUpstreamError, match="too large"):
        IdOClient._check_response_size(response)

    response.headers["Content-Length"] = "not-a-number"
    response._content = b"x" * (MAX_RESPONSE_BYTES + 1)
    with pytest.raises(IdOUpstreamError, match="too large"):
        IdOClient._check_response_size(response)


def test_network_failures_are_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    client = authenticated_client()

    def fail(*_args: object, **_kwargs: object) -> requests.Response:
        raise requests.ConnectionError("details that must not escape")

    monkeypatch.setattr(client._session, "request", fail)
    with pytest.raises(IdOUpstreamError, match="could not be reached"):
        client._send(client._session, "GET", BASE_URL)


@pytest.mark.parametrize(
    "action",
    [
        "https://example.test/steal",
        "http://www.idosport.app/login",
        "https://www.idosport.app:444/login",
        "https://user:pass@www.idosport.app/login",
        "https://www.idosport.app:invalid/login",
    ],
)
def test_login_action_rejects_untrusted_destinations(action: str) -> None:
    html = f'<form action="{action}"></form>'
    with pytest.raises(IdOAuthenticationError, match="untrusted"):
        _resolve_login_action(f"{BASE_URL}/login", html)


def test_login_action_accepts_relative_destination() -> None:
    html = '<form action="/login_check"></form>'
    assert _resolve_login_action(f"{BASE_URL}/login", html) == (f"{BASE_URL}/login_check")


def test_normalize_date_range_fills_missing_boundary() -> None:
    assert normalize_date_range("2026-05-10", None) == ("2026-05-10", "2026-12-31")
    assert normalize_date_range(None, "2026-05-10") == ("2026-01-01", "2026-05-10")


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        ("2026/01/01", "2026-01-02", "YYYY-MM-DD"),
        ("2026-02-01", "2026-01-01", "on or before"),
        ("2025-01-01", "2026-01-03", "cannot exceed"),
    ],
)
def test_normalize_date_range_rejects_invalid_ranges(start: str, end: str, message: str) -> None:
    with pytest.raises(IdOValidationError, match=message):
        normalize_date_range(start, end)


@responses.activate
def test_get_events_requests_and_filters_planned_sessions() -> None:
    responses.post(
        f"{BASE_URL}/athlete/load-events",
        json=[
            {"id": 1, "type": "run", "start": "2026-01-02"},
            {"id": 2, "type": "activity", "start": "2026-01-04"},
            {"id": 3, "type": "bike", "start": "2026-01-03"},
        ],
    )
    client = authenticated_client()

    events = client.get_events("2026-01-01", "2026-01-31", limit=2)

    assert [event["id"] for event in events] == [3, 1]
    body = parse_qs(responses.calls[0].request.body)
    assert body == {
        "start": ["2026-01-01"],
        "end": ["2026-01-31"],
        "type": ["planned"],
    }


@responses.activate
def test_login_preserves_hidden_fields_and_verifies_session() -> None:
    responses.get(
        f"{BASE_URL}/login",
        body='<form action="/login_check"><input type="hidden" name="csrf" value="token"></form>',
        content_type="text/html",
    )
    responses.post(
        f"{BASE_URL}/login_check",
        body="<html>accepted</html>",
        content_type="text/html",
    )
    responses.get(
        f"{BASE_URL}/athlete/",
        body="<html>calendar</html>",
        content_type="text/html",
    )
    client = IdOClient("athlete@example.test", "p@ss word")

    client.login()

    assert client._logged_in is True
    posted = parse_qs(responses.calls[1].request.body)
    assert posted == {
        "csrf": ["token"],
        "_username": ["athlete@example.test"],
        "_password": ["p@ss word"],
    }


def test_get_events_rejects_unknown_event_type() -> None:
    client = authenticated_client()
    with pytest.raises(IdOValidationError, match="Unsupported event type"):
        client.get_events("2026-01-01", "2026-01-02", ["activity"])


@pytest.mark.parametrize("limit", [0, 201])
def test_get_events_rejects_unbounded_limits(limit: int) -> None:
    client = authenticated_client()
    with pytest.raises(IdOValidationError, match="between 1 and 200"):
        client.get_events("2026-01-01", "2026-01-02", limit=limit)


@responses.activate
def test_get_events_rejects_unexpected_payload() -> None:
    responses.post(f"{BASE_URL}/athlete/load-events", json={"events": []})
    client = authenticated_client()
    with pytest.raises(IdOParseError, match="data structure"):
        client.get_events("2026-01-01", "2026-01-02")


def test_modal_parser_attaches_note_to_preceding_step() -> None:
    html = """
    <div class="seance-resume">
      <span class="event-name">Tempo run</span>
      <span class="icon-sport icon-run"></span>
    </div>
    <div class="seance-build-show">
      <div class="interval-group">
        <div class="nb-repeats-left"><span>2x</span></div>
        <div class="intervals-list">
          <div class="interval-type">Work</div>
          <div class="interval"><span class="time">5 min</span></div>
          <div class="interval"><span class="distance">1 km</span></div>
          <div class="note">Controlled effort</div>
        </div>
      </div>
    </div>
    """

    parsed = IdOClient._parse_modal_html(html)

    assert parsed["title"] == "Tempo run"
    assert parsed["sport"] == "run"
    assert parsed["intervals"][0]["repeats"] == 2
    assert "note" not in parsed["intervals"][0]["steps"][0]
    assert parsed["intervals"][0]["steps"][1]["note"] == "Controlled effort"


def test_modal_parser_extracts_summary_fields() -> None:
    html = """
    <div class="seance-resume">
      <div class="info-time-distance">
        <div><label>Horaire</label>08:30</div>
        <div><label>Durée</label>45 min</div>
        <div><label>ICF</label>not-rated</div>
      </div>
      <div class="info-intensity"><span class="badge">Z3 <small>150 bpm</small></span></div>
    </div>
    <div class="s-short-description">
      Main description
      <span class="used-intensities">legend</span>
    </div>
    <div class="seance-build-show">
      <div class="interval-group">
        <h4>Main set</h4>
        <div class="nb-repeats-left"><span>invalid</span></div>
        <div class="intervals-list">
          <div class="ignored">skip</div>
          <div class="interval">
            <span class="bg-intensity4"></span>
            <span class="zi-block">Threshold</span>
          </div>
        </div>
      </div>
    </div>
    <div class="resume-build-seance">
      <div class="show-time-distance-per-intensities">
        <span class="badge bg-intensity4">10 min</span>
      </div>
    </div>
    """

    parsed = IdOClient._parse_modal_html(html)

    assert parsed["time"] == "08:30"
    assert parsed["duration"] == "45 min"
    assert parsed["icf"] == "not-rated"
    assert parsed["intensity_zone"] == "Z3"
    assert parsed["intensity_hr"] == "150 bpm"
    assert parsed["description"] == "Main description"
    assert parsed["intervals"][0]["name"] == "Main set"
    assert parsed["intervals"][0]["steps"][0]["zone"] == 4
    assert parsed["intervals"][0]["steps"][0]["zone_details"] == ["Threshold"]
    assert parsed["summary_by_zone"] == {"Z4": "10 min"}


def test_modal_parser_rejects_unrecognized_html() -> None:
    with pytest.raises(IdOParseError, match="did not match"):
        IdOClient._parse_modal_html("<div>Unexpected</div>")


@responses.activate
def test_event_plan_uses_only_the_planned_modal() -> None:
    responses.post(
        f"{BASE_URL}/calendrier-partage/show-event-modal",
        json={"modal": '<div class="seance-resume"><span class="event-name">Easy</span></div>'},
    )
    client = authenticated_client()

    result = client.get_event_plan("42")

    assert result["event_id"] == "42"
    assert result["plan"]["title"] == "Easy"
    assert len(responses.calls) == 1


@responses.activate
def test_event_plan_falls_back_to_alternate_identifier() -> None:
    url = f"{BASE_URL}/calendrier-partage/show-event-modal"
    responses.post(url, status=404)
    responses.post(
        url,
        json={"modal": '<div class="seance-resume"><span class="event-name">Easy</span></div>'},
    )
    client = authenticated_client()

    result = client.get_event_plan(42)

    assert result["event_id"] == "42"
    assert parse_qs(responses.calls[1].request.body)["id"] == ["42"]


def test_event_plan_rejects_invalid_identifier() -> None:
    client = authenticated_client()
    with pytest.raises(IdOValidationError, match="1 to 20 digits"):
        client.show_event_modal("../42")
