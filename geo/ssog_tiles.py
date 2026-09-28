"""Convert a streamed SOG (lod-meta.json + SOG units) to a 3D Tiles 1.1 tileset.

The SSOG already holds what a hierarchical tileset needs: an adaptive spatial
tree with bounds, and coarse-to-fine LOD levels stored per leaf. This module
maps them onto 3D Tiles with REPLACE refinement:

  * A tile is (tree node, level). Its content is that level's splats from
    every leaf below the node, so parents and children cover exactly the same
    region and REPLACE never double-draws or leaves holes.
  * Level k tiles sit at the shallowest tree nodes whose level-k count fits
    `max_tile_splats`; each level splits further down the tree than the
    coarser one, because every level holds about twice the splats.
  * The coarsest level is the root content; level 0 (full detail) tiles are
    the leaves and have geometricError 0.
  * geometricError of a level-k tile is the median splat diameter of its
    content: the detail it cannot show that its children can.
  * One root transform (similarity -> ECEF, viewer y/z flip applied, same
    convention as tiles_exporter) positions the whole tree on the globe.

Only the folder form (lod-meta.json + unit folders) is read; bundled .ssog
archives must be extracted first.

CLI:
    python -m geo.ssog_tiles <ssog dir or lod-meta.json> <similarity.json> <out_dir>
        [--max-tile-splats 100000] [--error-scale 16] [--max-sh-degree 3]
"""
from __future__ import annotations

import json
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .sog_decoder import SH_COEFFS, SogSplats, read_sog_unit
from .tiles_exporter import aabb_box, root_transform, tileset_header, write_splat_glb

# Tuned on the Poland JG scene (106M splats) in ArcGIS Maps SDK 5.0: 100k-splat
# tiles keep ArcGIS within its splat memory while refining near the camera,
# and x16 errors make it refine once coarse splats reach about 1 px.
DEFAULT_MAX_TILE_SPLATS = 100_000
DEFAULT_ERROR_SCALE = 16.0


class ExportCancelled(RuntimeError):
    pass


# ─── Manifest tree ────────────────────────────────────────────────────────────

@dataclass
class _Node:
    bmin: np.ndarray
    bmax: np.ndarray
    children: list["_Node"] = field(default_factory=list)
    lods: dict[int, tuple[int, int, int]] = field(default_factory=dict)  # level -> (file, offset, count)
    counts: np.ndarray | None = None  # splats per level in this subtree
    leaves: list["_Node"] = field(default_factory=list)  # leaves in DFS order


def _parse_tree(node_json: dict, levels: int) -> _Node:
    b = node_json["bound"]
    node = _Node(np.asarray(b["min"], dtype=np.float64), np.asarray(b["max"], dtype=np.float64))
    if "children" in node_json:
        node.children = [_parse_tree(c, levels) for c in node_json["children"]]
        node.counts = np.sum([c.counts for c in node.children], axis=0)
        node.leaves = [leaf for c in node.children for leaf in c.leaves]
    else:
        node.lods = {int(k): (int(v["file"]), int(v["offset"]), int(v["count"]))
                     for k, v in node_json.get("lods", {}).items()}
        node.counts = np.zeros(levels, dtype=np.int64)
        for level, (_, _, count) in node.lods.items():
            node.counts[level] = count
        node.leaves = [node]
    return node


def _cover(node: _Node, level: int, budget: int) -> list[_Node]:
    """Shallowest descendants of `node` whose level count fits the budget."""
    if node.counts[level] <= budget or not node.children:
        return [node]
    return [n for c in node.children for n in _cover(c, level, budget)]


# ─── Tile plan ────────────────────────────────────────────────────────────────

@dataclass
class _Tile:
    node: _Node
    level: int
    uri: str | None = None
    children: list["_Tile"] = field(default_factory=list)
    geometric_error: float = 0.0


def _plan(node: _Node, level: int, budget: int, counter: list[int]) -> _Tile:
    tile = _Tile(node, level)
    if node.counts[level] > 0:
        counter[0] += 1
        tile.uri = f"tiles/L{level}/{counter[0]:06d}.glb"
    if level > 0:
        tile.children = [_plan(n, level - 1, budget, counter)
                         for n in _cover(node, level - 1, budget) if n.counts[level - 1] > 0]
    return tile


def _walk(tile: _Tile):
    yield tile
    for c in tile.children:
        yield from _walk(c)


# ─── Unit cache ───────────────────────────────────────────────────────────────

class _UnitCache:
    """Small LRU of decoded SOG units; tiles of one level read their files in order."""

    def __init__(self, root: Path, filenames: list[str], capacity: int = 6):
        self._root, self._files, self._cap = root, filenames, capacity
        self._cache: OrderedDict[int, SogSplats] = OrderedDict()

    def get(self, index: int) -> SogSplats:
        if index in self._cache:
            self._cache.move_to_end(index)
            return self._cache[index]
        unit = read_sog_unit(self._root / self._files[index])
        self._cache[index] = unit
        if len(self._cache) > self._cap:
            self._cache.popitem(last=False)
        return unit


def _gather(node: _Node, level: int, units: _UnitCache) -> SogSplats:
    parts = []
    for leaf in node.leaves:
        ref = leaf.lods.get(level)
        if ref and ref[2] > 0:
            parts.append(units.get(ref[0]).take(ref[1], ref[2]))
    return SogSplats.concat(parts)


# ─── Public API ───────────────────────────────────────────────────────────────

def _load_similarity(transform) -> dict:
    if isinstance(transform, (str, Path)):
        transform = json.loads(Path(transform).read_text(encoding="utf-8"))
    return {
        "scale":       transform.get("scale",       transform.get("s")),
        "rotation":    transform.get("rotation",    transform.get("R")),
        "translation": transform.get("translation", transform.get("t")),
    }


def export_3dtiles_from_ssog(
    ssog_path,
    transform,
    out_dir,
    *,
    max_tile_splats: int = DEFAULT_MAX_TILE_SPLATS,
    error_scale: float = DEFAULT_ERROR_SCALE,
    max_sh_degree: int = 3,
    world_transform: np.ndarray | None = None,
    progress_cb=None,
    log=print,
) -> dict:
    """Write <out_dir>/tileset.json + <out_dir>/tiles/L<level>/*.glb.

    ssog_path       : SSOG folder or its lod-meta.json
    transform       : similarity dict or JSON path (scale/rotation/translation),
                      mapping the viewer frame (dataset y/z flipped) to ECEF, as
                      saved by the plugin; SSOG positions are in the dataset frame.
    error_scale     : multiplies every tile's geometricError (median splat
                      diameter). 3D Tiles viewers refine above 16 px of error;
                      16 makes them refine once a coarse splat reaches ~1 px.
    world_transform : optional 4x4 model -> dataset-frame node transform.
    progress_cb     : callable(fraction); returning False cancels.
    Returns a summary dict (tile counts per level, splats, bytes, seconds).
    """
    t0 = time.time()
    ssog_path = Path(ssog_path)
    manifest_path = ssog_path / "lod-meta.json" if ssog_path.is_dir() else ssog_path
    root_dir = manifest_path.parent
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    levels = int(manifest["lodLevels"])
    tree = _parse_tree(manifest["tree"], levels)
    coarsest = levels - 1
    while coarsest > 0 and tree.counts[coarsest] == 0:
        coarsest -= 1

    counter = [0]
    top = [_plan(n, coarsest, max_tile_splats, counter)
           for n in _cover(tree, coarsest, max_tile_splats) if n.counts[coarsest] > 0]
    root = top[0] if len(top) == 1 else _Tile(tree, coarsest, None, top)
    tiles = [t for t in _walk(root) if t.uri]
    log(f"ssog: {levels} levels, {int(tree.counts.sum()):,} splats, {len(tree.leaves):,} leaves "
        f"-> {len(tiles)} tiles (max {max_tile_splats:,} splats/tile)")

    # Encode coarse levels first; within a level, tree order keeps unit reads sequential.
    order = sorted(tiles, key=lambda t: -t.level)
    units = _UnitCache(root_dir, manifest["filenames"])
    total_bytes = 0
    per_level: dict[int, list[int]] = {}
    for i, tile in enumerate(order):
        splats = _gather(tile.node, tile.level, units)
        degree = min(splats.sh_degree, max_sh_degree)
        rest = splats.f_rest_rgb[:, :SH_COEFFS[degree]] if degree > 0 else None
        path = out_dir / tile.uri
        path.parent.mkdir(parents=True, exist_ok=True)
        write_splat_glb(path, splats.positions, splats.rotations_wxyz, splats.scales_log,
                        splats.opacity_logit, splats.f_dc, rest, degree)
        total_bytes += path.stat().st_size
        if tile.level > 0:
            tile.geometric_error = float(np.median(2.0 * np.exp(splats.scales_log.max(axis=1))))
        per_level.setdefault(tile.level, [0, 0])
        per_level[tile.level][0] += 1
        per_level[tile.level][1] += len(splats)
        if progress_cb and progress_cb((i + 1) / len(order)) is False:
            raise ExportCancelled("3D Tiles conversion cancelled")

    # Errors must not grow towards the leaves; parents cover at least their children.
    def settle(tile: _Tile) -> float:
        child_max = max((settle(c) for c in tile.children), default=0.0)
        tile.geometric_error = max(tile.geometric_error, child_max)
        return tile.geometric_error

    settle(root)
    sim_scale = float(_load_similarity(transform)["scale"])
    diagonal_local = float(np.linalg.norm(tree.bmax - tree.bmin))
    if root.uri is None:
        # A content-less root shows nothing unrefined: always refine it.
        root.geometric_error = diagonal_local

    def to_json(tile: _Tile) -> dict:
        out = {
            "boundingVolume": {"box": aabb_box(tile.node.bmin, tile.node.bmax)},
            "geometricError": tile.geometric_error * sim_scale * error_scale,
        }
        if tile.uri:
            out["content"] = {"uri": tile.uri}
        if tile.children:
            out["children"] = [to_json(c) for c in tile.children]
        return out

    root_json = to_json(root)
    root_json["transform"] = root_transform(_load_similarity(transform), world_transform)
    root_json["refine"] = "REPLACE"
    tileset = {**tileset_header(), "geometricError": diagonal_local * sim_scale, "root": root_json}
    (out_dir / "tileset.json").write_text(json.dumps(tileset, separators=(",", ":")), encoding="utf-8")

    summary = {
        "tiles": len(tiles),
        "levels": {lvl: {"tiles": c[0], "splats": c[1]} for lvl, c in sorted(per_level.items(), reverse=True)},
        "bytes": total_bytes,
        "seconds": round(time.time() - t0, 1),
    }
    log(f"wrote {out_dir / 'tileset.json'}: {len(tiles)} tiles, {total_bytes / 2**30:.2f} GiB, "
        f"{summary['seconds']} s")
    return summary


def main():
    import argparse

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ssog", type=Path)
    ap.add_argument("similarity_json", type=Path)
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--max-tile-splats", type=int, default=DEFAULT_MAX_TILE_SPLATS)
    ap.add_argument("--error-scale", type=float, default=DEFAULT_ERROR_SCALE)
    ap.add_argument("--max-sh-degree", type=int, choices=[0, 1, 2, 3], default=3)
    args = ap.parse_args()

    def prog(f):
        print(f"  {f * 100:5.1f}%", end="\r", flush=True)

    summary = export_3dtiles_from_ssog(args.ssog, args.similarity_json, args.out_dir,
                                       max_tile_splats=args.max_tile_splats,
                                       error_scale=args.error_scale,
                                       max_sh_degree=args.max_sh_degree, progress_cb=prog)
    print()
    for lvl, info in summary["levels"].items():
        print(f"  L{lvl}: {info['tiles']:4d} tiles, {info['splats']:>12,} splats")


if __name__ == "__main__":
    main()
