"""Export a geo-registered Gaussian splat to a LAS 1.4 file (EPSG:4326)."""
from pathlib import Path

import numpy as np

_A  = 6_378_137.0
_E2 = 6.6943799901414e-3   # first eccentricity squared (WGS-84)

# OGC WKT for EPSG:4326 — embedded directly to avoid a pyproj dependency.
_WGS84_WKT = (
    'GEOGCS["WGS 84",'
    'DATUM["WGS_1984",'
    'SPHEROID["WGS 84",6378137,298.257223563,AUTHORITY["EPSG","7030"]],'
    'AUTHORITY["EPSG","6326"]],'
    'PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],'
    'UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],'
    'AUTHORITY["EPSG","4326"]]'
)


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


def export_las(node, transform: dict, output_path: str, progress_cb=None) -> None:
    """Export a SPLAT SceneNode to a LAS 1.4 file with EPSG:4326 coordinates.

    Parameters
    ----------
    node        : LichtFeld SceneNode (type SPLAT)
    transform   : dict with keys 's' (float), 'R' (3x3 list), 't' (list[3])
                  mapping scene/viewer world coords to ECEF
    output_path : destination .las file path
    progress_cb : optional callable(float 0..1) for progress reporting
    """
    import laspy

    def _prog(f: float) -> None:
        if progress_cb:
            progress_cb(f)

    splat_data = node.splat_data()
    if splat_data is None:
        raise RuntimeError("Selected node has no splat data.")

    _prog(0.05)

    # ── Positions: local → world (viewer) space ───────────────────────────────
    means = np.asarray(splat_data.means_raw.cpu().numpy(), dtype=np.float64)   # [N, 3]
    W     = np.asarray(node.world_transform, dtype=np.float64).reshape(4, 4)   # row-major
    ones  = np.ones((means.shape[0], 1), dtype=np.float64)
    means_h     = np.hstack([means, ones])                  # [N, 4]
    means_world = (W @ means_h.T).T[:, :3]                  # [N, 3] in viewer world

    _prog(0.15)

    # ── Remove deleted Gaussians ──────────────────────────────────────────────
    deleted_raw = np.asarray(splat_data.deleted.cpu().numpy())
    if deleted_raw.ndim == 0:
        keep = np.ones(means_world.shape[0], dtype=bool)   # scalar flag — keep all
    else:
        keep = ~deleted_raw.astype(bool)
    means_world = means_world[keep]

    # ── SH0 → RGB colours ─────────────────────────────────────────────────────
    colors   = np.asarray(splat_data.get_colors_rgb().cpu().numpy(), dtype=np.float32)  # [N, 3]
    colors   = colors[keep]
    c_u16    = (np.clip(colors, 0.0, 1.0) * 65535.0).astype(np.uint16)

    _prog(0.25)

    # ── Apply GL correction: dataset world (Y-down, Z-fwd) → viewer world ─────
    # Matches the diag(1,-1,-1) applied in camera_reader.py when building
    # the similarity transform src_pts.
    means_world[:, 1] *= -1.0
    means_world[:, 2] *= -1.0

    # ── Scene world → ECEF via similarity transform ───────────────────────────
    s = float(transform["s"])
    R = np.asarray(transform["R"], dtype=np.float64).reshape(3, 3)
    t = np.asarray(transform["t"], dtype=np.float64)
    ecef = s * (means_world @ R.T) + t                      # [N, 3]

    _prog(0.45)

    # ── ECEF → WGS-84 geodetic (vectorized) ──────────────────────────────────
    lats, lons, alts = _ecef_to_geodetic_batch(
        ecef[:, 0], ecef[:, 1], ecef[:, 2]
    )

    _prog(0.65)

    # ── Build LAS 1.4 file ────────────────────────────────────────────────────
    header = laspy.LasHeader(point_format=7, version="1.4")
    # Offsets at centroid; scales give ~1 mm precision for lat/lon/alt
    header.offsets = np.array([np.mean(lons), np.mean(lats), np.mean(alts)])
    header.scales  = np.array([1e-8, 1e-8, 1e-3])

    # Embed OGC WKT CRS (VLR record_id 2112 = WKT Coordinate System Record)
    header.vlrs.append(laspy.VLR(
        user_id     = "LASF_Projection",
        record_id   = 2112,
        description = "OGC Coordinate System WKT",
        record_data = _WGS84_WKT.encode("utf-8"),
    ))

    las       = laspy.LasData(header=header)
    las.x     = lons          # EPSG:4326: x = longitude
    las.y     = lats          #            y = latitude
    las.z     = alts          #            z = ellipsoidal height (m)
    las.red   = c_u16[:, 0]
    las.green = c_u16[:, 1]
    las.blue  = c_u16[:, 2]

    _prog(0.85)

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    compress = Path(output_path).suffix.lower() == ".laz"
    las.write(output_path, do_compress=compress or None)

    _prog(1.0)
