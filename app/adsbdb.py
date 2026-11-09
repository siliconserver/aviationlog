"""Aircraft lookup by registration via the free adsbdb.com API.

adsbdb returns type, manufacturer, Mode-S code and the registered owner with its
ICAO operator code. It has no MSN, build year or fleets, so it complements the
OpenSky import:

* "Info adsbdb" button on an aircraft: looks it up and applies the result directly.
* New aircraft (sighting form, fleet page, photo scan): looked up in the background,
  if enabled on the master data page.
* Master data page: looks up every aircraft of the collection and stages the result
  as a normal import, so it can be reviewed before it is applied.

The shared code is in lookups.py. Model and manufacturer are only filled when empty,
because adsbdb spells them differently from OpenSky.
"""
import os
import sys
import time
from urllib.parse import quote

import httpx

import lookups
import settings_store

KEY = "adsbdb"
LABEL = "adsbdb.com"
DEFAULT_URL = "https://api.adsbdb.com/v0"
USER_AGENT = lookups.USER_AGENT
SETTING_AUTO = "adsbdb_auto"
FILL_ONLY = {"model", "manufacturer"}  # only set when the aircraft has no value yet


class AdsbdbError(lookups.LookupError):
    """adsbdb could not be reached or answered with an error."""


def base_url() -> str:
    return os.environ.get("ADSBDB_URL", DEFAULT_URL).rstrip("/")


def enabled(db=None) -> bool:
    """ADSBDB_URL=off switches every lookup off (e.g. for installs without internet)."""
    return not lookups.off(base_url())


def auto_enabled(db) -> bool:
    return enabled() and settings_store.get(db, SETTING_AUTO) != "0"


def _delay() -> float:
    try:
        return float(os.environ.get("ADSBDB_DELAY", "0.5"))
    except ValueError:
        return 0.5


_throttle = lookups.Throttle(_delay)


def fetch(db, registration: str) -> dict | None:
    """The adsbdb ``aircraft`` object for a registration, None if adsbdb does not know it."""
    url = f"{base_url()}/aircraft/{quote(registration, safe='')}"
    for attempt in range(3):
        _throttle.wait()
        try:
            resp = httpx.get(url, timeout=15, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
        except httpx.HTTPError as exc:
            raise AdsbdbError(str(exc)) from exc
        if resp.status_code == 404:
            return None
        if resp.status_code == 429 and attempt < 2:
            time.sleep(5 * (attempt + 1))
            continue
        if resp.status_code != 200:
            raise AdsbdbError(f"HTTP {resp.status_code}")
        try:
            data = resp.json().get("response")
        except ValueError as exc:
            raise AdsbdbError("invalid answer") from exc
        if isinstance(data, dict) and isinstance(data.get("aircraft"), dict):
            return data["aircraft"]
        return None
    raise AdsbdbError("HTTP 429")


def to_record(db, ac, info: dict) -> dict:
    """adsbdb answer -> aircraft fields."""
    text = lookups.text
    record = {
        "icao24": (text(info.get("mode_s"), 8) or "").lower() or None,
        "type_icao": (text(info.get("icao_type"), 8) or "").upper() or None,
        "model": text(info.get("type"), 255),
        "manufacturer": text(info.get("manufacturer"), 255),
        "operator_name": text(info.get("registered_owner"), 255),
        "operator_icao": (text(info.get("registered_owner_operator_flag_code"), 4) or "").upper() or None,
    }
    record["operator_iata"] = lookups.airline_iata(db, record["operator_icao"])
    return record


# ---- shortcuts --------------------------------------------------------------------------

def _me():
    return sys.modules[__name__]


def refresh(db, ac):
    return lookups.refresh(db, _me(), ac)


def lookup_new(db, aircraft_ids):
    lookups.lookup_new(db, aircraft_ids)


wait_idle = lookups.wait_idle
collection_query = lookups.collection_query
