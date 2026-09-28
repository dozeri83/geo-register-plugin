# Geo Register Plugin

Registers a [LichtFeld Studio](https://github.com/MrNeRF/LichtFeld-Studio/) scene to real-world geographic coordinates (WGS-84 / ECEF).
Once registered, clicking any point on the model returns its latitude, longitude, and altitude.
The plugin can also export geo-referenced splat models as **LAS/LAZ** point clouds or
**3D Tiles 1.1** datasets (ArcGIS Gaussian Splat Layer / CesiumJS), including tiled
level-of-detail (LOD) tilesets for large models.

![Geo Register Plugin in action](assets/plugin_example.jpg)

---

## How It Works

The plugin solves a **similarity transform** that maps scene-space coordinates to
ECEF (Earth-Centered Earth-Fixed) coordinates:

```
world_ecef = scale * R @ p_scene + translation
```

Where:
- `scale` — uniform scale factor between scene units and metres
- `R` — 3x3 rotation matrix
- `translation` — 3D translation vector in metres

The transform is estimated using a robust RANSAC + IRLS solver (Umeyama 1991),
which automatically rejects outlier correspondences.

---

## Source Modes

Use the **Source** dropdown to choose how geographic reference data is provided.

---

### 1. EXIF

Automatically extracts GPS coordinates embedded in the original drone/camera images,
matches them to the camera poses in the loaded scene, and solves the transform.

**Steps:**
1. Load your dataset in LichtFeld Studio.
2. Select **EXIF** from the Source dropdown.
3. Click **Calc Georeference From EXIF**.

The plugin scans the dataset folder for images with GPS EXIF tags, matches each image
to its camera pose by filename stem, and runs the solver.

> **Original images folder:**
> If your dataset images no longer contain GPS EXIF data (e.g. they were undistorted
> or re-encoded), click **Set Original Images Folder** to point the plugin at the
> folder containing your original images. The plugin will scan that folder for GPS
> tags instead.
> The filename **stem** (name without extension) must match between the original
> images and the dataset cameras — for example, `DJI_0001.jpg` (original) matches
> `DJI_0001.JPG` or `DJI_0001.png` in the dataset.

> **Note:** EXIF GPS readings can be imprecise, especially in the altitude direction. For more accurate results, use professional alignment tools with GCPs (ground control points).

---

### 2. Similarity File

Loads a previously computed similarity transform from a JSON file.
Useful when you already have a valid transform (e.g. exported by this plugin from a
different session or computed externally).

**Steps:**
1. Select **Similarity File** from the Source dropdown.
2. Click **Load Similarity File** and pick a `.json` file.

**Expected JSON format:**

```json
{
  "scale": 0.999367,
  "rotation": [
    [ 0.9998,  0.0123, -0.0156],
    [-0.0121,  0.9998,  0.0089],
    [ 0.0157, -0.0087,  0.9998]
  ],
  "translation": [4052845.12, 617312.45, 4867891.78]
}
```

**Matrix composition:**

The transform applies components in this order:

1. **Scale** — multiply the scene point by `scale`
2. **Rotate** — apply the 3x3 rotation matrix `R`
3. **Translate** — add the `translation` vector

In matrix form as a 4x4 homogeneous transform:

```
| scale*R  translation |   @   | x |       | x_ecef |
|    0          1      |       | y |   =   | y_ecef |
                               | z |       | z_ecef |
                               | 1 |       |   1    |
```

The plugin exports this JSON automatically after every EXIF or CSV solve, to
`~/.lichtfeld/data/plugin_data/geo_register_pluggin/<project-key>/similarity_transform.json`.

---

### 3. Image Positions CSV

Loads image GPS positions from a CSV file and runs the same solver as the EXIF mode.
Useful when GPS data is not embedded in the images (e.g. stored separately by the
drone flight controller, or sourced from a ground control point log).

**Steps:**
1. Select **Image Positions CSV** from the Source dropdown.
2. Click **Load CSV File** and pick a `.csv` file.

**Required CSV format:**

The file must have a header row with these column names (in any order):

```
image_name,lat,lon,alt
DJI_0001.JPG,32.08154321,34.78912345,48.250
DJI_0002.JPG,32.08163897,34.78924561,48.431
DJI_0003.JPG,32.08172450,34.78937812,48.619
DJI_0004.JPG,32.08181023,34.78951034,48.802
```

- `image_name` — filename only, with extension (not a full path)
- `lat` — latitude in decimal degrees (WGS-84)
- `lon` — longitude in decimal degrees (WGS-84)
- `alt` — ellipsoidal altitude in metres

The plugin exports this CSV automatically after every EXIF solve, to
`~/.lichtfeld/data/plugin_data/geo_register_pluggin/<project-key>/image_positions.csv`.

---

### 4. RealityScan Parameters CSV

> **Recommended over EXIF when you aligned your data with RealityScan.**
>
> RealityScan performs bundle adjustment that refines each camera's position beyond
> the raw GPS reading. Importing these adjusted positions instead of raw EXIF gives
> significantly better geo-registration accuracy, because the plugin fits the
> similarity transform to coordinates that are already internally consistent with
> the reconstructed model. Expect a noticeably lower RMSE compared to EXIF mode.

**How to export from RealityScan:**

**Step 1 — Set the project output coordinate system to WGS 84:**

Go to **Workflow → Settings → Coordinate System** and set the output coordinate
system to **EPSG:4326 – GPS WGS 84**.

![RealityScan project coordinate system setting](assets/rs_project_setting.png)

**Step 2 — Export Internal/External Camera Parameters:**

In the export dialog, choose **Internal/External Camera Parameters**.
In the export settings, set the coordinate system to **Project Output**.

![RealityScan export settings](assets/rs_export_settings.png)

> **Important — Colmap export:** The output coordinate system setting is **global** for
> all RealityScan exports. If you later export to **Colmap** format, you must change it
> back to **Grid Plane / Local Euclidean** — Colmap expects Euclidean (non-geographic)
> coordinates, and leaving it set to EPSG:4326 will produce incorrect results.
>
> ![RealityScan Colmap export coordinate system setting](assets/rc_export_colmap_settings.png)

**Step 3 — Load in the plugin:**

1. Select **RealityScan Parameters CSV** from the Source dropdown.
2. Click **Load RealityScan CSV** and pick the exported `.csv` file.

**CSV format (exported by RealityScan):**

```
#name,x,y,alt,yaw,pitch,roll,f_35mm,px_norm,py_norm,k1,k2,k3,k4,t1,t2
DJI_0214.JPG,-34.82878738,7.16331052,86.94,131.99,...
```

- `name` — image filename
- `x` — longitude (decimal degrees, WGS-84)
- `y` — latitude (decimal degrees, WGS-84)
- `alt` — ellipsoidal altitude in metres

All other columns (yaw, pitch, roll, lens parameters) are ignored by the plugin.

---

### 5. Metashape Cameras XML

Loads camera GPS positions from an Agisoft Metashape camera XML export and runs
the same solver as the CSV modes.

**How to export from Metashape:**

1. In Metashape, make sure the chunk's **coordinate system** is set to **WGS 84 (EPSG:4326)**.
2. Go to **File → Export → Export Cameras…** and save as XML.

**Steps in the plugin:**

1. Select **Metashape Cameras XML** from the Source dropdown.
2. Click **Load Metashape Cameras XML** and pick the exported `.xml` file.

**Requirements:**

- The chunk CRS must be **GEOGCS / EPSG:4326**. Chunks with any other CRS
  (projected, geocentric, or local) are skipped with a warning.
- Cameras with `enabled="0"` on their `<reference>` tag are skipped.
- Multiple chunks in the same file are all processed and merged.

> **Note — naive parsing:**
> The parser reads GPS coordinates directly from the `<reference>` attribute of each
> `<camera>` element. It assumes that this tag is present and populated with GPS data —
> which is the case when cameras were imported with GPS coordinates in Metashape's
> Reference pane. If a camera was added without GPS data, its `<reference>` tag will
> be absent and that camera will simply be skipped.
>
> Before loading, you can verify your file contains reference data by checking that
> individual `<camera>` entries include a `<reference>` tag with `x`, `y`, `z`
> attributes, for example:
> ```xml
> <camera id="3" sensor_id="0" label="DJI_0003.JPG" enabled="1">
>   <transform>...</transform>
>   <reference x="8.71105718333333" y="50.1547084833333" z="195.665"
>              yaw="..." pitch="0" roll="0" enabled="1"/>
> </camera>
> ```
> Here `x` = longitude, `y` = latitude, `z` = altitude in metres (WGS-84).

---

## Output Files

After a successful solve the plugin writes to `~/.lichtfeld/data/plugin_data/geo_register_pluggin/<project-key>/`:

| File | Description |
|---|---|
| `similarity_transform.json` | The solved transform (scale, R, t, RMSE, inlier counts) |
| `similarity_transform_info.txt` | Human-readable explanation of the transform fields |
| `image_positions.csv` | GPS positions of all matched images (EXIF and CSV modes) |

`<project-key>` is the LichtFeld project file name plus the first 8 characters of the
project UUID (e.g. `dji-5_367c7181`), so each project keeps its own registration. The UUID
survives saves, renames and Save As, and a project reopened without its dataset still finds
its transform. Unsaved projects use `untitled_<uuid>`. The JSON also records the full
`project_uuid`; a file whose UUID does not match the open project is ignored.

---

## Configuration

The plugin reads `config.json` from its root directory at runtime. All keys are optional;
the defaults below are used when a key is absent or the file does not exist.

```json
{
  "las_export_coordinates": "UTM",

  "ransac_inlier_thr_m": 10.0,
  "ransac_confidence":    0.99,
  "ransac_max_iter":      2000,
  "irls_huber_delta_m":   2.0,
  "irls_max_iter":        50
}
```

| Key | Default | Description |
|---|---|---|
| `las_export_coordinates` | `"UTM"` | Output CRS for LAS/LAZ export: `"UTM"` or `"LLA"` (EPSG:4326) |
| `ransac_inlier_thr_m` | `10.0` | RANSAC inlier threshold in metres — correspondences with a larger residual are rejected as outliers |
| `ransac_confidence` | `0.99` | Target probability that RANSAC finds the correct model; drives the adaptive iteration count |
| `ransac_max_iter` | `2000` | Hard cap on RANSAC iterations regardless of the adaptive estimate |
| `irls_huber_delta_m` | `2.0` | Huber loss delta (metres) for IRLS refinement — points below this residual receive full weight |
| `irls_max_iter` | `50` | Maximum IRLS iterations |

> **Tuning tips:**
> - Increase `ransac_inlier_thr_m` if your GPS data is noisy (e.g. consumer-grade EXIF) and the solver rejects too many correspondences.
> - Decrease it when you have RTK-quality GPS and want stricter outlier rejection.
> - `irls_huber_delta_m` should be smaller than `ransac_inlier_thr_m` — a good rule of thumb is roughly ¼ of the inlier threshold.

---

## Export

Once geo-registration is complete (or a registration saved for the project is found),
the plugin can export any splat model in the scene as a geo-referenced LAS/LAZ point
cloud or 3D Tiles dataset. To convert a splat PLY file that is not in the scene, see
[Convert External PLY](#convert-external-ply).

The **Export** section appears below the transform. Use the **Splat Model** dropdown
to select which model to export and choose the output **Format**:

- **LAS / LAZ** — click **Export LAS/LAZ**. A save dialog opens with the splat name
  pre-filled as the filename; it asks before overwriting an existing file. The last
  exported path is shown in the panel for reference.
- **3D Tiles (SPZ)** — choose the options, pick an output directory, then click
  **Export 3D Tiles** (see [3D Tiles](#3d-tiles)).

### LAS — LASer file format

LAS is the industry-standard binary format for point cloud data, maintained by the
[ASPRS](https://www.asprs.org/divisions-committees/lidar-division/laser-las-file-format-exchange-activities).
The plugin writes **LAS 1.4, point format 7** (XYZ + RGB colour).

The output coordinate system is controlled by `las_export_coordinates` in `config.json`
(see [Configuration](#configuration)):

| Setting | CRS | X | Y | Z |
|---|---|---|---|---|
| `"UTM"` *(default)* | WGS-84 / UTM zone auto-selected from point-cloud median | Easting (m) | Northing (m) | Ellipsoidal height (m) |
| `"LLA"` | EPSG:4326 geographic | Longitude (°) | Latitude (°) | Ellipsoidal height (m) |

When `UTM` is selected:
- The UTM zone is determined from the **median latitude and longitude** of the exported
  point cloud — a robust centroid that ignores outliers.
- Northern/southern hemisphere is set automatically from the sign of the median latitude.
- The EPSG code is derived accordingly (e.g. `EPSG:32636` for UTM zone 36N).

For both modes:
- An **OGC WKT CRS record** is embedded in the file header so any compliant GIS tool
  (QGIS, ArcGIS, CloudCompare, etc.) can read the coordinate system automatically.
- Gaussian splat positions are transformed from scene space to ECEF using the solved
  similarity transform, then converted to WGS-84 geodetic coordinates.
- Colour is taken from the first spherical harmonics band (DC term), packed as
  16-bit per channel RGB.

### LAZ — Compressed LAS

LAZ is a losslessly compressed variant of LAS. The point data and CRS metadata are
identical to LAS; only the storage is compressed using the
[LASzip](https://laszip.org/) algorithm.

- File sizes are typically **5–10× smaller** than the equivalent LAS file.
- All major GIS tools that support LAS also support LAZ.
- Requires the `lazrs` or `laszip` Python package in the plugin environment
  (installed automatically with the plugin dependencies).

### 3D Tiles

![3D Tiles export in ArcGIS](assets/3dtiles_example.png)

The plugin can export the splat model as a georeferenced **3D Tiles 1.1** dataset
that renders as full Gaussian splats (not a point cloud). Two layouts are available,
selected with the **Level of detail (LOD)** checkbox:

| Mode | Output | Use for |
|---|---|---|
| **LOD** *(default)* | Many tiles in a coarse-to-fine hierarchy | Large models — viewers stream only the tiles and detail the camera needs |
| **Single tile** | The whole model in one GLB | Small models |

**Max SH** caps the spherical-harmonics degree written to the tiles (lower = smaller
files, less view-dependent colour).

In both modes the root tile's `transform` holds the similarity transform (scene → ECEF,
including the splat node's own transform), placing the model at the correct position on
Earth; all tiles below inherit it.

#### LOD tileset

**Output files:**

```
out_dir/
  tileset.json          # 3D Tiles 1.1 manifest (tile tree, bounds, geometric errors)
  tiles/
    L0/000123.glb       # full-detail tiles
    L1/...              # each level holds about half the splats of the level below
    ...
    L<n>/...            # coarsest level, loaded first
```

**How it is built** — the panel shows the current step, a progress bar and a
**Cancel** button:

1. **Exporting temporary SSOG** — LichtFeld Studio (`lf.io.save_ssog`) builds the LOD
   levels by merging splats and splits the scene into spatial chunks (a k-d tree).
2. **Converting to 3D Tiles** — each tile is one tree node at one LOD level. Parents
   and children cover the same region with `REPLACE` refinement, so zooming in swaps a
   coarse tile for finer ones without gaps or double drawing. A tile's
   `geometricError` is the median splat diameter of its content, times the error scale.
3. **Removing temporary files** — the temporary SSOG is always deleted, also after a
   cancelled or failed export. A failed or cancelled export also removes the partial
   `tileset.json` and `tiles/` it created.

The temporary SSOG is written to `~/.lichtfeld/data/plugin_data/geo_register_pluggin/tmp/`.

**LOD settings** (collapsible; **Reset to defaults** restores them):

| Setting | Default | Description |
|---|---|---|
| Error scale | `16` | Multiplies every tile's geometric error (see [Screen-space error](#screen-space-error-sse) below). Higher switches to finer tiles sooner (sharper, more memory); lower keeps coarse tiles longer |
| Levels | `Auto` | Number of LOD levels (1–8). Auto picks enough levels for the coarsest one to hold about 1M splats |
| Level ratio | `0.5` | Fraction of splats each coarser level keeps |
| Chunk splats (K) | `100` | Maximum splats per chunk, in thousands, summed over all levels. Also the tile budget: no tile holds more than this many splats |
| Chunk extent (m) | `16` | Chunks larger than this (in metres) are split further, unless they are below Min chunk splats |
| Min chunk splats (K) | `8` | Chunks with fewer splats are not split for their extent |

##### Screen-space error (SSE)

3D Tiles viewers decide which level to draw from each tile's `geometricError` (in
metres): the detail the tile lacks compared to its children. They project it to pixels,

```
SSE = geometricError × screen_height_px / (2 × distance × tan(fov / 2))
```

and refine the tile (replace it with its children) when the SSE exceeds the viewer's
maximum screen-space error, typically **16 px** (Cesium's `maximumScreenSpaceError`;
ArcGIS behaves similarly).

The plugin sets each tile's base error to the **median splat diameter** of its content
and multiplies it by the **error scale**. With the default scale of 16, a tile refines
once its typical splat would cover about **1 px** (16 px ÷ 16): coarse splats are
swapped for finer ones before they become visible as blobs. Doubling the scale
refines at twice the distance (sharper, more tiles in memory); halving it keeps coarse
levels up to twice as close.

##### Viewer memory

The defaults were tuned in ArcGIS Maps SDK 5.0. Web viewers have a fixed Gaussian splat
memory budget that does not depend on your GPU: ArcGIS logs
`ran out of gaussian splat memory` and stops drawing the layer when it is exceeded.
The coarsest level must fit in that budget because it is loaded first, so keep
**Levels** on **Auto**: fewer levels than Auto can produce a tileset that flashes and
disappears. For ArcGIS, `quality-profile="high"` on the scene raises the budget.

> **Tip:** splats floating far from the scene (common in drone captures) inflate the
> tileset's extent and make viewers zoom far out. Remove them before exporting, e.g.
> with LichtFeld Studio's **Crop Box** (**Fit Trim**, then **Apply**).

#### Single tile

**Output files:**

```
out_dir/
  tileset.json   # 3D Tiles 1.1 manifest
  splats.glb     # Binary glTF — SPZ-compressed splat data in the BIN chunk
```

The root tile holds the transform and `splats.glb` as its content.

#### Existing files

The export never overwrites: if the chosen directory already contains `tileset.json`
and `tiles/` (LOD) or `splats.glb` (single tile), the **Export** button is replaced
by an error until you pick another directory or remove the files.

#### Format details

| Property | Value |
|---|---|
| 3D Tiles version | 1.1 |
| glTF extension | `KHR_gaussian_splatting` + `KHR_gaussian_splatting_compression_spz_2` |
| Compression | SPZ v3 (gzipped), ~17 bytes/splat for SH degree 3 |
| SH bands | Up to degree 3 (full view-dependent colour) |
| Tested on | ArcGIS Maps SDK 5.0 — should also work on CesiumJS ≥ 1.139 and ArcGIS Pro ≥ 3.6 |

**[`KHR_gaussian_splatting`](https://github.com/KhronosGroup/glTF/tree/main/extensions/2.0/Khronos/KHR_gaussian_splatting)**
is a ratified Khronos glTF 2.0 extension for embedding 3D Gaussian Splat data inside
a standard glTF/GLB asset. It defines per-primitive attributes for position, rotation,
scale, opacity, and spherical harmonic coefficients, with a companion compression
extension (`KHR_gaussian_splatting_compression_spz_2`) that wraps the payload in an
SPZ blob.

**SPZ (Splat Zip) v3** is an open binary format for compact Gaussian splat storage,
developed by [Niantic Labs](https://github.com/nianticlabs/spz) (MIT licence).
It encodes positions, rotations, scales, opacity, and spherical harmonic coefficients
into a single gzipped binary blob (~17 bytes/splat at SH degree 3, roughly 14× smaller
than the source PLY). The encoder used here is a pure-Python implementation that mirrors
the Niantic reference byte-for-byte.

---

## Edit Mode (No Cameras)

When LichtFeld Studio is in **Edit Mode** (no cameras loaded), geo-registration cannot
be computed because there are no camera poses to fit the similarity transform against.

If a registration was saved for the open project (see [Output Files](#output-files)),
the plugin finds it and still offers the transform display, **Get Pixel Location** and
the full [Export](#export) section for the project's splat models. Otherwise it shows
a note and opens [Convert External PLY](#convert-external-ply).

---

## Convert External PLY

The collapsible **Convert external PLY** section at the bottom of the panel, available
in every mode, converts a 3DGS `.ply` file that is not loaded in the scene to LAS, LAZ
or 3D Tiles.

The PLY must be in the same coordinate frame the similarity transform was computed
for, e.g. a model trained or exported from the registered LichtFeld Studio project.

**Steps:**

1. Expand **Convert external PLY** (it opens by itself in Edit Mode without a
   registration).
2. Click **Pick PLY File** and select your 3DGS `.ply` file.
3. Similarity transform:
   - If the open project has a registration, it is used automatically.
   - Otherwise click **Pick Similarity JSON** and select a `similarity_transform.json`
     saved earlier by this plugin.
4. Choose the output **Format** and destination. For 3D Tiles, the **Max SH**,
   **Level of detail (LOD)** and LOD settings work as described in [3D Tiles](#3d-tiles)
   and share their values with the scene export.
5. Click **Export**. For LOD 3D Tiles the PLY is loaded first, then the three export
   steps run with progress and **Cancel**.

> **Important:** the similarity matrix must have been calculated with a scene still
> loaded in LichtFeld Studio. You cannot compute it in Edit Mode.

The similarity JSON format is the same as described in the [Similarity File](#2-similarity-file)
source mode section above. The plugin writes this file automatically to
`~/.lichtfeld/data/plugin_data/geo_register_pluggin/<project-key>/similarity_transform.json` after every successful solve.

---

## Requirements

- LichtFeld Studio with the Python plugin API `>=1,<2`.
- **LOD 3D Tiles** needs `lf.io.save_ssog`, and per-project storage uses
  `lf.plugins.data_dir` / `lf.project_uuid` / `lf.project_path`; both are in the LichtFeld
  Studio `dev` branch from September 2026 and in the first release after it.
- Python packages (installed automatically): `numpy`, `Pillow`, `laspy[lazrs]`.
