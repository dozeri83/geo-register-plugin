# Third-party notices

This module ships clean Python code, but its design and binary output format
mirror open-source work by other projects. Their licenses are reproduced below.

## Niantic SPZ (MIT)

`spz_encode.py` is a clean-room Python reimplementation of the SPZ v3 encoder
from [github.com/nianticlabs/spz](https://github.com/nianticlabs/spz). The
binary layout, quantization parameters, and bit-packing are determined by the
SPZ format specification published by Niantic Labs.

```
MIT License

Copyright (c) 2024 Niantic, Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## numpy (BSD 3-Clause)

The plugin imports `numpy`, distributed under the
[BSD 3-Clause license](https://numpy.org/doc/stable/license.html). No numpy
source is redistributed here — it is a runtime dependency only.

## Pillow (HPND)

The EXIF reader imports `Pillow` (PIL fork) for reading GPS metadata from images.
Pillow is distributed under the
[Historical Permission Notice and Disclaimer (HPND)](https://github.com/python-pillow/Pillow/blob/main/LICENSE)
license. No Pillow source is redistributed here — it is a runtime dependency only.

## laspy (BSD 2-Clause)

The LAS/LAZ exporter imports `laspy` for writing LAS 1.4 point cloud files.
laspy is distributed under the
[BSD 2-Clause license](https://github.com/laspy/laspy/blob/master/LICENSE).
No laspy source is redistributed here — it is a runtime dependency only.

## glTF and 3D Tiles standards

The output uses the following royalty-free open standards:

- **glTF 2.0** — Khronos Group
- **KHR_gaussian_splatting**, **KHR_gaussian_splatting_compression_spz_2** — Khronos Group
- **3D Tiles 1.1**, **3DTILES_content_gltf** — OGC Community Standards

