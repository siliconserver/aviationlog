"""Aircraft lookup by registration via the flightera.net data API (needs an API key).

``GET /aircraft/info?reg=`` costs 1 credit; a free account has 1000 credits a month.
That is why flightera is never asked automatically: only on a click ("Info flightera" on an
aircraft) or for the whole collection in Admin -> Master data. Errors (HTTP 400 and higher)
cost nothing. Without credits or with a wrong key the collection lookup stops at once.

It returns the Mode-S code, manufacturer, model, the current airline (usually as IATA code,
resolved via the airline master data) and the first flight.
The key is entered on the master data page and stored in the settings table.
"""
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

import lookups
import models
import settings_store

KEY = "flightera"
LABEL = "flightera.net"
DEFAULT_URL = "https://www.flightera.net/dapi/v1"
SETTING_KEY = "flightera_key"
SETTING_BALANCE = "flightera_balance"
SETTING_BALANCE_AT = "flightera_balance_at"
FILL_ONLY = {"model", "manufacturer"}
# error codes of the API that have their own message
ERROR_CODES = {"quota_exceeded", "invalid_key", "rate_limited", "your_limit_exceeded", "no_key"}
# errors after which further requests are pointless
FATAL = {"quota_exceeded", "invalid_key", "your_limit_exceeded", "no_key"}


class FlighteraError(lookups.LookupError):
    """flightera could not be reached or answered with an error. ``code`` is the API's error code."""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code
        self.fatal = code in FATAL


def base_url() -> str:
    return os.environ.get("FLIGHTERA_URL", DEFAULT_URL).rstrip("/")


def api_key(db) -> str:
    return settings_store.get(db, SETTING_KEY).strip()


def enabled(db=None) -> bool:
    """FLIGHTERA_URL=off switches flightera off; without a key it is off as well."""
    if lookups.off(base_url()):
        return False
    return db is None or bool(api_key(db))


def auto_enabled(db) -> bool:
    return False  # every call costs a credit: only on a click


def _delay() -> float:
    try:
        return float(os.environ.get("FLIGHTERA_DELAY", "0.25"))
    except ValueError:
        return 0.25


_throttle = lookups.Throttle(_delay)  # the free plan allows 5 requests per second


def balance(db) -> tuple[str, str]:
    """Credits left after the last call and when that was (ISO time), as reported by the API."""
    return settings_store.get(db, SETTING_BALANCE), settings_store.get(db, SETTING_BALANCE_AT)


def _error(resp) -> FlighteraError:
    try:
        body = resp.json()
    except ValueError:
        body = {}
    code = body.get("code") or "" if isinstance(body, dict) else ""
    message = body.get("error") if isinstance(body, dict) else None
    return FlighteraError(message or f"HTTP {resp.status_code}", code)


def fetch(db, registration: str) -> dict | None:
    """The answer of /aircraft/info, None if flightera does not know the registration."""
    key = api_key(db)
    if not key:
        raise FlighteraError("no API key", "no_key")
    url = f"{base_url()}/aircraft/info?reg={quote(registration, safe='')}"
    for attempt in range(3):
        _throttle.wait()
        try:
            resp = httpx.get(url, timeout=20, headers={"X-Api-Key": key, "User-Agent": lookups.USER_AGENT})
        except httpx.HTTPError as exc:
            raise FlighteraError(str(exc)) from exc
        left = resp.headers.get("X-Credit-Balance")
        if left is not None:
            settings_store.set(db, SETTING_BALANCE, left.strip())
            settings_store.set(db, SETTING_BALANCE_AT, datetime.now(timezone.utc).isoformat(timespec="minutes"))
        if resp.status_code == 404:
            return None
        if resp.status_code == 429 and attempt < 2:
            error = _error(resp)
            if error.code == "your_limit_exceeded":
                raise error
            try:
                wait = float(resp.headers.get("Retry-After", "2"))
            except ValueError:
                wait = 2.0
            if wait > 10:
                raise error
            time.sleep(max(wait, 0.5))
            continue
        if resp.status_code != 200:
            raise _error(resp)
        try:
            data = resp.json()
        except ValueError as exc:
            raise FlighteraError("invalid answer") from exc
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            data = data["data"]
        if not isinstance(data, dict) or not (data.get("reg") or data.get("model") or data.get("hex")):
            return None
        return data
    raise FlighteraError("HTTP 429", "rate_limited")


def to_record(db, ac, info: dict) -> dict:
    """flightera answer -> aircraft fields."""
    text = lookups.text
    hex_code = (text(info.get("hex"), 8) or "").lower()
    first = text(info.get("first_flight"), 10)
    return {
        "icao24": hex_code if len(hex_code) == 6 else None,
        "manufacturer": text(info.get("manufacturer"), 255),
        "model": text(info.get("model"), 255),
        "first_flight": first if first and first[:4].isdigit() else None,
        **_operator(db, text(info.get("airline"), 255)),
    }


def _operator(db, value: str | None) -> dict:
    """flightera's ``airline`` is usually the airline's IATA code (e.g. "OU"), sometimes a name.
    A code is resolved via the airline master data to name, IATA and ICAO code; an unknown or
    ambiguous code changes nothing."""
    if not value:
        return {}
    code = value.strip().upper()
    if re.fullmatch(r"[A-Z0-9]{2}", code):
        column = models.Airline.iata
    elif re.fullmatch(r"[A-Z]{3}", code):
        column = models.Airline.icao
    else:
        return {"operator_name": value}
    if db is None:
        return {}
    airlines = db.query(models.Airline).filter(column == code).all()
    active = [a for a in airlines if a.active] or airlines
    if len({(a.icao or a.name) for a in active}) != 1:
        return {}  # unknown or several airlines with this code
    airline = active[0]
    return {
        "operator_name": airline.name,
        "operator_iata": airline.iata,
        "operator_icao": airline.icao,
    }


def _me():
    return sys.modules[__name__]


def refresh(db, ac):
    return lookups.refresh(db, _me(), ac)
