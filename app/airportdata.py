"""Aircraft lookup by registration on airport-data.com (free, no key).

airport-data.com has no API for this: its ``ac_info`` endpoint only takes a Mode-S code and
returns little. The aircraft profile page ``/aircraft/{registration}`` has much more, so it
is read and parsed: manufacturer, model, year built, c/n (MSN), engines, Mode-S code,
delivery date and owner.

A registration can belong to several aircraft over the years. The page then lists all
records; the newest one (highest year built) is used.

Only the "Info airport-data" button on an aircraft uses it: the page is HTML meant for people,
so it is never asked automatically or for the whole collection.
Model, manufacturer and operator are only filled when empty, so adsbdb/OpenSky values stay.
"""
import logging
import os
import re
import sys
import time
from datetime import date
from html.parser import HTMLParser
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx

import lookups

logger = logging.getLogger("airportdata")

KEY = "airportdata"
LABEL = "airport-data.com"
DEFAULT_URL = "https://airport-data.com"
FILL_ONLY = {"model", "manufacturer", "operator_name"}

# label on the page -> our key (compared without case, spaces and a trailing colon)
LABELS = {
    "manufacturer": "manufacturer",
    "model": "model",
    "year built": "built",
    "construction number (c/n)": "msn",
    "construction number": "msn",
    "number of engines": "engine_count",
    "engine type": "engine_type",
    "engine manufacturer and model": "engines",
    "registration number": "registration",
    "mode s (icao24) code": "mode_s",
    "mode s code": "mode_s",
    "current status": "status",
    "delivery date": "delivery_date",
    "owner": "owner",
    "address": "address",
}
# other labels and headings of the page: never taken as a value
OTHER = {"airframe info", "aircraft", "aircraft type", "number of seats", "latest photos"}


class AirportDataError(lookups.LookupError):
    """airport-data.com could not be reached or answered with an error."""


def base_url() -> str:
    return os.environ.get("AIRPORTDATA_URL", DEFAULT_URL).rstrip("/")


def enabled(db=None) -> bool:
    """AIRPORTDATA_URL=off switches every lookup off."""
    return not lookups.off(base_url())


def auto_enabled(db) -> bool:
    return False  # only on a click


def _delay() -> float:
    try:
        return float(os.environ.get("AIRPORTDATA_DELAY", "1"))
    except ValueError:
        return 1.0


_throttle = lookups.Throttle(_delay)


# ---- parsing -----------------------------------------------------------------------------

def _norm_label(value: str) -> str:
    return " ".join(value.split()).rstrip(":").strip().lower()


class _Page(HTMLParser):
    """Collects the visible text pieces and the links of a page."""

    SKIP = {"script", "style", "noscript", "head"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.texts: list[str] = []
        self.links: list[tuple[str, str]] = []  # (href, text)
        self._skip = 0
        self._link: list | None = None

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "a":
            self._link = [dict(attrs).get("href") or "", []]

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag == "a" and self._link is not None:
            self.links.append((self._link[0], " ".join(" ".join(self._link[1]).split())))
            self._link = None

    def handle_data(self, data):
        if self._skip:
            return
        value = " ".join(data.split())
        if not value:
            return
        self.texts.append(value)
        if self._link is not None:
            self._link[1].append(value)


def _is_label(value: str) -> bool:
    n = _norm_label(value)
    return n in LABELS or n in OTHER


def parse(html: str) -> tuple[dict, list[tuple[str, str]]]:
    """Profile page -> (fields by our key, links). A label is followed by its value;
    a label followed by another label has no value (e.g. an empty Mode-S code)."""
    page = _Page()
    page.feed(html)
    texts = page.texts
    # the data tables start at "Airframe Info"; skips menus and search forms above it
    for i, value in enumerate(texts):
        if _norm_label(value) == "airframe info":
            texts = texts[i:]
            break
    fields: dict[str, str] = {}
    for i, value in enumerate(texts[:-1]):
        key = LABELS.get(_norm_label(value))
        if not key or key in fields:
            continue
        nxt = texts[i + 1]
        if _is_label(nxt):
            continue
        if key == "address":  # "Frankfurt," / "Germany" on two lines
            parts = [nxt]
            for more in texts[i + 2:i + 4]:
                if _is_label(more) or not parts[-1].endswith(","):
                    break
                parts.append(more)
            nxt = " ".join(parts)
        fields[key] = nxt
    return fields, page.links


def _year(value) -> int:
    m = re.match(r"\s*(\d{4})\b", value or "")
    return int(m.group(1)) if m else 0


def other_records(links, registration: str, page_url: str) -> list[tuple[int, str]]:
    """Links to other records of the same registration: (year built, absolute URL)."""
    reg = registration.upper()
    out = []
    for href, label in links:
        url = urljoin(page_url, href)
        path = unquote(urlsplit(url).path)
        m = re.match(r"^/aircraft/([^/:]+):[^/]+$", path)
        if not m or m.group(1).upper() != reg:
            continue
        year = _year(label)
        if year:
            out.append((year, url))
    return out


def _date(value):
    value = (value or "").strip()
    for pattern, order in ((r"^(\d{4})-(\d{2})-(\d{2})", "ymd"), (r"^(\d{1,2})\.(\d{1,2})\.(\d{4})", "dmy")):
        m = re.match(pattern, value)
        if m:
            a, b, c = (int(x) for x in m.groups())
            try:
                return date(a, b, c) if order == "ymd" else date(c, b, a)
            except ValueError:
                return None
    return None


# ---- fetching ----------------------------------------------------------------------------

def _get(url: str) -> str | None:
    for attempt in range(3):
        _throttle.wait()
        try:
            resp = httpx.get(url, timeout=20, follow_redirects=True,
                             headers={"User-Agent": lookups.USER_AGENT, "Accept": "text/html"})
        except httpx.HTTPError as exc:
            raise AirportDataError(str(exc)) from exc
        if resp.status_code in (404, 410):
            return None
        if resp.status_code == 429 and attempt < 2:
            time.sleep(5 * (attempt + 1))
            continue
        if resp.status_code != 200:
            raise AirportDataError(f"HTTP {resp.status_code}")
        return resp.text
    raise AirportDataError("HTTP 429")


def fetch(db, registration: str) -> dict | None:
    """Fields of the newest record of a registration, None if airport-data does not know it."""
    url = f"{base_url()}/aircraft/{quote(registration.upper(), safe='')}"
    html = _get(url)
    if html is None:
        return None
    fields, links = parse(html)
    current = _year(fields.get("built"))
    newer = [r for r in other_records(links, registration, url) if r[0] > current]
    if newer:
        year, other_url = max(newer)
        html = _get(other_url)
        if html:
            other, _ = parse(html)
            if other.get("msn") or other.get("model"):
                fields = other
    if not (fields.get("msn") or fields.get("model")):
        return None
    reg = fields.get("registration")
    if reg and reg.replace("-", "").upper() != registration.replace("-", "").upper():
        return None  # the site answered with another aircraft
    return fields


def to_record(db, ac, info: dict) -> dict:
    """Parsed page -> aircraft fields."""
    text = lookups.text
    mode_s = (text(info.get("mode_s"), 8) or "").lower()
    built = _year(info.get("built"))
    delivery = _date(info.get("delivery_date"))
    return {
        "icao24": mode_s if re.fullmatch(r"[0-9a-f]{6}", mode_s) else None,
        "msn": text(info.get("msn"), 32),
        "manufacturer": text(info.get("manufacturer"), 255),
        "model": text(info.get("model"), 255),
        "built": str(built) if built else None,
        "delivery_date": delivery.isoformat() if delivery else None,
        "engines": text(info.get("engines"), 255),
        "operator_name": text(info.get("owner"), 255),
    }


def _me():
    return sys.modules[__name__]


def refresh(db, ac):
    return lookups.refresh(db, _me(), ac)
