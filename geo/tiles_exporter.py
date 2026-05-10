"""Export a geo-registered Gaussian splat to 3D Tiles 1.1 (SPZ-compressed).

Tested on ArcGIS Maps SDK 5.0. Should also work on CesiumJS ≥ 1.139 and
ArcGIS Pro ≥ 3.6.

Can be used as a library (import export_3dtiles) or as a CLI:

    python tiles_exporter.py <in.ply> <similarity.json> <out_dir> [--max-sh-degree 3]
"""

from pathlib import Path
import json
import re
import struct

import numpy as np

from .spz_encode import encode_spz_v3, DIM_FOR_DEGREE

SH_C0 = 0.28209479177387814  # Y_{0,0}

GLB_MAGIC  = 0x46546C67
CHUNK_JSON = 0x4E4F534A
CHUNK_BIN  = 0x004E4942


# ─── Math helpers ─────────────────────────────────────────────────────────────

def _mat3_to_quat_wxyz(R: np.ndarray) -> np.ndarray:
    """3×3 rotation matrix → quaternion (w, x, y, z), Shepperd's method."""
    t = R[0, 0] + R[1, 1] + R[2, 2]
    if t > 0:
        s = 0.5 / np.sqrt(t + 1.0)
        return np.array([0.25 / s,
                         (R[2, 1] - R[1, 2]) * s,
                         (R[0, 2] - R[2, 0]) * s,
                         (R[1, 0] - R[0, 1]) * s])
    if R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
        s = 2.0 * np.sqrt(1.0 + R[0, 0] - R[1, 1] - R[2, 2])
        return np.array([(R[2, 1] - R[1, 2]) / s, 0.25 * s,
                         (R[0, 1] + R[1, 0]) / s, (R[0, 2] + R[2, 0]) / s])
    if R[1, 1] > R[2, 2]:
        s = 2.0 * np.sqrt(1.0 + R[1, 1] - R[0, 0] - R[2, 2])
        return np.array([(R[0, 2] - R[2, 0]) / s, (R[0, 1] + R[1, 0]) / s,
                         0.25 * s, (R[1, 2] + R[2, 1]) / s])
    s = 2.0 * np.sqrt(1.0 + R[2, 2] - R[0, 0] - R[1, 1])
    return np.array([(R[1, 0] - R[0, 1]) / s, (R[0, 2] + R[2, 0]) / s,
                     (R[1, 2] + R[2, 1]) / s, 0.25 * s])


def _quat_mul(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Multiply two scalar quaternions (w,x,y,z) → (w,x,y,z)."""
    w1, x1, y1, z1 = a
    w2, x2, y2, z2 = b
    return np.array([
        w1*w2 - x1*x2 - y1*y2 - z1*z2,
        w1*x2 + x1*w2 + y1*z2 - z1*y2,
        w1*y2 - x1*z2 + y1*w2 + z1*x2,
        w1*z2 + x1*y2 - y1*x2 + z1*w2,
    ])


def _quat_mul_batch(q_scalar: np.ndarray, q_batch: np.ndarray) -> np.ndarray:
    """q_scalar (4,) wxyz  ×  q_batch (N, 4) wxyz  →  (N, 4) wxyz."""
    w1, x1, y1, z1 = q_scalar.astype(np.float64)
    w2 = q_batch[:, 0]; x2 = q_batch[:, 1]
    y2 = q_batch[:, 2]; z2 = q_batch[:, 3]
    return np.stack([
        w1*w2 - x1*x2 - y1*y2 - z1*z2,
        w1*x2 + x1*w2 + y1*z2 - z1*y2,
        w1*y2 - x1*z2 + y1*w2 + z1*x2,
        w1*z2 + x1*y2 - y1*x2 + z1*w2,
    ], axis=-1)


# ─── Attribute probing (LFS API) ──────────────────────────────────────────────

def _probe(obj, *names):
    """Return the first resolvable attribute, calling it if callable."""
    for name in names:
        attr = getattr(obj, name, None)
        if attr is not None:
            try:
                return attr() if callable(attr) else attr
            except Exception:
                continue
    return None


# ─── GLB writer ───────────────────────────────────────────────────────────────

def _write_spz_glb(path: Path, spz_blob: bytes, num_points: int,
                   sh_degree: int, positions: np.ndarray) -> None:
    pmin = positions.min(axis=0).tolist()
    pmax = positions.max(axis=0).tolist()

    accessors: list = []
    attrs: dict = {}

    def add_acc(name, type_str, ct, normalized=False, with_minmax=False):
        acc = {"componentType": ct, "count": num_points, "type": type_str}
        if normalized:
            acc["normalized"] = True
        if with_minmax:
            acc["min"] = pmin
            acc["max"] = pmax
        accessors.append(acc)
        attrs[name] = len(accessors) - 1

    add_acc("POSITION", "VEC3", 5126, with_minmax=True)
    add_acc("COLOR_0", "VEC4", 5121, normalized=True)
    add_acc("KHR_gaussian_splatting:SCALE", "VEC3", 5126)
    add_acc("KHR_gaussian_splatting:ROTATION", "VEC4", 5126)
    for d in range(1, sh_degree + 1):
        for k in range({1: 3, 2: 5, 3: 7}[d]):
            add_acc(f"KHR_gaussian_splatting:SH_DEGREE_{d}_COEF_{k}", "VEC4", 5126)

    spz_len = len(spz_blob)
    bin_pad = (-spz_len) % 4
    bin_total = spz_len + bin_pad

    gltf = {
        "asset": {"version": "2.0", "generator": "geo_register_plugin/tiles_exporter.py"},
        "extensionsUsed": [
            "KHR_gaussian_splatting",
            "KHR_gaussian_splatting_compression_spz_2",
            "KHR_materials_unlit",
        ],
        "extensionsRequired": [
            "KHR_gaussian_splatting",
            "KHR_gaussian_splatting_compression_spz_2",
        ],
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "matrix": [
            1.0, 0.0,  0.0, 0.0,
            0.0, 0.0, -1.0, 0.0,
            0.0, 1.0,  0.0, 0.0,
            0.0, 0.0,  0.0, 1.0,
        ]}],
        "meshes": [{"primitives": [{
            "mode": 0,
            "material": 0,
            "attributes": attrs,
            "extensions": {
                "KHR_gaussian_splatting": {
                    "extensions": {
                        "KHR_gaussian_splatting_compression_spz_2": {"bufferView": 0}
                    }
                }
            },
        }]}],
        "materials": [{
            "pbrMetallicRoughness": {"baseColorFactor": [1.0, 1.0, 1.0, 1.0]},
            "extensions": {"KHR_materials_unlit": {}},
        }],
        "buffers": [{"byteLength": bin_total}],
        "bufferViews": [{"buffer": 0, "byteLength": spz_len}],
        "accessors": accessors,
    }

    json_blob = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    json_pad = (-len(json_blob)) % 4
    json_total = len(json_blob) + json_pad
    total_len = 12 + 8 + json_total + 8 + bin_total

    with open(path, "wb") as f:
        f.write(struct.pack("<III", GLB_MAGIC, 2, total_len))
        f.write(struct.pack("<II", json_total, CHUNK_JSON))
        f.write(json_blob)
        f.write(b" " * json_pad)
        f.write(struct.pack("<II", bin_total, CHUNK_BIN))
        f.write(spz_blob)
        if bin_pad:
            f.write(b"\x00" * bin_pad)


# ─── Tileset builder ──────────────────────────────────────────────────────────

def _build_tileset(sim: dict, positions: np.ndarray, content_uri: str,
                   world_transform: np.ndarray | None = None) -> dict:
    """Build tileset.json dict.

    Positions are in the splat's local space (model-local).  world_transform
    (4×4, model-local → scene-world) is baked into the tile matrix when
    supplied, so the renderer applies the full chain uniformly.
    """
    s = float(sim["scale"])
    R = np.array(sim["rotation"], dtype=np.float64).reshape(3, 3)
    t = np.array(sim["translation"], dtype=np.float64)

    # M = sim @ diag(1,-1,-1) [@ W]  — all applied by the renderer uniformly.
    M = np.eye(4, dtype=np.float64)
    M[:3, :3] = s * R
    M[:3, 3] = t
    M = M @ np.diag([1.0, -1.0, -1.0, 1.0])
    if world_transform is not None:
        M = M @ world_transform

    transform_col_major = M.T.flatten().tolist()

    pmin = positions.min(axis=0)
    pmax = positions.max(axis=0)
    center = (pmin + pmax) * 0.5
    half   = (pmax - pmin) * 0.5
    box = [
        float(center[0]), float(center[1]), float(center[2]),
        float(half[0]), 0.0, 0.0,
        0.0, float(half[1]), 0.0,
        0.0, 0.0, float(half[2]),
    ]
    geom_err = float(np.linalg.norm(pmax - pmin))

    return {
        "asset": {"version": "1.1"},
        "extensionsUsed": ["3DTILES_content_gltf"],
        "extensionsRequired": ["3DTILES_content_gltf"],
        "extensions": {
            "3DTILES_content_gltf": {
                "extensionsUsed": [
                    "KHR_gaussian_splatting",
                    "KHR_gaussian_splatting_compression_spz_2",
                ],
                "extensionsRequired": [
                    "KHR_gaussian_splatting",
                    "KHR_gaussian_splatting_compression_spz_2",
                ],
            }
        },
        "geometricError": geom_err,
        "root": {
            "transform": transform_col_major,
            "boundingVolume": {"box": box},
            "geometricError": geom_err,
            "refine": "REPLACE",
            "content": {"uri": content_uri},
        },
    }


# ─── Core encoder (numpy arrays in, files out) ───────────────────────────────

def _export_from_arrays(
    positions_local: np.ndarray,          # (N, 3) model-local space
    rotations_wxyz:  np.ndarray,          # (N, 4) quaternion wxyz (may be None → identity)
    scales_log:      np.ndarray,          # (N, 3) log-space scales
    opacity_logit:   np.ndarray,          # (N,) raw 3DGS logit
    f_dc:            np.ndarray,          # (N, 3) raw SH0 coefficients
    f_rest_rgb:      np.ndarray | None,   # (N, K, 3) or None
    sh_degree:       int,
    transform:       dict,
    out_dir:         Path,
    content_name:    str,
    progress_cb,
    world_transform: np.ndarray | None = None,  # 4×4 model-local → scene-world
) -> None:
    def prog(f):
        if progress_cb:
            progress_cb(f)

    positions = positions_local.astype(np.float32)

    if rotations_wxyz is None:
        n = len(positions)
        rotations_wxyz = np.tile([1.0, 0.0, 0.0, 0.0], (n, 1)).astype(np.float32)

    # Reorder wxyz → xyzw for SPZ
    rotations_xyzw = np.stack([
        rotations_wxyz[:, 1], rotations_wxyz[:, 2],
        rotations_wxyz[:, 3], rotations_wxyz[:, 0],
    ], axis=-1).astype(np.float32)
    norms = np.linalg.norm(rotations_xyzw, axis=-1, keepdims=True)
    norms[norms == 0] = 1.0
    rotations_xyzw /= norms

    prog(0.2)

    spz = encode_spz_v3(
        positions=positions,
        rotations_xyzw=rotations_xyzw,
        scales_log=scales_log.astype(np.float32),
        opacity_logit=opacity_logit.astype(np.float32),
        f_dc=f_dc.astype(np.float32),
        f_rest_rgb=f_rest_rgb,
        sh_degree=sh_degree,
    )
    prog(0.55)

    glb_path = out_dir / content_name
    _write_spz_glb(glb_path, spz, int(len(positions)), sh_degree, positions)
    prog(0.8)

    tileset = _build_tileset(transform, positions, content_name, world_transform)
    with open(out_dir / "tileset.json", "w", encoding="utf-8") as f:
        json.dump(tileset, f, indent=2)
    prog(1.0)


# ─── LFS node API entry point ─────────────────────────────────────────────────

def export_3dtiles(
    node,
    transform: dict,
    out_dir,
    sh_degree: int = 3,
    progress_cb=None,
    content_name: str = "splats.glb",
) -> None:
    """Export a LFS SPLAT node to 3D Tiles 1.1 (SPZ-compressed glTF).

    Parameters
    ----------
    node         : LichtFeld SceneNode (type SPLAT)
    transform    : dict with keys 's', 'R', 't' (local viewer world → ECEF)
    out_dir      : str or Path — output directory (must already exist)
    sh_degree    : 0..3 SH bands to include (0 = baked color only)
    progress_cb  : optional callable(float 0..1)
    content_name : GLB filename inside out_dir
    """
    out_dir = Path(out_dir)

    # Normalise both key conventions: plugin uses {s,R,t}, JSON uses {scale,rotation,translation}
    sim = {
        "scale":       transform.get("scale",       transform.get("s")),
        "rotation":    transform.get("rotation",    transform.get("R")),
        "translation": transform.get("translation", transform.get("t")),
    }

    def prog(f):
        if progress_cb:
            progress_cb(f)

    splat_data = node.splat_data()
    if splat_data is None:
        raise RuntimeError("Selected node has no splat data.")

    prog(0.05)

    W = np.asarray(node.world_transform, dtype=np.float64).reshape(4, 4)

    # ── Positions (raw model-local; world_transform goes into tileset matrix) ─
    means = np.asarray(splat_data.means_raw.cpu().numpy(), dtype=np.float32)

    # ── Filter deleted gaussians ──────────────────────────────────────────────
    deleted_raw = np.asarray(splat_data.deleted.cpu().numpy())
    if deleted_raw.ndim == 0:
        keep = np.ones(means.shape[0], dtype=bool)
    else:
        keep = ~deleted_raw.astype(bool)
    means = means[keep]

    prog(0.10)

    # ── Rotations (raw model-local; tile transform handles the rest) ──────────
    rotations_wxyz = np.asarray(
        splat_data.get_rotation().cpu().numpy(), dtype=np.float32
    )[keep]

    prog(0.18)

    # ── Scales (raw log-space; tile transform handles world scale) ────────────
    scales_log = np.asarray(splat_data.scaling_raw.cpu().numpy(), dtype=np.float32)[keep]

    prog(0.25)

    # ── Opacity ───────────────────────────────────────────────────────────────
    opacity_logit = np.asarray(
        splat_data.opacity_raw.cpu().numpy(), dtype=np.float32
    ).ravel()[keep]

    prog(0.30)

    # ── SH0 (f_dc) — LFS stores as [N, 1, 3] ─────────────────────────────────
    f_dc = np.asarray(splat_data.sh0_raw.cpu().numpy(), dtype=np.float32)[keep]
    f_dc = f_dc[:, 0, :]   # (N, 1, 3) → (N, 3)

    prog(0.35)

    # ── Higher SH — LFS stores shN_raw as [N, K, 3], already correct layout ──
    f_rest_rgb = None
    effective_sh = sh_degree
    if sh_degree > 0:
        rest_np = np.asarray(splat_data.shN_raw.cpu().numpy(), dtype=np.float32)
        if rest_np.ndim == 3 and rest_np.shape[1] > 0:
            rest_np = rest_np[keep]
            needed = DIM_FOR_DEGREE[sh_degree]
            if rest_np.shape[1] >= needed:
                f_rest_rgb = np.ascontiguousarray(rest_np[:, :needed, :])
            else:
                effective_sh = 0
        else:
            effective_sh = 0

    prog(0.40)

    _export_from_arrays(
        positions_local=means,
        rotations_wxyz=rotations_wxyz,
        scales_log=scales_log,
        opacity_logit=opacity_logit,
        f_dc=f_dc,
        f_rest_rgb=f_rest_rgb,
        sh_degree=effective_sh,
        transform=sim,
        out_dir=out_dir,
        content_name=content_name,
        progress_cb=lambda f: prog(0.40 + 0.60 * f),
        world_transform=W,
    )


# ─── PLY reader (for CLI path) ────────────────────────────────────────────────

def _read_ply(path: Path):
    with open(path, "rb") as f:
        f.seek(0)
        lines = []
        while True:
            line = f.readline()
            if not line:
                raise ValueError("EOF before end_header")
            lines.append(line)
            if line.strip() == b"end_header":
                break
        header_text = b"".join(lines).decode("ascii", errors="replace")
        if "format binary_little_endian" not in header_text:
            raise ValueError("only binary_little_endian PLY is supported")
        m = re.search(r"element vertex (\d+)", header_text)
        if not m:
            raise ValueError("could not find vertex count in PLY header")
        n = int(m.group(1))
        props = re.findall(r"property\s+(\S+)\s+(\S+)", header_text)
        if not all(t == "float" for t, _ in props):
            raise ValueError("only float32 PLY properties supported")
        names = [name for _, name in props]
        dtype = np.dtype([(name, "<f4") for name in names])
        data  = np.fromfile(f, dtype=dtype, count=n)
    if len(data) != n:
        raise ValueError(f"read {len(data)} of expected {n} vertices")
    return data, names


def _build_from_ply(ply, names, sh_degree: int):
    """Extract arrays from a structured PLY array."""
    positions = np.stack([ply["x"], ply["y"], ply["z"]], axis=-1).astype(np.float32)
    rotations_wxyz = np.stack(
        [ply["rot_0"], ply["rot_1"], ply["rot_2"], ply["rot_3"]], axis=-1
    ).astype(np.float32)
    scales_log = np.stack(
        [ply["scale_0"], ply["scale_1"], ply["scale_2"]], axis=-1
    ).astype(np.float32)
    opacity_logit = ply["opacity"].astype(np.float32)
    f_dc = np.stack(
        [ply["f_dc_0"], ply["f_dc_1"], ply["f_dc_2"]], axis=-1
    ).astype(np.float32)

    f_rest_rgb = None
    effective_sh = sh_degree
    if sh_degree > 0:
        rest_names = sorted(
            (nm for nm in names if nm.startswith("f_rest_")),
            key=lambda s: int(s.rsplit("_", 1)[1]),
        )
        if rest_names and len(rest_names) % 3 == 0:
            K = len(rest_names) // 3
            needed = DIM_FOR_DEGREE[sh_degree]
            if K >= needed:
                rest = np.stack([ply[nm] for nm in rest_names], axis=-1).astype(np.float32)
                rest = rest.reshape(-1, 3, K).transpose(0, 2, 1)  # (N, K, 3)
                f_rest_rgb = np.ascontiguousarray(rest[:, :needed, :])
            else:
                effective_sh = 0
        else:
            effective_sh = 0

    return positions, rotations_wxyz, scales_log, opacity_logit, f_dc, f_rest_rgb, effective_sh


# ─── CLI entry point ──────────────────────────────────────────────────────────

def main():
    import argparse
    import sys

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("ply",              type=Path, help="input 3DGS PLY")
    ap.add_argument("similarity_json",  type=Path, help="similarity_transform.json")
    ap.add_argument("out_dir",          type=Path, help="output directory")
    ap.add_argument("--content-name",   default="splats.glb")
    ap.add_argument("--max-sh-degree",  type=int, choices=[0, 1, 2, 3], default=3)
    ap.add_argument("--fraction",       type=float, default=1.0,
                    help="keep only this fraction of splats (0,1]")
    ap.add_argument("--seed",           type=int, default=0)
    args = ap.parse_args()

    if not (0.0 < args.fraction <= 1.0):
        sys.exit(f"--fraction must be in (0, 1], got {args.fraction}")

    args.out_dir.mkdir(parents=True, exist_ok=True)

    print(f"reading PLY: {args.ply}")
    ply, names = _read_ply(args.ply)
    print(f"  {len(ply):,} splats, {len(names)} properties")

    if args.fraction < 1.0:
        rng = np.random.default_rng(args.seed)
        n_keep = int(round(len(ply) * args.fraction))
        print(f"sampling {n_keep:,} of {len(ply):,} splats")
        ply = ply[np.random.default_rng(args.seed).choice(len(ply), n_keep, replace=False)]

    positions, rotations_wxyz, scales_log, opacity_logit, f_dc, f_rest_rgb, effective_sh = (
        _build_from_ply(ply, names, args.max_sh_degree)
    )
    del ply

    with open(args.similarity_json) as f:
        transform = json.load(f)
    transform = {
        "s": transform["scale"],
        "R": transform["rotation"],
        "t": transform["translation"],
    }

    def log_prog(f):
        print(f"  {int(f * 100)}%", end="\r", flush=True)

    _export_from_arrays(
        positions_local=positions,
        rotations_wxyz=rotations_wxyz,
        scales_log=scales_log,
        opacity_logit=opacity_logit,
        f_dc=f_dc,
        f_rest_rgb=f_rest_rgb,
        sh_degree=effective_sh,
        transform=transform,
        out_dir=args.out_dir,
        content_name=args.content_name,
        progress_cb=log_prog,
    )
    print(f"\nwrote {args.out_dir / args.content_name} + tileset.json")


if __name__ == "__main__":
    import sys
    sys.exit(main())
