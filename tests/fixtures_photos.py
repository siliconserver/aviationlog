"""Creates small test photos: a JPEG with EXIF and a minimal TIFF-based RAW like Sony ARW."""
import io
import struct

from PIL import Image, ImageDraw
from PIL.TiffImagePlugin import IFDRational as R


def _dms(value):
    value = abs(value)
    d = int(value)
    m = int((value - d) * 60)
    s = round(((value - d) * 60 - m) * 60 * 100)
    return (R(d, 1), R(m, 1), R(s, 100))


def jpeg_with_exif(path, date="2026:06:05 14:30:00", gps=(50.0405, 8.5862), color=(90, 140, 200),
                   size=(1200, 800)):
    img = Image.new("RGB", size, color)
    ImageDraw.Draw(img).rectangle([300, 350, 900, 450], fill=(240, 240, 240))
    exif = Image.Exif()
    exif[0x010F] = "SONY"
    exif[0x0110] = "ILCE-7RM5"
    exif[0x0112] = 1
    sub = exif.get_ifd(0x8769)
    sub[0xA434] = "FE 100-400mm F4.5-5.6 GM OSS"
    sub[0x829A] = R(1, 1000)
    sub[0x829D] = R(56, 10)
    sub[0x8827] = 200
    sub[0x9003] = date
    sub[0x920A] = R(400, 1)
    if gps:
        g = exif.get_ifd(0x8825)
        g[1] = "N"
        g[2] = _dms(gps[0])
        g[3] = "E"
        g[4] = _dms(gps[1])
    img.save(path, "JPEG", exif=exif.tobytes(), quality=80)


def fake_arw(path, preview_color=(200, 60, 60), orientation=6):
    """Minimal TIFF: IFD0 with orientation and an embedded JPEG preview."""
    buf = io.BytesIO()
    Image.new("RGB", (640, 427), preview_color).save(buf, "JPEG")
    jpg = buf.getvalue()
    ifd_off = 8
    count = 3
    jpg_off = ifd_off + 2 + 12 * count + 4
    entries = [(0x0112, 3, 1, orientation), (0x0201, 4, 1, jpg_off), (0x0202, 4, 1, len(jpg))]
    out = b"II*\x00" + struct.pack("<I", ifd_off) + struct.pack("<H", count)
    for tag, typ, cnt, val in entries:
        packed = struct.pack("<HH", val, 0) if typ == 3 else struct.pack("<I", val)
        out += struct.pack("<HHI", tag, typ, cnt) + packed
    out += struct.pack("<I", 0) + jpg
    with open(path, "wb") as fh:
        fh.write(out)
