# SPDX-License-Identifier: GPL-3.0-or-later
# Frax JPEG XL animation worker — runs in its OWN PROCESS (started with Blender's bundled Python), never inside
# Blender. It reads frames that Blender has written (TIFF / EXR files, or .npy arrays), encodes them into one
# animated JPEG XL and reports through status.json. Blender talks to it only through files in a spool folder:
#   job.json    settings + timing, written once        queue.txt   append-only: "F|frame|file", "N|frame|npy|linear",
#   status.json frames done / bytes / done / error                  "END" (finish and write) or "ABORT" (quit)
# No threading, no queue module, no bpy: just numpy and the same encoder code the add-on uses for stills.
import base64
import json
import os
import sys
import time
import types


def _status(spool, **kw):
    tmp = os.path.join(spool, "status.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(kw, f)
    os.replace(tmp, os.path.join(spool, "status.json"))


def main(spool, ext):
    # import the extension's modules as a bare package ("fraxext") without running its __init__ (which needs bpy)
    here = os.path.abspath(ext)
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") not in (here, os.path.abspath("."))]
    pkg = types.ModuleType("fraxext")
    pkg.__path__ = [here]
    pkg.__package__ = "fraxext"
    sys.modules["fraxext"] = pkg
    import numpy as np
    from fraxext import frames, jxlcore

    with open(os.path.join(spool, "job.json"), "r", encoding="utf-8") as f:
        job = json.load(f)
    s = types.SimpleNamespace(**job["settings"])
    boxes = [(t, base64.b64decode(b), bool(c)) for t, b, c in job["boxes"]]
    static = [tuple(r) for r in job["static_rects"]]
    track = {k: [tuple(r) for r in v] for k, v in job["track_rects"].items()}
    tps, step = tuple(job["tps"]), job["step"]
    enc = plan = None
    n, pos, files, buf = 0, 0, [], b""
    qpath = os.path.join(spool, "queue.txt")
    last_input = time.time()
    _status(spool, frames=0, bytes=0, done=False, error=None)
    try:
        while True:
            with open(qpath, "rb") as f:
                f.seek(pos)
                chunk = f.read()
            if not chunk:
                if time.time() - last_input > 900:                     # Blender is gone: do not linger
                    return
                time.sleep(0.05)
                continue
            pos += len(chunk)
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for raw in lines:
                line = raw.decode("utf-8").strip()
                if not line:
                    continue
                last_input = time.time()
                if line == "ABORT":
                    return
                if line == "END":
                    if enc is None:
                        _status(spool, frames=n, bytes=0, done=True, error="no frames were received")
                        return
                    data = enc.finish()
                    out = job["path"]
                    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
                    with open(out, "wb") as f:
                        f.write(data)
                    if job.get("remove_frames"):
                        for p in files:
                            try:
                                os.remove(p)
                            except OSError:
                                pass
                    _status(spool, frames=n, bytes=len(data), done=True, error=None)
                    return
                kind, frame, rest = line.split("|", 2)
                frame = int(frame)
                if kind == "F":
                    arr, linear = frames.read_any(rest)
                    files.append(rest)
                else:
                    npy, lin = rest.rsplit("|", 1)
                    arr, linear = np.load(npy), lin == "1"
                    try:
                        os.remove(npy)
                    except OSError:
                        pass
                h, w = arr.shape[:2]
                if enc is None:
                    plan = jxlcore.Plan(s, arr, linear, job["film"])
                    enc = plan.encoder(w, h, animated=True, fps=tps, boxes=boxes, cache_dir=job.get("cache_dir"))
                rects = list(static) + list(track.get(str(frame), []))
                jxlcore.add_with_focus(enc, plan, arr, jxlcore.focus_pixels(rects, w, h), step)
                n += 1
                _status(spool, frames=n, bytes=0, done=False, error=None)
    except Exception as e:                                           # noqa: BLE001 — reported to Blender
        _status(spool, frames=n, bytes=0, done=True, error=str(e))
    finally:
        if enc is not None:
            try:
                enc.close()
            except Exception:                                        # noqa: BLE001
                pass


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
