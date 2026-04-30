"""Read camera center positions in local scene space.

Preferred source: SceneNode data already loaded in LichtFeld (no file I/O).
Fallback:
  - COLMAP text:   <root>/sparse/0/images.txt
  - NeRF JSON:     <root>/transforms.json  (or transforms_train.json)
"""
import json
import numpy as np
from pathlib import Path


class NoCameraDataError(RuntimeError):
    pass


# ── Primary: read from the loaded scene ──────────────────────────────────────

def read_camera_positions_from_scene(scene) -> dict[str, tuple[float, float, float]]:
    """Return {image_filename: (cx, cy, cz)} from camera nodes already in the scene.

    Uses SceneNode.camera_R (world-to-camera [3,3]) and camera_T ([3,1]) to
    compute the camera centre:  C = -R^T · T
    """
    import lichtfeld.scene as lf_scene

    cameras: dict[str, tuple[float, float, float]] = {}

    for node in scene.get_nodes(type=lf_scene.NodeType.CAMERA):
        R_tensor = node.camera_R
        T_tensor = node.camera_T
        img_path = node.image_path

        if R_tensor is None or T_tensor is None or not img_path:
            continue

        R = np.array(R_tensor.cpu().numpy())          # [3, 3] world-to-camera
        T = np.array(T_tensor.cpu().numpy()).flatten() # [3]
        C = -(R.T @ T)                                 # camera centre in world

        name = Path(img_path).name
        cameras[name] = (float(C[0]), float(C[1]), float(C[2]))

    return cameras


# ── Fallback: read from dataset files ────────────────────────────────────────

def read_camera_positions(scene_path: str) -> dict[str, tuple[float, float, float]]:
    """Return {image_filename: (cx, cy, cz)} in local scene space.

    Tries COLMAP text format first, then NeRF transforms.json.
    Raises NoCameraDataError if no supported file is found or parsing yields nothing.
    """
    root = Path(scene_path)

    colmap_txt = root / "sparse" / "0" / "images.txt"
    if colmap_txt.exists():
        result = _read_colmap_images_txt(colmap_txt)
        if result:
            return result

    for name in ("transforms.json", "transforms_train.json"):
        tf = root / name
        if tf.exists():
            result = _read_transforms_json(tf)
            if result:
                return result

    raise NoCameraDataError(
        f"No camera pose file found under '{scene_path}'. "
        "Expected sparse/0/images.txt or transforms.json."
    )


# ── COLMAP text format ────────────────────────────────────────────────────────

def _read_colmap_images_txt(path: Path) -> dict[str, tuple[float, float, float]]:
    """Parse COLMAP images.txt.

    Each image occupies two lines:
      IMAGE_ID QW QX QY QZ TX TY TZ CAMERA_ID NAME
      POINTS2D[] ...
    Camera center in world space: C = -R^T * t
    where R is built from (QW QX QY QZ) and t = (TX TY TZ).
    """
    cameras: dict[str, tuple[float, float, float]] = {}

    with open(path, "r", encoding="utf-8") as f:
        lines = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]

    i = 0
    while i < len(lines):
        parts = lines[i].split()
        if len(parts) < 10:
            i += 1
            continue
        try:
            qw, qx, qy, qz = float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4])
            tx, ty, tz     = float(parts[5]), float(parts[6]), float(parts[7])
            name = Path(parts[9]).name
        except (ValueError, IndexError):
            i += 2
            continue

        R = _quat_to_rot(qw, qx, qy, qz)
        # C = -R^T * t
        C = _matT_vec(R, (-tx, -ty, -tz))
        cameras[name] = C
        i += 2  # skip the POINTS2D line

    return cameras


def _quat_to_rot(qw: float, qx: float, qy: float, qz: float) -> list[list[float]]:
    """Unit quaternion → 3×3 rotation matrix (row-major)."""
    return [
        [1 - 2*(qy*qy + qz*qz),     2*(qx*qy - qw*qz),     2*(qx*qz + qw*qy)],
        [    2*(qx*qy + qw*qz), 1 - 2*(qx*qx + qz*qz),     2*(qy*qz - qw*qx)],
        [    2*(qx*qz - qw*qy),     2*(qy*qz + qw*qx), 1 - 2*(qx*qx + qy*qy)],
    ]


def _matT_vec(R: list[list[float]], v: tuple) -> tuple[float, float, float]:
    """R^T * v  (multiply transpose of 3×3 R by 3-vector v)."""
    return (
        R[0][0]*v[0] + R[1][0]*v[1] + R[2][0]*v[2],
        R[0][1]*v[0] + R[1][1]*v[1] + R[2][1]*v[2],
        R[0][2]*v[0] + R[1][2]*v[1] + R[2][2]*v[2],
    )


# ── NeRF / instant-ngp transforms.json ───────────────────────────────────────

def _read_transforms_json(path: Path) -> dict[str, tuple[float, float, float]]:
    """Parse transforms.json.  Camera position = last column of transform_matrix."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    cameras: dict[str, tuple[float, float, float]] = {}
    for frame in data.get("frames", []):
        fp = frame.get("file_path", "")
        name = Path(fp).name
        m = frame.get("transform_matrix")
        if m and len(m) >= 3 and len(m[0]) >= 4:
            cameras[name] = (float(m[0][3]), float(m[1][3]), float(m[2][3]))

    return cameras
