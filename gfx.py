"""Low-level drawing helpers. Everything composites additively so the neon reads
like emitted light rather than a coloured line sitting on top of the map."""

import os

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

# --------------------------------------------------------------------- fonts

FONT_CANDIDATES = [
    # Drop an Anton / Bebas Neue / Inter ttf next to the project and it wins.
    "fonts/Anton-Regular.ttf",
    "fonts/BebasNeue-Regular.ttf",
    "fonts/Inter-Black.ttf",
    "/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]

_font_cache = {}


def _font_file():
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            return p
    raise RuntimeError("No usable bold font found. Put a .ttf in ./fonts/")


def font(size):
    size = int(round(size))
    if size not in _font_cache:
        _font_cache[size] = ImageFont.truetype(_font_file(), size)
    return _font_cache[size]


# --------------------------------------------------------------------- easing

def clamp01(t):
    return max(0.0, min(1.0, t))


def ease_in_out(t):
    t = clamp01(t)
    return t * t * (3 - 2 * t)


def ease_out_cubic(t):
    t = clamp01(t)
    return 1 - (1 - t) ** 3


def ease_out_back(t, s=1.6):
    t = clamp01(t) - 1
    return t * t * ((s + 1) * t + s) + 1


# ---------------------------------------------------------------- compositing

def add_rgb(canvas, layer):
    """Both float32 HxWx3. Compositing stays in float for the whole frame and is
    clipped once at the end, instead of clamping to uint8 after every draw."""
    canvas += layer


def tint(mask, color, gain=1.0):
    """L mask (float32 0..1) -> HxWx3 float32 in the given colour."""
    return mask[..., None] * (np.array(color, np.float32) * gain)


def bloom(mask, sigma):
    """Blur an L mask (uint8 0..255 or float32 0..1) and return float32 0..1.
    Blurring stays in float32 -- blurring a uint8 mask quantizes a wide-sigma
    halo to 1/255 steps before it's even tinted, which shows up as concentric
    banding rings around every glow (exactly what the output dither exists to
    hide, so it's cheaper to just not introduce it here)."""
    if mask.dtype == np.uint8:
        mask = mask.astype(np.float32) / 255.0
    return cv2.GaussianBlur(mask, (0, 0), sigma, borderType=cv2.BORDER_REPLICATE)


# ----------------------------------------------------------------- neon lines

def _bbox(pts, pad, W, H):
    """Clip a point cloud's bounding box to the frame. Everything draws into this
    ROI instead of allocating a full 1080x1920 buffer per call."""
    p = np.asarray(pts, np.float32).reshape(-1, 2)
    x0 = int(np.floor(p[:, 0].min())) - pad
    x1 = int(np.ceil(p[:, 0].max())) + pad
    y0 = int(np.floor(p[:, 1].min())) - pad
    y1 = int(np.ceil(p[:, 1].max())) + pad
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(W, x1), min(H, y1)
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1


# fixed-point precision for sub-pixel cv2 drawing (1/16 px). Without this,
# cv2.polylines/circle snap every point to a whole pixel with plain int32
# coords, which (a) truncates asymmetrically around 0, jittering the route
# and every dot by up to a pixel as the camera pans, and (b) forces stroke
# widths to whole pixels, which visibly steps the entire loop's thickness a
# handful of times during the whip/reveal as the zoom-driven width changes.
_FX_SHIFT = 4
_FX_MUL = 1 << _FX_SHIFT


def _fx(pts):
    return (np.asarray(pts, np.float32) * _FX_MUL).astype(np.int32)


def _stroke_mask(shape, pts, width):
    """Float32 0..1 open-polyline mask with fractional line width, built by
    blending the floor/ceil integer-width strokes -- cv2 has no fractional
    thickness, so a width that continuously changes frame to frame (zoom-driven)
    would otherwise step by whole pixels."""
    w0 = max(1, int(np.floor(width)))
    frac = width - w0
    pf = [_fx(pts)]
    buf0 = np.zeros(shape, np.uint8)
    cv2.polylines(buf0, pf, False, 255, w0, cv2.LINE_AA, shift=_FX_SHIFT)
    if frac < 1e-3:
        return buf0.astype(np.float32) / 255.0
    buf1 = np.zeros(shape, np.uint8)
    cv2.polylines(buf1, pf, False, 255, w0 + 1, cv2.LINE_AA, shift=_FX_SHIFT)
    return (buf0.astype(np.float32) * (1 - frac) + buf1.astype(np.float32) * frac) / 255.0


def _dot_mask(shape, pts, radius):
    """Float32 0..1 filled-circle mask with fractional radius; see _stroke_mask."""
    r0 = max(1, int(np.floor(radius)))
    frac = radius - r0
    pf = _fx(pts)
    buf0 = np.zeros(shape, np.uint8)
    for x, y in pf:
        cv2.circle(buf0, (int(x), int(y)), r0 * _FX_MUL, 255, -1, cv2.LINE_AA, shift=_FX_SHIFT)
    if frac < 1e-3:
        return buf0.astype(np.float32) / 255.0
    buf1 = np.zeros(shape, np.uint8)
    for x, y in pf:
        cv2.circle(buf1, (int(x), int(y)), (r0 + 1) * _FX_MUL, 255, -1, cv2.LINE_AA, shift=_FX_SHIFT)
    return (buf0.astype(np.float32) * (1 - frac) + buf1.astype(np.float32) * frac) / 255.0


def neon_polyline(canvas, pts, core_color, mid_color, outer_color,
                  core_w=4, mid_w=7, intensity=1.0, glow_boost=1.0):
    """glow_boost multiplies only the bloom layers, leaving the core line on
    plain `intensity` -- lets a caller swell the glow (e.g. a breathing hook)
    without the core line itself changing brightness. Defaults to a no-op."""
    if len(pts) < 2:
        return
    H, W = canvas.shape[:2]
    roi = _bbox(pts, int(mid_w * 4 + 40), W, H)
    if roi is None:
        return
    x0, y0, x1, y1 = roi
    rw, rh = x1 - x0, y1 - y0
    p = (np.asarray(pts, np.float32) - [x0, y0])

    # glow is authored at half res at 1080p; s grows with _scale so the
    # downsample cost stays flat above 1080p instead of the glow buffer
    # growing 4x at 4K for no visual gain -- the blur hides the resample
    # either way. The 26/9/3 sigmas are raw 1080p-authored pixels (unlike
    # core_w/mid_w, which callers already scale), so they're scaled up to
    # the current resolution before being brought back into buffer space.
    s = max(2, int(round(2 * _scale)))
    h2, w2 = max(2, rh // s), max(2, rw // s)
    m = _stroke_mask((h2, w2), p / s, max(1.0, mid_w / s))

    glow = tint(bloom(m, 26 * _scale / s), outer_color, 0.55 * intensity * glow_boost)
    glow += tint(bloom(m, 9 * _scale / s), mid_color, 0.80 * intensity * glow_boost)
    glow += tint(bloom(m, 3 * _scale / s), mid_color, 0.55 * intensity * glow_boost)
    glow = cv2.resize(glow, (rw, rh), interpolation=cv2.INTER_LINEAR)

    core = _stroke_mask((rh, rw), p, max(1.0, core_w))
    glow += tint(core, core_color, intensity)

    canvas[y0:y1, x0:x1] += glow


def flat_polyline(canvas, pts, color, width=2, alpha=0.8):
    if len(pts) < 2:
        return
    H, W = canvas.shape[:2]
    roi = _bbox(pts, int(width) + 4, W, H)
    if roi is None:
        return
    x0, y0, x1, y1 = roi
    m = _stroke_mask((y1 - y0, x1 - x0),
                     np.asarray(pts, np.float32) - [x0, y0], max(1.0, width))
    canvas[y0:y1, x0:x1] += tint(m, color, alpha)


def neon_dots(canvas, pts, color, radius=5, glow_sigma=10, intensity=1.0):
    if len(pts) == 0:
        return
    H, W = canvas.shape[:2]
    roi = _bbox(pts, int(radius + glow_sigma * 3 + 8), W, H)
    if roi is None:
        return
    x0, y0, x1, y1 = roi
    rw, rh = x1 - x0, y1 - y0
    p = (np.asarray(pts, np.float32) - [x0, y0])

    # radius/glow_sigma here are already output-resolution pixels (callers
    # scale them); only the downsample factor itself needs to track _scale,
    # so the glow buffer's cost stays flat instead of growing with OUT_W
    s = max(2, int(round(2 * _scale)))
    h2, w2 = max(2, rh // s), max(2, rw // s)
    m = _dot_mask((h2, w2), p / s, max(1.0, radius / s))
    glow = cv2.resize(tint(bloom(m, glow_sigma / s), color, 0.9 * intensity),
                      (rw, rh), interpolation=cv2.INTER_LINEAR)

    core = _dot_mask((rh, rw), p, max(1.0, radius))
    glow += tint(core, color, 0.85 * intensity)
    canvas[y0:y1, x0:x1] += glow


def flat_dots(canvas, pts, color, radius=3, alpha=0.8):
    if len(pts) == 0:
        return
    H, W = canvas.shape[:2]
    roi = _bbox(pts, int(radius) + 3, W, H)
    if roi is None:
        return
    x0, y0, x1, y1 = roi
    m = _dot_mask((y1 - y0, x1 - x0),
                  np.asarray(pts, np.float32) - [x0, y0], max(1.0, radius))
    canvas[y0:y1, x0:x1] += tint(m, color, alpha)


_scale = 1.0   # set once per render via set_scale(); see neon_polyline/neon_dots


def set_scale(scale):
    """scale = OUT_W / 1080. Keeps glow width/cost proportional to the
    1080p-authored look at any output resolution -- without this, a plain
    resolution bump would make the neon glow relatively half as wide at 4K
    while costing 4x more to compute."""
    global _scale
    _scale = scale


_sprite_cache = {}


def head_flare(canvas, x, y, color, size=140, intensity=1.0):
    """Hot radial flare at the leading edge of the route."""
    # quantized to keep the sprite-cache key space small (size continuously
    # varies with zoom + the stop pop, so without this every frame at high
    # --res would mint a near-unique sprite) and to make the cap below safe
    size = max(4, int(round(size / 4)) * 4)
    key = (size,)
    if key not in _sprite_cache:
        if len(_sprite_cache) > 400:
            # evict before inserting -- clearing *after* the insert wiped the
            # entry this call had just written, so the lookup right below it
            # KeyError'd on any run with enough distinct sizes (e.g. --res 2160)
            _sprite_cache.clear()
        r = max(2, size // 2)
        yy, xx = np.mgrid[-r:r, -r:r].astype(np.float32)
        d = np.sqrt(xx ** 2 + yy ** 2) / r
        _sprite_cache[key] = np.clip(1 - d, 0, 1) ** 2.6
    spr = _sprite_cache[key]
    r = spr.shape[0] // 2
    _paste_add(canvas, spr[..., None] * (np.array(color, np.float32) * intensity),
               int(round(x)) - r, int(round(y)) - r)


def ring(canvas, x, y, radius, color, width=3, alpha=1.0):
    if alpha <= 0.01 or radius < 1:
        return
    H, W = canvas.shape[:2]
    pad = int(width + 26)
    roi = _bbox([[x - radius, y - radius], [x + radius, y + radius]], pad, W, H)
    if roi is None:
        return
    x0, y0, x1, y1 = roi
    cx, cy = _fx([[x - x0, y - y0]])[0]
    w0 = max(1, int(np.floor(width)))
    frac = width - w0
    m = np.zeros((y1 - y0, x1 - x0), np.uint8)
    cv2.circle(m, (int(cx), int(cy)), int(round(radius * _FX_MUL)), 255,
              w0, cv2.LINE_AA, shift=_FX_SHIFT)
    m = m.astype(np.float32) / 255.0
    if frac >= 1e-3:
        m1 = np.zeros((y1 - y0, x1 - x0), np.uint8)
        cv2.circle(m1, (int(cx), int(cy)), int(round(radius * _FX_MUL)), 255,
                  w0 + 1, cv2.LINE_AA, shift=_FX_SHIFT)
        m = m * (1 - frac) + (m1.astype(np.float32) / 255.0) * frac
    layer = tint(bloom(m, 8 * _scale), color, 0.7 * alpha)
    layer += tint(m, color, alpha)
    canvas[y0:y1, x0:x1] += layer


# ----------------------------------------------------------------------- text

def measure(text, f, letter_spacing=0):
    """Ink width of a string at a given font, including letter spacing."""
    if not text:
        return 0.0
    if letter_spacing == 0:
        b = f.getbbox(text)
        return float(b[2] - b[0])
    return float(sum(f.getlength(ch) for ch in text) + letter_spacing * (len(text) - 1))


def fit_size(text, size, max_width, letter_spacing=0, min_size=10):
    """Shrink a font size until the string fits max_width.

    Long brand names ("EVERY MCDONALD'S") overflow a 1080px frame at the headline
    size that suits "EVERY COSTCO", so sizes are treated as a ceiling rather than
    a fixed value. Letter spacing scales with the text so tracking stays even.
    """
    if not max_width or not text:
        return size, letter_spacing
    w = measure(text, font(size), letter_spacing)
    if w <= max_width:
        return size, letter_spacing
    k = max_width / w
    new = max(min_size, int(size * k))
    # integer font sizes round down unpredictably; step until it really fits
    while new > min_size and measure(text, font(new), letter_spacing * new / size) > max_width:
        new -= 1
    return new, letter_spacing * new / size


def _render_text_mask(text, f, letter_spacing=0, pad=48):
    """Render text to a tight L mask plus its (w, h) ink box."""
    if letter_spacing == 0:
        box = f.getbbox(text)
        w, h = box[2] - box[0], box[3] - box[1]
        img = Image.new("L", (w + 2 * pad, h + 2 * pad), 0)
        ImageDraw.Draw(img).text((pad - box[0], pad - box[1]), text, font=f, fill=255)
        return img, w, h

    widths = [f.getlength(ch) for ch in text]
    total = sum(widths) + letter_spacing * (len(text) - 1)
    box = f.getbbox(text)
    h = box[3] - box[1]
    img = Image.new("L", (int(total) + 2 * pad, h + 2 * pad), 0)
    d = ImageDraw.Draw(img)
    x = float(pad)
    for ch, w in zip(text, widths):
        d.text((x, pad - box[1]), ch, font=f, fill=255)
        x += w + letter_spacing
    return img, int(total), h


def draw_text(canvas, text, size, xy, color, glow_color=None, anchor="mc",
              glow_sigma=22, glow_gain=0.75, alpha=1.0, letter_spacing=0,
              max_width=None):
    """anchor: two chars, horizontal (l/m/r) + vertical (t/m/b).

    size is a ceiling: if max_width is given the text shrinks to fit.
    """
    if alpha <= 0.003 or not text:
        return
    size, letter_spacing = fit_size(text, size, max_width, letter_spacing)
    f = font(size)
    pad = int(max(24, glow_sigma * 2.5))
    img, w, h = _render_text_mask(text, f, letter_spacing, pad)
    m = np.asarray(img, np.float32) / 255.0

    ax, ay = anchor[0], anchor[1]
    x = xy[0] - (0 if ax == "l" else w / 2 if ax == "m" else w) - pad
    y = xy[1] - (0 if ay == "t" else h / 2 if ay == "m" else h) - pad

    layer = np.zeros((m.shape[0], m.shape[1], 3), np.float32)
    if glow_color is not None:
        g = cv2.GaussianBlur(m, (0, 0), glow_sigma)
        layer += g[..., None] * (np.array(glow_color, np.float32) * glow_gain)
    layer += m[..., None] * np.array(color, np.float32)
    layer *= alpha

    _paste_add(canvas, layer, int(round(x)), int(round(y)))


def _paste_add(canvas, layer, x, y):
    H, W = canvas.shape[:2]
    lh, lw = layer.shape[:2]
    sx0, sy0 = max(0, -x), max(0, -y)
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(W, x + lw), min(H, y + lh)
    if x1 <= x0 or y1 <= y0:
        return
    canvas[y0:y1, x0:x1] += layer[sy0:sy0 + (y1 - y0), sx0:sx0 + (x1 - x0)]


def draw_odometer(canvas, text, size, xy, color, glow_color, anchor="mc",
                  glow_sigma=26, glow_gain=0.9, alpha=1.0, max_width=None):
    """Digits in fixed-width cells so a fast-counting number doesn't jitter."""
    f = font(size)
    cell = max(f.getlength(str(d)) for d in range(10))
    if max_width:
        # fixed-width cells make this wider than measure() would suggest
        w = sum(cell if ch.isdigit() else f.getlength(ch) for ch in text)
        if w > max_width:
            size = max(10, int(size * max_width / w))
            f = font(size)
            cell = max(f.getlength(str(d)) for d in range(10))
    widths = [cell if ch.isdigit() else f.getlength(ch) for ch in text]
    total = sum(widths)
    box = f.getbbox("0123456789")
    h = box[3] - box[1]
    pad = int(glow_sigma * 2.5)

    img = Image.new("L", (int(total) + 2 * pad, int(h) + 2 * pad), 0)
    d = ImageDraw.Draw(img)
    x = float(pad)
    for ch, w in zip(text, widths):
        off = (w - f.getlength(ch)) / 2
        d.text((x + off, pad - box[1]), ch, font=f, fill=255)
        x += w
    m = np.asarray(img, np.float32) / 255.0

    ax, ay = anchor[0], anchor[1]
    px = xy[0] - (0 if ax == "l" else total / 2 if ax == "m" else total) - pad
    py = xy[1] - (0 if ay == "t" else h / 2 if ay == "m" else h) - pad

    layer = cv2.GaussianBlur(m, (0, 0), glow_sigma)[..., None] * \
        (np.array(glow_color, np.float32) * glow_gain)
    layer += m[..., None] * np.array(color, np.float32)
    layer *= alpha
    _paste_add(canvas, layer, int(round(px)), int(round(py)))


# ------------------------------------------------------------------- chrome

def progress_bar(canvas, x, y, w, h, frac, fg, glow, bg=(28, 36, 60), alpha=1.0):
    H, W = canvas.shape[:2]
    pad = 24
    roi = _bbox([[x, y], [x + w, y + h]], pad, W, H)
    if roi is None:
        return
    x0, y0, x1, y1 = roi
    rw, rh = x1 - x0, y1 - y0
    bx, by = int(x - x0), int(y - y0)

    track = np.zeros((rh, rw), np.uint8)
    cv2.rectangle(track, (bx, by), (bx + int(w), by + int(h)), 255, -1)
    layer = tint(track.astype(np.float32) / 255.0, bg, 0.9 * alpha)

    fw = int(w * clamp01(frac))
    if fw >= 2:
        m = np.zeros((rh, rw), np.uint8)
        cv2.rectangle(m, (bx, by), (bx + fw, by + int(h)), 255, -1)
        layer += tint(bloom(m, 16 * _scale), glow, 0.8 * alpha)
        layer += tint(m.astype(np.float32) / 255.0, fg, alpha)
    canvas[y0:y1, x0:x1] += layer


def panel(canvas, x, y, w, h, alpha=0.55, feather=None):
    """Soft dark plate so HUD text stays legible over a busy map."""
    if alpha <= 0.01:
        return
    if feather is None:
        feather = 40 * _scale   # 1080p-authored default; scales with output res
    H, W = canvas.shape[:2]
    roi = _bbox([[x, y], [x + w, y + h]], int(feather * 3), W, H)
    if roi is None:
        return
    x0, y0, x1, y1 = roi
    m = np.zeros((y1 - y0, x1 - x0), np.float32)
    cv2.rectangle(m, (int(x - x0), int(y - y0)),
                  (int(x - x0 + w), int(y - y0 + h)), 1.0, -1)
    if feather > 0:
        m = cv2.GaussianBlur(m, (0, 0), feather)
    canvas[y0:y1, x0:x1] *= (1 - alpha * m[..., None])


def make_vignette(W, H, strength=0.45):
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    d = np.sqrt(((xx - W / 2) / (W / 2)) ** 2 + ((yy - H / 2) / (H / 2)) ** 2)
    v = 1.0 - strength * np.clip((d - 0.55) / 0.85, 0, 1) ** 1.5
    return v[..., None].astype(np.float32)
