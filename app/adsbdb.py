"""Aircraft lookup by registration via the free adsbdb.com API.

adsbdb returns type, manufacturer, Mode-S code and the registered owner with its
ICAO operator code. It has no MSN, build year or fleets, so it complements the
OpenSky import:

* "Load data" button on an aircraft: looks it up and applies the result directly.
* New aircraft (sighting form, fleet page, photo scan): looked up in the background,
  if enabled on the master data page.
* Master data page: looks up every aircraft of the collection and stages the result
  as a normal import, so it can be reviewed before it is applied.

Fields in ``manual_fields`` are never changed. Model and manufacturer are only filled
when empty, because adsbdb spells them differently from OpenSky.
"""
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import httpx

import jobs
import models
import settings_store
from database import SessionLocal

logger = logging.getLogger("adsbdb")

DEFAULT_URL = "https://api.adsbdb.com/v0"
USER_AGENT = "AviationLog (self-hosted plane spotting log; +https://github.com/siliconserver/aviationlog)"
SETTING_AUTO = "adsbdb_auto"
FILL_ONLY = {"model", "manufacturer"}  # only set when the aircraft has no value yet

_lock = threading.Lock()
_last_call = 0.0
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="adsbdb")


class AdsbdbError(Exception):
    """adsbdb could not be reached or answered with an error."""


def base_url() -> str:
    return os.environ.get("ADSBDB_URL", DEFAULT_URL).rstrip("/")


def enabled() -> bool:
    """ADSBDB_URL=off switches every lookup off (e.g. for installs without internet)."""
    return base_url().lower() not in ("", "off", "0", "false")


def auto_enabled(db) -> bool:
    return enabled() and settings_store.get(db, SETTING_AUTO) != "0"


def _delay() -> float:
    try:
        return float(os.environ.get("ADSBDB_DELAY", "0.5"))
    except ValueError:
        return 0.5


def _throttle():
    """At most one request per ADSBDB_DELAY seconds across all threads, to be polite."""
    global _last_call
    with _lock:
        wait = _last_call + _delay() - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


def fetch(registration: str) -> dict | None:
    """The adsbdb ``aircraft`` object for a registration, None if adsbdb does not know it."""
    url = f"{base_url()}/aircraft/{quote(registration, safe='')}"
    for attempt in range(3):
        _throttle()
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


def _text(value, limit: int) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value[:limit] or None


def to_record(db, ac: models.Aircraft, info: dict) -> dict:
    """adsbdb answer -> aircraft fields (only those adsbdb may change for this aircraft)."""
    record = {
        "icao24": (_text(info.get("mode_s"), 8) or "").lower() or None,
        "type_icao": (_text(info.get("icao_type"), 8) or "").upper() or None,
        "model": _text(info.get("type"), 255),
        "manufacturer": _text(info.get("manufacturer"), 255),
        "operator_name": _text(info.get("registered_owner"), 255),
        "operator_icao": (_text(info.get("registered_owner_operator_flag_code"), 4) or "").upper() or None,
    }
    if record["operator_icao"]:
        airline = (
            db.query(models.Airline)
            .filter(models.Airline.icao == record["operator_icao"])
            .order_by(models.Airline.active.desc(), models.Airline.id)
            .first()
        )
        if airline and airline.iata:
            record["operator_iata"] = airline.iata
    for field in FILL_ONLY:
        if getattr(ac, field):
            record.pop(field)
    return {k: v for k, v in record.items() if v is not None}


def changes(db, ac: models.Aircraft, info: dict) -> dict:
    """Fields adsbdb would change: field -> new value. Manual fields are left out."""
    manual = set(ac.manual_fields or [])
    out = {}
    for field, value in to_record(db, ac, info).items():
        if field in manual:
            continue
        if (getattr(ac, field) or None) != value:
            out[field] = value
    return out


def refresh(db, ac: models.Aircraft) -> list[str] | None:
    """Looks the aircraft up and applies the result. Returns the changed fields,
    None if adsbdb does not know the registration. Raises AdsbdbError."""
    info = fetch(ac.registration)
    if info is None:
        return None
    new = changes(db, ac, info)
    for field, value in new.items():
        setattr(ac, field, value)
    db.commit()
    if "operator_icao" in new:
        import importers

        importers.link_airlines(db)
    return sorted(new)


# ---- background lookup of new aircraft ------------------------------------------------------

def lookup_new(db, aircraft_ids):
    """Looks up newly created aircraft in the background (if switched on)."""
    ids = [i for i in aircraft_ids if i]
    if not ids or not auto_enabled(db):
        return
    _executor.submit(_lookup_ids, ids)


def _lookup_ids(ids):
    db = SessionLocal()
    try:
        for aircraft_id in ids:
            ac = db.get(models.Aircraft, aircraft_id)
            if ac is None or ac.reg_unknown:
                continue
            try:
                refresh(db, ac)
            except AdsbdbError as exc:
                logger.warning("adsbdb lookup of %s failed: %s", ac.registration, exc)
                db.rollback()
            except Exception:
                logger.exception("adsbdb lookup of %s failed", ac.registration)
                db.rollback()
    finally:
        db.close()


def wait_idle(timeout: float = 30.0):
    """For tests: blocks until all background lookups are finished."""
    _executor.submit(lambda: None).result(timeout=timeout)


# ---- lookup of the whole collection (staged as import) -----------------------------------

def collection_query(db):
    """Aircraft worth a lookup: spotted ones and ones added by hand or by the photo scan."""
    spotted = db.query(models.Sighting.aircraft_id)
    return (
        db.query(models.Aircraft)
        .filter(models.Aircraft.reg_unknown.is_(False))
        .filter(models.Aircraft.id.in_(spotted) | models.Aircraft.source.in_(["manual", "scan"]))
    )


def collection_aircraft(db) -> list[models.Aircraft]:
    return collection_query(db).order_by(models.Aircraft.registration).all()


def collect_rows(db, job_id: int | None = None):
    """Yields (registration, record) for every aircraft of the collection adsbdb knows."""
    aircraft = collection_aircraft(db)
    total = len(aircraft)
    found = failed = 0
    for i, ac in enumerate(aircraft, 1):
        try:
            info = fetch(ac.registration)
        except AdsbdbError as exc:
            failed += 1
            logger.warning("adsbdb lookup of %s failed: %s", ac.registration, exc)
            if failed >= 10 and found == 0:
                raise AdsbdbError(f"adsbdb not reachable: {exc}") from exc
            info = None
        if info:
            found += 1
            yield ac.registration, {"registration": ac.registration, **to_record(db, ac, info)}
        if job_id and (i % 5 == 0 or i == total):
            jobs.progress(job_id, i, total, "lookup")
