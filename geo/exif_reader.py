"""EXIF GPS extraction utilities."""
from pathlib import Path

_IMAGE_EXTS = {".jpg", ".jpeg", ".tif", ".tiff", ".png"}
_SEARCH_SUBDIRS = ["", "images", "input", "photos", "imgs"]


class NoGPSDataError(RuntimeError):
    pass


def find_images_with_gps(root: str) -> list[dict]:
    """
    Recursively scan *root* for images that contain GPS EXIF data.

    Returns a list of dicts: {path, lat, lon, alt}.
    Raises NoGPSDataError when images are found but none carry GPS tags,
    or when no images exist at all.
    """
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS, GPSTAGS
    except ImportError:
        raise RuntimeError(
            "Pillow is required for EXIF reading. "
            "Add 'Pillow>=10.0' to the plugin dependencies."
        )

    root_path = Path(root)
    candidates = _collect_images(root_path)

    if not candidates:
        raise NoGPSDataError(
            f"No image files found under '{root}'. "
            "Make sure the scene path points to a dataset directory."
        )

    results = []
    for p in candidates:
        gps = _extract_gps(p, Image, TAGS, GPSTAGS)
        if gps:
            results.append({"path": str(p), **gps})

    if not results:
        raise NoGPSDataError(
            f"Scanned {len(candidates)} image(s) but none contain GPS EXIF data. "
            "Images must be captured with a GPS-enabled device and retain original EXIF."
        )

    return results


def _collect_images(root: Path) -> list[Path]:
    found = []
    for sub in _SEARCH_SUBDIRS:
        d = root / sub if sub else root
        if not d.is_dir():
            continue
        for p in d.rglob("*"):
            if p.suffix.lower() in _IMAGE_EXTS:
                found.append(p)
    # deduplicate while preserving order
    seen = set()
    unique = []
    for p in found:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


def _extract_gps(path: Path, Image, TAGS, GPSTAGS) -> dict | None:
    try:
        img = Image.open(path)
        raw_exif = img._getexif()
        if not raw_exif:
            return None

        gps_info: dict = {}
        for tag_id, value in raw_exif.items():
            if TAGS.get(tag_id) == "GPSInfo":
                for k, v in value.items():
                    gps_info[GPSTAGS.get(k, k)] = v
                break

        if not gps_info:
            return None

        lat = _dms_to_decimal(gps_info.get("GPSLatitude"), gps_info.get("GPSLatitudeRef", "N"))
        lon = _dms_to_decimal(gps_info.get("GPSLongitude"), gps_info.get("GPSLongitudeRef", "E"))
        if lat is None or lon is None:
            return None

        alt = _rational_to_float(gps_info.get("GPSAltitude")) or 0.0
        return {"lat": lat, "lon": lon, "alt": alt}

    except Exception:
        return None


def _dms_to_decimal(dms, ref: str) -> float | None:
    if dms is None:
        return None
    try:
        d, m, s = float(dms[0]), float(dms[1]), float(dms[2])
        decimal = d + m / 60.0 + s / 3600.0
        if ref in ("S", "W"):
            decimal = -decimal
        return decimal
    except Exception:
        return None


def _rational_to_float(val) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except Exception:
        return None
