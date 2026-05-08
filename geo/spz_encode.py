"""Pure-Python SPZ v3 encoder.

Mirrors Niantic's `load-spz.cc` (https://github.com/nianticlabs/spz, MIT)
byte-for-byte for the legacy v3 path that CesiumJS 1.135's `@spz-loader/core`
decodes via `KHR_gaussian_splatting_compression_spz_2`.

Layout:
    16-byte header:
        u32 magic = 0x5053474E ('NGSP' little-endian)
        u32 version = 3
        u32 num_points
        u8  sh_degree
        u8  fractional_bits = 12
        u8  flags (bit 0 = antialiased)
        u8  reserved
    Streams (concatenated, then gzipped):
        positions: 9 bytes/pt  (3 axes * 24-bit fixed-point, fractional_bits=12)
        alphas:    1 byte/pt   (sigmoid(opacity) * 255)
        colors:    3 bytes/pt  (f_dc * 0.15 * 255 + 127.5)
        scales:    3 bytes/pt  ((scale_log + 10) * 16)
        rotations: 4 bytes/pt  (smallest-three quaternion in u32)
        sh:        sh_dim*3 bytes/pt   (per-coef RGB, quantizeSH-encoded)
"""

import gzip
import struct
import numpy as np


NGSP_MAGIC = 0x5053474E
SPZ_VERSION = 3
COLOR_SCALE = 0.15
SQRT1_2 = 0.7071067811865476
FRACTIONAL_BITS = 12

DIM_FOR_DEGREE = {0: 0, 1: 3, 2: 8, 3: 15}


def _quantize_sh(x: np.ndarray, bucket_size: int) -> np.ndarray:
    """Match Niantic quantizeSH: q = round(x*128) + 128, bucket-round, clamp 0-255."""
    q = np.round(x.astype(np.float64) * 128.0) + 128.0
    q = q.astype(np.int64)
    q = (q + bucket_size // 2) // bucket_size * bucket_size
    return np.clip(q, 0, 255).astype(np.uint8)


def _pack_quat_smallest_three(quats_xyzw: np.ndarray) -> np.ndarray:
    """Pack normalized quaternions (N, 4) xyzw -> (N, 4) uint8 SPZ smallest-three."""
    n = len(quats_xyzw)
    q = quats_xyzw.astype(np.float64)
    norms = np.linalg.norm(q, axis=1, keepdims=True)
    norms = np.where(norms > 0, norms, 1.0)
    q = q / norms

    abs_q = np.abs(q)
    i_largest = np.argmax(abs_q, axis=1).astype(np.uint32)

    largest_val = np.take_along_axis(q, i_largest[:, None].astype(np.int64), axis=1).ravel()
    negate = (largest_val < 0).astype(np.uint32)

    comp = i_largest.copy()  # bits start at 0..1, will shift left as we go
    for i in range(4):
        neg_bit = ((q[:, i] < 0).astype(np.uint32) ^ negate)
        mag = (511.0 * np.abs(q[:, i]) / SQRT1_2 + 0.5).astype(np.int64)
        mag = np.clip(mag, 0, 511).astype(np.uint32)
        new_val = (comp << np.uint32(10)) | (neg_bit << np.uint32(9)) | mag
        # Skip the largest component — keep comp unchanged for those rows.
        mask = (i_largest != i)
        comp = np.where(mask, new_val, comp)

    out = np.empty((n, 4), dtype=np.uint8)
    out[:, 0] = (comp >> 0) & 0xFF
    out[:, 1] = (comp >> 8) & 0xFF
    out[:, 2] = (comp >> 16) & 0xFF
    out[:, 3] = (comp >> 24) & 0xFF
    return out


def encode_spz_v3(
    positions: np.ndarray,
    rotations_xyzw: np.ndarray,
    scales_log: np.ndarray,
    opacity_logit: np.ndarray,
    f_dc: np.ndarray,
    f_rest_rgb: np.ndarray | None = None,
    sh_degree: int = 0,
    sh1_bits: int = 5,
    sh_rest_bits: int = 4,
    antialiased: bool = False,
    fractional_bits: int = FRACTIONAL_BITS,
) -> bytes:
    """Encode an SPZ v3 binary blob.

    positions     (N, 3) float, world units (after any coord-system flips applied by caller)
    rotations_xyzw(N, 4) float, glTF/SPZ xyzw order; will be normalized
    scales_log    (N, 3) float, log-scale (raw 3DGS values, no exp applied)
    opacity_logit (N,)   float, raw 3DGS opacity (sigmoid will be applied internally)
    f_dc          (N, 3) float, raw SH degree-0 RGB coefficients
    f_rest_rgb    (N, K, 3) float, where K is dim for sh_degree (3, 8, or 15);
                  per-coef RGB triples. None means no higher SH.
    """
    n = int(len(positions))
    if rotations_xyzw.shape != (n, 4):
        raise ValueError("rotations must be (N, 4)")
    if scales_log.shape != (n, 3):
        raise ValueError("scales must be (N, 3)")
    if opacity_logit.shape != (n,):
        raise ValueError("opacity must be (N,)")
    if f_dc.shape != (n, 3):
        raise ValueError("f_dc must be (N, 3)")

    flags = 0x01 if antialiased else 0x00
    header = struct.pack(
        "<III BBBB",
        NGSP_MAGIC, SPZ_VERSION, n,
        sh_degree, fractional_bits, flags, 0,
    )

    # Positions: 24-bit fixed-point, little-endian per axis.
    scale_factor = float(1 << fractional_bits)
    pos_fixed = np.round(positions.astype(np.float64) * scale_factor).astype(np.int64)
    pos_bytes = np.empty((n, 3, 3), dtype=np.uint8)
    pos_bytes[:, :, 0] = (pos_fixed >> 0) & 0xFF
    pos_bytes[:, :, 1] = (pos_fixed >> 8) & 0xFF
    pos_bytes[:, :, 2] = (pos_fixed >> 16) & 0xFF
    pos_data = pos_bytes.tobytes(order="C")

    # Alphas: sigmoid(opacity) * 255.
    alpha = 1.0 / (1.0 + np.exp(-opacity_logit.astype(np.float64)))
    alpha_data = np.clip(np.round(alpha * 255.0), 0, 255).astype(np.uint8).tobytes()

    # Colors: SH₀ * colorScale * 255 + 127.5.
    color = f_dc.astype(np.float64) * (COLOR_SCALE * 255.0) + (0.5 * 255.0)
    color_data = np.clip(np.round(color), 0, 255).astype(np.uint8).tobytes()

    # Scales: (log_scale + 10) * 16.
    sc = (scales_log.astype(np.float64) + 10.0) * 16.0
    scales_data = np.clip(np.round(sc), 0, 255).astype(np.uint8).tobytes()

    # Rotations: smallest-three packed.
    rot_data = _pack_quat_smallest_three(rotations_xyzw).tobytes()

    # Spherical harmonics, if any.
    if sh_degree > 0:
        coefs = DIM_FOR_DEGREE[sh_degree]
        if f_rest_rgb is None or f_rest_rgb.shape != (n, coefs, 3):
            raise ValueError(f"f_rest_rgb must have shape ({n}, {coefs}, 3) for sh_degree={sh_degree}")
        sh_packed = np.empty_like(f_rest_rgb, dtype=np.uint8)
        # Degree 1 (first 3 coefs): sh1_bits precision.
        sh_packed[:, 0:3, :] = _quantize_sh(f_rest_rgb[:, 0:3, :], 1 << (8 - sh1_bits))
        # Degrees 2-3 (remaining coefs): sh_rest_bits precision.
        if coefs > 3:
            sh_packed[:, 3:, :] = _quantize_sh(f_rest_rgb[:, 3:, :], 1 << (8 - sh_rest_bits))
        sh_data = sh_packed.tobytes(order="C")
    else:
        sh_data = b""

    payload = header + pos_data + alpha_data + color_data + scales_data + rot_data + sh_data
    return gzip.compress(payload, compresslevel=9, mtime=0)
