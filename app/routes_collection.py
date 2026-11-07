"""Collection pages: dashboard, aircraft, sightings, photos, airlines, airports, map, export."""
import csv
import io
import math
import os
import shutil
import tempfile
from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session, joinedload, selectinload

import models
import photos
import queries
import scan
from database import get_db
from web import flash, get_admin, get_user, render, t

router = APIRouter()

AIRCRAFT_EDIT_FIELDS = ["msn", "line_number", "type_icao", "model", "manufacturer", "christening_name",
                        "built", "first_flight", "delivery_date", "production_site", "engines", "notes"]


def _redirect(url: str):
    return RedirectResponse(url, status_code=303)


def _int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _date(value):
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        return None


# ---- dashboard ---------------------------------------------------------------------------

@router.get("/")
def dashboard(request: Request, user=Depends(get_user), db: Session = Depends(get_db)):
    recent = (
        db.query(models.Sighting)
        .options(joinedload(models.Sighting.aircraft), joinedload(models.Sighting.airport))
        .order_by(models.Sighting.date.desc(), models.Sighting.id.desc())
        .limit(10)
        .all()
    )
    return render(request, "dashboard.html", {
        "totals": queries.totals(db),
        "airlines": queries.by_airline(db),
        "types": queries.by_type(db),
        "airports": queries.by_airport(db),
        "years": queries.by_year(db),
        "months": queries.new_per_month(db),
        "cameras": queries.by_camera(db),
        "lenses": queries.by_lens(db),
        "fleets": queries.fleet_completion(db, limit=10),
        "recent": recent,
        "has_master_data": db.query(models.Airport.id).first() is not None,
    })


# ---- aircraft ------------------------------------------------------------------------------

@router.get("/aircraft")
def aircraft_list(request: Request, q: str = "", field: str = "all", scope: str = "collection",
                  sort: str = "last_seen", page: int = 1, airline: int | None = None, type: str | None = None,
                  user=Depends(get_user), db: Session = Depends(get_db)):
    if field not in queries.SEARCH_FIELDS:
        field = "all"
    page = max(page, 1)
    rows, total = queries.search_aircraft(db, q, field, scope, sort, page, airline_id=airline, type_icao=type)
    return render(request, "aircraft_list.html", {
        "rows": rows, "total": total, "q": q, "field": field, "scope": scope, "sort": sort, "page": page,
        "pages": max(1, math.ceil(total / 50)), "fields": queries.SEARCH_FIELDS,
        "airline": db.get(models.Airline, airline) if airline else None, "type": type,
    })


def _aircraft_or_404(db, aircraft_id: int) -> models.Aircraft:
    ac = (
        db.query(models.Aircraft)
        .options(selectinload(models.Aircraft.sightings).selectinload(models.Sighting.photos),
                 selectinload(models.Aircraft.sightings).joinedload(models.Sighting.airport),
                 selectinload(models.Aircraft.sightings).joinedload(models.Sighting.spot))
        .filter(models.Aircraft.id == aircraft_id)
        .first()
    )
    if not ac:
        raise HTTPException(404)
    return ac


@router.get("/aircraft/{aircraft_id}")
def aircraft_detail(aircraft_id: int, request: Request, user=Depends(get_user), db: Session = Depends(get_db)):
    ac = _aircraft_or_404(db, aircraft_id)
    same_msn = []
    if ac.msn and ac.type_icao:
        same_msn = (
            db.query(models.Aircraft)
            .filter(models.Aircraft.msn == ac.msn, models.Aircraft.type_icao == ac.type_icao,
                    models.Aircraft.id != ac.id)
            .order_by(models.Aircraft.registration)
            .all()
        )
    return render(request, "aircraft_detail.html", {"ac": ac, "same_msn": same_msn})


def _resolve_airline(db, value: str):
    value = (value or "").strip()
    if not value:
        return None
    code = value.upper()
    if len(code) in (2, 3) and code.isalnum():
        col = models.Airline.icao if len(code) == 3 else models.Airline.iata
        hit = db.query(models.Airline).filter(col == code).order_by(models.Airline.active.desc()).first()
        if hit:
            return hit
    return scan.find_or_create_airline(db, value)


@router.get("/aircraft/{aircraft_id}/edit")
def aircraft_edit_form(aircraft_id: int, request: Request, user=Depends(get_admin), db: Session = Depends(get_db)):
    return render(request, "aircraft_edit.html", {"ac": _aircraft_or_404(db, aircraft_id),
                                                  "fields": AIRCRAFT_EDIT_FIELDS})


@router.post("/aircraft/{aircraft_id}/edit")
async def aircraft_edit(aircraft_id: int, request: Request, user=Depends(get_admin), db: Session = Depends(get_db)):
    ac = _aircraft_or_404(db, aircraft_id)
    form = await request.form()
    manual = list(ac.manual_fields or [])
    for field in AIRCRAFT_EDIT_FIELDS:
        raw = (form.get(field) or "").strip()
        if field == "delivery_date":
            value = _date(raw)
        elif field == "type_icao":
            value = raw.upper() or None
        else:
            value = raw or None
        current = getattr(ac, field)
        if (current or None) != value:
            setattr(ac, field, value)
            if field not in manual:
                manual.append(field)
    airline_value = (form.get("airline") or "").strip()
    current_airline = ac.airline.name if ac.airline else ""
    if airline_value != current_airline:
        airline = _resolve_airline(db, airline_value)
        ac.airline_id = airline.id if airline else None
        if "airline_id" not in manual:
            manual.append("airline_id")
    active = form.get("active") == "on"
    if active != ac.active:
        ac.active = active
    for field in form.getlist("unlock"):
        if field in manual:
            manual.remove(field)
    ac.manual_fields = manual
    db.commit()
    flash(request, "saved")
    return _redirect(f"/aircraft/{ac.id}")


@router.post("/aircraft/{aircraft_id}/active")
def aircraft_toggle_active(aircraft_id: int, request: Request, next: str = Form(""),
                           user=Depends(get_admin), db: Session = Depends(get_db)):
    ac = db.get(models.Aircraft, aircraft_id)
    if not ac:
        raise HTTPException(404)
    ac.active = not ac.active
    db.commit()
    return _redirect(next if next.startswith("/") else f"/aircraft/{ac.id}")


@router.post("/aircraft/{aircraft_id}/delete")
def aircraft_delete(aircraft_id: int, request: Request, user=Depends(get_admin), db: Session = Depends(get_db)):
    ac = _aircraft_or_404(db, aircraft_id)
    for s in ac.sightings:
        for p in list(s.photos):
            scan.delete_photo(db, p)
    ac.cover_photo_id = None
    db.flush()
    if ac.source in ("manual", "scan"):
        db.delete(ac)
    else:
        for s in list(ac.sightings):
            db.delete(s)
    db.commit()
    flash(request, "deleted")
    return _redirect("/aircraft")


# ---- sightings -----------------------------------------------------------------------------

def _sighting_or_404(db, sighting_id: int) -> models.Sighting:
    s = db.get(models.Sighting, sighting_id)
    if not s:
        raise HTTPException(404)
    return s


@router.get("/sightings/new")
def sighting_new_form(request: Request, reg: str = "", user=Depends(get_admin), db: Session = Depends(get_db)):
    last = db.query(models.Sighting).order_by(models.Sighting.id.desc()).first()
    return render(request, "sighting_form.html", {
        "sighting": None, "reg": reg, "today": date.today().isoformat(),
        "last_airport": last.airport if last and last.airport else None,
        "last_spot_id": last.spot_id if last else None, "error": None, "form": {},
    })


def _store_files(db, sighting, files: list[UploadFile]) -> int:
    count = 0
    for f in files:
        if not f or not f.filename:
            continue
        if scan.store_upload(db, sighting, f.filename, f.file):
            count += 1
    return count


@router.post("/sightings/new")
async def sighting_new(request: Request, user=Depends(get_admin), db: Session = Depends(get_db)):
    form = await request.form()
    values = {k: (form.get(k) or "").strip() for k in
              ("registration", "date", "airport", "spot_id", "livery", "notes", "type_icao", "airline",
               "christening_name")}
    files = [f for f in form.getlist("photos") if getattr(f, "filename", None)]

    def fail(key):
        last = db.query(models.Sighting).order_by(models.Sighting.id.desc()).first()
        return render(request, "sighting_form.html", {
            "sighting": None, "reg": values["registration"], "today": date.today().isoformat(),
            "last_airport": None, "last_spot_id": last.spot_id if last else None,
            "error": t(request, key), "form": values,
        }, status_code=400)

    reg = photos.normalize_registration(values["registration"])
    if not reg:
        return fail("sighting.error_registration")
    airport = scan.find_airport(db, values["airport"]) if values["airport"] else None
    if values["airport"] and not airport:
        return fail("sighting.error_airport")
    if files and photos.photo_mode() != "upload":
        return fail("photos.upload_disabled")

    day = _date(values["date"])
    tmpdir = None
    if not day and files:
        # take the capture date of the first photo
        tmpdir = tempfile.mkdtemp(dir=str(photos.DATA_DIR)) if photos.DATA_DIR.exists() else tempfile.mkdtemp()
        first = files[0]
        tmp = os.path.join(tmpdir, "probe" + os.path.splitext(first.filename)[1].lower())
        with open(tmp, "wb") as out:
            shutil.copyfileobj(first.file, out)
        first.file.seek(0)
        taken = photos.read_exif(photos.Path(tmp)).get("taken_at")
        day = taken.date() if taken else None
        shutil.rmtree(tmpdir, ignore_errors=True)
    day = day or date.today()

    ac = scan.find_aircraft(db, reg)
    if not ac:
        ac = models.Aircraft(registration=reg, source="manual", active=True, manual_fields=[])
        manual = []
        if values["type_icao"]:
            ac.type_icao = values["type_icao"].upper()
            manual.append("type_icao")
        if values["christening_name"]:
            ac.christening_name = values["christening_name"]
            manual.append("christening_name")
        airline = _resolve_airline(db, values["airline"])
        if airline:
            ac.airline_id = airline.id
            ac.operator_name = airline.name
            manual.append("airline_id")
        ac.manual_fields = manual
        db.add(ac)
        db.flush()

    spot_id = _int(values["spot_id"])
    if spot_id:
        spot = db.get(models.Spot, spot_id)
        if not spot or (airport and spot.airport_id != airport.id):
            spot_id = None
        elif not airport:
            airport = spot.airport
    sighting = models.Sighting(aircraft_id=ac.id, date=day, airport_id=airport.id if airport else None,
                               spot_id=spot_id, livery=values["livery"] or None, notes=values["notes"] or None)
    db.add(sighting)
    db.flush()
    db.refresh(sighting)
    count = _store_files(db, sighting, files)
    db.commit()
    flash(request, "sighting_created", count=count)
    return _redirect(f"/aircraft/{ac.id}#s{sighting.id}")


@router.get("/sightings/{sighting_id}/edit")
def sighting_edit_form(sighting_id: int, request: Request, user=Depends(get_admin), db: Session = Depends(get_db)):
    s = _sighting_or_404(db, sighting_id)
    return render(request, "sighting_form.html", {
        "sighting": s, "reg": s.aircraft.registration, "today": s.date.isoformat(),
        "last_airport": s.airport, "last_spot_id": s.spot_id, "error": None, "form": {},
    })


@router.post("/sightings/{sighting_id}/edit")
async def sighting_edit(sighting_id: int, request: Request, user=Depends(get_admin), db: Session = Depends(get_db)):
    s = _sighting_or_404(db, sighting_id)
    form = await request.form()
    day = _date(form.get("date"))
    if day:
        s.date = day
    code = (form.get("airport") or "").strip()
    airport = scan.find_airport(db, code) if code else None
    if code and not airport:
        flash(request, "airport_unknown", code=code)
        return _redirect(f"/sightings/{s.id}/edit")
    s.airport_id = airport.id if airport else None
    spot = db.get(models.Spot, _int(form.get("spot_id"))) if _int(form.get("spot_id")) else None
    s.spot_id = spot.id if spot and (not airport or spot.airport_id == airport.id) else None
    s.livery = (form.get("livery") or "").strip() or None
    s.notes = (form.get("notes") or "").strip() or None
    files = [f for f in form.getlist("photos") if getattr(f, "filename", None)]
    count = 0
    if files:
        if photos.photo_mode() != "upload":
            flash(request, "upload_disabled")
            return _redirect(f"/sightings/{s.id}/edit")
        count = _store_files(db, s, files)
    db.commit()
    flash(request, "saved_photos", count=count) if count else flash(request, "saved")
    return _redirect(f"/aircraft/{s.aircraft_id}#s{s.id}")


@router.post("/sightings/{sighting_id}/delete")
def sighting_delete(sighting_id: int, request: Request, user=Depends(get_admin), db: Session = Depends(get_db)):
    s = _sighting_or_404(db, sighting_id)
    aircraft_id = s.aircraft_id
    for p in list(s.photos):
        scan.delete_photo(db, p)
    db.delete(s)
    db.commit()
    flash(request, "deleted")
    return _redirect(f"/aircraft/{aircraft_id}")


# ---- photos ----------------------------------------------------------------------------------

def _photo_or_404(db, photo_id: int) -> models.Photo:
    p = db.get(models.Photo, photo_id)
    if not p:
        raise HTTPException(404)
    return p


@router.get("/photos/{photo_id}")
def photo_page(photo_id: int, request: Request, user=Depends(get_user), db: Session = Depends(get_db)):
    p = _photo_or_404(db, photo_id)
    siblings = [x.id for x in p.sighting.gallery]
    if p.id not in siblings:
        siblings = [x.id for x in p.sighting.photos]
    idx = siblings.index(p.id)
    return render(request, "photo.html", {
        "photo": p, "s": p.sighting, "ac": p.sighting.aircraft,
        "prev_id": siblings[idx - 1] if idx > 0 else None,
        "next_id": siblings[idx + 1] if idx < len(siblings) - 1 else None,
        "exists": scan.path_exists(p.path),
    })


@router.get("/photos/{photo_id}/thumb/{size}")
def photo_thumb(photo_id: int, size: str, user=Depends(get_user), db: Session = Depends(get_db)):
    if size not in photos.THUMB_SIZES:
        raise HTTPException(404)
    path = photos.ensure_thumbnail(_photo_or_404(db, photo_id), size)
    if not path:
        return FileResponse(os.path.join(os.path.dirname(__file__), "static", "no-photo.svg"),
                            media_type="image/svg+xml")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@router.get("/photos/{photo_id}/original")
def photo_original(photo_id: int, user=Depends(get_user), db: Session = Depends(get_db)):
    p = _photo_or_404(db, photo_id)
    try:
        path = photos.safe_join(p.path)
    except ValueError:
        raise HTTPException(404)
    if not path.exists():
        raise HTTPException(404)
    inline = path.suffix.lower() in (".jpg", ".jpeg", ".png")
    return FileResponse(path, filename=path.name, content_disposition_type="inline" if inline else "attachment")


@router.post("/photos/{photo_id}/cover")
def photo_cover(photo_id: int, request: Request, user=Depends(get_admin), db: Session = Depends(get_db)):
    p = _photo_or_404(db, photo_id)
    p.sighting.aircraft.cover_photo_id = p.id
    db.commit()
    flash(request, "cover_set")
    return _redirect(f"/photos/{p.id}")


@router.post("/photos/{photo_id}/delete")
def photo_delete(photo_id: int, request: Request, user=Depends(get_admin), db: Session = Depends(get_db)):
    p = _photo_or_404(db, photo_id)
    aircraft_id = p.sighting.aircraft_id
    scan.delete_photo(db, p)
    db.commit()
    flash(request, "deleted")
    return _redirect(f"/aircraft/{aircraft_id}")


# ---- airlines & fleets -------------------------------------------------------------------------

@router.get("/airlines")
def airlines(request: Request, user=Depends(get_user), db: Session = Depends(get_db)):
    return render(request, "airlines.html", {"fleets": queries.fleet_completion(db)})


@router.get("/airlines/{airline_id}")
def airline_fleet(airline_id: int, request: Request, show: str = "all", user=Depends(get_user),
                  db: Session = Depends(get_db)):
    airline = db.get(models.Airline, airline_id)
    if not airline:
        raise HTTPException(404)
    rows = queries.fleet(db, airline_id, show)
    all_rows = queries.fleet(db, airline_id, "all") if show != "all" else rows
    active = [r for r in all_rows if r[0].active]
    x = sum(1 for r in active if r[1])
    return render(request, "airline_fleet.html", {
        "airline": airline, "rows": rows, "show": show, "x": x, "y": len(active),
        "inactive": len(all_rows) - len(active),
    })


# ---- airports & spots ----------------------------------------------------------------------------

@router.get("/airports")
def airports(request: Request, q: str = "", user=Depends(get_user), db: Session = Depends(get_db)):
    visited = (
        db.query(models.Airport, func.count(models.Sighting.id).label("n"))
        .join(models.Sighting, models.Sighting.airport_id == models.Airport.id)
        .group_by(models.Airport.id)
        .order_by(func.count(models.Sighting.id).desc())
        .all()
    )
    results = _airport_search(db, q, 50) if q.strip() else []
    return render(request, "airports.html", {"visited": visited, "results": results, "q": q})


def _airport_search(db, q: str, limit: int = 20):
    q = q.strip()
    if not q:
        return []
    like = f"%{q}%"
    code = q.upper()
    rank = case((models.Airport.iata == code, 0), (models.Airport.icao == code, 1), else_=2)
    size = case((models.Airport.type == "large_airport", 0), (models.Airport.type == "medium_airport", 1), else_=2)
    return (
        db.query(models.Airport)
        .filter(or_(models.Airport.iata == code, models.Airport.icao == code, models.Airport.name.ilike(like),
                    models.Airport.municipality.ilike(like)))
        .order_by(rank, size, models.Airport.name)
        .limit(limit)
        .all()
    )


@router.get("/airports/{airport_id}")
def airport_detail(airport_id: int, request: Request, user=Depends(get_user), db: Session = Depends(get_db)):
    airport = db.get(models.Airport, airport_id)
    if not airport:
        raise HTTPException(404)
    counts = dict(
        db.query(models.Sighting.spot_id, func.count(models.Sighting.id))
        .filter(models.Sighting.airport_id == airport.id)
        .group_by(models.Sighting.spot_id)
        .all()
    )
    recent = (
        db.query(models.Sighting)
        .filter(models.Sighting.airport_id == airport.id)
        .options(joinedload(models.Sighting.aircraft))
        .order_by(models.Sighting.date.desc())
        .limit(20)
        .all()
    )
    return render(request, "airport_detail.html", {"airport": airport, "counts": counts, "recent": recent,
                                                   "total": sum(counts.values())})


@router.post("/airports/{airport_id}/edit")
def airport_edit(airport_id: int, request: Request, name: str = Form(...), user=Depends(get_admin),
                 db: Session = Depends(get_db)):
    airport = db.get(models.Airport, airport_id)
    if not airport:
        raise HTTPException(404)
    name = name.strip()
    if name and name != airport.name:
        airport.name = name
        manual = list(airport.manual_fields or [])
        if "name" not in manual:
            manual.append("name")
        airport.manual_fields = manual
        db.commit()
        flash(request, "saved")
    return _redirect(f"/airports/{airport.id}")


@router.post("/airports/{airport_id}/spots")
def spot_create(airport_id: int, request: Request, name: str = Form(...), lat: float = Form(...),
                lon: float = Form(...), notes: str = Form(""), user=Depends(get_admin), db: Session = Depends(get_db)):
    airport = db.get(models.Airport, airport_id)
    if not airport or not name.strip():
        raise HTTPException(400)
    db.add(models.Spot(airport_id=airport.id, name=name.strip(), lat=lat, lon=lon, notes=notes.strip() or None))
    db.commit()
    flash(request, "spot_created")
    return _redirect(f"/airports/{airport.id}")


@router.post("/spots/{spot_id}/edit")
def spot_edit(spot_id: int, request: Request, name: str = Form(...), lat: float = Form(...), lon: float = Form(...),
              notes: str = Form(""), user=Depends(get_admin), db: Session = Depends(get_db)):
    spot = db.get(models.Spot, spot_id)
    if not spot:
        raise HTTPException(404)
    spot.name, spot.lat, spot.lon, spot.notes = name.strip() or spot.name, lat, lon, notes.strip() or None
    db.commit()
    flash(request, "saved")
    return _redirect(f"/airports/{spot.airport_id}")


@router.post("/spots/{spot_id}/delete")
def spot_delete(spot_id: int, request: Request, user=Depends(get_admin), db: Session = Depends(get_db)):
    spot = db.get(models.Spot, spot_id)
    if not spot:
        raise HTTPException(404)
    airport_id = spot.airport_id
    db.delete(spot)
    db.commit()
    flash(request, "deleted")
    return _redirect(f"/airports/{airport_id}")


@router.get("/map")
def map_page(request: Request, user=Depends(get_user)):
    return render(request, "map.html")


# ---- export ----------------------------------------------------------------------------------------

EXPORT_COLUMNS = ["registration", "msn", "type_icao", "type_iata", "type_name", "model", "airline", "airline_iata",
                  "airline_icao", "christening_name", "active", "built", "delivery_date", "production_site",
                  "engines", "date", "airport", "airport_iata", "airport_icao", "spot", "livery", "photos", "notes"]


@router.get("/export.csv")
def export_csv(user=Depends(get_user), db: Session = Depends(get_db)):
    types = {t.icao: t for t in db.query(models.AircraftType).filter(
        models.AircraftType.icao.in_(db.query(models.Aircraft.type_icao).filter(
            models.Aircraft.id.in_(queries.spotted_ids))))}
    photo_counts = dict(db.query(models.Photo.sighting_id, func.count(models.Photo.id))
                        .group_by(models.Photo.sighting_id).all())
    buf = io.StringIO()
    buf.write("﻿")
    writer = csv.writer(buf)
    writer.writerow(EXPORT_COLUMNS)
    for s in queries.export_rows(db):
        ac = s.aircraft
        tp = types.get(ac.type_icao)
        writer.writerow([
            ac.registration, ac.msn, ac.type_icao, tp.iata if tp else "", tp.display_name if tp else "", ac.model,
            ac.airline.name if ac.airline else ac.operator_name, ac.airline.iata if ac.airline else "",
            ac.airline.icao if ac.airline else "", ac.christening_name, "yes" if ac.active else "no", ac.built,
            ac.delivery_date or "", ac.production_site, ac.engines, s.date.isoformat(),
            s.airport.name if s.airport else "", s.airport.iata if s.airport else "",
            s.airport.icao if s.airport else "", s.spot.name if s.spot else "", s.livery,
            photo_counts.get(s.id, 0), s.notes,
        ])
    data = buf.getvalue().encode("utf-8")
    name = f"aviationlog-{date.today().isoformat()}.csv"
    return StreamingResponse(iter([data]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---- JSON API for forms and the map ---------------------------------------------------------------

@router.get("/api/aircraft/lookup")
def api_aircraft_lookup(reg: str = "", user=Depends(get_user), db: Session = Depends(get_db)):
    reg = photos.normalize_registration(reg)
    ac = scan.find_aircraft(db, reg)
    if not ac:
        return {"found": False, "registration": reg}
    last = (
        db.query(models.Sighting)
        .filter(models.Sighting.aircraft_id == ac.id, models.Sighting.livery.isnot(None))
        .order_by(models.Sighting.date.desc())
        .first()
    )
    tp = ac.aircraft_type
    return {
        "found": True,
        "id": ac.id,
        "registration": ac.registration,
        "type_icao": ac.type_icao,
        "type_name": tp.display_name if tp else None,
        "model": ac.model,
        "airline": ac.airline.name if ac.airline else ac.operator_name,
        "msn": ac.msn,
        "christening_name": ac.christening_name,
        "active": ac.active,
        "last_livery": last.livery if last else None,
        "seen": len(ac.sightings),
    }


@router.get("/api/aircraft/suggest")
def api_aircraft_suggest(q: str = "", user=Depends(get_user), db: Session = Depends(get_db)):
    q = photos.normalize_registration(q).replace("-", "")
    if len(q) < 2:
        return []
    rows = (
        db.query(models.Aircraft.registration, models.Aircraft.type_icao, models.Aircraft.operator_name)
        .filter(func.replace(models.Aircraft.registration, "-", "").like(f"{q}%"))
        .order_by(models.Aircraft.registration)
        .limit(15)
        .all()
    )
    return [{"registration": r[0], "type_icao": r[1], "operator": r[2]} for r in rows]


@router.get("/api/airports")
def api_airports(q: str = "", user=Depends(get_user), db: Session = Depends(get_db)):
    return [
        {"id": a.id, "code": a.code, "iata": a.iata, "icao": a.icao, "name": a.name, "city": a.municipality}
        for a in _airport_search(db, q)
    ]


@router.get("/api/airports/{airport_id}/spots")
def api_airport_spots(airport_id: int, user=Depends(get_user), db: Session = Depends(get_db)):
    spots = db.query(models.Spot).filter_by(airport_id=airport_id).order_by(models.Spot.name).all()
    return [{"id": s.id, "name": s.name, "lat": s.lat, "lon": s.lon} for s in spots]


@router.get("/api/airport-by-code")
def api_airport_by_code(code: str = "", user=Depends(get_user), db: Session = Depends(get_db)):
    a = scan.find_airport(db, code) if code.strip() else None
    if not a:
        return {"found": False}
    return {"found": True, "id": a.id, "code": a.code, "name": a.name}


@router.get("/api/map")
def api_map(user=Depends(get_user), db: Session = Depends(get_db)):
    airports, spots, spot_counts = queries.map_points(db)
    return JSONResponse({
        "airports": [
            {"id": a.id, "name": a.name, "code": a.code, "lat": a.lat, "lon": a.lon, "n": n}
            for a, n in airports if a.lat is not None
        ],
        "spots": [
            {"id": s.id, "name": s.name, "airport": s.airport.code, "airport_id": s.airport_id,
             "lat": s.lat, "lon": s.lon, "n": spot_counts.get(s.id, 0)}
            for s in spots
        ],
    })


@router.get("/api/jobs/{job_id}")
def api_job(job_id: int, user=Depends(get_user), db: Session = Depends(get_db)):
    job = db.get(models.Job, job_id)
    if not job:
        raise HTTPException(404)
    return {"id": job.id, "kind": job.kind, "status": job.status, "progress": job.progress, "total": job.total,
            "message": job.message, "result": job.result}
