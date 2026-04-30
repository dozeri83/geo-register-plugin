"""WGS-84 geodetic to ECEF conversion."""
import math

_A  = 6_378_137.0           # semi-major axis (m)
_F  = 1 / 298.257223563     # flattening
_B  = _A * (1 - _F)         # semi-minor axis
_E2 = 1 - (_B / _A) ** 2   # first eccentricity squared


def geodetic_to_ecef(lat_deg: float, lon_deg: float, alt_m: float) -> tuple[float, float, float]:
    """Return ECEF (X, Y, Z) in metres for a WGS-84 (lat, lon, alt) position."""
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    N = _A / math.sqrt(1 - _E2 * math.sin(lat) ** 2)
    x = (N + alt_m) * math.cos(lat) * math.cos(lon)
    y = (N + alt_m) * math.cos(lat) * math.sin(lon)
    z = (N * (1 - _E2) + alt_m) * math.sin(lat)
    return x, y, z


def ecef_to_geodetic(x: float, y: float, z: float) -> tuple[float, float, float]:
    """Return WGS-84 (lat_deg, lon_deg, alt_m) for an ECEF (X, Y, Z) position.

    Uses Bowring's iterative method — converges to sub-nanometre accuracy in
    fewer than 5 iterations for all terrestrial positions.
    """
    lon = math.atan2(y, x)
    p   = math.sqrt(x * x + y * y)

    # Seed estimate
    lat = math.atan2(z, p * (1.0 - _E2))

    for _ in range(10):
        N       = _A / math.sqrt(1.0 - _E2 * math.sin(lat) ** 2)
        lat_new = math.atan2(z + _E2 * N * math.sin(lat), p)
        if abs(lat_new - lat) < 1e-12:
            lat = lat_new
            break
        lat = lat_new

    N       = _A / math.sqrt(1.0 - _E2 * math.sin(lat) ** 2)
    cos_lat = math.cos(lat)
    alt     = (p / cos_lat - N) if abs(cos_lat) > 1e-10 else (abs(z) / math.sin(lat) - N * (1.0 - _E2))

    return math.degrees(lat), math.degrees(lon), alt
