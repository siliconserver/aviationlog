"""Aircraft lookups at airport-data.com (parsed profile page) and flightera.net (API key)."""
from datetime import date

import httpx
import pytest

import airportdata
import flightera
import importers
import jobs
import lookups
import models
import settings_store

NAV = """<html><head><title>Aircraft Data {reg}</title><script>var Manufacturer = 1;</script></head><body>
<nav><a href="/search">Search</a><label>Manufacturer</label><select><option>Airbus</option></select></nav>"""

PAGE_NEW = NAV.format(reg="D-AIJA") + """
<h1>D-AIJA &mdash; 2020 Airbus A320-271 Neo Data</h1>
<p>2 aircraft records found</p>
<ul>
  <li><a href="/aircraft/D-AIJA:airbus-a320-271-neo:900001">2020 Airbus A320-271 Neo, c/n 9555</a></li>
  <li><a href="/aircraft/D-AIJA:airbus-acj318-a318-112-cj:866855">2006 Airbus ACJ318 (A318-112/CJ), c/n 2910</a></li>
</ul>
<h2>Airframe Info</h2>
<table>
<tr><td>Manufacturer:</td><td>Airbus</td></tr>
<tr><td>Model:</td><td>A320-271 Neo <a href="/search?m=1">Search all Airbus A320-271 Neo</a></td></tr>
<tr><td>Year built:</td><td>2020</td></tr>
<tr><td>Construction Number (C/N):</td><td>9555</td></tr>
<tr><td>Aircraft Type:</td><td>Fixed wing multi engine</td></tr>
<tr><td>Number of Seats:</td><td>180</td></tr>
<tr><td>Number of Engines:</td><td>2</td></tr>
<tr><td>Engine Type:</td><td>Turbofan</td></tr>
<tr><td>Engine Manufacturer and Model:</td><td>Pratt &amp; Whitney PW 1127G</td></tr>
</table>
<h2>Aircraft</h2>
<table>
<tr><td>Registration Number:</td><td>D-AIJA</td></tr>
<tr><td>Mode S (ICAO24) Code:</td><td></td></tr>
<tr><td>Current Status:</td><td>Registered</td></tr>
<tr><td>Delivery Date:</td><td>2020-09-24</td></tr>
</table>
<h2>Owner</h2>
<table><tr><td>Owner:</td><td>Lufthansa</td></tr><tr><td>Address:</td><td>Frankfurt,<br>Germany</td></tr></table>
<h2>Latest photos of D-AIJA</h2></body></html>"""

# the page of a registration that shows an older record first
PAGE_OLD = NAV.format(reg="D-AOLD") + """
<p>2 aircraft records found</p>
<a href="/aircraft/D-AOLD:airbus-a321-271nx:42">2023 Airbus A321-271NX, c/n 11111</a>
<h2>Airframe Info</h2>
<table><tr><td>Manufacturer:</td><td>Boeing</td></tr><tr><td>Model:</td><td>737-330</td></tr>
<tr><td>Year built:</td><td>1987</td></tr><tr><td>Construction Number (C/N):</td><td>23835</td></tr></table>
<h2>Aircraft</h2><table><tr><td>Registration Number:</td><td>D-AOLD</td></tr>
<tr><td>Mode S (ICAO24) Code:</td><td>3C4A0F</td></tr></table>"""

PAGE_OLD_NEWER = NAV.format(reg="D-AOLD") + """
<h2>Airframe Info</h2>
<table><tr><td>Manufacturer:</td><td>Airbus</td></tr><tr><td>Model:</td><td>A321-271NX</td></tr>
<tr><td>Year built:</td><td>2023</td></tr><tr><td>Construction Number (C/N):</td><td>11111</td></tr></table>
<h2>Aircraft</h2><table><tr><td>Registration Number:</td><td>D-AOLD</td></tr>
<tr><td>Mode S (ICAO24) Code:</td><td>3c6abc</td></tr><tr><td>Delivery Date:</td><td>2023-03-01</td></tr></table>"""


def _site(url, **kwargs):
    path = url.split("example.invalid", 1)[1]
    pages = {
        "/aircraft/D-AIJA": PAGE_NEW,
        "/aircraft/D-AOLD": PAGE_OLD,
        "/aircraft/D-AOLD:airbus-a321-271nx:42": PAGE_OLD_NEWER,
    }
    if path in pages:
        return httpx.Response(200, text=pages[path])
    if path == "/aircraft/BROKEN":
        return httpx.Response(500)
    return httpx.Response(404, text="not found")


@pytest.fixture()
def site(monkeypatch):
    monkeypatch.setenv("AIRPORTDATA_URL", "https://example.invalid")
    monkeypatch.setenv("AIRPORTDATA_DELAY", "0")
    seen = []

    def get(url, **kwargs):
        seen.append(url)
        return _site(url, **kwargs)

    monkeypatch.setattr(httpx, "get", get)
    return seen


def test_parse_profile_page(site):
    info = airportdata.fetch(None, "D-AIJA")
    assert info["manufacturer"] == "Airbus" and info["model"] == "A320-271 Neo"
    assert info["built"] == "2020" and info["msn"] == "9555" and info["delivery_date"] == "2020-09-24"
    assert info["engines"] == "Pratt & Whitney PW 1127G" and info["owner"] == "Lufthansa"
    assert info["address"] == "Frankfurt, Germany"
    assert "mode_s" not in info  # empty on the page
    assert site == ["https://example.invalid/aircraft/D-AIJA"]  # newest record already shown
    rec = airportdata.to_record(None, None, info)
    assert rec["icao24"] is None and rec["built"] == "2020" and rec["delivery_date"] == "2020-09-24"

    assert airportdata.fetch(None, "D-ANONE") is None
    with pytest.raises(lookups.LookupError):
        airportdata.fetch(None, "BROKEN")


def test_newest_record_wins(site):
    info = airportdata.fetch(None, "d-aold")
    assert info["msn"] == "11111" and info["model"] == "A321-271NX" and info["mode_s"] == "3c6abc"
    assert site[-1] == "https://example.invalid/aircraft/D-AOLD:airbus-a321-271nx:42"


def test_button_fills_gaps_and_keeps_manual(client, db, site):
    ac = models.Aircraft(registration="D-AIJA", source="manual", model="A320neo", operator_name="Eurowings",
                         built="2019", manual_fields=["built"])
    db.add(ac)
    db.commit()
    page = client.get(f"/aircraft/{ac.id}").text
    assert f"/aircraft/{ac.id}/lookup/airportdata" in page and "/lookup/adsbdb" not in page
    assert "/lookup/flightera" not in page  # no key
    r = client.post(f"/aircraft/{ac.id}/lookup/airportdata")
    assert r.status_code == 200 and "airport-data.com" in r.text
    db.expire_all()
    assert ac.msn == "9555" and ac.delivery_date == date(2020, 9, 24) and ac.engines == "Pratt & Whitney PW 1127G"
    assert ac.manufacturer == "Airbus"
    assert ac.built == "2019"  # set by hand
    assert ac.model == "A320neo" and ac.operator_name == "Eurowings"  # only filled when empty
    r = client.post(f"/aircraft/{ac.id}/lookup/airportdata")
    assert "keine neuen Daten" in r.text or "no new data" in r.text
    ac.msn = "09555"  # same MSN with a leading zero (OpenSky style) is not changed
    db.commit()
    r = client.post(f"/aircraft/{ac.id}/lookup/airportdata")
    assert "keine neuen Daten" in r.text or "no new data" in r.text


def test_airportdata_only_on_click(client, db, site):
    client.post("/sightings/new", data={"registration": "D-AIJA", "date": "2026-11-01"})
    lookups.wait_idle()
    assert site == []  # not asked automatically
    page = client.get("/admin/data").text
    assert "/admin/data/airportdata/" not in page  # no lookup of the whole collection
    assert client.post("/admin/data/airportdata/download").status_code == 404


# ---- flightera ---------------------------------------------------------------------------

FLIGHTERA = {
    "reg": "D-ABYA", "hex": "3C4B21", "manufacturer": "Boeing", "model": "747-8", "model_family": "Boeing 747",
    "airline": "Lufthansa", "seat_config": {"F": 8, "C": 80, "W": 32, "Y": 244},
    "first_flight": "2012-02-29", "last_flight": "2026-10-20", "last_flight_number": "LH400", "is_cargo": False,
}


@pytest.fixture()
def api(monkeypatch):
    monkeypatch.setenv("FLIGHTERA_URL", "https://example.invalid/dapi/v1")
    monkeypatch.setenv("FLIGHTERA_DELAY", "0")
    seen = []
    quota = []

    def get(url, headers=None, **kwargs):
        seen.append((url, headers.get("X-Api-Key")))
        if headers.get("X-Api-Key") != "good":
            return httpx.Response(403, json={"error": "unknown key", "code": "invalid_key", "status": 403})
        if url.endswith("reg=D-ABYA"):
            return httpx.Response(200, json=FLIGHTERA, headers={"X-Credit-Cost": "1", "X-Credit-Balance": "999"})
        if url.endswith("reg=D-AEMPTY") or (quota and url.endswith("reg=D-AZZZ")):
            return httpx.Response(402, json={"error": "API quota exhausted", "code": "quota_exceeded",
                                             "status": 402}, headers={"X-Credit-Balance": "0"})
        return httpx.Response(404, json={"error": "not found", "code": "not_found", "status": 404})

    monkeypatch.setattr(httpx, "get", get)
    get.quota = quota
    return seen, quota


def test_flightera_key_setting_and_button(client, db, api):
    api, _ = api
    ac = models.Aircraft(registration="D-ABYA", source="opensky", model="747-830", manual_fields=[])
    db.add(ac)
    db.commit()
    assert "/admin/data/flightera/key" in client.get("/admin/data").text
    client.post("/admin/data/flightera/key", data={"key": "  good "})
    assert settings_store.get(db, flightera.SETTING_KEY) == "good"
    assert f"/aircraft/{ac.id}/lookup/flightera" in client.get(f"/aircraft/{ac.id}").text

    r = client.post(f"/aircraft/{ac.id}/lookup/flightera")
    assert "flightera.net" in r.text
    assert api[0] == ("https://example.invalid/dapi/v1/aircraft/info?reg=D-ABYA", "good")
    db.expire_all()
    assert ac.icao24 == "3c4b21" and ac.first_flight == "2012-02-29" and ac.operator_name == "Lufthansa"
    assert ac.model == "747-830" and ac.manufacturer == "Boeing"
    assert flightera.balance(db)[0] == "999"
    assert "999" in client.get("/admin/data").text

    # never looked up automatically
    client.post("/sightings/new", data={"registration": "D-AEMPTY", "date": "2026-11-01"})
    lookups.wait_idle()
    assert len(api) == 1

    other = db.query(models.Aircraft).filter_by(registration="D-AEMPTY").one()
    r = client.post(f"/aircraft/{other.id}/lookup/flightera")
    assert "aufgebraucht" in r.text or "used up" in r.text
    assert flightera.balance(db)[0] == "0"

    settings_store.set(db, flightera.SETTING_KEY, "bad")
    r = client.post(f"/aircraft/{other.id}/lookup/flightera")
    assert "ungültig" in r.text or "not valid" in r.text

    client.post("/admin/data/flightera/key", data={"remove": "1"})
    assert settings_store.get(db, flightera.SETTING_KEY) == ""
    assert "/lookup/flightera" not in client.get(f"/aircraft/{ac.id}").text


def test_flightera_whole_collection(client, db, api):
    seen, quota = api
    page = client.get("/admin/data").text
    assert "/admin/data/flightera/key" in page
    # without a key: no lookup
    client.post("/admin/data/flightera/download")
    jobs.wait_idle()
    assert seen == [] and not db.query(models.ImportBatch).filter_by(source="flightera").count()

    settings_store.set(db, flightera.SETTING_KEY, "good")
    for reg in ("D-ABYA", "D-AUNK", "D-AZZZ"):
        ac = models.Aircraft(registration=reg, source="manual", manual_fields=[])
        db.add(ac)
    db.commit()
    assert "data-confirm" in client.get("/admin/data").text
    client.post("/admin/data/flightera/download")
    jobs.wait_idle()
    assert len(seen) == 3
    batch = db.query(models.ImportBatch).filter_by(source="flightera").one()
    assert batch.status == "ready" and batch.stats["changed"] == 1
    row = db.query(models.ImportRow).filter_by(batch_id=batch.id).one()
    assert row.key == "D-ABYA" and {"icao24", "first_flight", "operator_name"} <= set(row.old)
    importers.apply_batch(db, batch)
    ac = db.query(models.Aircraft).filter_by(registration="D-ABYA").one()
    assert ac.first_flight == "2012-02-29" and ac.source == "manual"

    # credits used up: stops at once and keeps what was found before
    quota.append(True)
    seen.clear()
    client.post("/admin/data/flightera/download")
    jobs.wait_idle()
    assert [u for u, _ in seen][-1].endswith("D-AZZZ") and len(seen) == 3
    batch = db.query(models.ImportBatch).filter_by(source="flightera").order_by(models.ImportBatch.id.desc()).first()
    assert batch.status == "ready"


def test_flightera_airline_code_resolved(db):
    db.add_all([
        models.Airline(source_key="of:1", name="Croatia Airlines", icao="CTN", iata="OU", active=True, manual_fields=[]),
        models.Airline(source_key="of:2", name="Old OU Air", icao="OUX", iata="OU", active=False, manual_fields=[]),
        models.Airline(source_key="of:3", name="Twin A", icao="TWA", iata="TW", active=True, manual_fields=[]),
        models.Airline(source_key="of:4", name="Twin B", icao="TWB", iata="TW", active=True, manual_fields=[]),
    ])
    db.commit()
    ac = models.Aircraft(registration="9A-CTN", source="opensky", operator_name="Croatia Airlines",
                         operator_icao="CTN", manual_fields=[])
    db.add(ac)
    db.commit()
    rec = lookups.record(db, flightera, ac, {"reg": "9A-CTN", "airline": "OU", "first_flight": "2012-03-13"})
    assert rec["operator_name"] == "Croatia Airlines" and rec["operator_iata"] == "OU" and rec["operator_icao"] == "CTN"
    assert lookups.changes(db, flightera, ac, {"airline": "OU"}) == {"operator_iata": "OU"}
    # ICAO code works too; unknown or ambiguous codes change nothing
    assert lookups.record(db, flightera, ac, {"airline": "CTN"})["operator_iata"] == "OU"
    assert "operator_name" not in lookups.record(db, flightera, ac, {"airline": "XX"})
    assert "operator_name" not in lookups.record(db, flightera, ac, {"airline": "TW"})
