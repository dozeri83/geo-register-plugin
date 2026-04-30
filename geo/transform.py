"""Robust similarity transform estimation.

Pipeline:
  1. RANSAC  – identifies inlier set, rejects hard outliers
  2. IRLS    – refines on inliers using Huber weights (soft residuals)
  3. Fallback plain Umeyama available for use on clean data

All distance units are in whatever unit the dst points are expressed in
(metres for ECEF).
"""
import math
import random

import numpy as np


# ── Core weighted Umeyama ─────────────────────────────────────────────────────

def umeyama(
    src: list,
    dst: list,
    weights: list | None = None,
) -> dict:
    """Weighted closed-form Umeyama (1991):  dst ≈ s·R·src + t.

    weights: per-point non-negative weights (need not be normalised).
             None → uniform weights.
    Returns dict: s, R (3×3), t (3,), rmse, n, residuals (per-point).
    """
    n = len(src)
    if n < 3:
        raise ValueError(f"Need ≥ 3 correspondences, got {n}.")

    P = np.array(src, dtype=float)
    Q = np.array(dst, dtype=float)

    w = np.ones(n) if weights is None else np.asarray(weights, dtype=float)
    w_sum = w.sum()
    if w_sum < 1e-12:
        raise ValueError("All weights are zero.")
    w_norm = w / w_sum                  # normalised so they sum to 1

    mu_p = (w_norm[:, None] * P).sum(axis=0)
    mu_q = (w_norm[:, None] * Q).sum(axis=0)

    P_c = P - mu_p
    Q_c = Q - mu_q

    var_p = float((w_norm * (P_c ** 2).sum(axis=1)).sum())
    if var_p < 1e-12:
        raise ValueError("Source points are coincident — cannot estimate scale.")

    cov = (Q_c * w_norm[:, None]).T @ P_c   # (3,3)

    U, D_vec, Vt = np.linalg.svd(cov)

    S = np.eye(3)
    if np.linalg.det(U) * np.linalg.det(Vt) < 0:
        S[2, 2] = -1.0                  # prevent reflection

    R = U @ S @ Vt
    s = float(np.dot(D_vec, np.diag(S)) / var_p)
    t = (mu_q - s * (R @ mu_p)).tolist()

    P_hat = (s * (R @ P.T)).T + np.array(t)
    residuals = np.sqrt(((Q - P_hat) ** 2).sum(axis=1))
    rmse = float(np.sqrt((w_norm * residuals ** 2).sum()))

    return {
        "s": s,
        "R": R.tolist(),
        "t": t,
        "rmse": rmse,
        "n": n,
        "residuals": residuals.tolist(),
    }


# ── RANSAC ────────────────────────────────────────────────────────────────────

def ransac_umeyama(
    src: list,
    dst: list,
    *,
    inlier_thr: float = 5.0,
    confidence: float = 0.99,
    max_iter: int = 2000,
    min_sample: int = 3,
    seed: int = 42,
) -> dict:
    """RANSAC wrapper around Umeyama.

    inlier_thr  : residual threshold (same unit as dst, e.g. metres for ECEF).
    confidence  : desired probability of finding the correct model.
    max_iter    : hard cap on iterations (also updated adaptively).

    Extra keys in returned dict:
      inlier_mask  – bool list, length = len(src)
      n_inliers    – number of inliers after final refit
      n_total      – total number of input correspondences
    """
    n = len(src)
    if n < min_sample:
        raise ValueError(f"Need ≥ {min_sample} correspondences, got {n}.")

    P_all = np.array(src, dtype=float)
    Q_all = np.array(dst, dtype=float)

    best_mask = np.zeros(n, dtype=bool)
    best_count = 0
    rng = random.Random(seed)
    indices = list(range(n))
    iters_done = 0
    adaptive_max = max_iter

    for _ in range(max_iter):
        iters_done += 1
        sample = rng.sample(indices, min_sample)
        try:
            res = umeyama([src[k] for k in sample], [dst[k] for k in sample])
        except Exception:
            continue

        s = res["s"]
        R = np.array(res["R"])
        t = np.array(res["t"])

        P_hat = (s * (R @ P_all.T)).T + t
        residuals = np.sqrt(((Q_all - P_hat) ** 2).sum(axis=1))
        mask = residuals < inlier_thr
        count = int(mask.sum())

        if count > best_count:
            best_count = count
            best_mask = mask.copy()

            # Adaptive iteration count (Hartley & Zisserman §4.7.1)
            p_in = count / n
            if 0 < p_in < 1:
                denom = math.log(max(1.0 - p_in ** min_sample, 1e-12))
                adaptive_max = min(
                    max_iter,
                    max(min_sample, int(math.ceil(math.log(1.0 - confidence) / denom))),
                )
            if iters_done >= adaptive_max:
                break

    if best_count < min_sample:
        raise ValueError(
            f"RANSAC: no consistent model found with ≥ {min_sample} inliers "
            f"at threshold {inlier_thr:.1f} m. "
            "Try increasing inlier_thr or check your GPS/camera data."
        )

    # Refit on all inliers (uniform weights for now; IRLS refines further)
    inlier_idx = np.where(best_mask)[0].tolist()
    result = umeyama(
        [src[i] for i in inlier_idx],
        [dst[i] for i in inlier_idx],
    )
    result["inlier_mask"] = best_mask.tolist()
    result["n_inliers"] = int(best_mask.sum())
    result["n_total"] = n
    return result


# ── IRLS / Huber refinement ───────────────────────────────────────────────────

def irls_umeyama(
    src: list,
    dst: list,
    *,
    huber_delta: float = 2.0,
    max_iter: int = 50,
    tol: float = 1e-8,
) -> dict:
    """Iteratively Reweighted Least Squares with Huber loss around Umeyama.

    huber_delta : residual (metres) below which a point is treated as inlier.
    Converges when RMSE change < tol.
    """
    n = len(src)
    weights = np.ones(n)
    result = None
    prev_rmse = float("inf")

    for _ in range(max_iter):
        result = umeyama(src, dst, weights=weights.tolist())
        residuals = np.array(result["residuals"])

        # Huber weights: 1 for |r| ≤ δ,  δ/|r| otherwise
        w = np.where(
            residuals <= huber_delta,
            1.0,
            huber_delta / np.maximum(residuals, 1e-10),
        )
        w /= w.sum()
        weights = w

        if abs(prev_rmse - result["rmse"]) < tol:
            break
        prev_rmse = result["rmse"]

    return result   # type: ignore[return-value]


# ── Combined robust estimator (RANSAC → IRLS) ────────────────────────────────

def robust_umeyama(
    src: list,
    dst: list,
    *,
    inlier_thr: float = 5.0,
    confidence: float = 0.99,
    max_ransac_iter: int = 2000,
    huber_delta: float = 2.0,
    max_irls_iter: int = 50,
) -> dict:
    """Two-stage robust estimator:
      1. RANSAC  – hard rejection of gross outliers.
      2. IRLS    – Huber-weighted refinement on the inlier set.

    Returns the IRLS result dict plus RANSAC bookkeeping keys:
      inlier_mask, n_inliers, n_total.
    """
    # Stage 1 – RANSAC
    ransac_result = ransac_umeyama(
        src, dst,
        inlier_thr=inlier_thr,
        confidence=confidence,
        max_iter=max_ransac_iter,
    )

    inlier_mask: list[bool] = ransac_result["inlier_mask"]
    n_inliers: int          = ransac_result["n_inliers"]
    n_total: int            = ransac_result["n_total"]

    inlier_src = [s for s, m in zip(src, inlier_mask) if m]
    inlier_dst = [d for d, m in zip(dst, inlier_mask) if m]

    # Stage 2 – IRLS on inliers
    irls_result = irls_umeyama(
        inlier_src, inlier_dst,
        huber_delta=huber_delta,
        max_iter=max_irls_iter,
    )

    # Propagate RANSAC metadata
    irls_result["inlier_mask"] = inlier_mask
    irls_result["n_inliers"]   = n_inliers
    irls_result["n_total"]     = n_total

    return irls_result


# ── Matrix utilities ──────────────────────────────────────────────────────────

def to_4x4_col_major(s: float, R: list, t: list) -> list[float]:
    """Build a column-major 4×4 similarity matrix for lf.set_node_transform.

      | s·R  t |
      |  0   1 |
    """
    M = np.eye(4)
    M[:3, :3] = s * np.array(R)
    M[:3,  3] = np.array(t)
    return M.flatten(order="F").tolist()


def compose_col_major(G_col: list[float], M_col: list[float]) -> list[float]:
    """Return the column-major product G @ M (both inputs are column-major 4×4)."""
    G = np.array(G_col).reshape(4, 4, order="F")
    M = np.array(M_col).reshape(4, 4, order="F")
    return (G @ M).flatten(order="F").tolist()
