"""Screen Pixelator - settings program.

Writes settings.json next to screen_pixelator.py, starts/stops the pixelator,
shows what it is doing, manages "start with Windows" and an optional password.
"""

import ctypes
import hashlib
import json
import math
import os
import secrets
import subprocess
import sys
import time
import tkinter as tk
from collections import deque
from tkinter import messagebox, simpledialog, ttk

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "screen_pixelator.py")
SETTINGS_FILE = os.path.join(HERE, "settings.json")
STOP_FILE = os.path.join(HERE, "stop.request")
PID_FILE = os.path.join(HERE, "running.pid")
STATUS_FILE = os.path.join(HERE, "status.json")
LOG_FILE = os.path.join(HERE, "pixelator.log")
ICON_FILE = os.path.join(HERE, "pixelator.ico")
APP_VERSION = "1.1"
APP_NAME = "ScreenPixelator " + APP_VERSION
GITHUB_REPO = "screenpixelator/ScreenPixelator"     # where new versions are published
CONTACT = "screenpixelator@proton.me"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\ScreenPixelator"

# Organisations helping victims of sexual violence: (name, what they do, donation page)
CHARITIES = [
    ("Mukwege Foundation", "Care and support for survivors of sexual violence in war zones, worldwide",
     "https://www.mukwegefoundation.org/donate/"),
    ("SOS Viol (Brussels)", "Help line and support for rape victims in Belgium",
     "https://www.sosviol.be"),
    ("Child Focus (Belgium)", "Helps sexually exploited and missing children",
     "https://childfocus.be"),
]

# Donations to the creator: fill in one or both (leave "" when not used)
DONATE_URL = ""          # e.g. a Ko-fi, PayPal.me or Buy Me a Coffee page
BITCOIN_ADDRESS = ""     # e.g. bc1q...
WINDOWS = sys.platform == "win32"
NO_WINDOW = 0x08000000 if WINDOWS else 0               # CREATE_NO_WINDOW
DETACHED = 0x00000008 | 0x00000200 if WINDOWS else 0   # DETACHED_PROCESS | NEW_PROCESS_GROUP

# Colours
BG, CARD, FIELD, LINE = "#16171f", "#222431", "#2e3142", "#3b3f55"
TEXT, MUTED, ACCENT, ACCENT2 = "#eceef6", "#9a9fb5", "#a78bfa", "#7c5cf0"
GREEN, RED, AMBER = "#34d399", "#f87171", "#fbbf24"


def pythonw() -> str:
    p = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return p if os.path.exists(p) else sys.executable


def special_folder(csidl: int, fallback: str) -> str:
    try:
        buf = ctypes.create_unicode_buffer(260)
        ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf)
        if buf.value:
            return buf.value
    except Exception:
        pass
    return fallback


APPDATA = os.environ.get("APPDATA", HERE)
PROGRAMS = os.path.join(APPDATA, r"Microsoft\Windows\Start Menu\Programs")
STARTUP_LNK = os.path.join(special_folder(0x07, os.path.join(PROGRAMS, "Startup")), "Screen Pixelator.lnk")
MENU_LNK = os.path.join(special_folder(0x02, PROGRAMS), "Screen Pixelator Settings.lnk")
DESKTOP_LNK = os.path.join(special_folder(0x10, os.path.join(os.path.expanduser("~"), "Desktop")),
                           "Screen Pixelator Settings.lnk")

COVER_KEYS = ("cover_breasts", "cover_female_genitals", "also_clothed", "cover_male_genitals",
              "cover_buttocks", "cover_women", "cover_men", "cover_unclear")
COVER_LABELS = {
    "cover_women": "Whole women", "cover_men": "Whole men",
    "cover_unclear": "Unclear if man or woman, partly naked",
    "cover_breasts": "Women's breasts", "cover_female_genitals": "Women's genitals",
    "also_clothed": "...also when covered by clothing",
    "cover_male_genitals": "Men's genitals", "cover_buttocks": "Buttocks",
}
def _preset(cover, women="show", men="show", darkness=0, parts=80, people=65, margin=20, hold=0.8,
            block=15):
    """cover: breasts, women's genitals, also clothed, men's genitals, buttocks,
    whole women, whole men, unclear people (1/0)."""
    d = {k: bool(v) for k, v in zip(COVER_KEYS, cover)}
    d.update(faces_women=women, faces_men=men, darkness=darkness, catch_parts=parts,
             catch_people=people, zone_size=margin, hold=hold,
             block_size=block)
    return d


PRESETS = {
    "Hide nudity only": _preset((1, 1, 0, 1, 1, 0, 0, 0)),
    "Women covered, men visible": _preset((1, 1, 1, 0, 1, 1, 0, 1), parts=75, people=60),
    "Men covered, women visible": _preset((0, 0, 0, 1, 1, 0, 1, 0), parts=75, people=60),
    "Everyone covered": _preset((1, 1, 1, 1, 1, 1, 1, 1), parts=75, people=60),
    "Women fully covered (eyes only)": _preset((1, 1, 1, 0, 1, 1, 0, 1), women="eyes", darkness=85,
                                               parts=85, people=75, margin=10, hold=1.2,
                                               block=6),
    "Anonymous (eyes hidden)": _preset((0, 0, 0, 0, 0, 0, 0, 0), women="noeyes", men="noeyes"),
}
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

FACE_CHOICES = (("show", "Show"), ("hide", "Hide"), ("eyes", "Show only the eyes"),
                ("noeyes", "Hide only the eyes"))

# key, label, from, to, default, unit, hint
STRENGTH = [
    ("catch_parts", "Catch more nudity", 0, 100, 80, "", "higher = finds more, also more mistakes"),
    ("catch_people", "Catch more people", 0, 100, 65, "", "for covering whole bodies"),
    ("zone_size", "Margin around body parts", 0, 200, 20, "%", "breasts, buttocks (genitals at least 50%)"),
    ("body_margin", "Margin around bodies", 0, 6, 1, " blocks", "extra blocks around a body outline"),
    ("hold", "Stay covered after losing it", 0.2, 5.0, 0.8, " s", "longer = fewer flashes, slower to clear"),
    ("male_opening", "Opening around men's genitals", 0, 150, 30, "%", "bigger = more of him visible"),
    ("face_opening", "Opening around faces", 0, 100, 15, "%", "bigger = more of the head visible"),
    ("lean_male", "When unsure: lean to 'man'", 0, 100, 60, "", "higher = men are uncovered sooner"),
]
LOOK = [
    ("block_size", "Block size", 6, 48, 15, " px", ""),
    ("darkness", "Block colour", 0, 100, 0, "%", ""),
]
DEFAULTS = {"faces_women": "show", "faces_men": "show", "run_at_startup": False, "cpu_level": "auto",
            "check_updates": True,
            "start_hidden": False, "only_in_programs": True, "programs": list(DEFAULT_PROGRAMS)}
DEFAULTS.update({k: d for k, _, _, _, d, _, _ in STRENGTH + LOOK})
DEFAULTS.update(PRESETS["Hide nudity only"])      # new installs start with this choice


# --- Files and processes ---

def load() -> dict:
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        if "faces" in saved and "faces_women" not in saved:   # older settings
            saved["faces_women"] = {"hide_women": "hide", "women_eyes": "eyes"}.get(saved["faces"], "show")
        saved.pop("faces", None)
        if saved.get("style") == "black" and "darkness" not in saved:
            saved["darkness"] = 100
        saved.pop("style", None)
        s.update(saved)
    except Exception:
        pass
    return s


def save(s: dict) -> None:
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)
    os.replace(tmp, SETTINGS_FILE)


def hash_password(pw: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), 200_000).hex()


def running_pid():
    """PID of the running pixelator, or None."""
    try:
        with open(PID_FILE) as f:
            pid = int(f.read().strip())
    except Exception:
        return None
    if not WINDOWS:
        return None
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    h = k.OpenProcess(0x1000, False, pid)
    if not h:
        return None
    try:
        code, name, size = wintypes.DWORD(), ctypes.create_unicode_buffer(520), wintypes.DWORD(520)
        k.GetExitCodeProcess(wintypes.HANDLE(h), ctypes.byref(code))
        k.QueryFullProcessImageNameW(wintypes.HANDLE(h), 0, name, ctypes.byref(size))
        return pid if code.value == 259 and "python" in name.value.lower() else None
    finally:
        k.CloseHandle(wintypes.HANDLE(h))


def start_pixelator() -> None:
    if running_pid():
        return
    subprocess.Popen([pythonw(), SCRIPT], cwd=HERE, creationflags=DETACHED, close_fds=True)


def stop_pixelator() -> bool:
    pid = running_pid()
    if not pid:
        return True
    open(STOP_FILE, "w").close()
    end = time.monotonic() + 8
    while time.monotonic() < end:
        if not running_pid():
            return True
        time.sleep(0.2)
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], creationflags=NO_WINDOW,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    for f in (STOP_FILE, PID_FILE, STATUS_FILE):
        try:
            os.remove(f)
        except OSError:
            pass
    return not running_pid()


def make_shortcut(path: str, target: str, arguments: str) -> None:
    q = lambda t: t.replace("'", "''")
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{q(path)}');"
          f"$s.TargetPath='{q(target)}';$s.Arguments='{q(arguments)}';"
          f"$s.WorkingDirectory='{q(HERE)}';$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                   creationflags=NO_WINDOW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def set_startup(on: bool) -> None:
    if on:
        make_shortcut(STARTUP_LNK, pythonw(), f'"{SCRIPT}"')
    else:
        try:
            os.remove(STARTUP_LNK)
        except OSError:
            pass


def remove_program() -> None:
    """Stop, remove shortcuts, the Windows "Installed apps" entry and the folder."""
    stop_pixelator()
    for p in (STARTUP_LNK, MENU_LNK, DESKTOP_LNK):
        try:
            os.remove(p)
        except OSError:
            pass
    if WINDOWS:
        try:
            import winreg
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
        except OSError:
            pass
        subprocess.Popen(f'cmd /c timeout /t 2 /nobreak >nul & rmdir /s /q "{HERE}"',
                         creationflags=DETACHED | NO_WINDOW, close_fds=True)


def read_status():
    try:
        with open(STATUS_FILE) as f:
            st = json.load(f)
        return st if time.time() - st.get("time", 0) < 8 else None
    except Exception:
        return None


# --- Look ---

def setup_style(root: tk.Tk) -> None:
    root.configure(bg=BG)
    st = ttk.Style(root)
    st.theme_use("clam")
    base = ("Segoe UI", 10)
    st.configure(".", background=CARD, foreground=TEXT, font=base, bordercolor=LINE,
                 lightcolor=CARD, darkcolor=CARD, troughcolor=FIELD, focuscolor=CARD)
    st.configure("Bg.TFrame", background=BG)
    st.configure("Card.TFrame", background=CARD)
    st.configure("Bg.TLabel", background=BG)
    st.configure("Title.TLabel", background=BG, font=("Segoe UI Semibold", 20))
    st.configure("Sub.TLabel", background=BG, foreground=MUTED)
    st.configure("Card.TLabel", background=CARD)
    st.configure("Head.TLabel", background=CARD, foreground=ACCENT, font=("Segoe UI Semibold", 12))
    st.configure("Hint.TLabel", background=CARD, foreground=MUTED, font=("Segoe UI", 8))
    st.configure("Value.TLabel", background=CARD, foreground=ACCENT, font=("Segoe UI Semibold", 10))
    st.configure("Big.TLabel", background=CARD, foreground=TEXT, font=("Segoe UI Semibold", 18))
    st.configure("Warn.TLabel", background=CARD, foreground=AMBER, font=("Segoe UI", 8))
    st.configure("Unsaved.TLabel", background=BG, foreground=AMBER)
    for w in ("TCheckbutton", "TRadiobutton"):
        st.configure(w, background=CARD, foreground=TEXT, indicatorbackground=FIELD,
                     indicatorforeground=TEXT, indicatorrelief="flat", padding=2)
        st.map(w, background=[("active", CARD)], foreground=[("disabled", MUTED)],
               indicatorbackground=[("selected", ACCENT2), ("active", LINE)])
    st.configure("Horizontal.TScale", background=ACCENT, troughcolor=FIELD, bordercolor=CARD,
                 lightcolor=ACCENT, darkcolor=ACCENT2, gripcount=0, sliderlength=16)
    st.map("Horizontal.TScale", background=[("active", "#c4b5fd")])
    st.configure("TButton", background=FIELD, foreground=TEXT, bordercolor=LINE, lightcolor=FIELD,
                 darkcolor=FIELD, padding=(12, 5), relief="flat")
    st.map("TButton", background=[("active", LINE), ("pressed", LINE)])
    st.configure("Accent.TButton", background=ACCENT2, lightcolor=ACCENT2, darkcolor=ACCENT2,
                 font=("Segoe UI Semibold", 10))
    st.map("Accent.TButton", background=[("active", ACCENT), ("pressed", ACCENT)])
    st.configure("TCombobox", fieldbackground=FIELD, background=FIELD, foreground=TEXT,
                 arrowcolor=TEXT, bordercolor=LINE, lightcolor=FIELD, darkcolor=FIELD)
    st.map("TCombobox", fieldbackground=[("readonly", FIELD)], foreground=[("readonly", TEXT)],
           selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", TEXT)])
    root.option_add("*TCombobox*Listbox.background", FIELD)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT2)


class Preview:
    """A small animated scene that shows the mosaic with the current settings."""

    W, H = 132, 84

    def __init__(self, parent, scale: float) -> None:
        self.zoom = max(1, round(2 * scale))
        self.small = tk.PhotoImage(width=self.W, height=self.H)
        self.big = tk.PhotoImage(width=self.W * self.zoom, height=self.H * self.zoom)
        self.canvas = tk.Canvas(parent, width=self.W * self.zoom, height=self.H * self.zoom,
                                bg=CARD, highlightthickness=0)
        self.canvas.create_image(0, 0, image=self.big, anchor="nw")
        self.yy, self.xx = np.mgrid[0:self.H, 0:self.W].astype(np.float32)
        self.hex = np.array([f"{i:02x}" for i in range(256)])
        self.t0 = time.monotonic()

    def draw(self, block: int, darkness: float, faces: str, cover: bool) -> None:
        t = time.monotonic() - self.t0
        W, H, xx, yy = self.W, self.H, self.xx, self.yy
        f = (yy / H)[..., None]
        top = np.array([60 + 30 * math.sin(t * 0.3), 40, 120 + 40 * math.sin(t * 0.2)])
        bottom = np.array([250, 150 + 40 * math.sin(t * 0.25), 110])
        img = top * (1 - f) + bottom * f
        for i, (col, r) in enumerate((((255, 220, 90), 9), ((90, 200, 255), 6), ((255, 120, 170), 5))):
            cx = W * (0.5 + 0.42 * math.sin(t * (0.5 + 0.23 * i) + i * 2))
            cy = H * (0.35 + 0.22 * math.cos(t * (0.7 + 0.31 * i) + i))
            img[(xx - cx) ** 2 + (yy - cy) ** 2 < r * r] = col
        hills = yy > H * 0.78 + 3 * np.sin(xx / 9 + t * 0.8)
        img[hills] = (40, 150, 110)
        # A person walking to and fro
        px = W * (0.5 + 0.3 * math.sin(t * 0.6))
        head = (xx - px) ** 2 + (yy - H * 0.33) ** 2 < 36
        body = ((xx - px) / 9) ** 2 + ((yy - H * 0.62) / 15) ** 2 < 1
        img[body] = (235, 185, 150)
        img[head] = (240, 195, 160)
        eyes = (np.abs(yy - (H * 0.31)) < 1) & ((np.abs(xx - px - 2) < 1) | (np.abs(xx - px + 2) < 1))
        img[eyes] = (40, 40, 60)
        img = img.clip(0, 255)
        # The mosaic: blocks on one grid, like on the real screen
        b = max(1, round(block / 6))
        zones = []
        if cover:
            zones.append((px - 11, H * 0.40, px + 11, H * 0.86))
        if faces in ("hide", "eyes"):
            zones.append((px - 8, H * 0.22, px + 8, H * 0.42))
        if faces == "noeyes":
            zones.append((px - 7, H * 0.27, px + 7, H * 0.36))
        out = img.copy()
        for x1, y1, x2, y2 in zones:
            for by in range(int(y1 // b) * b, int(math.ceil(y2 / b)) * b, b):
                for bx in range(int(x1 // b) * b, int(math.ceil(x2 / b)) * b, b):
                    ya, xa = max(0, by), max(0, bx)
                    yb, xb = min(H, by + b), min(W, bx + b)
                    if yb > ya and xb > xa:
                        out[ya:yb, xa:xb] = img[ya:yb, xa:xb].mean((0, 1)) * (1 - darkness)
        if faces == "eyes":   # the eye band stays clear
            band = (np.abs(yy - H * 0.31) < 2.5) & (np.abs(xx - px) < 7)
            out[band] = img[band]
        px8 = out.astype(np.uint8)
        h = self.hex
        cells = np.char.add(np.char.add(np.char.add("#", h[px8[..., 0]]), h[px8[..., 1]]), h[px8[..., 2]])
        self.small.put(" ".join("{" + " ".join(row) + "}" for row in cells.tolist()))
        self.big.tk.call(self.big, "copy", self.small, "-zoom", self.zoom, self.zoom)


# --- Window ---

class App:
    def __init__(self, root: tk.Tk, s: dict) -> None:
        self.root, self.s = root, s
        self.new_password = None          # (salt, hash), "" to remove, None = unchanged
        self.busy = None                  # text while starting/stopping in the background
        self.busy_result = None
        self.apply_later = False          # a quick choice made while busy
        self.last_seen, self.changed_at = None, 0.0   # for applying changes by themselves
        self.scale = root.winfo_fpixels("1i") / 96
        self.history = deque([(0.0, 0)] * 120, maxlen=120)
        self.pulse = 0.0
        root.title(APP_NAME)
        root.resizable(False, False)
        S = self.scale

        outer = ttk.Frame(root, style="Bg.TFrame", padding=14)
        outer.grid()

        # Header
        head = ttk.Frame(outer, style="Bg.TFrame")
        head.grid(row=0, column=0, columnspan=4, sticky="ew", pady=(0, 10))
        head.columnconfigure(1, weight=1)
        ttk.Label(head, text=APP_NAME, style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(head, text="keeps your screen clean, live  \u00b7  " + CONTACT, style="Sub.TLabel").grid(
            row=1, column=0, sticky="w")
        right = ttk.Frame(head, style="Bg.TFrame")
        right.grid(row=0, column=2, rowspan=2, sticky="e")
        self.dot = tk.Canvas(right, width=int(22 * S), height=int(22 * S), bg=BG, highlightthickness=0)
        self.dot.grid(row=0, column=0, padx=(0, 6))
        self.status = ttk.Label(right, text="", style="Bg.TLabel", font=("Segoe UI Semibold", 12), width=12)
        self.status.grid(row=0, column=1, padx=(0, 12))
        ttk.Button(right, text="Start", command=self.start).grid(row=0, column=2, padx=3)
        ttk.Button(right, text="Stop", command=self.stop).grid(row=0, column=3, padx=3)
        self.update_button = ttk.Button(right, text="", style="Accent.TButton")
        self.hotkey = ttk.Label(right, text="", style="Sub.TLabel", justify="right")
        self.hotkey.grid(row=1, column=0, columnspan=4, sticky="e", pady=(4, 0))

        cards = []
        for c in range(4):
            f = ttk.Frame(outer, style="Card.TFrame", padding=12)
            f.grid(row=1, column=c, sticky="nsew", padx=6)
            cards.append(f)

        # 1. What to cover + faces
        cv = cards[0]
        ttk.Label(cv, text="What to cover", style="Head.TLabel").grid(row=0, column=0, sticky="w")
        self.preset = ttk.Combobox(cv, state="readonly", width=30, values=list(PRESETS))
        self.preset.grid(row=1, column=0, sticky="w", pady=(6, 8))
        self.preset.bind("<<ComboboxSelected>>", self.use_preset)
        self.v = {k: tk.BooleanVar(value=bool(s[k])) for k in COVER_KEYS}
        order = ("cover_women", "cover_men", "cover_unclear", "cover_breasts", "cover_female_genitals",
                 "also_clothed", "cover_male_genitals", "cover_buttocks")
        for i, k in enumerate(order):
            ttk.Checkbutton(cv, text=COVER_LABELS[k], variable=self.v[k], command=self.custom).grid(
                row=2 + i, column=0, sticky="w", padx=(16 if k == "also_clothed" else 0, 0))
        ttk.Label(cv, text="Faces", style="Head.TLabel").grid(row=11, column=0, sticky="w", pady=(12, 2))
        self.faces = {}
        for i, (key, who) in enumerate((("faces_women", "Women"), ("faces_men", "Men"))):
            row = ttk.Frame(cv, style="Card.TFrame")
            row.grid(row=12 + i, column=0, sticky="w", pady=1)
            ttk.Label(row, text=who, style="Card.TLabel", width=7).grid(row=0, column=0, sticky="w")
            box = ttk.Combobox(row, state="readonly", width=20, values=[t for _, t in FACE_CHOICES])
            box.set(dict(FACE_CHOICES).get(s.get(key, "show"), "Show"))
            box.grid(row=0, column=1, sticky="w")
            self.faces[key] = box
        ttk.Label(cv, text="Where", style="Head.TLabel").grid(row=15, column=0, sticky="w", pady=(12, 2))
        self.only = tk.BooleanVar(value=bool(s.get("only_in_programs", True)))
        self.programs = list(s.get("programs", DEFAULT_PROGRAMS))
        ttk.Checkbutton(cv, text="Only in browsers, video players,\nphoto viewers and chat apps",
                        variable=self.only).grid(row=16, column=0, sticky="w")
        ttk.Label(cv, text="never in Word, Excel, the desktop...", style="Hint.TLabel").grid(
            row=17, column=0, sticky="w", padx=(24, 0))
        ttk.Button(cv, text="Programs...", command=self.edit_programs).grid(
            row=18, column=0, sticky="w", pady=(6, 0))

        # 2. Strength sliders
        self.num = {}
        st = cards[1]
        ttk.Label(st, text="Strength", style="Head.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        for i, spec in enumerate(STRENGTH):
            self.slider(st, 1 + i, spec)
        ttk.Button(st, text="Default strength", command=self.reset_strength).grid(
            row=1 + len(STRENGTH), column=0, columnspan=2, sticky="w", pady=(8, 0))

        # 3. Look + preview
        lk = cards[2]
        ttk.Label(lk, text="Look", style="Head.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        self.preview = Preview(lk, S)
        self.preview.canvas.grid(row=1, column=0, columnspan=2, pady=(6, 8))
        self.warn = ttk.Label(lk, text="", style="Warn.TLabel", wraplength=int(250 * S), justify="left")
        for i, spec in enumerate(LOOK):
            self.slider(lk, 2 + i, spec)
        self.warn.grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Button(lk, text="Default look", command=self.reset_look).grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))

        # Processor use
        ttk.Label(lk, text="Processor use", style="Head.TLabel").grid(row=6, column=0, columnspan=2,
                                                                     sticky="w", pady=(14, 0))
        level = s.get("cpu_level", "auto")
        self.cpu_auto = tk.BooleanVar(value=level == "auto")
        self.cpu = tk.DoubleVar(value=3 if level == "auto" else float(level))
        cpu_top = ttk.Frame(lk, style="Card.TFrame")
        cpu_top.grid(row=7, column=0, columnspan=2, sticky="ew")
        cpu_top.columnconfigure(0, weight=1)
        ttk.Checkbutton(cpu_top, text="Auto (chosen for this PC)", variable=self.cpu_auto,
                        command=self.show_cpu).grid(row=0, column=0, sticky="w")
        self.cpu_text = ttk.Label(cpu_top, style="Value.TLabel", width=9, anchor="e")
        self.cpu_text.grid(row=0, column=1, sticky="e")
        self.cpu_scale = ttk.Scale(cpu_top, from_=0, to=4, length=int(250 * S), variable=self.cpu,
                                   command=lambda _=None: self.snap_cpu())
        self.cpu_scale.grid(row=1, column=0, columnspan=2, sticky="w")
        marks = ttk.Frame(cpu_top, style="Card.TFrame")
        marks.grid(row=2, column=0, columnspan=2, sticky="ew")
        for c, (text, anchor) in enumerate((("light", "w"), ("medium", "center"), ("maximum", "e"))):
            marks.columnconfigure(c, weight=1, uniform="m")
            ttk.Label(marks, text=text, style="Hint.TLabel", anchor=anchor).grid(row=0, column=c, sticky="ew")
        ttk.Label(cpu_top, text="lighter = quieter fan, new content found a little later",
                  style="Hint.TLabel").grid(row=3, column=0, columnspan=2, sticky="w")
        self.show_cpu()

        # 4. Live
        lv = cards[3]
        ttk.Label(lv, text="Live", style="Head.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        self.big = {}
        for i, (key, label) in enumerate((("covered", "of screen covered"), ("found", "found since start"),
                                          ("bodies", "bodies now"), ("zones", "body parts now"),
                                          ("openings", "openings now"), ("uptime", "running for"))):
            cell = ttk.Frame(lv, style="Card.TFrame")
            cell.grid(row=1 + i // 2, column=i % 2, sticky="w", padx=(0, 18), pady=4)
            self.big[key] = ttk.Label(cell, text="-", style="Big.TLabel")
            self.big[key].grid(row=0, column=0, sticky="w")
            ttk.Label(cell, text=label, style="Hint.TLabel").grid(row=1, column=0, sticky="w")
        self.graph = tk.Canvas(lv, width=int(250 * S), height=int(70 * S), bg=FIELD, highlightthickness=0)
        self.graph.grid(row=4, column=0, columnspan=2, pady=(8, 2), sticky="w")
        legend = ttk.Frame(lv, style="Card.TFrame")
        legend.grid(row=5, column=0, columnspan=2, sticky="w")
        ttk.Label(legend, text="last 2 minutes:", style="Hint.TLabel").grid(row=0, column=0)
        ttk.Label(legend, text="  covered %", style="Hint.TLabel", foreground=ACCENT).grid(row=0, column=1)
        ttk.Label(legend, text="  bodies + parts", style="Hint.TLabel", foreground=GREEN).grid(row=0, column=2)
        self.speed = ttk.Label(lv, text="", style="Hint.TLabel", wraplength=int(250 * S), justify="left")
        self.speed.grid(row=6, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # Footer
        ft = ttk.Frame(outer, style="Bg.TFrame")
        ft.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(12, 0))
        ft.columnconfigure(5, weight=1)
        st_cb = ttk.Style(root)
        st_cb.configure("Footer.TCheckbutton", background=BG)
        st_cb.map("Footer.TCheckbutton", background=[("active", BG)])
        self.startup = tk.BooleanVar(value=bool(s["run_at_startup"]))
        self.hidden = tk.BooleanVar(value=bool(s.get("start_hidden", False)))
        ttk.Checkbutton(ft, text="Start with Windows", variable=self.startup, style="Footer.TCheckbutton").grid(
            row=0, column=0, columnspan=2, sticky="w", padx=(6, 16))
        ttk.Checkbutton(ft, text="Start hidden (no icon next to the clock)", variable=self.hidden,
                        style="Footer.TCheckbutton").grid(row=0, column=2, columnspan=3, sticky="w")
        self.updates = tk.BooleanVar(value=bool(s.get("check_updates", True)))
        ttk.Checkbutton(ft, text="Check for new versions", variable=self.updates,
                        style="Footer.TCheckbutton").grid(row=0, column=5, sticky="w")
        ttk.Button(ft, text="\u2665  Donate to victims of sexual violence", command=self.donate).grid(
            row=0, column=6, columnspan=3, sticky="e", padx=(3, 6))
        ttk.Label(ft, text="Setting a password means the pixelator settings cannot be changed without "
                           "entering the password or uninstalling the app. CTRL + ALT + Q will no longer "
                           "stop it.", style="Unsaved.TLabel", font=("Segoe UI", 9)).grid(
            row=1, column=0, columnspan=9, sticky="w", padx=6, pady=(10, 2))
        self.pw_text = ttk.Label(ft, style="Bg.TLabel")
        self.pw_text.grid(row=2, column=0, sticky="w", padx=(6, 6))
        ttk.Button(ft, text="Set password...", command=self.set_password).grid(row=2, column=1, padx=3)
        ttk.Button(ft, text="Remove", command=self.remove_password).grid(row=2, column=2, padx=3, sticky="w")
        ttk.Button(ft, text="Open log", command=self.open_log).grid(row=2, column=4, padx=(16, 3), sticky="w")
        self.unsaved = ttk.Label(ft, text="", style="Unsaved.TLabel")
        self.unsaved.grid(row=2, column=5, sticky="e", padx=10)
        ttk.Button(ft, text="Uninstall...", command=self.uninstall).grid(row=2, column=6, padx=3)
        ttk.Button(ft, text="Close", style="Accent.TButton", command=self.close).grid(
            row=2, column=8, padx=(3, 6))

        self.show_password_state()
        self.custom()
        if self.updates.get():
            self.check_for_update()
        self.tick()
        self.animate()

    # -- building blocks
    def slider(self, parent, row: int, spec) -> None:
        key, label, low, high, default, unit, hint = spec
        var = tk.DoubleVar(value=float(self.s.get(key, default)))
        self.num[key] = (var, spec)
        top = ttk.Frame(parent, style="Card.TFrame")
        top.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        top.columnconfigure(0, weight=1)
        ttk.Label(top, text=label, style="Card.TLabel").grid(row=0, column=0, sticky="w")
        value = ttk.Label(top, style="Value.TLabel", width=8, anchor="e")
        value.grid(row=0, column=1, sticky="e")

        def label(*_):
            # follows every change of the value: dragging, "Default" buttons
            try:
                v = var.get()
            except tk.TclError:
                return
            v = round(v, 1) if isinstance(default, float) else round(v)
            value.config(text=f"{v:g}{unit[:-1] if unit == ' blocks' and v == 1 else unit}")
            if key == "darkness":
                self.warn.config(text="Warning: very dark blocks hide everything underneath them. If "
                                      "something is detected by mistake, buttons or text there can't "
                                      "be seen." if v >= 80 else "")

        def show(_=None):   # dragging: snap to whole steps
            v = var.get()
            var.set(round(v, 1) if isinstance(default, float) else round(v))

        var.trace_add("write", label)
        ttk.Scale(top, from_=low, to=high, length=int(250 * self.scale), variable=var,
                  command=show).grid(row=1, column=0, columnspan=2, sticky="w")
        if key == "darkness":
            marks = ttk.Frame(top, style="Card.TFrame")
            marks.grid(row=2, column=0, columnspan=2, sticky="ew")
            for c, (text, anchor) in enumerate((("mosaic", "w"), ("darkness", "center"), ("black", "e"))):
                marks.columnconfigure(c, weight=1, uniform="m")
                ttk.Label(marks, text=text, style="Hint.TLabel", anchor=anchor).grid(row=0, column=c, sticky="ew")
        if hint:
            ttk.Label(top, text=hint, style="Hint.TLabel").grid(row=2, column=0, columnspan=2, sticky="w")
        top.after_idle(label)

    def value(self, key):
        var, spec = self.num[key]
        return round(var.get(), 1) if isinstance(spec[4], float) else int(round(var.get()))

    # -- live parts
    def animate(self) -> None:
        cover = any(self.v[k].get() for k in ("cover_women", "cover_men", "cover_unclear",
                                              "cover_breasts", "cover_female_genitals"))
        try:
            self.preview.draw(self.value("block_size"), self.value("darkness") / 100,
                              self.face_value("faces_women"), cover)
        except Exception:
            pass
        # Pulsing dot while running
        self.pulse += 0.25
        S, d = self.scale, self.dot
        d.delete("all")
        c = 11 * S
        if self.running:
            r = (5 + 2.5 * (1 + math.sin(self.pulse))) * S
            d.create_oval(c - r - 2 * S, c - r - 2 * S, c + r + 2 * S, c + r + 2 * S, fill="#123c2e", outline="")
            d.create_oval(c - 5 * S, c - 5 * S, c + 5 * S, c + 5 * S, fill=GREEN, outline="")
        else:
            d.create_oval(c - 5 * S, c - 5 * S, c + 5 * S, c + 5 * S, fill=RED, outline="")
        self.root.after(80, self.animate)

    def tick(self) -> None:
        self.running = running_pid() is not None
        if self.busy:          # start/stop still working in the background
            self.root.after(300, self.tick)
            return
        if self.apply_later:              # the last quick choice still has to be applied
            self.apply_later = False
            self.apply()
            self.root.after(300, self.tick)
            return
        if self.busy_result is not None:
            self.root.config(cursor="")
            if self.busy_result is False:
                messagebox.showerror(APP_NAME, "Could not stop the pixelator.")
            self.busy_result = None
        self.status.config(text="Running" if self.running else "Not running",
                           foreground=GREEN if self.running else RED)
        self.hotkey.config(text=("CTRL + ALT + Q is off while a password is set" if self.s.get("password_hash")
                                 else "CTRL + ALT + Q stops the pixelator")
                           + "\nCTRL + ALT + P saves a picture with the mosaic")
        st = read_status() if self.running else None
        if st:
            up = int(time.time() - st.get("started", time.time()))
            self.big["covered"].config(text=f"{st['covered']:.0f}%")
            self.big["found"].config(text=str(st["found"]))
            for k in ("bodies", "zones", "openings"):
                self.big[k].config(text=str(st[k]))
            self.big["uptime"].config(text=f"{up // 3600}:{up // 60 % 60:02d}" if up >= 3600
                                      else f"{up // 60}:{up % 60:02d}")
            scan = f"{st['full_scan']:.2f} s" if st.get("full_scan") else "-"
            self.speed.config(text=f"screen {st['screen_fps']:.0f}/s  ·  redraws {st['redraws']:.0f}/s\n"
                                   f"focus {st['focus']:.0f}/s  ·  full scan {scan}"
                                   + (f"\nprocessor use: {st['cpu']}" if st.get("cpu") else ""))
            self.history.append((st["covered"], st["zones"] + st["bodies"]))
        else:
            for k in self.big:
                self.big[k].config(text="-")
            self.speed.config(text="Start the pixelator to see it work." if not self.running
                              else "Waiting for the first numbers...")
            self.history.append((0.0, 0))
        self.draw_graph()
        # Changes take effect by themselves, 2 seconds after the last one
        now = self.collect()
        if now == self.s:
            self.unsaved.config(text="")
        elif now != self.last_seen:
            self.last_seen, self.changed_at = now, time.monotonic()
            self.unsaved.config(text="● Applying your changes...")
        elif time.monotonic() - self.changed_at >= 2:
            self.apply()
        self.custom()
        self.root.after(1000, self.tick)

    def draw_graph(self) -> None:
        g, S = self.graph, self.scale
        w, h = int(250 * S), int(70 * S)
        g.delete("all")
        for y in (0.25, 0.5, 0.75):
            g.create_line(0, h * y, w, h * y, fill=LINE)
        data = list(self.history)
        top_cov = max(10.0, max(c for c, _ in data))
        top_z = max(4, max(z for _, z in data))
        step = w / (len(data) - 1)
        for idx, top, colour in ((0, top_cov, ACCENT), (1, top_z, GREEN)):
            pts = []
            for i, d in enumerate(data):
                pts += [i * step, h - 3 - (h - 6) * d[idx] / top]
            g.create_line(*pts, fill=colour, width=max(1, int(2 * S)), smooth=True)

    # -- helpers
    def use_preset(self, _=None) -> None:
        """A quick choice takes effect at once (no Save needed); own changes
        made afterwards take effect 2 seconds after the last change."""
        p = PRESETS.get(self.preset.get())
        if not p:
            return
        for k, v in p.items():
            if k in self.v:
                self.v[k].set(v)
            elif k in self.faces:
                self.faces[k].set(dict(FACE_CHOICES)[v])
            elif k in self.num:
                self.num[k][0].set(v)
        if self.busy:              # still restarting for the previous choice
            self.apply_later = True
        else:
            self.apply()

    def custom(self) -> None:
        """Shows which quick choice the current settings match, if any."""
        now = self.collect()
        name = next((n for n, p in PRESETS.items()
                     if all(now.get(k) == v for k, v in p.items())), "")
        if self.preset.get() != (name or "(your own choice)"):
            self.preset.set(name or "(your own choice)")

    def reset_strength(self) -> None:
        for key, _, _, _, default, _, _ in STRENGTH:
            self.num[key][0].set(default)

    def edit_programs(self) -> None:
        w = tk.Toplevel(self.root, bg=CARD, padx=16, pady=14)
        w.title("Programs")
        w.transient(self.root)
        ttk.Label(w, text="Cover only inside these programs", style="Head.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(w, text="One program file name per line, e.g. chrome.exe. To find a name: Task Manager,\n"
                          "Details tab, column Name.", style="Hint.TLabel").grid(row=1, column=0, sticky="w")
        box = tk.Text(w, width=44, height=18, bg=FIELD, fg=TEXT, insertbackground=TEXT, relief="flat",
                      font=("Consolas", 10))
        box.grid(row=2, column=0, pady=8)
        box.insert("1.0", "\n".join(self.programs))
        row = ttk.Frame(w, style="Card.TFrame")
        row.grid(row=3, column=0, sticky="ew")

        def default():
            box.delete("1.0", "end")
            box.insert("1.0", "\n".join(DEFAULT_PROGRAMS))

        def ok():
            names = [n.strip().lower() for n in box.get("1.0", "end").split() if n.strip()]
            self.programs = [n if n.endswith(".exe") else n + ".exe" for n in names]
            w.destroy()

        ttk.Button(row, text="Default list", command=default).grid(row=0, column=0)
        ttk.Button(row, text="OK", style="Accent.TButton", command=ok).grid(row=0, column=1, padx=(80, 4))
        ttk.Button(row, text="Cancel", command=w.destroy).grid(row=0, column=2)

    CPU_NAMES = ("Light", "Low", "Medium", "High", "Maximum")

    def snap_cpu(self) -> None:
        v = int(round(self.cpu.get()))
        if self.cpu.get() != v:
            self.cpu.set(v)
        self.show_cpu()

    def show_cpu(self) -> None:
        auto = self.cpu_auto.get()
        self.cpu_scale.state(["disabled"] if auto else ["!disabled"])
        self.cpu_text.config(text="Auto" if auto else self.CPU_NAMES[int(round(self.cpu.get()))])

    def check_for_update(self) -> None:
        """Asks GitHub for the newest release, in the background."""
        import threading
        import urllib.request

        def ask():
            try:
                req = urllib.request.Request(f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
                                             headers={"User-Agent": APP_NAME,
                                                      "Accept": "application/vnd.github+json"})
                with urllib.request.urlopen(req, timeout=8) as r:
                    info = json.load(r)
                tag = str(info.get("tag_name", "")).lstrip("vV")
                newest = tuple(int(x) for x in tag.split(".") if x.isdigit())
                mine = tuple(int(x) for x in APP_VERSION.split("."))
                if newest and newest > mine:
                    self.root.after(0, lambda: self.show_update(tag, info.get("html_url", "")))
            except Exception:
                pass   # no internet, no release yet: just no message

        threading.Thread(target=ask, daemon=True).start()

    def show_update(self, version: str, url: str) -> None:
        import webbrowser
        page = url or f"https://github.com/{GITHUB_REPO}/releases/latest"
        self.update_button.config(text=f"New version {version}: download",
                                  command=lambda: webbrowser.open(page))
        self.update_button.grid(row=2, column=0, columnspan=4, sticky="e", pady=(6, 0))

    def face_value(self, key: str) -> str:
        text = self.faces[key].get()
        return next((v for v, t in FACE_CHOICES if t == text), "show")

    def reset_look(self) -> None:
        self.num["block_size"][0].set(DEFAULTS["block_size"])
        self.num["darkness"][0].set(0)

    def donate(self) -> None:
        import webbrowser
        w = tk.Toplevel(self.root, bg=CARD, padx=20, pady=16)
        w.title("Donate")
        w.resizable(False, False)
        w.transient(self.root)
        ttk.Label(w, text="Help victims of sexual violence", style="Head.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(w, text="Your gift goes straight to the organisation, on its own website.",
                  style="Hint.TLabel").grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 10))
        r = 2
        for name, what, url in CHARITIES:
            ttk.Label(w, text=name, style="Card.TLabel", font=("Segoe UI Semibold", 10)).grid(
                row=r, column=0, sticky="w")
            ttk.Label(w, text=what, style="Hint.TLabel").grid(row=r + 1, column=0, sticky="w", pady=(0, 8))
            ttk.Button(w, text="Donate", style="Accent.TButton",
                       command=lambda u=url: webbrowser.open(u)).grid(row=r, column=1, rowspan=2, padx=(16, 0))
            r += 2
        if DONATE_URL or BITCOIN_ADDRESS:
            ttk.Label(w, text="Support the creator of " + APP_NAME, style="Head.TLabel").grid(
                row=r, column=0, columnspan=2, sticky="w", pady=(10, 4))
            r += 1
        if DONATE_URL:
            ttk.Button(w, text="Open the donation page", command=lambda: webbrowser.open(DONATE_URL)).grid(
                row=r, column=0, sticky="w", pady=(0, 8))
            r += 1
        if BITCOIN_ADDRESS:
            e = ttk.Entry(w, width=46)
            e.insert(0, BITCOIN_ADDRESS)
            e.configure(state="readonly")
            e.grid(row=r, column=0, sticky="w")

            def copy():
                self.root.clipboard_clear()
                self.root.clipboard_append(BITCOIN_ADDRESS)

            ttk.Button(w, text="Copy Bitcoin address", command=copy).grid(row=r, column=1, padx=(16, 0))
        ttk.Button(w, text="Close", command=w.destroy).grid(row=r + 1, column=1, sticky="e", pady=(12, 0))

    def show_password_state(self) -> None:
        if self.new_password == "":
            t = "Password: removed when saved"
        elif self.new_password:
            t = "Password: set when saved"
        else:
            t = "Password: " + ("on" if self.s.get("password_hash") else "off")
        self.pw_text.config(text=t)

    # -- actions
    def in_background(self, work, busy_text: str) -> None:
        """Runs start/stop work outside the window, so the window never hangs
        (stopping can take a few seconds, longer when Windows is slow)."""
        import threading
        if self.busy:
            return
        self.busy = busy_text
        self.root.config(cursor="watch")
        self.status.config(text=busy_text, foreground=AMBER)

        def run():
            try:
                self.busy_result = work()
            except Exception:
                self.busy_result = False
            self.busy = None      # tick() notices this and tidies up

        threading.Thread(target=run, daemon=False).start()   # finishes even when the window closes

    def start(self) -> None:
        self.in_background(lambda: start_pixelator() or True, "Starting...")

    def stop(self) -> None:
        self.in_background(stop_pixelator, "Stopping...")

    def set_password(self) -> None:
        a = simpledialog.askstring("Password", "New password:", show="*", parent=self.root)
        if not a:
            return
        b = simpledialog.askstring("Password", "Same password again:", show="*", parent=self.root)
        if a != b:
            messagebox.showerror("Password", "The two passwords are not the same.")
            return
        salt = secrets.token_hex(16)
        self.new_password = (salt, hash_password(a, salt))
        self.show_password_state()

    def remove_password(self) -> None:
        self.new_password = ""
        self.show_password_state()

    def open_log(self) -> None:
        if os.path.exists(LOG_FILE) and WINDOWS:
            os.startfile(LOG_FILE)
        else:
            messagebox.showinfo(APP_NAME, "No log yet.")

    def collect(self) -> dict:
        s = dict(self.s)
        s.update({k: bool(self.v[k].get()) for k in COVER_KEYS})
        s.update({k: self.value(k) for k in self.num})
        s.update(only_in_programs=bool(self.only.get()), programs=list(self.programs))
        s.update({k: self.face_value(k) for k in self.faces})
        s["cpu_level"] = "auto" if self.cpu_auto.get() else int(round(self.cpu.get()))
        s["check_updates"] = bool(self.updates.get())
        s.update(run_at_startup=bool(self.startup.get()),
                 start_hidden=bool(self.hidden.get()))
        if self.new_password == "":
            s.pop("password_hash", None)
            s.pop("password_salt", None)
        elif self.new_password:
            s["password_salt"], s["password_hash"] = self.new_password
        return s

    def apply(self) -> None:
        s = self.collect()
        try:
            save(s)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Could not save the settings:\n{e}")
            return
        self.s, self.new_password = s, None
        self.show_password_state()
        self.history.extend([(0.0, 0)] * 3)

        def restart():
            set_startup(s["run_at_startup"])
            ok = stop_pixelator()      # it reads the settings when it starts
            start_pixelator()
            return ok

        if self.busy:          # a restart is running: restart once more when it is done
            self.apply_later = True
            return
        self.in_background(restart, "Restarting...")

    def close(self) -> None:
        if self.collect() != self.s:   # a change still waiting: apply it before closing
            self.busy = None
            self.apply()
        self.root.destroy()

    def uninstall(self) -> None:
        if not messagebox.askyesno("Uninstall", "Remove " + APP_NAME + " from this computer?\n"
                                   "(Python and its packages stay installed.)"):
            return
        remove_program()
        self.root.destroy()


_MUTEX = None   # held while this settings window is open


def already_open() -> bool:
    """True when another settings window is open; that one is brought to the front."""
    global _MUTEX
    if not WINDOWS:
        return False
    try:
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        u = ctypes.WinDLL("user32", use_last_error=True)
        k.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        k.CreateMutexW.restype = wintypes.HANDLE
        _MUTEX = k.CreateMutexW(None, False, "Local\\ScreenPixelatorSettings")
        if ctypes.get_last_error() != 183:   # ERROR_ALREADY_EXISTS
            return False
        u.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        u.FindWindowW.restype = wintypes.HWND
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u.SetForegroundWindow.argtypes = [wintypes.HWND]
        u.IsIconic.argtypes = [wintypes.HWND]
        hwnd = u.FindWindowW("TkTopLevel", APP_NAME)   # the settings window itself
        if hwnd:
            if u.IsIconic(hwnd):
                u.ShowWindow(hwnd, 9)        # SW_RESTORE
            u.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def ask_password(root: tk.Tk, s: dict) -> bool:
    """Asks the password in the window itself. True when right (or none set)."""
    if not s.get("password_hash"):
        return True
    root.title(APP_NAME)
    frame = ttk.Frame(root, style="Bg.TFrame", padding=30)
    frame.grid()
    ttk.Label(frame, text=APP_NAME, style="Title.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
    ttk.Label(frame, text="Password:", style="Bg.TLabel").grid(row=1, column=0, sticky="w", pady=(16, 0))
    entry = ttk.Entry(frame, show="*", width=28)
    entry.grid(row=1, column=1, sticky="w", pady=(16, 0), padx=(8, 0))
    note = ttk.Label(frame, text="", style="Unsaved.TLabel")
    note.grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
    result = tk.StringVar(value="")
    tries = [0]

    def check(_=None):
        if secrets.compare_digest(hash_password(entry.get(), s.get("password_salt", "")), s["password_hash"]):
            result.set("ok")
            return
        tries[0] += 1
        entry.delete(0, "end")
        if tries[0] >= 3:
            result.set("no")
        else:
            note.config(text="Wrong password.")

    buttons = ttk.Frame(frame, style="Bg.TFrame")
    buttons.grid(row=3, column=0, columnspan=2, sticky="e", pady=(14, 0))
    ttk.Button(buttons, text="OK", style="Accent.TButton", command=check).grid(row=0, column=0, padx=4)
    ttk.Button(buttons, text="Cancel", command=lambda: result.set("no")).grid(row=0, column=1)
    entry.bind("<Return>", check)
    root.protocol("WM_DELETE_WINDOW", lambda: result.set("no"))
    root.deiconify()
    root.lift()
    root.focus_force()
    entry.focus_set()
    root.wait_variable(result)
    frame.destroy()
    return result.get() == "ok"


def main() -> None:
    if "--uninstall" not in sys.argv and already_open():
        return
    try:   # own taskbar identity, so the taskbar shows the mosaic icon instead of Python's
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ScreenPixelator.Settings")
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # sharp text
    except Exception:
        pass
    root = tk.Tk()
    root.withdraw()
    try:
        root.iconbitmap(default=ICON_FILE)
    except Exception:
        pass
    if "--uninstall" in sys.argv:
        if messagebox.askyesno("Uninstall", "Remove " + APP_NAME + " from this computer?\n"
                               "(Python and its packages stay installed.)"):
            remove_program()     # no "removed" message: it would keep the app listed until OK
        root.destroy()
        return
    s = load()
    setup_style(root)
    if not ask_password(root, s):
        root.destroy()
        return
    root.withdraw()
    app = App(root, s)
    root.protocol("WM_DELETE_WINDOW", app.close)
    root.deiconify()
    root.mainloop()


def log_errors() -> None:
    """Without a console window, errors go to settings.log next to the program."""
    import traceback

    def write(*info):
        try:
            with open(os.path.join(HERE, "settings.log"), "a", encoding="utf-8") as f:
                f.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + "".join(traceback.format_exception(*info)))
        except Exception:
            pass

    sys.excepthook = write
    tk.Tk.report_callback_exception = lambda self, *info: write(*info)


if __name__ == "__main__":
    log_errors()
    try:
        main()
    except Exception:
        sys.excepthook(*sys.exc_info())
