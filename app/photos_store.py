"""Photo processing and storage for the Photos tab.

Every upload becomes three files under MEDIA_DIR/photos/<id[:2]>/<id>/:

  original.jpg  full resolution, full quality, location removed
  display.jpg   2560px long edge, what the big view shows
  thumb.jpg     320px long edge, the filmstrip and the instant placeholder

Location privacy: a phone photo carries GPS coordinates precise enough to
find someone's front door. For JPEGs the GPS block is zeroed in place and
XMP/IPTC (which can repeat the location) are dropped, without re-encoding,
so the original keeps its exact pixels. Anything that isn't a JPEG (HEIC,
PNG, WebP) is re-encoded at quality 95 with no metadata at all. The display
and thumb copies never carry metadata.
"""

import io
import os
import re
import secrets
import shutil
import struct

from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

from .config import MEDIA_DIR

register_heif_opener()
# Pillow refuses anything over twice this as a decompression bomb. 200MP
# still covers every real camera (the biggest are ~100MP).
Image.MAX_IMAGE_PIXELS = 200_000_000

THUMB_EDGE = 320
DISPLAY_EDGE = 2560
SIZES = ("thumb", "display", "original")
EXIF_FIELDS = ("camera", "lens", "aperture", "shutter", "iso", "focal", "film", "taken")
EXIF_FIELD_MAX_CHARS = 120

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16}$")


class PhotoError(Exception):
    """The upload isn't an image we can handle. The message is user-facing."""


def new_photo_id() -> str:
    return secrets.token_urlsafe(12)  # 16 url-safe chars


def valid_photo_id(photo_id: str) -> bool:
    return bool(_ID_RE.match(photo_id))


def photo_dir(photo_id: str) -> str:
    return os.path.join(MEDIA_DIR, "photos", photo_id[:2], photo_id)


def photo_path(photo_id: str, size: str) -> str:
    return os.path.join(photo_dir(photo_id), f"{size}.jpg")


def delete_photo_files(photo_ids) -> None:
    for photo_id in photo_ids:
        if valid_photo_id(photo_id):
            shutil.rmtree(photo_dir(photo_id), ignore_errors=True)


# ── EXIF → display strings ──

def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _clean(value) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", "ignore")
    return str(value or "").replace("\x00", "").strip()[:EXIF_FIELD_MAX_CHARS]


def read_exif(img: Image.Image) -> dict:
    exif = img.getexif()
    sub = exif.get_ifd(0x8769)
    info = dict.fromkeys(EXIF_FIELDS, "")

    make, model = _clean(exif.get(0x010F)), _clean(exif.get(0x0110))
    # "Canon" + "Canon EOS R5" shouldn't read "Canon Canon EOS R5".
    if make and model and make.split()[0].lower() not in model.lower():
        info["camera"] = f"{make} {model}"
    else:
        info["camera"] = model or make
    info["lens"] = _clean(sub.get(0xA434))

    f = _num(sub.get(0x829D))
    if f:
        info["aperture"] = f"f/{round(f, 1):g}"
    t = _num(sub.get(0x829A))
    if t:
        info["shutter"] = f"{round(t, 1):g}s" if t >= 0.3 else f"1/{round(1 / t)}"
    iso = sub.get(0x8827)
    if isinstance(iso, (tuple, list)):
        iso = iso[0] if iso else None
    if _num(iso):
        info["iso"] = str(int(_num(iso)))
    focal, eq = _num(sub.get(0x920A)), _num(sub.get(0xA405))
    # A phone's real focal length (6.8mm) means nothing to anyone; its
    # 35mm-equivalent (24mm) does. Cameras show the real number.
    if focal and eq and eq / focal > 2:
        info["focal"] = f"{round(eq):g}mm"
    elif focal:
        info["focal"] = f"{round(focal, 1):g}mm"
    taken = _clean(sub.get(0x9003))
    if re.match(r"^\d{4}:\d{2}:\d{2} \d{2}:\d{2}:\d{2}$", taken):
        info["taken"] = taken[:10].replace(":", "-") + taken[10:]
    return info


def sanitize_exif(raw) -> dict:
    """Client-edited EXIF from the composer: known keys only, plain strings."""
    if not isinstance(raw, dict):
        return dict.fromkeys(EXIF_FIELDS, "")
    return {k: _clean(raw.get(k, "")) for k in EXIF_FIELDS}


# ── Lossless JPEG location strip ──

_TIFF_TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}


def _scrub_gps(app1_payload: bytes) -> bytes:
    """Zero the GPS IFD inside an Exif APP1 payload, keeping its length, so
    every other offset in the block stays valid. The IFD0 pointer is left
    pointing at what is now an empty IFD, which readers treat as no GPS."""
    buf = bytearray(app1_payload)
    base = 6  # after b"Exif\0\0"
    order = buf[base:base + 2]
    if order == b"II":
        e = "<"
    elif order == b"MM":
        e = ">"
    else:
        raise PhotoError("unreadable EXIF")

    def u16(off):
        return struct.unpack_from(e + "H", buf, base + off)[0]

    def u32(off):
        return struct.unpack_from(e + "I", buf, base + off)[0]

    ifd0 = u32(4)
    for k in range(u16(ifd0)):
        entry = ifd0 + 2 + 12 * k
        if u16(entry) != 0x8825:
            continue
        gps = u32(entry + 8)
        count = u16(gps)
        end = base + gps + 2 + 12 * count + 4
        if end > len(buf):
            raise PhotoError("unreadable EXIF")
        for g in range(count):
            ge = gps + 2 + 12 * g
            size = _TIFF_TYPE_SIZES.get(u16(ge + 2), 1) * u32(ge + 4)
            if size > 4:
                off = base + u32(ge + 8)
                buf[off:off + size] = bytes(min(size, max(0, len(buf) - off)))
        buf[base + gps:end] = bytes(end - base - gps)
    return bytes(buf)


def _find_eoi(data: bytes, j: int) -> int:
    """Index of the end-of-image marker for the first image in the file.
    Phones append more JPEGs after it (HDR gain maps, depth maps), each with
    its own EXIF and GPS, so everything past this point gets cut."""
    n = len(data)
    while True:
        j = data.find(b"\xff", j)
        if j < 0 or j + 1 >= n:
            raise PhotoError("truncated JPEG")
        m = data[j + 1]
        if m == 0x00 or 0xD0 <= m <= 0xD7:
            j += 2
        elif m == 0xFF:
            j += 1
        elif m == 0xD9:
            return j
        else:
            # A marker between scans (progressive JPEGs): skip its segment
            # so bytes inside a table can't be mistaken for markers.
            j += 2 + struct.unpack_from(">H", data, j + 2)[0]


def strip_jpeg_location(data: bytes) -> bytes:
    if data[:2] != b"\xff\xd8":
        raise PhotoError("not a JPEG")
    out = bytearray(b"\xff\xd8")
    i, n = 2, len(data)
    while i + 4 <= n:
        if data[i] != 0xFF:
            raise PhotoError("corrupt JPEG")
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        length = struct.unpack_from(">H", data, i + 2)[0]
        seg_end = i + 2 + length
        payload = data[i + 4:seg_end]
        if marker == 0xDA:  # start of scan: image data runs to the EOI
            eoi = _find_eoi(data, seg_end)
            out += data[i:eoi + 2]
            return bytes(out)
        if marker == 0xE1 and payload.startswith(b"Exif\x00\x00"):
            out += data[i:i + 4] + _scrub_gps(payload)
        elif marker == 0xE1 or marker == 0xED:
            pass  # XMP and IPTC can both repeat the location
        elif marker == 0xE2 and payload.startswith(b"MPF\x00"):
            pass  # indexes the trailing images cut above
        else:
            out += data[i:seg_end]  # includes the ICC colour profile
        i = seg_end
    raise PhotoError("truncated JPEG")


# ── The whole upload ──

def _save_jpeg(img: Image.Image, path: str, quality: int, icc) -> None:
    kwargs = {"quality": quality, "optimize": True, "progressive": True}
    if icc:
        kwargs["icc_profile"] = icc
    img.save(path, "JPEG", **kwargs)


def process_upload(data: bytes) -> dict:
    """Validate, strip and store one upload. Returns
    {id, width, height, exif}; files are on disk before this returns."""
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Image.DecompressionBombError:
        raise PhotoError("that image is too large")
    except Exception:
        raise PhotoError("that file isn't an image we can read")

    exif = read_exif(img)
    icc = img.info.get("icc_profile")

    original = None
    if data[:2] == b"\xff\xd8":
        try:
            original = strip_jpeg_location(data)
            check = Image.open(io.BytesIO(original))
            check.load()  # proves the stripped file still decodes
            if check.getexif().get_ifd(0x8825):
                original = None
            else:
                img = check
        except Exception:
            original = None

    oriented = ImageOps.exif_transpose(img)
    if oriented.mode not in ("RGB", "L"):
        if "A" in oriented.getbands():
            flat = Image.new("RGB", oriented.size, (255, 255, 255))
            flat.paste(oriented, mask=oriented.getchannel("A"))
            oriented = flat
        else:
            oriented = oriented.convert("RGB")

    photo_id = new_photo_id()
    final = photo_dir(photo_id)
    tmp = final + ".partial"
    os.makedirs(tmp, exist_ok=True)
    try:
        if original is not None:
            with open(os.path.join(tmp, "original.jpg"), "wb") as fh:
                fh.write(original)
        else:
            _save_jpeg(oriented, os.path.join(tmp, "original.jpg"), 95, icc)
        display = oriented.copy()
        display.thumbnail((DISPLAY_EDGE, DISPLAY_EDGE), Image.LANCZOS)
        _save_jpeg(display, os.path.join(tmp, "display.jpg"), 88, icc)
        display.thumbnail((THUMB_EDGE, THUMB_EDGE), Image.LANCZOS)
        _save_jpeg(display, os.path.join(tmp, "thumb.jpg"), 80, icc)
        os.rename(tmp, final)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    width, height = oriented.size
    return {"id": photo_id, "width": width, "height": height, "exif": exif}
