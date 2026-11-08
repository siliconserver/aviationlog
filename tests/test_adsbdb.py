"""Aircraft lookup via adsbdb.com: button, automatic lookup of new aircraft, whole collection."""
from datetime import date

import httpx
import pytest

import adsbdb
import importers
import jobs
import models
import settings_store

ANSWERS = {
    "D-AXYZ": {
        "type": "A320 214", "icao_type": "A320", "manufacturer": "Airbus", "mode_s": "3C1234",
        "registration": "D-AXYZ", "registered_owner_country_iso_name": "DE",
        "registered_owner_country_name": "Germany", "registered_owner_operator_flag_code": "DLH",
        "registered_owner": "Lufthansa", "url_photo": None, "url_photo_thumbnail": None,
    },
    "D-ANEW": {
        "type": "B737 8K5", "icao_type": "B738", "manufacturer": "Boeing", "mode_s": "3c5555",
        "registration": "D-ANEW", "registered_owner_operator_flag_code": "TUI", "registered_owner": "TUIfly",
    },
}


@pytest.fixture()
def fake(monkeypatch):
    calls = []

    def fetch(reg):
        calls.append(reg)
        return ANSWERS.get(reg)

    monkeypatch.setattr(adsbdb, "enabled", lambda: True)
    monkeypatch.setattr(adsbdb, "fetch", fetch)
    return calls


def test_fetch_parses_answer(monkeypatch):
    monkeypatch.setenv("ADSBDB_URL", "https://example.invalid/v0")
    monkeypatch.setenv("ADSBDB_DELAY", "0")
    seen = []

    def get(url, **kwargs):
        seen.append(url)
        if url.endswith("/D-AXYZ"):
            return httpx.Response(200, json={"response": {"aircraft": ANSWERS["D-AXYZ"]}})
        if url.endswith("/BROKEN"):
            return httpx.Response(500)
        return httpx.Response(404, json={"response": "unknown aircraft"})

    monkeypatch.setattr(httpx, "get", get)
    assert adsbdb.fetch("D-AXYZ")["icao_type"] == "A320"
    assert seen[0] == "https://example.invalid/v0/aircraft/D-AXYZ"
    assert adsbdb.fetch("D-ANONE") is None
    with pytest.raises(adsbdb.AdsbdbError):
        adsbdb.fetch("BROKEN")


def test_off_by_default_in_tests(client, db):
    assert not adsbdb.enabled()
    ac = models.Aircraft(registration="D-AXYZ", source="manual", manual_fields=[])
    db.add(ac)
    db.commit()
    assert "/lookup" not in client.get(f"/aircraft/{ac.id}").text


def test_button_keeps_manual_fields(client, db, fake):
    db.add(models.Airline(source_key="of:1", name="Lufthansa", icao="DLH", iata="LH", active=True,
                          manual_fields=[]))
    ac = models.Aircraft(registration="D-AXYZ", source="manual", type_icao="A20N", model="Airbus A320",
                         manual_fields=["type_icao"])
    db.add(ac)
    db.commit()
    page = client.get(f"/aircraft/{ac.id}").text
    assert f"/aircraft/{ac.id}/lookup" in page
    r = client.post(f"/aircraft/{ac.id}/lookup")
    assert r.status_code == 200 and "adsbdb" in r.text
    db.expire_all()
    assert ac.type_icao == "A20N"  # set by hand
    assert ac.model == "Airbus A320"  # only filled when empty
    assert ac.manufacturer == "Airbus" and ac.icao24 == "3c1234"
    assert ac.operator_icao == "DLH" and ac.operator_iata == "LH" and ac.operator_name == "Lufthansa"
    assert ac.airline is not None and ac.airline.icao == "DLH"
    # second click: nothing new
    client.post(f"/aircraft/{ac.id}/lookup")
    # unknown registration
    other = models.Aircraft(registration="D-AUNK", source="manual", manual_fields=[])
    db.add(other)
    db.commit()
    r = client.post(f"/aircraft/{other.id}/lookup")
    assert "D-AUNK" in r.text


def test_new_aircraft_looked_up_automatically(client, db, fake):
    client.post("/sightings/new", data={"registration": "d-anew", "date": "2026-11-01"})
    adsbdb.wait_idle()
    db.expire_all()
    ac = db.query(models.Aircraft).filter_by(registration="D-ANEW").one()
    assert ac.type_icao == "B738" and ac.operator_icao == "TUI" and ac.icao24 == "3c5555"

    # a second sighting of a known aircraft does not ask again
    fake.clear()
    client.post("/sightings/new", data={"registration": "D-ANEW", "date": "2026-11-02"})
    adsbdb.wait_idle()
    assert fake == []

    # switched off on the master data page
    r = client.post("/admin/data/adsbdb/auto", data={})
    assert r.status_code == 200
    assert settings_store.get(db, adsbdb.SETTING_AUTO) == "0"
    client.post("/sightings/new", data={"registration": "D-AXYZ", "date": "2026-11-02"})
    adsbdb.wait_idle()
    assert fake == []
    client.post("/admin/data/adsbdb/auto", data={"auto": "on"})
    assert adsbdb.auto_enabled(db)


def test_lookup_whole_collection_with_review(client, db, fake):
    ac = models.Aircraft(registration="D-AXYZ", source="opensky", type_icao="A320", model="A320-214",
                         operator_icao="EWG", operator_name="Eurowings", manual_fields=["operator_name"])
    unspotted = models.Aircraft(registration="D-ANEW", source="opensky", manual_fields=[])
    db.add_all([ac, unspotted])
    db.flush()
    db.add(models.Sighting(aircraft_id=ac.id, date=date(2026, 5, 1)))
    db.commit()
    page = client.get("/admin/data").text
    assert "/admin/data/adsbdb/download" in page
    assert client.post("/admin/data/adsbdb/upload", files={"file": ("a.csv", b"x")}).status_code == 404

    client.post("/admin/data/adsbdb/download")
    jobs.wait_idle()
    assert fake == ["D-AXYZ"]  # only the collection
    batch = db.query(models.ImportBatch).filter_by(source="adsbdb").one()
    assert batch.status == "ready" and batch.stats["changed"] == 1 and batch.stats["new"] == 0
    row = db.query(models.ImportRow).filter_by(batch_id=batch.id).one()
    # manual name and existing model stay, empty manufacturer is filled
    assert set(row.old) == {"icao24", "operator_icao", "manufacturer"}
    assert client.get(f"/admin/data/batch/{batch.id}").status_code == 200

    importers.apply_batch(db, batch)
    db.expire_all()
    assert ac.operator_icao == "DLH" and ac.icao24 == "3c1234" and ac.operator_name == "Eurowings"
    assert ac.model == "A320-214" and ac.source == "opensky"
