"""Photo archive scan (NAS mode), photo upload (upload mode) and shared photo handling."""
import logging
import os
import shutil
from datetime import date
from pathlib import Path

from sqlalchemy import func, or_

import jobs
import models
import photos

logger = logging.getLogger("scan")

AIRCRAFT_FIELDS = ("registration", "airline", "type_icao", "christening_name", "livery", "reg_unknown",
                   "aircraft_id")
DATE_FIELDS = ("date", "airport")


# ---- lookups ---------------------------------------------------------------------------

def find_airport(db, code: str | None):
    if not code:
        return None
    code = code.strip().upper()
    q = db.query(models.Airport)
    if len(code) == 3:
        hit = q.filter(models.Airport.iata == code).order_by(models.Airport.type).first()
        if hit:
            return hit
    hit = q.filter(models.Airport.icao == code).first()
    if hit:
        return hit
    return q.filter(models.Airport.ident == code).first()


def find_aircraft(db, registration: str | None):
    """Aircraft by registration; also finds "DAIJC" when the database has "D-AIJC"."""
    reg = photos.normalize_registration(registration or "")
    if not reg:
        return None
    hit = db.query(models.Aircraft).filter(models.Aircraft.registration == reg).first()
    if hit or "-" in reg:
        return hit
    return (
        db.query(models.Aircraft)
        .filter(func.replace(models.Aircraft.registration, "-", "") == reg)
        .order_by(models.Aircraft.id)
        .first()
    )


def create_unknown_aircraft(db, source: str = "manual") -> models.Aircraft:
    """New aircraft whose registration is not known yet, with placeholder registration "?-<n>"."""
    numbers = [int(r[2:]) for (r,) in db.query(models.Aircraft.registration)
               .filter(models.Aircraft.registration.like("?-%")) if r[2:].isdigit()]
    ac = models.Aircraft(registration=f"?-{max(numbers, default=0) + 1}", reg_unknown=True, source=source,
                         active=True, manual_fields=[])
    db.add(ac)
    db.flush()
    return ac


def set_registration(db, ac: models.Aircraft, registration: str) -> models.Aircraft:
    """Gives an aircraft with unknown registration its real one. If that registration already
    exists (e.g. from the master data), the sightings move over and the placeholder is removed.
    Returns the aircraft that now holds the sightings."""
    reg = photos.normalize_registration(registration)
    target = find_aircraft(db, reg)
    if target and target.id != ac.id:
        for s in list(ac.sightings):
            s.aircraft_id = target.id
        manual = list(target.manual_fields or [])
        for field in ("type_icao", "christening_name", "notes", "airline_id"):
            if getattr(ac, field) and not getattr(target, field):
                setattr(target, field, getattr(ac, field))
                if field not in manual:
                    manual.append(field)
        target.manual_fields = manual
        if not target.cover_photo_id and ac.cover_photo_id:
            target.cover_photo_id = ac.cover_photo_id
        ac.cover_photo_id = None
        db.flush()
        db.expire(ac, ["sightings"])
        db.delete(ac)
    else:
        target = ac
        ac.registration = reg
        ac.reg_unknown = False
    # scan corrections that pointed to the placeholder now carry the real registration
    for row in db.query(models.FolderOverride).all():
        if row.data.get("aircraft_id") == ac.id:
            data = {k: v for k, v in row.data.items() if k not in ("aircraft_id", "reg_unknown")}
            data["registration"] = target.registration
            row.data = data
    db.flush()
    return target


def find_or_create_airline(db, name: str | None):
    name = (name or "").strip()
    if not name:
        return None
    # exact name or alias first, then "<name> ..." (e.g. "Condor" -> "Condor Flugdienst"); active airlines win
    lowered = name.lower()
    exact = or_(func.lower(models.Airline.name) == lowered, func.lower(models.Airline.alias) == lowered)
    candidates = (
        db.query(models.Airline)
        .filter(or_(exact, func.lower(models.Airline.name).like(lowered.replace("%", "") + " %")))
        .all()
    )
    if candidates:
        def rank(a):
            is_exact = a.name.lower() == lowered or (a.alias or "").lower() == lowered
            return (not a.active, not is_exact, a.id)
        return sorted(candidates, key=rank)[0]
    hit = db.query(models.Airline).filter(models.Airline.source_key == f"manual:{name.lower()}").first()
    if hit:
        return hit
    airline = models.Airline(source_key=f"manual:{name.lower()}"[:64], name=name, active=True,
                             manual_fields=["name"])
    db.add(airline)
    db.flush()
    return airline


def aircraft_folder(folder: str) -> str:
    parts = folder.split("/")
    return "/".join(parts[:3]) if len(parts) >= 3 else folder


# ---- analysis --------------------------------------------------------------------------

def analyze(db, folder: str) -> tuple[dict, list[str], int | None]:
    """Parsed values (with remembered corrections), problems and a matching sighting."""
    parsed, _ = photos.parse_folder(folder)
    corrected = False
    unknown_id = None
    parsed["reg_unknown"] = False
    for key in (aircraft_folder(folder), folder):
        override = db.get(models.FolderOverride, key)
        if override:
            parsed.update({k: v for k, v in override.data.items() if k in parsed})
            corrected = corrected or bool(override.data.get("registration"))
            unknown_id = override.data.get("aircraft_id", unknown_id)
    problems = []
    if parsed["reg_unknown"]:
        # registration marked as unknown: one placeholder aircraft per aircraft folder
        aircraft = db.get(models.Aircraft, unknown_id) if unknown_id else None
        reg = aircraft.registration if aircraft else ""
    else:
        reg = photos.normalize_registration(parsed.get("registration") or "")
        aircraft = find_aircraft(db, reg) if reg else None
    if aircraft:
        reg = aircraft.registration
    parsed["registration"] = reg or None
    parsed["known_aircraft"] = aircraft is not None
    # a registration entered by hand is trusted (military serials, unusual formats)
    if not parsed["reg_unknown"] and (not reg or not (aircraft or photos.REG_RE.match(reg)
                                                       or (corrected and photos.PLAUSIBLE_REG_RE.match(reg)))):
        problems.append("registration")
    day = None
    try:
        day = date.fromisoformat(parsed.get("date") or "")
    except ValueError:
        problems.append("date")
    airport = find_airport(db, parsed.get("airport"))
    if not airport:
        problems.append("airport")
    sighting_id = None
    if aircraft and day:
        existing = db.query(models.Sighting).filter_by(aircraft_id=aircraft.id, folder=folder).first()
        if not existing:
            q = db.query(models.Sighting).filter_by(aircraft_id=aircraft.id, date=day)
            if airport:
                q = q.filter(models.Sighting.airport_id == airport.id)
            existing = q.filter(models.Sighting.folder.is_(None)).first()
        if existing:
            sighting_id = existing.id
    return parsed, problems, sighting_id


def save_override(db, folder: str, values: dict):
    """Remembers corrections: aircraft fields per aircraft folder, date fields per date folder."""
    for key, fields in ((aircraft_folder(folder), AIRCRAFT_FIELDS), (folder, DATE_FIELDS)):
        data = {k: values[k] for k in fields if k in values}
        if not data:
            continue
        row = db.get(models.FolderOverride, key)
        if row:
            row.data = {**row.data, **data}
        else:
            db.add(models.FolderOverride(folder=key, data=data))
    db.flush()


# ---- photos ------------------------------------------------------------------------------

def register_photo(db, sighting: models.Sighting, rel_path: str, make_thumbs: bool = True) -> models.Photo:
    full = photos.safe_join(rel_path)
    stat = full.stat()
    exif = photos.read_exif(full)
    photo = models.Photo(sighting_id=sighting.id, path=rel_path, size=stat.st_size, mtime=stat.st_mtime,
                         exif_read=True, **exif)
    db.add(photo)
    db.flush()
    if make_thumbs:
        size = photos.make_thumbnails(photo.id, full)
        if size:
            photo.width, photo.height = size
    if exif.get("gps_lat") is not None and sighting.spot_id is None and sighting.airport_id:
        spots = db.query(models.Spot).filter_by(airport_id=sighting.airport_id).all()
        spot = photos.nearest_spot(spots, exif["gps_lat"], exif["gps_lon"])
        if spot:
            sighting.spot_id = spot.id
    aircraft = sighting.aircraft
    # first photo becomes the cover; a JPEG/HEIF is preferred over a RAW file
    current = db.get(models.Photo, aircraft.cover_photo_id) if aircraft.cover_photo_id else None
    if current is None or (current.is_raw and not photo.is_raw):
        aircraft.cover_photo_id = photo.id
    return photo


def sighting_folder(db, sighting: models.Sighting) -> str:
    if sighting.folder:
        return sighting.folder
    ac = sighting.aircraft
    airline = ac.airline.name if ac.airline else (ac.operator_name or "Unknown")
    code = sighting.airport.code if sighting.airport else None
    folder = photos.build_folder(airline, ac.type_icao, ac.registration, ac.christening_name,
                                 sighting.livery, sighting.date, code)
    sighting.folder = folder
    return folder


def store_upload(db, sighting: models.Sighting, filename: str, fileobj) -> models.Photo | None:
    """Saves an uploaded photo in the folder scheme (upload mode only)."""
    if photos.photo_mode() != "upload":
        raise PermissionError("uploads are disabled in NAS mode")
    if not photos.is_photo(filename or ""):
        return None
    folder = sighting_folder(db, sighting)
    target_dir = photos.safe_join(folder)
    target_dir.mkdir(parents=True, exist_ok=True)
    name = photos.unique_name(target_dir, os.path.basename(filename))
    with open(target_dir / name, "wb") as out:
        shutil.copyfileobj(fileobj, out)
    return register_photo(db, sighting, f"{folder}/{name}")


def delete_photo(db, photo: models.Photo):
    """Removes a photo from the database; in upload mode the file is deleted as well."""
    if photos.photo_mode() == "upload":
        try:
            photos.safe_join(photo.path).unlink(missing_ok=True)
        except ValueError:
            pass
    photos.delete_thumbnails(photo.id)
    ac = photo.sighting.aircraft
    if ac.cover_photo_id == photo.id:
        ac.cover_photo_id = None
    db.delete(photo)


# ---- jobs --------------------------------------------------------------------------------

def _readable(name: str) -> bool:
    """False for file names that are not valid UTF-8 (e.g. Latin-1 umlauts from old SMB clients).
    Python keeps those bytes as surrogates, which cannot be stored in the database."""
    try:
        name.encode("utf-8")
        return True
    except UnicodeEncodeError:
        return False


def _printable(path: str) -> str:
    return path.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


@jobs.handler("scan")
def job_scan(db, job):
    root = photos.photo_dir()
    if not root.exists():
        raise FileNotFoundError(f"photo folder {root} does not exist")
    known = {p for (p,) in db.query(models.Photo.path)}
    groups: dict[str, list[str]] = {}
    unreadable: list[str] = []
    dirs = 0
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        visible = sorted(d for d in dirnames if not d.startswith((".", "@")))
        for d in visible:
            if not _readable(d):
                unreadable.append(_printable(d if rel_dir == "." else f"{rel_dir}/{d}"))
        dirnames[:] = [d for d in visible if _readable(d)]
        dirs += 1
        if dirs % 200 == 0:
            jobs.progress(job.id, dirs, 0, "walk")
        if rel_dir == ".":
            continue
        for name in sorted(filenames):
            if not photos.is_photo(name):
                continue
            if not _readable(name):
                unreadable.append(_printable(f"{rel_dir}/{name}"))
                continue
            rel = f"{rel_dir}/{name}"
            if rel in known:
                continue
            parts = rel_dir.split("/")
            key = "/".join(parts[:4])
            groups.setdefault(key, []).append(rel)
    missing = sum(1 for p in known if not (root / p).exists())

    # entries of older, unfinished scans are replaced
    old_jobs = [j.id for j in db.query(models.Job.id).filter(models.Job.kind == "scan", models.Job.id != job.id)]
    if old_jobs:
        db.query(models.ScanEntry).filter(models.ScanEntry.job_id.in_(old_jobs)).delete(synchronize_session=False)
    total = len(groups)
    with_problems = 0
    for i, (folder, files) in enumerate(sorted(groups.items()), 1):
        parsed, problems, sighting_id = analyze(db, folder)
        if problems:
            with_problems += 1
        db.add(models.ScanEntry(job_id=job.id, folder=folder, parsed=parsed, problems=problems,
                                files=files, sighting_id=sighting_id, accepted=not problems))
        if i % 100 == 0:
            db.commit()
            jobs.progress(job.id, i, total, "analyze")
    db.commit()
    result = {"folders": total, "files": sum(len(f) for f in groups.values()),
              "problems": with_problems, "missing": missing}
    if unreadable:
        logger.warning("Skipped %d names that are not valid UTF-8, e.g. %s", len(unreadable), unreadable[0])
        result["unreadable"] = len(unreadable)
        result["unreadable_paths"] = unreadable[:50]
    return result


def apply_entry(db, entry: models.ScanEntry, make_thumbs: bool = True) -> int:
    parsed, problems, sighting_id = analyze(db, entry.folder)
    if problems:
        entry.parsed, entry.problems = parsed, problems
        return 0
    reg = parsed["registration"]
    aircraft = find_aircraft(db, reg) if reg else None
    airline = find_or_create_airline(db, parsed.get("airline"))
    if not aircraft and parsed.get("reg_unknown"):
        aircraft = create_unknown_aircraft(db, source="scan")
        save_override(db, entry.folder, {"aircraft_id": aircraft.id})
    elif not aircraft:
        aircraft = models.Aircraft(registration=reg, source="scan", active=True, manual_fields=[])
        db.add(aircraft)
    manual = list(aircraft.manual_fields or [])

    def set_field(field, value):
        if value and not getattr(aircraft, field):
            setattr(aircraft, field, value)
            if field not in manual:
                manual.append(field)

    set_field("type_icao", parsed.get("type_icao"))
    set_field("christening_name", parsed.get("christening_name"))
    if airline and not aircraft.airline_id:
        aircraft.airline_id = airline.id
        aircraft.operator_name = aircraft.operator_name or airline.name
        if "airline_id" not in manual:
            manual.append("airline_id")
    aircraft.manual_fields = manual
    db.flush()

    sighting = db.get(models.Sighting, sighting_id) if sighting_id else None
    if not sighting:
        airport = find_airport(db, parsed.get("airport"))
        sighting = models.Sighting(aircraft_id=aircraft.id, date=date.fromisoformat(parsed["date"]),
                                   airport_id=airport.id if airport else None, livery=parsed.get("livery"))
        db.add(sighting)
        db.flush()
    if not sighting.folder:
        sighting.folder = entry.folder
    if not sighting.livery and parsed.get("livery"):
        sighting.livery = parsed["livery"]
    db.flush()
    db.refresh(sighting)
    added = 0
    existing = {p for (p,) in db.query(models.Photo.path).filter(models.Photo.path.in_(entry.files))}
    for rel in entry.files:
        if rel in existing:
            continue
        try:
            register_photo(db, sighting, rel, make_thumbs=make_thumbs)
            added += 1
        except (OSError, ValueError):
            logger.warning("Photo skipped: %s", rel)
    entry.applied = True
    entry.sighting_id = sighting.id
    return added


@jobs.handler("scan_apply")
def job_scan_apply(db, job):
    entries = (
        db.query(models.ScanEntry)
        .filter(models.ScanEntry.job_id == job.params["scan_job_id"], models.ScanEntry.accepted.is_(True),
                models.ScanEntry.applied.is_(False))
        .order_by(models.ScanEntry.id)
        .all()
    )
    total = sum(len(e.files) for e in entries)
    done = added = skipped = 0
    for entry in entries:
        n = apply_entry(db, entry)
        if entry.applied:
            added += n
        else:
            skipped += 1
        db.commit()
        done += len(entry.files)
        jobs.progress(job.id, done, total, "apply")
    return {"photos": added, "skipped": skipped}


@jobs.handler("thumbnails")
def job_thumbnails(db, job):
    """Creates missing thumbnails and reads missing EXIF data."""
    ids = [i for (i,) in db.query(models.Photo.id).order_by(models.Photo.id)]
    created = 0
    for n, pid in enumerate(ids, 1):
        photo = db.get(models.Photo, pid)
        try:
            source = photos.safe_join(photo.path)
        except ValueError:
            continue
        if not source.exists():
            continue
        if not photo.exif_read:
            for k, v in photos.read_exif(source).items():
                setattr(photo, k, v)
            photo.exif_read = True
        if not photos.thumb_path(photo.id, "s").exists() or job.params.get("force"):
            size = photos.make_thumbnails(photo.id, source)
            if size:
                photo.width, photo.height = size
                created += 1
        if n % 50 == 0:
            db.commit()
            jobs.progress(job.id, n, len(ids), "thumbnails")
    db.commit()
    return {"created": created}


@jobs.handler("remove_missing")
def job_remove_missing(db, job):
    """Removes photos whose files no longer exist."""
    root = photos.photo_dir()
    removed = 0
    for photo in db.query(models.Photo).all():
        if not (root / photo.path).exists():
            delete_photo(db, photo)
            removed += 1
    db.commit()
    return {"removed": removed}


def path_exists(rel: str) -> bool:
    try:
        return Path(photos.safe_join(rel)).exists()
    except ValueError:
        return False
