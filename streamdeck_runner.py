#!/usr/bin/env python3
"""
Stream Deck runner for Daily Adventure / Yoto.

Modes:
  NORMAL        -- slots 1-13 play cards, slot 14 = play/pause, slot 15 = settings
  SETTINGS      -- slot 1 = select player, slot 2 = refresh, slot 15 = back
  DEVICE_SELECT -- device tiles, slot 15 = cancel

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
from PIL import Image, ImageDraw

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
_sleep_lock  = threading.Lock()

PLAYPAUSE_ICON: Image.Image | None = None

# -- Icon loading -------------------------------------------------------------

def load_icons():
    global PLAYPAUSE_ICON
    path = os.path.join(ICONS_DIR, "playpause.png")
    if os.path.exists(path):
        PLAYPAUSE_ICON = Image.open(path).convert("RGBA")
        log.info("Loaded icon: %s", path)
    else:
        log.warning("Icon not found at %s -- using fallback", path)

# -- API helpers --------------------------------------------------------------

def _headers() -> dict:
    return {"x-streamdeck-key": API_KEY, "Content-Type": "application/json"}

def fetch_config() -> dict:
    """Returns { buttons, dimTime, offTime }."""
    try:
        r = requests.get(f"{API_URL}/api/yoto/streamdeck", headers=_headers(), timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        log.warning("Failed to fetch config: %s", exc)
        return {}

def fetch_devices() -> tuple[list[dict], str]:
    try:
        r = requests.get(f"{API_URL}/api/yoto/streamdeck/devices", headers=_headers(), timeout=10)
        r.raise_for_status()
        data = r.json()
        return data.get("devices", []), data.get("activeDeviceId") or ""
    except Exception as exc:
        log.warning("Failed to fetch devices: %s", exc)
        return [], ""

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
    base = make_card_image(deck, slot, a)
    return Image.alpha_composite(base.convert("RGBA"), Image.new("RGBA", base.size, (0, 0, 0, 110))).convert("RGB")

def make_feedback_image(deck, slot: int, success: bool) -> Image.Image:
    with _config_lock:
        a = BUTTON_CONFIG.get(slot)
    base  = make_card_image(deck, slot, a)
    color = (0, 190, 70, 150) if success else (210, 30, 30, 150)
    return Image.alpha_composite(base.convert("RGBA"), Image.new("RGBA", base.size, color)).convert("RGB")

def make_already_playing_image(deck, slot: int) -> Image.Image:
    with _config_lock:
        a = BUTTON_CONFIG.get(slot)
    base = make_card_image(deck, slot, a)
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
            img = make_card_image(deck, slot, a)
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
    global SLEEP_STATE, WAKE_UNTIL
    with _sleep_lock:
        SLEEP_STATE = SLEEP_AWAKE
        WAKE_UNTIL  = time.time() + WAKE_COOLDOWN
    deck.set_brightness(NORMAL_BRIGHTNESS)
    log.info("Woke up (stays awake for %ds)", WAKE_COOLDOWN)

def sleep_scheduler(deck):
    """Background thread: enforce dim/off schedule."""
    global SLEEP_STATE
    while True:
        time.sleep(30)
        with _sleep_lock:
            state     = SLEEP_STATE
            wake_end  = WAKE_UNTIL
            dim_t     = DIM_TIME
            off_t     = OFF_TIME

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
        r = requests.post(f"{API_URL}/api/yoto/streamdeck/trigger", headers=_headers(),
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

# -- Button press handler -----------------------------------------------------

def on_button_press(deck, key_index: int, state: bool):
    if not state:
        return
    slot = key_index + 1

    # Sleep intercept — wake without triggering
    with _sleep_lock:
        sleep_state = SLEEP_STATE

    if sleep_state != SLEEP_AWAKE:
        wake_up(deck)
        return

    with _state_lock:
        mode        = CURRENT_MODE
        device_list = list(DEVICE_LIST)

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
            r = requests.post(f"{API_URL}/api/yoto/streamdeck/trigger", headers=_headers(),
                              json={"slot": SETTINGS_SLOT, "deviceId": device_id}, timeout=15)
            if r.ok:
                global ACTIVE_DEVICE_NAME, ACTIVE_DEVICE_ID
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
        elif slot == SETTINGS_SLOT:
            go_normal(deck)
        return

    # Normal mode
    if slot == SETTINGS_SLOT:
        go_settings(deck)
        return

    if slot == PLAYPAUSE_SLOT:
        _trigger_playpause(deck, key_index)
        return

    # Page-switcher (multi-page only). Cycles locally, no server call.
    if slot == PAGE_SWITCHER_SLOT and _is_multipage():
        global PAGE_IDX
        with _config_lock:
            PAGE_IDX = (PAGE_IDX + 1) % len(PAGES)
            _apply_current_page()
            new_idx = PAGE_IDX
        COVER_CACHE.clear()
        log.info("Switched to page %d", new_idx + 1)
        render_normal(deck)
        return

    with _config_lock:
        assignment = BUTTON_CONFIG.get(slot)

    if not assignment:
        log.info("Slot %d: nothing assigned", slot)
        return

    # Rapid double-tap guard
    with _pressed_lock:
        last     = LAST_PRESSED.get(slot, 0)
        too_soon = (time.time() - last) < PRESS_DEBOUNCE
        if not too_soon:
            LAST_PRESSED[slot] = time.time()
    if too_soon:
        log.info("Slot %d: debounced", slot)
        return

    log.info("Slot %d pressed -> %s", slot, assignment.get("title", "?"))
    deck.set_key_image(key_index, PILHelper.to_native_format(deck, make_pressed_image(deck, slot)))

    with _config_lock:
        current_page_idx = PAGE_IDX

    feedback = False
    try:
        r = requests.post(f"{API_URL}/api/yoto/streamdeck/trigger", headers=_headers(),
                          json={"slot": slot, "pageIdx": current_page_idx}, timeout=15)
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
            deck.set_key_image(key_index, PILHelper.to_native_format(deck, make_card_image(deck, slot, a)))
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
