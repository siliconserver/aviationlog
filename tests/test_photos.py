import fixtures_photos
import photos


def test_parse_examples_from_the_archive():
    parsed, problems = photos.parse_folder("Lufthansa/A20N/D-AIJC Lufthansa A20N Lünen/2026-06-05-FRA")
    assert problems == []
    assert parsed["registration"] == "D-AIJC"
    assert parsed["airline"] == "Lufthansa"
    assert parsed["type_icao"] == "A20N"
    assert parsed["christening_name"] == "Lünen"
    assert parsed["livery"] is None
    assert parsed["date"] == "2026-06-05"
    assert parsed["airport"] == "FRA"

    parsed, _ = photos.parse_folder("SunExpress/B738/TC-SOZ SunExpress (Istanbul Livery) B738/2026-06-15-CGN")
    assert parsed["registration"] == "TC-SOZ"
    assert parsed["livery"] == "Istanbul Livery"
    assert parsed["christening_name"] is None

    parsed, _ = photos.parse_folder("German Airways/E190/D-AJHW German Airways E190/2026-08-10-FRA")
    assert parsed["airline"] == "German Airways"
    assert parsed["christening_name"] is None

    parsed, _ = photos.parse_folder("Lufthansa/B789/D-ABPU Lufthansa B789 Berlin 100yrs/2026-04-17-FRA")
    assert parsed["christening_name"] == "Berlin 100yrs"


def test_parse_problems():
    _, problems = photos.parse_folder("Misc/whatever/photo-folder")
    assert "date" in problems and "airport" in problems
    parsed, problems = photos.parse_folder("Lufthansa/A20N/D-AIJC Lufthansa A20N/2026-13-45-FRA")
    assert "date" in problems


def test_build_folder_round_trip():
    from datetime import date

    folder = photos.build_folder("SunExpress", "B738", "TC-SOZ", None, "Istanbul Livery", date(2026, 6, 15), "CGN")
    assert folder == "SunExpress/B738/TC-SOZ SunExpress B738 (Istanbul Livery)/2026-06-15-CGN"
    parsed, problems = photos.parse_folder(folder)
    assert problems == []
    assert parsed["livery"] == "Istanbul Livery"
    assert photos.build_folder("A/B", "X", "D-A", None, None, date(2026, 1, 1), None).startswith("A B/X/")


def test_safe_join_refuses_traversal(tmp_path, monkeypatch):
    monkeypatch.setenv("PHOTO_DIR", str(tmp_path))
    try:
        photos.safe_join("../etc/passwd")
    except ValueError:
        pass
    else:
        raise AssertionError("path traversal not refused")


def test_exif_and_previews(tmp_path):
    jpg = tmp_path / "a.jpg"
    fixtures_photos.jpeg_with_exif(str(jpg))
    exif = photos.read_exif(jpg)
    assert exif["camera_model"] == "ILCE-7RM5"
    assert exif["lens"].startswith("FE 100-400")
    assert exif["aperture"] == 5.6 and exif["iso"] == 200 and exif["exposure"] == "1/1000"
    assert abs(exif["gps_lat"] - 50.0405) < 0.001

    arw = tmp_path / "b.arw"
    fixtures_photos.fake_arw(str(arw), orientation=6)
    img = photos.open_image(arw)
    assert img.size == (427, 640)  # rotated by the orientation tag

    from PIL import Image

    heic = tmp_path / "c.heic"
    with Image.open(jpg) as im:
        im.save(heic, exif=im.info["exif"])
    assert photos.open_image(heic).size == (1200, 800)
    assert photos.read_exif(heic)["camera_model"] == "ILCE-7RM5"


def test_nearest_spot():
    class S:
        def __init__(self, lat, lon):
            self.lat, self.lon = lat, lon

    near, far = S(50.0406, 8.5861), S(50.1, 8.7)
    assert photos.nearest_spot([far, near], 50.0405, 8.5862) is near
    assert photos.nearest_spot([far], 48.0, 11.0) is None
