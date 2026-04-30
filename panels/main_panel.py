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

    _MODES     = ["EXIF", "Matrix File"]
    _MODE_KEYS = ["exif", "matrix"]

    def __init__(self):
        self._mode_idx: int           = 0
        self._status: str             = ""
        self._status_is_error         = False
        self._transform: dict | None  = None
        self._picking: bool           = False
        self._lla: tuple | None       = None   # (lat, lon, alt) from last pick
        self._world_pos: tuple | None = None   # local 3-D position of last pick

    @property
    def _mode(self) -> str:
        return self._MODE_KEYS[self._mode_idx]

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def on_scene_changed(self, doc):
        if self._picking:
            from ..operators.geo_picker import clear_pick_callback
            clear_pick_callback()
            lf.ui.ops.cancel_modal()
        self._mode_idx         = 0
        self._status           = ""
        self._status_is_error  = False
        self._transform        = None
        self._picking          = False
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
        else:
            self._draw_matrix_section(layout, theme)

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
        if layout.button_styled("Calc Georeference From EXIF", "primary", (-1, 32 * scale)):
            self._run_exif()

    def _draw_matrix_section(self, layout, theme):
        layout.text_colored("Similarity matrix file import -- coming soon.", theme.palette.text_dim)

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

    # ── Georeference pipeline ─────────────────────────────────────────────────

    def _run_exif(self):
        from lfs_plugins.ui.state import AppState
        from ..geo.exif_reader import find_images_with_gps, NoGPSDataError
        from ..geo.camera_reader import (
            read_camera_positions_from_scene,
            read_camera_positions,
            NoCameraDataError,
        )
        from ..geo.ecef import geodetic_to_ecef
        from ..geo.transform import robust_umeyama

        self._transform = None
        self._lla = None
        self._clear_point()

        scene_path = AppState.scene_path.value
        scene = lf.get_scene()
        if not scene_path or scene is None:
            self._set_status("No scene is currently loaded.", error=True)
            return

        lf.log.info(f"geo_register: scanning '{scene_path}' for GPS EXIF ...")

        try:
            gps_list = find_images_with_gps(scene_path)
        except NoGPSDataError as exc:
            self._set_status(str(exc), error=True)
            lf.log.warn(f"geo_register: {exc}")
            return
        except Exception as exc:
            self._set_status(f"EXIF scan error: {exc}", error=True)
            lf.log.error(f"geo_register: {exc}")
            return

        lf.log.info(f"geo_register: GPS found in {len(gps_list)} image(s).")

        cameras = read_camera_positions_from_scene(scene)
        if cameras:
            lf.log.info(f"geo_register: {len(cameras)} camera pose(s) read from scene.")
        else:
            lf.log.info("geo_register: no camera nodes in scene, falling back to dataset files ...")
            try:
                cameras = read_camera_positions(scene_path)
            except (NoCameraDataError, FileNotFoundError) as exc:
                self._set_status(str(exc), error=True)
                lf.log.warn(f"geo_register: {exc}")
                return
            lf.log.info(f"geo_register: {len(cameras)} camera pose(s) read from file.")

        src_pts: list = []
        dst_pts: list = []
        for entry in gps_list:
            name = Path(entry["path"]).name
            if name in cameras:
                src_pts.append(cameras[name])
                dst_pts.append(geodetic_to_ecef(entry["lat"], entry["lon"], entry["alt"]))

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
        n_in  = result.get("n_inliers", result["n"])
        n_tot = result.get("n_total",   result["n"])
        self._set_status(
            f"Ready -- {n_in}/{n_tot} inliers, RMSE {result['rmse']:.3f} m",
            error=False,
        )
        lf.log.info(
            f"geo_register: inliers={n_in}/{n_tot}  "
            f"scale={result['s']:.6f}  RMSE={result['rmse']:.3f} m"
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _set_status(self, message: str, *, error: bool) -> None:
        self._status          = message
        self._status_is_error = error
