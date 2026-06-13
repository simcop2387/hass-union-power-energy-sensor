"""Union Power API client for Home Assistant integration."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import aiohttp
from aiohttp import ClientTimeout, ClientError
from bs4 import BeautifulSoup

from .const import (
    BASE_URL,
    DEFAULT_TIMEOUT,
)
from .exceptions import (
    UnionPowerAuthenticationError,
    UnionPowerConnectionError,
    UnionPowerDataError,
)

_LOGGER = logging.getLogger(__name__)

def _log(level: int, msg: str, *args: Any) -> None:
    getattr(_LOGGER, level)(f"[UNION] {msg}", *args)


@dataclass
class IntervalUsage:
    """Single hourly interval reading."""
    timestamp: str  # "MM/DD/YYYY HH:MM AM/PM"
    kwh: float
    used_from_grid: float
    total_generation: float
    excess_generation: float
    temp: Optional[float]
    humidity: Optional[float]
    wind_speed: Optional[float]


def _parse_float(val: Any) -> Optional[float]:
    """Parse a float value, returning None for NaN/empty."""
    if val is None:
        return None
    s = str(val).strip()
    if not s or s == "NaN":
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def derive_account_ids(account_number: str) -> tuple[str, str]:
    """Derive keymbr and MemberSep from account number.

    Account 5497637001 → keymbr='05497637', MemberSep='5497637-001'
    """
    prefix = account_number[:-3]
    suffix = account_number[-3:]
    keymbr = "0" + prefix
    member_sep = f"{prefix}-{suffix}"
    return keymbr, member_sep


class UnionPowerAPI:
    """Class to interact with the Union Power API."""

    def __init__(
        self,
        account_number: str,
        password: str,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        """Initialize the Union Power API client."""
        self.account_number = account_number
        self.password = password
        self.keymbr, self.member_sep = derive_account_ids(account_number)
        self.timeout = timeout
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create an aiohttp session."""
        if self._session is None or self._session.closed:
            timeout = ClientTimeout(total=self.timeout)
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                connector=aiohttp.TCPConnector(ssl=True),
                cookie_jar=aiohttp.CookieJar(),
                headers={
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:151.0) Gecko/20100101 Firefox/151.0",
                },
            )
        return self._session

    async def close(self) -> None:
        """Close the aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def login(self) -> None:
        """Authenticate with the Union Power portal.

        Steps:
        1. GET /Customer-Login to extract form tokens
        2. POST /Customer-Login with credentials
        3. Navigate through redirect chain to establish session
        """
        # Always use a fresh session — sessions expire after 5 minutes
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
        session = await self._get_session()

        # Step 1: GET login page to extract tokens
        _log("info", "Fetching login page")
        async with session.get(f"{BASE_URL}/Customer-Login") as resp:
            resp.raise_for_status()
            html = await resp.text()

        soup = BeautifulSoup(html, "html.parser")

        vs = soup.find("input", attrs={"name": "__VIEWSTATE"})
        ev = soup.find("input", attrs={"name": "__EVENTVALIDATION"})
        csrf = soup.find("input", attrs={"name": "__RequestVerificationToken"})

        if not vs or not ev or not csrf:
            missing = []
            if not vs:
                missing.append("__VIEWSTATE")
            if not ev:
                missing.append("__EVENTVALIDATION")
            if not csrf:
                missing.append("__RequestVerificationToken")
            _log("error", "Missing form tokens: %s", ", ".join(missing))
            raise UnionPowerAuthenticationError(
                f"Could not extract login form tokens from page (missing: {', '.join(missing)})"
            )

        viewstate = vs.get("value", "")
        eventvalidation = ev.get("value", "")
        csrftoken = csrf.get("value", "")

        # Step 2: POST credentials
        _log("info", "Submitting login credentials")
        payload = {
            "ScriptManager": "dnn$ctr384$CustomerLogin$UpdatePanel1|dnn$ctr384$CustomerLogin$btnLogin",
            "__LASTFOCUS": "",
            "__EVENTTARGET": "",
            "__EVENTARGUMENT": "",
            "__VIEWSTATE": viewstate,
            "__VIEWSTATEGENERATOR": "F57EDA00",
            "__VIEWSTATEENCRYPTED": "",
            "__EVENTVALIDATION": eventvalidation,
            "dnn$MiniSearch1$txtSearch": "",
            "dnn$ctr384$CustomerLogin$txtUsername": self.account_number,
            "dnn$ctr384$CustomerLogin$txtPassword": self.password,
            "dnn$ctr384$CustomerLogin$hdnSecretkey": "",
            "dnn$ctr384$CustomerLogin$hdnMFATokenkey": "",
            "dnn$ctr384$CustomerLogin$HiddenField1": "",
            "ScrollTop": "0",
            "__dnnVariable": "`{`__scdoff`:`1`,`sf_siteRoot`:`/onlineportal/`,`sf_tabId`:`34`}",
            "__RequestVerificationToken": csrftoken,
            "__ASYNCPOST": "true",
            "dnn$ctr384$CustomerLogin$btnLogin": "Sign In",
        }

        async with session.post(
            f"{BASE_URL}/Customer-Login",
            data=payload,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "X-MicrosoftAjax": "Delta=true",
                "Referer": f"{BASE_URL}/Customer-Login",
            },
        ) as resp:
            resp.raise_for_status()
            text = await resp.text()
            _log("info", "Login POST set-cookies: %s", list(resp.cookies.keys()))

        if "pageRedirect" not in text:
            _log("error", "Login POST returned no pageRedirect, response length: %d, first 500 chars: %s", len(text), text[:500])
            raise UnionPowerAuthenticationError(
                "Login failed — no redirect in response. Check credentials."
            )

        # Step 3: Navigate redirect chain to establish full session
        _log("info", "Following post-login redirect chain")
        for path in (
            "/BillingPayments/tabid/42/Default.aspx",
            "/Billing-Payments",
            "/My-Account/Usage-History",
        ):
            async with session.get(f"{BASE_URL}{path}", allow_redirects=True) as resp:
                resp.raise_for_status()
                await resp.text()

        _log("info", "Successfully logged in to Union Power portal")
        _log("info", "Cookies after login: %s", [c.key for c in session.cookie_jar])

    async def _activate_meter_session(self, day: datetime) -> None:
        """Call GetDailyUsageData to activate the meter data session.

        The API requires this call before GetIntervalData will return data.
        """
        body = {
            "keymbr": self.keymbr,
            "MemberSep": self.member_sep,
            "StartDate": day.strftime("%m/%d/%Y"),
            "EndDate": day.strftime("%m/%d/%Y"),
            "IsEnergy": "false",
            "IsPPM": "false",
            "IsCostEnable": "3",
        }
        session = await self._get_session()
        body_text = json.dumps(body, separators=(",", ":")).replace('"', "'")

        async with session.post(
            f"{BASE_URL}/DesktopModules/MeterUsage/API/MeterData.aspx/GetDailyUsageData",
            data=body_text,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{BASE_URL}/My-Account/Usage-History",
            },
        ) as resp:
            resp.raise_for_status()
            await resp.json()

    async def get_interval_usage(
        self, start_date: datetime, end_date: datetime
    ) -> List[IntervalUsage]:
        """Fetch hourly interval usage data with day-by-day pagination.

        Re-logs in every 10 days to keep the session alive (expires after 5 min).
        """
        all_records: List[IntervalUsage] = []
        current = start_date
        days_since_login = -1
        total_days = (end_date - start_date).days + 1

        _log("info", "Fetching interval data: %s → %s (%d days, keymbr=%s, MemberSep=%s)",
                      start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"),
                      total_days, self.keymbr, self.member_sep)

        while current <= end_date:
            days_since_login += 1
            if days_since_login % 10 == 0:
                _log("info", "Re-authenticating (day %d/%d: %s)",
                             days_since_login, total_days, current.strftime("%Y-%m-%d"))
                await self.login()
            # Activate meter session before fetching interval data
            await self._activate_meter_session(current)
            records = await self._fetch_interval_day(current)
            all_records.extend(records)
            current += timedelta(days=1)

        _log("info",
            "Fetched %d interval records for %s → %s",
            len(all_records),
            start_date.strftime("%Y-%m-%d"),
            end_date.strftime("%Y-%m-%d"))
        return all_records

    async def _fetch_interval_day(self, day: datetime) -> List[IntervalUsage]:
        """Fetch interval data for a single day."""
        body = {
            "keymbr": self.keymbr,
            "MemberSep": self.member_sep,
            "StartDate": day.strftime("%m/%d/%Y"),
            "EndDate": (day + timedelta(days=1)).strftime("%m/%d/%Y"),
            "IntervalType": "60",
        }

        session = await self._get_session()
        body_text = json.dumps(body, separators=(",", ":")).replace('"', "'")
        url = f"{BASE_URL}/DesktopModules/MeterUsage/API/MeterData.aspx/GetIntervalData"

        _log("info", "POST %s body=%s", url, body_text)
        _log("info", "Cookies in jar: %s", [c.key for c in session.cookie_jar])

        async with session.post(
            url,
            data=body_text,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{BASE_URL}/My-Account/Usage-History",
            },
        ) as resp:
            resp.raise_for_status()
            _log("info", "API response status=%d, set-cookies=%s", resp.status, list(resp.cookies.keys()))
            raw = await resp.json()

        data = raw.get("d", raw)
        items = data.get("Items", [])

        if not items:
            error = data.get("errorObject", {})
            err_desc = error.get("errordesc", "")
            if err_desc:
                _log("warning", "API error for %s: %s", day.strftime("%Y-%m-%d"), err_desc)
            _log("warning", "No items for %s — keys: %s, __type: %s, full response sample: %s",
                           day.strftime("%Y-%m-%d"),
                           list(data.keys()) if isinstance(data, dict) else type(data),
                           data.get("__type", "unknown") if isinstance(data, dict) else "not-dict",
                           json.dumps(data, default=str)[:300])
            return []

        _log("info", "Got %d items for %s", len(items), day.strftime("%Y-%m-%d"))
        return [self._parse_interval_item(i) for i in items]

    @staticmethod
    def _parse_interval_item(item: Dict[str, Any]) -> IntervalUsage:
        """Parse a single interval data item.

        Interval API fields: KWH (consumption), GKWH (generation).
        """
        return IntervalUsage(
            timestamp=item.get("UsageHourDate", ""),
            kwh=_parse_float(item.get("KWH")) or 0.0,
            used_from_grid=_parse_float(item.get("KWH")) or 0.0,
            total_generation=_parse_float(item.get("GKWH")) or 0.0,
            excess_generation=0.0,
            temp=_parse_float(item.get("Temp")),
            humidity=_parse_float(item.get("Humidity")),
            wind_speed=_parse_float(item.get("WindSpeed")),
        )

    @staticmethod
    def parse_timestamp(ts_str: str) -> datetime:
        """Parse a Union Power timestamp string like '06/11/2026 12:00 AM'.

        Returns a naive datetime (local time).
        """
        return datetime.strptime(ts_str, "%m/%d/%Y %I:%M %p")
