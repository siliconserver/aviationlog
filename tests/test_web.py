import re
from datetime import date

import fixtures_photos
import models
from test_import import import_all


def test_login_required(db):
    from fastapi.testclient import TestClient

    import main

    with TestClient(main.app) as c:
        r = c.get("/aircraft", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
        r = c.post("/login", data={"username": "admin", "password": "wrong"})
        assert r.status_code == 401


def test_pages_render(client, db):
    import_all(db)
    for url in ["/", "/aircraft", "/aircraft?scope=all&q=lufthansa", "/airlines", "/airports", "/airports?q=MUC",
                "/map", "/api/map", "/profile", "/about", "/admin/data", "/admin/photos", "/admin/users",
                "/admin/backups", "/export.csv", "/sightings/new", "/lang/de", "/aircraft"]:
        r = client.get(url)
        assert r.status_code == 200, url
    assert "Flugzeuge" in client.get("/aircraft").text  # switched to German


def test_map_tiles_get_referer(client, db):
    # tile.openstreetmap.org answers 403 without a Referer: the page must not forbid it
    r = client.get("/map")
    assert r.status_code == 200
    assert r.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert 'referrerPolicy: "strict-origin-when-cross-origin"' in r.text


def test_opensky_notice(client, db):
    for url in ["/about", "/admin/data"]:
        r = client.get(url)
        assert r.status_code == 200
        assert "Bringing Up OpenSky" in r.text and 'class="info-tip-btn"' in r.text
        assert "free for non-commercial use" not in r.text


def test_upload_sighting_with_photo(client, db, tmp_path, monkeypatch):
    import_all(db)
    monkeypatch.setenv("PHOTO_MODE", "upload")
    monkeypatch.setenv("PHOTO_DIR", str(tmp_path / "photos"))
    jpg = tmp_path / "DSC1.jpg"
    fixtures_photos.jpeg_with_exif(str(jpg))
    airport = db.query(models.Airport).filter_by(iata="FRA").one()
    db.add(models.Spot(airport_id=airport.id, name="Terrace", lat=50.0406, lon=8.5861))
    db.commit()
    with open(jpg, "rb") as fh:
        r = client.post("/sightings/new", data={"registration": "daijc", "date": "", "airport": "fra"},
                        files=[("photos", ("DSC1.jpg", fh, "image/jpeg"))])
    assert r.status_code == 200
    db.expire_all()
    ac = db.query(models.Aircraft).filter_by(registration="D-AIJC").one()
    s = ac.sightings[0]
    assert s.date == date(2026, 6, 5)  # from EXIF
    assert s.spot.name == "Terrace"  # from GPS
    photo = s.photos[0]
    assert photo.lens.startswith("FE 100-400")
    assert (tmp_path / "photos" / photo.path).exists()
    assert photo.path.startswith("Lufthansa/A20N/D-AIJC Lufthansa A20N/2026-06-05-FRA/")
    assert ac.cover_photo_id == photo.id
    assert client.get(f"/photos/{photo.id}/thumb/s").headers["content-type"] == "image/jpeg"
    assert "ILCE-7RM5" in client.get(f"/photos/{photo.id}").text
    assert "D-AIJC" in client.get("/aircraft?field=lens&q=100-400").text
    csv = client.get("/export.csv").text
    assert "D-AIJC" in csv and "Terrace" in csv
    fleet = client.get(f"/airlines/{ac.airline_id}").text
    assert "1 / 4" in fleet


def test_nas_scan(client, db, tmp_path, monkeypatch):
    import jobs
    import_all(db)
    root = tmp_path / "nas"
    monkeypatch.setenv("PHOTO_MODE", "nas")
    monkeypatch.setenv("PHOTO_DIR", str(root))
    for folder in ["Lufthansa/A20N/D-AIJC Lufthansa A20N Lünen/2026-06-05-FRA",
                   "SunExpress/B738/TC-SOZ SunExpress (Istanbul Livery) B738/2026-06-15-CGN",
                   "German Airways/E190/D-AJHW German Airways E190/2026-08-10-XXX"]:
        d = root / folder
        d.mkdir(parents=True)
        fixtures_photos.jpeg_with_exif(str(d / "a.jpg"), gps=None)
        fixtures_photos.fake_arw(str(d / "a.arw"))
    # quick sighting from the phone, to be matched by the scan
    client.post("/sightings/new", data={"registration": "TC-SOZ", "date": "2026-06-15", "airport": "CGN"})

    client.post("/admin/photos/scan")
    jobs.wait_idle()
    db.expire_all()
    entries = {e.folder.split("/")[2].split()[0]: e for e in db.query(models.ScanEntry)}
    assert entries["D-AJHW"].problems == ["airport"] and not entries["D-AJHW"].accepted
    assert entries["TC-SOZ"].sighting_id is not None

    # correct the airport of the German Airways folder
    r = client.post(f"/admin/photos/entry/{entries['D-AJHW'].id}",
                    data={"airport": "FRA", "registration": "D-AJHW", "accepted": "on"})
    assert r.status_code == 200
    client.post("/admin/photos/apply")
    jobs.wait_idle()
    db.expire_all()
    soz = db.query(models.Aircraft).filter_by(registration="TC-SOZ").one()
    assert len(soz.sightings) == 1 and len(soz.sightings[0].photos) == 2
    assert soz.sightings[0].livery == "Istanbul Livery"
    ijc = db.query(models.Aircraft).filter_by(registration="D-AIJC").one()
    assert ijc.christening_name == "Lünen"
    ajhw = db.query(models.Aircraft).filter_by(registration="D-AJHW").one()
    assert ajhw.airline.name == "German Airways" and ajhw.sightings[0].airport.iata == "FRA"
    assert db.query(models.FolderOverride).count() == 1

    # second scan finds nothing new; files on the NAS were not touched
    client.post("/admin/photos/scan")
    jobs.wait_idle()
    db.expire_all()
    latest = db.query(models.Job).filter_by(kind="scan").order_by(models.Job.id.desc()).first()
    assert latest.result["folders"] == 0
    assert sorted(p.name for p in (root / "Lufthansa/A20N").rglob("*")) == sorted(
        ["D-AIJC Lufthansa A20N Lünen", "2026-06-05-FRA", "a.jpg", "a.arw"])
    # uploads are refused in NAS mode
    r = client.post("/sightings/new", data={"registration": "D-AIMH", "airport": "FRA"},
                    files=[("photos", ("x.jpg", b"123", "image/jpeg"))])
    assert r.status_code == 400


def test_nas_scan_military_and_corrected_registration(client, db, tmp_path, monkeypatch):
    import jobs
    import_all(db)
    root = tmp_path / "nas"
    monkeypatch.setenv("PHOTO_MODE", "nas")
    monkeypatch.setenv("PHOTO_DIR", str(root))
    for folder in ["Luftwaffe/A21N/15+11 Luftwaffe A21N/2026-06-15-CGN",
                   "Emirates/B77W/Emirates B77W/2026-10-31-FRA"]:
        d = root / folder
        d.mkdir(parents=True)
        fixtures_photos.jpeg_with_exif(str(d / "a.jpg"), gps=None)
    client.post("/admin/photos/scan")
    jobs.wait_idle()
    db.expire_all()
    entries = {e.folder.split("/")[0]: e for e in db.query(models.ScanEntry)}
    # German military serial is accepted as it is
    assert "registration" not in entries["Luftwaffe"].problems
    # a folder without registration is flagged, saving it unchanged does not help ...
    assert "registration" in entries["Emirates"].problems
    client.post(f"/admin/photos/entry/{entries['Emirates'].id}", data={"registration": "EMIRATES", "accepted": "on"})
    db.expire_all()
    assert "registration" in db.get(models.ScanEntry, entries["Emirates"].id).problems
    # ... but any registration or serial entered by hand is trusted
    client.post(f"/admin/photos/entry/{entries['Emirates'].id}", data={"registration": "MM62243", "accepted": "on"})
    db.expire_all()
    assert db.get(models.ScanEntry, entries["Emirates"].id).problems == []
    for e in entries.values():
        client.post(f"/admin/photos/entry/{e.id}", data={"accepted": "on"})
    client.post("/admin/photos/apply")
    jobs.wait_idle()
    db.expire_all()
    assert {a.registration for a in db.query(models.Aircraft).filter(
        models.Aircraft.registration.in_(["15+11", "MM62243"]))} == {"15+11", "MM62243"}


def test_registration_unknown(client, db, tmp_path, monkeypatch):
    import jobs
    import queries
    import_all(db)
    root = tmp_path / "nas"
    monkeypatch.setenv("PHOTO_MODE", "nas")
    monkeypatch.setenv("PHOTO_DIR", str(root))
    for folder in ["Lufthansa/B77W/Lufthansa B77W/2026-10-31-FRA",
                   "Lufthansa/B77W/Lufthansa B77W/2026-11-01-MUC"]:
        d = root / folder
        d.mkdir(parents=True)
        fixtures_photos.jpeg_with_exif(str(d / "a.jpg"), gps=None)
    lh = db.query(models.Airline).filter_by(icao="DLH").one()
    before = {r.id: (r.x, r.y) for r in queries.fleet_completion(db)}

    client.post("/admin/photos/scan")
    jobs.wait_idle()
    db.expire_all()
    entries = db.query(models.ScanEntry).order_by(models.ScanEntry.folder).all()
    assert all("registration" in e.problems for e in entries)
    # ticking "registration unknown" on one folder covers the whole aircraft folder
    client.post(f"/admin/photos/entry/{entries[0].id}", data={"reg_unknown": "on", "accepted": "on"})
    client.post(f"/admin/photos/entry/{entries[1].id}", data={"reg_unknown": "on", "accepted": "on"})
    db.expire_all()
    assert all(db.get(models.ScanEntry, e.id).problems == [] for e in entries)
    client.post("/admin/photos/apply")
    jobs.wait_idle()
    db.expire_all()
    unknown = db.query(models.Aircraft).filter_by(reg_unknown=True).all()
    assert len(unknown) == 1 and unknown[0].registration == "?-1"
    assert len(unknown[0].sightings) == 2 and unknown[0].airline_id == lh.id
    assert "Registrierung unbekannt" in client.get(f"/aircraft/{unknown[0].id}").text or \
        "Registration unknown" in client.get(f"/aircraft/{unknown[0].id}").text
    # not counted in fleet completion
    assert {r.id: (r.x, r.y) for r in queries.fleet_completion(db)}.get(lh.id) == before.get(lh.id)

    # quick sighting without registration creates another placeholder
    client.post("/sightings/new", data={"reg_unknown": "on", "date": "2026-11-02", "airport": "FRA",
                                        "type_icao": "a20n", "airline": "DLH"})
    db.expire_all()
    second = db.query(models.Aircraft).filter_by(registration="?-2").one()
    assert second.reg_unknown and second.type_icao == "A20N"

    # registration found later: new one renames, existing one merges
    client.post(f"/aircraft/{unknown[0].id}/edit", data={"registration": "d-aixa", "airline": "Lufthansa"})
    db.expire_all()
    renamed = db.get(models.Aircraft, unknown[0].id)
    assert renamed.registration == "D-AIXA" and not renamed.reg_unknown
    assert all(o.data.get("registration") == "D-AIXA" for o in db.query(models.FolderOverride)
               if o.folder == "Lufthansa/B77W/Lufthansa B77W")
    second_id, renamed_id = second.id, renamed.id
    client.post(f"/aircraft/{second_id}/edit", data={"registration": "D-AIMH", "airline": "Lufthansa"})
    db.expire_all()
    assert db.get(models.Aircraft, second_id) is None
    aimh = db.query(models.Aircraft).filter_by(registration="D-AIMH").one()
    assert len(aimh.sightings) == 1 and aimh.sightings[0].date.isoformat() == "2026-11-02"
    # a known registration cannot be changed this way
    client.post(f"/aircraft/{renamed_id}/edit", data={"registration": "D-XXXX", "airline": "Lufthansa"})
    db.expire_all()
    assert db.get(models.Aircraft, renamed_id).registration == "D-AIXA"


def test_two_factor_login(client, db):
    import pyotp

    r = client.get("/profile/2fa/setup")
    secret = re.search(r"<code>([A-Z2-7]+)</code>", r.text).group(1)
    r = client.post("/profile/2fa/setup", data={"code": pyotp.TOTP(secret).now()})
    codes = re.findall(r"<li>([a-z0-9]{5}-[a-z0-9]{5})</li>", r.text)
    assert len(codes) == 10
    client.get("/logout")
    r = client.post("/login", data={"username": "admin", "password": "admin-password"})
    assert "/login/2fa" in str(r.url)
    r = client.post("/login/2fa", data={"code": "000000"})
    assert r.status_code == 401
    r = client.post("/login/2fa", data={"code": codes[0]})
    assert r.status_code == 200 and "/login" not in str(r.url)


def test_viewer_cannot_write(client, db):
    client.post("/admin/users/new", data={"username": "leser", "password": "leser-pass", "role": "viewer"})
    client.get("/logout")
    client.post("/login", data={"username": "leser", "password": "leser-pass"})
    assert client.get("/aircraft").status_code == 200
    assert client.get("/sightings/new").status_code == 403
    assert client.post("/admin/photos/scan").status_code == 403
