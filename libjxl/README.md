# frax-libjxl

[libjxl](https://github.com/libjxl/libjxl) 0.13 (BSD-3-Clause, © the JPEG XL Project Authors) compiled to a standalone
WebAssembly module (`frax_libjxl/frax-jxl-wasi.wasm`) plus `engine.py`, the small ctypes/wasmtime wrapper the
Frax JXL DCP Render Blender extension uses. It is a pure-Python, platform-independent wheel (`py3-none-any`);
the extension bundles it next to its `wasmtime` wheel. License text: `LICENSE-libjxl.txt`.

Build: `python -m pip wheel . --no-deps -w ../extension/wheels` (from this folder).
