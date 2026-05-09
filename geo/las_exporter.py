"""Export a geo-registered Gaussian splat to a LAS 1.4 file.

Supported output coordinate systems (configured via config.json):
  "UTM"  – WGS-84 / UTM zone chosen from the point-cloud median (default)
  "LLA"  – EPSG:4326 geographic (longitude, latitude, ellipsoidal height)
"""
from pathlib import Path

import numpy as np

# ── WGS-84 ellipsoid constants ─────────────────────────────────────────────
_A  = 6_378_137.0
_E2 = 6.6943799901414e-3   # first eccentricity squared

_WGS84_WKT = (
    'GEOGCS["WGS 84",'
    'DATUM["WGS_1984",'
    'SPHEROID["WGS 84",6378137,298.257223563,AUTHORITY["EPSG","7030"]],'
    'AUTHORITY["EPSG","6326"]],'
    'PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],'
    'UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],'
    'AUTHORITY["EPSG","4326"]]'
)

# ── Config ─────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    import json
    cfg_path = Path(__file__).parent.parent / "config.json"
    if cfg_path.exists():
        with open(cfg_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _las_coord_mode() -> str:
    """Return 'UTM' or 'LLA' from config.json (default: 'UTM')."""
    mode = _load_config().get("las_export_coordinates", "UTM").upper()
    if mode not in ("UTM", "LLA"):
        raise ValueError(f"config.json: unsupported las_export_coordinates={mode!r} (use 'UTM' or 'LLA')")
    return mode


# ── Coordinate conversions ─────────────────────────────────────────────────

def _ecef_to_geodetic_batch(
    x: np.ndarray, y: np.ndarray, z: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Vectorized ECEF → WGS-84 (lat_deg, lon_deg, alt_m) via Bowring iteration."""
    lon = np.arctan2(y, x)
    p   = np.sqrt(x * x + y * y)
    lat = np.arctan2(z, p * (1.0 - _E2))

    for _ in range(10):
        sin_lat = np.sin(lat)
        N       = _A / np.sqrt(1.0 - _E2 * sin_lat * sin_lat)
        lat_new = np.arctan2(z + _E2 * N * sin_lat, p)
        delta   = np.abs(lat_new - lat)
        lat     = lat_new
        if delta.max() < 1e-12:
            break

    sin_lat = np.sin(lat)
    cos_lat = np.cos(lat)
    N       = _A / np.sqrt(1.0 - _E2 * sin_lat * sin_lat)
    polar   = np.abs(cos_lat) < 1e-10
    alt     = np.where(polar,
                       np.abs(z) / np.abs(sin_lat) - N * (1.0 - _E2),
                       p / cos_lat - N)

    return np.degrees(lat), np.degrees(lon), alt


def _utm_zone(lat_med: float, lon_med: float) -> tuple[int, bool]:
    """Return (zone_number, northern) for the UTM zone of the median point."""
    zone_number = int((lon_med + 180.0) / 6.0) + 1
    zone_number = max(1, min(60, zone_number))
    northern    = lat_med >= 0.0
    return zone_number, northern


def _geodetic_to_utm_batch(
    lat_deg: np.ndarray, lon_deg: np.ndarray,
    zone_number: int, northern: bool
) -> tuple[np.ndarray, np.ndarray]:
    """Vectorized WGS-84 geodetic → UTM easting/northing (metres).

    Uses the standard Helmert series (accurate to ~1 mm within the zone).
    """
    k0 = 0.9996
    E0 = 500_000.0
    N0 = 0.0 if northern else 10_000_000.0

    lat  = np.radians(lat_deg)
    lon  = np.radians(lon_deg)
    lon0 = np.radians((zone_number - 1) * 6 - 180 + 3)   # central meridian

    e2       = _E2
    e4       = e2 * e2
    e6       = e4 * e2
    e_prime2 = e2 / (1.0 - e2)

    sin_lat = np.sin(lat)
    cos_lat = np.cos(lat)
    tan_lat = np.tan(lat)

    N = _A / np.sqrt(1.0 - e2 * sin_lat * sin_lat)
    T = tan_lat * tan_lat
    C = e_prime2 * cos_lat * cos_lat
    A = cos_lat * (lon - lon0)

    M = _A * (
        (1.0 - e2 / 4.0 - 3.0 * e4 / 64.0 - 5.0 * e6 / 256.0) * lat
        - (3.0 * e2 / 8.0 + 3.0 * e4 / 32.0 + 45.0 * e6 / 1024.0) * np.sin(2.0 * lat)
        + (15.0 * e4 / 256.0 + 45.0 * e6 / 1024.0) * np.sin(4.0 * lat)
        - (35.0 * e6 / 3072.0) * np.sin(6.0 * lat)
    )

    A2 = A * A
    A3 = A2 * A
    A4 = A2 * A2
    A5 = A4 * A
    A6 = A3 * A3

    easting = k0 * N * (
        A
        + (1.0 - T + C) * A3 / 6.0
        + (5.0 - 18.0 * T + T * T + 72.0 * C - 58.0 * e_prime2) * A5 / 120.0
    ) + E0

    northing = k0 * (
        M + N * tan_lat * (
            A2 / 2.0
            + (5.0 - T + 9.0 * C + 4.0 * C * C) * A4 / 24.0
            + (61.0 - 58.0 * T + T * T + 600.0 * C - 330.0 * e_prime2) * A6 / 720.0
        )
    ) + N0

    return easting, northing


def _utm_wkt(zone_number: int, northern: bool) -> str:
    hem      = "N" if northern else "S"
    epsg     = (32600 if northern else 32700) + zone_number
    lon0     = (zone_number - 1) * 6 - 180 + 3
    false_n  = "0" if northern else "10000000"
    return (
        f'PROJCS["WGS 84 / UTM zone {zone_number}{hem}",'
        'GEOGCS["WGS 84",'
        'DATUM["WGS_1984",'
        'SPHEROID["WGS 84",6378137,298.257223563,AUTHORITY["EPSG","7030"]],'
        'AUTHORITY["EPSG","6326"]],'
        'PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],'
        'UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],'
        'AUTHORITY["EPSG","4326"]],'
        'PROJECTION["Transverse_Mercator"],'
        'PARAMETER["latitude_of_origin",0],'
        f'PARAMETER["central_meridian",{lon0}],'
        'PARAMETER["scale_factor",0.9996],'
        'PARAMETER["false_easting",500000],'
        f'PARAMETER["false_northing",{false_n}],'
        'UNIT["metre",1,AUTHORITY["EPSG","9001"]],'
        f'AUTHORITY["EPSG","{epsg}"]]'
    )


# ── LAS helpers ────────────────────────────────────────────────────────────

def _write_las(output_path: str, x, y, z, c_u16, wkt: str) -> None:
    import laspy
    header = laspy.LasHeader(point_format=7, version="1.4")
    header.offsets = np.array([np.median(x), np.median(y), np.median(z)])
    header.scales  = np.array([1e-3, 1e-3, 1e-3])
    header.vlrs.append(laspy.VLR(
        user_id     = "LASF_Projection",
        record_id   = 2112,
        description = "OGC Coordinate System WKT",
        record_data = wkt.encode("utf-8"),
    ))
    las       = laspy.LasData(header=header)
    las.x     = x
    las.y     = y
    las.z     = z
    las.red   = c_u16[:, 0]
    las.green = c_u16[:, 1]
    las.blue  = c_u16[:, 2]
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    compress = Path(output_path).suffix.lower() == ".laz"
    las.write(output_path, do_compress=compress or None)


# ── Public API ─────────────────────────────────────────────────────────────

def export_las(node, transform: dict, output_path: str, progress_cb=None) -> None:
    """Export a SPLAT SceneNode to a LAS 1.4 file.

    Output CRS is read from config.json (las_export_coordinates: UTM | LLA).
    """
    def _prog(f: float) -> None:
        if progress_cb:
            progress_cb(f)

    import laspy  # noqa: F401 – imported here to keep startup cost low

    splat_data = node.splat_data()
    if splat_data is None:
        raise RuntimeError("Selected node has no splat data.")

    _prog(0.05)

    # ── Positions: local → world (viewer) space ───────────────────────────
    means = np.asarray(splat_data.means_raw.cpu().numpy(), dtype=np.float64)
    W     = np.asarray(node.world_transform, dtype=np.float64).reshape(4, 4)
    ones  = np.ones((means.shape[0], 1), dtype=np.float64)
    means_world = (W @ np.hstack([means, ones]).T).T[:, :3]

    _prog(0.15)

    # ── Remove deleted Gaussians ──────────────────────────────────────────
    deleted_raw = np.asarray(splat_data.deleted.cpu().numpy())
    keep = np.ones(means_world.shape[0], dtype=bool) if deleted_raw.ndim == 0 else ~deleted_raw.astype(bool)
    means_world = means_world[keep]

    # ── SH0 → RGB colours ────────────────────────────────────────────────
    colors = np.asarray(splat_data.get_colors_rgb().cpu().numpy(), dtype=np.float32)[keep]
    c_u16  = (np.clip(colors, 0.0, 1.0) * 65535.0).astype(np.uint16)

    _prog(0.25)

    # ── GL correction: dataset world (Y-down, Z-fwd) → viewer world ──────
    means_world[:, 1] *= -1.0
    means_world[:, 2] *= -1.0

    # ── Scene world → ECEF via similarity transform ───────────────────────
    s    = float(transform["s"])
    R    = np.asarray(transform["R"], dtype=np.float64).reshape(3, 3)
    t    = np.asarray(transform["t"], dtype=np.float64)
    ecef = s * (means_world @ R.T) + t

    _prog(0.45)

    # ── ECEF → WGS-84 geodetic ────────────────────────────────────────────
    lats, lons, alts = _ecef_to_geodetic_batch(ecef[:, 0], ecef[:, 1], ecef[:, 2])

    _prog(0.65)

    # ── Choose output CRS and convert ─────────────────────────────────────
    mode = _las_coord_mode()
    if mode == "UTM":
        lat_med, lon_med = float(np.median(lats)), float(np.median(lons))
        zone_number, northern = _utm_zone(lat_med, lon_med)
        eastings, northings = _geodetic_to_utm_batch(lats, lons, zone_number, northern)
        _write_las(output_path, eastings, northings, alts, c_u16, _utm_wkt(zone_number, northern))
    else:
        # LLA: x=longitude, y=latitude, z=altitude (EPSG:4326)
        _write_las(output_path, lons, lats, alts, c_u16, _WGS84_WKT)

    _prog(1.0)


def export_las_from_ply(ply_path: str, transform: dict, output_path: str,
                        progress_cb=None) -> None:
    """Export a 3DGS PLY file to LAS 1.4 using a similarity transform.

    Output CRS is read from config.json (las_export_coordinates: UTM | LLA).
    """
    import re

    def _prog(f):
        if progress_cb:
            progress_cb(f)

    # ── Read PLY ──────────────────────────────────────────────────────────
    with open(ply_path, "rb") as f:
        lines = []
        while True:
            line = f.readline()
            if not line:
                raise ValueError("EOF before end_header in PLY")
            lines.append(line)
            if line.strip() == b"end_header":
                break
        header = b"".join(lines).decode("ascii", errors="replace")
        if "format binary_little_endian" not in header:
            raise ValueError("Only binary_little_endian PLY supported")
        m = re.search(r"element vertex (\d+)", header)
        if not m:
            raise ValueError("Cannot find vertex count in PLY header")
        n = int(m.group(1))
        props = re.findall(r"property\s+(\S+)\s+(\S+)", header)
        if not all(t == "float" for t, _ in props):
            raise ValueError("Only float32 PLY properties supported")
        names = [name for _, name in props]
        dtype = np.dtype([(name, "<f4") for name in names])
        ply = np.fromfile(f, dtype=dtype, count=n)

    _prog(0.15)

    # ── Positions ─────────────────────────────────────────────────────────
    pos = np.stack([ply["x"].astype(np.float64),
                    ply["y"].astype(np.float64),
                    ply["z"].astype(np.float64)], axis=-1)
    pos[:, 1] *= -1.0
    pos[:, 2] *= -1.0

    _prog(0.25)

    # ── Similarity → ECEF ─────────────────────────────────────────────────
    s    = float(transform.get("scale",       transform.get("s")))
    R    = np.array(transform.get("rotation", transform.get("R")), dtype=np.float64).reshape(3, 3)
    t    = np.array(transform.get("translation", transform.get("t")), dtype=np.float64)
    ecef = s * (pos @ R.T) + t

    _prog(0.45)

    # ── ECEF → WGS-84 geodetic ────────────────────────────────────────────
    lats, lons, alts = _ecef_to_geodetic_batch(ecef[:, 0], ecef[:, 1], ecef[:, 2])

    _prog(0.60)

    # ── Colors from SH0 DC term ───────────────────────────────────────────
    SH_C0 = 0.28209479177387814
    if all(f"f_dc_{i}" in names for i in range(3)):
        f_dc = np.stack([ply["f_dc_0"], ply["f_dc_1"], ply["f_dc_2"]], axis=-1).astype(np.float64)
        rgb  = np.clip(f_dc * SH_C0 + 0.5, 0.0, 1.0)
    else:
        rgb = np.full((n, 3), 0.5)
    c_u16 = (rgb * 65535.0).astype(np.uint16)

    _prog(0.75)

    # ── Choose output CRS and convert ─────────────────────────────────────
    mode = _las_coord_mode()
    if mode == "UTM":
        lat_med, lon_med = float(np.median(lats)), float(np.median(lons))
        zone_number, northern = _utm_zone(lat_med, lon_med)
        eastings, northings = _geodetic_to_utm_batch(lats, lons, zone_number, northern)
        _write_las(output_path, eastings, northings, alts, c_u16, _utm_wkt(zone_number, northern))
    else:
        _write_las(output_path, lons, lats, alts, c_u16, _WGS84_WKT)

    _prog(1.0)
