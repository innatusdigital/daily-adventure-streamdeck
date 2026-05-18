#!/usr/bin/env python3
"""
AdventurePad runner — drives a physical Elgato Stream Deck against the
Daily Adventure API. Authenticates with x-streamdeck-key (header name
kept stable to avoid orphaning Pis already in the wild).

Modes:
  NORMAL        -- slots 1-13 play cards / fire HA actions, slot 14 = play/pause, slot 15 = settings
  SETTINGS      -- slot 1 = select player, slot 2 = refresh, slot 15 = back
  DEVICE_SELECT -- device tiles, slot 15 = cancel
  CHAPTERS      -- chapter / track picker opened by long-pressing a card

Sleep states:
  AWAKE  -- full brightness, normal interaction
  DIM    -- reduced brightness, button press wakes without triggering
  OFF    -- brightness 0, button press wakes without triggering
"""

import os
import io
import sys
import time
import logging
import threading
import requests
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont

try:
    from StreamDeck.DeviceManager import DeviceManager
    from StreamDeck.ImageHelpers import PILHelper
except ImportError:
    print("ERROR: streamdeck library not installed. Run: pip3 install streamdeck")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("streamdeck")

# -- Config -------------------------------------------------------------------

API_URL = os.environ.get("DA_API_URL", "https://www.dailyadventure.io").rstrip("/")
API_KEY = os.environ.get("DA_API_KEY", "")
CONFIG_POLL_INTERVAL = int(os.environ.get("DA_POLL_INTERVAL", "300"))

if not API_KEY:
    conf = "/etc/streamdeck.conf"
    if os.path.exists(conf):
        for line in open(conf):
            line = line.strip()
            if line.startswith("DA_API_URL="):
                API_URL = line.split("=", 1)[1].strip().strip('"').rstrip("/")
            elif line.startswith("DA_API_KEY="):
                API_KEY = line.split("=", 1)[1].strip().strip('"')

if not API_KEY:
    log.error("DA_API_KEY not set.")
    sys.exit(1)

PLAYPAUSE_SLOT   = 14
SETTINGS_SLOT    = 15
PRESS_DEBOUNCE   = 2    # seconds, rapid double-tap guard
NORMAL_BRIGHTNESS = 70
DIM_BRIGHTNESS    = 10
WAKE_COOLDOWN     = 60  # seconds to stay awake after a manual wake before re-evaluating schedule

ICONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")

# -- Modes --------------------------------------------------------------------

MODE_NORMAL        = "normal"
MODE_SETTINGS      = "settings"
MODE_DEVICE_SELECT = "device_select"
MODE_CHAPTERS      = "chapters"

CHAPTERS_PER_PAGE       = 12  # slots 1-12; slot 13=next/cycle, 14=playpause, 15=back
CHAPTER_NEXT_SLOT       = 13
CHAPTER_BACK_SLOT       = SETTINGS_SLOT  # slot 15 re-used as Back

# -- Sleep states -------------------------------------------------------------

SLEEP_AWAKE = "awake"
SLEEP_DIM   = "dim"
SLEEP_OFF   = "off"

# -- State --------------------------------------------------------------------

BUTTON_CONFIG: dict[int, dict] = {}   # Buttons for the currently-displayed page
PAGES:         list[dict] = []         # All pages from server: [{"id", "buttons"}, ...]
PAGE_IDX:      int        = 0          # Currently displayed page index
COVER_CACHE:   dict[str, Image.Image] = {}
_config_lock = threading.Lock()

PAGE_SWITCHER_SLOT = 13   # Slot 13 becomes the page-switcher when len(PAGES) > 1

# Chapter-browse state — only valid while CURRENT_MODE == MODE_CHAPTERS
CHAPTERS:                 list[dict] = []  # [{key, title}, ...] for the long-pressed card
CHAPTER_PAGE_IDX:         int        = 0
CHAPTER_CARD_ID:          str        = ""
CHAPTER_CARD_TITLE:       str        = ""
CHAPTER_SOURCE_SLOT:      int        = 0   # the deck slot whose card we're browsing
CHAPTER_SOURCE_PAGE_IDX:  int        = 0   # the deck page that slot was on

CURRENT_MODE       = MODE_NORMAL
DEVICE_LIST:       list[dict] = []
ACTIVE_DEVICE_ID   = ""
ACTIVE_DEVICE_NAME = ""
_state_lock = threading.Lock()

LAST_PRESSED: dict[int, float] = {}
_pressed_lock = threading.Lock()

SLEEP_STATE  = SLEEP_AWAKE
WAKE_UNTIL   = 0.0   # epoch time: ignore schedule until this timestamp
DIM_TIME     = ""    # "HH:MM" or ""
OFF_TIME     = ""    # "HH:MM" or ""
MANUAL_SLEEP = False # True when user pressed Sleep in settings — scheduler stays out of the way
_sleep_lock  = threading.Lock()

PLAYPAUSE_ICON: Image.Image | None = None

# Noto Color Emoji is a bitmap font that only renders at one fixed pixel size.
# We load it at that size and resize the rendered emoji down to fit the tile.
EMOJI_FONT_PATHS = [
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/noto/NotoColorEmoji.ttf",
]
EMOJI_FONT_NATIVE_SIZE = 109
EMOJI_FONT: ImageFont.FreeTypeFont | None = None

# -- Icon loading -------------------------------------------------------------

def load_icons():
    global PLAYPAUSE_ICON
    path = os.path.join(ICONS_DIR, "playpause.png")
    if os.path.exists(path):
        PLAYPAUSE_ICON = Image.open(path).convert("RGBA")
        log.info("Loaded icon: %s", path)
    else:
        log.warning("Icon not found at %s -- using fallback", path)
    _load_emoji_font()

def _load_emoji_font():
    """Best-effort load of a color emoji font. If unavailable, HA tiles still
    render — they just show the label without an emoji glyph."""
    global EMOJI_FONT
    for path in EMOJI_FONT_PATHS:
        if os.path.exists(path):
            try:
                EMOJI_FONT = ImageFont.truetype(path, size=EMOJI_FONT_NATIVE_SIZE)
                log.info("Loaded color emoji font: %s", path)
                return
            except Exception as exc:
                log.warning("Failed to load emoji font %s: %s", path, exc)
    log.info("No color emoji font found — HA tiles will render without emoji glyphs")

def render_emoji(emoji: str, target_px: int) -> Image.Image | None:
    """Renders a Unicode emoji string into a square RGBA image at target_px,
    using Pillow's color-emoji path. Returns None if no font is loaded or the
    glyph can't be rendered."""
    if not EMOJI_FONT or not emoji:
        return None
    try:
        canvas_size = EMOJI_FONT_NATIVE_SIZE + 40
        img = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        # embedded_color=True tells Pillow to honor the bitmap CBDT glyphs
        draw.text((0, 0), emoji, font=EMOJI_FONT, embedded_color=True)
        bbox = img.getbbox()
        if not bbox:
            return None
        img = img.crop(bbox)
        return img.resize((target_px, target_px), Image.LANCZOS)
    except Exception as exc:
        log.debug("Emoji render failed for %r: %s", emoji, exc)
        return None

# -- API helpers --------------------------------------------------------------

def _headers() -> dict:
    return {"x-streamdeck-key": API_KEY, "Content-Type": "application/json"}

def fetch_config() -> dict:
    """Returns { buttons, dimTime, offTime }."""
    try:
        r = requests.get(f"{API_URL}/api/yoto/adventurepad", headers=_headers(), timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        log.warning("Failed to fetch config: %s", exc)
        return {}

def fetch_devices() -> tuple[list[dict], str]:
    try:
        r = requests.get(f"{API_URL}/api/yoto/adventurepad/devices", headers=_headers(), timeout=10)
        r.raise_for_status()
        data = r.json()
        return data.get("devices", []), data.get("activeDeviceId") or ""
    except Exception as exc:
        log.warning("Failed to fetch devices: %s", exc)
        return [], ""

def fetch_chapters(card_id: str) -> list[dict]:
    """Returns [{key, title}, ...] for a card, or [] on error."""
    try:
        r = requests.get(f"{API_URL}/api/yoto/adventurepad/chapters",
                         params={"cardId": card_id}, headers=_headers(), timeout=10)
        r.raise_for_status()
        return r.json().get("chapters", [])
    except Exception as exc:
        log.warning("Failed to fetch chapters for %s: %s", card_id, exc)
        return []

# -- Image helpers ------------------------------------------------------------

def fetch_cover(url: str) -> Image.Image | None:
    if url in COVER_CACHE:
        return COVER_CACHE[url]
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content)).convert("RGB")
        COVER_CACHE[url] = img
        return img
    except Exception as exc:
        log.debug("Cover fetch failed %s: %s", url, exc)
        return None

def _txt(draw, text: str, w: int, y: int, fill, maxch: int = 9):
    t = text[:maxch]
    draw.text((max(2, (w - len(t) * 6) // 2), y), t, fill=fill)

def _tile(deck, bg: tuple, label: str = "", label_color=(200, 200, 200)) -> Image.Image:
    kf = deck.key_image_format()
    w, h = kf["size"]
    img = Image.new("RGB", (w, h), bg)
    if label:
        ImageDraw.Draw(img).text((3, 3), label, fill=label_color)
    return img

# -- Multi-page helpers -------------------------------------------------------

def _is_multipage() -> bool:
    """True when the server returned 2+ pages — slot 13 becomes the page-switcher."""
    with _config_lock:
        return len(PAGES) > 1

def _apply_current_page():
    """Copy PAGES[PAGE_IDX].buttons into BUTTON_CONFIG. Caller must hold _config_lock or
    accept that another writer may interleave (we only use this from already-locked paths)."""
    if not PAGES:
        BUTTON_CONFIG.clear()
        return
    idx = max(0, min(PAGE_IDX, len(PAGES) - 1))
    raw = PAGES[idx].get("buttons", {}) or {}
    BUTTON_CONFIG.clear()
    BUTTON_CONFIG.update({int(k): v for k, v in raw.items()})

def _apply_config_payload(data: dict):
    """Single entry point for server config → local state. Handles both new (`pages`)
    and legacy (`buttons`) shapes. Resets PAGE_IDX if it goes out of range after a
    page is removed."""
    global PAGES, PAGE_IDX
    raw_pages = data.get("pages")
    with _config_lock:
        if isinstance(raw_pages, list) and raw_pages:
            PAGES = raw_pages
        else:
            # Legacy single-page fallback
            buttons = data.get("buttons", {}) or {}
            PAGES = [{"id": "legacy", "buttons": buttons}]
        if PAGE_IDX >= len(PAGES):
            PAGE_IDX = 0
        _apply_current_page()

# -- Tile renderers -----------------------------------------------------------
#
# HA tile colour gradients. Keys + RGB tuples must mirror src/components/
# adventurepad/adventurepad.types.ts:TINTS in the web app — if the web side
# adds a tint, add it here too.
TINTS = {
    "amber":   ((253, 186, 116), (194,  65,  12)),
    "blue":    ((56,  189, 248), (2,   132, 199)),
    "purple":  ((192, 132, 252), (126, 34,  206)),
    "emerald": ((52,  211, 153), (4,   120,  87)),
    "pink":    ((244, 114, 182), (190, 24,   93)),
    "slate":   ((100, 116, 139), (30,  41,   59)),
}

def _vertical_gradient(w: int, h: int, c1: tuple, c2: tuple) -> Image.Image:
    """Top-to-bottom linear gradient between c1 and c2."""
    img = Image.new("RGB", (w, h), c1)
    draw = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(1, h - 1)
        r = int(c1[0] + (c2[0] - c1[0]) * t)
        g = int(c1[1] + (c2[1] - c1[1]) * t)
        b = int(c1[2] + (c2[2] - c1[2]) * t)
        draw.line([(0, y), (w, y)], fill=(r, g, b))
    return img

def make_ha_tile_image(deck, slot: int, assignment: dict) -> Image.Image:
    """Home Assistant action tile — coloured gradient with emoji on top and
    label below. The emoji renders only on systems whose Pillow has a colour-
    emoji-capable font; otherwise it shows as a glyph box, which is still
    distinct from a card tile. The label is always readable.
    """
    kf = deck.key_image_format()
    w, h = kf["size"]
    tint_id = assignment.get("tintId", "amber")
    c1, c2 = TINTS.get(tint_id, TINTS["amber"])
    img = _vertical_gradient(w, h, c1, c2)
    draw = ImageDraw.Draw(img)
    # Slot number badge (top-left, semi-transparent black behind)
    badge = Image.new("RGBA", (16, 12), (0, 0, 0, 110))
    img.paste(badge, (2, 2), badge)
    ImageDraw.Draw(img).text((5, 3), str(slot), fill=(255, 255, 255))
    # Colour emoji rendered via Pillow's embedded_color path. If no emoji font
    # is loaded, render_emoji returns None and the tile shows label only.
    emoji = assignment.get("emoji", "")
    if emoji:
        emoji_px = max(20, int(min(w, h) * 0.45))
        emoji_img = render_emoji(emoji, emoji_px)
        if emoji_img is not None:
            ex = (w - emoji_px) // 2
            ey = max(14, h // 2 - emoji_px // 2 - 6)
            img.paste(emoji_img, (ex, ey), emoji_img)
    # Label centred lower-middle, wrapped to two short lines
    label = assignment.get("label", "")
    if label:
        words = label.split()
        l1 = ""
        l2 = ""
        for word in words:
            cand = (l1 + " " + word).strip() if l1 else word
            if len(cand) <= 9:
                l1 = cand
            else:
                cand2 = (l2 + " " + word).strip() if l2 else word
                l2 = cand2[:9]
        y = h - 22 if l2 else h - 14
        _txt(draw, l1, w, y, (255, 255, 255))
        if l2:
            _txt(draw, l2, w, y + 10, (255, 255, 255))
    return img

def make_assignment_image(deck, slot: int, assignment: dict | None) -> Image.Image:
    """Dispatch to the right tile renderer based on assignment kind. Pre-v7
    records have no `kind` field — default to 'card' so existing layouts keep
    working."""
    if assignment and assignment.get("kind") == "ha":
        return make_ha_tile_image(deck, slot, assignment)
    return make_card_image(deck, slot, assignment)

def make_card_image(deck, slot: int, assignment: dict | None) -> Image.Image:
    kf = deck.key_image_format()
    w, h = kf["size"]
    if assignment and assignment.get("coverUrl"):
        img = fetch_cover(assignment["coverUrl"])
        if img:
            img = img.resize((w, h), Image.LANCZOS)
            ov  = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            ImageDraw.Draw(ov).rectangle([(0, h - 22), (w, h)], fill=(0, 0, 0, 140))
            img = Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")
            ImageDraw.Draw(img).text((4, h - 17), str(slot), fill=(255, 255, 255))
            return img
    img = Image.new("RGB", (w, h), (30, 30, 30))
    _txt(ImageDraw.Draw(img), str(slot), w, h // 2 - 4, (80, 80, 80))
    return img

def make_pressed_image(deck, slot: int) -> Image.Image:
    with _config_lock:
        a = BUTTON_CONFIG.get(slot)
    base = make_assignment_image(deck, slot, a)
    return Image.alpha_composite(base.convert("RGBA"), Image.new("RGBA", base.size, (0, 0, 0, 110))).convert("RGB")

def make_feedback_image(deck, slot: int, success: bool) -> Image.Image:
    with _config_lock:
        a = BUTTON_CONFIG.get(slot)
    base  = make_assignment_image(deck, slot, a)
    color = (0, 190, 70, 150) if success else (210, 30, 30, 150)
    return Image.alpha_composite(base.convert("RGBA"), Image.new("RGBA", base.size, color)).convert("RGB")

def make_already_playing_image(deck, slot: int) -> Image.Image:
    with _config_lock:
        a = BUTTON_CONFIG.get(slot)
    base = make_assignment_image(deck, slot, a)
    return Image.alpha_composite(base.convert("RGBA"), Image.new("RGBA", base.size, (220, 180, 0, 150))).convert("RGB")

def make_playpause_image(deck) -> Image.Image:
    kf = deck.key_image_format()
    w, h = kf["size"]
    img = Image.new("RGB", (w, h), (75, 35, 130))
    if PLAYPAUSE_ICON:
        pad  = int(w * 0.18)
        size = (w - pad * 2, h - pad * 2)
        icon = PLAYPAUSE_ICON.resize(size, Image.LANCZOS)
        img.paste(icon, (pad, pad), icon)
    else:
        draw = ImageDraw.Draw(img)
        iw, ih = int(w * 0.55), int(h * 0.45)
        x0, y0 = (w - iw) // 2, (h - ih) // 2
        bw, gap, c = max(3, iw // 7), max(2, iw // 9), (210, 190, 255)
        draw.rectangle([x0, y0, x0 + bw, y0 + ih], fill=c)
        draw.rectangle([x0 + bw + gap, y0, x0 + bw * 2 + gap, y0 + ih], fill=c)
        tx = x0 + bw * 2 + gap * 2
        draw.polygon([(tx, y0), (x0 + iw, y0 + ih // 2), (tx, y0 + ih)], fill=c)
    ImageDraw.Draw(img).text((3, 3), "14", fill=(170, 130, 210))
    return img

def make_page_switcher_image(deck, page_idx: int, page_count: int) -> Image.Image:
    """Sky-blue tile shown on slot 13 when multi-page is active. Pressing cycles pages."""
    kf = deck.key_image_format()
    w, h = kf["size"]
    img  = Image.new("RGB", (w, h), (15, 95, 165))
    draw = ImageDraw.Draw(img)
    draw.text((3, 3), "13", fill=(120, 200, 255))
    # Big arrow ring (rough refresh glyph)
    cx, cy = w // 2, h // 2 - 4
    r = max(8, w // 5)
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(220, 240, 255), width=2)
    # Arrowhead at top-right of ring
    ax, ay = cx + r - 1, cy - r + 1
    draw.polygon([(ax, ay - 4), (ax + 6, ay + 2), (ax, ay + 6)], fill=(220, 240, 255))
    _txt(draw, f"Page {page_idx + 1}/{page_count}", w, h - 14, (200, 230, 255))
    return img

def make_settings_image(deck) -> Image.Image:
    kf = deck.key_image_format()
    w, h = kf["size"]
    img  = Image.new("RGB", (w, h), (180, 90, 15))
    draw = ImageDraw.Draw(img)
    draw.text((3, 3), "15", fill=(255, 180, 80))
    with _state_lock:
        name = ACTIVE_DEVICE_NAME
    if name:
        words = name.split()
        l1 = words[0][:9]
        l2 = (" ".join(words[1:]))[:9] if len(words) > 1 else ""
        y  = h // 2 - 10 if l2 else h // 2 - 4
        _txt(draw, l1, w, y, (255, 240, 200))
        if l2:
            _txt(draw, l2, w, y + 12, (255, 220, 160))
    else:
        _txt(draw, "Settings", w, h // 2 - 4, (255, 200, 120))
    return img

def make_device_tile_image(deck, device: dict, is_active: bool) -> Image.Image:
    kf = deck.key_image_format()
    w, h = kf["size"]
    img  = Image.new("RGB", (w, h), (30, 90, 200) if is_active else (25, 50, 120))
    draw = ImageDraw.Draw(img)
    words = device.get("deviceName", "?").split()
    l1 = words[0][:9]
    l2 = (" ".join(words[1:]))[:9] if len(words) > 1 else ""
    y  = h // 2 - 10 if l2 else h // 2 - 4
    _txt(draw, l1, w, y, (255, 255, 255))
    if l2:
        _txt(draw, l2, w, y + 12, (200, 220, 255))
    if is_active:
        draw.rectangle([(0, 0), (w - 1, h - 1)], outline=(100, 200, 255), width=2)
    return img

def make_chapter_tile_image(deck, chapter: dict, chapter_number: int) -> Image.Image:
    """Tile shown for a chapter in chapter-browse mode. Big chapter number,
    truncated title underneath."""
    kf = deck.key_image_format()
    w, h = kf["size"]
    img  = Image.new("RGB", (w, h), (35, 50, 70))
    draw = ImageDraw.Draw(img)
    # Chapter number large in upper third
    num_str = str(chapter_number)
    nx = (w - len(num_str) * 12) // 2
    draw.text((nx, 4), num_str, fill=(255, 220, 140))
    # Title in lower portion (up to two lines)
    title = chapter.get("title", "?")
    words = title.split()
    l1 = ""
    l2 = ""
    for word in words:
        candidate = (l1 + " " + word).strip() if l1 else word
        if len(candidate) <= 9:
            l1 = candidate
        else:
            candidate2 = (l2 + " " + word).strip() if l2 else word
            l2 = candidate2[:9]
    y = h - 24
    _txt(draw, l1, w, y, (220, 230, 255))
    if l2:
        _txt(draw, l2, w, y + 10, (180, 195, 230))
    return img

# -- Render modes -------------------------------------------------------------

def render_normal(deck):
    with _config_lock:
        multipage  = len(PAGES) > 1
        page_idx   = PAGE_IDX
        page_count = len(PAGES)
    for i in range(deck.key_count()):
        slot = i + 1
        if   slot == PLAYPAUSE_SLOT: img = make_playpause_image(deck)
        elif slot == SETTINGS_SLOT:  img = make_settings_image(deck)
        elif slot == PAGE_SWITCHER_SLOT and multipage:
            img = make_page_switcher_image(deck, page_idx, page_count)
        else:
            with _config_lock:
                a = BUTTON_CONFIG.get(slot)
            img = make_assignment_image(deck, slot, a)
        deck.set_key_image(i, PILHelper.to_native_format(deck, img))

def render_settings(deck):
    kf = deck.key_image_format()
    w, h = kf["size"]
    for i in range(deck.key_count()):
        slot = i + 1
        if slot == 1:
            img  = _tile(deck, (25, 80, 160), "1", (100, 180, 255))
            draw = ImageDraw.Draw(img)
            _txt(draw, "Select", w, h // 2 - 8, (200, 220, 255))
            _txt(draw, "Player", w, h // 2 + 2, (200, 220, 255))
        elif slot == 2:
            img = _tile(deck, (20, 120, 60), "2", (100, 220, 140))
            _txt(ImageDraw.Draw(img), "Refresh", w, h // 2 - 4, (180, 255, 200))
        elif slot == 3:
            img  = _tile(deck, (45, 45, 75), "3", (170, 170, 220))
            _txt(ImageDraw.Draw(img), "Sleep", w, h // 2 - 4, (220, 220, 255))
        elif slot == SETTINGS_SLOT:
            img = _tile(deck, (100, 30, 30), "15", (220, 100, 100))
            _txt(ImageDraw.Draw(img), "Back", w, h // 2 - 4, (255, 160, 160))
        else:
            img = Image.new("RGB", (w, h), (15, 15, 15))
        deck.set_key_image(i, PILHelper.to_native_format(deck, img))

def render_device_select(deck):
    kf = deck.key_image_format()
    w, h = kf["size"]
    with _state_lock:
        devices   = list(DEVICE_LIST)
        active_id = ACTIVE_DEVICE_ID
    for i in range(deck.key_count()):
        slot = i + 1
        if slot == SETTINGS_SLOT:
            img = _tile(deck, (100, 30, 30), "15", (220, 100, 100))
            _txt(ImageDraw.Draw(img), "Cancel", w, h // 2 - 4, (255, 160, 160))
        else:
            idx = slot - 1
            img = make_device_tile_image(deck, devices[idx], devices[idx].get("deviceId") == active_id) \
                  if idx < len(devices) else Image.new("RGB", (w, h), (15, 15, 15))
        deck.set_key_image(i, PILHelper.to_native_format(deck, img))

def render_chapters(deck):
    """Slots 1-12: chapter tiles for the current chapter-page. Slot 13: next/cycle
    page (only when more than one chapter-page). Slot 14: play/pause preserved.
    Slot 15: Back to the normal card grid."""
    kf = deck.key_image_format()
    w, h = kf["size"]
    with _config_lock:
        chapters    = list(CHAPTERS)
        page_idx    = CHAPTER_PAGE_IDX
    total       = len(chapters)
    total_pages = max(1, (total + CHAPTERS_PER_PAGE - 1) // CHAPTERS_PER_PAGE)
    start       = page_idx * CHAPTERS_PER_PAGE
    page_chapters = chapters[start : start + CHAPTERS_PER_PAGE]

    for i in range(deck.key_count()):
        slot = i + 1
        if slot == PLAYPAUSE_SLOT:
            img = make_playpause_image(deck)
        elif slot == CHAPTER_BACK_SLOT:  # slot 15
            img  = _tile(deck, (100, 30, 30), "15", (220, 100, 100))
            _txt(ImageDraw.Draw(img), "Back", w, h // 2 - 4, (255, 160, 160))
        elif slot == CHAPTER_NEXT_SLOT and total_pages > 1:  # slot 13
            img  = _tile(deck, (15, 95, 165), "13", (120, 200, 255))
            _txt(ImageDraw.Draw(img), "Next", w, h // 2 - 8, (220, 240, 255))
            _txt(ImageDraw.Draw(img), f"{page_idx + 1}/{total_pages}", w, h // 2 + 4, (200, 230, 255))
        else:
            # Chapter slot 1..12 (or 13 if single-page)
            chapter_slot_idx = slot - 1
            if chapter_slot_idx < len(page_chapters):
                ch = page_chapters[chapter_slot_idx]
                img = make_chapter_tile_image(deck, ch, start + chapter_slot_idx + 1)
            else:
                img = Image.new("RGB", (w, h), (15, 15, 15))
        deck.set_key_image(i, PILHelper.to_native_format(deck, img))

# -- Mode transitions ---------------------------------------------------------

def go_normal(deck):
    global CURRENT_MODE
    with _state_lock: CURRENT_MODE = MODE_NORMAL
    log.info("Mode: normal")
    render_normal(deck)

def go_settings(deck):
    global CURRENT_MODE
    with _state_lock: CURRENT_MODE = MODE_SETTINGS
    log.info("Mode: settings")
    render_settings(deck)

def go_device_select(deck):
    global CURRENT_MODE, DEVICE_LIST, ACTIVE_DEVICE_ID
    devices, active_id = fetch_devices()
    with _state_lock:
        CURRENT_MODE     = MODE_DEVICE_SELECT
        DEVICE_LIST      = devices
        ACTIVE_DEVICE_ID = active_id
    log.info("Mode: device select -- %d devices", len(devices))
    render_device_select(deck)

def go_chapters(deck, card_id: str, card_title: str, source_slot: int, source_page_idx: int):
    """Enter chapter-browse for a card. Fetches chapters synchronously; bails out
    to normal mode if the card has none or the fetch fails. Remembers the source
    slot/page so when the user picks a chapter we can resend the original slot
    and let the server resolve the cardId from the user's own config."""
    global CURRENT_MODE, CHAPTERS, CHAPTER_PAGE_IDX, CHAPTER_CARD_ID, CHAPTER_CARD_TITLE
    global CHAPTER_SOURCE_SLOT, CHAPTER_SOURCE_PAGE_IDX
    chapters = fetch_chapters(card_id)
    if len(chapters) <= 1:
        log.info("Card %s has %d chapter(s) — staying in normal mode", card_title, len(chapters))
        return False
    with _config_lock:
        CHAPTERS                = chapters
        CHAPTER_PAGE_IDX        = 0
        CHAPTER_CARD_ID         = card_id
        CHAPTER_CARD_TITLE      = card_title
        CHAPTER_SOURCE_SLOT     = source_slot
        CHAPTER_SOURCE_PAGE_IDX = source_page_idx
    with _state_lock: CURRENT_MODE = MODE_CHAPTERS
    log.info("Mode: chapters -- %s (%d chapters)", card_title, len(chapters))
    render_chapters(deck)
    return True

# -- Sleep state machine ------------------------------------------------------

def _current_hhmm() -> str:
    return datetime.now().strftime("%H:%M")

def enter_dim(deck):
    global SLEEP_STATE
    with _sleep_lock:
        SLEEP_STATE = SLEEP_DIM
    deck.set_brightness(DIM_BRIGHTNESS)
    log.info("Night mode: dim")

def enter_off(deck):
    global SLEEP_STATE
    with _sleep_lock:
        SLEEP_STATE = SLEEP_OFF
    deck.set_brightness(0)
    log.info("Night mode: off")

def wake_up(deck):
    global SLEEP_STATE, WAKE_UNTIL, MANUAL_SLEEP
    with _sleep_lock:
        SLEEP_STATE  = SLEEP_AWAKE
        WAKE_UNTIL   = time.time() + WAKE_COOLDOWN
        MANUAL_SLEEP = False
    deck.set_brightness(NORMAL_BRIGHTNESS)
    log.info("Woke up (stays awake for %ds)", WAKE_COOLDOWN)

def manual_sleep(deck):
    """User-triggered sleep from the settings menu. Any key press wakes it back up
    without triggering. Scheduler stays out of the way until cleared."""
    global SLEEP_STATE, MANUAL_SLEEP
    with _sleep_lock:
        SLEEP_STATE  = SLEEP_OFF
        MANUAL_SLEEP = True
    deck.set_brightness(0)
    log.info("Manual sleep — press any button to wake")

def sleep_scheduler(deck):
    """Background thread: enforce dim/off schedule."""
    global SLEEP_STATE
    while True:
        time.sleep(30)
        with _sleep_lock:
            state        = SLEEP_STATE
            wake_end     = WAKE_UNTIL
            dim_t        = DIM_TIME
            off_t        = OFF_TIME
            manual_sleep = MANUAL_SLEEP

        # Manual sleep takes precedence — only a key press clears it
        if manual_sleep:
            continue

        if not dim_t and not off_t:
            continue  # night mode disabled

        # Respect manual wake cooldown
        if time.time() < wake_end:
            continue

        now = _current_hhmm()

        # Determine what state we should be in right now
        should_be_off = off_t and now >= off_t
        should_be_dim = dim_t and now >= dim_t and not should_be_off

        if should_be_off and state != SLEEP_OFF:
            enter_off(deck)
        elif should_be_dim and state == SLEEP_AWAKE:
            enter_dim(deck)
        elif not should_be_off and not should_be_dim and state != SLEEP_AWAKE:
            # Before dim time (morning) — natural reset
            log.info("Morning reset: waking")
            with _sleep_lock:
                SLEEP_STATE = SLEEP_AWAKE
            deck.set_brightness(NORMAL_BRIGHTNESS)

# -- Play/pause action --------------------------------------------------------

def _trigger_playpause(deck, key_index: int):
    log.info("Play/pause pressed")
    base = make_playpause_image(deck)
    dim  = Image.alpha_composite(base.convert("RGBA"), Image.new("RGBA", base.size, (0, 0, 0, 100))).convert("RGB")
    deck.set_key_image(key_index, PILHelper.to_native_format(deck, dim))

    success = False
    try:
        r = requests.post(f"{API_URL}/api/yoto/adventurepad/trigger", headers=_headers(),
                          json={"slot": PLAYPAUSE_SLOT}, timeout=15)
        success = r.ok
        if r.ok:
            log.info("Play/pause: %s", r.json().get("action", "toggled"))
        else:
            log.warning("Play/pause failed: %s %s", r.status_code, r.text[:120])
    except Exception as exc:
        log.error("Play/pause error: %s", exc)

    color    = (0, 190, 70, 150) if success else (210, 30, 30, 150)
    feedback = Image.alpha_composite(base.convert("RGBA"), Image.new("RGBA", base.size, color)).convert("RGB")
    deck.set_key_image(key_index, PILHelper.to_native_format(deck, feedback))

    def _restore():
        time.sleep(1.5)
        with _state_lock: m = CURRENT_MODE
        if m == MODE_NORMAL:
            deck.set_key_image(key_index, PILHelper.to_native_format(deck, make_playpause_image(deck)))
    threading.Thread(target=_restore, daemon=True).start()

# -- Gesture detection --------------------------------------------------------
#
# We get raw press/release events from the Stream Deck library. Map them to
# three gestures: 'single', 'double', 'long'. Latency profile:
#   - long: fires the moment the hold crosses LONG_PRESS_MS, no wait for release
#   - single: fires DOUBLE_TAP_WINDOW_MS after release (we have to wait to know
#     if a second tap is coming)
#   - double: fires immediately on second release inside the window

LONG_PRESS_MS        = 600
DOUBLE_TAP_WINDOW_MS = 300

# Per-key: { press_time, release_time, tap_count, hold_timer, fire_timer, long_fired }
_gesture_state: dict[int, dict] = {}
_gesture_lock = threading.Lock()

def _gesture_reset(key_index: int):
    """Drop any pending timers/state for a key. Used after sleep wake so the
    press that woke us doesn't get counted toward a gesture."""
    with _gesture_lock:
        s = _gesture_state.get(key_index)
        if not s: return
        for t_key in ("hold_timer", "fire_timer"):
            t = s.get(t_key)
            if t: t.cancel()
        _gesture_state[key_index] = {}

def _gesture_dispatch(deck, key_index: int, state: bool):
    now = time.time()
    with _gesture_lock:
        s = _gesture_state.setdefault(key_index, {})
        if state:
            # PRESS-DOWN
            # If the previous release was outside the double-tap window, reset count
            last_release = s.get("release_time", 0) or 0
            if (now - last_release) * 1000 > DOUBLE_TAP_WINDOW_MS:
                s["tap_count"] = 0
            # Cancel any pending single-tap fire (another press came in)
            ft = s.get("fire_timer")
            if ft: ft.cancel()
            s["fire_timer"] = None
            s["press_time"] = now
            s["release_time"] = None
            s["long_fired"] = False
            s["tap_count"] = s.get("tap_count", 0) + 1
            # Start long-press watcher
            def _long_check():
                with _gesture_lock:
                    if s.get("long_fired"): return
                    if s.get("release_time"): return  # already released
                    s["long_fired"] = True
                    s["tap_count"] = 0
                handle_gesture(deck, key_index, "long")
            ht = threading.Timer(LONG_PRESS_MS / 1000, _long_check)
            ht.daemon = True
            s["hold_timer"] = ht
            ht.start()
        else:
            # PRESS-UP
            s["release_time"] = now
            ht = s.get("hold_timer")
            if ht: ht.cancel()
            if s.get("long_fired"):
                return  # long already fired; ignore release
            # Wait DOUBLE_TAP_WINDOW_MS to see if another press follows
            def _fire():
                with _gesture_lock:
                    if s.get("long_fired"): return
                    count = s.get("tap_count", 0)
                    s["tap_count"] = 0
                if count >= 2:
                    handle_gesture(deck, key_index, "double")
                else:
                    handle_gesture(deck, key_index, "single")
            ft = threading.Timer(DOUBLE_TAP_WINDOW_MS / 1000, _fire)
            ft.daemon = True
            s["fire_timer"] = ft
            ft.start()

# -- Button press handler -----------------------------------------------------

def on_button_press(deck, key_index: int, state: bool):
    # Sleep wake on press-down must fire immediately, not after gesture detection,
    # so the kid sees the screen come on the instant they touch a key.
    if state:
        with _sleep_lock:
            sleep_state = SLEEP_STATE
        if sleep_state != SLEEP_AWAKE:
            wake_up(deck)
            _gesture_reset(key_index)
            return
    _gesture_dispatch(deck, key_index, state)

def handle_gesture(deck, key_index: int, gesture: str):
    # Python requires `global` declarations before any read of the name in the
    # function. Several branches below mutate these — declare upfront.
    global ACTIVE_DEVICE_NAME, ACTIVE_DEVICE_ID, PAGE_IDX, CHAPTER_PAGE_IDX

    slot = key_index + 1
    log.info("Slot %d: %s", slot, gesture)

    with _state_lock:
        mode        = CURRENT_MODE
        device_list = list(DEVICE_LIST)

    # Menu modes only respond to single tap — double/long would feel weird here.
    # Play/pause (slot 14) is the exception: it should keep working in chapter mode.
    if mode in (MODE_DEVICE_SELECT, MODE_SETTINGS, MODE_CHAPTERS) and gesture != "single":
        return

    # Device selection mode
    if mode == MODE_DEVICE_SELECT:
        if slot == SETTINGS_SLOT:
            go_settings(deck)
            return
        idx = slot - 1
        if idx >= len(device_list):
            return
        chosen      = device_list[idx]
        device_id   = chosen.get("deviceId", "")
        device_name = chosen.get("deviceName", "?")
        log.info("Device selected: %s", device_name)
        try:
            r = requests.post(f"{API_URL}/api/yoto/adventurepad/trigger", headers=_headers(),
                              json={"slot": SETTINGS_SLOT, "deviceId": device_id}, timeout=15)
            if r.ok:
                with _state_lock:
                    ACTIVE_DEVICE_NAME = device_name
                    ACTIVE_DEVICE_ID   = device_id
                log.info("Switched to: %s", device_name)
            else:
                log.warning("Device switch failed: %s %s", r.status_code, r.text[:120])
        except Exception as exc:
            log.error("Device switch error: %s", exc)
        go_normal(deck)
        return

    # Settings menu mode
    if mode == MODE_SETTINGS:
        if slot == 1:
            go_device_select(deck)
        elif slot == 2:
            log.info("Refreshing...")
            data = fetch_config()
            _apply_config_payload(data)
            COVER_CACHE.clear()
            _apply_schedule(data)
            go_normal(deck)
        elif slot == 3:
            # Manual sleep — exit settings first so wake_up returns to normal mode
            go_normal(deck)
            manual_sleep(deck)
        elif slot == SETTINGS_SLOT:
            go_normal(deck)
        return

    # Chapter browse mode
    if mode == MODE_CHAPTERS:
        # Play/pause keeps working
        if slot == PLAYPAUSE_SLOT:
            _trigger_playpause(deck, key_index)
            return
        # Back to normal grid
        if slot == CHAPTER_BACK_SLOT:
            go_normal(deck)
            return
        with _config_lock:
            chapters       = list(CHAPTERS)
            page_idx       = CHAPTER_PAGE_IDX
            card_title     = CHAPTER_CARD_TITLE
            source_slot    = CHAPTER_SOURCE_SLOT
            source_page_ix = CHAPTER_SOURCE_PAGE_IDX
        total       = len(chapters)
        total_pages = max(1, (total + CHAPTERS_PER_PAGE - 1) // CHAPTERS_PER_PAGE)
        # Next/cycle chapter page
        if slot == CHAPTER_NEXT_SLOT and total_pages > 1:
            with _config_lock:
                CHAPTER_PAGE_IDX = (CHAPTER_PAGE_IDX + 1) % total_pages
                new_idx = CHAPTER_PAGE_IDX
            log.info("Chapters page %d/%d", new_idx + 1, total_pages)
            render_chapters(deck)
            return
        # Chapter pick
        ch_idx = page_idx * CHAPTERS_PER_PAGE + (slot - 1)
        if ch_idx >= total:
            return
        ch = chapters[ch_idx]
        chapter_key = ch.get("key")
        if not chapter_key:
            return
        log.info("Chapter pick: %s ch %s (%s)", card_title, chapter_key, ch.get("title", "?"))
        deck.set_key_image(key_index, PILHelper.to_native_format(deck, make_pressed_image(deck, slot)))
        ok = False
        try:
            r = requests.post(f"{API_URL}/api/yoto/adventurepad/trigger", headers=_headers(),
                              json={"slot": source_slot, "pageIdx": source_page_ix, "chapterKey": chapter_key}, timeout=15)
            ok = r.ok
            if not r.ok:
                log.warning("Chapter trigger failed: %s %s", r.status_code, r.text[:120])
        except Exception as exc:
            log.error("Chapter trigger error: %s", exc)
        # Quick feedback then return to normal grid
        img = make_feedback_image(deck, slot, ok)
        deck.set_key_image(key_index, PILHelper.to_native_format(deck, img))
        threading.Timer(1.2, lambda: go_normal(deck)).start()
        return

    # Normal mode — reserved slots only respond to single tap for now
    if slot == SETTINGS_SLOT:
        if gesture == "single":
            go_settings(deck)
        return

    if slot == PLAYPAUSE_SLOT:
        if gesture == "single":
            _trigger_playpause(deck, key_index)
        return

    # Page-switcher (multi-page only). Cycles locally, no server call.
    if slot == PAGE_SWITCHER_SLOT and _is_multipage():
        if gesture != "single":
            return
        with _config_lock:
            PAGE_IDX = (PAGE_IDX + 1) % len(PAGES)
            _apply_current_page()
            new_idx = PAGE_IDX
        COVER_CACHE.clear()
        log.info("Switched to page %d", new_idx + 1)
        render_normal(deck)
        return

    with _config_lock:
        assignment       = BUTTON_CONFIG.get(slot)
        current_page_idx = PAGE_IDX

    if not assignment:
        log.info("Slot %d: nothing assigned", slot)
        return

    # Long press on a Yoto card → enter chapter browse. HA actions have no
    # chapters, so fall through and fire the action normally.
    if gesture == "long" and assignment.get("kind", "card") == "card":
        go_chapters(deck, assignment.get("cardId", ""), assignment.get("title", "?"), slot, current_page_idx)
        return

    log.info("Slot %d %s -> %s", slot, gesture, assignment.get("title") or assignment.get("label") or "?")
    deck.set_key_image(key_index, PILHelper.to_native_format(deck, make_pressed_image(deck, slot)))

    feedback = False
    try:
        r = requests.post(f"{API_URL}/api/yoto/adventurepad/trigger", headers=_headers(),
                          json={"slot": slot, "pageIdx": current_page_idx, "gesture": gesture}, timeout=15)
        if r.ok:
            data = r.json()
            if data.get("alreadyPlaying"):
                log.info("Already playing: %s", data.get("title", "?"))
                feedback = "yellow"
            else:
                log.info("Playing: %s", data.get("title", "?"))
                feedback = True
        else:
            log.warning("Trigger failed: %s %s", r.status_code, r.text[:120])
    except Exception as exc:
        log.error("Trigger error: %s", exc)

    if feedback == "yellow":
        img = make_already_playing_image(deck, slot)
    else:
        img = make_feedback_image(deck, slot, bool(feedback))
    deck.set_key_image(key_index, PILHelper.to_native_format(deck, img))

    def _restore():
        time.sleep(1.5)
        with _state_lock: m = CURRENT_MODE
        if m == MODE_NORMAL:
            with _config_lock: a = BUTTON_CONFIG.get(slot)
            deck.set_key_image(key_index, PILHelper.to_native_format(deck, make_assignment_image(deck, slot, a)))
    threading.Thread(target=_restore, daemon=True).start()

# -- Config polling -----------------------------------------------------------

def _apply_schedule(data: dict):
    global DIM_TIME, OFF_TIME
    with _sleep_lock:
        DIM_TIME = data.get("dimTime", "") or ""
        OFF_TIME = data.get("offTime", "") or ""
    if DIM_TIME or OFF_TIME:
        log.info("Night mode schedule: dim=%s off=%s", DIM_TIME or "disabled", OFF_TIME or "disabled")

def poll_config(deck):
    while True:
        time.sleep(CONFIG_POLL_INTERVAL)
        data = fetch_config()
        _apply_config_payload(data)
        COVER_CACHE.clear()
        _apply_schedule(data)
        with _state_lock: m = CURRENT_MODE
        with _sleep_lock: s = SLEEP_STATE
        if m == MODE_NORMAL and s == SLEEP_AWAKE:
            render_normal(deck)

# -- Main ---------------------------------------------------------------------

def main():
    global ACTIVE_DEVICE_NAME, ACTIVE_DEVICE_ID

    log.info("Starting -- API: %s", API_URL)
    load_icons()

    decks = DeviceManager().enumerate()
    if not decks:
        log.error("No Stream Deck found.")
        sys.exit(1)

    deck = decks[0]
    deck.open()
    deck.reset()
    deck.set_brightness(NORMAL_BRIGHTNESS)
    log.info("Connected: %s (%d keys)", deck.deck_type(), deck.key_count())

    data = fetch_config()
    _apply_config_payload(data)
    _apply_schedule(data)

    devices, active_id = fetch_devices()
    if devices and active_id:
        found = next((d for d in devices if d.get("deviceId") == active_id), None)
        if found:
            ACTIVE_DEVICE_NAME = found.get("deviceName", "")
            ACTIVE_DEVICE_ID   = active_id
            log.info("Active device: %s", ACTIVE_DEVICE_NAME)

    render_normal(deck)
    deck.set_key_callback(on_button_press)

    threading.Thread(target=poll_config,      args=(deck,), daemon=True).start()
    threading.Thread(target=sleep_scheduler,  args=(deck,), daemon=True).start()

    log.info("Running. Press Ctrl+C to quit.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        deck.reset()
        deck.close()
        log.info("Stopped.")

if __name__ == "__main__":
    main()
