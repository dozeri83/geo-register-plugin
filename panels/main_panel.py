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

    _MODES     = ["EXIF", "Similarity File", "Image Positions CSV", "RealityScan Parameters CSV"]
    _MODE_KEYS = ["exif", "similarity", "csv", "rs_csv"]

    def __init__(self):
        self._mode_idx: int                  = 0
        self._status: str                    = ""
        self._status_is_error                = False
        self._transform: dict | None         = None
        self._picking: bool                  = False
        self._lla: tuple | None              = None   # (lat, lon, alt) from last pick
        self._world_pos: tuple | None        = None   # local 3-D position of last pick
        self._orig_images_folder: str | None = None   # override folder for EXIF scan
        self._export_splat_idx: int           = 0
        self._export_format_idx: int         = 0      # 0=LAS, 1=LAZ, 2=3D Tiles (SPZ)
        self._export_output_path: str | None = None
        self._export_progress: float | None  = None   # None=idle, 0-1=running
        self._export_error: str | None       = None
        self._export_success: str | None     = None
        self._tiles_out_dir: str | None      = None
        self._tiles_progress: float | None   = None
        self._tiles_error: str | None        = None
        self._tiles_success: str | None      = None
        # PLY converter (Edit Mode)
        self._ply_file_path: str | None      = None
        self._ply_sim_path: str | None       = None
        self._ply_format_idx: int            = 2      # default to 3D Tiles
        self._ply_out_file: str | None       = None
        self._ply_out_dir: str | None        = None
        self._ply_progress: float | None     = None
        self._ply_error: str | None          = None
        self._ply_success: str | None        = None

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
        self._export_splat_idx    = 0
        self._export_format_idx   = 0
        self._export_output_path  = None
        self._export_progress     = None
        self._export_error        = None
        self._export_success      = None
        self._tiles_out_dir       = None
        self._tiles_progress      = None
        self._tiles_error         = None
        self._tiles_success       = None
        self._ply_progress        = None
        self._ply_error           = None
        self._ply_success         = None
        self._clear_point()
        if doc is not None:
            self._detect_existing_registration()

    # ── Draw ──────────────────────────────────────────────────────────────────

    def draw(self, layout):
        scale = layout.get_dpi_scale()
        theme = lf.ui.theme()

        # If no cameras are present the user has switched to Edit Mode and the
        # dataset (including camera data) has been discarded.  Geo-registration
        # requires camera information, so show a blocking notice and return.
        scene = lf.get_scene()
        if scene is not None:
            import lichtfeld.scene as lf_scene
            has_cameras = any(True for _ in scene.get_nodes(type=lf_scene.NodeType.CAMERA))
            if not has_cameras:
                self._draw_ply_converter_section(layout, scale, theme)
                return

        layout.label("Detect / Add Geo Reference")
        layout.separator()

        # Mode selector
        layout.label("Source:")
        layout.same_line()
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
        elif self._mode == "csv":
            self._draw_csv_section(layout, scale, theme)
        else:
            self._draw_rs_csv_section(layout, scale, theme)

        # Status line
        if self._status:
            layout.spacing()
            prefix = "[!] " if self._status_is_error else "[ok] "
            color  = (1.0, 0.4, 0.4, 1.0) if self._status_is_error else (0.4, 1.0, 0.4, 1.0)
            layout.text_colored(prefix + self._status, color)

        # Transform result + pick section
        if self._transform is not None:
            self._draw_transform_section(layout, scale, theme)
            self._draw_export_section(layout, scale, theme)

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

        if self._orig_images_folder:
            scan_folder = self._orig_images_folder
        else:
            params = lf.dataset_params()
            data_path = params.data_path if params else None
            images_sub = (params.images if params else None) or ""
            if data_path:
                scan_folder = str(Path(data_path) / images_sub) if images_sub else data_path
            else:
                scan_folder = scene_path
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
            import json
            _cfg_path = Path(__file__).parent.parent / "config.json"
            _cfg = json.loads(_cfg_path.read_text(encoding="utf-8")) if _cfg_path.exists() else {}
            result = robust_umeyama(
                src_pts, dst_pts,
                inlier_thr     = float(_cfg.get("ransac_inlier_thr_m",  10.0)),
                confidence     = float(_cfg.get("ransac_confidence",      0.99)),
                max_ransac_iter= int(  _cfg.get("ransac_max_iter",        2000)),
                huber_delta    = float(_cfg.get("irls_huber_delta_m",     2.0)),
                max_irls_iter  = int(  _cfg.get("irls_max_iter",          50)),
            )
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
                lines = f.read().splitlines()
            # Strip leading # from header line if present
            if lines and lines[0].startswith("#"):
                lines[0] = lines[0].lstrip("#").strip()
            import io
            reader = csv.DictReader(io.StringIO("\n".join(lines)))
            fieldnames = reader.fieldnames
            required = {"image_name", "lat", "lon", "alt"}
            if set(fieldnames) != required:
                raise ValueError(f"Invalid CSV header. Expected columns: {required}, got {set(fieldnames)}")
            
            # Find column indices for any order
            lat_idx = fieldnames.index("lat")
            lon_idx = fieldnames.index("lon")
            alt_idx = fieldnames.index("alt")
            image_idx = fieldnames.index("image_name")
            
            for row in reader:
                values = list(row.values())
                gps_list.append({
                    "name": Path(values[image_idx]).stem,
                    "lat": float(values[lat_idx]),
                    "lon": float(values[lon_idx]),
                    "alt": float(values[alt_idx]),
                })
        except Exception as exc:
            self._set_status(f"Failed to parse CSV: {exc}", error=True)
            lf.log.error(f"geo_register: {exc}")
            return

        lf.log.info(f"geo_register: loaded {len(gps_list)} image positions from '{path}'")
        self._run_georeg(gps_list)

    def _draw_rs_csv_section(self, layout, scale, theme):
        layout.text_colored(
            "Load a RealityScan Internal/External\n"
            "Camera Parameters CSV file\n"
            "(columns: name, x=lon, y=lat, alt, ...).",
            theme.palette.text_dim,
        )
        layout.spacing()
        if layout.button_styled("Load RealityScan CSV", "primary", (-1, 32 * scale)):
            self._load_rs_csv_file()

    def _load_rs_csv_file(self) -> None:
        import csv
        import io

        path = lf.ui.open_csv_file_dialog()
        if not path:
            return

        self._transform = None
        self._lla = None
        self._clear_point()

        try:
            gps_list = []
            with open(path, "r", encoding="utf-8", newline="") as f:
                lines = f.read().splitlines()
            # Strip leading # from header line if present
            if lines and lines[0].startswith("#"):
                lines[0] = lines[0].lstrip("#").strip()
            reader = csv.DictReader(io.StringIO("\n".join(lines)))
            for row in reader:
                gps_list.append({
                    "name": Path(row["name"]).stem,
                    "lat":  float(row["y"]),   # RealityScan: y = latitude
                    "lon":  float(row["x"]),   # RealityScan: x = longitude
                    "alt":  float(row["alt"]),
                })
        except Exception as exc:
            self._set_status(f"Failed to parse RealityScan CSV: {exc}", error=True)
            lf.log.error(f"geo_register: {exc}")
            return

        lf.log.info(f"geo_register: loaded {len(gps_list)} RealityScan positions from '{path}'")
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
                writer.writerow(["#image_name", "lat", "lon", "alt"])
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

    def _draw_export_section(self, layout, scale, theme) -> None:
        layout.separator()
        layout.label("Export")
        layout.separator()

        splat_names = self._get_splat_names()
        layout.label("Splat Model:")
        layout.same_line()
        if splat_names:
            if self._export_splat_idx >= len(splat_names):
                self._export_splat_idx = 0
            changed, new_idx = layout.combo("##export_splat", self._export_splat_idx, splat_names)
            if changed:
                self._export_splat_idx = new_idx
        else:
            layout.text_colored("No splat models found in scene.", theme.palette.text_dim)

        layout.label("Format:")
        layout.same_line()
        fmt_changed, fmt_idx = layout.combo(
            "##export_format", self._export_format_idx, ["LAS", "LAZ", "3D Tiles (SPZ)"]
        )
        if fmt_changed:
            self._export_format_idx = fmt_idx

        layout.spacing()

        if self._export_format_idx in (0, 1):
            self._draw_las_export(layout, scale, theme, splat_names)
        else:
            self._draw_tiles_export(layout, scale, theme, splat_names)

    def _draw_las_export(self, layout, scale, theme, splat_names) -> None:
        if self._export_output_path:
            layout.text_colored(self._export_output_path, theme.palette.text_dim)
            layout.spacing()

        if self._export_progress is not None:
            pct = int(self._export_progress * 100)
            layout.progress_bar(self._export_progress, overlay=f"Exporting... {pct}%",
                                width=-1, height=24 * scale)
        else:
            if self._export_error:
                layout.text_colored(f"[!] {self._export_error}", (1.0, 0.4, 0.4, 1.0))
            if self._export_success:
                layout.text_colored(self._export_success, (0.3, 1.0, 0.3, 1.0))
            if splat_names:
                if layout.button_styled("Export LAS/LAZ##export_las_btn", "primary", (-1, 32 * scale)):
                    splat_name = splat_names[self._export_splat_idx]
                    if self._export_format_idx == 1:
                        save_fn = getattr(lf.ui, "save_laz_file_dialog", None)
                        if save_fn:
                            path = save_fn(default_name=splat_name)
                        else:
                            path = lf.ui.save_las_file_dialog(default_name=splat_name)
                            if path:
                                path = str(Path(path).with_suffix(".laz"))
                    else:
                        path = lf.ui.save_las_file_dialog(default_name=splat_name)
                    if path:
                        self._export_output_path = path
                        self._start_export_las(path)
                        lf.ui.request_redraw()
            else:
                layout.text_colored("No splat models found in scene.", theme.palette.text_dim)

    def _draw_tiles_export(self, layout, scale, theme, splat_names) -> None:
        if self._tiles_out_dir:
            layout.text_colored(self._tiles_out_dir, theme.palette.text_dim)
            if layout.button_styled("Change Output Directory##tiles_change_dir", "warning", (-1, 28 * scale)):
                self._pick_tiles_out_dir()
        else:
            layout.text_colored("No output directory selected.", theme.palette.text_dim)
            if layout.button_styled("Choose Output Directory##tiles_pick_dir", "primary", (-1, 32 * scale)):
                self._pick_tiles_out_dir()

        layout.spacing()

        if self._tiles_progress is not None:
            pct = int(self._tiles_progress * 100)
            layout.progress_bar(self._tiles_progress, overlay=f"Exporting... {pct}%",
                                width=-1, height=24 * scale)
        else:
            if self._tiles_error:
                layout.text_colored(f"[!] {self._tiles_error}", (1.0, 0.4, 0.4, 1.0))
            if self._tiles_success:
                layout.text_colored(self._tiles_success, (0.3, 1.0, 0.3, 1.0))
            if splat_names and self._tiles_out_dir:
                if layout.button_styled("Export 3D Tiles##export_tiles_btn", "primary", (-1, 32 * scale)):
                    self._start_export_tiles()
                    lf.ui.request_redraw()
            elif not splat_names:
                layout.text_colored("No splat models found in scene.", theme.palette.text_dim)

    def _get_splat_names(self) -> list[str]:
        import lichtfeld.scene as lf_scene

        scene = lf.get_scene()
        if scene is None:
            return []
        return [
            node.name if node.name else f"Splat #{node.id}"
            for node in scene.get_nodes(type=lf_scene.NodeType.SPLAT)
        ]

    def _start_export_las(self, output_path: str) -> None:
        import threading
        import lichtfeld.scene as lf_scene

        scene = lf.get_scene()
        if scene is None:
            self._export_error = "No scene loaded."
            return

        nodes = scene.get_nodes(type=lf_scene.NodeType.SPLAT)
        if self._export_splat_idx >= len(nodes):
            self._export_error = "Selected splat model not found."
            return

        node = nodes[self._export_splat_idx]

        self._export_progress = 0.0
        self._export_error    = None
        self._export_success  = None
        lf.ui.request_redraw()

        threading.Thread(
            target=self._export_las_worker,
            args=(node, dict(self._transform), output_path),
            daemon=True,
        ).start()

    def _export_las_worker(self, node, transform: dict, output_path: str) -> None:
        try:
            from ..geo.las_exporter import export_las
            export_las(node, transform, output_path, progress_cb=self._on_export_progress)
            lf.log.info(f"geo_register: LAS exported to '{output_path}'")
            self._export_success = f"Export succeeded: {Path(output_path).name}"
        except Exception as exc:
            self._export_error = str(exc)
            lf.log.error(f"geo_register: LAS export failed: {exc}")
        finally:
            self._export_progress = None
            lf.ui.request_redraw()

    def _on_export_progress(self, fraction: float) -> None:
        self._export_progress = fraction
        lf.ui.request_redraw()

    def _pick_tiles_out_dir(self) -> None:
        folder = lf.ui.open_folder_dialog(title="Select 3D Tiles Output Directory")
        if folder:
            self._tiles_out_dir = folder
            self._tiles_error   = None
            self._tiles_success = None
            lf.ui.request_redraw()

    def _start_export_tiles(self) -> None:
        import threading
        import lichtfeld.scene as lf_scene

        out_dir = Path(self._tiles_out_dir)

        # Conflict check
        for fname in ("tileset.json", "splats.glb"):
            if (out_dir / fname).exists():
                self._tiles_error = (
                    f"{fname} already exists in the selected directory. "
                    "Please choose a different directory."
                )
                lf.ui.request_redraw()
                return

        scene = lf.get_scene()
        if scene is None:
            self._tiles_error = "No scene loaded."
            return

        nodes = scene.get_nodes(type=lf_scene.NodeType.SPLAT)
        if self._export_splat_idx >= len(nodes):
            self._tiles_error = "Selected splat model not found."
            return

        node = nodes[self._export_splat_idx]
        self._tiles_progress = 0.0
        self._tiles_error    = None
        self._tiles_success  = None
        lf.ui.request_redraw()

        threading.Thread(
            target=self._export_tiles_worker,
            args=(node, dict(self._transform), str(out_dir)),
            daemon=True,
        ).start()

    def _on_tiles_progress(self, fraction: float) -> None:
        self._tiles_progress = fraction
        lf.ui.request_redraw()

    def _export_tiles_worker(self, node, transform: dict, out_dir: str) -> None:
        try:
            from ..geo.tiles_exporter import export_3dtiles
            export_3dtiles(
                node, transform, out_dir,
                sh_degree=3,
                progress_cb=self._on_tiles_progress,
            )
            lf.log.info(f"geo_register: 3D Tiles exported to '{out_dir}'")
            self._tiles_success = f"Export succeeded: {Path(out_dir).name}/"
        except Exception as exc:
            self._tiles_error = str(exc)
            lf.log.error(f"geo_register: 3D Tiles export failed: {exc}")
        finally:
            self._tiles_progress = None
            lf.ui.request_redraw()

    # ── PLY Converter (Edit Mode) ─────────────────────────────────────────────

    def _draw_ply_converter_section(self, layout, scale, theme) -> None:
        layout.label("PLY → Geo Export")
        layout.separator()
        layout.text_colored(
            "Geo Registration Plugin cannot align — no scene was detected.\n\n"
            "You can still convert a splat PLY file using a pre-calculated\n"
            "similarity matrix. Note: the similarity matrix must be calculated\n"
            "while a scene is still loaded in LichtFeld Studio.",
            (1.0, 0.75, 0.2, 1.0),
        )
        layout.spacing()
        layout.separator()
        layout.spacing()

        # PLY file
        layout.label("PLY File:")
        if self._ply_file_path:
            layout.text_colored(Path(self._ply_file_path).name, theme.palette.text_dim)
            if layout.button_styled("Change PLY File##ply_change", "warning", (-1, 28 * scale)):
                self._pick_ply_file()
        else:
            layout.text_colored("No file selected.", theme.palette.text_dim)
            if layout.button_styled("Pick PLY File##ply_pick", "primary", (-1, 32 * scale)):
                self._pick_ply_file()

        layout.spacing()

        # Similarity JSON
        layout.label("Similarity Transform JSON:")
        if self._ply_sim_path:
            layout.text_colored(Path(self._ply_sim_path).name, theme.palette.text_dim)
            if layout.button_styled("Change JSON##ply_sim_change", "warning", (-1, 28 * scale)):
                self._pick_ply_sim_file()
        else:
            layout.text_colored("No file selected.", theme.palette.text_dim)
            if layout.button_styled("Pick Similarity JSON##ply_sim_pick", "primary", (-1, 32 * scale)):
                self._pick_ply_sim_file()

        inputs_ready = self._ply_file_path is not None and self._ply_sim_path is not None

        if not inputs_ready:
            return

        layout.spacing()
        layout.separator()

        # Format
        layout.label("Format:")
        layout.same_line()
        fmt_changed, fmt_idx = layout.combo(
            "##ply_format", self._ply_format_idx, ["LAS", "LAZ", "3D Tiles (SPZ)"]
        )
        if fmt_changed:
            self._ply_format_idx = fmt_idx

        layout.spacing()

        # Output
        if self._ply_format_idx in (0, 1):
            if self._ply_out_file:
                layout.text_colored(self._ply_out_file, theme.palette.text_dim)
                layout.spacing()
        else:
            if self._ply_out_dir:
                layout.text_colored(self._ply_out_dir, theme.palette.text_dim)
                if layout.button_styled("Change Directory##ply_dir_change", "warning", (-1, 28 * scale)):
                    self._pick_ply_out_dir()
            else:
                layout.text_colored("No output directory selected.", theme.palette.text_dim)
                if layout.button_styled("Choose Output Directory##ply_dir_pick", "primary", (-1, 32 * scale)):
                    self._pick_ply_out_dir()
            layout.spacing()

        ready = self._ply_format_idx in (0, 1) or self._ply_out_dir is not None

        if self._ply_progress is not None:
            pct = int(self._ply_progress * 100)
            layout.progress_bar(self._ply_progress, overlay=f"Exporting... {pct}%",
                                width=-1, height=24 * scale)
        elif ready:
            if self._ply_error:
                layout.text_colored(f"[!] {self._ply_error}", (1.0, 0.4, 0.4, 1.0))
            if self._ply_success:
                layout.text_colored(self._ply_success, (0.3, 1.0, 0.3, 1.0))
            btn_label = ["Export LAS##ply_exp", "Export LAZ##ply_exp",
                         "Export 3D Tiles##ply_exp"][self._ply_format_idx]
            if layout.button_styled(btn_label, "primary", (-1, 32 * scale)):
                self._start_ply_export()
                lf.ui.request_redraw()

    def _pick_ply_file(self) -> None:
        start_dir = str(Path(self._ply_file_path).parent) if self._ply_file_path else ""
        path = lf.ui.open_ply_file_dialog(start_dir)
        if path:
            self._ply_file_path = path
            self._ply_error = None
            self._ply_success = None
            lf.ui.request_redraw()

    def _pick_ply_sim_file(self) -> None:
        path = lf.ui.open_json_file_dialog()
        if path:
            self._ply_sim_path = path
            self._ply_error = None
            self._ply_success = None
            lf.ui.request_redraw()

    def _pick_ply_out_dir(self) -> None:
        folder = lf.ui.open_folder_dialog(title="Select Output Directory")
        if folder:
            self._ply_out_dir = folder
            self._ply_error = None
            self._ply_success = None
            lf.ui.request_redraw()

    def _start_ply_export(self) -> None:
        import json
        import threading

        # Load similarity transform
        try:
            with open(self._ply_sim_path, "r", encoding="utf-8") as f:
                sim_data = json.load(f)
            transform = {
                "scale":       sim_data.get("scale",       sim_data.get("s")),
                "rotation":    sim_data.get("rotation",    sim_data.get("R")),
                "translation": sim_data.get("translation", sim_data.get("t")),
            }
            if any(v is None for v in transform.values()):
                self._ply_error = "Similarity JSON missing scale/rotation/translation."
                lf.ui.request_redraw()
                return
        except Exception as exc:
            self._ply_error = f"Failed to read similarity JSON: {exc}"
            lf.ui.request_redraw()
            return

        # For LAS/LAZ: open save dialog
        if self._ply_format_idx in (0, 1):
            splat_name = Path(self._ply_file_path).stem
            if self._ply_format_idx == 1:
                save_fn = getattr(lf.ui, "save_laz_file_dialog", None)
                if save_fn:
                    out_path = save_fn(default_name=splat_name)
                else:
                    out_path = lf.ui.save_las_file_dialog(default_name=splat_name)
                    if out_path:
                        out_path = str(Path(out_path).with_suffix(".laz"))
            else:
                out_path = lf.ui.save_las_file_dialog(default_name=splat_name)
            if not out_path:
                return
            self._ply_out_file = out_path
        else:
            # 3D Tiles: conflict check
            out_dir = Path(self._ply_out_dir)
            for fname in ("tileset.json", "splats.glb"):
                if (out_dir / fname).exists():
                    self._ply_error = (
                        f"{fname} already exists. Please choose a different directory."
                    )
                    lf.ui.request_redraw()
                    return
            out_path = str(out_dir)

        self._ply_progress = 0.0
        self._ply_error    = None
        self._ply_success  = None
        lf.ui.request_redraw()

        threading.Thread(
            target=self._ply_export_worker,
            args=(self._ply_file_path, transform, out_path, self._ply_format_idx),
            daemon=True,
        ).start()

    def _ply_export_worker(self, ply_path: str, transform: dict,
                           out_path: str, fmt_idx: int) -> None:
        try:
            if fmt_idx in (0, 1):
                from ..geo.las_exporter import export_las_from_ply
                export_las_from_ply(ply_path, transform, out_path,
                                    progress_cb=self._on_ply_progress)
                self._ply_success = f"Exported: {Path(out_path).name}"
                lf.log.info(f"geo_register: PLY→LAS exported to '{out_path}'")
            else:
                from ..geo.tiles_exporter import export_3dtiles as _tiles_exp
                from ..geo.tiles_exporter import _read_ply, _build_from_ply, _export_from_arrays
                from pathlib import Path as _P
                ply_data, names = _read_ply(_P(ply_path))
                positions, rotations_wxyz, scales_log, opacity_logit, f_dc, f_rest_rgb, eff_sh = (
                    _build_from_ply(ply_data, names, sh_degree=3)
                )
                _export_from_arrays(
                    positions_local=positions,
                    rotations_wxyz=rotations_wxyz,
                    scales_log=scales_log,
                    opacity_logit=opacity_logit,
                    f_dc=f_dc,
                    f_rest_rgb=f_rest_rgb,
                    sh_degree=eff_sh,
                    transform=transform,
                    out_dir=_P(out_path),
                    content_name="splats.glb",
                    progress_cb=self._on_ply_progress,
                )
                self._ply_success = f"Exported: {_P(out_path).name}/"
                lf.log.info(f"geo_register: PLY→3DTiles exported to '{out_path}'")
        except Exception as exc:
            self._ply_error = str(exc)
            lf.log.error(f"geo_register: PLY export failed: {exc}")
        finally:
            self._ply_progress = None
            lf.ui.request_redraw()

    def _on_ply_progress(self, fraction: float) -> None:
        self._ply_progress = fraction
        lf.ui.request_redraw()

    def _detect_existing_registration(self) -> None:
        import json
        from lfs_plugins.ui.state import AppState

        scene_path = AppState.scene_path.value
        if not scene_path:
            return

        params = lf.dataset_params()
        output_path = params.output_path if params else None
        base = Path(output_path) if output_path else Path(scene_path)
        candidate = base / "geo_register_plugin_data" / "similarity_transform.json"

        if not candidate.exists():
            return

        try:
            with open(candidate, "r", encoding="utf-8") as f:
                data = json.load(f)

            for key in ("scale", "rotation", "translation"):
                if key not in data:
                    return

            n = data.get("n_total", data.get("n_inliers", 0))
            self._transform = {
                "s":         data["scale"],
                "R":         data["rotation"],
                "t":         data["translation"],
                "rmse":      data.get("rmse_m", 0.0),
                "n":         n,
                "n_inliers": data.get("n_inliers", n),
                "n_total":   data.get("n_total",   n),
            }
            msg = "Detected pre existing registration"
            self._set_status(msg, error=False)
            lf.log.info(f"geo_register: {msg} from '{candidate}'")

        except Exception as exc:
            lf.log.warn(f"geo_register: failed to load existing transform: {exc}")

    def _set_status(self, message: str, *, error: bool) -> None:
        self._status          = message
        self._status_is_error = error
