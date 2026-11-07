from pathlib import Path

import importers
import models

DATA = Path(__file__).parent / "data"


def run_import(db, source, path):
    batch = models.ImportBatch(source=source, status="loading")
    db.add(batch)
    db.commit()
    entity = importers.SOURCES[source]["entity"]
    importers.stage(batch.id, entity, importers.PARSERS[source](path))
    stats = importers.diff(db, batch.id, entity)
    batch.status = "ready"
    batch.stats = stats
    db.commit()
    return batch, stats


def import_all(db):
    for source, name in [("ourairports", "ourairports.csv"), ("openflights_airlines", "airlines.dat"),
                         ("openflights_planes", "planes.dat"), ("opensky", "opensky.csv")]:
        batch, _ = run_import(db, source, DATA / name)
        importers.apply_batch(db, batch)


def test_parsers():
    rows = dict(importers.parse_opensky(DATA / "opensky.csv"))
    assert rows["D-AIJC"]["msn"] == "7965"
    assert rows["D-AIJC"]["type_icao"] == "A20N"
    assert rows["D-AIJC"]["operator_icao"] == "DLH"
    assert rows["D-AIJC"]["built"] == "2018-02-01"
    assert rows["D-ANRA"]["built"] == "2023"
    assert "" not in rows
    airports = dict(importers.parse_ourairports(DATA / "ourairports.csv"))
    assert airports["EDDM"]["iata"] == "MUC" and airports["EDDM"]["icao"] == "EDDM"
    assert "DE-0001" not in airports  # closed
    airlines = dict(importers.parse_openflights_airlines(DATA / "airlines.dat"))
    assert airlines["of:3320"]["icao"] == "DLH" and airlines["of:3320"]["active"] is True


def test_opensky_classic_format(tmp_path):
    f = tmp_path / "aircraftDatabase.csv"
    f.write_text('"icao24","registration","manufacturericao","manufacturername","model","typecode",'
                 '"serialnumber","linenumber","icaoaircrafttype","operator","operatorcallsign","operatoricao",'
                 '"operatoriata","owner","testreg","registered","reguntil","status","built","firstflightdate",'
                 '"seatconfiguration","engines","modes","adsb","acars","notes","categoryDescription"\n'
                 '"3c6568","D-AIMH","AIRBUS","Airbus","A320-211","A320","0123","","L2J","Lufthansa","LUFTHANSA",'
                 '"DLH","LH","","","","","","1990-01-01","","","CFM56","false","false","false","",""\n')
    rows = dict(importers.parse_opensky(f))
    assert rows["D-AIMH"]["msn"] == "0123" and rows["D-AIMH"]["operator_icao"] == "DLH"


def test_import_apply_and_airline_link(db):
    import_all(db)
    ac = db.query(models.Aircraft).filter_by(registration="D-AIJC").one()
    assert ac.airline.name == "Lufthansa"
    assert ac.aircraft_type.iata == "32N"
    anra = db.query(models.Aircraft).filter_by(registration="D-ANRA").one()
    assert anra.airline.icao == "CFG"  # active airline wins over the defunct one
    assert db.query(models.Airport).filter_by(iata="FRA").one().icao == "EDDF"


def test_reimport_protects_manual_values(db, tmp_path):
    import_all(db)
    ac = db.query(models.Aircraft).filter_by(registration="D-AIJC").one()
    ac.engines = "my own value"
    ac.manual_fields = ["engines"]
    db.add(models.Sighting(aircraft_id=ac.id, date=__import__("datetime").date(2026, 1, 1)))
    db.commit()

    changed = tmp_path / "opensky.csv"
    text = (DATA / "opensky.csv").read_text().replace("'7965'", "'7966'").replace("CFM LEAP-1A26", "LEAP NEW")
    changed.write_text(text)
    batch, stats = run_import(db, "opensky", changed)
    assert stats["changed"] == 1 and stats["changed_in_collection"] == 1 and stats["new"] == 0
    row = db.query(models.ImportRow).filter_by(batch_id=batch.id).one()
    assert set(row.old) == {"msn"}  # engines is protected, so it is no change at all
    importers.apply_batch(db, batch)
    db.refresh(ac)
    assert ac.msn == "7966" and ac.engines == "my own value"


def test_rejected_and_edited_rows(db, tmp_path):
    import_all(db)
    changed = tmp_path / "opensky.csv"
    changed.write_text((DATA / "opensky.csv").read_text().replace("'2053'", "'9999'").replace("'2061'", "'8888'"))
    batch, _ = run_import(db, "opensky", changed)
    rows = {r.key: r for r in db.query(models.ImportRow).filter_by(batch_id=batch.id)}
    rows["D-ANRA"].accepted = False
    rows["D-ANRB"].edited = {"msn": "2061-corrected"}
    db.commit()
    importers.apply_batch(db, batch)
    anra = db.query(models.Aircraft).filter_by(registration="D-ANRA").one()
    anrb = db.query(models.Aircraft).filter_by(registration="D-ANRB").one()
    assert anra.msn == "2053"
    assert anrb.msn == "2061-corrected" and "msn" in anrb.manual_fields
