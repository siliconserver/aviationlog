"""Search and statistics."""
from sqlalchemy import and_, case, distinct, exists, extract, func, or_, select

import models

SEARCH_FIELDS = ["all", "registration", "msn", "type", "airline", "livery", "christening_name",
                 "airport", "spot", "camera", "lens"]

A = models.Aircraft
S = models.Sighting
P = models.Photo

spotted_ids = select(S.aircraft_id)


def _conditions(field: str, q: str):
    like = f"%{q}%"
    reg_like = f"%{q.replace('-', '').replace(' ', '')}%"
    conds = {
        "registration": func.replace(A.registration, "-", "").ilike(reg_like),
        "msn": A.msn.ilike(like),
        "type": or_(
            A.type_icao.ilike(like),
            A.model.ilike(like),
            exists().where(models.AircraftType.icao == A.type_icao,
                           or_(models.AircraftType.name.ilike(like), models.AircraftType.iata.ilike(q),
                               models.AircraftType.model_full.ilike(like))),
        ),
        "airline": or_(
            A.operator_name.ilike(like),
            A.operator_icao.ilike(q),
            A.operator_iata.ilike(q),
            exists().where(models.Airline.id == A.airline_id,
                           or_(models.Airline.name.ilike(like), models.Airline.iata.ilike(q),
                               models.Airline.icao.ilike(q))),
        ),
        "livery": exists().where(S.aircraft_id == A.id, S.livery.ilike(like)),
        "christening_name": A.christening_name.ilike(like),
        "airport": exists().where(
            S.aircraft_id == A.id,
            exists().where(models.Airport.id == S.airport_id,
                           or_(models.Airport.name.ilike(like), models.Airport.iata.ilike(q),
                               models.Airport.icao.ilike(q), models.Airport.municipality.ilike(like))),
        ),
        "spot": exists().where(S.aircraft_id == A.id,
                               exists().where(models.Spot.id == S.spot_id, models.Spot.name.ilike(like))),
        "camera": exists().where(S.aircraft_id == A.id, P.sighting_id == S.id,
                                 or_(P.camera_model.ilike(like), P.camera_make.ilike(like))),
        "lens": exists().where(S.aircraft_id == A.id, P.sighting_id == S.id, P.lens.ilike(like)),
    }
    if field == "all":
        return or_(*conds.values())
    return conds.get(field, conds["registration"])


SORT_COLUMNS = ["registration", "type", "airline", "msn", "seen", "last_seen", "first_seen"]


def search_aircraft(db, q: str = "", field: str = "all", scope: str = "collection",
                    sort: str = "", direction: str = "asc", page: int = 1, per_page: int = 50,
                    airline_id: int | None = None, type_icao: str | None = None):
    """Search aircraft. Without ``sort`` the default order applies (last seen first)."""
    sightings = (
        select(S.aircraft_id, func.count(S.id).label("seen"), func.max(S.date).label("last_seen"),
               func.min(S.date).label("first_seen"))
        .group_by(S.aircraft_id)
        .subquery()
    )
    query = db.query(A, sightings.c.seen, sightings.c.last_seen, sightings.c.first_seen)
    if scope == "all":
        query = query.outerjoin(sightings, sightings.c.aircraft_id == A.id)
    else:
        query = query.join(sightings, sightings.c.aircraft_id == A.id)
    q = (q or "").strip()
    if q:
        query = query.filter(_conditions(field, q))
    if airline_id:
        query = query.filter(A.airline_id == airline_id)
    if type_icao:
        query = query.filter(A.type_icao == type_icao)
    total = query.count()
    if sort in SORT_COLUMNS:
        if sort == "airline":
            query = query.outerjoin(models.Airline, models.Airline.id == A.airline_id)
        column = {
            "registration": A.registration,
            "type": A.type_icao,
            "airline": func.lower(func.coalesce(models.Airline.name, A.operator_name)),
            "msn": A.msn,
            "seen": func.coalesce(sightings.c.seen, 0),
            "last_seen": sightings.c.last_seen,
            "first_seen": sightings.c.first_seen,
        }[sort]
        # empty values always at the end, whatever the direction
        column = column.desc().nullslast() if direction == "desc" else column.asc().nullslast()
        order = [column, A.registration]
    else:
        order = [sightings.c.last_seen.desc().nullslast(), A.registration]
    rows = query.order_by(*order).offset((page - 1) * per_page).limit(per_page).all()
    return rows, total


def totals(db) -> dict:
    return {
        "aircraft": db.query(func.count(distinct(S.aircraft_id))).scalar() or 0,
        "sightings": db.query(func.count(S.id)).scalar() or 0,
        "photos": db.query(func.count(P.id)).scalar() or 0,
        "airports": db.query(func.count(distinct(S.airport_id))).scalar() or 0,
        "airlines": db.query(func.count(distinct(A.airline_id))).filter(A.id.in_(spotted_ids)).scalar() or 0,
        "types": db.query(func.count(distinct(A.type_icao))).filter(A.id.in_(spotted_ids)).scalar() or 0,
    }


def by_airline(db, limit: int = 15):
    name = func.coalesce(models.Airline.name, A.operator_name, "?")
    return (
        db.query(name.label("label"), models.Airline.id, func.count(distinct(A.id)).label("n"))
        .select_from(A)
        .outerjoin(models.Airline, models.Airline.id == A.airline_id)
        .filter(A.id.in_(spotted_ids))
        .group_by(name, models.Airline.id)
        .order_by(func.count(distinct(A.id)).desc())
        .limit(limit)
        .all()
    )


def by_type(db, limit: int = 15):
    return (
        db.query(func.coalesce(A.type_icao, "?").label("label"), func.count(distinct(A.id)).label("n"))
        .filter(A.id.in_(spotted_ids))
        .group_by(A.type_icao)
        .order_by(func.count(distinct(A.id)).desc())
        .limit(limit)
        .all()
    )


def by_airport(db, limit: int = 15):
    return (
        db.query(models.Airport.iata, models.Airport.icao, models.Airport.name, models.Airport.id,
                 func.count(S.id).label("n"))
        .join(S, S.airport_id == models.Airport.id)
        .group_by(models.Airport.id)
        .order_by(func.count(S.id).desc())
        .limit(limit)
        .all()
    )


def by_year(db):
    year = extract("year", S.date)
    first = (
        select(S.aircraft_id, func.min(S.date).label("first")).group_by(S.aircraft_id).subquery()
    )
    sightings = dict(db.query(year, func.count(S.id)).group_by(year).all())
    new = dict(db.query(extract("year", first.c.first), func.count()).group_by(extract("year", first.c.first)).all())
    years = sorted(set(sightings) | set(new))
    return [{"year": int(y), "sightings": sightings.get(y, 0), "new": new.get(y, 0)} for y in years]


def new_per_month(db, months: int = 24):
    first = select(S.aircraft_id, func.min(S.date).label("first")).group_by(S.aircraft_id).subquery()
    month = func.to_char(first.c.first, "YYYY-MM")
    rows = db.query(month, func.count()).group_by(month).order_by(month.desc()).limit(months).all()
    return list(reversed(rows))


def by_camera(db, limit: int = 10):
    return (
        db.query(P.camera_make, P.camera_model, func.count(P.id).label("n"))
        .filter(P.camera_model.isnot(None))
        .group_by(P.camera_make, P.camera_model)
        .order_by(func.count(P.id).desc())
        .limit(limit)
        .all()
    )


def by_lens(db, limit: int = 10):
    return (
        db.query(P.lens, func.count(P.id).label("n"))
        .filter(P.lens.isnot(None))
        .group_by(P.lens)
        .order_by(func.count(P.id).desc())
        .limit(limit)
        .all()
    )


def fleet_completion(db, limit: int | None = None):
    """Per airline: spotted active aircraft (X) of all active aircraft (Y)."""
    spotted = case((A.id.in_(spotted_ids), 1), else_=0)
    q = (
        db.query(
            models.Airline.id,
            models.Airline.name,
            models.Airline.iata,
            models.Airline.icao,
            func.sum(case((and_(A.active.is_(True), A.id.in_(spotted_ids)), 1), else_=0)).label("x"),
            func.sum(case((A.active.is_(True), 1), else_=0)).label("y"),
            func.sum(spotted).label("spotted_total"),
        )
        .join(A, and_(A.airline_id == models.Airline.id, A.reg_unknown.is_(False)))
        .group_by(models.Airline.id)
        .having(func.sum(spotted) > 0)
        .order_by(func.sum(spotted).desc(), models.Airline.name)
    )
    if limit:
        q = q.limit(limit)
    return q.all()


def fleet(db, airline_id: int, show: str = "all"):
    seen = (
        select(S.aircraft_id, func.count(S.id).label("seen"), func.max(S.date).label("last_seen"))
        .group_by(S.aircraft_id)
        .subquery()
    )
    q = (
        db.query(A, seen.c.seen, seen.c.last_seen)
        .outerjoin(seen, seen.c.aircraft_id == A.id)
        .filter(A.airline_id == airline_id)
    )
    if show == "missing":
        q = q.filter(seen.c.seen.is_(None), A.active.is_(True))
    elif show == "spotted":
        q = q.filter(seen.c.seen.isnot(None))
    elif show == "inactive":
        q = q.filter(A.active.is_(False))
    return q.order_by(A.active.desc(), A.type_icao, A.registration).all()


def map_points(db):
    airports = (
        db.query(models.Airport, func.count(S.id).label("n"))
        .join(S, S.airport_id == models.Airport.id)
        .filter(S.spot_id.is_(None))
        .group_by(models.Airport.id)
        .all()
    )
    spot_counts = dict(
        db.query(S.spot_id, func.count(S.id)).filter(S.spot_id.isnot(None)).group_by(S.spot_id).all()
    )
    spots = db.query(models.Spot).all()
    return airports, spots, spot_counts


def export_rows(db):
    """One row per sighting for the CSV export."""
    return (
        db.query(S)
        .join(A, A.id == S.aircraft_id)
        .order_by(S.date, A.registration)
        .all()
    )
