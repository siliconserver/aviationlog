"""Editing and adding airlines, airports and fleet aircraft by hand (issue #15)."""
from datetime import date

import importers
import models
from test_import import DATA, import_all, run_import


def test_edit_airport_is_protected_from_import(client, db):
    import_all(db)
    fra = db.query(models.Airport).filter_by(icao="EDDF").one()
    r = client.post(f"/airports/{fra.id}/edit", data={
        "name": "Frankfurt", "iata": "fra", "icao": "EDDF", "type": "large_airport",
        "municipality": "Frankfurt", "country": "de", "lat": "50,0363", "lon": "8.5593"})
    assert r.status_code == 200
    db.expire_all()
    assert fra.name == "Frankfurt" and fra.lat == 50.0363
    assert set(fra.manual_fields) == {"name", "municipality", "lat", "lon"}
    batch, stats = run_import(db, "ourairports", DATA / "ourairports.csv")
    assert stats["changed"] == 0  # protected fields are no change
    # invalid codes are refused
    r = client.post(f"/airports/{fra.id}/edit", data={"name": "Frankfurt", "iata": "FRAX", "icao": "EDDF"})
    assert r.status_code == 400 and "3" in r.text
    # unlocking hands the field back to the import
    client.post(f"/airports/{fra.id}/edit", data={
        "name": "Frankfurt", "iata": "FRA", "icao": "EDDF", "type": "large_airport", "municipality": "Frankfurt",
        "country": "DE", "lat": "50.0363", "lon": "8.5593", "unlock": ["name"]})
    db.expire_all()
    assert "name" not in fra.manual_fields


def test_new_airport_is_merged_by_icao(client, db):
    r = client.post("/airports/new", data={"name": "Mein München", "icao": "eddm", "iata": ""})
    assert r.status_code == 200
    ap = db.query(models.Airport).filter_by(icao="EDDM").one()
    assert ap.ident.startswith("manual:") and ap.manual_fields == ["name", "icao"] and ap.lat is None
    assert client.get(f"/airports/{ap.id}").status_code == 200  # no coordinates yet
    db.add(models.Spot(airport_id=ap.id, name="Besucherhügel", lat=48.35, lon=11.77))
    db.commit()

    batch, stats = run_import(db, "ourairports", DATA / "ourairports.csv")
    assert stats["changed"] == 1 and stats["new"] == 2
    importers.apply_batch(db, batch)
    db.expire_all()
    ap = db.query(models.Airport).filter_by(icao="EDDM").one()
    assert ap.ident == "EDDM" and ap.name == "Mein München" and ap.iata == "MUC" and ap.lat == 48.353802
    assert [s.name for s in ap.spots] == ["Besucherhügel"]

    # a second airport with that ICAO code is refused
    r = client.post("/airports/new", data={"name": "Doppelt", "icao": "EDDM"})
    assert r.status_code == 400 and "Mein München" in r.text


def test_manual_duplicate_is_merged_into_existing(client, db):
    import_all(db)
    dup = models.Airport(ident="manual:99", name="Köln/Bonn", icao="EDDK", manual_fields=["name", "icao"])
    db.add(dup)
    db.flush()
    ac = db.query(models.Aircraft).filter_by(registration="D-AIJC").one()
    db.add(models.Sighting(aircraft_id=ac.id, date=date(2026, 5, 1), airport_id=dup.id))
    db.commit()
    run_import(db, "ourairports", DATA / "ourairports.csv")
    db.expire_all()
    eddk = db.query(models.Airport).filter_by(icao="EDDK").one()  # only one left
    assert eddk.ident == "EDDK" and eddk.name == "Köln/Bonn" and "name" in eddk.manual_fields
    assert ac.sightings[0].airport_id == eddk.id


def test_new_airline_fleet_and_merge(client, db):
    r = client.post("/airlines/new", data={"name": "Mein Condor", "icao": "cfg", "iata": "", "active": "on"})
    assert r.status_code == 200
    al = db.query(models.Airline).filter_by(icao="CFG").one()
    assert al.source_key.startswith("manual:") and al.active

    # aircraft not spotted yet
    r = client.post(f"/airlines/{al.id}/aircraft/new", data={"registration": "d-abcd", "type_icao": "a32n",
                                                            "msn": "12345"})
    assert r.status_code == 200
    ac = db.query(models.Aircraft).filter_by(registration="D-ABCD").one()
    assert ac.airline_id == al.id and ac.type_icao == "A32N" and ac.source == "manual"
    assert set(ac.manual_fields) == {"airline_id", "type_icao", "msn"} and not ac.sightings
    page = client.get(f"/airlines/{al.id}").text
    assert "D-ABCD" in page and "0 / 1" in page
    r = client.post(f"/airlines/{al.id}/aircraft/new", data={"registration": "D-ABCD"})
    assert r.status_code == 400

    import_all(db)
    db.expire_all()
    assert al.source_key == "of:1868" and al.name == "Mein Condor" and al.iata == "DE"
    assert db.query(models.Airline).filter_by(icao="CFG").count() == 1
    assert ac.airline_id == al.id

    # edit; a second active airline with the same ICAO code is refused
    r = client.post(f"/airlines/{al.id}/edit", data={"name": "Condor", "iata": "DE", "icao": "CFG",
                                                    "callsign": "CONDOR", "country": "Germany", "active": "on"})
    assert r.status_code == 200
    db.expire_all()
    assert al.name == "Condor" and al.callsign == "CONDOR"
    r = client.post("/airlines/new", data={"name": "Noch ein Condor", "icao": "CFG", "active": "on"})
    assert r.status_code == 400
    r = client.post("/airlines/new", data={"name": "Falsch", "icao": "CF1"})
    assert r.status_code == 400


def test_fleet_overview_toggle(client, db):
    import_all(db)
    ac = db.query(models.Aircraft).filter_by(registration="D-AIJC").one()
    db.add(models.Sighting(aircraft_id=ac.id, date=date(2026, 5, 1)))
    db.commit()
    default = client.get("/airlines").text
    assert "Lufthansa" in default and "Germania" not in default and "?all=1" in default
    everything = client.get("/airlines?all=1").text
    assert "Germania" in everything and "SunExpress" in everything


def test_delete_only_without_sightings(client, db):
    import_all(db)
    fra = db.query(models.Airport).filter_by(icao="EDDF").one()
    muc = db.query(models.Airport).filter_by(icao="EDDM").one()
    db.add(models.Spot(airport_id=muc.id, name="Hügel", lat=48.35, lon=11.77))
    ac = db.query(models.Aircraft).filter_by(registration="D-AIJC").one()
    db.add(models.Sighting(aircraft_id=ac.id, date=date(2026, 5, 1), airport_id=fra.id))
    db.commit()
    lh, cfg = ac.airline, db.query(models.Airline).filter_by(icao="CFG").one()
    condor_ac = db.query(models.Aircraft).filter_by(airline_id=cfg.id).all()
    assert condor_ac

    assert "Löschen geht nicht" not in client.get(f"/airports/{muc.id}/edit").text
    client.post(f"/airports/{fra.id}/delete")
    client.post(f"/airlines/{lh.id}/delete")
    db.expire_all()
    assert db.query(models.Airport).filter_by(id=fra.id).count() == 1
    assert db.query(models.Airline).filter_by(id=lh.id).count() == 1

    muc_id, cfg_id = muc.id, cfg.id
    client.post(f"/airports/{muc_id}/delete")
    client.post(f"/airlines/{cfg_id}/delete")
    db.expire_all()
    assert db.query(models.Airport).filter_by(id=muc_id).count() == 0 and db.query(models.Spot).count() == 0
    assert db.query(models.Airline).filter_by(id=cfg_id).count() == 0
    for a in condor_ac:
        db.refresh(a)
        assert a.airline_id is None and "airline_id" in a.manual_fields


def test_master_forms_render_and_viewer_cannot_edit(client, db):
    import_all(db)
    al = db.query(models.Airline).first()
    ap = db.query(models.Airport).first()
    for url in ["/airlines/new", "/airports/new", f"/airlines/{al.id}/edit", f"/airports/{ap.id}/edit",
                f"/airlines/{al.id}/aircraft/new"]:
        assert client.get(url).status_code == 200, url
    client.post("/admin/users/new", data={"username": "leser", "password": "leser-pass", "role": "viewer"})
    client.get("/logout")
    client.post("/login", data={"username": "leser", "password": "leser-pass"})
    assert client.get("/airlines/new").status_code == 403
    assert client.post(f"/airports/{ap.id}/edit", data={"name": "x"}).status_code == 403
    assert client.post(f"/airlines/{al.id}/delete").status_code == 403
    assert "/airlines/new" not in client.get("/airlines").text
