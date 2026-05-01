"""Read camera centre positions from the loaded LichtFeld scene."""
from pathlib import Path


def read_camera_positions_from_scene(scene) -> dict[str, tuple[float, float, float]]:
    """Return {image_filename: (cx, cy, cz)} in visualizer world space.

    Computes camera centre C = -R^T · T from SceneNode.camera_R/T (raw dataset
    world-to-camera convention), then flips Y and Z to convert to the visualizer
    world space used by pick_at_screen.
    """
    import numpy as np
    import lichtfeld.scene as lf_scene

    cameras: dict[str, tuple[float, float, float]] = {}

    for node in scene.get_nodes(type=lf_scene.NodeType.CAMERA):
        R_tensor = node.camera_R
        T_tensor = node.camera_T
        img_path = node.image_path

        if R_tensor is None or T_tensor is None or not img_path:
            continue

        R = np.array(R_tensor.cpu().numpy())           # [3, 3] world-to-camera
        T = np.array(T_tensor.cpu().numpy()).flatten()  # [3]
        C = -(R.T @ T)                                  # camera centre, raw dataset world

        # Convert raw dataset world (Y-down, Z-forward) → visualizer world (Y-up, Z-backward)
        name = Path(img_path).name
        cameras[name] = (float(C[0]), float(-C[1]), float(-C[2]))

    return cameras
