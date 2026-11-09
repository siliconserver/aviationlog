"""Shared code of the aircraft lookups by registration.

A provider is a module (``adsbdb``, ``airportdata``, ``flightera``) with:

* ``KEY`` and ``LABEL``
* ``FILL_ONLY``: fields that are only set when the aircraft has no value yet
* ``enabled(db)`` and ``auto_enabled(db)``
* ``fetch(db, registration)``: the provider's answer, None if it does not know the registration
* ``to_record(db, ac, info)``: answer -> aircraft fields

The lookup never changes fields in ``manual_fields``.
"""
import importlib
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import jobs
import models
from database import SessionLocal

logger = logging.getLogger("lookups")

PROVIDERS = ["adsbdb", "airportdata", "flightera"]
USER_AGENT = "AviationLog (self-hosted plane spotting log; +https://github.com/siliconserver/aviationlog)"

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lookup")


class LookupError(Exception):  # noqa: A001 - the provider errors derive from it
    """The provider could not be reached or answered with an error."""


def provider(key: str):
    if key not in PROVIDERS:
        raise KeyError(key)
    return importlib.import_module(key)


def off(value: str) -> bool:
    """A provider URL of off/0/false switches the provider off (e.g. without internet)."""
    return value.strip().lower() in ("", "off", "0", "false")


class Throttle:
    """At most one request per ``delay()`` seconds across all threads, to be polite."""

    def __init__(self, delay):
        self._delay = delay
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self):
        with self._lock:
            pause = self._last + self._delay() - time.monotonic()
            if pause > 0:
                time.sleep(pause)
            self._last = time.monotonic()


def text(value, limit: int) -> str | None:
    if value is None:
        return None
    value = " ".join(str(value).split())
    if value.lower() in ("", "-", "n/a", "unknown", "none", "null"):
        return None
    return value[:limit] or None


def airline_iata(db, icao: str | None) -> str | None:
    if not icao:
        return None
    airline = (
        db.query(models.Airline)
        .filter(models.Airline.icao == icao)
        .order_by(models.Airline.active.desc(), models.Airline.id)
        .first()
    )
    return airline.iata if airline and airline.iata else None


def record(db, prov, ac: models.Aircraft, info: dict) -> dict:
    """The answer as aircraft fields, without empty values and without FILL_ONLY fields that are set."""
    out = prov.to_record(db, ac, info)
    for field in prov.FILL_ONLY:
        if getattr(ac, field) not in (None, ""):
            out.pop(field, None)
    return {k: v for k, v in out.items() if v is not None}


def _same(old, new, field: str = "") -> bool:
    if field == "msn" and old and new and str(old).lstrip("0") == str(new).lstrip("0"):
        return True  # "09555" and "9555" are the same MSN
    if hasattr(old, "isoformat"):
        old = old.isoformat()
    if hasattr(new, "isoformat"):
        new = new.isoformat()
    return (old or None) == (new or None)


def changes(db, prov, ac: models.Aircraft, info: dict) -> dict:
    """Fields the provider would change: field -> new value. Manual fields are left out."""
    manual = set(ac.manual_fields or [])
    return {
        field: value
        for field, value in record(db, prov, ac, info).items()
        if field not in manual and not _same(getattr(ac, field), value, field)
    }


def refresh(db, prov, ac: models.Aircraft) -> list[str] | None:
    """Looks the aircraft up and applies the result. Returns the changed fields,
    None if the provider does not know the registration. Raises LookupError."""
    info = prov.fetch(db, ac.registration)
    if info is None:
        return None
    new = changes(db, prov, ac, info)
    for field, value in new.items():
        if field == "delivery_date" and isinstance(value, str):
            value = date.fromisoformat(value)
        setattr(ac, field, value)
    db.commit()
    if "operator_icao" in new:
        import importers

        importers.link_airlines(db)
    return sorted(new)


# ---- background lookup of new aircraft ------------------------------------------------------

def lookup_new(db, aircraft_ids):
    """Looks up newly created aircraft in the background at every provider switched on for it."""
    ids = [i for i in aircraft_ids if i]
    keys = [key for key in PROVIDERS if provider(key).auto_enabled(db)]
    if ids and keys:
        _executor.submit(_lookup_ids, ids, keys)


def _lookup_ids(ids, keys):
    db = SessionLocal()
    try:
        for aircraft_id in ids:
            for key in keys:
                ac = db.get(models.Aircraft, aircraft_id)
                if ac is None or ac.reg_unknown:
                    break
                try:
                    refresh(db, provider(key), ac)
                except LookupError as exc:
                    logger.warning("%s lookup of %s failed: %s", key, ac.registration, exc)
                    db.rollback()
                except Exception:
                    logger.exception("%s lookup of %s failed", key, ac.registration)
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


def collect_rows(db, prov, job_id: int | None = None):
    """Yields (registration, record) for every aircraft of the collection the provider knows."""
    aircraft = collection_query(db).order_by(models.Aircraft.registration).all()
    total = len(aircraft)
    found = failed = 0
    for i, ac in enumerate(aircraft, 1):
        try:
            info = prov.fetch(db, ac.registration)
        except LookupError as exc:
            failed += 1
            logger.warning("%s lookup of %s failed: %s", prov.KEY, ac.registration, exc)
            if getattr(exc, "fatal", False):
                # e.g. no credits left: keep what was found so far, unless nothing was
                if found == 0:
                    raise
                if job_id:
                    jobs.progress(job_id, total, total, "lookup")
                return
            if failed >= 10 and found == 0:
                raise LookupError(f"{prov.LABEL} not reachable: {exc}") from exc
            info = None
        if info:
            found += 1
            yield ac.registration, {"registration": ac.registration, **record(db, prov, ac, info)}
        if job_id and (i % 5 == 0 or i == total):
            jobs.progress(job_id, i, total, "lookup")
