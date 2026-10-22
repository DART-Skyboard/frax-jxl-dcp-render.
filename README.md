# Frax JXL DCP Render

Blender extension by Radical Deepscale LLC / DART Meadow. Render to **JPEG XL** stills and animations, and export **festival DCPs**.

- Pure-Python JPEG XL encoder (lossless and lossy, 8 to 32-bit, alpha, animation, focus areas, progressive)
- Optional libjxl 0.13 engine (WebAssembly) for VarDCT, photon noise, JPEG recompression and opening `.jxl` files
- One-click **Render Animation → JPEG XL** packs the frame range into one animated `.jxl`
- **Render DCP** builds a Digital Cinema Package master through DCP-o-matic
- Blender 5.2 LTS or later · GPL-3.0-or-later

Support and troubleshooting: see [`docs/index.html`](docs/index.html) (published at the repository's GitHub Pages site) or open an [issue](../../issues).

## Install
- **Blender Extensions platform:** search "Frax JXL DCP Render" in Preferences → Get Extensions (once approved).
- **From disk:** download the zip from [Releases](../../releases), then Preferences → Get Extensions → Install from Disk.
- **Android (experimental):** use the `-android` zip; see the support page.

## Build the package
```
blender --command extension build --source-dir extension
```
The `wasmtime` wheels are bundled in release zips only (not in git). Source for the extension is in `extension/`.

## Credits
JPEG XL and libjxl by the JPEG XL Project Authors (BSD-3-Clause). wasmtime (Apache-2.0).
