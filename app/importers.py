"""Import of master data from public sources.

Flow: download (or uploaded file) -> parse -> stage all rows in ``import_rows`` ->
diff against the database (only new and changed rows remain) -> review in the
UI -> apply. Fields listed in a row's ``manual_fields`` are never changed.
"""
import csv
import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import httpx
from psycopg2.extras import execute_values
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

import jobs
import models
from database import engine
from photos import DATA_DIR, normalize_registration

logger = logging.getLogger("import")

IMPORT_DIR = DATA_DIR / "imports"
CHUNK = 5000
USER_AGENT = "AviationLog (self-hosted plane spotting log; +https://github.com/siliconserver/aviationlog)"

OPENSKY_LIST_URL = "https://s3.opensky-network.org/data-samples?list-type=2&prefix=metadata/aircraft-database-complete-"
OPENSKY_FILE_URL = "https://s3.opensky-network.org/data-samples/{key}"
OPENSKY_FALLBACK_URL = "https://opensky-network.org/datasets/metadata/aircraftDatabase.csv"

SOURCES = {
    "opensky": {
        "entity": "aircraft",
        "label": "OpenSky Aircraft Database",
        "info": "https://opensky-network.org/data/aircraft",
        "license": "OpenSky Network, free for non-commercial use",
        "url_env": "OPENSKY_URL",
        "url": None,  # discovered: newest monthly snapshot
    },
    "ourairports": {
        "entity": "airport",
        "label": "OurAirports",
        "info": "https://ourairports.com/data/",
        "license": "Public Domain",
        "url_env": "OURAIRPORTS_URL",
        "url": "https://davidmegginson.github.io/ourairports-data/airports.csv",
    },
    "openflights_airlines": {
        "entity": "airline",
        "label": "OpenFlights airlines",
        "info": "https://openflights.org/data",
        "license": "Open Database License (ODbL)",
        "url_env": "OPENFLIGHTS_AIRLINES_URL",
        "url": "https://raw.githubusercontent.com/jpatokal/openflights/master/data/airlines.dat",
    },
    "openflights_planes": {
        "entity": "type",
        "label": "OpenFlights aircraft types (IATA/ICAO)",
        "info": "https://openflights.org/data",
        "license": "Open Database License (ODbL)",
        "url_env": "OPENFLIGHTS_PLANES_URL",
        "url": "https://raw.githubusercontent.com/jpatokal/openflights/master/data/planes.dat",
    },
    "doc8643": {
        "entity": "type",
        "label": "ICAO Doc 8643 aircraft types (via OpenSky)",
        "info": "https://www.icao.int/publications/DOC8643",
        "license": "OpenSky Network, free for non-commercial use",
        "url_env": "DOC8643_URL",
        "url": "https://opensky-network.org/datasets/metadata/doc8643AircraftTypes.csv",
    },
}

# entity -> (model, key column, comparable fields)
ENTITIES = {
    "aircraft": (
        models.Aircraft,
        "registration",
        ["icao24", "msn", "line_number", "type_icao", "model", "manufacturer", "operator_name",
         "operator_icao", "operator_iata", "built", "first_flight", "engines"],
    ),
    "airport": (
        models.Airport,
        "ident",
        ["name", "iata", "icao", "type", "municipality", "country", "lat", "lon"],
    ),
    "airline": (
        models.Airline,
        "source_key",
        ["name", "alias", "iata", "icao", "callsign", "country", "active"],
    ),
    "type": (
        models.AircraftType,
        "icao",
        ["iata", "name", "manufacturer", "model_full", "engine_count", "engine_type", "description"],
    ),
}

# column lengths, so overly long source values do not break the import
_MAXLEN = {}
for _entity, (_model, _key, _fields) in ENTITIES.items():
    for _col in [_key] + _fields:
        _type = _model.__table__.c[_col].type
        if getattr(_type, "length", None):
            _MAXLEN[(_entity, _col)] = _type.length


# ---- helpers --------------------------------------------------------------------------

def _clean(value):
    if value is None:
        return None
    value = str(value).strip().strip("'").strip()
    if value in ("", "\\N", "-", "N/A", "n/a", "null", "None"):
        return None
    return value


def _date(value):
    value = _clean(value)
    if not value:
        return None
    m = re.match(r"^(\d{4})(?:-(\d{2})-(\d{2}))?", value)
    if not m:
        return None
    if m.group(2):
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return m.group(1)
    return m.group(1)


def _finish(entity: str, data: dict) -> dict:
    out = {}
    for k, v in data.items():
        if v is None or v == "":
            continue
        limit = _MAXLEN.get((entity, k))
        if limit and isinstance(v, str):
            v = v[:limit]
        out[k] = v
    return out


def _open_text(path: Path):
    return open(path, encoding="utf-8", errors="replace", newline="")


def source_url(source: str) -> str:
    cfg = SOURCES[source]
    env = os.environ.get(cfg["url_env"])
    if env:
        return env
    if cfg["url"]:
        return cfg["url"]
    return discover_opensky_url()


def discover_opensky_url() -> str:
    """URL of the newest monthly OpenSky aircraft database snapshot."""
    try:
        resp = httpx.get(OPENSKY_LIST_URL, timeout=30, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        keys = [el.text for el in root.iter() if el.tag.endswith("Key") and el.text and el.text.endswith(".csv")]
        if keys:
            return OPENSKY_FILE_URL.format(key=sorted(keys)[-1])
    except Exception:
        logger.warning("OpenSky snapshot list not available, using fallback URL")
    return OPENSKY_FALLBACK_URL


def download(source: str, job_id: int | None = None) -> Path:
    url = source_url(source)
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    target = IMPORT_DIR / f"{source}.download"
    with httpx.stream("GET", url, follow_redirects=True, timeout=120,
                      headers={"User-Agent": USER_AGENT}) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("content-length") or 0)
        done = 0
        last = 0
        with open(target, "wb") as fh:
            for chunk in resp.iter_bytes(1 << 20):
                fh.write(chunk)
                done += len(chunk)
                if job_id and done - last > (5 << 20):
                    last = done
                    jobs.progress(job_id, done // (1 << 20), total // (1 << 20) if total else 0,
                                  f"download:{url}")
    final = IMPORT_DIR / f"{source}.csv"
    target.replace(final)
    return final


# ---- parsers --------------------------------------------------------------------------

def parse_opensky(path: Path):
    with _open_text(path) as fh:
        first = fh.readline()
        quote = "'" if first.startswith("'") else '"'
        header = [h.strip().strip("'\"").lower() for h in next(csv.reader([first], quotechar=quote))]
        reader = csv.reader(fh, quotechar=quote, escapechar="\\")
        for row in reader:
            rec = dict(zip(header, row))
            reg = normalize_registration(_clean(rec.get("registration")) or "")
            if not reg or len(reg) > 16 or not re.match(r"^[A-Z0-9-]+$", reg):
                continue
            yield reg, _finish("aircraft", {
                "registration": reg,
                "icao24": (_clean(rec.get("icao24")) or "").lower() or None,
                "msn": _clean(rec.get("serialnumber")),
                "line_number": _clean(rec.get("linenumber")),
                "type_icao": (_clean(rec.get("typecode")) or "").upper() or None,
                "model": _clean(rec.get("model")),
                "manufacturer": _clean(rec.get("manufacturername")),
                "operator_name": _clean(rec.get("operator")),
                "operator_icao": (_clean(rec.get("operatoricao")) or "").upper() or None,
                "operator_iata": (_clean(rec.get("operatoriata")) or "").upper() or None,
                "built": _date(rec.get("built")),
                "first_flight": _date(rec.get("firstflightdate")),
                "engines": _clean(rec.get("engines")),
            })


AIRPORT_TYPES = {"large_airport", "medium_airport", "small_airport", "heliport", "seaplane_base"}


def parse_ourairports(path: Path):
    with _open_text(path) as fh:
        for rec in csv.DictReader(fh):
            if rec.get("type") not in AIRPORT_TYPES:
                continue
            iata = (_clean(rec.get("iata_code")) or "").upper() or None
            icao = (_clean(rec.get("icao_code")) or "").upper() or None
            if not icao:
                for cand in (rec.get("gps_code"), rec.get("ident")):
                    cand = (_clean(cand) or "").upper()
                    if re.match(r"^[A-Z]{4}$", cand):
                        icao = cand
                        break
            if not iata and not icao:
                continue
            ident = _clean(rec.get("ident"))
            if not ident:
                continue
            try:
                lat = round(float(rec.get("latitude_deg")), 6)
                lon = round(float(rec.get("longitude_deg")), 6)
            except (TypeError, ValueError):
                lat = lon = None
            yield ident, _finish("airport", {
                "ident": ident,
                "name": _clean(rec.get("name")),
                "iata": iata,
                "icao": icao,
                "type": _clean(rec.get("type")),
                "municipality": _clean(rec.get("municipality")),
                "country": _clean(rec.get("iso_country")),
                "lat": lat,
                "lon": lon,
            })


def parse_openflights_airlines(path: Path):
    with _open_text(path) as fh:
        for row in csv.reader(fh):
            if len(row) < 8 or row[0] in ("-1", "id"):
                continue
            iata = (_clean(row[3]) or "").upper()
            icao = (_clean(row[4]) or "").upper()
            iata = iata if re.match(r"^[A-Z0-9]{2}$", iata) else None
            icao = icao if re.match(r"^[A-Z]{3}$", icao) else None
            if not iata and not icao:
                continue
            key = f"of:{row[0]}"
            yield key, _finish("airline", {
                "source_key": key,
                "name": _clean(row[1]),
                "alias": _clean(row[2]),
                "iata": iata,
                "icao": icao,
                "callsign": _clean(row[5]),
                "country": _clean(row[6]),
                "active": (_clean(row[7]) or "N").upper() == "Y",
            })


def parse_openflights_planes(path: Path):
    seen = set()
    with _open_text(path) as fh:
        for row in csv.reader(fh):
            if len(row) < 3:
                continue
            icao = (_clean(row[2]) or "").upper()
            if not icao or icao in seen:
                continue
            seen.add(icao)
            yield icao, _finish("type", {"icao": icao, "name": _clean(row[0]), "iata": _clean(row[1])})


def parse_doc8643(path: Path):
    seen = set()
    with _open_text(path) as fh:
        first = fh.readline()
        quote = "'" if first.startswith("'") else '"'
        header = [h.strip().strip("'\"").lower() for h in next(csv.reader([first], quotechar=quote))]
        for row in csv.reader(fh, quotechar=quote):
            rec = dict(zip(header, row))
            icao = (_clean(rec.get("designator")) or "").upper()
            if not icao or icao in seen or len(icao) > 8:
                continue
            seen.add(icao)
            yield icao, _finish("type", {
                "icao": icao,
                "manufacturer": _clean(rec.get("manufacturercode")),
                "model_full": _clean(rec.get("modelfullname")),
                "engine_count": _clean(rec.get("enginecount")),
                "engine_type": _clean(rec.get("enginetype")),
                "description": _clean(rec.get("aircraftdescription")),
            })


PARSERS = {
    "opensky": parse_opensky,
    "ourairports": parse_ourairports,
    "openflights_airlines": parse_openflights_airlines,
    "openflights_planes": parse_openflights_planes,
    "doc8643": parse_doc8643,
}


# ---- staging & diff -----------------------------------------------------------------

STAGE_SQL = """
INSERT INTO import_rows (batch_id, entity, key, data, in_collection, accepted, edited) VALUES %s
ON CONFLICT (batch_id, entity, key) DO UPDATE SET data = CASE
  WHEN (SELECT count(*) FROM jsonb_object_keys(excluded.data))
       >= (SELECT count(*) FROM jsonb_object_keys(import_rows.data))
  THEN excluded.data ELSE import_rows.data END
"""


def merge_records(a: dict, b: dict) -> dict:
    """Combine two source records for the same key (e.g. a registration listed twice):
    start from the one with more fields and fill its gaps from the other."""
    if len(b) > len(a):
        a, b = b, a
    merged = dict(a)
    for k, v in b.items():
        if merged.get(k) in (None, "") and v not in (None, ""):
            merged[k] = v
    return merged


def stage(batch_id: int, entity: str, rows, job_id: int | None = None) -> int:
    """Write parsed rows into import_rows. Duplicate keys are merged in memory first,
    because one INSERT ... ON CONFLICT statement must not touch the same row twice;
    duplicates across chunks are handled by ON CONFLICT."""
    raw = engine.raw_connection()
    count = 0

    def flush(buf: dict):
        values = [(batch_id, entity, key, json.dumps(data), False, True, "{}") for key, data in buf.items()]
        execute_values(cur, STAGE_SQL, values, page_size=1000)
        raw.commit()
        return len(values)

    try:
        cur = raw.cursor()
        buf: dict = {}
        for key, data in rows:
            buf[key] = merge_records(buf[key], data) if key in buf else data
            if len(buf) >= CHUNK:
                count += flush(buf)
                buf = {}
                if job_id:
                    jobs.progress(job_id, count, 0, "stage")
        if buf:
            count += flush(buf)
    finally:
        raw.close()
    return count


def _norm(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return round(value, 5)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, int):
        return value
    return str(value).strip()


def _collection_keys(db, entity: str) -> set:
    if entity == "aircraft":
        q = "SELECT DISTINCT a.registration FROM aircraft a JOIN sightings s ON s.aircraft_id = a.id"
    elif entity == "airport":
        q = ("SELECT ident FROM airports WHERE id IN (SELECT airport_id FROM sightings "
             "UNION SELECT airport_id FROM spots)")
    elif entity == "airline":
        q = ("SELECT DISTINCT al.source_key FROM airlines al JOIN aircraft a ON a.airline_id = al.id "
             "JOIN sightings s ON s.aircraft_id = a.id")
    else:
        q = ("SELECT DISTINCT t.icao FROM aircraft_types t JOIN aircraft a ON a.type_icao = t.icao "
             "JOIN sightings s ON s.aircraft_id = a.id")
    return {r[0] for r in db.execute(text(q))}


def diff(db, batch_id: int, entity: str, job_id: int | None = None) -> dict:
    model, key_col, fields = ENTITIES[entity]
    key_attr = getattr(model, key_col)
    collection = _collection_keys(db, entity)
    stats = {"new": 0, "changed": 0, "changed_in_collection": 0, "unchanged": 0}
    total = db.query(models.ImportRow).filter_by(batch_id=batch_id, entity=entity).count()
    last_id = 0
    processed = 0
    while True:
        rows = (
            db.query(models.ImportRow)
            .filter(models.ImportRow.batch_id == batch_id, models.ImportRow.entity == entity,
                    models.ImportRow.id > last_id)
            .order_by(models.ImportRow.id)
            .limit(CHUNK)
            .all()
        )
        if not rows:
            break
        last_id = rows[-1].id
        existing = {
            getattr(obj, key_col): obj
            for obj in db.query(model).filter(key_attr.in_([r.key for r in rows]))
        }
        for row in rows:
            obj = existing.get(row.key)
            if obj is None:
                row.kind = "new"
                stats["new"] += 1
                continue
            manual = set(obj.manual_fields or [])
            old = {}
            for field in fields:
                if field not in row.data or field in manual:
                    continue
                if _norm(getattr(obj, field)) != _norm(row.data[field]):
                    old[field] = _norm(getattr(obj, field))
            if not old:
                db.delete(row)
                stats["unchanged"] += 1
                continue
            row.kind = "changed"
            row.old = old
            row.in_collection = row.key in collection
            stats["changed"] += 1
            if row.in_collection:
                stats["changed_in_collection"] += 1
        db.commit()
        processed += len(rows)
        if job_id:
            jobs.progress(job_id, processed, total, "diff")
    return stats


# ---- apply ------------------------------------------------------------------------------

def _coerce(entity: str, field: str, value):
    if value is None or value == "":
        return None
    if field in ("lat", "lon"):
        return float(value)
    if field == "active":
        return value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "y", "on")
    limit = _MAXLEN.get((entity, field))
    value = str(value).strip()
    return value[:limit] if limit else value


def apply_batch(db, batch: models.ImportBatch, job_id: int | None = None) -> dict:
    entity = SOURCES[batch.source]["entity"]
    model, key_col, fields = ENTITIES[entity]
    key_attr = getattr(model, key_col)
    q = db.query(models.ImportRow).filter(
        models.ImportRow.batch_id == batch.id, models.ImportRow.accepted.is_(True)
    )
    total = q.count()
    stats = {"inserted": 0, "updated": 0}
    last_id = 0
    done = 0
    while True:
        rows = q.filter(models.ImportRow.id > last_id).order_by(models.ImportRow.id).limit(CHUNK).all()
        if not rows:
            break
        last_id = rows[-1].id
        new_values = []
        changed = {}
        for row in rows:
            if row.kind == "new":
                values = {f: _coerce(entity, f, row.data.get(f)) for f in fields if f in row.data}
                for f, v in (row.edited or {}).items():
                    if f in fields:
                        values[f] = _coerce(entity, f, v)
                values[key_col] = row.key
                values["manual_fields"] = sorted((row.edited or {}).keys())
                if entity == "aircraft":
                    values["source"] = "opensky"
                    values.setdefault("active", True)
                if entity == "airport" and not values.get("name"):
                    values["name"] = row.key
                if entity == "airline" and not values.get("name"):
                    values["name"] = row.key
                new_values.append(values)
            else:
                changed[row.key] = row
        if new_values:
            # rows in one statement need the same columns
            all_cols = set().union(*(v.keys() for v in new_values))
            for v in new_values:
                for c in all_cols:
                    v.setdefault(c, None)
                if v.get("manual_fields") is None:
                    v["manual_fields"] = []
                if entity == "aircraft" and v.get("active") is None:
                    v["active"] = True
                if entity == "airline" and v.get("active") is None:
                    v["active"] = True
            stmt = pg_insert(model).values(new_values).on_conflict_do_nothing(index_elements=[key_col])
            stats["inserted"] += db.execute(stmt).rowcount or 0
        if changed:
            for obj in db.query(model).filter(key_attr.in_(list(changed.keys()))):
                row = changed[getattr(obj, key_col)]
                manual = list(obj.manual_fields or [])
                for f in (row.old or {}):
                    if f in manual:
                        continue
                    setattr(obj, f, _coerce(entity, f, row.data.get(f)))
                for f, v in (row.edited or {}).items():
                    if f in fields:
                        setattr(obj, f, _coerce(entity, f, v))
                        if f not in manual:
                            manual.append(f)
                obj.manual_fields = manual
                stats["updated"] += 1
        db.commit()
        done += len(rows)
        if job_id:
            jobs.progress(job_id, done, total, "apply")
    if entity in ("aircraft", "airline"):
        link_airlines(db)
    return stats


def link_airlines(db):
    """Links aircraft to airlines via the operator ICAO code (unless set by hand)."""
    db.execute(text("""
        UPDATE aircraft a SET airline_id = sub.id
        FROM (SELECT DISTINCT ON (icao) id, icao FROM airlines
              WHERE icao IS NOT NULL ORDER BY icao, active DESC, id) sub
        WHERE a.operator_icao = sub.icao
          AND NOT (a.manual_fields ? 'airline_id')
          AND a.airline_id IS DISTINCT FROM sub.id
    """))
    db.commit()


# ---- jobs -------------------------------------------------------------------------------

@jobs.handler("import_prepare")
def job_prepare(db, job):
    source = job.params["source"]
    entity = SOURCES[source]["entity"]
    path = job.params.get("file")
    if path:
        path = Path(path)
    else:
        jobs.progress(job.id, 0, 0, "download")
        path = download(source, job.id)

    # only one open review per source
    for old in db.query(models.ImportBatch).filter(
        models.ImportBatch.source == source, models.ImportBatch.status.in_(["loading", "ready"])
    ):
        old.status = "discarded"
        db.query(models.ImportRow).filter_by(batch_id=old.id).delete()
    batch = models.ImportBatch(source=source, status="loading", stats={})
    db.add(batch)
    db.commit()
    try:
        staged = stage(batch.id, entity, PARSERS[source](path), job.id)
        if staged == 0:
            raise ValueError("no usable rows found in file")
        stats = diff(db, batch.id, entity, job.id)
        stats["rows"] = staged
        batch.stats = stats
        batch.status = "ready"
        db.commit()
    except Exception as exc:
        db.rollback()
        batch = db.get(models.ImportBatch, batch.id)
        batch.status = "failed"
        batch.message = str(exc)[:1000]
        db.query(models.ImportRow).filter_by(batch_id=batch.id).delete()
        db.commit()
        raise
    return {"batch_id": batch.id, **batch.stats}


@jobs.handler("import_apply")
def job_apply(db, job):
    batch = db.get(models.ImportBatch, job.params["batch_id"])
    if not batch or batch.status != "ready":
        raise ValueError("import is not ready to be applied")
    batch.status = "applying"
    db.commit()
    stats = apply_batch(db, batch, job.id)
    batch.status = "applied"
    batch.stats = {**batch.stats, **stats}
    db.query(models.ImportRow).filter_by(batch_id=batch.id).delete()
    db.commit()
    return stats


def save_upload(source: str, fileobj) -> Path:
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    target = IMPORT_DIR / f"{source}.upload.csv"
    with open(target, "wb") as out:
        while True:
            chunk = fileobj.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
    return target

