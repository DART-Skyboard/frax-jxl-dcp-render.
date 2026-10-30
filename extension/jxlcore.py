# SPDX-License-Identifier: GPL-3.0-or-later
# Pure-numpy core of the JPEG XL pipeline (no bpy): colour maths, the per-file Plan, focus-area frames, and the
# settings → encoder-options mapping. It is shared by Blender's main process (stills) and by the separate worker
# process that packs animations (worker.py), which is why it must never import bpy.
import numpy as np

from frax_libjxl import engine
from frax_libjxl.engine import ID, quality_to_distance

from . import pyengine

# ── colour maths ──────────────────────────────────────────────────────────────────────────────────────────────
M_709_TO_2020 = np.array([[0.6274040, 0.3292820, 0.0433136], [0.0690970, 0.9195400, 0.0113612], [0.0163916, 0.0880132, 0.8955950]], np.float32)
M_709_TO_P3 = np.array([[0.8224622, 0.1775378, 0.0], [0.0331942, 0.9668058, 0.0], [0.0170827, 0.0723974, 0.9105199]], np.float32)


def srgb_to_linear(v):
    v = np.clip(v, 0.0, None)
    return np.where(v <= 0.04045, v / 12.92, ((v + 0.055) / 1.055) ** 2.4).astype(np.float32)


def linear_to_srgb(v):
    v = np.clip(v, 0.0, None)
    return np.where(v <= 0.0031308, v * 12.92, 1.055 * np.power(v, 1 / 2.4) - 0.055).astype(np.float32)


def linear_to_bt709(v):
    v = np.clip(v, 0.0, None)
    return np.where(v < 0.018, 4.5 * v, 1.099 * np.power(v, 0.45) - 0.099).astype(np.float32)


def pq_encode(nits):            # SMPTE ST 2084
    m1, m2, c1, c2, c3 = 0.1593017578125, 78.84375, 0.8359375, 18.8515625, 18.6875
    y = np.clip(nits / 10000.0, 0.0, 1.0) ** m1
    return ((c1 + c2 * y) / (1 + c3 * y)) ** m2


def hlg_encode(e):              # ARIB STD-B67 / BT.2100
    a, b, c = 0.17883277, 0.28466892, 0.55991073
    e = np.clip(e, 0.0, 1.0)
    return np.where(e <= 1 / 12, np.sqrt(3 * e), a * np.log(np.maximum(12 * e - b, 1e-9)) + c).astype(np.float32)


def to_target(rgb, linear_src, color):
    """rgb HxWx3 float → values in the JPEG XL colour space chosen. Blender's scene-linear is Rec.709 primaries;
    HDR uses BT.2408 reference white (scene value 1.0 = 203 nits)."""
    color = int(color)
    if color == 0 and not linear_src:
        return rgb                                                    # already sRGB display values: keep exactly
    lin = rgb if linear_src else srgb_to_linear(rgb)
    if color == 0:
        return linear_to_srgb(lin)
    if color == 1:
        return lin
    if color == 2:
        return linear_to_srgb(lin @ M_709_TO_P3.T)
    l2020 = np.clip(lin @ M_709_TO_2020.T, 0.0, None)
    if color == 5:
        return linear_to_bt709(l2020)
    if color == 3:
        return pq_encode(l2020 * 203.0).astype(np.float32)
    if color == 4:
        return hlg_encode(l2020 * 0.2652)
    return rgb



def _opt(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return -1


def frame_options(s, distance=None, lossless=None):
    """Settings → the dict Encoder.add_frame expects."""
    ints, floats = {}, {}

    def I(k, v):
        v = _opt(v)
        if v != -1:
            ints[ID[k]] = v
    I("EFFORT", s.effort)
    if s.decoding_speed:
        I("DECODING_SPEED", s.decoding_speed)
    for k, a in (("MODULAR_GROUP_SIZE", "group_size"), ("GROUP_ORDER", "group_order"), ("PROGRESSIVE_DC", "progressive_dc"),
                 ("PROGRESSIVE_AC", "progressive_ac"), ("QPROGRESSIVE_AC", "qprogressive_ac"), ("RESPONSIVE", "responsive"),
                 ("BUFFERING", "buffering"), ("EPF", "epf"), ("GABORISH", "gaborish"), ("NOISE", "noise"), ("DOTS", "dots"),
                 ("PATCHES", "patches"), ("RESAMPLING", "resampling"), ("EC_RESAMPLING", "ec_resampling"),
                 ("KEEP_INVISIBLE", "keep_invisible"), ("LOSSY_PALETTE", "lossy_palette"), ("COLOR_TRANSFORM", "color_transform"),
                 ("DISABLE_PERCEPTUAL", "disable_perceptual"), ("FULL_IMAGE_HEURISTICS", "full_heuristics"),
                 ("JPEG_COMPRESS_BOXES", "compress_boxes")):
        I(k, getattr(s, a))
    I("CENTER_X", s.center_x)
    I("CENTER_Y", s.center_y)
    I("MODULAR", 1 if s.mode == "MODULAR" else s.force_modular)
    I("MODULAR_PREDICTOR", s.predictor)
    I("PALETTE_COLORS", s.palette_colors)
    I("NB_PREV_CH", s.nb_prev)
    I("MODULAR_COLOR_SPACE", s.modular_color_space)
    I("BROTLI_EFFORT", s.brotli_effort)
    if s.photon_noise > 0:
        floats[ID["PHOTON_NOISE"]] = s.photon_noise
    if s.ma_tree >= 0:
        floats[ID["MA_TREE_PCT"]] = s.ma_tree
    if s.colors_global >= 0:
        floats[ID["CH_COLORS_GLOBAL"]] = s.colors_global
    if s.colors_group >= 0:
        floats[ID["CH_COLORS_GROUP"]] = s.colors_group
    ll = (s.mode == "LOSSLESS") if lossless is None else lossless
    return dict(ints=ints, floats=floats, lossless=ll, distance=None if ll else (s.distance if distance is None else distance),
                alpha_distance=0.0 if s.alpha_lossless else quality_to_distance(s.alpha_quality))


def depth_params(s):
    """→ bits, exp_bits, numpy dtype for the chosen bit depth"""
    import numpy as np
    return {"8": (8, 0, np.uint8), "10": (10, 0, np.uint16), "12": (12, 0, np.uint16), "16": (16, 0, np.uint16),
            "F16": (16, 5, np.float32), "F32": (32, 8, np.float32)}[s.bit_depth]



# ── settings → encoder job ────────────────────────────────────────────────────────────────────────────────────
def analyse(arr):
    a = arr[..., 3]
    has_alpha = bool((a < 0.999).any())
    rgb = arr[..., :3]
    grey = bool(np.abs(rgb[..., 0] - rgb[..., 1]).max() < 1e-4 and np.abs(rgb[..., 1] - rgb[..., 2]).max() < 1e-4)
    return has_alpha, grey


class Plan:
    """Everything fixed for the whole file: depth, channels, alpha, colour. Built from the first frame."""

    def __init__(self, s, first, linear_src, film_transparent=False):
        self.s = s
        has_alpha, grey = analyse(first)
        self.bits, self.exp, self.dtype = depth_params(s)
        self.float_out = self.exp > 0
        self.linear_src = linear_src
        self.color = int(s.color)
        if linear_src and not self.float_out and self.color == 1:
            self.color = 0                                              # linear in 8–16-bit integer bands badly: store sRGB
        self.grey = s.channels == "GREY" or (s.channels == "AUTO" and grey)
        self.alpha = s.alpha == "KEEP" or (s.alpha == "AUTO" and (has_alpha or film_transparent))
        if self.alpha:
            if s.alpha_bits == "SAME":
                self.alpha_bits, self.alpha_exp = self.bits, self.exp
            else:
                self.alpha_bits, self.alpha_exp = int(s.alpha_bits), (5 if self.float_out and s.alpha_bits == "16" else 0)
        else:
            self.alpha_bits = self.alpha_exp = 0
        self.channels = 1 if self.grey else 3

    def convert(self, arr, crop=None):
        if crop:
            x, y, w, h = crop
            arr = arr[y:y + h, x:x + w]
        rgb = to_target(arr[..., :3], self.linear_src, self.color)
        if self.grey:
            rgb = (0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2])[..., None]
        parts = [rgb]
        if self.alpha:
            parts.append(arr[..., 3:4])
        out = np.concatenate(parts, axis=2) if len(parts) > 1 else rgb
        if self.float_out:
            if self.bits == 16:                                             # half floats: round once, here
                out = out.astype(np.float16)
            return np.ascontiguousarray(out, dtype=np.float32)
        top = 255.0 if self.dtype == np.uint8 else 65535.0
        return np.ascontiguousarray(np.clip(out, 0, 1) * top + 0.5).astype(self.dtype)

    def encoder(self, width, height, animated=False, fps=(24, 1), boxes=(), cache_dir=None):
        s = self.s
        lossless = s.mode == "LOSSLESS"
        cls = engine.Encoder if use_libjxl(s) else pyengine.Encoder
        return cls(width, height, bits=self.bits, exp_bits=self.exp, channels=self.channels,
                              alpha_bits=self.alpha_bits, alpha_exp=self.alpha_exp, premultiplied=s.premultiplied,
                              animated=animated, tps_num=fps[0], tps_den=fps[1], loops=s.loops,
                              original_profile=lossless or s.color_transform == "1", intensity_target=s.intensity,
                              color=self.color, intent=int(s.intent), container=int(s.container), level=int(s.level),
                              boxes=boxes, cache_dir=cache_dir, lossy_float=not lossless)



def focus_pixels(rects, w, h):
    out = []
    for (x, y, rw, rh) in rects:
        px, py = max(0, int(round(x * w))), max(0, int(round(y * h)))
        pw, ph = min(w - px, int(round(rw * w))), min(h - py, int(round(rh * h)))
        if pw >= 8 and ph >= 8:
            out.append((px, py, pw, ph))
    return out



def use_libjxl(s):
    return getattr(s, "engine", "PYTHON") == "LIBJXL" and engine.available()


def add_with_focus(enc, plan, arr, rects_px, duration):
    """One displayed frame.  Pure-Python engine: focus tiles are coded at the focus quality inside the same frame
    (and stored first).  libjxl engine: base image, then sharper focus layers on top (crop + replace blending)."""
    s = plan.s
    base = frame_options(s)
    if not rects_px or s.mode == "LOSSLESS":
        enc.add_frame(plan.convert(arr), base, duration=duration)
        return
    if isinstance(enc, pyengine.Encoder):
        enc.add_frame(plan.convert(arr), base, duration=duration, focus=rects_px, focus_quality=s.focus_quality,
                      focus_first=s.focus_first and int(s.group_order) == -1)
        return
    if s.focus_first and int(s.group_order) == -1:
        x, y, w, h = rects_px[0]
        base["ints"][13] = 1
        base["ints"][14] = x + w // 2
        base["ints"][15] = y + h // 2
    enc.add_frame(plan.convert(arr), base, duration=0)
    fd = min(engine.quality_to_distance(s.focus_quality), s.distance)
    for i, c in enumerate(rects_px):
        last = i == len(rects_px) - 1
        enc.add_frame(plan.convert(arr, crop=c), frame_options(s, distance=fd, lossless=False),
                      duration=duration if last else 0, crop=c, blend=0, name="focus %d" % (i + 1))


def xmp_xml(s, info, blender_version="", note="Frax JPEG XL / DCP Render"):
    """The XMP packet for the 'xml ' box (pure string building; `info` is a dict of frax:* attributes)."""
    def esc(t):
        return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    x = ('<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?><x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
         'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description rdf:about="" '
         'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:xmp="http://ns.adobe.com/xap/1.0/" '
         'xmlns:frax="https://radicaldeepscale.com/ns/frax/1.0/" xmp:CreatorTool="Blender %s \u00b7 %s"' % (esc(blender_version), note))
    x += "".join(' frax:%s="%s"' % (k, esc(v)) for k, v in info.items()) + ">"
    if s.title:
        x += '<dc:title><rdf:Alt><rdf:li xml:lang="x-default">%s</rdf:li></rdf:Alt></dc:title>' % esc(s.title)
    if s.author:
        x += "<dc:creator><rdf:Seq><rdf:li>%s</rdf:li></rdf:Seq></dc:creator>" % esc(s.author)
    if s.copyright:
        x += '<dc:rights><rdf:Alt><rdf:li xml:lang="x-default">%s</rdf:li></rdf:Alt></dc:rights>' % esc(s.copyright)
    x += '</rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end="w"?>'
    return x
