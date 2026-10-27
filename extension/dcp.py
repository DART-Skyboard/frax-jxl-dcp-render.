# SPDX-License-Identifier: GPL-3.0-or-later
# Digital Cinema Package (DCP) — festival / cinema delivery.
# A DCP is JPEG 2000 picture + PCM audio in MXF files with CPL/PKL/ASSETMAP XML (SMPTE ST 429 / DCI spec).
# Everything is built in, nothing else to install: a 16-bit TIFF master + 48 kHz/24-bit WAV rendered by Blender go through
# Blender's own JPEG 2000 writer (OpenJPEG, DCI cinema profile) and the Frax MXF / CPL / PKL writer in dcpwriter.py.
# 2K or 4K, SMPTE or Interop, stereo or 5.1. Runs on Blender's main thread in small timer slices (no threads).
import datetime
import os
import shutil
import sys

import numpy as np

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, StringProperty

# DCI containers (exact pixel sizes a DCP picture must have)
CONTAINERS = {
    ("FLAT", "2K"): (1998, 1080, "185"), ("SCOPE", "2K"): (2048, 858, "239"), ("FULL", "2K"): (2048, 1080, "190"),
    ("FLAT", "4K"): (3996, 2160, "185"), ("SCOPE", "4K"): (4096, 1716, "239"), ("FULL", "4K"): (4096, 2160, "190"),
}
CONTENT_TYPES = [("SHR", "Short", ""), ("FTR", "Feature", ""), ("TLR", "Trailer", ""), ("TSR", "Teaser", ""),
                 ("TST", "Test", ""), ("ADV", "Advertisement", ""), ("PSA", "Public service", ""), ("POL", "Policy", ""),
                 ("RTG", "Rating card", ""), ("XSN", "Transitional", "")]


class FRAXDCP_Settings(bpy.types.PropertyGroup):
    container: EnumProperty(name="Aspect", default="FLAT", items=[
        ("FLAT", "Flat 1.85", "1998×1080 / 3996×2160 — most films and shorts"),
        ("SCOPE", "Scope 2.39", "2048×858 / 4096×1716 — widescreen"),
        ("FULL", "Full container 1.90", "2048×1080 / 4096×2160")])
    resolution: EnumProperty(name="Resolution", default="2K", items=[("2K", "2K", "What almost every festival asks for"), ("4K", "4K", "DCI 4K, 24 fps only")])
    fps: EnumProperty(name="Frame rate", default="24", items=[
        ("24", "24 fps", "The safe choice for festivals"), ("25", "25 fps", "PAL territories"), ("30", "30 fps", ""),
        ("48", "48 fps (HFR)", ""), ("50", "50 fps (HFR)", ""), ("60", "60 fps (HFR)", "")])
    fit: EnumProperty(name="Fit", default="LETTERBOX", items=[
        ("RENDER", "Render at container size", "Change the scene resolution to the exact DCP size (best quality)"),
        ("LETTERBOX", "Fit into container", "Keep your resolution; it is scaled and letter/pillar-boxed into the container")])
    standard: EnumProperty(name="Standard", default="SMPTE", items=[("SMPTE", "SMPTE", "Current standard; most festivals require it"), ("interop", "Interop", "Legacy MXF Interop; only if a venue asks for it")])
    content_type: EnumProperty(name="Content type", default="SHR", items=CONTENT_TYPES)
    title: StringProperty(name="Title", default="")
    audio_lang: StringProperty(name="Audio language", default="EN", description="ISO 639 code, e.g. EN, FR, ES; XX for no dialogue")
    sub_lang: StringProperty(name="Subtitle language", default="XX", description="XX = no subtitles")
    territory: StringProperty(name="Territory", default="US")
    rating: StringProperty(name="Rating", default="NR")
    audio: EnumProperty(name="Audio", default="STEREO", items=[("STEREO", "Stereo (2.0)", ""), ("51", "5.1 surround", ""), ("NONE", "No audio (MOS)", "")])
    studio: StringProperty(name="Studio", default="DART")
    facility: StringProperty(name="Facility", default="RDS")
    output: StringProperty(name="Output folder", subtype="DIR_PATH", default="//DCP/")
    keep_master: BoolProperty(name="Keep the TIFF master", default=True)
    status: StringProperty(default="")
    progress: FloatProperty(default=0.0)


# ── ISDCF Digital Cinema Naming Convention ─────────────────────────────────────────────────────────────────────
def isdcf_name(scene):
    d = scene.frax_dcp
    title = "".join(ch for ch in (d.title or bpy.path.display_name(bpy.data.filepath) or scene.name).title() if ch.isalnum())[:14] or "Untitled"
    fps = d.fps if d.fps != "24" else ""
    aspect = {"FLAT": "F", "SCOPE": "S", "FULL": "C"}[d.container]
    audio = {"STEREO": "20", "51": "51", "NONE": "MOS"}[d.audio]
    date = datetime.date.today().strftime("%Y%m%d")
    std = "SMPTE" if d.standard == "SMPTE" else "IOP"
    parts = [title + ("-" + fps if fps else ""), d.content_type, aspect, "%s-%s" % (d.audio_lang.upper()[:3], d.sub_lang.upper()[:3]),
             "%s-%s" % (d.territory.upper()[:3], d.rating.upper()[:5]), audio, d.resolution, d.studio.upper()[:4], date,
             d.facility.upper()[:4], std, "OV"]
    return "_".join(p for p in parts if p)


# ── compliance checks shown in the panel ───────────────────────────────────────────────────────────────────────
def checks(scene):
    d, r = scene.frax_dcp, scene.render
    w, h, _ = CONTAINERS[(d.container, d.resolution)]
    rw, rh = r.resolution_x * r.resolution_percentage // 100, r.resolution_y * r.resolution_percentage // 100
    fps = r.fps / r.fps_base
    out = []
    if (rw, rh) == (w, h):
        out.append((True, "Resolution %d×%d matches the %s %s container" % (w, h, d.resolution, d.container.title())))
    elif d.fit == "LETTERBOX":
        out.append((True, "%d×%d will be fitted into %d×%d (letter/pillar-boxed)" % (rw, rh, w, h)))
    else:
        out.append((False, "Render size %d×%d ≠ DCP %d×%d — press Apply DCP format" % (rw, rh, w, h)))
    if abs(fps - int(d.fps)) < 1e-6:
        out.append((True, "Frame rate %s fps" % d.fps))
    else:
        out.append((False, "Scene runs at %.3f fps; DCP is %s fps — press Apply DCP format" % (fps, d.fps)))
    if r.pixel_aspect_x != r.pixel_aspect_y:
        out.append((False, "Pixel aspect must be 1:1"))
    if d.audio != "NONE":
        has_sound = any(s.type == "SOUND" for s in (scene.sequence_editor.strips_all if scene.sequence_editor and hasattr(scene.sequence_editor, "strips_all")
                                                      else (scene.sequence_editor.sequences_all if scene.sequence_editor else [])))
        out.append((True, "Audio will be mixed down at 48 kHz / 24-bit") if has_sound else (False, "No sound strips found — choose No audio (MOS) or add sound"))
    secs = (scene.frame_end - scene.frame_start + 1) / max(fps, 1)
    out.append((True, "Length %d:%02d (%d frames)" % (secs // 60, secs % 60, scene.frame_end - scene.frame_start + 1)))
    out.append(builtin_ok(d, fps))
    return out


def builtin_ok(d, fps=None):
    if d.resolution == "4K" and int(d.fps) != 24:
        return False, "DCI 4K is 24 fps only — pick 2K for %s fps" % d.fps
    if int(d.fps) > 48:
        return False, "%s fps pushes JPEG 2000 above the DCI 250 Mbit/s limit — 24/25/30/48 are safe" % d.fps
    return True, "%s %s DCP, JPEG 2000 under the DCI limit, unencrypted — nothing to install" % (
        d.resolution, "SMPTE" if d.standard == "SMPTE" else "Interop")


# ── built-in builder: TIFF master → JPEG 2000 (Blender) → MXF + CPL/PKL/ASSETMAP (dcpwriter) ─────────────────────
IMAGE_EXTS = (".tif", ".tiff", ".exr", ".png", ".jpg", ".jpeg", ".dpx", ".bmp", ".tga")


class BuiltinDCPJob:
    """Runs on Blender's main thread in short timer slices (the JPEG 2000 writer is Blender's own), so the UI stays
    responsive and no threads are needed. Phases: frames → picture hash → wait for audio → sound → sound hash → XML."""
    current = None

    def __init__(self, scene, master_dir, wav, film_dir, delete_master=False):
        from . import dcpwriter
        self.W = dcpwriter
        d = scene.frax_dcp
        self.scene_name = scene.name
        self.files = sorted(os.path.join(master_dir, f) for f in os.listdir(master_dir)
                            if f.lower().endswith(IMAGE_EXTS) and not f.startswith("."))
        self.master_dir = master_dir if delete_master else None
        self.wav, self.film_dir = wav, film_dir
        self.name = os.path.basename(film_dir.rstrip("/\\"))
        self.w, self.h, self.ratio = CONTAINERS[(d.container, d.resolution)]
        self.fps = int(d.fps)
        self.interop = d.standard == "interop"
        self.k4 = d.resolution == "4K"
        self.meta = dict(kind=d.content_type, territory=d.territory.upper()[:3] or "US", facility=d.facility,
                         audio=d.audio, title=d.title or self.name, issuer=d.studio or "Radical Deepscale",
                         lang=(d.audio_lang or "en").lower()[:3])
        self.status, self.progress, self.done, self.ok, self.cancelled = "Preparing…", 0.0, False, False, False
        self.i = 0
        self.pic = self.snd = None
        self.phase = "frames"
        self.hash = None
        self.hashes = {}
        self.blocks = None
        self.wait_t0 = None
        self.last_size = -1
        if not self.files:
            self._fail("no frames found in " + master_dir)
            return
        ok, why = builtin_ok(d)
        if not ok:
            self._fail(why)
            return
        os.makedirs(film_dir, exist_ok=True)
        self.tmp = os.path.join(film_dir, ".frax_frame.j2c")
        self._setup(scene)
        self.pic = self.W.TrackFile(os.path.join(film_dir, self.name + "_pic.mxf"), "picture", self.fps, interop=self.interop)
        if bpy.app.background:                               # no event loop: run to the end right here
            import time
            while True:
                r = self._tick()
                if r is None:
                    break
                time.sleep(min(r, 0.05))
        else:
            bpy.app.timers.register(self._tick, first_interval=0.05)

    def _setup(self, scene):
        st, vs = scene.render.image_settings, scene.view_settings
        self.keep = {"st": {k: getattr(st, k) for k in (("media_type",) if hasattr(st, "media_type") else ()) + ("file_format", "color_mode", "color_depth", "quality")},
                     "vs": {k: getattr(vs, k) for k in ("view_transform", "look", "exposure", "gamma", "use_curve_mapping")}}
        for k in ("jpeg2k_codec", "use_jpeg2k_cinema_preset", "use_jpeg2k_cinema_48", "use_jpeg2k_ycc"):
            self.keep["st"][k] = getattr(st, k)
        if hasattr(st, "media_type"):
            st.media_type = "IMAGE"
        st.file_format = "JPEG2000"
        st.jpeg2k_codec = "J2K"
        st.color_mode = "RGB"
        st.color_depth = "12"
        st.use_jpeg2k_cinema_preset = True
        st.use_jpeg2k_cinema_48 = not self.k4      # 2K: half the 24 fps budget (≈125 Mbit/s); 4K at 24 fps already fits under 250
        st.use_jpeg2k_ycc = False
        vs.view_transform, vs.look, vs.exposure, vs.gamma, vs.use_curve_mapping = "Standard", "None", 0.0, 1.0, False
        self.img = bpy.data.images.new(".frax_dcp_frame", self.w, self.h, alpha=False, float_buffer=True)
        self.img.colorspace_settings.name = "Non-Color"     # X'Y'Z' code values go straight to the codestream

    def _restore(self):
        scene = bpy.data.scenes.get(self.scene_name)
        if scene and getattr(self, "keep", None):
            st, vs = scene.render.image_settings, scene.view_settings
            for k_ in ("media_type", "file_format"):
                try:
                    if k_ in self.keep["st"]:
                        setattr(st, k_, self.keep["st"][k_])
                except Exception:
                    pass
            for obj, kv in ((st, self.keep["st"]), (vs, self.keep["vs"])):
                for k, v in kv.items():
                    try:
                        setattr(obj, k, v)
                    except Exception:
                        pass
        if getattr(self, "img", None):
            try:
                bpy.data.images.remove(self.img)
            except Exception:
                pass
            self.img = None

    def _read(self, path):
        from . import capture, frames
        try:
            return frames.read_any(path)
        except ValueError:
            return capture.load_file_pixels(path)          # PNG / DPX / JPEG …: Blender reads it (not inside a render)

    def _tick(self):
        """One slice of work; returns seconds until the next slice, or None when finished."""
        if self.cancelled:
            self._abort("cancelled")
            return None
        import time
        t0 = time.time()
        try:
            return getattr(self, "_p_" + self.phase)(t0)
        except Exception as e:                                       # noqa: BLE001
            self._abort("%s: %s" % (self.phase, e))
            return None

    def _p_frames(self, t0):
        import time
        scene = bpy.data.scenes.get(self.scene_name)
        while self.i < len(self.files) and time.time() - t0 < 0.25:
            arr, linear = self._read(self.files[self.i])
            rgb = self.W.fit_into(arr[..., :3], self.w, self.h)
            xyz = self.W.srgb_to_xyz12(rgb, linear)
            px = np.ones((self.h, self.w, 4), np.float32)
            px[..., :3] = xyz[::-1]
            self.img.pixels.foreach_set(px.ravel())
            self.img.save_render(filepath=self.tmp, scene=scene)
            with open(self.tmp, "rb") as f:
                self.pic.write_frame(f.read())
            self.i += 1
        self.progress = 0.8 * self.i / len(self.files)
        self.status = "JPEG 2000 frame %d / %d" % (self.i, len(self.files))
        if self.i < len(self.files):
            return 0.01
        self._restore()
        self.pic.close(hash=False)
        self.hash = self.W.Sha1Job(self.pic.path)
        self.phase = "hash_pic"
        return 0.01

    def _p_hash_pic(self, t0):
        self.status = "Checksumming picture MXF…"
        self.progress = 0.8 + 0.1 * self.hash.done_bytes / max(self.hash.size, 1)
        if not self.hash.step():
            return 0.01
        self.pic.sha1 = self.hashes[self.pic.path] = self.hash.b64
        self.hash = None
        self.phase = "wait_wav" if self.wav else "xml"
        return 0.01

    def _p_wait_wav(self, t0):
        """The mixdown is a Blender job: wait (without blocking) until the WAV exists and has stopped growing."""
        import time
        self.status = "Waiting for the audio mixdown…"
        if self.wait_t0 is None:
            self.wait_t0 = time.time()
        if time.time() - getattr(self, "_chk", 0) < 0.5:
            return 0.1
        self._chk = time.time()
        n = os.path.getsize(self.wav) if os.path.exists(self.wav) else -1
        stable = n > 44 and n == self.last_size
        self.last_size = n
        if stable:
            self.snd = self.W.TrackFile(os.path.join(self.film_dir, self.name + "_snd.mxf"), "sound", self.fps,
                                        channels=6, language=self.meta["lang"], interop=self.interop)
            self.blocks = self.W.wav_frames(self.wav, self.fps, self.pic.frames, self.meta["audio"])
            self.phase = "sound"
            return 0.01
        if time.time() - self.wait_t0 > 300:                           # no audio arrived: carry on picture-only
            self.wav = None
            self.phase = "xml"
        return 0.5

    def _p_sound(self, t0):
        import time
        for blk in self.blocks:
            self.snd.write_frame(blk)
            if time.time() - t0 > 0.2:
                self.status = "Writing sound MXF (%d / %d)" % (self.snd.frames, self.pic.frames)
                return 0.01
        self.snd.close(hash=False)
        self.hash = self.W.Sha1Job(self.snd.path)
        self.phase = "hash_snd"
        return 0.01

    def _p_hash_snd(self, t0):
        self.status = "Checksumming sound MXF…"
        if not self.hash.step():
            return 0.01
        self.snd.sha1 = self.hashes[self.snd.path] = self.hash.b64
        self.hash = None
        self.phase = "xml"
        return 0.01

    def _p_xml(self, t0):
        self.status = "Writing CPL, PKL and ASSETMAP…"
        m = self.meta
        self.W.finish_package(self.film_dir, self.name, m["kind"], self.pic, self.snd, self.fps, self.w, self.h,
                              issuer=m["issuer"], territory=m["territory"], facility=m["facility"], audio=m["audio"],
                              title=m["title"], interop=self.interop, hashes=self.hashes, container_ratio=self.w / self.h)
        self._finish()
        return None

    def _finish(self):
        try:
            os.remove(self.tmp)
        except OSError:
            pass
        if self.master_dir:
            shutil.rmtree(self.master_dir, ignore_errors=True)
        self.ok, self.done, self.progress = True, True, 1.0
        self.status = "DCP ready in " + self.film_dir
        self._log()

    def _abort(self, why):
        self._restore()
        for tf in (self.pic, self.snd):
            try:
                if tf and not tf.f.closed:
                    tf.f.close()
            except Exception:
                pass
        try:
            os.remove(self.tmp)
        except Exception:
            pass
        self._fail(why)

    def _fail(self, why):
        self.status = "DCP failed: " + why
        self.done = True
        self._log()

    def _log(self):
        try:
            with open(self.film_dir.rstrip("/\\") + "_log.txt", "w", encoding="utf-8") as f:
                f.write("Frax JXL DCP Render built-in DCP writer\n%s\nframes: %d\n" % (self.status, self.i))
        except Exception:
            pass

    def cancel(self):
        self.cancelled = True


def make_job(scene, master_dir, wav, film_dir):
    d = scene.frax_dcp
    return BuiltinDCPJob(scene, master_dir, wav, film_dir, delete_master=not d.keep_master)


classes = (FRAXDCP_Settings,)
