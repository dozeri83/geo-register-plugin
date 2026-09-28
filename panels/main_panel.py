"""Main panel for the Geo Reference plugin."""
from pathlib import Path

import lichtfeld as lf

_OP_ID = "lfs_plugins.geo_register_pluggin.operators.geo_picker.GEO_OT_pick_location"

# Module-level world position so the draw handler can access it without a panel ref.
_active_world_pos: tuple | None = None

# The plugin's registered name (its package folder), used as its data folder name.
_PLUGIN_ID = __package__.split(".")[1] if __package__ and __package__.count(".") >= 1 else "geo_register_pluggin"


def _normalized(path) -> str:
    import os
    return os.path.normcase(os.path.abspath(str(path)))


def _camera_image_dir() -> str | None:
    """Common folder of the scene's camera images, or None without cameras."""
    import os
    import lichtfeld.scene as lf_scene

    scene = lf.get_scene()
    if scene is None:
        return None
    dirs = {os.path.dirname(n.image_path)
            for n in scene.get_nodes(type=lf_scene.NodeType.CAMERA) if n.image_path}
    if not dirs:
        return None
    try:
        return os.path.commonpath(list(dirs))
    except ValueError:  # images on different drives
        return None


def _dataset_path() -> str | None:
    """Dataset the scene's camera poses come from.

    Opening a .licht project leaves dataset_params() and AppState.scene_path
    empty, so fall back to the folder holding the camera images (minus the
    dataset's images subfolder, e.g. <dataset>/images -> <dataset>).
    """
    from lfs_plugins.ui.state import AppState

    params = lf.dataset_params()
    if params and params.data_path:
        return str(params.data_path)
    if AppState.scene_path.value:
        return str(AppState.scene_path.value)
    image_dir = _camera_image_dir()
    if not image_dir:
        return None
    images_sub = (params.images if params else None) or "images"
    image_dir = Path(image_dir)
    return str(image_dir.parent if image_dir.name.lower() == images_sub.lower() else image_dir)


def _plugin_root_dir() -> Path:
    """This plugin's durable data folder, as defined by LFS.

    lf.plugins.data_dir() gives <lfs data>/plugin_data/<plugin>/; older LFS
    builds without it get the same layout under the LFS user home.
    """
    data_dir = getattr(lf.plugins, "data_dir", None)
    if data_dir:
        return Path(data_dir(_PLUGIN_ID))
    try:
        from lfs_plugins.asset_storage import lichtfeld_home
        home = Path(lichtfeld_home())
    except Exception:
        home = Path.home() / ".lichtfeld"
    return home / "data" / "plugin_data" / _PLUGIN_ID


def _project_uuid() -> str | None:
    get_uuid = getattr(lf, "project_uuid", None)  # absent before LFS exposed it
    return get_uuid() if get_uuid else None


def _data_key() -> str | None:
    """<project file stem>_<short project UUID>.

    The UUID survives saves, renames and Save As, so a model-only reopen of the
    project still finds its registration. Unsaved projects use 'untitled'.
    LFS builds without the project API fall back to the dataset folder name
    plus a short hash of its path.
    """
    import hashlib

    uuid = _project_uuid()
    if uuid:
        get_path = getattr(lf, "project_path", None)
        path = get_path() if get_path else None
        stem = Path(path).stem if path else "untitled"
        return f"{stem}_{uuid[:8]}"
    dataset = _dataset_path()
    if not dataset:
        return None
    digest = hashlib.sha1(_normalized(dataset).encode("utf-8")).hexdigest()[:8]
    return f"{Path(dataset).name or 'dataset'}_{digest}"


def _plugin_data_dir() -> Path | None:
    """<plugin data dir>/<project stem>_<short uuid>/"""
    key = _data_key()
    return _plugin_root_dir() / key if key else None


def _existing_outputs(out_dir, lod: bool) -> list[str]:
    """3D Tiles outputs that already exist in out_dir; an export would overwrite them."""
    if not out_dir:
        return []
    names = ("tileset.json", "tiles") if lod else ("tileset.json", "splats.glb")
    return [name for name in names if (Path(out_dir) / name).exists()]


def _draw_output_conflict(layout, theme, conflicts: list[str], just_exported: bool) -> None:
    """Replaces the Export button while the output directory holds export files."""
    # Files from the export that just succeeded: its success line says enough.
    if not just_exported:
        layout.text_colored(
            f"[!] Already in this directory: {', '.join(conflicts)}. "
            "Choose another directory or remove them.",
            (1.0, 0.4, 0.4, 1.0),
        )


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
    # LFS panels default to dirty-driven redraws, which only follow panel input
    # and scene changes. Picks (modal operator) and export progress (worker
    # threads) change state outside the panel, so redraw on an interval.
    update_policy      = "interval"
    update_interval_ms = 100

    _MODES     = ["EXIF", "Similarity File", "Image Positions CSV", "RealityScan Parameters CSV", "Metashape Cameras XML"]
    _MODE_KEYS = ["exif", "similarity", "csv", "rs_csv", "metashape_xml"]

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
        self._tiles_max_sh: int              = 3
        self._tiles_sh_info: tuple | None    = None   # (detected, user_bound, output)
        # Level-of-detail 3D Tiles (temporary SSOG -> tileset)
        from ..geo.lod_tiles import LodTilesOptions
        self._tiles_lod: bool                = True
        self._lod_opts                       = LodTilesOptions()
        self._tiles_stage: str | None        = None   # e.g. "2/3 Converting to 3D Tiles: ..."
        self._tiles_cancel: bool             = False
        # PLY converter (Edit Mode)
        self._ply_file_path: str | None      = None
        self._ply_sim_path: str | None       = None
        self._ply_format_idx: int            = 2      # default to 3D Tiles
        self._ply_out_file: str | None       = None
        self._ply_out_dir: str | None        = None
        self._ply_progress: float | None     = None
        self._ply_error: str | None          = None
        self._ply_success: str | None        = None
        self._ply_max_sh: int                = 3
        self._ply_sh_info: tuple | None      = None   # (detected, user_bound, output)
        self._ply_lod: bool                  = True   # LOD tileset; shares self._lod_opts
        self._ply_stage: str | None          = None
        self._ply_cancel: bool               = False

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
        self._tiles_stage         = None
        self._ply_progress        = None
        self._ply_error           = None
        self._ply_success         = None
        self._ply_stage           = None
        self._clear_point()
        if doc is not None:
            self._detect_existing_registration()

    # ── Draw ──────────────────────────────────────────────────────────────────

    def draw(self, layout):
        scale = layout.get_dpi_scale()
        theme = lf.ui.theme()

        # If no cameras are present the user has switched to Edit Mode and the
        # dataset (including camera data) has been discarded.  Solving needs
        # cameras, but a registration saved for this project still applies to
        # its splats, so offer picking and export before the PLY converter.
        scene = lf.get_scene()
        if scene is not None:
            import lichtfeld.scene as lf_scene
            has_cameras = any(True for _ in scene.get_nodes(type=lf_scene.NodeType.CAMERA))
            if not has_cameras:
                if self._transform is not None:
                    layout.label("Geo Reference (saved for this project)")
                    if self._status:
                        prefix = "[!] " if self._status_is_error else "[ok] "
                        color  = (1.0, 0.4, 0.4, 1.0) if self._status_is_error else (0.4, 1.0, 0.4, 1.0)
                        layout.text_colored(prefix + self._status, color)
                    self._draw_transform_section(layout, scale, theme)
                    self._draw_export_section(layout, scale, theme)
                else:
                    layout.text_colored(
                        "No scene with cameras is loaded, so a geo reference cannot be computed.",
                        (1.0, 0.75, 0.2, 1.0),
                    )
                # Without a registration the converter is all this mode offers.
                self._draw_ply_converter_section(layout, scale, theme,
                                                 default_open=self._transform is None)
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
        elif self._mode == "rs_csv":
            self._draw_rs_csv_section(layout, scale, theme)
        else:
            self._draw_metashape_xml_section(layout, scale, theme)

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

        self._draw_ply_converter_section(layout, scale, theme)

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
        from ..geo.exif_reader import find_images_with_gps, NoGPSDataError

        self._transform = None
        self._lla = None
        self._clear_point()

        if lf.get_scene() is None:
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
                # Project (.licht) loads: scan where the camera images live.
                scan_folder = _camera_image_dir() or _dataset_path()
            if not scan_folder:
                self._set_status("Cannot find the dataset images. Use Set Original Images Folder.", error=True)
                return
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
        from ..geo.camera_reader import read_camera_positions_from_scene
        from ..geo.ecef import geodetic_to_ecef
        from ..geo.transform import robust_umeyama

        scene = lf.get_scene()
        if scene is None:
            self._set_status("No scene is currently loaded.", error=True)
            return
        out_dir = _plugin_data_dir()
        if out_dir is None:
            self._set_status("Cannot determine where to store the registration (no project or dataset).", error=True)
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
        saved_json, saved_csv = self._save_transform(result, out_dir, matched_gps)
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

    def _draw_metashape_xml_section(self, layout, scale, theme):
        layout.text_colored(
            "Load a Metashape camera XML export.\n"
            "Chunk CRS must be GEOGCS/EPSG:4326.\n"
            "Camera GPS positions are read from\n"
            "the reference tag of each camera.",
            theme.palette.text_dim,
        )
        layout.spacing()
        if layout.button_styled("Load Metashape Cameras XML", "primary", (-1, 32 * scale)):
            self._load_metashape_xml_file()

    def _load_metashape_xml_file(self) -> None:
        from ..geo.metashape_parser import parse_metashape_xml, MetashapeXMLError

        path = lf.ui.open_xml_file_dialog()
        if not path:
            return

        self._transform = None
        self._lla = None
        self._clear_point()

        try:
            gps_list = parse_metashape_xml(path)
        except MetashapeXMLError as exc:
            self._set_status(str(exc), error=True)
            lf.log.error(f"geo_register: {exc}")
            return
        except Exception as exc:
            self._set_status(f"Failed to parse Metashape XML: {exc}", error=True)
            lf.log.error(f"geo_register: {exc}")
            return

        if not gps_list:
            self._set_status(
                "No cameras with GPS reference found in Metashape XML.", error=True
            )
            lf.log.warn("geo_register: Metashape XML contained no cameras with <reference> data.")
            return

        lf.log.info(f"geo_register: loaded {len(gps_list)} camera positions from Metashape XML '{path}'")
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

            # Copy to the project's plugin data dir if the file is not already there
            copied_to = None
            expected_dir = _plugin_data_dir()
            if expected_dir is not None:
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

    def _save_transform(self, result: dict, out_dir: Path, matched_gps: list | None = None) -> tuple:
        import json
        import csv

        out_dir.mkdir(parents=True, exist_ok=True)

        payload = {
            "scale": result["s"],
            "rotation": result["R"],
            "translation": result["t"],
            "rmse_m": result["rmse"],
            "n_inliers": result.get("n_inliers", result["n"]),
            "n_total": result.get("n_total", result["n"]),
            "project_uuid": _project_uuid(),
            "dataset_path": _dataset_path(),
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

        if self._tiles_sh_info is not None:
            detected, user_bound, output = self._tiles_sh_info
            dim = theme.palette.text_dim
            layout.text_colored(f"Detected SH Degree:  {detected}", dim)
            layout.text_colored(f"User Bound Degree:   {user_bound}", dim)
            layout.text_colored(f"Output SH Degree:    {output}", dim)
            layout.spacing()

        if self._tiles_progress is not None:
            if self._tiles_stage:
                layout.text_colored(self._tiles_stage, (0.55, 0.8, 1.0, 1.0))
            pct = int(self._tiles_progress * 100)
            layout.progress_bar(self._tiles_progress, overlay=f"Exporting... {pct}%",
                                width=-1, height=24 * scale)
            if self._tiles_lod and not self._tiles_cancel:
                if layout.button_styled("Cancel##tiles_cancel", "error", (-1, 0)):
                    self._tiles_cancel = True
                    self._tiles_stage = "Cancelling..."
            elif self._tiles_cancel:
                layout.text_colored("Cancelling...", theme.palette.text_dim)
        else:
            if self._tiles_error:
                layout.text_colored(f"[!] {self._tiles_error}", (1.0, 0.4, 0.4, 1.0))
            if self._tiles_success:
                layout.text_colored(self._tiles_success, (0.3, 1.0, 0.3, 1.0))
            if not splat_names:
                layout.text_colored("No splat models found in scene.", theme.palette.text_dim)
                return
            # Options first; only the export itself needs an output directory.
            layout.label("Max SH:")
            layout.same_line()
            sh_changed, sh_idx = layout.combo(
                "##tiles_max_sh", 3 - self._tiles_max_sh, ["3", "2", "1", "0"]
            )
            if sh_changed:
                self._tiles_max_sh = 3 - sh_idx
            lod_changed, lod = layout.checkbox("Level of detail (LOD)##tiles_lod", self._tiles_lod)
            if lod_changed:
                self._tiles_lod = lod
            if self._tiles_lod:
                self._draw_lod_settings(layout, scale, theme)
            else:
                layout.text_colored("Single tile: the whole model in one GLB.", theme.palette.text_dim)
            # Keep Export well apart from the LOD "Reset to defaults" button.
            layout.spacing()
            layout.separator()
            layout.spacing()
            conflicts = _existing_outputs(self._tiles_out_dir, self._tiles_lod)
            if not self._tiles_out_dir:
                layout.text_colored("Choose an output directory to export.", theme.palette.text_dim)
            elif conflicts:
                # Shown in place of the Export button, so nothing is overwritten.
                _draw_output_conflict(layout, theme, conflicts, just_exported=bool(self._tiles_success))
            elif layout.button_styled("Export 3D Tiles##export_tiles_btn", "primary", (-1, 32 * scale)):
                self._start_export_tiles()
                lf.ui.request_redraw()

    def _draw_lod_settings(self, layout, scale, theme) -> None:
        """Editable LOD 3D Tiles parameters, collapsed by default."""
        opts = self._lod_opts
        if not layout.collapsing_header("LOD settings##lod_settings", True):
            levels = "auto" if opts.lod_levels == 0 else str(opts.lod_levels)
            layout.text_colored(
                f"{opts.chunk_count_k}k splats/chunk, error x{opts.error_scale:g}, {levels} levels",
                theme.palette.text_dim,
            )
            return
        dim = theme.palette.text_dim
        layout.text_colored("See the plugin README (3D Tiles > LOD tileset) for details.", dim)

        layout.text_colored("3D Tiles", dim)
        ch, v = layout.input_float("Error scale##lod_err", opts.error_scale, 1.0, 4.0, "%.1f")
        if ch:
            opts.error_scale = max(0.1, min(1000.0, v))

        layout.text_colored("LOD levels (temporary SSOG)", dim)
        ch, idx = layout.combo("Levels##lod_levels", opts.lod_levels,
                               ["Auto"] + [str(i) for i in range(1, 9)])
        if ch:
            opts.lod_levels = idx
        ch, v = layout.input_float("Level ratio##lod_ratio", opts.lod_ratio, 0.05, 0.1, "%.2f")
        if ch:
            opts.lod_ratio = max(0.1, min(0.9, v))

        layout.text_colored("Chunks (temporary SSOG; chunk splats is also the tile budget)", dim)
        ch, v = layout.input_int("Chunk splats (K)##lod_chunk_k", opts.chunk_count_k, 10, 100)
        if ch:
            opts.chunk_count_k = max(1, min(2048, v))
        ch, v = layout.input_float("Chunk extent (m)##lod_chunk_m", opts.chunk_extent_m, 1.0, 8.0, "%.1f")
        if ch:
            opts.chunk_extent_m = max(0.5, min(1000.0, v))
        ch, v = layout.input_int("Min chunk splats (K)##lod_chunk_min", opts.chunk_min_k, 1, 8)
        if ch:
            opts.chunk_min_k = max(0, min(1024, v))

        if layout.button_styled("Reset to defaults##lod_reset", "warning", (-1, 0)):
            opts.reset()

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

        import dataclasses

        out_dir = Path(self._tiles_out_dir)

        # Conflict check (the panel already hides Export; files may appear since)
        conflicts = _existing_outputs(out_dir, self._tiles_lod)
        if conflicts:
            self._tiles_error = (
                f"{', '.join(conflicts)} already exists in the selected directory. "
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
        self._tiles_sh_info  = None
        self._tiles_stage    = None
        self._tiles_cancel   = False
        lf.ui.request_redraw()

        if self._tiles_lod:
            options = dataclasses.replace(self._lod_opts, max_sh_degree=self._tiles_max_sh)
            threading.Thread(
                target=self._export_tiles_lod_worker,
                args=(node, dict(self._transform), str(out_dir), options),
                daemon=True,
            ).start()
            return

        threading.Thread(
            target=self._export_tiles_worker,
            args=(node, dict(self._transform), str(out_dir), self._tiles_max_sh),
            daemon=True,
        ).start()

    def _on_tiles_progress(self, fraction: float) -> bool:
        self._tiles_progress = fraction
        lf.ui.request_redraw()
        return not self._tiles_cancel

    def _on_tiles_stage(self, index: int, count: int, message: str) -> None:
        self._tiles_stage = f"Step {index + 1}/{count}: {message}"
        lf.log.info(f"geo_register: 3D Tiles {self._tiles_stage}")
        lf.ui.request_redraw()

    def _export_tiles_lod_worker(self, node, transform: dict, out_dir: str, options) -> None:
        import numpy as np
        from ..geo.lod_tiles import export_lod_3dtiles
        from ..geo.ssog_tiles import ExportCancelled
        from ..geo.tiles_exporter import DIM_FOR_DEGREE

        try:
            splat_data = node.splat_data()
            if splat_data is None:
                raise RuntimeError("Selected node has no splat data.")
            try:
                sh_raw = splat_data.shN_raw
                k = int(sh_raw.shape[1]) if sh_raw.ndim == 3 else 0
                actual_sh = max(d for d in range(4) if DIM_FOR_DEGREE[d] <= k)
            except Exception:
                actual_sh = 3
            self._tiles_sh_info = (actual_sh, options.max_sh_degree, min(options.max_sh_degree, actual_sh))

            summary = export_lod_3dtiles(
                splat_data, transform, out_dir,
                work_root=_plugin_root_dir() / "tmp",
                options=options,
                world_transform=np.asarray(node.world_transform, dtype=np.float64).reshape(4, 4),
                on_stage=self._on_tiles_stage,
                on_progress=self._on_tiles_progress,
                log=lambda m: lf.log.info(f"geo_register: {m}"),
            )
            minutes, seconds = divmod(int(summary["seconds"]), 60)
            self._tiles_success = (
                f"Export succeeded: {summary['tiles']} tiles, {summary['lod_levels']} LOD levels, "
                f"{summary['bytes'] / 2**30:.2f} GiB in {minutes}:{seconds:02d}"
            )
            lf.log.info(f"geo_register: {self._tiles_success} -> '{out_dir}'")
        except ExportCancelled:
            self._tiles_error = "Export cancelled."
            lf.log.warn("geo_register: 3D Tiles export cancelled")
        except Exception as exc:
            self._tiles_error = "Export cancelled." if self._tiles_cancel else str(exc)
            lf.log.error(f"geo_register: 3D Tiles export failed: {exc}")
        finally:
            self._tiles_progress = None
            self._tiles_stage = None
            lf.ui.request_redraw()

    def _export_tiles_worker(self, node, transform: dict, out_dir: str, max_sh: int = 3) -> None:
        try:
            from ..geo.tiles_exporter import export_3dtiles, DIM_FOR_DEGREE
            try:
                splat_data = node.splat_data()
                sh_raw = splat_data.shN_raw
                k = int(sh_raw.shape[1]) if sh_raw.ndim == 3 else 0
                actual_sh = max(d for d in range(4) if DIM_FOR_DEGREE[d] <= k)
            except Exception:
                actual_sh = 3
            effective_sh = min(max_sh, actual_sh)
            self._tiles_sh_info = (actual_sh, max_sh, effective_sh)
            lf.ui.request_redraw()
            export_3dtiles(
                node, transform, out_dir,
                sh_degree=effective_sh,
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

    # ── PLY Converter (all modes) ─────────────────────────────────────────────

    def _draw_ply_converter_section(self, layout, scale, theme, default_open: bool = False) -> None:
        # A registration found for this project replaces the similarity JSON.
        use_saved = self._transform is not None
        layout.separator()
        if not layout.collapsing_header("Convert external PLY##ply_converter", default_open):
            return
        if not use_saved:
            layout.text_colored(
                "Converts a splat PLY file using a similarity JSON saved\n"
                "earlier by this plugin (computed while the scene was loaded).",
                theme.palette.text_dim,
            )
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
        if use_saved:
            layout.text_colored("Using this project's saved registration.", theme.palette.text_dim)
        else:
            layout.label("Similarity Transform JSON:")
            if self._ply_sim_path:
                layout.text_colored(Path(self._ply_sim_path).name, theme.palette.text_dim)
                if layout.button_styled("Change JSON##ply_sim_change", "warning", (-1, 28 * scale)):
                    self._pick_ply_sim_file()
            else:
                layout.text_colored("No file selected.", theme.palette.text_dim)
                if layout.button_styled("Pick Similarity JSON##ply_sim_pick", "primary", (-1, 32 * scale)):
                    self._pick_ply_sim_file()

        inputs_ready = self._ply_file_path is not None and (use_saved or self._ply_sim_path is not None)

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

        if self._ply_format_idx == 2:
            layout.spacing()
            layout.label("Max SH:")
            layout.same_line()
            sh_changed, sh_idx = layout.combo(
                "##ply_max_sh", 3 - self._ply_max_sh, ["3", "2", "1", "0"]
            )
            if sh_changed:
                self._ply_max_sh = 3 - sh_idx
            lod_changed, lod = layout.checkbox("Level of detail (LOD)##ply_lod", self._ply_lod)
            if lod_changed:
                self._ply_lod = lod
            if self._ply_lod:
                # Same settings as the scene export; own ids for the widgets.
                layout.push_id("ply")
                self._draw_lod_settings(layout, scale, theme)
                layout.pop_id()
            else:
                layout.text_colored("Single tile: the whole model in one GLB.", theme.palette.text_dim)

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

        if self._ply_format_idx == 2 and self._ply_sh_info is not None:
            detected, user_bound, output = self._ply_sh_info
            dim = theme.palette.text_dim
            layout.text_colored(f"Detected SH Degree:  {detected}", dim)
            layout.text_colored(f"User Bound Degree:   {user_bound}", dim)
            layout.text_colored(f"Output SH Degree:    {output}", dim)
            layout.spacing()

        if self._ply_progress is not None:
            if self._ply_stage:
                layout.text_colored(self._ply_stage, (0.55, 0.8, 1.0, 1.0))
            pct = int(self._ply_progress * 100)
            layout.progress_bar(self._ply_progress, overlay=f"Exporting... {pct}%",
                                width=-1, height=24 * scale)
            if self._ply_stage is not None and not self._ply_cancel:
                if layout.button_styled("Cancel##ply_cancel", "error", (-1, 0)):
                    self._ply_cancel = True
                    self._ply_stage = "Cancelling..."
        elif ready:
            if self._ply_error:
                layout.text_colored(f"[!] {self._ply_error}", (1.0, 0.4, 0.4, 1.0))
            if self._ply_success:
                layout.text_colored(self._ply_success, (0.3, 1.0, 0.3, 1.0))
            btn_label = ["Export LAS##ply_exp", "Export LAZ##ply_exp",
                         "Export 3D Tiles##ply_exp"][self._ply_format_idx]
            conflicts = (_existing_outputs(self._ply_out_dir, lod=self._ply_lod)
                         if self._ply_format_idx == 2 else [])
            if conflicts:
                _draw_output_conflict(layout, theme, conflicts, just_exported=bool(self._ply_success))
            elif layout.button_styled(btn_label, "primary", (-1, 32 * scale)):
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

        # Load similarity transform (saved registration first, else the picked JSON)
        try:
            if self._transform is not None:
                sim_data = self._transform
            else:
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
            conflicts = _existing_outputs(out_dir, lod=self._ply_lod)
            if conflicts:
                self._ply_error = (
                    f"{', '.join(conflicts)} already exists. Please choose a different directory."
                )
                lf.ui.request_redraw()
                return
            out_path = str(out_dir)

        self._ply_progress = 0.0
        self._ply_error    = None
        self._ply_success  = None
        self._ply_sh_info  = None
        self._ply_stage    = None
        self._ply_cancel   = False
        lf.ui.request_redraw()

        if self._ply_format_idx == 2 and self._ply_lod:
            import dataclasses
            options = dataclasses.replace(self._lod_opts, max_sh_degree=self._ply_max_sh)
            threading.Thread(
                target=self._ply_export_lod_worker,
                args=(self._ply_file_path, transform, out_path, options),
                daemon=True,
            ).start()
            return

        threading.Thread(
            target=self._ply_export_worker,
            args=(self._ply_file_path, transform, out_path, self._ply_format_idx,
                  self._ply_max_sh),
            daemon=True,
        ).start()

    def _ply_export_worker(self, ply_path: str, transform: dict,
                           out_path: str, fmt_idx: int, max_sh: int = 3) -> None:
        try:
            if fmt_idx in (0, 1):
                from ..geo.las_exporter import export_las_from_ply
                export_las_from_ply(ply_path, transform, out_path,
                                    progress_cb=self._on_ply_progress)
                self._ply_success = f"Exported: {Path(out_path).name}"
                lf.log.info(f"geo_register: PLY→LAS exported to '{out_path}'")
            else:
                from ..geo.tiles_exporter import _read_ply, _build_from_ply, _export_from_arrays, DIM_FOR_DEGREE
                from pathlib import Path as _P
                ply_data, names = _read_ply(_P(ply_path))
                # f_rest_* props are flat RGB triples, so /3 gives per-channel coef count.
                # Find the highest complete SH degree the PLY contains, then cap at user's choice.
                rest_names = [nm for nm in names if nm.startswith("f_rest_")]
                k = len(rest_names) // 3 if rest_names and len(rest_names) % 3 == 0 else 0
                actual_sh = max(d for d in range(4) if DIM_FOR_DEGREE[d] <= k)
                effective_sh = min(max_sh, actual_sh)
                self._ply_sh_info = (actual_sh, max_sh, effective_sh)
                lf.ui.request_redraw()
                positions, rotations_wxyz, scales_log, opacity_logit, f_dc, f_rest_rgb, eff_sh = (
                    _build_from_ply(ply_data, names, sh_degree=effective_sh)
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

    def _ply_export_lod_worker(self, ply_path: str, transform: dict, out_dir: str, options) -> None:
        from ..geo.lod_tiles import export_lod_3dtiles
        from ..geo.ssog_tiles import ExportCancelled
        from ..geo.tiles_exporter import DIM_FOR_DEGREE

        splat_data = None
        try:
            # The PLY is in the dataset frame the similarity was computed for.
            self._ply_stage = f"Loading {Path(ply_path).name}..."
            lf.ui.request_redraw()
            splat_data = lf.io.load(ply_path).splat_data
            if splat_data is None:
                raise RuntimeError("The PLY file holds no splats.")
            if self._ply_cancel:
                raise ExportCancelled("cancelled while loading the PLY")
            try:
                sh_raw = splat_data.shN_raw
                k = int(sh_raw.shape[1]) if sh_raw.ndim == 3 else 0
                actual_sh = max(d for d in range(4) if DIM_FOR_DEGREE[d] <= k)
            except Exception:
                actual_sh = 3
            self._ply_sh_info = (actual_sh, options.max_sh_degree, min(options.max_sh_degree, actual_sh))

            summary = export_lod_3dtiles(
                splat_data, transform, out_dir,
                work_root=_plugin_root_dir() / "tmp",
                options=options,
                on_stage=self._on_ply_stage,
                on_progress=self._on_ply_progress,
                log=lambda m: lf.log.info(f"geo_register: {m}"),
            )
            minutes, seconds = divmod(int(summary["seconds"]), 60)
            self._ply_success = (
                f"Export succeeded: {summary['tiles']} tiles, {summary['lod_levels']} LOD levels, "
                f"{summary['bytes'] / 2**30:.2f} GiB in {minutes}:{seconds:02d}"
            )
            lf.log.info(f"geo_register: PLY {self._ply_success} -> '{out_dir}'")
        except ExportCancelled:
            self._ply_error = "Export cancelled."
            lf.log.warn("geo_register: PLY 3D Tiles export cancelled")
        except Exception as exc:
            self._ply_error = "Export cancelled." if self._ply_cancel else str(exc)
            lf.log.error(f"geo_register: PLY 3D Tiles export failed: {exc}")
        finally:
            del splat_data  # release the loaded model's memory
            self._ply_progress = None
            self._ply_stage = None
            lf.ui.request_redraw()

    def _on_ply_stage(self, index: int, count: int, message: str) -> None:
        self._ply_stage = f"Step {index + 1}/{count}: {message}"
        lf.log.info(f"geo_register: PLY 3D Tiles {self._ply_stage}")
        lf.ui.request_redraw()

    def _on_ply_progress(self, fraction: float) -> bool:
        self._ply_progress = fraction
        lf.ui.request_redraw()
        return not self._ply_cancel

    def _detect_existing_registration(self) -> None:
        import json

        data_dir = _plugin_data_dir()
        if data_dir is None:
            return
        candidate = data_dir / "similarity_transform.json"

        if not candidate.exists():
            return

        try:
            with open(candidate, "r", encoding="utf-8") as f:
                data = json.load(f)

            for key in ("scale", "rotation", "translation"):
                if key not in data:
                    return

            # The folder key only holds a short UUID; the full one guards collisions.
            stored, current = data.get("project_uuid"), _project_uuid()
            if stored and current and stored != current:
                lf.log.warn(f"geo_register: ignoring '{candidate}', it belongs to project {stored}")
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
