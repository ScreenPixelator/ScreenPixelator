"""
Full-screen, click-through pixelation overlay (Windows 10 2004+ / Windows 11).

Captures the screen and draws pixelated patches on a transparent,
always-on-top window:
  - body parts found by NudeNet's model (TARGET_CLASSES), and
  - optionally the complete body of every woman (her outline, found by a
    person model), leaving faces and men visible.
Two scanners run side by side: a full-screen scan that finds new zones
anywhere, and a focus scan that re-checks the areas around known zones many
times per second. Between scans each patch follows the movement of the video.
Mouse clicks and keyboard input go straight through to the programs
underneath. The overlay is hidden from screenshots and recordings (that is how
it avoids detecting its own mosaic).

Needs, next to this script, for whole bodies: yolo11n-seg.onnx
Settings: made with pixelator_settings.pyw (settings.json next to this script).

Start:  pyw -3.13 screen_pixelator.py   (or py -3.13 ... to see messages)
Stop:   from the settings program, or Ctrl+Alt+Q when no password is set
"""

import ctypes
import json
import math
import os
import signal
import sys
import multiprocessing as mp
import queue
import time
import traceback
from collections import deque
from ctypes import wintypes

import cv2
import mss
import nudenet
import nudenet.nudenet as _nudenet_module
import numpy as np
import onnxruntime as ort

# --- Configuration ---
# 1 = main monitor, 2 = second monitor, ...  0 = all monitors together
MONITOR_INDEX = 1

# Minimum confidence (0.0 - 1.0). Lower = catches more, also more false alarms.
CONFIDENCE_THRESHOLD = 0.1

# Mosaic block size in pixels (bigger = coarser blocks)
PIXEL_BLOCK_SIZE = 15

# Enlarge each detected body-part zone by this fraction on every side
BOX_PADDING = 0.20
# Genitals (and anus) get at least this much: small, and they must not slip out
GENITAL_MIN_PADDING = 0.50

# Body-part labels to pixelate (NudeNet labels)
TARGET_CLASSES = {
    "FEMALE_BREAST_EXPOSED",
    "FEMALE_GENITALIA_EXPOSED",
    "FEMALE_BREAST_COVERED",
    "FEMALE_GENITALIA_COVERED",
}

# --- Whole bodies ---
# Cover the complete body of every woman (her outline), except the face.
# Needs yolo11n-seg.onnx next to this script.
COVER_FEMALE_BODIES = True

# Man or woman is decided from the body first: male genitals or chest ->
# man (stays visible); female breasts or genitals -> woman (covered).
# When no body signs are visible: decide from the face (True), or treat the
# person as unknown (False). NudeNet often mistakes men's faces for women's.
TRUST_FACES = True

# People where it can't tell man or woman (nothing visible to decide on,
# e.g. seen from behind): True = cover them, False = leave them visible
COVER_UNKNOWN_PEOPLE = True

# Minimum confidence that something is a person (0.0 - 1.0)
PERSON_THRESHOLD = 0.35

# Grow the body outline by this many mosaic blocks on every side
BODY_GROW_BLOCKS = 1

# Never pixelate faces (any zone): faces get a see-through opening
SHOW_FACES = True

# Make the face opening this much bigger than the face (fraction)
FACE_OPENING_GROW = 0.15

# Never pixelate male genitals: they get a see-through opening too, and a
# female-part detection on the same spot is ignored
SHOW_MALE_GENITALIA = True
MALE_OPENING_GROW = 0.3         # how much bigger than the detection (fraction per side)

# Keep openings (faces, male genitals) open this long after they were last
# seen (seconds); they follow the movement like the mosaic does
OPENING_HOLD_SECONDS = 1.5

# Ignore female-part detections inside the outline of someone recognised as
# a man (unless a woman's outline is there too). Clear detections (at least
# this confidence) are never ignored, so an overlapping woman stays covered.
IGNORE_FEMALE_PARTS_ON_MEN = True
CLEAR_FEMALE_PART = 0.5

# Minimum confidence for male genitals/chest (lower = recognises men sooner)
MALE_SIGN_MIN = 0.05

# How strongly to lean towards "man" when both male and female body signs
# are seen on one person (0 = no preference)
LEAN_TO_MALE = 0.6

# Cover the complete body of every man too (set from the settings program)
COVER_MALE_BODIES = False

# Faces of women and of men: "show" (seen through every mosaic), "hide"
# (covered), "eyes" (covered except the eyes), "noeyes" (only the eyes covered)
FACE_WOMEN = "show"
FACE_MEN = "show"

# How dark the mosaic blocks are: 0.0 = the colours of the picture, 1.0 = black
DARKNESS = 0.0

# Ctrl+Alt+Q stops the pixelator (switched off when a password is set)
EXIT_HOTKEY = True

# Show an icon next to the clock while running (False = completely hidden)
TRAY_ICON = True

# Only cover inside the windows of these programs (fewer false alarms in
# Word, Excel, the desktop...). Empty = everywhere.
ONLY_IN_PROGRAMS = set()
DEFAULT_PROGRAMS = (
    # browsers
    "chrome.exe msedge.exe firefox.exe opera.exe opera_gx.exe brave.exe vivaldi.exe iexplore.exe "
    "waterfox.exe librewolf.exe thorium.exe arc.exe yandex.exe "
    # video players
    "vlc.exe mpc-hc64.exe mpc-hc.exe mpc-be64.exe mpc-be.exe potplayermini64.exe potplayermini.exe "
    "mpv.exe wmplayer.exe microsoft.media.player.exe kmplayer.exe kmplayer64x.exe gom.exe smplayer.exe "
    "video.ui.exe "
    # photo viewers
    "microsoft.photos.exe photosapp.exe photos.exe i_view64.exe i_view32.exe xnview.exe xnviewmp.exe "
    "fsviewer.exe honeyview.exe imageglass.exe nomacs.exe "
    # chat apps
    "telegram.exe whatsapp.exe whatsapp.root.exe discord.exe signal.exe messenger.exe"
).split()

# --- Settings from the settings program ---
HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(HERE, "settings.json")
STOP_FILE = os.path.join(HERE, "stop.request")     # the settings program asks to stop
STATUS_FILE = os.path.join(HERE, "status.json")    # live numbers for the settings program
PID_FILE = os.path.join(HERE, "running.pid")
LOG_FILE = os.path.join(HERE, "pixelator.log")
CHECK_FILE = os.path.join(HERE, "capture_check.json")   # result of the one-time overlay check
SETTINGS_APP = os.path.join(HERE, "pixelator_settings.pyw")
ICON_FILE = os.path.join(HERE, "pixelator.ico")
APP_NAME = "ScreenPixelator 1.1"


def apply_settings() -> None:
    """Overrides the settings above with settings.json, when it exists."""
    global PIXEL_BLOCK_SIZE, DARKNESS, TARGET_CLASSES, FACE_WOMEN, FACE_MEN, COVER_FEMALE_BODIES
    global COVER_MALE_BODIES, COVER_UNKNOWN_PEOPLE, SHOW_MALE_GENITALIA, EXIT_HOTKEY
    global CONFIDENCE_THRESHOLD, PERSON_THRESHOLD, BOX_PADDING, BODY_GROW_BLOCKS, HOLD_SECONDS
    global BODY_HOLD_SECONDS, OPENING_HOLD_SECONDS, MALE_OPENING_GROW, FACE_OPENING_GROW, LEAN_TO_MALE
    global TRAY_ICON, ONLY_IN_PROGRAMS, CPU_LEVEL, CPU_AUTO, DETECT_PAUSE, FOCUS_PAUSE, SCAN_SCALES
    global THREAD_SHARE, CAPTURE_FPS, REFRESH_MS
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except FileNotFoundError:
        s = {}           # no settings yet: the defaults below (hide nudity only)
    except Exception:
        return
    PIXEL_BLOCK_SIZE = max(6, min(60, int(s.get("block_size", PIXEL_BLOCK_SIZE))))
    def num(key, default, low, high):
        try:
            return max(low, min(high, float(s.get(key, default))))
        except (TypeError, ValueError):
            return default
    DARKNESS = num("darkness", 100 if s.get("style") == "black" else 0, 0, 100) / 100
    # Sliders of the settings program ("catch more" 0-100, sizes in %)
    CONFIDENCE_THRESHOLD = 0.55 - 0.005 * num("catch_parts", 80, 0, 100)
    PERSON_THRESHOLD = 0.70 - 0.005 * num("catch_people", 65, 0, 100)
    BOX_PADDING = num("zone_size", 20, 0, 200) / 100
    BODY_GROW_BLOCKS = int(round(num("body_margin", 1, 0, 6)))
    HOLD_SECONDS = num("hold", 0.8, 0.2, 5.0)
    BODY_HOLD_SECONDS = HOLD_SECONDS + 0.7
    OPENING_HOLD_SECONDS = HOLD_SECONDS + 0.7
    MALE_OPENING_GROW = num("male_opening", 30, 0, 150) / 100
    FACE_OPENING_GROW = num("face_opening", 15, 0, 100) / 100
    LEAN_TO_MALE = num("lean_male", 60, 0, 100) / 100
    clothed = bool(s.get("also_clothed", False))
    parts = set()
    if s.get("cover_breasts", True):
        parts |= {"FEMALE_BREAST_EXPOSED"} | ({"FEMALE_BREAST_COVERED"} if clothed else set())
    if s.get("cover_female_genitals", True):
        parts |= {"FEMALE_GENITALIA_EXPOSED"} | ({"FEMALE_GENITALIA_COVERED"} if clothed else set())
    if s.get("cover_male_genitals", True):
        parts.add("MALE_GENITALIA_EXPOSED")
    if s.get("cover_buttocks", True):
        parts |= {"BUTTOCKS_EXPOSED", "ANUS_EXPOSED"}
    old = {"hide_women": "hide", "women_eyes": "eyes"}.get(s.get("faces"), "show")   # older settings
    FACE_WOMEN = s.get("faces_women", old)
    FACE_MEN = s.get("faces_men", "show")
    TARGET_CLASSES = parts
    COVER_FEMALE_BODIES = bool(s.get("cover_women", False))
    COVER_MALE_BODIES = bool(s.get("cover_men", False))
    COVER_UNKNOWN_PEOPLE = bool(s.get("cover_unclear", False)) and (COVER_FEMALE_BODIES or COVER_MALE_BODIES)
    SHOW_MALE_GENITALIA = not (s.get("cover_male_genitals", True) or COVER_MALE_BODIES)
    EXIT_HOTKEY = not s.get("password_hash")
    TRAY_ICON = not s.get("start_hidden", False)
    level = s.get("cpu_level", "auto")
    CPU_AUTO = level == "auto"
    try:
        CPU_LEVEL = auto_cpu_level() if CPU_AUTO else max(0, min(4, int(level)))
    except (TypeError, ValueError):
        CPU_LEVEL, CPU_AUTO = auto_cpu_level(), True
    DETECT_PAUSE, FOCUS_PAUSE, fine, THREAD_SHARE, CAPTURE_FPS, REFRESH_MS = CPU_LEVELS[CPU_LEVEL]
    SCAN_SCALES = (fine, 0.25)
    if s.get("only_in_programs", True):
        names = s.get("programs", DEFAULT_PROGRAMS)
        ONLY_IN_PROGRAMS = {str(n).strip().lower() for n in names if str(n).strip()}


# --- Scanning ---
# Detail levels of the full-screen scan. 1.0 = every screen pixel is looked
# at (finds zones from about 25 px wide), 0.25 = whole screen shrunk to a
# quarter (finds big close-ups). Together they cover every size.
SCAN_SCALES = (1.0, 0.25)

# Scan at full detail only the parts of the screen that changed since the
# last scan (a playing video, scrolling); the rest is skipped. The whole
# screen is still scanned every FULL_REFRESH_SECONDS.
SCAN_ONLY_CHANGES = True
FULL_REFRESH_SECONDS = 2.0

# Keep re-scanning the areas around found zones, many times per second
FOCUS_SCAN = True

# Extra area around each zone that the focus scan looks at (fraction of size)
FOCUS_MARGIN = 0.5

# Use the graphics card when onnxruntime-directml is installed
USE_GPU = True

# Capture the screen with dxcam when it is installed
# (py -3.13 -m pip install dxcam): much faster than the standard way
USE_DXCAM = True

# Without dxcam: capture the screen at most this many times per second
CAPTURE_FPS = 60

# Pause between full-screen scans in seconds (raise for less CPU)
DETECT_PAUSE = 0.0
FOCUS_PAUSE = 0.0     # pause between rounds of the focus scan
THREAD_SHARE = 1.0    # share of the processor threads the scanners may use

# Processor use, set from the settings program: "auto" or 0 (light) ... 4 (maximum).
# level: (full scan pause, focus pause, finest detail, thread share, captures/s, redraw ms)
CPU_LEVELS = {
    0: (0.6, 0.25, 0.6, 0.25, 15, 66),
    1: (0.3, 0.12, 0.75, 0.4, 20, 50),
    2: (0.12, 0.05, 1.0, 0.6, 30, 33),
    3: (0.03, 0.01, 1.0, 0.8, 45, 25),
    4: (0.0, 0.0, 1.0, 1.0, 60, 20),
}
CPU_NAMES = ("Light", "Low", "Medium", "High", "Maximum")
CPU_LEVEL = 4
CPU_AUTO = False


def auto_cpu_level() -> int:
    """Lighter on PCs with few processor threads, and on battery."""
    cpus = os.cpu_count() or 4
    level = 0 if cpus <= 4 else 1 if cpus <= 6 else 2 if cpus <= 8 else 3
    try:
        class POWER(ctypes.Structure):
            _fields_ = [("ac", ctypes.c_ubyte), ("flag", ctypes.c_ubyte), ("percent", ctypes.c_ubyte),
                        ("saver", ctypes.c_ubyte), ("life", ctypes.c_ulong), ("full", ctypes.c_ulong)]
        st = POWER()
        if ctypes.WinDLL("kernel32").GetSystemPowerStatus(ctypes.byref(st)) and st.ac == 0:
            level = min(level, 1)
    except Exception:
        pass
    return level

# --- Mosaic ---
# Keep a zone covered this long after it was last detected (seconds),
# so a missed detection doesn't uncover it
HOLD_SECONDS = 0.8

# The same for whole bodies (a person stays a person, so this can be longer)
BODY_HOLD_SECONDS = 1.5

# A body or part cut off by the edge of the screen continues beyond it: while
# the page scrolls, keep covering towards that edge until the next scan
EXTEND_AT_SCREEN_EDGE = True

# Let the mosaic follow the movement of the video between scans
TRACK_MOVEMENT = True

# Draw each mosaic ahead in the direction it is moving, to make up for the
# time between capturing the screen and the mosaic appearing. This is the
# extra time (seconds) on top of the measured delay. 0 = only the measured delay
PREDICT_SECONDS = 0.03

# How often the mosaic is redrawn (milliseconds)
REFRESH_MS = 20

# Exit hotkey: Ctrl + Alt + Q
EXIT_KEYS = (0x11, 0x12, ord("Q"))  # VK_CONTROL, VK_MENU (Alt), Q
PICTURE_KEYS = (0x11, 0x12, ord("P"))  # Ctrl+Alt+P: save a picture of the screen with the mosaic

# Print scan speed every 10 seconds
SHOW_SPEED = True

# Print what NudeNet and the person model see on every full scan
DEBUG = False

# At start, show a red-framed mosaic square in the middle of the screen for
# this many seconds, to prove the overlay works (0 = off)
SELF_TEST_SECONDS = 0
# ---------------------

PERSON_MODEL = "yolo11n-seg.onnx"

apply_settings()

# Started without a window (pyw): messages go to a log file
if sys.stdout is None:
    try:
        sys.stdout = sys.stderr = open(LOG_FILE, "a", buffering=1, encoding="utf-8")
    except Exception:
        pass
SHOW_SPEED = SHOW_SPEED and sys.stdout is not None and not getattr(sys.stdout, "name", "").endswith(".log")
KEY_COLOR = "#ff00ff"          # this colour becomes see-through
KEY_BGR = (255, 0, 255)        # same colour in OpenCV (BGR) order

FEMALE_BODY = {"FEMALE_BREAST_EXPOSED", "FEMALE_BREAST_COVERED",
               "FEMALE_GENITALIA_EXPOSED", "FEMALE_GENITALIA_COVERED"}
MALE_BODY = {"MALE_BREAST_EXPOSED", "MALE_GENITALIA_EXPOSED"}
FACE_LABELS = {"FACE_FEMALE", "FACE_MALE"}
FEMALE_SIGNS = FEMALE_BODY | {"FACE_FEMALE"}
MALE_SIGNS = MALE_BODY | {"FACE_MALE"}
SIGN_MIN = 0.3                 # minimum score for a face / man-or-woman sign
# Bare skin: people whose gender is unclear are only covered when one of these is seen on them
EXPOSED_SIGNS = {"FEMALE_BREAST_EXPOSED", "FEMALE_GENITALIA_EXPOSED", "MALE_BREAST_EXPOSED",
                 "MALE_GENITALIA_EXPOSED", "BUTTOCKS_EXPOSED", "ANUS_EXPOSED", "BELLY_EXPOSED"}
WOMAN_FACE_MIN = 0.5           # a face counts as a woman's only from this score...

QUARTER = 0.25                 # movement is measured on quarter-size pictures
COLOR_STEP = 6                 # redraw a mosaic block when its colour changed more than this
SAME_ZONE = 0.7                # zones of one kind overlapping this much are merged
STILL_AFTER = 0.08             # no new picture this long (s): nothing moves, stop drawing ahead
TRACK_SIZE = 64                # templates are shrunk to about this size
RETEMPLATE_EVERY = 4           # refresh a template every N redraws
MATCH_MIN = 0.45               # minimum match score to move a patch
SEARCH_MARGIN = 0.35           # search around a patch, per redraw (fraction)
SEARCH_MARGIN_NEW = 0.9        # search around a new or lost zone
MAX_SHIFT = 160                # ... but never further than this (pixels)
MAX_SHIFT_NEW = 400
LOST_SECONDS = 0.5             # drop a zone whose content can't be found this long
NMS_OVERLAP = 0.45             # same as NudeNet

# --- Win32 ---
user32 = ctypes.windll.user32

GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020  # clicks pass through
WS_EX_TOOLWINDOW = 0x00000080   # no taskbar / Alt+Tab entry
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000   # never takes focus
WDA_EXCLUDEFROMCAPTURE = 0x00000011
GA_ROOT = 2
HWND_TOPMOST = wintypes.HWND(-1)
SWP_FLAGS = 0x0001 | 0x0002 | 0x0010  # NOSIZE | NOMOVE | NOACTIVATE

user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
user32.SetWindowLongW.restype = ctypes.c_long
user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short

Box = tuple[int, int, int, int]  # x1, y1, x2, y2 in monitor pixels


def make_dpi_aware() -> None:
    """Use real pixels so the overlay lines up exactly with the screenshot."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        user32.SetProcessDPIAware()


def precise_timers(on: bool) -> None:
    """Windows timers normally tick every ~16 ms; ask for 1 ms while running."""
    try:
        if on:
            ctypes.windll.winmm.timeBeginPeriod(1)
        else:
            ctypes.windll.winmm.timeEndPeriod(1)
    except Exception:
        pass


def new_mss():
    # mss asks Windows to include "layered" windows in every capture
    # (CAPTUREBLT), which makes capturing much slower. We don't need them
    # (the overlay must stay out of the capture anyway), so switch it off.
    for name in ("mss.windows.gdi", "mss.windows"):
        try:
            module = __import__(name, fromlist=["CAPTUREBLT"])
            if hasattr(module, "CAPTUREBLT"):
                module.CAPTUREBLT = 0
        except Exception:
            pass
    return mss.MSS() if hasattr(mss, "MSS") else mss.mss()


def grab_bgra(sct, left: int, top: int, width: int, height: int) -> np.ndarray:
    return np.array(sct.grab({"left": left, "top": top, "width": width, "height": height}))


def grab_bgr(sct, region: dict, box: Box) -> np.ndarray:
    x1, y1, x2, y2 = box
    return cv2.cvtColor(grab_bgra(sct, region["left"] + x1, region["top"] + y1, x2 - x1, y2 - y1),
                        cv2.COLOR_BGRA2BGR)


def exit_hotkey_pressed() -> bool:
    return all(user32.GetAsyncKeyState(k) & 0x8000 for k in EXIT_KEYS)


def picture_hotkey_pressed() -> bool:
    return all(user32.GetAsyncKeyState(k) & 0x8000 for k in PICTURE_KEYS)


def pictures_folder() -> str:
    """Pictures\\ScreenPixelator (also when Pictures was moved, e.g. to OneDrive)."""
    base = os.path.join(os.path.expanduser("~"), "Pictures")
    try:
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.WinDLL("shell32").SHGetFolderPathW(None, 0x27, None, 0, buf) == 0 and buf.value:
            base = buf.value                                   # CSIDL_MYPICTURES
    except Exception:
        pass
    folder = os.path.join(base, "ScreenPixelator")
    os.makedirs(folder, exist_ok=True)
    return folder


def save_picture(frame: np.ndarray, shown: np.ndarray, block: int) -> str:
    """The screen picture with the mosaic blocks as they are shown now, as PNG.
    (Screenshot tools can't see the mosaic: it is hidden from screen capture.)"""
    h, w = frame.shape[:2]
    out = np.ascontiguousarray(frame[:, :, :3])
    blocks = np.repeat(np.repeat(shown, block, axis=0), block, axis=1)[:h, :w]
    on = blocks[:, :, 3] == 255
    out[on] = blocks[on][:, :3].astype(np.uint8)
    path = os.path.join(pictures_folder(), time.strftime("ScreenPixelator %Y-%m-%d %H-%M-%S.png"))
    ok, data = cv2.imencode(".png", out)
    if not ok:
        raise RuntimeError("PNG encoding failed")
    with open(path, "wb") as f:     # works with any characters in the folder name
        f.write(data.tobytes())
    return path


def overlap(a: Box, b: Box) -> float:
    """Intersection as a fraction of the smaller box."""
    iw = min(a[2], b[2]) - max(a[0], b[0])
    ih = min(a[3], b[3]) - max(a[1], b[1])
    if iw <= 0 or ih <= 0:
        return 0.0
    smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return iw * ih / max(1, smaller)


def merge_boxes(boxes: list[Box], min_overlap: float = 0.3) -> list[Box]:
    """Join overlapping boxes."""
    boxes = list(boxes)
    changed = True
    while changed:
        changed = False
        out: list[Box] = []
        for a in boxes:
            for i, b in enumerate(out):
                if overlap(a, b) > min_overlap:
                    out[i] = (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))
                    changed = True
                    break
            else:
                out.append(a)
        boxes = out
    return boxes


def expand(box: Box, frac: float, extra: int, img_w: int, img_h: int, cap: int = 1 << 30) -> Box:
    x1, y1, x2, y2 = box
    mx = min(int((x2 - x1) * frac) + extra, cap)
    my = min(int((y2 - y1) * frac) + extra, cap)
    return (max(0, x1 - mx), max(0, y1 - my), min(img_w, x2 + mx), min(img_h, y2 + my))


def align_to(box: Box, step: int, img_w: int, img_h: int) -> Box:
    """Grow a box outwards to multiples of `step` pixels."""
    x1, y1, x2, y2 = box
    return (x1 // step * step, y1 // step * step,
            min(img_w, -(-x2 // step) * step), min(img_h, -(-y2 // step) * step))


def align4(box: Box, img_w: int, img_h: int) -> Box:
    """Grow a box to multiples of 4 pixels, so half- and quarter-size copies of
    captures taken at different redraws line up exactly."""
    x1, y1, x2, y2 = box
    return (x1 // 4 * 4, y1 // 4 * 4, min(img_w, -(-x2 // 4) * 4), min(img_h, -(-y2 // 4) * 4))


def pad_box(box, frac: float, img_w: int, img_h: int):
    """(x, y, w, h) -> padded (x1, y1, x2, y2), clamped; None if empty."""
    x, y, w, h = box
    px, py = int(w * frac), int(h * frac)
    x1, y1 = max(0, int(x) - px), max(0, int(y) - py)
    x2, y2 = min(img_w, int(x + w) + px), min(img_h, int(y + h) + py)
    return (x1, y1, x2, y2) if x2 > x1 and y2 > y1 else None


# --- Models ---

def _make_session(path: str, threads: int):
    def options(gpu: bool):
        o = ort.SessionOptions()
        o.intra_op_num_threads = threads
        o.inter_op_num_threads = 1
        o.add_session_config_entry("session.intra_op.allow_spinning", "0")
        if gpu:
            o.enable_mem_pattern = False
            o.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        return o

    if USE_GPU and "DmlExecutionProvider" in ort.get_available_providers():
        try:
            s = ort.InferenceSession(path, sess_options=options(True),
                                     providers=["DmlExecutionProvider", "CPUExecutionProvider"])
            s.run(None, {s.get_inputs()[0].name: np.zeros((1, 3, 320, 320), np.float32)})
            return s, "graphics card"
        except Exception as e:
            print(f"Graphics card not usable ({e}); using the CPU.")
    s = ort.InferenceSession(path, sess_options=options(False), providers=["CPUExecutionProvider"])
    return s, f"CPU, {threads} thread(s)"


def _to_blob(img: np.ndarray, scale: float):
    """Resize by `scale`, pad to multiples of 32, make a model input."""
    h, w = img.shape[:2]
    sw, sh = max(32, round(w * scale)), max(32, round(h * scale))
    if (sw, sh) != (w, h):
        img = cv2.resize(img, (sw, sh), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
    pad_w, pad_h = -sw % 32, -sh % 32
    if pad_w or pad_h:
        img = cv2.copyMakeBorder(img, 0, pad_h, 0, pad_w, cv2.BORDER_CONSTANT, value=0)
    elif not img.flags["C_CONTIGUOUS"]:
        img = np.ascontiguousarray(img)
    return cv2.dnn.blobFromImage(img, 1 / 255.0, swapRB=True), w / sw, h / sh


def _nms(rects: np.ndarray, scores: np.ndarray, min_score: float, overlap_max: float) -> np.ndarray:
    idx = cv2.dnn.NMSBoxes(rects.tolist(), scores.tolist(), min_score, overlap_max)
    return np.array(idx, dtype=int).flatten()


class FastNude:
    """NudeNet's own model, run directly: any image size and detail level,
    and the results are decoded in one go instead of a slow Python loop."""

    def __init__(self, threads: int) -> None:
        path = os.path.join(os.path.dirname(nudenet.__file__), "320n.onnx")
        self.labels = list(getattr(_nudenet_module, "__labels"))
        self.target_ids = np.array([i for i, n in enumerate(self.labels) if n in TARGET_CLASSES])
        wanted = set()
        if SHOW_FACES or FACE_WOMEN != "show" or FACE_MEN != "show":
            wanted |= FACE_LABELS
        if SHOW_MALE_GENITALIA:
            wanted |= {"MALE_GENITALIA_EXPOSED"}
        if COVER_FEMALE_BODIES or COVER_MALE_BODIES:
            wanted |= FEMALE_SIGNS | MALE_SIGNS | EXPOSED_SIGNS
        self.sign_ids = [(i, n) for i, n in enumerate(self.labels) if n in wanted]
        self.session, self.device = _make_session(path, threads)
        self.input_name = self.session.get_inputs()[0].name

    def detect(self, img: np.ndarray, scale: float, min_score: float, seen: dict | None = None):
        """Returns (target parts as (x, y, w, h), signs as (label, score, x, y, w, h)),
        in image pixels."""
        blob, fx, fy = _to_blob(img, scale)
        rows = self.session.run(None, {self.input_name: blob})[0][0].T
        scores = rows[:, 4:]
        if seen is not None:  # for DEBUG: best score per label
            best, label = scores.max(1), scores.argmax(1)
            for i in np.flatnonzero(best >= 0.25):
                name = self.labels[label[i]]
                seen[name] = max(seen.get(name, 0.0), float(best[i]))

        def rects_of(keep):
            cx, cy, bw, bh = rows[keep, 0], rows[keep, 1], rows[keep, 2], rows[keep, 3]
            return np.stack([(cx - bw / 2) * fx, (cy - bh / 2) * fy, bw * fx, bh * fy], 1)

        # Target parts: score of the most likely *target* class, even if
        # another class scores higher at that spot
        targets = []
        tscore = scores[:, self.target_ids].max(1) if len(self.target_ids) else np.zeros(len(scores))
        keep = np.flatnonzero(tscore >= min_score) if len(self.target_ids) else []
        if len(keep):
            rects = rects_of(keep)
            targets = [tuple(rects[i]) for i in _nms(rects, tscore[keep], min_score, NMS_OVERLAP)]

        signs = []
        for cid, name in self.sign_ids:
            lowest = MALE_SIGN_MIN if name in MALE_BODY else SIGN_MIN
            keep = np.flatnonzero(scores[:, cid] >= lowest)
            if len(keep):
                rects, sc = rects_of(keep), scores[keep, cid]
                signs += [(name, float(sc[i]), *rects[i]) for i in _nms(rects, sc, lowest, NMS_OVERLAP)]
        return targets, signs


class Person:
    """A person found by the person model, with their outline."""

    def __init__(self, box, score, mask, mbox, k) -> None:
        self.box = box        # (x, y, w, h) in image pixels
        self.score = score
        self.mask = mask      # float 0..1 (chance), outline inside mbox (mask pixels)
        self.mbox = mbox      # (x1, y1, x2, y2) in mask pixels
        self.k = k            # image pixels -> mask pixels

    def contains(self, x: float, y: float) -> bool:
        qx, qy = int(x * self.k) - self.mbox[0], int(y * self.k) - self.mbox[1]
        return 0 <= qy < self.mask.shape[0] and 0 <= qx < self.mask.shape[1] and self.mask[qy, qx] > 0.5

    def mask_for(self, zone: Box) -> np.ndarray:
        """Outline cut out for `zone` (x1, y1, x2, y2 in image pixels), float 0/1."""
        zx1, zy1 = int(zone[0] * self.k), int(zone[1] * self.k)
        zx2, zy2 = math.ceil(zone[2] * self.k), math.ceil(zone[3] * self.k)
        out = np.zeros((max(1, zy2 - zy1), max(1, zx2 - zx1)), np.float32)
        mx1, my1, mx2, my2 = self.mbox
        ox1, oy1 = max(mx1, zx1), max(my1, zy1)
        ox2, oy2 = min(mx2, zx2), min(my2, zy2)
        if ox2 > ox1 and oy2 > oy1:
            out[oy1 - zy1:oy2 - zy1, ox1 - zx1:ox2 - zx1] = self.mask[oy1 - my1:oy2 - my1, ox1 - mx1:ox2 - mx1]
        return out


class PersonFinder:
    """YOLO11n-seg person outlines (COCO class 0)."""

    def __init__(self, path: str, threads: int) -> None:
        self.session, self.device = _make_session(path, threads)
        self.input_name = self.session.get_inputs()[0].name

    def find(self, img: np.ndarray, max_side: int) -> list[Person]:
        h, w = img.shape[:2]
        scale = min(1.0, max_side / max(h, w))
        blob, fx, fy = _to_blob(img, scale)
        det, protos = self.session.run(None, {self.input_name: blob})
        rows, protos = det[0].T, protos[0]
        score = rows[:, 4]
        keep = np.flatnonzero(score >= PERSON_THRESHOLD)
        if not len(keep):
            return []
        cx, cy, bw, bh = rows[keep, 0], rows[keep, 1], rows[keep, 2], rows[keep, 3]
        rects = np.stack([cx - bw / 2, cy - bh / 2, bw, bh], 1)     # model pixels
        ph, pw = protos.shape[1:]
        k_model = pw / blob.shape[3]                                 # model -> mask pixels
        people = []
        for i in _nms(rects, score[keep], PERSON_THRESHOLD, 0.5):
            x, y, bw_, bh_ = rects[i]
            mx1, my1 = max(0, int(x * k_model)), max(0, int(y * k_model))
            mx2, my2 = min(pw, math.ceil((x + bw_) * k_model)), min(ph, math.ceil((y + bh_) * k_model))
            if mx2 <= mx1 or my2 <= my1:
                continue
            coef = rows[keep[i], 84:116]
            logits = coef @ protos[:, my1:my2, mx1:mx2].reshape(32, -1)
            # Kept as a chance (0..1), not yes/no: a smooth outline when it is
            # enlarged to small blocks
            mask = (1.0 / (1.0 + np.exp(-np.clip(logits, -30, 30)))).astype(np.float32)
            mask = mask.reshape(my2 - my1, mx2 - mx1)
            box = (x * fx, y * fy, bw_ * fx, bh_ * fy)
            people.append(Person(box, float(score[keep[i]]), mask, (mx1, my1, mx2, my2), k_model / fx))
        return people


# --- Turning detections into zones ---

class ZoneSpec:
    def __init__(self, box: Box, kind: str, mask=None, holes=(), pad: float = 0.0,
                 edges: str = "") -> None:
        self.box = box        # x1, y1, x2, y2 in monitor pixels
        self.kind = kind      # "part", "body" or "show" (an opening)
        self.mask = mask      # outline (float 0/1) covering box, any resolution
        self.holes = list(holes)  # face openings, relative to the box corner
        self.pad = pad        # padding fraction used for this box
        self.edges = edges    # screen edges the detected thing touches: "l", "t", "r", "b"


def face_margin() -> float:
    """...and only this much surer than for a man's face (more with LEAN_TO_MALE)."""
    return 0.1 + 0.3 * LEAN_TO_MALE


def is_woman_face(sign, signs) -> bool:
    """A FACE_FEMALE detection that is sure enough, and not a man's face
    detected on the same spot (NudeNet often calls men's faces female)."""
    n, s, x, y, w, h = sign
    if n != "FACE_FEMALE" or s < WOMAN_FACE_MIN:
        return False
    cx, cy = x + w / 2, y + h / 2
    male = max((s2 for n2, s2, x2, y2, w2, h2 in signs
                if n2 == "FACE_MALE" and x2 <= cx <= x2 + w2 and y2 <= cy <= y2 + h2), default=0.0)
    return s >= male + face_margin()


def face_owner(sign, signs, men, women):
    """Whose face is this: "woman", "man", or None when unsure (then it stays visible)."""
    n, s, x, y, w, h = sign
    cx, cy = x + w / 2, y + h / 2

    def best(label):
        return max((s2 for n2, s2, x2, y2, w2, h2 in signs
                    if n2 == label and x2 <= cx <= x2 + w2 and y2 <= cy <= y2 + h2), default=0.0)

    female, male = best("FACE_FEMALE"), best("FACE_MALE")
    on_man = any(m.contains(cx, cy) for m in men)
    on_woman = any(p.contains(cx, cy) for p in women)
    if on_man and not on_woman:
        return "man"
    if female >= WOMAN_FACE_MIN and female >= male + face_margin():
        return "woman"
    if male >= WOMAN_FACE_MIN and male >= female + 0.1:
        return "man"
    if on_woman:
        return "woman"
    return None


def person_gender(p: Person, signs) -> str:
    """'woman', 'man' (stays visible) or 'unknown', from the signs inside this
    person's outline. The body counts first; clothing-covered female signs
    only when clear."""
    inside = [g for g in signs if p.contains(g[2] + g[4] / 2, g[3] + g[5] / 2)]

    def best(labels, lowest=0.0):
        return max((s for n, s, x, y, w, h in inside if n in labels and s >= lowest), default=0.0)

    female_body = max(best(FEMALE_BODY - {"FEMALE_BREAST_COVERED", "FEMALE_GENITALIA_COVERED"}),
                      best({"FEMALE_BREAST_COVERED", "FEMALE_GENITALIA_COVERED"}, CLEAR_FEMALE_PART))
    male_body = best(MALE_BODY)
    if male_body > 0 and male_body + LEAN_TO_MALE >= female_body:
        return "man"
    if female_body > 0:
        return "woman"
    if TRUST_FACES:
        if any(is_woman_face(g, signs) for g in inside):
            return "woman"
        if best({"FACE_MALE"}, SIGN_MIN) > 0 or best({"FACE_FEMALE"}, SIGN_MIN) > 0:
            return "man"   # a face, but not clearly a woman's
    return "unknown"


def shows_skin(p: Person, signs) -> bool:
    return any(n in EXPOSED_SIGNS and p.contains(x + w / 2, y + h / 2) for n, s, x, y, w, h in signs)


def quick_zones(targets, signs, people: list[Person], img_w: int, img_h: int) -> list[ZoneSpec]:
    """After the quick low-detail look: women without any male sign. Sent
    before the detailed look; the complete result removes what it doesn't
    confirm, so a man taken for a woman is uncovered again within moments."""
    women = [p for p in people if person_gender(p, signs) == "woman"
             and not any(n in MALE_SIGNS and p.contains(x + w / 2, y + h / 2)
                         for n, s, x, y, w, h in signs)]
    if not women:
        return []
    on_them = [t for t in targets
               if any(p.contains(t[0] + t[2] / 2, t[1] + t[3] / 2) for p in women)]
    return build_zones(on_them, signs, women, 0, 0, img_w, img_h)


def inner_xywh(box: Box, pad: float):
    x1, y1, x2, y2 = inner(box, pad)
    return x1, y1, x2 - x1, y2 - y1


def build_zones(targets, signs, people: list[Person], ox: int, oy: int,
                img_w: int, img_h: int) -> list[ZoneSpec]:
    """Detections in a (cropped) image at offset (ox, oy) -> zones in monitor pixels."""
    male_parts = [(x, y, x + w, y + h) for n, s, x, y, w, h in signs
                  if n == "MALE_GENITALIA_EXPOSED"] if SHOW_MALE_GENITALIA else []
    if male_parts:
        # A female-part detection on the same spot as male genitals is a mix-up
        targets = [(x, y, w, h) for x, y, w, h in targets
                   if not any(mx1 <= x + w / 2 <= mx2 and my1 <= y + h / 2 <= my2
                              for mx1, my1, mx2, my2 in male_parts)]

    genders = [(p, person_gender(p, signs)) for p in people]
    if IGNORE_FEMALE_PARTS_ON_MEN:
        men = [p for p, g in genders if g == "man"]
        others = [p for p, g in genders if g != "man"]
        clear = [(x, y, x + w, y + h) for n, s, x, y, w, h in signs
                 if n in FEMALE_BODY and s >= CLEAR_FEMALE_PART]

        # Male genitals to be covered are not "female parts on a man"
        male_spots = [(x, y, x + w, y + h) for n, s, x, y, w, h in signs
                      if n == "MALE_GENITALIA_EXPOSED"] if "MALE_GENITALIA_EXPOSED" in TARGET_CLASSES else []

        def keep(x, y, w, h) -> bool:
            cx, cy = x + w / 2, y + h / 2
            if any(c[0] <= cx <= c[2] and c[1] <= cy <= c[3] for c in clear + male_spots):
                return True
            return not (any(m.contains(cx, cy) for m in men)
                        and not any(o.contains(cx, cy) for o in others))

        targets = [t for t in targets if keep(*t)]

    # Men covered but women not: buttocks found on a recognised woman stay
    # visible (the detector itself does not know whose buttocks they are)
    if COVER_MALE_BODIES and not COVER_FEMALE_BODIES:
        women = [p for p, g in genders if g == "woman"]
        men_ = [p for p, g in genders if g == "man"]
        rear = [(x, y, x + w, y + h) for n, s, x, y, w, h in signs if n in ("BUTTOCKS_EXPOSED", "ANUS_EXPOSED")]

        def hers(x, y, w, h) -> bool:
            cx, cy = x + w / 2, y + h / 2
            return (any(r[0] <= cx <= r[2] and r[1] <= cy <= r[3] for r in rear)
                    and any(p.contains(cx, cy) for p in women) and not any(m.contains(cx, cy) for m in men_))

        targets = [t for t in targets if not hers(*t)]

    def edges_of(x, y, w, h) -> str:  # in screen pixels
        return "".join(e for e, hit in (("l", x <= 2), ("t", y <= 2), ("r", x + w >= img_w - 2),
                                        ("b", y + h >= img_h - 2)) if hit)

    zones = []
    # Breasts and buttocks get the margin of the slider; genitals at least
    # GENITAL_MIN_PADDING
    small = [(x, y, w, h) for n, s_, x, y, w, h in signs if "GENITALIA" in n or "ANUS" in n]

    def pad_for(x, y, w, h) -> float:
        cx, cy = x + w / 2, y + h / 2
        if any(sx <= cx <= sx + sw and sy <= cy <= sy + sh for sx, sy, sw, sh in small):
            return max(BOX_PADDING, GENITAL_MIN_PADDING)
        return BOX_PADDING

    padded = []
    for x, y, w, h in targets:
        pad = pad_for(x, y, w, h)
        b = pad_box((x + ox, y + oy, w, h), pad, img_w, img_h)
        if b:
            padded.append((b, pad))
    for b in merge_boxes([b for b, _ in padded]):
        # The smallest margin of what was joined: the oval stays big enough
        pad = min((p_ for pb, p_ in padded if pb[0] >= b[0] and pb[1] >= b[1]
                   and pb[2] <= b[2] and pb[3] <= b[3]), default=BOX_PADDING)
        zones.append(ZoneSpec(b, "part", pad=pad, edges=edges_of(*inner_xywh(b, pad))))

    cover = {"woman": COVER_FEMALE_BODIES, "man": COVER_MALE_BODIES, "unknown": COVER_UNKNOWN_PEOPLE}
    for p, gender in genders:
        if not cover[gender] or (gender == "unknown" and not shows_skin(p, signs)):
            continue
        x, y, w, h = p.box
        grow = BODY_GROW_BLOCKS * PIXEL_BLOCK_SIZE
        local = (max(0, int(x) - grow), max(0, int(y) - grow), int(x + w) + grow, int(y + h) + grow)
        box = (local[0] + ox, local[1] + oy, min(img_w, local[2] + ox), min(img_h, local[3] + oy))
        if box[2] > box[0] and box[3] > box[1]:
            local = (local[0], local[1], box[2] - ox, box[3] - oy)
            zones.append(ZoneSpec(box, "body", mask=p.mask_for(local), pad=0.0,
                                  edges=edges_of(x + ox, y + oy, w, h)))

    # See-through openings (faces, male genitals): zones of their own, followed
    # like the mosaic, that keep the mosaic away
    men = [p for p, g in genders if g == "man"]
    women = [p for p, g in genders if g == "woman"]
    for sign in signs:
        n, s, x, y, w, h = sign
        g = None
        mode = "show"
        if n in FACE_LABELS:
            owner = face_owner(sign, signs, men, women)
            mode = FACE_WOMEN if owner == "woman" else FACE_MEN if owner == "man" else "show"
        if mode == "noeyes":
            # A wide mosaic band over the eyes, like an anonymous photo; the rest
            # of the face stays visible
            band = (round(x + ox - 0.15 * w), round(y + oy + 0.12 * h),
                    round(x + ox + 1.15 * w), round(y + oy + 0.6 * h))
            band = (max(0, band[0]), max(0, band[1]), min(img_w, band[2]), min(img_h, band[3]))
            if band[2] > band[0] and band[3] > band[1]:
                zones.append(ZoneSpec(band, "bar", pad=0.0))
                face = pad_box((x + ox, y + oy, w, h), FACE_OPENING_GROW, img_w, img_h)
                if face:   # openings above and below the band, never over it
                    for b in ((face[0], face[1], face[2], band[1]), (face[0], band[3], face[2], face[3])):
                        if b[2] > b[0] and b[3] - b[1] >= 2:
                            zones.append(ZoneSpec(b, "show", pad=0.0))
            continue
        if mode != "show":   # covered, also when the body is not
            b = pad_box((x + ox, y + oy, w, h), 0.1, img_w, img_h)
            if b:
                zones.append(ZoneSpec(b, "part", pad=0.1))
        if mode == "hide":
            continue
        if mode == "eyes":
            # The band across the eyes, a little wider than the face
            b = (round(x + ox - 0.05 * w), round(y + oy + 0.2 * h),
                 round(x + ox + 1.05 * w), round(y + oy + 0.55 * h))
            b = (max(0, b[0]), max(0, b[1]), min(img_w, b[2]), min(img_h, b[3]))
            if b[2] > b[0] and b[3] > b[1]:
                zones.append(ZoneSpec(b, "show", pad=0.0))
            continue
        if SHOW_FACES and n in FACE_LABELS:
            g = FACE_OPENING_GROW
        elif SHOW_MALE_GENITALIA and n == "MALE_GENITALIA_EXPOSED":
            g = MALE_OPENING_GROW
        if g is not None:
            b = pad_box((x + ox, y + oy, w, h), g, img_w, img_h)
            if b:
                zones.append(ZoneSpec(b, "show", pad=g))
    return zones


# --- Scanners (each in its own process, so they never slow down the redraws) ---

class ChangeFinder:
    """Finds the parts of the screen that changed since the previous scan."""

    STEP = 8  # compare at 1/8 size: fast, and ignores single-pixel noise

    def __init__(self, img_w: int, img_h: int) -> None:
        self.w, self.h = img_w, img_h
        self.prev = None
        self.last_full = 0.0
        self.last_call = 0.0

    def areas(self, frame: np.ndarray, now: float, moved=(0.0, 0.0)):
        """List of changed areas, or None when the whole screen should be scanned.
        `moved`: how far the whole picture moved (scrolled) since the previous call."""
        small = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY),
                           (self.w // self.STEP, self.h // self.STEP), interpolation=cv2.INTER_AREA)
        prev, self.prev = self.prev, small
        scrolling = abs(moved[0]) >= 1 or abs(moved[1]) >= 1
        # The first picture after a still screen: what changed comes first,
        # the regular look at everything waits for the next round
        woke = self.last_call and now - self.last_call > 0.5
        self.last_call = now
        if prev is None or (now - self.last_full >= FULL_REFRESH_SECONDS and not scrolling
                            and not woke):
            self.last_full = now
            return None
        if scrolling:
            # Compare with the old picture moved along: only what is really new
            # (the strip that scrolled in, other changes) counts
            move = np.float32([[1, 0, moved[0] / self.STEP], [0, 1, moved[1] / self.STEP]])
            size = (small.shape[1], small.shape[0])
            prev = cv2.warpAffine(prev, move, size, flags=cv2.INTER_LINEAR)
            valid = cv2.warpAffine(np.ones_like(small), move, size, flags=cv2.INTER_NEAREST)
            mask = ((cv2.absdiff(small, prev) > 12) | (valid == 0)).astype(np.uint8)
        else:
            mask = (cv2.absdiff(small, prev) > 8).astype(np.uint8)
        if not mask.any():
            return []
        mask = cv2.dilate(mask, np.ones((5, 5), np.uint8))
        _, _, stats, _ = cv2.connectedComponentsWithStats(mask)
        s = self.STEP
        boxes = [(x * s, y * s, (x + bw) * s, (y + bh) * s) for x, y, bw, bh, _ in stats[1:]]
        boxes = merge_boxes([expand(b, 0.1, 48, self.w, self.h) for b in boxes])
        if sum((b[2] - b[0]) * (b[3] - b[1]) for b in boxes) > 0.6 * self.w * self.h:
            return None  # most of the screen changed: one scan of everything is faster
        return boxes


class FrameSource:
    """The newest screen pictures, shared between the processes. A separate
    capture process writes them; everybody else just reads."""

    BUFFERS = 4

    def __init__(self, name: str, w: int, h: int, counter, times, scroll, small_name: str) -> None:
        from multiprocessing import shared_memory
        self.shm = shared_memory.SharedMemory(name=name)
        self.frames = np.ndarray((self.BUFFERS, h, w, 4), np.uint8, buffer=self.shm.buf)
        self.counter, self.times, self.scroll = counter, times, scroll
        # Per picture also: a quarter-size gray copy (to measure movement) and
        # the average colour of every mosaic block
        self.qw, self.qh = int(w * QUARTER), int(h * QUARTER)
        self.gw, self.gh = -(-w // PIXEL_BLOCK_SIZE), -(-h // PIXEL_BLOCK_SIZE)
        self.small_shm = shared_memory.SharedMemory(name=small_name)
        q = self.BUFFERS * self.qh * self.qw
        self.quarter = np.ndarray((self.BUFFERS, self.qh, self.qw), np.uint8, buffer=self.small_shm.buf)
        self.colors = np.ndarray((self.BUFFERS, self.gh, self.gw, 3), np.uint8,
                                 buffer=self.small_shm.buf, offset=q)

    @staticmethod
    def small_size(w: int, h: int) -> int:
        gw, gh = -(-w // PIXEL_BLOCK_SIZE), -(-h // PIXEL_BLOCK_SIZE)
        return FrameSource.BUFFERS * (int(h * QUARTER) * int(w * QUARTER) + gh * gw * 3)

    def latest(self):
        """(frame number, picture (BGRA, shared: copy what you keep), capture time)."""
        n = self.counter.value
        if n == 0:
            return 0, None, 0.0
        i = n % self.BUFFERS
        return n, self.frames[i], self.times[i]

    def scrolled(self, n: int) -> tuple[float, float]:
        """Running total of the whole-screen movement (scrolling) up to picture n."""
        i = n % self.BUFFERS
        return self.scroll[2 * i], self.scroll[2 * i + 1]

    def wait_new(self, last: int, stop, timeout: float = 1.0):
        end = time.monotonic() + timeout
        while self.counter.value == last and not stop.is_set() and time.monotonic() < end:
            time.sleep(0.003)
        return self.latest()

    def close(self) -> None:
        for s in (self.shm, self.small_shm):
            try:
                s.close()
            except Exception:
                pass


def capture_main(region: dict, shm_args, use_mss, stop, info_q) -> None:
    """Start of the capture process: copies the screen into shared memory,
    as often as the screen changes (dxcam) or CAPTURE_FPS times a second (mss)."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    make_dpi_aware()
    precise_timers(True)
    w, h = region["width"], region["height"]
    src = FrameSource(*shm_args)
    cam, sct = None, None
    if USE_DXCAM and MONITOR_INDEX == 1:
        try:
            import dxcam
            cam = dxcam.create(output_color="BGRA")
        except ImportError:
            cam = None
        except Exception as e:
            info_q.put(f"dxcam not usable ({e}); using mss.")
            cam = None
    info_q.put(("method", "dxcam" if cam is not None else "mss"))
    last = 0.0
    prev_small, total = None, [0.0, 0.0]
    while not stop.is_set():
        try:
            if cam is not None and use_mss.is_set():
                cam.release()
                cam = None
                info_q.put(("method", "mss"))
            if cam is not None:
                wait = last + 1 / CAPTURE_FPS - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                try:
                    frame = cam.grab(copy=False, new_frame_only=True)  # copied below anyway
                except TypeError:
                    frame = cam.grab()
                if frame is None:  # the screen didn't change
                    time.sleep(0.002)
                    continue
                last = time.monotonic()
                if frame.shape[:2] != (h, w):
                    info_q.put(f"dxcam picture size {frame.shape[1]}x{frame.shape[0]} differs; using mss.")
                    use_mss.set()
                    continue
            else:
                wait = last + 1 / CAPTURE_FPS - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                last = time.monotonic()
                if sct is None:
                    sct = new_mss()
                frame = grab_bgra(sct, region["left"], region["top"], w, h)
            t = time.monotonic()
            quarter = cv2.resize(frame, (src.qw, src.qh), interpolation=cv2.INTER_AREA)
            gray = cv2.cvtColor(quarter, cv2.COLOR_BGRA2GRAY)
            # How far did the whole picture move since the previous one (scrolling)?
            small = cv2.resize(gray, (w // 8, h // 8), interpolation=cv2.INTER_AREA).astype(np.float32)
            if cam is None and prev_small is not None and np.array_equal(small, prev_small):
                continue  # mss: the screen didn't change (dxcam leaves these out itself)
            if prev_small is not None:
                (sx, sy), response = cv2.phaseCorrelate(prev_small, small)
                if response > 0.25 and (abs(sx) > 0.2 or abs(sy) > 0.2):
                    total[0] += sx * 8
                    total[1] += sy * 8
            prev_small = small
            n = src.counter.value + 1
            i = n % FrameSource.BUFFERS
            src.frames[i] = frame
            src.quarter[i] = gray
            src.colors[i] = cv2.resize(quarter, (src.gw, src.gh), interpolation=cv2.INTER_AREA)[:, :, :3]
            src.times[i] = t
            src.scroll[2 * i], src.scroll[2 * i + 1] = total
            src.counter.value = n
        except Exception:
            traceback.print_exc()
            time.sleep(0.5)
    if cam is not None:
        cam.release()
    src.close()


def pack_zones(zones: list["ZoneSpec"], image: np.ndarray, ox: int, oy: int) -> list[dict]:
    """Zones as plain data for the overlay process, each with a small picture
    of the detected thing (used to follow it)."""
    ih, iw = image.shape[:2]
    out = []
    for z in zones:
        tmpl = None
        ix1, iy1, ix2, iy2 = inner(z.box, z.pad)
        lx1, ly1, lx2, ly2 = ix1 - ox, iy1 - oy, ix2 - ox, iy2 - oy
        if lx1 >= 0 and ly1 >= 0 and lx2 <= iw and ly2 <= ih:
            tmpl = make_template(cv2.cvtColor(image[ly1:ly2, lx1:lx2], cv2.COLOR_BGR2GRAY))
        out.append({"box": z.box, "kind": z.kind, "mask": z.mask, "holes": z.holes,
                    "pad": z.pad, "tmpl": tmpl, "edges": z.edges})
    return out


def send(out_q, item) -> None:
    try:
        out_q.put(item, timeout=0.5)
    except Exception:
        pass  # the overlay is busy; this result would be stale anyway


PAUSED = None   # set while none of ONLY_IN_PROGRAMS is on screen (scanner processes)


def paused() -> bool:
    if PAUSED is not None and PAUSED.is_set():
        time.sleep(0.1)
        return True
    return False


def full_scan_loop(nude: FastNude, finder, region: dict, source: FrameSource, out_q, zones_q,
                   stop) -> None:
    w, h = region["width"], region["height"]
    changes = ChangeFinder(w, h) if SCAN_ONLY_CHANGES else None
    seen_frame, last_scroll = 0, (0.0, 0.0)
    while not stop.is_set():
        if paused():
            continue
        try:
            n, shared_frame, start = source.wait_new(seen_frame, stop)
            if shared_frame is None or n == seen_frame:
                continue  # still screen: nothing new to look at
            seen_frame = n
            began = time.monotonic()
            scroll = source.scrolled(seen_frame)
            frame = cv2.cvtColor(shared_frame, cv2.COLOR_BGRA2BGR)  # also makes our own copy
            moved = (scroll[0] - last_scroll[0], scroll[1] - last_scroll[1])
            last_scroll = scroll
            areas = changes.areas(frame, start, moved) if changes else None
            seen: dict | None = {} if DEBUG else None
            targets, signs = [], []
            scanned = 1.0
            low = [s for s in SCAN_SCALES if s < 0.5]
            high = [s for s in SCAN_SCALES if s >= 0.5]
            for s in low:  # the quick look at the whole screen first
                t, g = nude.detect(frame, s, CONFIDENCE_THRESHOLD, seen)
                targets += t
                signs += g
            people = finder.find(frame, 800) if finder else []
            if low and high:
                # Big things are known now: send them before the slow detailed look
                early = quick_zones(targets, signs, people, w, h)
                if early:
                    send(out_q, ("quick", pack_zones(early, frame, 0, 0), start, 0.0, 0.0))
            for s in high:
                if areas is not None:
                    # Full detail is expensive: only where the screen changed
                    for x1, y1, x2, y2 in areas:
                        t, g = nude.detect(frame[y1:y2, x1:x2], s, CONFIDENCE_THRESHOLD, seen)
                        targets += [(x + x1, y + y1, bw, bh) for x, y, bw, bh in t]
                        signs += [(n, sc, x + x1, y + y1, bw, bh) for n, sc, x, y, bw, bh in g]
                    scanned = sum((b[2] - b[0]) * (b[3] - b[1]) for b in areas) / (w * h)
                else:
                    t, g = nude.detect(frame, s, CONFIDENCE_THRESHOLD, seen)
                    targets += t
                    signs += g
            zones = build_zones(targets, signs, people, 0, 0, w, h)
            send(out_q, ("full", pack_zones(zones, frame, 0, 0), start,
                         time.monotonic() - began, scanned))
            if DEBUG and (seen or people):
                labels = ", ".join(f"{k} {v:.2f}" for k, v in sorted((seen or {}).items()))
                bodies = sum(z.kind == "body" for z in zones)
                print(f"[{time.strftime('%H:%M:%S')}] seen: {labels}; {len(people)} person(s), "
                      f"{bodies} covered  -> {len(zones)} zone(s)")
        except Exception:
            traceback.print_exc()
            time.sleep(1)
        if DETECT_PAUSE:
            time.sleep(DETECT_PAUSE)


def focus_scan_loop(nude: FastNude, finder, region: dict, source: FrameSource, out_q, zones_q,
                    stop) -> None:
    w, h = region["width"], region["height"]
    zones: list[Box] = []
    last_scroll, last_time = None, 0.0
    # Results of the last round: while the screen does not change, they are
    # sent again instead of scanning the same picture over and over
    done_frame, done_areas, done_msgs, done_time = -1, [], [], 0.0

    def same_areas(a, b) -> bool:
        return len(a) == len(b) and all(max(abs(p - q) for p, q in zip(x, y)) <= 8
                                        for x, y in zip(a, b))

    while not stop.is_set():
        if paused():
            continue
        try:
            while True:  # take the newest zone list from the overlay
                try:
                    zones = zones_q.get_nowait()
                except Exception:
                    break
            # While the page scrolls, also check the strip where new content comes in
            areas = [expand(z, FOCUS_MARGIN, 16, w, h) for z in zones]
            n, _, t = source.latest()
            if n:
                sx, sy = source.scrolled(n)
                if last_scroll is not None and t > last_time:
                    vx = (sx - last_scroll[0]) / (t - last_time)
                    vy = (sy - last_scroll[1]) / (t - last_time)
                    if vy < -50:
                        areas.append((0, int(h * 0.6), w, h))      # scrolling down: bottom
                    elif vy > 50:
                        areas.append((0, 0, w, int(h * 0.4)))      # scrolling up: top
                    if vx < -50:
                        areas.append((int(w * 0.6), 0, w, h))
                    elif vx > 50:
                        areas.append((0, 0, int(w * 0.4), h))
                last_scroll, last_time = (sx, sy), t
            if not areas:
                time.sleep(0.02)
                continue
            areas = merge_boxes(areas)
            if n == done_frame and same_areas(areas, done_areas):
                if time.monotonic() - done_time >= 0.15:
                    for m in done_msgs:
                        send(out_q, ("again",) + m[1:])
                    done_time = time.monotonic()
                time.sleep(0.01)
                continue
            done_frame, done_areas, done_msgs, done_time = n, areas, [], time.monotonic()
            for area in areas:
                ax1, ay1, ax2, ay2 = area
                _, shared_frame, t0 = source.latest()
                if shared_frame is None:
                    break
                crop = cv2.cvtColor(shared_frame[ay1:ay2, ax1:ax2], cv2.COLOR_BGRA2BGR)
                # Small areas are zoomed in for more detail, big ones scanned at
                # a detail level that keeps the scan quick
                scale = min(2.0, max(0.35, 640 / max(ax2 - ax1, ay2 - ay1)))
                targets, signs = nude.detect(crop, scale, CONFIDENCE_THRESHOLD)
                people = finder.find(crop, 384) if finder else []
                # A person cut off by the edge of this area is only partly
                # visible here; the full-screen scan handles them
                cw, ch = ax2 - ax1, ay2 - ay1
                people = [p for p in people
                          if not ((p.box[0] <= 2 and ax1 > 0) or (p.box[1] <= 2 and ay1 > 0)
                                  or (p.box[0] + p.box[2] >= cw - 2 and ax2 < w)
                                  or (p.box[1] + p.box[3] >= ch - 2 and ay2 < h))]
                specs = build_zones(targets, signs, people, ax1, ay1, w, h)
                msg = ("focus", pack_zones(specs, crop, ax1, ay1), t0, 0.0, 0.0)
                send(out_q, msg)
                done_msgs.append(msg)
            if FOCUS_PAUSE:
                time.sleep(FOCUS_PAUSE)
        except Exception:
            traceback.print_exc()
            time.sleep(1)


def person_model_path() -> str:
    return os.path.join(HERE, PERSON_MODEL)


def scanner_main(kind: str, region: dict, shm_args, out_q, zones_q, stop, pause=None) -> None:
    """Start of a scanner process ("full" or "focus")."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # the main process handles Ctrl+C
    try:  # the video and the redraws come first
        kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x4000)  # BELOW_NORMAL
    except Exception:
        pass
    make_dpi_aware()
    cv2.setNumThreads(2)
    cpus = os.cpu_count() or 4
    # Low priority, so they may use most of the processor without slowing the
    # redraws. Together they use the threads the capture and redraws leave
    # free, no more: more threads than the processor has only slow each other down.
    focus_threads = max(1, cpus // 3) if FOCUS_SCAN else 0
    threads = max(1, cpus - 2 - focus_threads) if kind == "full" else focus_threads
    threads = max(1, round(threads * THREAD_SHARE))   # processor use setting
    nude = FastNude(threads)
    finder = None
    if (COVER_FEMALE_BODIES or COVER_MALE_BODIES) and os.path.exists(person_model_path()):
        finder = PersonFinder(person_model_path(), threads)
    source = FrameSource(*shm_args)
    loop = full_scan_loop if kind == "full" else focus_scan_loop
    global PAUSED
    PAUSED = pause
    loop(nude, finder, region, source, out_q, zones_q, stop)
    source.close()


# --- Following movement between scans ---

def make_template(gray_patch: np.ndarray):
    """Small grayscale copy of a zone, or None if there's nothing to follow."""
    h, w = gray_patch.shape[:2]
    if w < 4 or h < 4:
        return None
    s = min(1.0, TRACK_SIZE / max(w, h))
    tmpl = cv2.resize(gray_patch, (max(4, round(w * s)), max(4, round(h * s))),
                      interpolation=cv2.INTER_AREA)
    return tmpl if tmpl.std() >= 3 else None


def paste(dst: np.ndarray, src: np.ndarray, x: int, y: int) -> None:
    """Copy src into dst at (x, y), cutting off what falls outside."""
    h, w = src.shape[:2]
    H, W = dst.shape[:2]
    x1, y1, x2, y2 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)
    if x2 > x1 and y2 > y1:
        dst[y1:y2, x1:x2] = src[y1 - y:y2 - y, x1 - x:x2 - x]


def inner(box: Box, pad: float) -> Box:
    """The detected thing itself, without the extra padding around it."""
    x1, y1, x2, y2 = box
    k = pad / (1 + 2 * pad)
    kx, ky = int((x2 - x1) * k), int((y2 - y1) * k)
    return (x1 + kx, y1 + ky, x2 - kx, y2 - ky)


class Zone:
    def __init__(self, spec: ZoneSpec, tmpl, now: float, batch: int, t0: float | None = None) -> None:
        self.kind = spec.kind
        self.fx, self.fy = float(spec.box[0]), float(spec.box[1])   # exact position
        self.w, self.h = spec.box[2] - spec.box[0], spec.box[3] - spec.box[1]
        self.mask, self.holes, self.pad = spec.mask, spec.holes, spec.pad
        self.edges = spec.edges
        self.tmpl = tmpl
        self.confirmed = now
        self.batch = batch      # which scan result it came from
        self.fresh = True       # not yet placed in the current picture
        self.moves = 0
        self.lost_since = 0.0   # when following started to fail (0 = not lost)
        self.early = None       # picture time, while only the quick look has found it
        self.vx = self.vy = 0.0  # speed in pixels per second
        t0 = now if t0 is None else t0  # when the picture it was found in was taken
        self.last_move = t0
        # How far it has travelled, over time. A scan result describes the
        # screen as it was when the scan started; this tells where the zone
        # was back then, so the result can't drag it back to an old spot.
        self.travel = (0.0, 0.0)
        self.history: deque = deque([(t0, 0.0, 0.0)])
        self.pts = None         # points on the thing, followed from picture to picture

    @property
    def box(self) -> Box:
        x, y = round(self.fx), round(self.fy)
        return (x, y, x + self.w, y + self.h)

    def move(self, dx: float, dy: float, now: float, img_w: int, img_h: int) -> None:
        # Partly off screen is fine (content scrolling away), but keep a bit on it
        nx = min(max(16.0 - self.w, self.fx + dx), img_w - 16.0)
        ny = min(max(16.0 - self.h, self.fy + dy), img_h - 16.0)
        dx, dy = nx - self.fx, ny - self.fy
        dt = now - self.last_move
        if not self.fresh and 0 < dt < 0.25:
            self.vx = 0.5 * self.vx + 0.5 * dx / dt
            self.vy = 0.5 * self.vy + 0.5 * dy / dt
        self.last_move = now
        self.fx, self.fy = nx, ny
        self.travel = (self.travel[0] + dx, self.travel[1] + dy)
        self.history.append((now, *self.travel))
        while len(self.history) > 2 and now - self.history[1][0] > 3.0:
            self.history.popleft()

    def moved_since(self, t: float) -> tuple[float, float]:
        tx, ty = self.history[0][1], self.history[0][2]
        for ht, hx, hy in self.history:
            if ht > t:
                break
            tx, ty = hx, hy
        return self.travel[0] - tx, self.travel[1] - ty

    def reshape(self, spec: ZoneSpec, dx: float, dy: float, tmpl, now: float,
                img_w: int, img_h: int) -> None:
        """Take the new size/outline from a scan, shifted by the movement since then."""
        x1, y1, x2, y2 = spec.box
        self.w, self.h = x2 - x1, y2 - y1
        self.fx = min(max(16.0 - self.w, x1 + dx), img_w - 16.0)
        self.fy = min(max(16.0 - self.h, y1 + dy), img_h - 16.0)
        self.mask, self.holes, self.pad = spec.mask, spec.holes, spec.pad
        self.edges = spec.edges
        if tmpl is not None:
            self.tmpl = tmpl
        self.confirmed = now
        self.lost_since = 0.0
        self.pts = None  # pick fresh points on the new shape


HOLDS = {"show": OPENING_HOLD_SECONDS, "body": BODY_HOLD_SECONDS}


class Tracker:
    def __init__(self, img_w: int, img_h: int) -> None:
        self.w, self.h = img_w, img_h
        self.zones: list[Zone] = []
        self.batches = 0
        self.found = 0          # zones found since the start (for the settings program)
        self.prev_gray = None   # the previous screen picture (quarter size, gray)
        self.dis = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_ULTRAFAST)
        self.scroll = (0.0, 0.0)  # overall movement between the last two pictures
        self.scrolled: deque = deque()  # (picture time, running scroll total)

    def scroll_since(self, t: float) -> tuple[float, float]:
        """How far the whole screen moved (scrolled) since time t."""
        if not self.scrolled:
            return 0.0, 0.0
        then = self.scrolled[0]
        for entry in self.scrolled:
            if entry[0] > t:
                break
            then = entry
        last = self.scrolled[-1]
        return last[1] - then[1], last[2] - then[2]

    def add_detections(self, specs: list[dict], now: float, t0: float, kind: str = "full") -> None:
        """A scan result (zones as plain data), describing the screen at time t0.
        kind "quick": early result of the full scan, "full": its complete result."""
        self.batches += 1
        taken: set[int] = set()
        for d in specs:
            spec = ZoneSpec(tuple(d["box"]), d["kind"], d["mask"], d["holes"], d["pad"], d["edges"])
            tmpl = d["tmpl"] if TRACK_MOVEMENT else None
            # Is it a zone we already follow? Compare with where each zone was at t0
            best, best_ov = None, 0.3
            for z in self.zones:
                if z.kind != spec.kind or id(z) in taken or z.batch == self.batches:
                    continue
                dx, dy = z.moved_since(t0)
                x1, y1, x2, y2 = z.box
                ov = overlap(spec.box, (round(x1 - dx), round(y1 - dy), round(x2 - dx), round(y2 - dy)))
                if ov > best_ov:
                    best, best_ov = z, ov
            if best is not None:
                dx, dy = best.moved_since(t0)
                best.reshape(spec, dx, dy, tmpl, now, self.w, self.h)
                taken.add(id(best))
                if kind != "quick":
                    best.early = None
            else:
                z = Zone(spec, tmpl, now, self.batches, t0)
                if kind == "quick":
                    z.early = t0
                if z.kind != "show":
                    self.found += 1
                # The screen may have scrolled since the scan's picture was taken
                sx, sy = self.scroll_since(t0)
                if (sx or sy) and self.scrolled:
                    z.move(sx, sy, self.scrolled[-1][0], self.w, self.h)
                self.zones.append(z)
        if kind == "full":  # the complete look did not confirm these early zones
            self.zones = [z for z in self.zones
                          if z.early is None or z.early > t0 or id(z) in taken]

    def expire(self, now: float) -> None:
        # A zone whose content can't be found any more goes quickly;
        # otherwise it is held HOLD_SECONDS after its last detection
        self.zones = [z for z in self.zones
                      if now - z.confirmed <= HOLDS.get(z.kind, HOLD_SECONDS)
                      and not (z.lost_since and now - z.lost_since > LOST_SECONDS)
                      and z.box[2] > 0 and z.box[3] > 0 and z.box[0] < self.w and z.box[1] < self.h]

    def margins(self, z: Zone) -> tuple[int, int]:
        """How far a zone may move from one picture to the next."""
        ix1, iy1, ix2, iy2 = inner(z.box, z.pad)
        wide = z.fresh or z.lost_since
        m = SEARCH_MARGIN_NEW if wide else SEARCH_MARGIN
        cap = MAX_SHIFT_NEW if wide else MAX_SHIFT
        return min(int((ix2 - ix1) * m) + 16, cap), min(int((iy2 - iy1) * m) + 16, cap)

    def search_area(self, z: Zone) -> Box:
        """Where to look for the zone in the new picture (within the screen)."""
        mx, my = self.margins(z)
        x1, y1, x2, y2 = z.box
        return (max(0, x1 - mx), max(0, y1 - my), min(self.w, x2 + mx), min(self.h, y2 + my))

    def follow_flow(self, z: Zone, flow: np.ndarray, now: float) -> None:
        """Move the zone by the typical movement of its own area (inside the
        outline for bodies, the detected core otherwise)."""
        q = QUARTER
        fh, fw = flow.shape[:2]
        x1, y1, x2, y2 = z.box
        if z.mask is not None:
            qx1, qy1, qx2, qy2 = math.floor(x1 * q), math.floor(y1 * q), math.ceil(x2 * q), math.ceil(y2 * q)
        else:
            ix1, iy1, ix2, iy2 = inner(z.box, z.pad)
            qx1, qy1, qx2, qy2 = math.floor(ix1 * q), math.floor(iy1 * q), math.ceil(ix2 * q), math.ceil(iy2 * q)
        cx1, cy1, cx2, cy2 = max(0, qx1), max(0, qy1), min(fw, qx2), min(fh, qy2)
        sel = None
        if cx2 > cx1 and cy2 > cy1:
            # Sample at most about 2000 points: plenty for a typical value
            step = max(1, int(math.sqrt((cx2 - cx1) * (cy2 - cy1) / 2000)))
            area = flow[cy1:cy2:step, cx1:cx2:step]
            if z.mask is not None:
                m = cv2.resize(z.mask, (qx2 - qx1, qy2 - qy1), interpolation=cv2.INTER_NEAREST) > 0.5
                m = m[cy1 - qy1:cy2 - qy1:step, cx1 - qx1:cx2 - qx1:step]
                sel = area[m[:area.shape[0], :area.shape[1]]]
            else:
                sel = area.reshape(-1, 2)
        if sel is None or len(sel) < 4:
            # (Almost) off screen: go along with the page
            z.move(self.scroll[0], self.scroll[1], now, self.w, self.h)
            return
        d = np.median(sel, axis=0) / q
        mx, my = self.margins(z)
        z.move(max(-mx, min(mx, float(d[0]))), max(-my, min(my, float(d[1]))), now, self.w, self.h)
        z.lost_since = 0.0

    def follow_template(self, z: Zone, gray: np.ndarray, now: float) -> None:
        """Find a new zone (found in an older picture) in the current one."""
        if z.tmpl is None:
            return
        q = QUARTER
        ix1, iy1, ix2, iy2 = inner(z.box, z.pad)
        iw, ih = ix2 - ix1, iy2 - iy1
        x1, y1, x2, y2 = z.box
        sx1, sy1, sx2, sy2 = self.search_area(z)
        # Where the core may go, so that the whole zone stays inside the search area
        rx1, ry1 = sx1 + (ix1 - x1), sy1 + (iy1 - y1)
        rx2, ry2 = sx2 - (x2 - ix2), sy2 - (y2 - iy2)
        if rx1 < 0 or ry1 < 0 or rx2 > self.w or ry2 > self.h:
            return  # partly off screen: the scans will place it
        th, tw = z.tmpl.shape[:2]
        fx, fy = tw / max(1, iw), th / max(1, ih)
        rw, rh = round((rx2 - rx1) * fx), round((ry2 - ry1) * fy)
        if rw < tw or rh < th:
            return
        region = gray[round(ry1 * q):round(ry2 * q), round(rx1 * q):round(rx2 * q)]
        if region.shape[0] < 2 or region.shape[1] < 2:
            return
        small = cv2.resize(region, (rw, rh), interpolation=cv2.INTER_LINEAR)
        res = np.nan_to_num(cv2.matchTemplate(small, z.tmpl, cv2.TM_CCOEFF_NORMED), nan=-1.0)
        _, score, _, (lx, ly) = cv2.minMaxLoc(res)
        if score < MATCH_MIN:
            if not z.lost_since:
                z.lost_since = now
            return
        z.lost_since = 0.0
        z.move(rx1 + lx / fx - ix1, ry1 + ly / fy - iy1, now, self.w, self.h)

    def draw_box(self, z: Zone, ahead: float) -> Box:
        """Where to draw: `ahead` seconds further along its movement."""
        x1, y1, x2, y2 = z.box
        if ahead <= 0 or z.kind == "body" and not z.vx and not z.vy:
            return (x1, y1, x2, y2)
        cap = MAX_SHIFT
        dx = max(-cap, min(cap, round(z.vx * ahead)))
        dy = max(-cap, min(cap, round(z.vy * ahead)))
        x = min(max(16 - z.w, x1 + dx), self.w - 16)
        y = min(max(16 - z.h, y1 + dy), self.h - 16)
        return (x, y, x + z.w, y + z.h)

    def update(self, gray: np.ndarray, now: float, total: tuple[float, float] = (0.0, 0.0)) -> None:
        """gray: the newest screen picture at quarter size, taken at time `now`;
        total: running total of the whole-screen movement (scrolling) up to it."""
        last = self.scrolled[-1] if self.scrolled else (now, *total)
        self.scroll = (total[0] - last[1], total[1] - last[2])
        self.scrolled.append((now, *total))
        while len(self.scrolled) > 2 and now - self.scrolled[1][0] > 5.0:
            self.scrolled.popleft()

        if TRACK_MOVEMENT and self.zones:
            flow = None
            if self.prev_gray is not None and self.prev_gray.shape == gray.shape:
                # The movement of every part of the screen at once. Shifting the
                # previous picture by the scroll movement first makes big jumps easy.
                sx, sy = self.scroll[0] * QUARTER, self.scroll[1] * QUARTER
                prev = self.prev_gray
                if abs(sx) >= 0.5 or abs(sy) >= 0.5:
                    prev = cv2.warpAffine(prev, np.float32([[1, 0, sx], [0, 1, sy]]),
                                          (gray.shape[1], gray.shape[0]), borderMode=cv2.BORDER_REPLICATE)
                flow = self.dis.calc(prev, gray, None)
                flow[..., 0] += sx
                flow[..., 1] += sy
            for z in self.zones:
                if z.fresh:
                    self.follow_template(z, gray, now)   # found in an older picture: place it
                elif flow is not None:
                    self.follow_flow(z, flow, now)
        self.prev_gray = gray

        # A new zone that turns out to be one we already follow: keep the followed one
        fresh = [z for z in self.zones if z.fresh]
        if fresh:
            old = [z for z in self.zones if not z.fresh]
            keep = []
            for f in fresh:
                twin = next((o for o in old if o.kind == f.kind and overlap(o.box, f.box) > 0.5), None)
                if twin is not None:
                    twin.confirmed = max(twin.confirmed, f.confirmed)
                else:
                    f.fresh = False
                    keep.append(f)
            self.zones = old + keep
        # Two zones of one kind on (nearly) the same spot: keep the newest
        merged: list[Zone] = []
        for z in sorted(self.zones, key=lambda z: -z.confirmed):
            if not any(k.kind == z.kind and overlap(k.box, z.box) > SAME_ZONE for k in merged):
                merged.append(z)
        self.zones = merged


# --- Overlay ---

class MosaicGrid:
    """The mosaic as one grid of blocks over the whole monitor. Zones mark the
    blocks they cover; each block shows the average colour of the screen under
    it. Only the blocks that changed are handed to the overlay window."""

    def __init__(self, img_w: int, img_h: int) -> None:
        self.b = b = PIXEL_BLOCK_SIZE
        self.gw, self.gh = -(-img_w // b), -(-img_h // b)
        self.colors = np.zeros((self.gh, self.gw, 3), np.uint8)
        self.prev = np.zeros((self.gh, self.gw), bool)
        self.shown = np.zeros((self.gh, self.gw, 4), np.int16)   # what the window shows now
        self.empty = np.zeros((self.gh, self.gw), bool)
        self.align = b * 4 // math.gcd(b, 4)  # captures start on block (and 4-pixel) lines

    def cells(self, box: Box) -> Box:
        b = self.b
        return (max(0, box[0] // b), max(0, box[1] // b),
                min(self.gw, -(-box[2] // b)), min(self.gh, -(-box[3] // b)))

    def rounded(self, box: Box, gw_: int, gh_: int, zx1: int, zy1: int, pad: float) -> np.ndarray:
        """A zone as an oval instead of a rectangle (no square corners sticking
        out), but always big enough to cover the detected thing itself."""
        if gw_ < 4 or gh_ < 4:
            return np.ones((gh_, gw_), bool)
        b = self.b
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        core = 1 / (1 + 2 * max(0.0, pad))         # the thing itself, as part of the zone
        grow = max(1.0, core * math.sqrt(2) * 1.05)
        ax = max(1.0, (box[2] - box[0]) / 2 * grow + b / 2)
        ay = max(1.0, (box[3] - box[1]) / 2 * grow + b / 2)
        xs = ((np.arange(gw_) + zx1 + 0.5) * b - cx) / ax
        ys = ((np.arange(gh_) + zy1 + 0.5) * b - cy) / ay
        return xs[None, :] ** 2 + ys[:, None] ** 2 <= 1.0

    def coverage(self, items) -> np.ndarray:
        """items: (box, outline or None, kind, screen edges it was cut off by, padding).
        Openings ("show") keep the mosaic away."""
        body = np.zeros((self.gh, self.gw), bool)
        part = np.zeros((self.gh, self.gw), bool)
        openings, bars = [], []
        for box, mask, kind, edges, pad in items:
            if kind == "show":
                openings.append(box)
                continue
            if kind == "bar":   # eye bands: drawn after the openings, so always covered
                bars.append(box)
                continue
            # Whole zone in blocks, even the part that is off screen
            b = self.b
            zx1, zy1, zx2, zy2 = box[0] // b, box[1] // b, -(-box[2] // b), -(-box[3] // b)
            if zx2 <= zx1 or zy2 <= zy1:
                continue
            gw_, gh_ = zx2 - zx1, zy2 - zy1
            if mask is not None:
                if gw_ > mask.shape[1] or gh_ > mask.shape[0]:
                    # Small blocks, coarse outline: smooth it instead of copying
                    # its square steps
                    m = cv2.resize(mask, (gw_, gh_), interpolation=cv2.INTER_LINEAR) > 0.4
                else:
                    m = cv2.resize((mask > 0.5).astype(np.float32), (gw_, gh_),
                                   interpolation=cv2.INTER_AREA) > 0.02
                target = body
            else:
                # Cut off by a screen edge: keep it square, it goes on past the edge
                m = (np.ones((gh_, gw_), bool) if edges
                     else self.rounded(box, gw_, gh_, zx1, zy1, pad))
                target = part
            gx1, gy1, gx2, gy2 = max(0, zx1), max(0, zy1), min(self.gw, zx2), min(self.gh, zy2)
            if gx2 > gx1 and gy2 > gy1:
                target[gy1:gy2, gx1:gx2] |= m[gy1 - zy1:gy2 - zy1, gx1 - zx1:gx2 - zx1]
            if EXTEND_AT_SCREEN_EDGE and edges:
                # It continues beyond the screen edge it was cut off by: if it has
                # moved away from that edge (scrolling), cover the gap the same way
                if "b" in edges and zy2 < self.gh and gx2 > gx1:
                    target[zy2:, gx1:gx2] |= m[-1, gx1 - zx1:gx2 - zx1]
                if "t" in edges and zy1 > 0 and gx2 > gx1:
                    target[:zy1, gx1:gx2] |= m[0, gx1 - zx1:gx2 - zx1]
                if "r" in edges and zx2 < self.gw and gy2 > gy1:
                    target[gy1:gy2, zx2:] |= m[gy1 - zy1:gy2 - zy1, -1:]
                if "l" in edges and zx1 > 0 and gy2 > gy1:
                    target[gy1:gy2, :zx1] |= m[gy1 - zy1:gy2 - zy1, :1]
        if BODY_GROW_BLOCKS and body.any():
            n = 2 * BODY_GROW_BLOCKS + 1
            body = cv2.dilate(body.astype(np.uint8), np.ones((n, n), np.uint8)) > 0
        cover = body | part
        b = self.b
        for hx1, hy1, hx2, hy2 in openings:  # blocks whose centre lies in an opening
            gx1, gy1 = max(0, math.ceil(hx1 / b - 0.5)), max(0, math.ceil(hy1 / b - 0.5))
            gx2 = min(self.gw, math.floor(hx2 / b - 0.5) + 1)
            gy2 = min(self.gh, math.floor(hy2 / b - 0.5) + 1)
            if gx2 > gx1 and gy2 > gy1:
                cover[gy1:gy2, gx1:gx2] = False
        for x1, y1, x2, y2 in bars:
            gx1, gy1 = max(0, x1 // b), max(0, y1 // b)
            gx2, gy2 = min(self.gw, -(-x2 // b)), min(self.gh, -(-y2 // b))
            if gx2 > gx1 and gy2 > gy1:
                cover[gy1:gy2, gx1:gx2] = True
        return cover

    def changes(self, cover: np.ndarray):
        """What to redraw: (block x, block y, BGRA blocks), or None. Blocks whose
        colour only changed a tiny bit are left as they are."""
        new = np.zeros((self.gh, self.gw, 4), np.uint8)
        new[cover, :3] = (self.colors[cover] * (1.0 - DARKNESS)).astype(np.uint8) if DARKNESS else self.colors[cover]
        new[cover, 3] = 255
        changed = (cover != self.prev) | (cover & (np.abs(new[:, :, :3].astype(np.int16)
                                                          - self.shown[:, :, :3]).max(2) > COLOR_STEP))
        self.prev = cover
        if not changed.any():
            return None
        rows, cols = np.flatnonzero(changed.any(1)), np.flatnonzero(changed.any(0))
        y1, y2, x1, x2 = rows[0], rows[-1] + 1, cols[0], cols[-1] + 1
        self.shown[y1:y2, x1:x2] = new[y1:y2, x1:x2]
        return x1, y1, new[y1:y2, x1:x2]


# --- Overlay window, drawn directly by Windows (fast) ---

gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
                ("hIconSm", wintypes.HICON)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 1)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class UPDATELAYEREDWINDOWINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hdcDst", wintypes.HDC),
                ("pptDst", ctypes.POINTER(wintypes.POINT)), ("psize", ctypes.POINTER(wintypes.SIZE)),
                ("hdcSrc", wintypes.HDC), ("pptSrc", ctypes.POINTER(wintypes.POINT)),
                ("crKey", wintypes.COLORREF), ("pblend", ctypes.POINTER(BLENDFUNCTION)),
                ("dwFlags", wintypes.DWORD), ("prcDirty", ctypes.POINTER(wintypes.RECT))]


user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.RegisterClassExW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.CreateWindowExW.restype = wintypes.HWND
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT,
                                wintypes.UINT, wintypes.UINT]
user32.PeekMessageW.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
                                       ctypes.POINTER(wintypes.SIZE), wintypes.HDC,
                                       ctypes.POINTER(wintypes.POINT), wintypes.COLORREF,
                                       ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD]
user32.UpdateLayeredWindow.restype = wintypes.BOOL
user32.UpdateLayeredWindowIndirect.argtypes = [wintypes.HWND, ctypes.POINTER(UPDATELAYEREDWINDOWINFO)]
user32.UpdateLayeredWindowIndirect.restype = wintypes.BOOL
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetCurrentThread.restype = wintypes.HANDLE
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.SetPriorityClass.restype = wintypes.BOOL
kernel32.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
kernel32.SetThreadPriority.restype = wintypes.BOOL
gdi32.GdiFlush.restype = wintypes.BOOL


class LayeredOverlay:
    """Borderless, see-through, topmost, click-through window over the monitor.
    The mosaic is painted into a memory picture and handed to Windows in one go."""

    def __init__(self, region: dict) -> None:
        self.x, self.y = region["left"], region["top"]
        self.w, self.h = region["width"], region["height"]
        hinst = kernel32.GetModuleHandleW(None)
        self._wndproc = WNDPROC(lambda hwnd, msg, wp, lp: user32.DefWindowProcW(hwnd, msg, wp, lp))
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        wc.lpszClassName = "ScreenPixelatorOverlay"
        user32.RegisterClassExW(ctypes.byref(wc))  # fails harmlessly if already registered
        ex = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | 0x00000008  # TOPMOST
        self.hwnd = user32.CreateWindowExW(ex, "ScreenPixelatorOverlay", "Screen Pixelator",
                                           0x80000000, self.x, self.y, self.w, self.h,  # WS_POPUP
                                           None, None, hinst, None)
        if not self.hwnd:
            raise OSError(f"CreateWindowEx failed ({ctypes.GetLastError()})")

        self.hdc_screen = user32.GetDC(None)
        self.hdc_mem = gdi32.CreateCompatibleDC(self.hdc_screen)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth, bmi.bmiHeader.biHeight = self.w, -self.h  # top-down
        bmi.bmiHeader.biPlanes, bmi.bmiHeader.biBitCount = 1, 32
        bits = ctypes.c_void_p()
        self.hbmp = gdi32.CreateDIBSection(self.hdc_mem, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        if not self.hbmp or not bits.value:
            raise OSError("CreateDIBSection failed")
        self._old_bmp = gdi32.SelectObject(self.hdc_mem, self.hbmp)
        raw = (ctypes.c_uint8 * (self.w * self.h * 4)).from_address(bits.value)
        self.buf = np.frombuffer(raw, np.uint8).reshape(self.h, self.w, 4)
        self.buf[:] = 0

        self._pt_dst = wintypes.POINT(self.x, self.y)
        self._size = wintypes.SIZE(self.w, self.h)
        self._pt_src = wintypes.POINT(0, 0)
        self._blend = BLENDFUNCTION(0, 0, 255, 1)  # AC_SRC_OVER, per-pixel alpha
        self._push(None)
        user32.ShowWindow(self.hwnd, 4)  # SW_SHOWNOACTIVATE

        if user32.SetWindowDisplayAffinity(self.hwnd, WDA_EXCLUDEFROMCAPTURE):
            print("Overlay hidden from screen capture: OK")
        else:
            print("WARNING: could not hide the overlay from screen capture "
                  "(needs Windows 10 version 2004 or newer). Expect flicker.")

    def _push(self, dirty) -> None:
        """Hand the picture to Windows; only the `dirty` rectangle changed."""
        gdi32.GdiFlush()
        if dirty is not None:
            rect = wintypes.RECT(*dirty)
            info = UPDATELAYEREDWINDOWINFO(
                ctypes.sizeof(UPDATELAYEREDWINDOWINFO), self.hdc_screen, ctypes.pointer(self._pt_dst),
                ctypes.pointer(self._size), self.hdc_mem, ctypes.pointer(self._pt_src), 0,
                ctypes.pointer(self._blend), 2, ctypes.pointer(rect))  # ULW_ALPHA
            if user32.UpdateLayeredWindowIndirect(self.hwnd, ctypes.byref(info)):
                return
        user32.UpdateLayeredWindow(self.hwnd, self.hdc_screen, ctypes.byref(self._pt_dst),
                                   ctypes.byref(self._size), self.hdc_mem, ctypes.byref(self._pt_src),
                                   0, ctypes.byref(self._blend), 2)

    def pump(self) -> bool:
        msg = wintypes.MSG()
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):  # PM_REMOVE
            if msg.message == 0x0012:  # WM_QUIT
                return False
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        return True

    def keep_on_top(self) -> None:
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_FLAGS)

    def render(self, change) -> None:
        """change: (block x, block y, BGRA blocks) from MosaicGrid.changes."""
        if change is None:
            return
        gx, gy, blocks = change
        b = PIXEL_BLOCK_SIZE
        x1, y1 = gx * b, gy * b
        x2, y2 = min(self.w, x1 + blocks.shape[1] * b), min(self.h, y1 + blocks.shape[0] * b)
        big = cv2.resize(blocks, (blocks.shape[1] * b, blocks.shape[0] * b), interpolation=cv2.INTER_NEAREST)
        self.buf[y1:y2, x1:x2] = big[:y2 - y1, :x2 - x1]
        self._push((x1, y1, x2, y2))

    def close(self) -> None:
        try:
            gdi32.SelectObject(self.hdc_mem, self._old_bmp)
            gdi32.DeleteObject(self.hbmp)
            gdi32.DeleteDC(self.hdc_mem)
            user32.ReleaseDC(None, self.hdc_screen)
            user32.DestroyWindow(self.hwnd)
        except Exception:
            pass


# --- Overlay window via Tk (slower; used only if the one above fails) ---

class ProgramFilter:
    """Which mosaic blocks lie on a window of one of ONLY_IN_PROGRAMS.
    Windows are painted from the bottom of the stack to the top, so a window
    in front hides the ones behind it."""

    def __init__(self, region: dict, gw: int, gh: int, block: int) -> None:
        self.left, self.top, self.w, self.h = region["left"], region["top"], region["width"], region["height"]
        self.gw, self.gh, self.b = gw, gh, block
        self.allowed = np.ones((gh, gw), bool)
        self.version = 0
        self.any = True
        self.names: dict[int, str] = {}
        self.names_time = 0.0
        self.me = os.getpid()
        self.u = ctypes.WinDLL("user32", use_last_error=True)
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        try:
            self.dwm = ctypes.WinDLL("dwmapi")
        except OSError:
            self.dwm = None
        u, k = self.u, self.k
        self.ENUM = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        u.EnumWindows.argtypes = [self.ENUM, wintypes.LPARAM]
        u.EnumChildWindows.argtypes = [wintypes.HWND, self.ENUM, wintypes.LPARAM]
        u.IsWindowVisible.argtypes = [wintypes.HWND]
        u.IsIconic.argtypes = [wintypes.HWND]
        u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        u.GetWindowLongW.restype = ctypes.c_long
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                 ctypes.POINTER(wintypes.DWORD)]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        if self.dwm:
            self.dwm.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, wintypes.LPVOID,
                                                       wintypes.DWORD]

    def pid_of(self, hwnd) -> int:
        pid = wintypes.DWORD()
        self.u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value

    def exe(self, pid: int) -> str:
        if pid not in self.names:
            name = ""
            h = self.k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if h:
                buf, size = ctypes.create_unicode_buffer(520), wintypes.DWORD(520)
                if self.k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    name = os.path.basename(buf.value).lower()
                self.k.CloseHandle(h)
            self.names[pid] = name
        return self.names[pid]

    def app_of(self, hwnd, pid: int) -> str:
        name = self.exe(pid)
        if name == "applicationframehost.exe":   # store apps: the real app is a child window
            found = []

            def child(c, _):
                p = self.pid_of(c)
                if p != pid:
                    found.append(p)
                    return False
                return True

            self.u.EnumChildWindows(hwnd, self.ENUM(child), 0)
            if found:
                name = self.exe(found[0])
        return name

    def windows(self):
        """Visible windows, top of the stack first: (rectangle on this monitor, program)."""
        found = []

        def each(hwnd, _):
            found.append(hwnd)
            return True

        self.u.EnumWindows(self.ENUM(each), 0)
        out = []
        for hwnd in found:
            if not self.u.IsWindowVisible(hwnd) or self.u.IsIconic(hwnd):
                continue
            ex = self.u.GetWindowLongW(hwnd, -20) & 0xFFFFFFFF
            if ex & (0x20 | 0x80):   # click-through overlays, tool windows (menus, tips)
                continue
            if self.dwm:
                cloaked = wintypes.DWORD()
                self.dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), 4)  # DWMWA_CLOAKED
                if cloaked.value:
                    continue
            pid = self.pid_of(hwnd)
            if pid == self.me:
                continue
            r = wintypes.RECT()
            if not (self.dwm and self.dwm.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(r), 16) == 0):
                self.u.GetWindowRect(hwnd, ctypes.byref(r))      # 9 = DWMWA_EXTENDED_FRAME_BOUNDS
            box = (r.left - self.left, r.top - self.top, r.right - self.left, r.bottom - self.top)
            if box[2] - box[0] < 8 or box[3] - box[1] < 8:
                continue
            out.append((box, self.app_of(hwnd, pid)))
        return out

    def paint(self, wins) -> np.ndarray:
        """Block grid: True where the window in front belongs to an allowed program.
        Allowed windows count generously (partly covered blocks), others only
        with blocks fully inside, so the mosaic rather spills a block over."""
        grid = np.zeros((self.gh, self.gw), bool)
        b = self.b
        for (x1, y1, x2, y2), name in reversed(wins):
            if name in ONLY_IN_PROGRAMS:
                bx1, by1, bx2, by2 = x1 // b, y1 // b, -(-x2 // b), -(-y2 // b)
                value = True
            else:
                bx1, by1, bx2, by2 = -(-x1 // b), -(-y1 // b), x2 // b, y2 // b
                value = False
            bx1, by1 = max(0, bx1), max(0, by1)
            bx2, by2 = min(self.gw, bx2), min(self.gh, by2)
            if bx2 > bx1 and by2 > by1:
                grid[by1:by2, bx1:bx2] = value
        return grid

    def update(self) -> None:
        now = time.monotonic()
        if now - self.names_time > 60:   # process numbers get reused
            self.names, self.names_time = {}, now
        grid = self.paint(self.windows())
        if not np.array_equal(grid, self.allowed):
            self.allowed = grid
            self.version += 1
        self.any = bool(grid.any())


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT),
                ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wintypes.HICON)]


class TrayIcon:
    """Icon next to the clock: click = settings, right-click = menu."""

    CALLBACK = 0x8001             # WM_APP + 1
    SETTINGS, STOP, PICTURE = 1, 2, 3

    def __init__(self) -> None:
        self.u = ctypes.WinDLL("user32", use_last_error=True)
        self.shell = ctypes.WinDLL("shell32", use_last_error=True)
        u = self.u
        u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.DefWindowProcW.restype = LRESULT
        u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                      ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                      wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        u.CreateWindowExW.restype = wintypes.HWND
        u.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int,
                                 ctypes.c_int, wintypes.UINT]
        u.LoadImageW.restype = wintypes.HANDLE
        u.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPVOID]
        u.LoadIconW.restype = wintypes.HICON
        u.CreatePopupMenu.restype = wintypes.HMENU
        u.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
        u.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, wintypes.HWND, wintypes.LPVOID]
        u.DestroyMenu.argtypes = [wintypes.HMENU]
        u.SetForegroundWindow.argtypes = [wintypes.HWND]
        u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        u.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
        u.RegisterWindowMessageW.restype = wintypes.UINT
        u.DestroyWindow.argtypes = [wintypes.HWND]
        u.DestroyIcon.argtypes = [wintypes.HICON]
        self.shell.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
        self.shell.Shell_NotifyIconW.restype = wintypes.BOOL

        self.taskbar_created = u.RegisterWindowMessageW("TaskbarCreated")
        self._wndproc = WNDPROC(self._on_message)
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        wc.lpszClassName = "ScreenPixelatorTray"
        user32.RegisterClassExW(ctypes.byref(wc))
        self.hwnd = u.CreateWindowExW(0x80, "ScreenPixelatorTray", "ScreenPixelator tray", 0x80000000,
                                      0, 0, 0, 0, None, None, hinst, None)
        self.icon = None
        if os.path.exists(ICON_FILE):
            self.icon = u.LoadImageW(None, ICON_FILE, 1, 0, 0, 0x10 | 0x40)  # IMAGE_ICON, from file
        self.own_icon = bool(self.icon)
        if not self.icon:
            self.icon = u.LoadIconW(None, ctypes.c_void_p(32512))  # IDI_APPLICATION
        self.shown = False
        self.picture_at = 0.0     # when to save a picture (asked from the menu)
        self.ensure()

    def _data(self) -> NOTIFYICONDATAW:
        d = NOTIFYICONDATAW()
        d.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        d.hWnd, d.uID = self.hwnd, 1
        d.uFlags = 0x1 | 0x2 | 0x4                     # NIF_MESSAGE | NIF_ICON | NIF_TIP
        d.uCallbackMessage, d.hIcon = self.CALLBACK, self.icon
        d.szTip = f"{APP_NAME} is running"
        return d

    def ensure(self) -> None:
        """Show the icon (again): the taskbar may not be there yet at sign-in."""
        if self.hwnd and not self.shown:
            self.shown = bool(self.shell.Shell_NotifyIconW(0, ctypes.byref(self._data())))  # NIM_ADD

    def _on_message(self, hwnd, msg, wp, lp):
        try:
            if msg == self.taskbar_created:   # Explorer restarted
                self.shown = False
                self.ensure()
            elif msg == self.CALLBACK:
                event = lp & 0xFFFF
                if event == 0x0202:            # left click
                    self.open_settings()
                elif event == 0x0205:          # right click
                    self.menu()
        except Exception:
            traceback.print_exc()
        return self.u.DefWindowProcW(hwnd, msg, wp, lp)

    def menu(self) -> None:
        u = self.u
        m = u.CreatePopupMenu()
        u.AppendMenuW(m, 0, self.SETTINGS, "Settings...")
        u.AppendMenuW(m, 0, self.PICTURE, "Save picture with mosaic")
        if EXIT_HOTKEY:   # with a password, stopping goes through the settings
            u.AppendMenuW(m, 0, self.STOP, "Stop the pixelator")
        pt = wintypes.POINT()
        u.GetCursorPos(ctypes.byref(pt))
        u.SetForegroundWindow(self.hwnd)
        choice = u.TrackPopupMenu(m, 0x0100 | 0x0002, pt.x, pt.y, 0, self.hwnd, None)  # RETURNCMD|RIGHTBUTTON
        u.PostMessageW(self.hwnd, 0, 0, 0)
        u.DestroyMenu(m)
        if choice == self.SETTINGS:
            self.open_settings()
        elif choice == self.PICTURE:   # a moment later, when the menu is gone
            self.picture_at = time.monotonic() + 0.5
        elif choice == self.STOP:
            open(STOP_FILE, "w").close()

    def balloon(self, title: str, text: str) -> None:
        """A short notification from the tray icon."""
        if not self.shown:
            return
        d = self._data()
        d.uFlags = 0x10                 # NIF_INFO
        d.szInfoTitle, d.szInfo = title[:63], text[:255]
        d.dwInfoFlags = 0x1             # NIIF_INFO
        self.shell.Shell_NotifyIconW(1, ctypes.byref(d))   # NIM_MODIFY

    @staticmethod
    def open_settings() -> None:
        import subprocess
        pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        subprocess.Popen([pyw if os.path.exists(pyw) else sys.executable, SETTINGS_APP], cwd=HERE,
                         creationflags=0x00000008 | 0x00000200, close_fds=True)

    def close(self) -> None:
        if self.shown:
            self.shell.Shell_NotifyIconW(2, ctypes.byref(self._data()))  # NIM_DELETE
            self.shown = False
        if self.own_icon:
            self.u.DestroyIcon(self.icon)
        if self.hwnd:
            self.u.DestroyWindow(self.hwnd)
            self.hwnd = None


class TkOverlay:
    def __init__(self, region: dict) -> None:
        import tkinter as tk
        self.tk = tk
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.geometry(f"{region['width']}x{region['height']}+{region['left']}+{region['top']}")
        self.root.configure(bg=KEY_COLOR)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", KEY_COLOR)
        self.canvas = tk.Canvas(self.root, bg=KEY_COLOR, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.root.update()
        self.hwnd = user32.GetAncestor(self.root.winfo_id(), GA_ROOT)
        style = user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(self.hwnd, GWL_EXSTYLE,
                              style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        if user32.SetWindowDisplayAffinity(self.hwnd, WDA_EXCLUDEFROMCAPTURE):
            print("Overlay hidden from screen capture: OK")
        else:
            print("WARNING: could not hide the overlay from screen capture. Expect flicker.")
        self._images: list = []
        self._alive = True

    def pump(self) -> bool:
        try:
            self.root.update()
            return self._alive
        except Exception:
            return False

    def keep_on_top(self) -> None:
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_FLAGS)

    def render(self, change) -> None:
        if change is None:
            return
        import base64
        gx, gy, blocks = change
        self.canvas.delete("patch")   # the changed area holds everything drawn before
        self._images.clear()
        bgr = blocks[:, :, :3].copy()
        bgr[np.all(bgr == KEY_BGR, axis=2)] = (254, 0, 254)
        bgr[blocks[:, :, 3] == 0] = KEY_BGR
        ok, png = cv2.imencode(".png", bgr)
        if ok and (blocks[:, :, 3] > 0).any():
            b = PIXEL_BLOCK_SIZE
            img = self.tk.PhotoImage(data=base64.b64encode(png.tobytes())).zoom(b, b)
            self.canvas.create_image(gx * b, gy * b, image=img, anchor="nw", tags="patch")
            self._images.append(img)

    def close(self) -> None:
        try:
            self.root.destroy()
        except Exception:
            pass


def overlay_hidden_from_capture(overlay, source: FrameSource) -> bool:
    """Flash a test pattern in the overlay and check the captured pictures
    don't contain it (otherwise the mosaic would follow itself)."""
    # Checked once per Windows version: the test patch would otherwise flash
    # at every start
    build = sys.getwindowsversion().build if hasattr(sys, "getwindowsversion") else 0
    try:
        with open(CHECK_FILE) as f:
            known = json.load(f)
        if known.get("build") == build and "hidden" in known:
            return bool(known["hidden"])
    except Exception:
        pass
    x1, y1, x2, y2 = 4, 4, 20, 20     # small, in the corner
    pattern = np.zeros((y2 - y1, x2 - x1, 4), np.uint8)
    pattern[:, :, 3] = 255
    pattern[::2, :, 2] = 255     # red / blue stripes, unlike anything on screen
    pattern[1::2, :, 0] = 255
    overlay.buf[y1:y2, x1:x2] = pattern
    overlay._push((x1, y1, x2, y2))
    end = time.monotonic() + 0.4
    while time.monotonic() < end:
        overlay.pump()
        time.sleep(0.02)
    _, frame, _ = source.latest()
    seen = None if frame is None else frame[y1:y2, x1:x2, :3].astype(int)
    overlay.buf[y1:y2, x1:x2] = 0
    overlay._push((x1, y1, x2, y2))
    if seen is None:
        return True              # no picture yet: nothing seen, check again next time
    hidden = bool(np.abs(seen - pattern[:, :, :3].astype(int)).mean() > 40)
    try:
        with open(CHECK_FILE, "w") as f:
            json.dump({"build": build, "hidden": hidden}, f)
    except Exception:
        pass
    return hidden


def other_copy_running() -> bool:
    """Is the pixelator already running (started at sign-in, say)?"""
    try:
        with open(PID_FILE) as f:
            pid = int(f.read().strip())
    except Exception:
        return False
    if pid == os.getpid():
        return False
    try:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code, name, size = wintypes.DWORD(), ctypes.create_unicode_buffer(520), wintypes.DWORD(520)
            k.GetExitCodeProcess(wintypes.HANDLE(h), ctypes.byref(code))
            k.QueryFullProcessImageNameW(wintypes.HANDLE(h), 0, name, ctypes.byref(size))
            return code.value == 259 and "python" in name.value.lower()  # STILL_ACTIVE
        finally:
            k.CloseHandle(wintypes.HANDLE(h))
    except Exception:
        return False


def main() -> None:
    if other_copy_running():
        print("The pixelator is already running.")
        return
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 2_000_000:
            open(LOG_FILE, "w").close()
    except Exception:
        pass
    for f in (STOP_FILE,):
        try:
            os.remove(f)
        except OSError:
            pass
    try:
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass
    try:
        run()
    finally:
        try:
            with open(PID_FILE) as f:
                mine = f.read().strip() == str(os.getpid())
            if mine:
                os.remove(PID_FILE)
        except Exception:
            pass
        for f in (STOP_FILE, STATUS_FILE):
            try:
                os.remove(f)
            except OSError:
                pass


def write_status(info: dict) -> None:
    try:
        tmp = STATUS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(info, f)
        os.replace(tmp, STATUS_FILE)
    except Exception:
        pass


def stop_requested() -> bool:
    return (EXIT_HOTKEY and exit_hotkey_pressed()) or os.path.exists(STOP_FILE)


def run() -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Starting screen pixelator...")
    started = time.time()
    make_dpi_aware()
    precise_timers(True)
    cv2.setNumThreads(1)  # this process splits its own work over threads

    cpus = os.cpu_count() or 4
    print(f"{cpus} CPU threads; onnxruntime {ort.__version__} "
          f"with {', '.join(ort.get_available_providers())}")
    if COVER_FEMALE_BODIES or COVER_MALE_BODIES:
        print("Whole bodies: " + ("ON" if os.path.exists(person_model_path())
                                         else f"OFF ({PERSON_MODEL} not found next to this script)"))

    sct = new_mss()
    region = dict(sct.monitors[MONITOR_INDEX])
    sct.close()
    w, h = region["width"], region["height"]

    # Shared screen pictures, written by the capture process
    from multiprocessing import shared_memory
    shm = shared_memory.SharedMemory(create=True, size=FrameSource.BUFFERS * w * h * 4)
    small_shm = shared_memory.SharedMemory(create=True, size=FrameSource.small_size(w, h))
    counter = mp.Value("q", 0, lock=False)
    times = mp.Array("d", FrameSource.BUFFERS, lock=False)
    scroll = mp.Array("d", 2 * FrameSource.BUFFERS, lock=False)
    shm_args = (shm.name, w, h, counter, times, scroll, small_shm.name)
    source = FrameSource(*shm_args)

    stop, use_mss, info_q, pause = mp.Event(), mp.Event(), mp.Queue(), mp.Event()
    out_q, zones_q = mp.Queue(maxsize=16), mp.Queue(maxsize=2)
    procs = [mp.Process(target=capture_main, args=(region, shm_args, use_mss, stop, info_q), daemon=True),
             mp.Process(target=scanner_main, args=("full", region, shm_args, out_q, zones_q, stop, pause),
                        daemon=True)]
    if FOCUS_SCAN:
        procs.append(mp.Process(target=scanner_main, args=("focus", region, shm_args, out_q, zones_q, stop, pause),
                                daemon=True))
    for p in procs:
        p.start()

    try:
        overlay = LayeredOverlay(region)
        print("Drawing: direct (fast).")
    except Exception as e:
        print(f"Direct drawing not available ({e}); using Tk (slower).")
        overlay = TkOverlay(region)

    # Which capture method, and is the overlay really kept out of it?
    method = "mss"
    end = time.monotonic() + 15
    while time.monotonic() < end:
        try:
            msg = info_q.get(timeout=0.5)
        except queue.Empty:
            continue
        if isinstance(msg, tuple):
            method = msg[1]
            break
        print(msg)
    source.wait_new(0, stop, timeout=3)
    if method == "dxcam" and isinstance(overlay, LayeredOverlay) \
            and not overlay_hidden_from_capture(overlay, source):
        print("dxcam can see the overlay; using mss instead.")
        use_mss.set()
        method = "mss"
    print(f"Screen capture: {method}. Monitor {MONITOR_INDEX}: {w}x{h} px.")

    running = {"on": True}
    signal.signal(signal.SIGINT, lambda *_: running.update(on=False))
    try:  # the redraws go before everything else when the processor is busy
        kernel32.SetThreadPriority(kernel32.GetCurrentThread(), 2)  # THREAD_PRIORITY_HIGHEST
    except Exception:
        pass
    tracker = Tracker(w, h)
    grid = MosaicGrid(w, h)
    program_filter = None
    if ONLY_IN_PROGRAMS:
        try:
            program_filter = ProgramFilter(region, grid.gw, grid.gh, PIXEL_BLOCK_SIZE)
            print(f"Only covering inside: {', '.join(sorted(ONLY_IN_PROGRAMS))}")
        except Exception as e:
            print(f"Program filter not available ({e}); covering everywhere.")
    test_box = (w // 2 - 150, h // 2 - 150, w // 2 + 150, h // 2 + 150)
    test_until = time.monotonic() + SELF_TEST_SECONDS if SELF_TEST_SECONDS else 0.0
    stats = {"redraws": 0, "capture": 0.0, "follow": 0.0, "draw": 0.0, "full": [], "share": [],
             "focus": 0, "next": time.monotonic() + 2, "since": time.monotonic(), "round": 0,
             "sent": 0.0, "frames": counter.value, "filter_next": 0.0,
             "seen": 0, "key": None}

    def redraw(now: float) -> None:
        while True:  # take the scan results that came in
            try:
                kind, specs, t0, took, share = out_q.get_nowait()
            except queue.Empty:
                break
            tracker.add_detections(specs, now, t0, kind)
            if kind == "full":
                stats["full"].append(took)
                stats["share"].append(share)
            elif kind == "focus":
                stats["focus"] += 1
        tracker.expire(now)

        testing = now < test_until
        t_a = time.monotonic()
        n, _, t_frame = source.latest()
        new_picture = bool(n) and n != stats["seen"]
        if new_picture:  # follow the movement
            i = n % FrameSource.BUFFERS
            gray, colors = source.quarter[i].copy(), source.colors[i].copy()
            t_b = time.monotonic()
            tracker.update(gray, t_frame, source.scrolled(n))
            grid.colors[:] = colors
            stats["seen"] = n
        else:
            t_b = t_a
        t_c = time.monotonic()
        items = []
        if (tracker.zones or testing) and n:
            # Draw ahead by the time since the picture was taken, plus a little.
            # No new picture for a while means the screen stands still.
            gap = now - t_frame
            ahead = gap + PREDICT_SECONDS if gap < STILL_AFTER else 0.0
            items = [(tracker.draw_box(z, ahead), z.mask, z.kind, z.edges, z.pad) for z in tracker.zones]
            if testing:
                items.append((test_box, None, "part", "", 0.0))
        if program_filter and now >= stats["filter_next"]:
            stats["filter_next"] = now + 0.25
            try:
                program_filter.update()
            except Exception:
                traceback.print_exc()
            if program_filter.any:
                pause.clear()
            else:
                pause.set()   # none of the programs on screen: the scanners rest
        key = [(b, id(m), k, e) for b, m, k, e, _ in items]
        if program_filter:
            key.append(program_filter.version)
        if new_picture or key != stats["key"]:  # otherwise the window already shows this
            stats["key"] = key
            cover = grid.coverage(items) if items else grid.empty
            if program_filter:
                cover = cover & program_filter.allowed
            overlay.render(grid.changes(cover))
        t_d = time.monotonic()

        if now - stats["sent"] > 0.1:  # tell the focus scanner where the zones are
            stats["sent"] = now
            try:
                zones_q.put_nowait([z.box for z in tracker.zones])
            except queue.Full:
                pass

        stats["redraws"] += 1
        stats["capture"] += t_b - t_a
        stats["follow"] += t_c - t_b
        stats["draw"] += t_d - t_c
        if now >= stats["next"]:
            span = max(0.5, now - stats["since"])
            times_, share = stats["full"], stats["share"]
            avg = f"{sum(times_) / len(times_):.2f} s" if times_ else "-"
            part = f"{100 * sum(share) / len(share):.0f}%" if share else "-"
            n_ = max(1, stats["redraws"])
            fps = (counter.value - stats["frames"]) / span
            bodies = sum(z.kind == "body" for z in tracker.zones)
            write_status({
                "time": time.time(), "started": started, "screen_fps": round(fps, 1),
                "redraws": round(n_ / span, 1), "focus": round(stats["focus"] / span, 1),
                "full_scan": round(sum(times_) / len(times_), 2) if times_ else None,
                "zones": sum(z.kind == "part" for z in tracker.zones), "bodies": bodies,
                "openings": sum(z.kind == "show" for z in tracker.zones),
                "covered": round(100 * float(grid.prev.mean()), 1), "found": tracker.found,
                "cpu": CPU_NAMES[CPU_LEVEL] + (" (auto)" if CPU_AUTO else "")})
            stats["round"] += 1
            if SHOW_SPEED and stats["round"] % 5 == 0:
                print(f"[{time.strftime('%H:%M:%S')}] screen {fps:.0f}/s, full scan {avg} ({part} in detail), "
                      f"focus {stats['focus'] / span:.0f}/s, redraws {n_ / span:.0f}/s "
                      f"(copy {1000 * stats['capture'] / n_:.0f} + follow {1000 * stats['follow'] / n_:.0f}"
                      f" + draw {1000 * stats['draw'] / n_:.0f} ms), "
                      f"zones {len(tracker.zones)} ({bodies} bodies)")
            stats.update(redraws=0, capture=0.0, follow=0.0, draw=0.0, full=[], share=[], focus=0,
                         next=now + 2, since=now, frames=counter.value)

    tray = None
    if TRAY_ICON:
        try:
            tray = TrayIcon()
        except Exception as e:
            print(f"No tray icon ({e})")
    print(f"Processor use: {CPU_NAMES[CPU_LEVEL]}" + (" (auto)" if CPU_AUTO else ""))
    print("Pixelator running. Stop " + ("with Ctrl+Alt+Q, or Ctrl+C here." if EXIT_HOTKEY
                                        else "from the settings program (password set)."))
    period = REFRESH_MS / 1000
    ticks = 0
    try:
        while running["on"] and overlay.pump() and not (ticks % 10 == 0 and stop_requested()):
            redraw(time.monotonic())
            if ticks % 5 == 0:   # Ctrl+Alt+P or the tray menu: picture with the mosaic
                held = picture_hotkey_pressed()
                asked = bool(tray and tray.picture_at and time.monotonic() >= tray.picture_at)
                if asked:
                    tray.picture_at = 0.0
                if (held and not stats.get("picture_key")) or asked:
                    n, frame, _ = source.latest()
                    if frame is not None:
                        try:
                            path = save_picture(frame.copy(), grid.shown.copy(), PIXEL_BLOCK_SIZE)
                            print(f"Picture saved: {path}")
                            if tray:
                                tray.balloon(APP_NAME, "Picture saved in Pictures\\ScreenPixelator")
                        except Exception:
                            traceback.print_exc()
                stats["picture_key"] = held
            if ticks % 50 == 0:
                overlay.keep_on_top()
                if tray:
                    tray.ensure()
            ticks += 1
            # Wait for the next screen picture, but redraw at least every REFRESH_MS
            last, end = counter.value, time.monotonic() + period
            while counter.value == last and time.monotonic() < end:
                time.sleep(0.001)
    finally:
        stop.set()
        if tray:
            tray.close()
        overlay.close()
        precise_timers(False)
        for p in procs:
            p.join(timeout=2)
            if p.is_alive():
                p.terminate()
        source.close()
        for s in (shm, small_shm):
            try:
                s.close()
                s.unlink()
            except Exception:
                pass


if __name__ == "__main__":
    mp.freeze_support()
    main()
