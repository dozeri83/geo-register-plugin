"""Level-of-detail 3D Tiles export: SplatData -> temporary SSOG -> 3D Tiles.

LichtFeld Studio builds the LOD levels and the spatial chunk tree
(lf.io.save_ssog: decimation + k-d partition); ssog_tiles turns that tree
into a REPLACE-refined tileset. The temporary SSOG is always removed.

Stages reported through on_stage(index, count, message):
  1. export the temporary SSOG     (LFS, GPU)
  2. convert SSOG to 3D Tiles      (tiles + tileset.json)
  3. remove the temporary SSOG
"""
from __future__ import annotations

import math
import shutil
import time
import uuid
from dataclasses import dataclass, fields
from pathlib import Path

from .ssog_tiles import (DEFAULT_ERROR_SCALE, DEFAULT_MAX_TILE_SPLATS, ExportCancelled,
                         export_3dtiles_from_ssog)

STAGES = ("Exporting temporary SSOG", "Converting to 3D Tiles", "Removing temporary files")
# Share of the progress bar per stage (conversion dominates on large scenes).
_STAGE_SPAN = ((0.00, 0.35), (0.35, 0.98), (0.98, 1.00))


@dataclass
class LodTilesOptions:
    error_scale: float = DEFAULT_ERROR_SCALE   # geometricError multiplier
    lod_levels: int = 0                        # 0 = automatic (coarsest level ~1M splats)
    lod_ratio: float = 0.5                     # splats kept per coarser level
    # SSOG leaf cap (thousands, summed over LOD levels); also the 3D Tiles budget:
    # one leaf fits a tile at every level, coarser tiles group leaves up to it.
    chunk_count_k: int = DEFAULT_MAX_TILE_SPLATS // 1000
    chunk_extent_m: float = 16.0               # SSOG leaf extent in metres (converted to scene units)
    chunk_min_k: int = 8                       # SSOG: never split leaves below this
    max_sh_degree: int = 3

    def reset(self) -> None:
        for f in fields(self):
            setattr(self, f.name, f.default)


def auto_lod_levels(splat_count: int, coarsest_splats: int = 1_000_000) -> int:
    """Levels so the coarsest one (the first thing a viewer loads) holds ~1M splats."""
    if splat_count <= coarsest_splats:
        return 1
    return max(1, min(8, math.ceil(math.log2(splat_count / coarsest_splats)) + 1))


def clean_work_root(work_root) -> None:
    """Remove everything under the plugin's temporary export folder.

    Exports run one at a time, so anything left there is from an export that
    never reached its cleanup (crash, killed process).
    """
    root = Path(work_root)
    if not root.is_dir():
        return
    for entry in root.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)


def _splat_count(splat_data) -> int:
    visible = getattr(splat_data, "visible_count", None)
    if callable(visible):
        try:
            return int(visible())
        except TypeError:
            pass
    return int(splat_data.num_points)


def export_lod_3dtiles(
    splat_data,
    transform: dict,
    out_dir,
    work_root,
    *,
    options: LodTilesOptions | None = None,
    world_transform=None,
    on_stage=None,
    on_progress=None,
    log=print,
) -> dict:
    """Export splat_data as an LOD 3D Tiles tileset in out_dir.

    splat_data      : lichtfeld.scene.SplatData (dataset frame)
    transform       : plugin similarity {s,R,t} or {scale,rotation,translation}
    work_root       : folder for the temporary SSOG (removed afterwards)
    world_transform : optional 4x4 model -> dataset-frame node transform
    on_stage        : callable(index, count, message)
    on_progress     : callable(fraction 0..1); returning False cancels
    """
    import lichtfeld as lf

    opts = options or LodTilesOptions()
    t0 = time.time()
    scale = float(transform.get("scale", transform.get("s")))
    count = _splat_count(splat_data)
    levels = opts.lod_levels or auto_lod_levels(count)

    def stage(i: int, detail: str = "") -> None:
        message = STAGES[i] + (f": {detail}" if detail else "")
        log(f"[{i + 1}/{len(STAGES)}] {message}")
        if on_stage:
            on_stage(i, len(STAGES), message)

    def progress(i: int, fraction: float) -> bool:
        lo, hi = _STAGE_SPAN[i]
        if on_progress and on_progress(lo + (hi - lo) * min(max(fraction, 0.0), 1.0)) is False:
            return False
        return True

    # One folder per export: LFS stages the SSOG in a sibling "<name>.tmp-*"
    # folder, so removing work_dir also clears a cancelled or failed export.
    # Leftovers from a crashed or killed LFS are swept first.
    clean_work_root(work_root)
    work_dir = Path(work_root) / f"export_{uuid.uuid4().hex[:8]}"
    work_dir.mkdir(parents=True, exist_ok=True)
    tmp = work_dir / "ssog"
    out_dir = Path(out_dir)
    # The caller checked these do not exist, so on failure they are ours to remove.
    created = [out_dir / "tileset.json", out_dir / "tiles"]
    ok = False
    try:
        stage(0, f"{count:,} splats, {levels} LOD levels")
        lf.io.save_ssog(
            splat_data, str(tmp),
            lod_levels=levels, lod_ratio=opts.lod_ratio,
            chunk_count_k=opts.chunk_count_k,
            chunk_extent=opts.chunk_extent_m / scale,   # metres -> scene units
            chunk_min_k=opts.chunk_min_k,
            progress=lambda f, _stage: progress(0, f),
        )

        stage(1, f"max {opts.chunk_count_k}k splats per tile, error scale {opts.error_scale:g}")
        summary = export_3dtiles_from_ssog(
            tmp, transform, out_dir,
            max_tile_splats=opts.chunk_count_k * 1000,
            error_scale=opts.error_scale,
            max_sh_degree=opts.max_sh_degree,
            world_transform=world_transform,
            progress_cb=lambda f: progress(1, f),
            log=log,
        )
        ok = True
    except RuntimeError as exc:
        # save_ssog reports a False progress return as a failed export.
        if on_progress and "cancel" in str(exc).lower():
            raise ExportCancelled(str(exc)) from exc
        raise
    finally:
        stage(2)
        shutil.rmtree(work_dir, ignore_errors=True)
        if not ok:
            # No half-written tileset: it would also block the next export here.
            for path in created:
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                elif path.exists():
                    path.unlink(missing_ok=True)
        progress(2, 1.0)

    summary["lod_levels"] = levels
    summary["seconds"] = round(time.time() - t0, 1)
    return summary
