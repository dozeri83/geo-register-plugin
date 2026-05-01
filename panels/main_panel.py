"""Main panel for the Geo Reference plugin."""
from pathlib import Path

import lichtfeld as lf

_OP_ID = "lfs_plugins.geo_register_pluggin.operators.geo_picker.GEO_OT_pick_location"

# Module-level world position so the draw handler can access it without a panel ref.
_active_world_pos: tuple | None = None


def _geo_draw_handler(ctx) -> None:
    pos = _active_world_pos
    if pos is None:
        return
    color = (0.4, 1.0, 0.4, 1.0)
    ctx.draw_point_3d(pos, color, 8.0)
    screen = ctx.world_to_screen(pos)
    if screen is not None:
        ctx.draw_circle_2d(screen, 8.0, color, 1.5)
        ctx.draw_text_2d((screen[0] + 18, screen[1] - 8), "Geo", color)


class MainPanel(lf.ui.Panel):
    id    = "geo_register_pluggin.main_panel"
    label = "Geo Reference"
    space = lf.ui.PanelSpace.MAIN_PANEL_TAB
    order = 50

    _MODES     = ["EXIF", "Similarity File", "Image Positions CSV"]
    _MODE_KEYS = ["exif", "similarity", "csv"]

    def __init__(self):
        self._mode_idx: int                  = 0
        self._status: str                    = ""
        self._status_is_error                = False
        self._transform: dict | None         = None
        self._picking: bool                  = False
        self._lla: tuple | None              = None   # (lat, lon, alt) from last pick
        self._world_pos: tuple | None        = None   # local 3-D position of last pick
        self._orig_images_folder: str | None = None   # override folder for EXIF scan

    @property
    def _mode(self) -> str:
        return self._MODE_KEYS[self._mode_idx]

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def on_scene_changed(self, doc):
        if self._picking:
            from ..operators.geo_picker import clear_pick_callback
            clear_pick_callback()
            lf.ui.ops.cancel_modal()
        self._mode_idx            = 0
        self._status              = ""
        self._status_is_error     = False
        self._transform           = None
        self._picking             = False
        self._orig_images_folder  = None
        self._clear_point()

    # ── Draw ──────────────────────────────────────────────────────────────────

    def draw(self, layout):
        scale = layout.get_dpi_scale()
        theme = lf.ui.theme()

        layout.label("Detect / Add Geo Reference")
        layout.separator()

        # Mode selector
        layout.label("Source:")
        changed, new_idx = layout.combo("##geo_mode", self._mode_idx, self._MODES)
        if changed and new_idx != self._mode_idx:
            self._mode_idx = new_idx
            self._transform = None
            self._status = ""
            self._clear_point()

        layout.separator()

        if self._mode == "exif":
            self._draw_exif_section(layout, scale, theme)
        elif self._mode == "similarity":
            self._draw_similarity_section(layout, scale, theme)
        else:
            self._draw_csv_section(layout, scale, theme)

        # Status line
        if self._status:
            layout.spacing()
            prefix = "[!] " if self._status_is_error else "[ok] "
            color  = (1.0, 0.4, 0.4, 1.0) if self._status_is_error else (0.4, 1.0, 0.4, 1.0)
            layout.text_colored(prefix + self._status, color)

        # Transform result + pick section
        if self._transform is not None:
            self._draw_transform_section(layout, scale, theme)

    def _draw_exif_section(self, layout, scale, theme):
        layout.text_colored(
            "Scans dataset images for GPS EXIF tags,\n"
            "matches to camera poses, and solves the\n"
            "similarity transform to ECEF (WGS-84).",
            theme.palette.text_dim,
        )
        layout.spacing()

        if self._orig_images_folder:
            layout.text_colored(self._orig_images_folder, theme.palette.text_dim)
            if layout.button_styled("Change Original Images Folder", "warning", (-1, 28 * scale)):
                self._pick_orig_images_folder()
        else:
            layout.text_colored("Images: dataset folder (default)", theme.palette.text_dim)
            if layout.button_styled("Set Original Images Folder", "warning", (-1, 28 * scale)):
                self._pick_orig_images_folder()

        layout.spacing()
        if layout.button_styled("Calc Georeference From EXIF", "primary", (-1, 32 * scale)):
            self._run_exif()

    def _draw_similarity_section(self, layout, scale, theme):
        layout.text_colored(
            "Load a similarity transform JSON file\n"
            "to register the scene to ECEF coordinates.",
            theme.palette.text_dim,
        )
        layout.spacing()
        if layout.button_styled("Load Similarity File", "primary", (-1, 32 * scale)):
            self._load_similarity_file()

    def _draw_transform_section(self, layout, scale, theme):
        t     = self._transform
        n_in  = t.get("n_inliers", t["n"])
        n_tot = t.get("n_total",   t["n"])

        layout.separator()
        layout.label("Computed Transform  (local -> ECEF)")
        layout.text_colored(f"Inliers : {n_in} / {n_tot}  ({n_tot - n_in} rejected)", theme.palette.text_dim)
        layout.text_colored(f"Scale   : {t['s']:.8f}", theme.palette.text_dim)
        layout.text_colored(f"RMSE    : {t['rmse']:.4f} m", theme.palette.text_dim)

        layout.separator()

        # Pick location button / stop picking button
        if self._picking:
            if layout.button_styled("Stop Picking##geo_pick_stop", "error", (-1, 32 * scale)):
                self._cancel_pick()
            layout.text_colored("Click on the model -- ESC to cancel", theme.palette.text_dim)
        else:
            if layout.button_styled("Get Pixel Location##geo_pick_start", "primary", (-1, 32 * scale)):
                self._start_pick()

        # LLA result
        if self._lla is not None:
            self._draw_lla_section(layout, scale, theme)

    def _draw_lla_section(self, layout, scale, theme):
        lat, lon, alt = self._lla
        layout.separator()
        layout.label("Geographic Location (LLA WGS-84)")
        layout.text_colored(f"Lat : {lat:+.8f} deg", theme.palette.text_dim)
        layout.text_colored(f"Lon : {lon:+.8f} deg", theme.palette.text_dim)
        layout.text_colored(f"Alt : {alt:.3f} m", theme.palette.text_dim)
        layout.spacing()
        if layout.button_styled("Copy to Clipboard", "primary", (-1, 0)):
            self._copy_lla()
        layout.spacing()
        if layout.button_styled("Clear Point", "error", (-1, 0)):
            self._clear_point()

    # ── Picking ───────────────────────────────────────────────────────────────

    def _start_pick(self):
        from ..operators.geo_picker import set_pick_callback
        self._picking = True
        self._lla = None
        self._clear_point()
        set_pick_callback(self._on_location_picked)
        lf.ui.ops.invoke(_OP_ID)
        lf.ui.request_redraw()

    def _cancel_pick(self):
        from ..operators.geo_picker import clear_pick_callback
        self._picking = False
        clear_pick_callback()
        lf.ui.ops.cancel_modal()
        lf.ui.request_redraw()

    def _on_location_picked(self, world_pos: tuple):
        """Called by the operator when the user clicks on the model."""
        from ..geo.transform import to_4x4_col_major
        from ..geo.ecef import ecef_to_geodetic
        import numpy as np

        global _active_world_pos

        self._world_pos = world_pos
        _active_world_pos = world_pos

        t = self._transform
        G = np.array(to_4x4_col_major(t["s"], t["R"], t["t"])).reshape(4, 4, order="F")
        p = np.array([world_pos[0], world_pos[1], world_pos[2], 1.0])
        ecef = G @ p

        lat, lon, alt = ecef_to_geodetic(float(ecef[0]), float(ecef[1]), float(ecef[2]))
        self._lla = (lat, lon, alt)
        lf.log.info(f"geo_register: picked lat={lat:.8f} lon={lon:.8f} alt={alt:.3f} m")
        lf.ui.request_redraw()

    def _clear_point(self):
        global _active_world_pos
        self._world_pos = None
        self._lla = None
        _active_world_pos = None
        lf.ui.request_redraw()

    def _copy_lla(self):
        if self._lla is None:
            return
        lat, lon, alt = self._lla
        text = f"{lat:.8f}, {lon:.8f}, {alt:.3f}"
        lf.ui.set_clipboard_text(text)
        lf.log.info(f"geo_register: copied to clipboard: {text}")

    def _pick_orig_images_folder(self):
        folder = lf.ui.open_folder_dialog(title="Select Original Images Folder")
        if folder:
            self._orig_images_folder = folder
            lf.log.info(f"geo_register: original images folder set to '{folder}'")
            lf.ui.request_redraw()

    # ── Georeference pipeline ─────────────────────────────────────────────────

    def _run_exif(self):
        from lfs_plugins.ui.state import AppState
        from ..geo.exif_reader import find_images_with_gps, NoGPSDataError

        self._transform = None
        self._lla = None
        self._clear_point()

        scene_path = AppState.scene_path.value
        if not scene_path or lf.get_scene() is None:
            self._set_status("No scene is currently loaded.", error=True)
            return

        scan_folder = self._orig_images_folder or scene_path
        lf.log.info(f"geo_register: scanning '{scan_folder}' for GPS EXIF ...")
        try:
            raw = find_images_with_gps(scan_folder)
        except NoGPSDataError as exc:
            self._set_status(str(exc), error=True)
            lf.log.warn(f"geo_register: {exc}")
            return
        except Exception as exc:
            self._set_status(f"EXIF scan error: {exc}", error=True)
            lf.log.error(f"geo_register: {exc}")
            return

        lf.log.info(f"geo_register: GPS found in {len(raw)} image(s).")
        gps_list = [
            {"name": Path(e["path"]).stem, "lat": e["lat"], "lon": e["lon"], "alt": e["alt"]}
            for e in raw
        ]
        self._run_georeg(gps_list)

    def _run_georeg(self, gps_list: list) -> None:
        from lfs_plugins.ui.state import AppState
        from ..geo.camera_reader import read_camera_positions_from_scene
        from ..geo.ecef import geodetic_to_ecef
        from ..geo.transform import robust_umeyama

        scene_path = AppState.scene_path.value
        scene = lf.get_scene()
        if not scene_path or scene is None:
            self._set_status("No scene is currently loaded.", error=True)
            return

        cameras = read_camera_positions_from_scene(scene)
        if not cameras:
            self._set_status("No camera nodes found in the scene. Load a dataset first.", error=True)
            lf.log.warn("geo_register: no camera nodes in scene.")
            return
        lf.log.info(f"geo_register: {len(cameras)} camera pose(s) read from scene.")

        src_pts: list = []
        dst_pts: list = []
        matched_gps: list = []
        for entry in gps_list:
            name = entry["name"]
            if name in cameras:
                src_pts.append(cameras[name])
                dst_pts.append(geodetic_to_ecef(entry["lat"], entry["lon"], entry["alt"]))
                matched_gps.append(entry)

        if len(src_pts) < 3:
            msg = (
                f"Only {len(src_pts)} matched image(s) "
                f"(GPS: {len(gps_list)}, cameras: {len(cameras)}). "
                "Need at least 3."
            )
            self._set_status(msg, error=True)
            lf.log.warn(f"geo_register: {msg}")
            return

        lf.log.info(f"geo_register: {len(src_pts)} correspondences - running RANSAC+IRLS ...")
        try:
            result = robust_umeyama(src_pts, dst_pts)
        except Exception as exc:
            self._set_status(f"Transform estimation failed: {exc}", error=True)
            lf.log.error(f"geo_register: {exc}")
            return

        self._transform = result
        saved_json, saved_csv = self._save_transform(result, scene_path, matched_gps)
        n_in  = result.get("n_inliers", result["n"])
        n_tot = result.get("n_total",   result["n"])
        status = f"Ready -- {n_in}/{n_tot} inliers, RMSE {result['rmse']:.3f} m"
        if saved_json:
            status += f" | Saved: {saved_json}"
        if saved_csv:
            status += f" | CSV: {saved_csv}"
        self._set_status(status, error=False)
        lf.log.info(
            f"geo_register: inliers={n_in}/{n_tot}  "
            f"scale={result['s']:.6f}  RMSE={result['rmse']:.3f} m"
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _draw_csv_section(self, layout, scale, theme):
        layout.text_colored(
            "Load an image positions CSV file\n"
            "(columns: image_name, lat, lon, alt).",
            theme.palette.text_dim,
        )
        layout.spacing()
        if layout.button_styled("Load CSV File", "primary", (-1, 32 * scale)):
            self._load_csv_file()

    def _load_csv_file(self) -> None:
        import csv

        path = lf.ui.open_csv_file_dialog()
        if not path:
            return

        self._transform = None
        self._lla = None
        self._clear_point()

        try:
            gps_list = []
            with open(path, "r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    gps_list.append({
                        "name": Path(row["image_name"]).stem,
                        "lat":  float(row["lat"]),
                        "lon":  float(row["lon"]),
                        "alt":  float(row["alt"]),
                    })
        except Exception as exc:
            self._set_status(f"Failed to parse CSV: {exc}", error=True)
            lf.log.error(f"geo_register: {exc}")
            return

        lf.log.info(f"geo_register: loaded {len(gps_list)} image positions from '{path}'")
        self._run_georeg(gps_list)

    def _load_similarity_file(self) -> None:
        import json
        import shutil

        path = lf.ui.open_json_file_dialog()
        if not path:
            return

        self._transform = None
        self._lla = None
        self._clear_point()

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            for key in ("scale", "rotation", "translation"):
                if key not in data:
                    self._set_status(f"Invalid similarity file: missing '{key}'", error=True)
                    lf.log.error(f"geo_register: similarity file missing key '{key}'")
                    return

            n = data.get("n_total", data.get("n_inliers", 0))
            self._transform = {
                "s":        data["scale"],
                "R":        data["rotation"],
                "t":        data["translation"],
                "rmse":     data.get("rmse_m", 0.0),
                "n":        n,
                "n_inliers": data.get("n_inliers", n),
                "n_total":   data.get("n_total",   n),
            }

            # Copy to output dir if the file is not already there
            copied_to = None
            output_path = lf.dataset_params().output_path
            if output_path:
                expected_dir = Path(output_path) / "geo_register_plugin_data"
                src = Path(path)
                if src.parent.resolve() != expected_dir.resolve():
                    expected_dir.mkdir(parents=True, exist_ok=True)
                    dst = expected_dir / src.name
                    shutil.copy2(str(src), str(dst))
                    copied_to = dst
                    lf.log.info(f"geo_register: copied similarity file to '{dst}'")

            status = f"Loaded: {Path(path).name}"
            if copied_to:
                status += f" | Copied to: {copied_to}"
            self._set_status(status, error=False)
            lf.log.info(f"geo_register: loaded similarity transform from '{path}'")

        except Exception as exc:
            self._set_status(f"Failed to load file: {exc}", error=True)
            lf.log.error(f"geo_register: {exc}")

    def _save_transform(self, result: dict, scene_path: str, matched_gps: list | None = None) -> tuple:
        import json
        import csv

        output_path = lf.dataset_params().output_path
        base = Path(output_path) if output_path else Path(scene_path)
        out_dir = base / "geo_register_plugin_data"
        out_dir.mkdir(parents=True, exist_ok=True)

        payload = {
            "scale": result["s"],
            "rotation": result["R"],
            "translation": result["t"],
            "rmse_m": result["rmse"],
            "n_inliers": result.get("n_inliers", result["n"]),
            "n_total": result.get("n_total", result["n"]),
        }

        out_file = out_dir / "similarity_transform.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

        saved_csv = None
        if matched_gps:
            csv_file = out_dir / "image_positions.csv"
            with open(csv_file, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["image_name", "lat", "lon", "alt"])
                for row in matched_gps:
                    writer.writerow([row["name"], row["lat"], row["lon"], row["alt"]])
            saved_csv = str(csv_file)
            lf.log.info(f"geo_register: image positions saved to '{csv_file}'")

        info_file = out_dir / "similarity_transform_info.txt"
        with open(info_file, "w", encoding="utf-8") as f:
            f.write("Similarity Transform\n")
            f.write("====================\n\n")
            f.write("Formula: world_ecef = scale * R @ p_scene + translation\n\n")
            f.write("Fields:\n")
            f.write("  scale       - uniform scale factor\n")
            f.write("  rotation    - 3x3 rotation matrix (row-major)\n")
            f.write("  translation - 3D translation vector in metres\n")
            f.write("  rmse_m      - root mean square error of the fit in metres\n\n")
            f.write("Order of operations:\n")
            f.write("  1. Multiply scene point by scale\n")
            f.write("  2. Apply rotation R\n")
            f.write("  3. Add translation\n\n")
            f.write("The result is an ECEF (Earth-Centered Earth-Fixed) coordinate in metres.\n")

        lf.log.info(f"geo_register: transform saved to '{out_file}'")
        return str(out_file), saved_csv

    def _set_status(self, message: str, *, error: bool) -> None:
        self._status          = message
        self._status_is_error = error
