"""HTTP client for read-only access to planned iDO Sport sessions."""

from __future__ import annotations

import logging
import os
import re
import threading
from contextlib import suppress
from datetime import date
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger("mcp-idosport.client")

BASE_URL = "https://www.idosport.app"
LOGIN_URL = f"{BASE_URL}/login"
ATHLETE_URL = f"{BASE_URL}/athlete/"
PLANNED_EVENT_TYPES = frozenset({"swim", "run", "bike", "race", "autre", "ppg"})
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
DEFAULT_TIMEOUT = (5, 30)

COMMON_HEADERS = {
    "User-Agent": "mcp-idosport/0.2",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fr,en;q=0.9",
    "Origin": BASE_URL,
    "Referer": ATHLETE_URL,
}
XHR_HEADERS = {
    **COMMON_HEADERS,
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}


class IdOClientError(Exception):
    """Base error for anticipated client failures."""


class IdOAuthenticationError(IdOClientError):
    """Authentication failed or an authenticated session expired."""


class IdOValidationError(IdOClientError):
    """A caller supplied an invalid value."""


class IdOUpstreamError(IdOClientError):
    """The upstream service could not complete a request."""


class IdOParseError(IdOClientError):
    """The upstream response did not match the expected format."""


def _validate_same_origin(url: str, expected_origin: str = BASE_URL) -> str:
    parsed = urlparse(url)
    expected = urlparse(expected_origin)
    if parsed.scheme != "https" or parsed.hostname != expected.hostname:
        raise IdOAuthenticationError("The login form returned an untrusted destination.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise IdOAuthenticationError("The login form returned an untrusted destination.") from exc
    if port not in (None, 443):
        raise IdOAuthenticationError("The login form returned an untrusted destination.")
    if parsed.username or parsed.password:
        raise IdOAuthenticationError("The login form returned an untrusted destination.")
    return url


def _resolve_login_action(login_url: str, html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    form = soup.select_one("form")
    action = form.get("action") if form else None
    destination = urljoin(login_url, str(action or "/login"))
    return _validate_same_origin(destination)


def _parse_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise IdOValidationError(f"{field} must use YYYY-MM-DD format.") from exc


def normalize_date_range(start: str | None, end: str | None) -> tuple[str, str]:
    """Validate a date range and fill a missing boundary with that year's limit."""
    current_year = date.today().year
    parsed_start = _parse_date(start, "start") if start else None
    parsed_end = _parse_date(end, "end") if end else None

    if parsed_start is None:
        parsed_start = date(parsed_end.year if parsed_end else current_year, 1, 1)
    if parsed_end is None:
        parsed_end = date(parsed_start.year if parsed_start else current_year, 12, 31)
    if parsed_start > parsed_end:
        raise IdOValidationError("start must be on or before end.")
    if (parsed_end - parsed_start).days > 366:
        raise IdOValidationError("The requested date range cannot exceed 367 days.")
    return parsed_start.isoformat(), parsed_end.isoformat()


class IdOClient:
    """Session-aware, read-only client for planned calendar data."""

    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        *,
        timeout: tuple[int, int] = DEFAULT_TIMEOUT,
    ) -> None:
        env_username = os.environ.get("IDO_USERNAME", "")
        env_password = os.environ.get("IDO_PASSWORD", "")
        self.username = (username if username is not None else env_username).strip()
        self.password = password if password is not None else env_password
        if not self.username or not self.password:
            raise IdOAuthenticationError("IDO credentials are not configured.")

        self.timeout = timeout
        self._session = self._new_session()
        self._logged_in = False
        self._lock = threading.RLock()

    @staticmethod
    def _new_session() -> requests.Session:
        session = requests.Session()
        session.headers.update(COMMON_HEADERS)
        return session

    def close(self) -> None:
        with self._lock:
            self._session.close()
            self._logged_in = False

    @staticmethod
    def _check_response_size(response: requests.Response) -> None:
        header = response.headers.get("Content-Length")
        if header:
            try:
                if int(header) > MAX_RESPONSE_BYTES:
                    raise IdOUpstreamError("The upstream response was too large.")
            except ValueError:
                pass
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise IdOUpstreamError("The upstream response was too large.")

    def _send(
        self,
        session: requests.Session,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        try:
            response = session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            raise IdOUpstreamError("The upstream service could not be reached.") from exc
        self._check_response_size(response)
        return response

    @staticmethod
    def _looks_like_login(response: requests.Response) -> bool:
        path = urlparse(response.url).path.rstrip("/")
        if path == "/login":
            return True
        content_type = response.headers.get("Content-Type", "").lower()
        return "text/html" in content_type and 'type="password"' in response.text.lower()

    @staticmethod
    def _hidden_fields(html: str) -> dict[str, str]:
        soup = BeautifulSoup(html, "html.parser")
        result: dict[str, str] = {}
        for field in soup.select('input[type="hidden"][name]'):
            name = field.get("name")
            value = field.get("value", "")
            if isinstance(name, str) and isinstance(value, str):
                result[name] = value
        return result

    def login(self) -> None:
        """Authenticate and retain a verified athlete session."""
        with self._lock:
            field_sets = [
                ("_username", "_password"),
                ("email", "password"),
            ]
            last_failure = "unsupported login form"

            for username_field, password_field in field_sets:
                session = self._new_session()
                page = self._send(session, "GET", LOGIN_URL)
                if page.status_code >= 400:
                    session.close()
                    raise IdOUpstreamError(f"The login page returned HTTP {page.status_code}.")

                post_url = _resolve_login_action(page.url, page.text)
                data = self._hidden_fields(page.text)
                data[username_field] = self.username
                data[password_field] = self.password

                response = self._send(
                    session,
                    "POST",
                    post_url,
                    data=data,
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Referer": LOGIN_URL,
                        "Origin": BASE_URL,
                        "Accept": (
                            "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
                        ),
                    },
                    allow_redirects=True,
                )
                _validate_same_origin(response.url)
                if response.status_code >= 500:
                    last_failure = f"HTTP {response.status_code}"
                    session.close()
                    continue
                if response.status_code >= 400 or self._looks_like_login(response):
                    last_failure = "credentials were rejected"
                    session.close()
                    continue

                verification = self._send(
                    session,
                    "GET",
                    ATHLETE_URL,
                    headers=COMMON_HEADERS,
                    allow_redirects=True,
                )
                _validate_same_origin(verification.url)
                if verification.status_code >= 400 or self._looks_like_login(verification):
                    last_failure = "authenticated session could not be verified"
                    session.close()
                    continue

                self._session.close()
                self._session = session
                self._logged_in = True
                logger.info("Authenticated to iDO Sport.")
                return

            raise IdOAuthenticationError(f"Authentication failed: {last_failure}.")

    def _request_authenticated(
        self,
        method: str,
        url: str,
        *,
        allow_http_errors: bool = False,
        **kwargs: Any,
    ) -> requests.Response:
        for attempt in range(2):
            if not self._logged_in:
                self.login()
            response = self._send(self._session, method, url, **kwargs)
            if response.status_code in (401, 403) or self._looks_like_login(response):
                self._logged_in = False
                if attempt == 0:
                    continue
                raise IdOAuthenticationError("The authenticated session was rejected.")
            if response.status_code >= 400 and not allow_http_errors:
                raise IdOUpstreamError(
                    f"The upstream service returned HTTP {response.status_code}."
                )
            return response
        raise IdOAuthenticationError("The authenticated session was rejected.")

    @staticmethod
    def _json(response: requests.Response, context: str) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise IdOParseError(f"{context} returned an unexpected response format.") from exc

    def get_events(
        self,
        start: str | None = None,
        end: str | None = None,
        event_types: list[str] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return a bounded, newest-first list of planned sessions."""
        normalized_start, normalized_end = normalize_date_range(start, end)
        if not 1 <= limit <= 200:
            raise IdOValidationError("limit must be between 1 and 200.")

        requested_types = {value.casefold() for value in event_types or PLANNED_EVENT_TYPES}
        unknown_types = requested_types - PLANNED_EVENT_TYPES
        if unknown_types:
            names = ", ".join(sorted(unknown_types))
            raise IdOValidationError(f"Unsupported event type(s): {names}.")

        with self._lock:
            response = self._request_authenticated(
                "POST",
                f"{BASE_URL}/athlete/load-events",
                data={
                    "start": normalized_start,
                    "end": normalized_end,
                    "type": "planned",
                },
                headers=XHR_HEADERS,
            )
            payload = self._json(response, "load-events")

        if not isinstance(payload, list):
            raise IdOParseError("load-events returned an unexpected data structure.")

        events = [
            event
            for event in payload
            if isinstance(event, dict) and str(event.get("type", "")).casefold() in requested_types
        ]
        events.sort(
            key=lambda event: str(
                event.get("start") or event.get("date") or event.get("startDate") or ""
            ),
            reverse=True,
        )
        return events[:limit]

    def show_event_modal(self, event_id: str | int) -> dict[str, Any]:
        """Load and parse the planned-session modal."""
        normalized_id = str(event_id)
        if not re.fullmatch(r"[0-9]{1,20}", normalized_id):
            raise IdOValidationError("event_id must contain 1 to 20 digits.")

        with self._lock:
            url = f"{BASE_URL}/calendrier-partage/show-event-modal"
            response = self._request_authenticated(
                "POST",
                url,
                data={"event": normalized_id, "type": "sharecalevent"},
                headers=XHR_HEADERS,
                allow_http_errors=True,
            )
            if response.status_code in {400, 404, 422, 500}:
                response = self._request_authenticated(
                    "POST",
                    url,
                    data={"id": normalized_id, "type": "sharecalevent"},
                    headers=XHR_HEADERS,
                    allow_http_errors=True,
                )
            if response.status_code >= 400:
                raise IdOUpstreamError(f"The event endpoint returned HTTP {response.status_code}.")
            payload = self._json(response, "show-event-modal")

        if not isinstance(payload, dict):
            raise IdOParseError("show-event-modal returned an unexpected data structure.")
        modal_html = payload.get("modal")
        if not isinstance(modal_html, str) or not modal_html.strip():
            raise IdOParseError("The planned session did not contain a readable plan.")
        return self._parse_modal_html(modal_html)

    @staticmethod
    def _parse_modal_html(html: str) -> dict[str, Any]:
        """Parse planned-session HTML into a compact structured representation."""
        soup = BeautifulSoup(html, "html.parser")
        result: dict[str, Any] = {}

        resume = soup.select_one(".seance-resume")
        if resume:
            name_el = resume.select_one(".event-name")
            if name_el:
                result["title"] = name_el.get_text(strip=True)

            icon_el = resume.select_one("[class*='icon-sport']")
            if icon_el:
                for cls in icon_el.get("class", []):
                    if cls.startswith("icon-") and cls != "icon-sport":
                        result["sport"] = cls.removeprefix("icon-")
                        break

            for div in resume.select(".info-time-distance > div"):
                label_el = div.select_one("label")
                if not label_el:
                    continue
                raw_label = label_el.get_text(strip=True)
                label = raw_label.casefold()
                value = div.get_text(strip=True).replace(raw_label, "").strip()
                if "horaire" in label:
                    result["time"] = value
                elif "durée" in label or "duree" in label:
                    result["duration"] = value
                elif label in {"icf", "ic"}:
                    try:
                        result["icf"] = int(value)
                    except ValueError:
                        result["icf"] = value

            zone_el = resume.select_one(".info-intensity .badge")
            if zone_el:
                zone_text = zone_el.get_text(" ", strip=True)
                zone_match = re.match(r"(Z\d+)", zone_text)
                if zone_match:
                    result["intensity_zone"] = zone_match.group(1)
                hr_el = zone_el.select_one("small")
                if hr_el:
                    result["intensity_hr"] = hr_el.get_text(strip=True)

        desc_el = soup.select_one(".s-short-description")
        if desc_el:
            used = desc_el.select_one(".used-intensities")
            if used:
                used.decompose()
            description = desc_el.get_text("\n", strip=True)
            if description:
                result["description"] = description

        intervals: list[dict[str, Any]] = []
        for group in soup.select(".seance-build-show .interval-group"):
            repeats_el = group.select_one(".nb-repeats-left span")
            repeats = 1
            if repeats_el:
                with suppress(ValueError):
                    repeats = int(repeats_el.get_text(strip=True).casefold().replace("x", ""))

            group_data: dict[str, Any] = {"repeats": repeats}
            heading = group.select_one("h4")
            if heading and heading.get_text(strip=True):
                group_data["name"] = heading.get_text(strip=True)

            steps: list[dict[str, Any]] = []
            intervals_list = group.select_one(".intervals-list")
            if intervals_list:
                current_type = ""
                for child in intervals_list.children:
                    if not hasattr(child, "get"):
                        continue
                    classes = child.get("class", [])
                    if "interval-type" in classes:
                        current_type = child.get_text(strip=True)
                        continue
                    if "note" in classes:
                        if steps:
                            note = child.get_text(strip=True)
                            if note:
                                steps[-1]["note"] = note
                        continue
                    if "interval" not in classes:
                        continue

                    step: dict[str, Any] = {"type": current_type}
                    time_el = child.select_one(".time")
                    if time_el:
                        step["duration"] = time_el.get_text(strip=True)
                    distance_el = child.select_one(".distance")
                    if distance_el:
                        step["distance"] = distance_el.get_text(strip=True)

                    intensity_el = child.select_one("[class*='bg-intensity']")
                    if intensity_el:
                        for cls in intensity_el.get("class", []):
                            match = re.match(r"bg-intensity(\d+)", cls)
                            if match:
                                step["zone"] = int(match.group(1))
                                break

                    details = [
                        block.get_text(strip=True)
                        for block in child.select(".zi-block")
                        if block.get_text(strip=True)
                    ]
                    if details:
                        step["zone_details"] = details
                    steps.append(step)

            group_data["steps"] = steps
            intervals.append(group_data)

        if intervals:
            result["intervals"] = intervals

        summary = soup.select_one(".resume-build-seance")
        if summary:
            time_per_zone: dict[str, str] = {}
            for badge in summary.select(".show-time-distance-per-intensities .badge"):
                for cls in badge.get("class", []):
                    match = re.match(r"bg-intensity(\d+)", cls)
                    if match:
                        time_per_zone[f"Z{int(match.group(1))}"] = badge.get_text(strip=True)
                        break
            if time_per_zone:
                result["summary_by_zone"] = time_per_zone

            icf_el = summary.select_one(".icf strong")
            if icf_el and "icf" not in result:
                with suppress(ValueError):
                    result["icf"] = int(icf_el.get_text(strip=True))

        if not result:
            raise IdOParseError("The planned session HTML did not match the expected format.")
        return result

    def get_event_plan(self, event_id: str | int) -> dict[str, Any]:
        """Return only the planned-session representation for an event."""
        return {"event_id": str(event_id), "plan": self.show_event_modal(event_id)}
