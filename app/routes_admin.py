"""Admin pages: master data import, photo archive scan, users, backups."""
import math

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

import backup
import emailer
import importers
import flightera
import jobs
import lookups
import models
import photos
import scan
import security
import settings_store
from database import get_db
from web import flash, get_admin, render

router = APIRouter(prefix="/admin")

IMPORT_JOBS = ["import_prepare", "import_apply"]
SCAN_JOBS = ["scan", "scan_apply", "thumbnails", "remove_missing"]


def _redirect(url: str):
    return RedirectResponse(url, status_code=303)


# ---- master data import ------------------------------------------------------------------

@router.get("/data")
def data_page(request: Request, user=Depends(get_admin), db: Session = Depends(get_db)):
    batches = {}
    for b in db.query(models.ImportBatch).order_by(models.ImportBatch.id.desc()).limit(50):
        batches.setdefault(b.source, b)
    counts = {
        "aircraft": db.query(func.count(models.Aircraft.id)).scalar(),
        "airport": db.query(func.count(models.Airport.id)).scalar(),
        "airline": db.query(func.count(models.Airline.id)).scalar(),
        "type": db.query(func.count(models.AircraftType.id)).scalar(),
    }
    return render(request, "admin_data.html", {
        "sources": importers.SOURCES, "batches": batches, "counts": counts,
        "job": jobs.running(db, IMPORT_JOBS),
        "last_job": db.query(models.Job).filter(models.Job.kind.in_(IMPORT_JOBS))
        .order_by(models.Job.id.desc()).first(),
        "lookups": {
            key: {"enabled": lookups.provider(key).enabled(), "auto": lookups.provider(key).auto_enabled(db)}
            for key in ("adsbdb", "airportdata")
        },
        "lookup_count": lookups.collection_query(db).count(),
        "flightera_enabled": flightera.enabled(),
        "flightera_key_set": bool(flightera.api_key(db)),
        "flightera_balance": flightera.balance(db),
    })


@router.post("/data/{source}/auto")
def data_lookup_auto(source: str, request: Request, auto: str = Form(""), user=Depends(get_admin),
                     db: Session = Depends(get_db)):
    if source not in ("adsbdb", "airportdata"):
        raise HTTPException(404)
    settings_store.set(db, lookups.provider(source).SETTING_AUTO, "1" if auto == "on" else "0")
    flash(request, "saved")
    return _redirect("/admin/data")


@router.post("/data/flightera/key")
def data_flightera_key(request: Request, key: str = Form(""), remove: str = Form(""),
                       user=Depends(get_admin), db: Session = Depends(get_db)):
    key = "" if remove else key.strip()
    if key or remove:
        settings_store.set(db, flightera.SETTING_KEY, key)
        if remove:
            settings_store.set(db, flightera.SETTING_BALANCE, "")
            settings_store.set(db, flightera.SETTING_BALANCE_AT, "")
        flash(request, "saved")
    return _redirect("/admin/data")


@router.post("/data/{source}/download")
def data_download(source: str, request: Request, user=Depends(get_admin), db: Session = Depends(get_db)):
    if source not in importers.SOURCES:
        raise HTTPException(404)
    lookup = importers.SOURCES[source].get("lookup")
    if lookup and not lookups.provider(lookup).enabled():
        raise HTTPException(404)
    if jobs.running(db, IMPORT_JOBS):
        flash(request, "job_running")
        return _redirect("/admin/data")
    jobs.submit(db, "import_prepare", {"source": source})
    flash(request, "import_started")
    return _redirect("/admin/data")


@router.post("/data/{source}/upload")
def data_upload(source: str, request: Request, file: UploadFile = File(...), user=Depends(get_admin),
                db: Session = Depends(get_db)):
    if source not in importers.SOURCES or importers.SOURCES[source].get("lookup"):
        raise HTTPException(404)
    if jobs.running(db, IMPORT_JOBS):
        flash(request, "job_running")
        return _redirect("/admin/data")
    path = importers.save_upload(source, file.file)
    jobs.submit(db, "import_prepare", {"source": source, "file": str(path)})
    flash(request, "import_started")
    return _redirect("/admin/data")


def _batch_or_404(db, batch_id: int) -> models.ImportBatch:
    batch = db.get(models.ImportBatch, batch_id)
    if not batch:
        raise HTTPException(404)
    return batch


@router.get("/data/batch/{batch_id}")
def batch_review(batch_id: int, request: Request, tab: str = "collection", q: str = "", page: int = 1,
                 user=Depends(get_admin), db: Session = Depends(get_db)):
    batch = _batch_or_404(db, batch_id)
    entity = importers.SOURCES[batch.source]["entity"]
    query = db.query(models.ImportRow).filter(models.ImportRow.batch_id == batch.id)
    if tab == "collection":
        query = query.filter(models.ImportRow.in_collection.is_(True))
    elif tab == "new":
        query = query.filter(models.ImportRow.kind == "new")
    elif tab == "changed":
        query = query.filter(models.ImportRow.kind == "changed")
    elif tab == "rejected":
        query = query.filter(models.ImportRow.accepted.is_(False))
    if q.strip():
        query = query.filter(or_(models.ImportRow.key.ilike(f"%{q.strip()}%"),
                                 models.ImportRow.data.cast(models.Text).ilike(f"%{q.strip()}%")))
    total = query.count()
    page = max(page, 1)
    rows = query.order_by(models.ImportRow.key).offset((page - 1) * 50).limit(50).all()
    rejected = db.query(func.count(models.ImportRow.id)).filter(
        models.ImportRow.batch_id == batch.id, models.ImportRow.accepted.is_(False)).scalar()
    return render(request, "admin_batch.html", {
        "batch": batch, "entity": entity, "fields": importers.ENTITIES[entity][2], "rows": rows, "tab": tab,
        "q": q, "page": page, "pages": max(1, math.ceil(total / 50)), "total": total, "rejected": rejected,
        "job": jobs.running(db, IMPORT_JOBS),
    })


@router.post("/data/batch/{batch_id}/row/{row_id}")
async def batch_row(batch_id: int, row_id: int, request: Request, user=Depends(get_admin),
                    db: Session = Depends(get_db)):
    batch = _batch_or_404(db, batch_id)
    row = db.get(models.ImportRow, row_id)
    if not row or row.batch_id != batch.id or batch.status != "ready":
        raise HTTPException(404)
    form = await request.form()
    entity = importers.SOURCES[batch.source]["entity"]
    edited = {}
    for field in importers.ENTITIES[entity][2]:
        if f"f_{field}" in form:
            value = (form.get(f"f_{field}") or "").strip()
            original = row.data.get(field)
            if str(original if original is not None else "") != value:
                edited[field] = value or None
    row.edited = edited
    row.accepted = form.get("accepted") == "on"
    db.commit()
    flash(request, "saved")
    return _redirect(form.get("back") or f"/admin/data/batch/{batch.id}")


@router.post("/data/batch/{batch_id}/bulk")
def batch_bulk(batch_id: int, request: Request, action: str = Form(...), tab: str = Form("all"),
               user=Depends(get_admin), db: Session = Depends(get_db)):
    batch = _batch_or_404(db, batch_id)
    query = db.query(models.ImportRow).filter(models.ImportRow.batch_id == batch.id)
    if tab == "collection":
        query = query.filter(models.ImportRow.in_collection.is_(True))
    elif tab in ("new", "changed"):
        query = query.filter(models.ImportRow.kind == tab)
    query.update({models.ImportRow.accepted: action == "accept"}, synchronize_session=False)
    db.commit()
    return _redirect(f"/admin/data/batch/{batch.id}?tab={tab}")


@router.post("/data/batch/{batch_id}/apply")
def batch_apply(batch_id: int, request: Request, user=Depends(get_admin), db: Session = Depends(get_db)):
    batch = _batch_or_404(db, batch_id)
    if batch.status != "ready" or jobs.running(db, IMPORT_JOBS):
        flash(request, "job_running")
        return _redirect(f"/admin/data/batch/{batch.id}")
    jobs.submit(db, "import_apply", {"batch_id": batch.id})
    flash(request, "import_applying")
    return _redirect("/admin/data")


@router.post("/data/batch/{batch_id}/discard")
def batch_discard(batch_id: int, request: Request, user=Depends(get_admin), db: Session = Depends(get_db)):
    batch = _batch_or_404(db, batch_id)
    if batch.status == "ready":
        batch.status = "discarded"
        db.query(models.ImportRow).filter_by(batch_id=batch.id).delete()
        db.commit()
        flash(request, "import_discarded")
    return _redirect("/admin/data")


# ---- photo archive -------------------------------------------------------------------------

def _latest_scan(db):
    return db.query(models.Job).filter(models.Job.kind == "scan").order_by(models.Job.id.desc()).first()


@router.get("/photos")
def photos_page(request: Request, show: str = "open", page: int = 1, user=Depends(get_admin),
                db: Session = Depends(get_db)):
    scan_job = _latest_scan(db)
    entries, total = [], 0
    counts = {"open": 0, "problems": 0, "applied": 0}
    if scan_job:
        base = db.query(models.ScanEntry).filter(models.ScanEntry.job_id == scan_job.id)
        counts["open"] = base.filter(models.ScanEntry.applied.is_(False)).count()
        counts["problems"] = base.filter(models.ScanEntry.applied.is_(False),
                                         func.jsonb_array_length(models.ScanEntry.problems) > 0).count()
        counts["applied"] = base.filter(models.ScanEntry.applied.is_(True)).count()
        q = base
        if show == "problems":
            q = q.filter(models.ScanEntry.applied.is_(False), func.jsonb_array_length(models.ScanEntry.problems) > 0)
        elif show == "applied":
            q = q.filter(models.ScanEntry.applied.is_(True))
        else:
            q = q.filter(models.ScanEntry.applied.is_(False))
        total = q.count()
        page = max(page, 1)
        entries = q.order_by(models.ScanEntry.folder).offset((page - 1) * 50).limit(50).all()
    return render(request, "admin_photos.html", {
        "scan_job": scan_job, "entries": entries, "counts": counts, "show": show, "page": page,
        "pages": max(1, math.ceil(total / 50)), "job": jobs.running(db, SCAN_JOBS),
        "last_job": db.query(models.Job).filter(models.Job.kind.in_(SCAN_JOBS)).order_by(models.Job.id.desc()).first(),
        "photo_dir": str(photos.photo_dir()), "photo_count": db.query(func.count(models.Photo.id)).scalar(),
        "has_airports": db.query(models.Airport.id).first() is not None,
    })


@router.post("/photos/scan")
def photos_scan(request: Request, user=Depends(get_admin), db: Session = Depends(get_db)):
    if photos.photo_mode() != "nas":
        raise HTTPException(400, "scan is only available in NAS mode")
    if jobs.running(db, SCAN_JOBS):
        flash(request, "job_running")
    else:
        jobs.submit(db, "scan")
        flash(request, "scan_started")
    return _redirect("/admin/photos")


@router.post("/photos/entry/{entry_id}")
async def photos_entry(entry_id: int, request: Request, user=Depends(get_admin), db: Session = Depends(get_db)):
    entry = db.get(models.ScanEntry, entry_id)
    if not entry or entry.applied:
        raise HTTPException(404)
    form = await request.form()
    values = {}
    for field in scan.AIRCRAFT_FIELDS + scan.DATE_FIELDS:
        if field in ("reg_unknown", "aircraft_id"):
            continue
        if field in form:
            new = (form.get(field) or "").strip() or None
            if new != entry.parsed.get(field):
                values[field] = new
    reg_unknown = form.get("reg_unknown") == "on"
    if reg_unknown != bool(entry.parsed.get("reg_unknown")):
        values["reg_unknown"] = reg_unknown
    if values:
        scan.save_override(db, entry.folder, values)
    parsed, problems, sighting_id = scan.analyze(db, entry.folder)
    entry.parsed, entry.problems, entry.sighting_id = parsed, problems, sighting_id
    entry.accepted = form.get("accepted") == "on" and not problems
    db.commit()
    return _redirect(form.get("back") or "/admin/photos")


@router.post("/photos/entry/{entry_id}/delete")
async def photos_entry_delete(entry_id: int, request: Request, user=Depends(get_admin), db: Session = Depends(get_db)):
    """Removes a review entry only; the folder on the NAS is untouched and shows up again with the next scan."""
    entry = db.get(models.ScanEntry, entry_id)
    if not entry or entry.applied:
        raise HTTPException(404)
    form = await request.form()
    db.delete(entry)
    db.commit()
    return _redirect(form.get("back") or "/admin/photos")


@router.post("/photos/apply")
def photos_apply(request: Request, user=Depends(get_admin), db: Session = Depends(get_db)):
    scan_job = _latest_scan(db)
    if not scan_job or jobs.running(db, SCAN_JOBS):
        flash(request, "job_running")
    else:
        jobs.submit(db, "scan_apply", {"scan_job_id": scan_job.id})
        flash(request, "scan_applying")
    return _redirect("/admin/photos")


@router.post("/photos/thumbnails")
def photos_thumbnails(request: Request, force: str = Form(""), user=Depends(get_admin),
                      db: Session = Depends(get_db)):
    if jobs.running(db, SCAN_JOBS):
        flash(request, "job_running")
    else:
        jobs.submit(db, "thumbnails", {"force": force == "1"})
        flash(request, "thumbnails_started")
    return _redirect("/admin/photos")


@router.post("/photos/remove-missing")
def photos_remove_missing(request: Request, user=Depends(get_admin), db: Session = Depends(get_db)):
    if jobs.running(db, SCAN_JOBS):
        flash(request, "job_running")
    else:
        jobs.submit(db, "remove_missing")
        flash(request, "remove_missing_started")
    return _redirect("/admin/photos")


# ---- users ------------------------------------------------------------------------------------

@router.get("/users")
def users_page(request: Request, user=Depends(get_admin), db: Session = Depends(get_db)):
    return render(request, "admin_users.html", {
        "users": db.query(models.User).order_by(models.User.username).all(),
        "smtp": emailer.is_configured(),
    })


@router.post("/users/new")
def user_create(request: Request, username: str = Form(...), password: str = Form(...), email: str = Form(""),
                role: str = Form("viewer"), user=Depends(get_admin), db: Session = Depends(get_db)):
    username = username.strip()
    if not username or len(password) < 8:
        flash(request, "password_invalid")
        return _redirect("/admin/users")
    if db.query(models.User).filter(func.lower(models.User.username) == username.lower()).first():
        flash(request, "user_exists")
        return _redirect("/admin/users")
    db.add(models.User(username=username, email=email.strip() or None,
                       password_hash=security.hash_password(password),
                       role=models.Role.admin if role == "admin" else models.Role.viewer, recovery_codes=[]))
    db.commit()
    flash(request, "user_created")
    return _redirect("/admin/users")


def _other_user(db, user_id: int, me) -> models.User:
    target = db.get(models.User, user_id)
    if not target:
        raise HTTPException(404)
    return target


def _admin_count(db) -> int:
    return db.query(models.User).filter(models.User.role == models.Role.admin).count()


@router.post("/users/{user_id}/role")
def user_role(user_id: int, request: Request, role: str = Form(...), user=Depends(get_admin),
              db: Session = Depends(get_db)):
    target = _other_user(db, user_id, user)
    if target.id == user.id:
        flash(request, "user_self")
    elif target.role == models.Role.admin and role != "admin" and _admin_count(db) <= 1:
        flash(request, "user_last_admin")
    else:
        target.role = models.Role.admin if role == "admin" else models.Role.viewer
        db.commit()
        flash(request, "saved")
    return _redirect("/admin/users")


@router.post("/users/{user_id}/password")
def user_password(user_id: int, request: Request, password: str = Form(...), user=Depends(get_admin),
                  db: Session = Depends(get_db)):
    target = _other_user(db, user_id, user)
    if len(password) < 8:
        flash(request, "password_invalid")
    else:
        target.password_hash = security.hash_password(password)
        db.commit()
        flash(request, "password_updated")
    return _redirect("/admin/users")


@router.post("/users/{user_id}/2fa/reset")
def user_reset_2fa(user_id: int, request: Request, user=Depends(get_admin), db: Session = Depends(get_db)):
    target = _other_user(db, user_id, user)
    target.totp_enabled = False
    target.totp_secret = None
    target.recovery_codes = []
    db.commit()
    flash(request, "totp_disabled")
    return _redirect("/admin/users")


@router.post("/users/{user_id}/delete")
def user_delete(user_id: int, request: Request, user=Depends(get_admin), db: Session = Depends(get_db)):
    target = _other_user(db, user_id, user)
    if target.id == user.id:
        flash(request, "user_self")
    elif target.role == models.Role.admin and _admin_count(db) <= 1:
        flash(request, "user_last_admin")
    else:
        db.delete(target)
        db.commit()
        flash(request, "deleted")
    return _redirect("/admin/users")


# ---- backups -----------------------------------------------------------------------------------

@router.get("/backups")
def backups_page(request: Request, user=Depends(get_admin), db: Session = Depends(get_db)):
    return render(request, "admin_backups.html", {
        "backups": backup.list_backups(),
        "backup_time": settings_store.get(db, "backup_time"),
        "backup_retention": settings_store.get(db, "backup_retention"),
    })


@router.post("/backups/schedule")
def backups_schedule(request: Request, backup_time: str = Form(...), backup_retention: int = Form(...),
                     user=Depends(get_admin), db: Session = Depends(get_db)):
    try:
        h, m = (int(x) for x in backup_time.split(":"))
        assert 0 <= h < 24 and 0 <= m < 60
    except (ValueError, AssertionError):
        flash(request, "invalid_time")
        return _redirect("/admin/backups")
    settings_store.set(db, "backup_time", f"{h:02d}:{m:02d}")
    settings_store.set(db, "backup_retention", str(max(1, min(backup_retention, 365))))
    sched = getattr(request.app.state, "scheduler", None)
    if sched:
        import scheduler

        scheduler.reschedule(sched)
    flash(request, "saved")
    return _redirect("/admin/backups")


@router.post("/backups/run")
def backups_run(request: Request, user=Depends(get_admin), db: Session = Depends(get_db)):
    try:
        backup.run_backup(retention=int(settings_store.get(db, "backup_retention")))
        flash(request, "backup_ok")
    except Exception:
        flash(request, "backup_error")
    return _redirect("/admin/backups")


@router.get("/backups/{filename}/download")
def backups_download(filename: str, user=Depends(get_admin)):
    try:
        path = backup.backup_path(filename)
    except FileNotFoundError:
        raise HTTPException(404)
    return FileResponse(path, filename=path.name, media_type="application/gzip")


@router.post("/backups/{filename}/restore")
def backups_restore(filename: str, request: Request, confirm: str = Form(""), user=Depends(get_admin)):
    if confirm != "RESTORE":
        flash(request, "restore_not_confirmed")
        return _redirect("/admin/backups")
    try:
        backup.restore_backup(filename)
        flash(request, "restore_ok")
    except Exception:
        flash(request, "restore_error")
    return _redirect("/admin/backups")


@router.post("/backups/upload")
def backups_upload(request: Request, file: UploadFile = File(...), user=Depends(get_admin)):
    backup.save_uploaded(file.file, file.filename)
    flash(request, "backup_uploaded")
    return _redirect("/admin/backups")


@router.post("/backups/{filename}/delete")
def backups_delete(filename: str, request: Request, user=Depends(get_admin)):
    backup.delete_backup(filename)
    flash(request, "deleted")
    return _redirect("/admin/backups")
