"""Photo storage, folder naming, EXIF and thumbnails.

Folder scheme (same for upload and NAS mode)::

    <Airline>/<Type ICAO>/<REG Airline Type [Christening name] [(Livery)]>/<YYYY-MM-DD-IATA>/<files>
"""
import io
import logging
import math
import os
import re
import struct
from datetime import date, datetime
from pathlib import Path

import exifread
from PIL import Image, ImageOps

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover - optional at runtime
    pillow_heif = None

logger = logging.getLogger("photos")

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
THUMB_DIR = DATA_DIR / "thumbs"
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heif", ".heic", ".hif", ".arw"}
THUMB_SIZES = {"s": 480, "m": 1920}


def photo_mode() -> str:
    mode = os.environ.get("PHOTO_MODE", "upload").strip().lower()
    return mode if mode in ("upload", "nas") else "upload"


def photo_dir() -> Path:
    return Path(os.environ.get("PHOTO_DIR", str(DATA_DIR / "photos")))


def is_photo(name: str) -> bool:
    return not name.startswith(".") and Path(name).suffix.lower() in PHOTO_EXTENSIONS


def safe_join(rel_path: str) -> Path:
    """Absolute path of a photo below the photo folder; refuses path traversal."""
    root = photo_dir().resolve()
    full = (root / rel_path).resolve()
    if full != root and root not in full.parents:
        raise ValueError("path outside photo folder")
    return full


# ---- Folder names -----------------------------------------------------------------

# Civil registrations (D-AIJC, N123AB) and German military serials (15+11). Used to spot folders
# where the first word is not a registration at all ("Emirates B77W").
REG_RE = re.compile(r"^[A-Z0-9]{1,4}(-[A-Z0-9]{1,6})?$|^N[0-9]{1,5}[A-Z]{0,2}$|^[0-9]{2}\+[0-9]{2}$")
# Anything a user may enter by hand as a registration or military serial (MM62243, 11-5101, ZZ336).
PLAUSIBLE_REG_RE = re.compile(r"^[A-Z0-9][A-Z0-9+.-]{0,15}$")
DATE_DIR_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[-_ ]+([A-Za-z0-9]{3,4})\b\s*(.*)$")


def normalize_registration(reg: str) -> str:
    return re.sub(r"\s+", "", (reg or "").upper())


def _remove_word(text: str, word: str) -> str:
    if not word:
        return text
    pattern = r"(?i)(^|\s)" + re.escape(word) + r"(?=\s|$)"
    return re.sub(pattern, " ", text, count=1)


def parse_aircraft_dir(name: str, airline: str = "", type_icao: str = "") -> dict:
    """Splits ``D-AIJC Lufthansa A20N Lünen`` into registration, christening name and livery.

    Text in brackets is the livery, everything else (minus airline and type) the christening name.
    """
    name = " ".join((name or "").split())
    reg, _, rest = name.partition(" ")
    liveries = [p.strip() for p in re.findall(r"\(([^)]*)\)", rest) if p.strip()]
    rest = re.sub(r"\([^)]*\)", " ", rest)
    rest = _remove_word(rest, airline)
    rest = _remove_word(rest, type_icao)
    christening = " ".join(rest.split())
    return {
        "registration": normalize_registration(reg),
        "christening_name": christening or None,
        "livery": "; ".join(liveries) or None,
    }


def parse_date_dir(name: str) -> dict:
    m = DATE_DIR_RE.match((name or "").strip())
    if not m:
        return {"date": None, "airport": None}
    try:
        d = date.fromisoformat(m.group(1)).isoformat()
    except ValueError:
        d = None
    return {"date": d, "airport": m.group(2).upper()}


def parse_folder(rel_folder: str) -> tuple[dict, list[str]]:
    """Parses a date folder path (relative to the photo folder).

    Returns the detected values and a list of problem codes.
    """
    parts = [p for p in rel_folder.split("/") if p]
    problems = []
    parsed = {
        "airline": None,
        "type_icao": None,
        "registration": None,
        "christening_name": None,
        "livery": None,
        "date": None,
        "airport": None,
    }
    if len(parts) != 4:
        problems.append("structure")
    date_dir = None
    if len(parts) >= 4:
        airline, type_icao, ac_dir, date_dir = parts[-4:]
    elif len(parts) == 3:
        airline, type_icao, ac_dir = parts
    else:
        airline, type_icao, ac_dir = "", "", parts[-1] if parts else ""
        date_dir = parts[-1] if parts else None
    parsed["airline"] = airline or None
    parsed["type_icao"] = type_icao.upper() or None
    parsed.update(parse_aircraft_dir(ac_dir, airline, type_icao))
    if date_dir:
        parsed.update(parse_date_dir(date_dir))
    if not parsed["registration"] or not REG_RE.match(parsed["registration"]):
        problems.append("registration")
    if not parsed["date"]:
        problems.append("date")
    if not parsed["airport"]:
        problems.append("airport")
    return parsed, problems


def _clean_component(text: str) -> str:
    text = re.sub(r"[/\\\x00-\x1f:*?\"<>|]", " ", text or "")
    text = " ".join(text.split()).strip(" .")
    return text or "Unknown"


def build_folder(airline: str, type_icao: str, registration: str, christening_name: str | None,
                 livery: str | None, day: date, airport_code: str | None) -> str:
    """Relative date folder for an uploaded photo, following the folder scheme."""
    airline = _clean_component(airline)
    type_icao = _clean_component(type_icao or "UNKN")
    ac = f"{registration} {airline} {type_icao}"
    if christening_name:
        ac += f" {christening_name}"
    if livery:
        ac += f" ({livery})"
    date_dir = f"{day.isoformat()}-{airport_code or 'XXX'}"
    return "/".join([airline, type_icao, _clean_component(ac), _clean_component(date_dir)])


def unique_name(folder: Path, filename: str) -> str:
    base = _clean_component(Path(filename).stem)
    ext = Path(filename).suffix.lower()
    candidate = f"{base}{ext}"
    n = 1
    while (folder / candidate).exists():
        n += 1
        candidate = f"{base}_{n}{ext}"
    return candidate


# ---- EXIF -----------------------------------------------------------------------------

def _num(tag):
    try:
        v = tag.values[0]
        return float(v.num) / float(v.den) if hasattr(v, "den") else float(v)
    except (AttributeError, IndexError, ZeroDivisionError, TypeError, ValueError):
        return None


def _str(tag):
    if tag is None:
        return None
    s = str(tag).strip().strip("\x00").strip()
    return s or None


def _gps(tags, coord: str, ref: str):
    tag = tags.get(f"GPS {coord}")
    if not tag:
        return None
    try:
        vals = [float(v.num) / float(v.den) if hasattr(v, "den") else float(v) for v in tag.values]
        value = vals[0] + vals[1] / 60 + vals[2] / 3600
    except (IndexError, ZeroDivisionError, TypeError, ValueError):
        return None
    if str(tags.get(f"GPS {ref}", "")).strip().upper() in ("S", "W"):
        value = -value
    return round(value, 7)


def read_exif(path: Path) -> dict:
    """Camera, lens, settings, capture time and GPS. Missing values are None."""
    tags = {}
    try:
        with open(path, "rb") as fh:
            tags = exifread.process_file(fh, details=False, extract_thumbnail=False)
    except Exception:
        tags = {}
    if not tags:
        # fallback (e.g. some HEIF files): let Pillow find the EXIF block, then parse it
        try:
            with Image.open(path) as img:
                raw = img.info.get("exif") or b""
            if raw.startswith(b"Exif\x00\x00"):
                raw = raw[6:]
            if raw:
                tags = exifread.process_file(io.BytesIO(raw), details=False, extract_thumbnail=False)
        except Exception:
            logger.warning("EXIF could not be read: %s", path)
    exposure = _str(tags.get("EXIF ExposureTime"))
    taken = None
    for key in ("EXIF DateTimeOriginal", "Image DateTime"):
        raw = _str(tags.get(key))
        if raw:
            try:
                taken = datetime.strptime(raw[:19], "%Y:%m:%d %H:%M:%S")
                break
            except ValueError:
                pass
    iso = _num(tags.get("EXIF ISOSpeedRatings")) or _num(tags.get("EXIF PhotographicSensitivity"))
    focal = _num(tags.get("EXIF FocalLength"))
    aperture = _num(tags.get("EXIF FNumber"))
    return {
        "camera_make": _str(tags.get("Image Make")),
        "camera_model": _str(tags.get("Image Model")),
        "lens": _str(tags.get("EXIF LensModel")) or _str(tags.get("MakerNote LensModel")),
        "focal_length": round(focal, 1) if focal else None,
        "aperture": round(aperture, 1) if aperture else None,
        "exposure": exposure,
        "iso": int(iso) if iso else None,
        "taken_at": taken,
        "gps_lat": _gps(tags, "GPSLatitude", "GPSLatitudeRef"),
        "gps_lon": _gps(tags, "GPSLongitude", "GPSLongitudeRef"),
    }


# ---- RAW previews -----------------------------------------------------------------

def _tiff_previews(path: Path) -> tuple[list[tuple[int, int]], int]:
    """Embedded JPEG previews (offset, length) of a TIFF-based RAW file (e.g. Sony ARW)
    and the orientation of IFD0."""
    previews: list[tuple[int, int]] = []
    orientation = 1
    with open(path, "rb") as fh:
        header = fh.read(8)
        if len(header) < 8 or header[:2] not in (b"II", b"MM"):
            return previews, orientation
        endian = "<" if header[:2] == b"II" else ">"
        first = struct.unpack(endian + "I", header[4:8])[0]
        queue, seen = [(first, True)], set()
        while queue and len(seen) < 32:
            offset, is_ifd0 = queue.pop(0)
            if not offset or offset in seen:
                continue
            seen.add(offset)
            fh.seek(offset)
            raw = fh.read(2)
            if len(raw) < 2:
                continue
            count = struct.unpack(endian + "H", raw)[0]
            if count > 1000:
                continue
            entries = fh.read(12 * count)
            jpeg_off = jpeg_len = None
            for i in range(count):
                tag, typ, cnt = struct.unpack(endian + "HHI", entries[i * 12:i * 12 + 8])
                value_bytes = entries[i * 12 + 8:i * 12 + 12]
                if typ == 3:
                    value = struct.unpack(endian + "H", value_bytes[:2])[0]
                else:
                    value = struct.unpack(endian + "I", value_bytes)[0]
                if tag == 0x0201:
                    jpeg_off = value
                elif tag == 0x0202:
                    jpeg_len = value
                elif tag == 0x0112 and is_ifd0:
                    orientation = value
                elif tag == 0x014A:
                    if cnt == 1:
                        queue.append((value, False))
                    else:
                        pos = fh.tell()
                        fh.seek(value)
                        subs = struct.unpack(endian + "I" * cnt, fh.read(4 * cnt))
                        fh.seek(pos)
                        queue.extend((s, False) for s in subs)
            nxt = fh.read(4)
            if len(nxt) == 4:
                queue.append((struct.unpack(endian + "I", nxt)[0], False))
            if jpeg_off and jpeg_len:
                previews.append((jpeg_off, jpeg_len))
    return previews, orientation


_ORIENT_OPS = {
    2: [Image.Transpose.FLIP_LEFT_RIGHT],
    3: [Image.Transpose.ROTATE_180],
    4: [Image.Transpose.FLIP_TOP_BOTTOM],
    5: [Image.Transpose.TRANSPOSE],
    6: [Image.Transpose.ROTATE_270],
    7: [Image.Transpose.TRANSVERSE],
    8: [Image.Transpose.ROTATE_90],
}


def open_image(path: Path) -> Image.Image:
    """Opens any supported photo as an upright RGB image."""
    if path.suffix.lower() == ".arw":
        previews, orientation = _tiff_previews(path)
        for off, length in sorted(previews, key=lambda p: p[1], reverse=True):
            with open(path, "rb") as fh:
                fh.seek(off)
                data = fh.read(length)
            if data[:2] != b"\xff\xd8":
                continue
            try:
                img = Image.open(io.BytesIO(data))
                img.load()
            except Exception:
                continue
            for op in _ORIENT_OPS.get(orientation, []):
                img = img.transpose(op)
            return img.convert("RGB")
        raise ValueError("no embedded preview found")
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    return img


def thumb_path(photo_id: int, size: str) -> Path:
    return THUMB_DIR / str(photo_id // 1000) / f"{photo_id}_{size}.jpg"


def make_thumbnails(photo_id: int, source: Path) -> tuple[int, int] | None:
    """Creates all thumbnail sizes. Returns the image size of the source (or preview)."""
    try:
        img = open_image(source)
    except Exception:
        logger.warning("Thumbnail failed: %s", source)
        return None
    width, height = img.size
    for size, px in THUMB_SIZES.items():
        target = thumb_path(photo_id, size)
        target.parent.mkdir(parents=True, exist_ok=True)
        copy = img.copy()
        copy.thumbnail((px, px), Image.Resampling.LANCZOS)
        copy.convert("RGB").save(target, "JPEG", quality=85, optimize=True)
    return width, height


def ensure_thumbnail(photo, size: str) -> Path | None:
    target = thumb_path(photo.id, size)
    if target.exists():
        return target
    try:
        source = safe_join(photo.path)
    except ValueError:
        return None
    if not source.exists():
        return None
    make_thumbnails(photo.id, source)
    return target if target.exists() else None


def delete_thumbnails(photo_id: int):
    for size in THUMB_SIZES:
        thumb_path(photo_id, size).unlink(missing_ok=True)


# ---- Geo ------------------------------------------------------------------------------

def distance_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def nearest_spot(spots, lat, lon, max_km: float = 3.0):
    best, best_d = None, max_km
    for spot in spots:
        d = distance_km(lat, lon, spot.lat, spot.lon)
        if d <= best_d:
            best, best_d = spot, d
    return best
