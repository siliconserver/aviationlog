from datetime import date

from models import Aircraft


def _age(start: date) -> float:
    return round((date.today() - start).days / 365.25, 1)


def test_age_uses_build_year_before_delivery():
    ac = Aircraft(registration="D-AIMA", built="2008", delivery_date=date(2009, 5, 19))
    assert ac.age_years == _age(date(2008, 7, 1))
    ac.built = "2008-11-03"
    assert ac.age_years == _age(date(2008, 11, 3))


def test_age_fallbacks():
    assert Aircraft(registration="X", first_flight="2010-02-01",
                    delivery_date=date(2011, 1, 1)).age_years == _age(date(2010, 2, 1))
    assert Aircraft(registration="X", delivery_date=date(2011, 1, 1)).age_years == _age(date(2011, 1, 1))
    assert Aircraft(registration="X", built="unknown").age_years is None
    assert Aircraft(registration="X", built="2012-13-40").age_years == _age(date(2012, 7, 1))
