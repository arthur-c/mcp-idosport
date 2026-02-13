"""
IDO Sport app HTTP client — read-only, séances prévues uniquement.
Accès au calendrier et aux plans de séances prévues (pas aux activités réalisées).
Uses session cookies; credentials from environment, never hardcoded.
"""

import logging
import os
import re
from typing import Any

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger("ido-client")

BASE_URL = "https://www.idosport.app"
LOGIN_URL = f"{BASE_URL}/login"
ATHLETE_URL = f"{BASE_URL}/athlete/"

# Headers that mimic the web app (XHR from athlete page)
COMMON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fr,en;q=0.9",
    "Origin": BASE_URL,
    "Referer": f"{ATHLETE_URL}",
}
XHR_HEADERS = {
    **COMMON_HEADERS,
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}


class IdOClientError(Exception):
    """Raised when IDO client fails (auth, API error)."""
    pass


class IdOClient:
    """Read-only client for IDO Sport: login, load events, load event plan."""

    def __init__(self, username: str | None = None, password: str | None = None):
        self.username = username or os.environ.get("IDO_USERNAME", "").strip()
        self.password = password or os.environ.get("IDO_PASSWORD", "").strip()
        if not self.username or not self.password:
            raise IdOClientError(
                "IDO_USERNAME and IDO_PASSWORD must be set (env or .env file)."
            )
        self._session = requests.Session()
        self._session.headers.update(COMMON_HEADERS)
        self._logged_in = False

    def _ensure_logged_in(self) -> None:
        if not self._logged_in:
            self.login()

    def login(self) -> None:
        """Authenticate and keep session cookies (PHPSESSID, REMEMBERME)."""
        # Try each field set with a fresh session each time (a failed POST can corrupt session/CSRF)
        field_sets = [
            {"_username": self.username, "_password": self.password},
            {"email": self.username, "password": self.password},
        ]
        last_error = None
        for field_set in field_sets:
            field_names = list(field_set.keys())
            logger.info("Login attempt with fields: %s", field_names)

            # Fresh session for each attempt
            self._session = requests.Session()
            self._session.headers.update(COMMON_HEADERS)

            # GET login page to obtain cookies and optional CSRF
            r = self._session.get(LOGIN_URL, timeout=15)
            r.raise_for_status()
            logger.info("  GET %s → %s, cookies: %s", LOGIN_URL, r.status_code, list(self._session.cookies.get_dict().keys()))

            # Try to find CSRF token
            csrf = None
            if "_csrf_token" in r.text:
                m = re.search(r'name="_csrf_token"[^>]*value="([^"]+)"', r.text)
                if m:
                    csrf = m.group(1)
            if not csrf and 'csrf' in r.text.lower():
                m = re.search(r'name="[^"]*csrf[^"]*"[^>]*value="([^"]+)"', r.text, re.I)
                if m:
                    csrf = m.group(1)
            logger.info("  CSRF token: %s", "found" if csrf else "none")

            # Resolve form action
            post_url = LOGIN_URL
            form_action = re.search(r'<form[^>]*action="([^"]+)"', r.text, re.I)
            if form_action:
                action = form_action.group(1).strip()
                if action and not action.startswith("#"):
                    if action.startswith("/"):
                        post_url = f"{BASE_URL}{action}"
                    elif action.startswith("http"):
                        post_url = action
            logger.info("  POST URL: %s", post_url)

            data = dict(field_set)
            if csrf:
                data["_csrf_token"] = csrf

            post_headers = {
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": LOGIN_URL,
                "Origin": BASE_URL,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
            r2 = self._session.post(
                post_url,
                data=data,
                headers=post_headers,
                allow_redirects=True,
                timeout=15,
            )
            logger.info("  POST → status=%s, final_url=%s, cookies=%s",
                         r2.status_code, r2.url, list(self._session.cookies.get_dict().keys()))

            if r2.status_code == 500:
                logger.info("  → 500, trying next field set")
                last_error = "Server returned 500 with fields %s" % field_names
                continue
            r2.raise_for_status()

            # Check if we landed back on login page
            has_password_field = 'type="password"' in r2.text.lower()
            if has_password_field:
                logger.info("  → Still on login page (password field detected)")
                last_error = "Bad credentials with fields %s" % field_names
                continue  # try next field set instead of raising

            if "PHPSESSID" not in self._session.cookies.get_dict():
                logger.info("  → No PHPSESSID cookie")
                last_error = "No session cookie with fields %s" % field_names
                continue

            logger.info("  → Login OK!")
            self._logged_in = True
            return

        raise IdOClientError("Login failed: %s" % last_error)

    def get_events(
        self,
        start: str | None = None,
        end: str | None = None,
        planned_only: bool = True,
        event_types: list[str] | None = None,
        limit: int | None = None,
    ) -> Any:
        """
        Load calendar events (read-only).
        start/end: optional date bounds YYYY-MM-DD; default current year.
        planned_only: if True, filter out type='activity' (keep only séances prévues).
        event_types: optional list of types to keep (e.g. ['run', 'bike']). Applied after planned_only.
        limit: optional max number of events to return (most recent first).
        Returns list of events.
        """
        self._ensure_logged_in()
        url = f"{BASE_URL}/athlete/load-events"
        if start and end:
            body = {"start": start, "end": end}
        else:
            from datetime import date
            y = date.today().year
            body = {"start": f"{y}-01-01", "end": f"{y}-12-31"}
        r = self._session.post(
            url,
            data=body,
            headers=XHR_HEADERS,
            timeout=30,
        )
        r.raise_for_status()
        text = r.text.strip()
        if not text:
            raise IdOClientError(
                "load-events returned empty body (status=%s)." % r.status_code
            )
        try:
            data = r.json()
        except ValueError as e:
            snippet = text[:300] if len(text) >= 300 else text
            raise IdOClientError(
                "load-events returned non-JSON (status=%s): %s... [%s]"
                % (r.status_code, snippet, e)
            )
        if not isinstance(data, list):
            return data
        # Filter: séances prévues only (exclude type='activity' = activités réalisées)
        if planned_only:
            data = [ev for ev in data if isinstance(ev, dict) and ev.get("type") != "activity"]
        # Filter by event types (e.g. ['run', 'bike'])
        if event_types:
            allowed = {t.lower() for t in event_types}
            data = [ev for ev in data if isinstance(ev, dict) and ev.get("type", "").lower() in allowed]
        # Limit number of results (keep the last N = most recent)
        if limit and limit > 0 and len(data) > limit:
            data = data[-limit:]
        return data

    def show_event_modal(self, event_id: str | int) -> dict[str, Any]:
        """Open event modal for a séance prévue (POST). Returns parsed structured data.

        The endpoint returns JSON like {"modal": "<html>"}.
        We extract the HTML and parse it to get session info,
        structured intervals, zones and description.
        """
        self._ensure_logged_in()
        url = f"{BASE_URL}/calendrier-partage/show-event-modal"
        # The site expects event=<id>&type=sharecalevent (evidenced by data-event attrs and JS calls)
        body = {"event": str(event_id), "type": "sharecalevent"}
        r = self._session.post(url, data=body, headers=XHR_HEADERS, timeout=15)
        if r.status_code == 500:
            # Fallback: try with "id" instead of "event"
            logger.info("show-event-modal: 500 with event=%s, retrying with id=", event_id)
            body = {"id": str(event_id), "type": "sharecalevent"}
            r = self._session.post(url, data=body, headers=XHR_HEADERS, timeout=15)
        r.raise_for_status()
        # Response is JSON: {"modal": "<html>"}
        data = r.json()
        modal_html = data.get("modal", "")
        if not modal_html:
            logger.warning("show-event-modal returned empty modal HTML for event %s", event_id)
            return {}
        return self._parse_modal_html(modal_html)

    # ------------------------------------------------------------------
    # HTML parsing for the show-event-modal response
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_modal_html(html: str) -> dict[str, Any]:
        """Parse the HTML returned by show-event-modal into structured data.

        Extracts session info, structured intervals with zones, and summary.
        """
        soup = BeautifulSoup(html, "html.parser")
        result: dict[str, Any] = {}

        # --- Session info from .seance-resume ---
        resume = soup.select_one(".seance-resume")
        if resume:
            # Title
            name_el = resume.select_one(".event-name")
            if name_el:
                result["title"] = name_el.get_text(strip=True)

            # Sport (from icon class like icon-run, icon-swim, icon-bike)
            icon_el = resume.select_one("[class*='icon-sport']")
            if icon_el:
                for cls in icon_el.get("class", []):
                    if cls.startswith("icon-") and cls != "icon-sport":
                        result["sport"] = cls.replace("icon-", "")
                        break

            # Duration, time, ICF from .info-time-distance
            for div in resume.select(".info-time-distance > div"):
                label_el = div.select_one("label")
                if not label_el:
                    continue
                label = label_el.get_text(strip=True).lower()
                # Remove label text from the div to get the value
                value = div.get_text(strip=True).replace(label_el.get_text(strip=True), "").strip()
                if "horaire" in label:
                    result["time"] = value
                elif "durée" in label or "duree" in label:
                    result["duration"] = value
                elif "ic" in label:
                    try:
                        result["icf"] = int(value)
                    except ValueError:
                        result["icf"] = value

            # Intensity zone from .info-intensity .badge
            zone_el = resume.select_one(".info-intensity .badge")
            if zone_el:
                zone_text = zone_el.get_text(" ", strip=True)
                # Extract zone name (e.g. "Z4")
                zone_match = re.match(r"(Z\d+)", zone_text)
                if zone_match:
                    result["intensity_zone"] = zone_match.group(1)
                # Extract HR range
                hr_el = zone_el.select_one("small")
                if hr_el:
                    result["intensity_hr"] = hr_el.get_text(strip=True)

        # --- Description / series from .s-short-description ---
        desc_el = soup.select_one(".s-short-description")
        if desc_el:
            # Remove the .used-intensities block before extracting text
            used = desc_el.select_one(".used-intensities")
            if used:
                used.decompose()
            desc_text = desc_el.get_text("\n", strip=True)
            if desc_text:
                result["description"] = desc_text

        # --- Structured intervals from .interval-group ---
        interval_groups = soup.select(".seance-build-show .interval-group")
        intervals: list[dict[str, Any]] = []
        for group in interval_groups:
            group_data: dict[str, Any] = {}

            # Repeats
            repeats_el = group.select_one(".nb-repeats-left span")
            if repeats_el:
                repeats_text = repeats_el.get_text(strip=True)  # e.g. "x4"
                try:
                    group_data["repeats"] = int(repeats_text.lower().replace("x", ""))
                except ValueError:
                    group_data["repeats"] = 1
            else:
                group_data["repeats"] = 1

            # Group title
            h4 = group.select_one("h4")
            if h4 and h4.get_text(strip=True):
                group_data["name"] = h4.get_text(strip=True)

            # Steps within the group
            steps: list[dict[str, Any]] = []
            intervals_list = group.select_one(".intervals-list")
            if intervals_list:
                current_type = ""
                for child in intervals_list.children:
                    if not hasattr(child, "get"):
                        continue  # skip NavigableString
                    classes = child.get("class", [])

                    if "interval-type" in classes:
                        current_type = child.get_text(strip=True)
                    elif "interval" in classes:
                        step: dict[str, Any] = {"type": current_type}

                        # Duration from .time
                        time_el = child.select_one(".time")
                        if time_el:
                            step["duration"] = time_el.get_text(strip=True)

                        # Distance (some intervals use distance instead of time)
                        dist_el = child.select_one(".distance")
                        if dist_el:
                            step["distance"] = dist_el.get_text(strip=True)

                        # Intensity zone from bg-intensityN class
                        td_el = child.select_one("[class*='bg-intensity']")
                        if td_el:
                            for cls in td_el.get("class", []):
                                m = re.match(r"bg-intensity(\d+)", cls)
                                if m:
                                    step["zone"] = int(m.group(1))
                                    break

                        # Zone details from .zi-block
                        zi_blocks = child.select(".zi-block")
                        zone_details: list[str] = []
                        for zb in zi_blocks:
                            txt = zb.get_text(strip=True)
                            if txt:
                                zone_details.append(txt)
                        if zone_details:
                            step["zone_details"] = zone_details

                        # Note
                        note_el = child.find_next_sibling(class_="note")
                        if note_el:
                            note_text = note_el.get_text(strip=True)
                            if note_text:
                                step["note"] = note_text

                        steps.append(step)
                    elif "note" in classes:
                        # Note attached to the previous step
                        if steps:
                            note_text = child.get_text(strip=True)
                            if note_text:
                                steps[-1]["note"] = note_text

            group_data["steps"] = steps
            intervals.append(group_data)

        if intervals:
            result["intervals"] = intervals

        # --- Summary by zone from .resume-build-seance ---
        summary_el = soup.select_one(".resume-build-seance")
        if summary_el:
            time_per_zone: dict[str, str] = {}
            badges = summary_el.select(".show-time-distance-per-intensities .badge")
            for badge in badges:
                # Class like bg-intensity1
                zone_num = None
                for cls in badge.get("class", []):
                    m = re.match(r"bg-intensity(\d+)", cls)
                    if m:
                        zone_num = int(m.group(1))
                        break
                if zone_num is not None:
                    time_per_zone[f"Z{zone_num}"] = badge.get_text(strip=True)
            if time_per_zone:
                result["summary_by_zone"] = time_per_zone

            # ICF from summary
            icf_el = summary_el.select_one(".icf strong")
            if icf_el and "icf" not in result:
                try:
                    result["icf"] = int(icf_el.get_text(strip=True))
                except ValueError:
                    pass

        return result

    def get_event_plan_data(self, event_id: str | int) -> dict[str, Any]:
        """Get plan-vs-realised data for a séance prévue (read-only GET).

        Calls /v-calevent-load-plan-made-datas?type=sharecalevent&refId=<id>.
        Returns parsed JSON dict, or empty dict on failure.
        """
        self._ensure_logged_in()
        url = f"{BASE_URL}/v-calevent-load-plan-made-datas"
        params = {"type": "sharecalevent", "refId": str(event_id)}
        try:
            r = self._session.get(url, params=params, headers=COMMON_HEADERS, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.warning("get_event_plan_data(%s) failed: %s", event_id, e)
            return {}

    def get_event_plan(self, event_id: str | int) -> dict[str, Any]:
        """
        Get full detail of a séance prévue (read-only).
        event_id must be a caleventId from get_events (planned sessions).

        Returns a dict with:
        - 'modal': structured data parsed from the HTML modal (intervals, zones, etc.)
        - 'plan': JSON data from the plan-vs-realised endpoint

        Each part is fetched independently; if one fails, the other is still returned.
        """
        self._ensure_logged_in()

        # Fetch modal (HTML → parsed dict)
        modal: dict[str, Any] = {}
        try:
            modal = self.show_event_modal(event_id)
        except Exception as e:
            logger.warning("show_event_modal(%s) failed: %s", event_id, e)

        # Fetch plan data (JSON)
        plan = self.get_event_plan_data(event_id)

        return {"modal": modal, "plan": plan}
