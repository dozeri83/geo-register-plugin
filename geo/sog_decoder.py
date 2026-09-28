"""Decode PlayCanvas SOG v2 units into raw 3DGS parameter arrays.

A SOG unit is a folder with meta.json plus WebP textures (one pixel per
splat, row-major). Decoding follows the SOG v2 spec and mirrors LichtFeld
Studio's sogs.cpp:

  means   : 16-bit per axis split over means_l/means_u, lerp(mins, maxs),
            then the symmetric log transform is undone: sign(n)*(exp|n|-1)
  scales  : codebook indices; the codebook is already log-scale
  quats   : smallest-three; A-252 names the omitted component (w,x,y,z order),
            kept components are (v/255-0.5)*sqrt(2)
  sh0     : RGB codebook indices -> raw DC coefficients; A = sigmoid(opacity)*255
            (alpha <= 1 encodes opacity 0)
  shN     : optional palette (centroids) + 16-bit labels, band-major codebook

Returned arrays match what spz_encode.encode_spz_v3 expects.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SH_COEFFS = (0, 3, 8, 15)
_SQRT2 = np.float32(np.sqrt(2.0))


@dataclass
class SogSplats:
    positions: np.ndarray       # (N, 3) float32
    rotations_wxyz: np.ndarray  # (N, 4) float32, normalised
    scales_log: np.ndarray      # (N, 3) float32
    opacity_logit: np.ndarray   # (N,)   float32
    f_dc: np.ndarray            # (N, 3) float32
    f_rest_rgb: np.ndarray | None  # (N, K, 3) float32 or None
    sh_degree: int

    def __len__(self) -> int:
        return len(self.positions)

    def take(self, start: int, count: int) -> "SogSplats":
        sl = slice(start, start + count)
        return SogSplats(
            self.positions[sl], self.rotations_wxyz[sl], self.scales_log[sl],
            self.opacity_logit[sl], self.f_dc[sl],
            None if self.f_rest_rgb is None else self.f_rest_rgb[sl],
            self.sh_degree,
        )

    @staticmethod
    def concat(parts: list["SogSplats"]) -> "SogSplats":
        degree = min(p.sh_degree for p in parts)
        rest = None
        if degree > 0:
            k = SH_COEFFS[degree]
            rest = np.concatenate([p.f_rest_rgb[:, :k] for p in parts])
        return SogSplats(
            np.concatenate([p.positions for p in parts]),
            np.concatenate([p.rotations_wxyz for p in parts]),
            np.concatenate([p.scales_log for p in parts]),
            np.concatenate([p.opacity_logit for p in parts]),
            np.concatenate([p.f_dc for p in parts]),
            rest, degree,
        )


def _texture(folder: Path, name: str, count: int) -> np.ndarray:
    """First `count` pixels of a WebP texture as (count, 4) uint8, row-major."""
    from PIL import Image

    with Image.open(folder / name) as img:
        rgba = np.asarray(img.convert("RGBA"), dtype=np.uint8).reshape(-1, 4)
    if len(rgba) < count:
        raise ValueError(f"{folder / name}: {len(rgba)} pixels < {count} splats")
    return rgba[:count]


def _unlog(n: np.ndarray) -> np.ndarray:
    return np.sign(n) * np.expm1(np.abs(n))


def _decode_quats(q: np.ndarray) -> np.ndarray:
    """Smallest-three RGBA -> (N, 4) wxyz."""
    kept = (q[:, :3].astype(np.float32) / 255.0 - 0.5) * _SQRT2
    largest = np.sqrt(np.clip(1.0 - np.sum(kept * kept, axis=1), 0.0, 1.0))
    mode = q[:, 3].astype(np.int64) - 252
    if np.any((mode < 0) | (mode > 3)):
        raise ValueError("quats.webp alpha outside 252..255")
    out = np.empty((len(q), 4), dtype=np.float32)
    # Kept components stay in w,x,y,z order with the omitted one skipped.
    for m in range(4):
        rows = mode == m
        slots = [i for i in range(4) if i != m]
        out[rows, m] = largest[rows]
        for k, slot in enumerate(slots):
            out[rows, slot] = kept[rows, k]
    out /= np.maximum(np.linalg.norm(out, axis=1, keepdims=True), 1e-12)
    return out


def read_sog_unit(meta_path: str | Path) -> SogSplats:
    """Decode one SOG unit given its meta.json path."""
    meta_path = Path(meta_path)
    folder = meta_path.parent
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if meta.get("version") != 2:
        raise ValueError(f"{meta_path}: only SOG version 2 is supported")
    n = int(meta["count"])

    mf = meta["means"]["files"]
    lo, hi = _texture(folder, mf[0], n), _texture(folder, mf[1], n)
    q16 = (lo[:, :3].astype(np.uint32) | (hi[:, :3].astype(np.uint32) << 8)).astype(np.float64)
    mins = np.asarray(meta["means"]["mins"], dtype=np.float64)
    maxs = np.asarray(meta["means"]["maxs"], dtype=np.float64)
    positions = _unlog(mins + (maxs - mins) * (q16 / 65535.0)).astype(np.float32)

    scale_cb = np.asarray(meta["scales"]["codebook"], dtype=np.float32)
    scales_log = scale_cb[_texture(folder, meta["scales"]["files"][0], n)[:, :3]]

    rotations = _decode_quats(_texture(folder, meta["quats"]["files"][0], n))

    sh0 = _texture(folder, meta["sh0"]["files"][0], n)
    sh0_cb = np.asarray(meta["sh0"]["codebook"], dtype=np.float32)
    f_dc = sh0_cb[sh0[:, :3]]
    alpha = sh0[:, 3].astype(np.float32)
    opacity = np.where(alpha <= 1.0, 1e-5, alpha / 255.0)
    opacity = np.clip(opacity, 1e-5, 1.0 - 1e-5)
    opacity_logit = np.log(opacity / (1.0 - opacity)).astype(np.float32)

    f_rest, degree = None, 0
    if "shN" in meta:
        sh = meta["shN"]
        degree = int(sh.get("bands", 0))
        if degree > 0:
            k = SH_COEFFS[degree]
            palette = int(sh.get("count", sh.get("palette_size", 0)))
            cb = np.asarray(sh["codebook"], dtype=np.float32)
            centroids = _texture(folder, sh["files"][0], palette * k)[:, :3]   # (palette*k, 3)
            centroids = cb[centroids].reshape(palette, k, 3)                  # [label, coef, rgb]
            labels_px = _texture(folder, sh["files"][1], n)
            labels = labels_px[:, 0].astype(np.int64) | (labels_px[:, 1].astype(np.int64) << 8)
            f_rest = np.zeros((n, k, 3), dtype=np.float32)
            valid = labels < palette
            f_rest[valid] = centroids[labels[valid]]

    return SogSplats(positions, rotations, scales_log, opacity_logit, f_dc, f_rest, degree)
