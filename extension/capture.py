# SPDX-License-Identifier: GPL-3.0-or-later
# Pixels in, JPEG XL out: reading renders / frame files, and the streaming animation writer. The colour maths, the
# per-file Plan and the encoder options live in jxlcore.py (no bpy) so the animation worker process can share them.
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import zipfile

import bpy
import numpy as np

from frax_libjxl import engine

from . import pyengine                                         # noqa: F401  (re-exported for callers)
from .jxlcore import (Plan, add_with_focus, analyse, focus_pixels, frame_options,  # noqa: F401
                      hlg_encode, linear_to_bt709, linear_to_srgb, pq_encode, srgb_to_linear, to_target, use_libjxl,
                      xmp_xml)

LINEAR_EXTS = {".exr", ".hdr"}

# ── reading pixels ─────────────────────────────────────────────────────────────────────────────────────────────
def load_file_pixels(path):
    """Any image Blender can read → (H×W×4 float32 top-down, raw file values, is_linear)."""
    img = bpy.data.images.load(path, check_existing=False)
    try:
        img.colorspace_settings.is_data = True                           # raw values, no colour conversion on read
        w, h = img.size
        arr = np.empty(w * h * 4, np.float32)
        img.pixels.foreach_get(arr)
    finally:
        bpy.data.images.remove(img)
    return arr.reshape(h, w, 4)[::-1].copy(), os.path.splitext(path)[1].lower() in LINEAR_EXTS


def image_pixels(image, scene, linear):
    """A Blender image (including the Render Result and the compositor Viewer) → pixels via save_render, which
    applies the scene's view transform for display output and writes raw scene-linear for EXR."""
    st = scene.render.image_settings
    keep = dict(media_type=getattr(st, "media_type", None), file_format=st.file_format, color_mode=st.color_mode, color_depth=st.color_depth)
    fd, tmp = tempfile.mkstemp(suffix=".exr" if linear else ".png", prefix="frax_jxl_")
    os.close(fd)
    try:
        if hasattr(st, "media_type"):
            st.media_type = "IMAGE"
        st.file_format = "OPEN_EXR" if linear else "PNG"
        st.color_mode = "RGBA"
        st.color_depth = "32" if linear else "16"
        image.save_render(filepath=tmp, scene=scene)
        return load_file_pixels(tmp)
    finally:
        for k, v in keep.items():
            if v is None:
                continue
            try:
                setattr(st, k, v)
            except Exception:
                pass
        try:
            os.remove(tmp)
        except OSError:
            pass


def xmp_box(s, scene, frame=None, extra=None):
    info = {}
    if s.write_render_info and scene:
        r = scene.render
        info = {"Scene": scene.name, "Engine": r.engine.replace("BLENDER_", "").title(), "Blender": bpy.app.version_string,
                "Resolution": "%dx%d" % (r.resolution_x * r.resolution_percentage // 100, r.resolution_y * r.resolution_percentage // 100),
                "FPS": "%.3f" % (r.fps / r.fps_base), "Camera": scene.camera.name if scene.camera else "",
                "ViewTransform": scene.view_settings.view_transform}
        if r.engine == "CYCLES" and hasattr(scene, "cycles"):
            info["Samples"] = str(scene.cycles.samples)
        elif hasattr(scene, "eevee") and hasattr(scene.eevee, "taa_render_samples"):
            info["Samples"] = str(scene.eevee.taa_render_samples)
        if frame is not None:
            info["Frame"] = str(frame)
    if extra:
        info.update(extra)
    if not (info or s.author or s.copyright or s.title):
        return []
    x = xmp_xml(s, info, bpy.app.version_string)
    return [("xml ", x.encode("utf-8"), s.compress_boxes == "1")]


def object_rect(scene, obj, depsgraph=None):
    """Normalised (x, y, w, h), origin top-left, of an object's bounding box as the scene camera sees it."""
    from bpy_extras.object_utils import world_to_camera_view
    from mathutils import Vector
    cam = scene.camera
    if not cam or not obj:
        return None
    ob = obj.evaluated_get(depsgraph) if depsgraph else obj
    pts = [world_to_camera_view(scene, cam, ob.matrix_world @ Vector(c)) for c in ob.bound_box]
    pts = [p for p in pts if p.z > 0]
    if not pts:
        return None
    x0, x1 = max(0.0, min(p.x for p in pts)), min(1.0, max(p.x for p in pts))
    y0, y1 = max(0.0, min(p.y for p in pts)), min(1.0, max(p.y for p in pts))
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, 1.0 - y1, x1 - x0, y1 - y0)


def scene_focus_rects(scene, s, depsgraph=None):
    if not s.focus_on:
        return []
    out = []
    for f in s.focus:
        if f.track:
            r = object_rect(scene, f.track, depsgraph)
            if r:
                out.append(r)
        else:
            out.append((f.x, f.y, f.w, f.h))
    return out


def encode_still(arr, linear, s, scene, path, rects=None, cache_dir=None, frame=None):
    h, w = arr.shape[:2]
    film = bool(scene and scene.render.film_transparent)
    plan = Plan(s, arr, linear, film)
    enc = plan.encoder(w, h, boxes=xmp_box(s, scene, frame), cache_dir=cache_dir)
    t0 = time.time()
    try:
        add_with_focus(enc, plan, arr, focus_pixels(rects or [], w, h), 0)
        data = enc.finish()
    finally:
        enc.close()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return len(data), time.time() - t0


# ── streaming animation writer: the encoder runs in its own PROCESS (worker.py) ──────────────────────────────────
def settings_dict(s):
    """A plain-JSON copy of the scene's JPEG XL settings for the worker (no Blender objects cross over)."""
    d = {}
    for p in s.bl_rna.properties:
        k = p.identifier
        if k in ("rna_type", "focus", "focus_index") or p.type in ("POINTER", "COLLECTION"):
            continue
        v = getattr(s, k)
        d[k] = v if isinstance(v, (str, int, float, bool)) else str(v)
    return d


# The worker is a small program of its own. Rather than import the add-on's package from another process, the add-on
# zips the (bpy-free) encoder modules and the frax_libjxl wrapper next to each job and runs the archive as a script: Python itself puts the archive
# on its path, so the worker needs no sys.path / sys.modules changes and no environment variables.
_WORKER_FILES = ("frames.py", "jxlcore.py", "pyengine.py", "worker.py")
_WORKER_MAIN = "import sys\nfrom fraxworker import worker\nworker.main(sys.argv[1])\n"


def _build_worker_zip(path):
    here = os.path.dirname(os.path.abspath(__file__))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("__main__.py", _WORKER_MAIN)
        z.writestr("fraxworker/__init__.py", "")
        for n in _WORKER_FILES:
            z.write(os.path.join(here, n), "fraxworker/" + n)
        core = os.path.dirname(os.path.abspath(engine.__file__))          # frax_libjxl (the bundled wheel): Python files only
        for n in sorted(os.listdir(core)):
            if n.endswith(".py"):
                z.write(os.path.join(core, n), "frax_libjxl/" + n)
        for n in sorted(os.listdir(os.path.join(here, "jxl"))):
            if n.endswith(".py"):
                z.write(os.path.join(here, "jxl", n), "fraxworker/jxl/" + n)


def _worker_command(archive, spool):
    """Blender's own Python, started the way Blender asks (bpy.app.python_args keeps the environment compatible)."""
    return [sys.executable, *bpy.app.python_args, archive, spool]


class AnimationWriter:
    """Frames go in as they are rendered; a separate worker process (worker.py) encodes them so rendering never waits.
    The two sides talk through plain files in a spool folder: this side appends lines to queue.txt, the worker
    answers in status.json — no threads, queues or pipes, so it behaves the same on every OS and inside Blender."""

    def __init__(self, s, scene, path, cache_dir=None, track_rects=None):
        self.s, self.path, self.cache_dir = s, path, cache_dir
        r = scene.render
        # exact frame timing: ticks per second = fps / fps_base, each frame lasts frame_step ticks
        self.tps = (int(round(r.fps * 1000)), int(round(r.fps_base * 1000)))
        self.step = max(1, scene.frame_step)
        self.film = r.film_transparent
        self.boxes = xmp_box(s, scene, extra={"FrameStart": str(scene.frame_start), "FrameEnd": str(scene.frame_end)})
        self.static_rects = [(f.x, f.y, f.w, f.h) for f in s.focus if not f.track] if s.focus_on else []
        self.track_rects = track_rects or {}
        self.error = None
        self._frames = 0
        self._bytes = 0
        self.files = []
        self._done = False
        self._sent_end = False
        self._seq = 0
        self.spool = tempfile.mkdtemp(prefix="frax_jxl_spool_")
        self.queue_path = os.path.join(self.spool, "queue.txt")
        self.status_path = os.path.join(self.spool, "status.json")
        open(self.queue_path, "w").close()
        job = dict(settings=settings_dict(s), tps=list(self.tps), step=self.step, film=bool(self.film),
                   boxes=[[t, base64.b64encode(b).decode("ascii"), bool(c)] for t, b, c in self.boxes],
                   static_rects=[list(r_) for r_ in self.static_rects],
                   track_rects={str(k): [list(r_) for r_ in v] for k, v in self.track_rects.items()},
                   path=path, cache_dir=cache_dir, remove_frames=bool(s.remove_frames), files=[])
        with open(os.path.join(self.spool, "job.json"), "w", encoding="utf-8") as f:
            json.dump(job, f)
        archive = os.path.join(self.spool, "frax_worker.zip")
        _build_worker_zip(archive)
        self.proc = subprocess.Popen(_worker_command(archive, self.spool), stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    # ── status (polled from the main thread) ──
    def _poll(self):
        try:
            with open(self.status_path, "r", encoding="utf-8") as f:
                st = json.load(f)
            self._frames, self._bytes = st.get("frames", 0), st.get("bytes", 0)
            if st.get("error") and not self.error:
                self.error = st["error"]
            if st.get("done"):
                self._done = True
        except (OSError, ValueError):
            pass
        if not self._done and self.proc.poll() is not None:             # worker died without saying so
            self._poll_final()

    def _poll_final(self):
        try:
            with open(self.status_path, "r", encoding="utf-8") as f:
                st = json.load(f)
            self._frames, self._bytes = st.get("frames", 0), st.get("bytes", 0)
            self.error = self.error or st.get("error")
        except (OSError, ValueError):
            self.error = self.error or "the JPEG XL worker process stopped unexpectedly"
        self._done = True

    @property
    def frames(self):
        self._poll()
        return self._frames

    @property
    def bytes(self):
        self._poll()
        return self._bytes

    @property
    def is_done(self):
        self._poll()
        return self._done

    def wait(self, timeout=None):
        """Background mode: block until the worker has finished."""
        t0 = time.time()
        while not self.is_done:
            if timeout is not None and time.time() - t0 > timeout:
                return False
            time.sleep(0.1)
        return True

    # ── feeding ──
    def _send(self, line):
        with open(self.queue_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def add(self, arr, linear, frame, file_path=None):
        if self.error:
            return
        if file_path:
            self.files.append(file_path)
        self._seq += 1
        npy = os.path.join(self.spool, "f%06d.npy" % self._seq)
        np.save(npy, np.ascontiguousarray(arr, dtype=np.float32))
        self._send("N|%d|%s|%d" % (frame, npy, 1 if linear else 0))

    def add_file(self, path, frame):
        """Queue a frame file (TIFF / EXR); the worker reads it with its own bpy-free reader."""
        if self.error:
            return
        self.files.append(path)
        self._send("F|%d|%s" % (frame, path))

    def finish(self):
        if not self._sent_end:
            self._sent_end = True
            self._send("END")

    def abort(self):
        self.error = self.error or "cancelled"
        if not self._sent_end:
            self._sent_end = True
            self._send("ABORT")

    def cleanup(self):
        import shutil
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
        except Exception:                                              # noqa: BLE001
            pass
        shutil.rmtree(self.spool, ignore_errors=True)
