#!/usr/bin/env python3
"""Backend for Wallpaper Rotator (ric-v.wallrotator).

Desktop-agnostic: used by the Omarchy shell plugin (Panel.qml) and by the
standalone system tray app (wallrotator_tray.py) on any Linux/BSD desktop.

Supports two wallpaper modes:
  - wallhaven: Static wallpapers from Wallhaven (https://wallhaven.cc/api/v1/search)
               with optional per-time-of-day color palette filtering.
  - pixabay:   Live video wallpapers from Pixabay (https://pixabay.com/api/videos/).

Time-of-day buckets:
  - morning: 07:00 - 11:59 (7am - 12pm)
  - noon:    12:00 - 14:59 (12pm - 3pm)
  - evening: 15:00 - 17:59 (3pm - 6pm)
  - night:   18:00 - 06:59 (6pm - 7am)

Commands (every output is one JSON line on stdout):
  wallrotator.py status
  wallrotator.py set-mode <wallhaven|pixabay>
  wallrotator.py set-running <true|false>
  wallrotator.py set-schedule <0|5|15|30|60|120>
  wallrotator.py set-shuffle <true|false>
  wallrotator.py add-tag <tag> [period]
  wallrotator.py remove-tag <tag> [period]
  wallrotator.py set-color <period> <hex|none>
  wallrotator.py set-key <pixabay|wallhaven>   (reads key from stdin)
  wallrotator.py set-backend <auto|backend>
  wallrotator.py next
  wallrotator.py prev
  wallrotator.py shuffle
  wallrotator.py apply <path>

Wallpaper backends (auto-detected, override with set-backend or the
WALLROTATOR_BACKEND environment variable):
  omarchy, gnome (also Budgie/Pantheon/Unity/COSMIC-on-GNOME), cinnamon, mate,
  kde, xfce, lxqt, lxde, hyprpaper, swww (or awww), sway, swaybg, feh,
  xwallpaper, nitrogen, hsetroot

Live (video) wallpapers use the first available engine: owe (Omarchy),
mpvpaper (Wayland compositors with layer-shell), or xwinwrap + mpv (X11).
Without one, a still frame of the video is set as a static wallpaper.
"""

import datetime
import fcntl
import hashlib
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HOME = Path.home()
XDG_DATA_HOME = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share")
STATE_DIR = HOME / ".local/state/wallrotator"
LIVEWALL_STATE_DIR = HOME / ".local/state/livewall"
STATE_FILE = STATE_DIR / "state.json"
THEME_NAME_FILE = HOME / ".local/state/omarchy/current/theme.name"
CURRENT_BG_LINK = HOME / ".local/state/omarchy/current/background"
THEME_BG_DIR = HOME / ".local/state/omarchy/current/theme/backgrounds"
BG_ROOT = HOME / ".config/omarchy/backgrounds"
PICTURES_ROTATE_DIR = HOME / "Pictures/wallrotate"
OMARCHY_SHARE_DIR = HOME / ".local/share/omarchy"
# Download location on non-Omarchy desktops
GENERIC_WALL_DIR = Path(os.environ.get("WALLROTATOR_DIR") or XDG_DATA_HOME / "wallrotator/wallpapers")
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", HOME / ".cache")) / "wallrotator"
THUMB_DIR = CACHE_DIR / "thumbs"
SEARCH_DIR = CACHE_DIR / "search"

VERSION = "1.2.0"
UA = f"omarchy-wallrotator/{VERSION}"
TIMEOUT = 20
SEARCH_TTL = 1800
MAX_JSON = 4 * 1024 * 1024
MAX_THUMB = 3 * 1024 * 1024
MAX_IMAGE = 50 * 1024 * 1024
MAX_VIDEO = 300 * 1024 * 1024
MAX_HISTORY = 10
MAX_TAGS = 15
VALID_SCHEDULES = (0, 5, 15, 30, 60, 120)
VALID_MODES = ("wallhaven", "pixabay")
PERIODS = ("morning", "noon", "evening", "night")

DEFAULT_PERIOD_TAGS = {
    "morning": ["sunrise", "morning mist", "forest", "meadow", "fresh nature"],
    "noon": ["sunny landscape", "ocean", "mountains", "waterfall", "blue sky"],
    "evening": ["sunset", "golden hour", "autumn", "city skyline", "warm clouds"],
    "night": ["night sky", "cyberpunk", "aurora", "galaxy", "neon city"],
}

# Official Wallhaven API supported color palette hex codes (without '#')
WALLHAVEN_COLORS = (
    "660000", "990000", "cc0000", "cc3333", "ea4c88",
    "993399", "663399", "333399", "0066cc", "0099cc",
    "66cccc", "77cc33", "669900", "336600", "666600",
    "999900", "cccc33", "ffff00", "ffcc33", "ff9900",
    "ff6600", "cc6633", "996633", "663300", "000000",
    "999999", "cccccc", "ffffff", "424153",
)

STATIC_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
# Paths handed to tools that parse a command string (swaymsg, hyprctl) must match this.
SAFE_PATH_RE = re.compile(r"[A-Za-z0-9._/+-]+")

# backend -> executables (any one is enough for the backend to be usable)
BACKEND_TOOLS = {
    "omarchy": ("omarchy",),
    "gnome": ("gsettings",),
    "cinnamon": ("gsettings",),
    "mate": ("gsettings",),
    "kde": ("plasma-apply-wallpaperimage", "qdbus6", "qdbus"),
    "xfce": ("xfconf-query",),
    "lxqt": ("pcmanfm-qt",),
    "lxde": ("pcmanfm",),
    "hyprpaper": ("hyprctl",),
    "swww": ("awww", "swww"),
    "sway": ("swaymsg",),
    "swaybg": ("swaybg",),
    "feh": ("feh",),
    "xwallpaper": ("xwallpaper",),
    "nitrogen": ("nitrogen",),
    "hsetroot": ("hsetroot",),
}
BACKENDS = tuple(BACKEND_TOOLS)
LIVE_ENGINES = ("omarchy", "mpvpaper", "xwinwrap", "frame")
VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".m4v"}

WALLHAVEN_API = "https://wallhaven.cc/api/v1/search"
WALLHAVEN_MEDIA_HOSTS = {"w.wallhaven.cc"}
WALLHAVEN_THUMB_HOSTS = {"th.wallhaven.cc"}

PIXABAY_API = "https://pixabay.com/api/videos/"
PIXABAY_MEDIA_HOSTS = {"cdn.pixabay.com"}
PIXABAY_THUMB_HOSTS = {"cdn.pixabay.com", "i.vimeocdn.com"}


class WRError(Exception):
    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = neutralize_markup(code, 40)
        self.detail = neutralize_markup(detail, 200)


def neutralize_markup(value, max_len=120):
    """Strip HTML/RichText delimiter characters and control chars from untrusted strings."""
    s = str(value or "")
    s = re.sub(r"[<>&\"'`\x00-\x1f\x7f]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:max_len]


def current_period(hour=None):
    """Return active time-of-day bucket: morning (7-12), noon (12-15), evening (15-18), night (18-7)."""
    if hour is None:
        hour = datetime.datetime.now().hour
    h = int(hour) % 24
    if 7 <= h < 12:
        return "morning"
    if 12 <= h < 15:
        return "noon"
    if 15 <= h < 18:
        return "evening"
    return "night"


def out(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def fail(code, detail=""):
    out({
        "ok": False,
        "code": re.sub(r"[^a-z_]", "", str(code or "generic"))[:40] or "generic",
        "detail": neutralize_markup(detail, 200),
    })
    sys.exit(1)


def private_dir(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def write_private(path, data):
    private_dir(path.parent)
    if isinstance(data, str):
        data = data.encode("utf-8")
    tmp = path.with_name(path.name + ".part")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def prepare_storage():
    private_dir(STATE_DIR)
    private_dir(CACHE_DIR)
    private_dir(THUMB_DIR)
    private_dir(SEARCH_DIR)


def theme_slug():
    try:
        name = THEME_NAME_FILE.read_text().strip()
    except OSError:
        name = ""
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    return slug or "default"


def _resolve(p):
    try:
        return Path(p).resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def omarchy_background():
    if CURRENT_BG_LINK.is_symlink() or CURRENT_BG_LINK.is_file():
        r = _resolve(CURRENT_BG_LINK)
        if r and r.is_file():
            return str(r)
    return ""


def current_background(saved_path="", backend="omarchy"):
    """Active wallpaper: Omarchy's background symlink, else the path we last applied."""
    if backend == "omarchy":
        cur = omarchy_background()
        if cur:
            return cur
    p = str(saved_path or "")
    if p and Path(p).is_file():
        return p
    return ""


def clean_tag(raw):
    t = neutralize_markup(raw, 32).lower()
    t = re.sub(r"[^a-z0-9 _+-]", "", t).strip()
    return t[:28]


def clean_color(raw):
    c = str(raw or "").strip().lower().lstrip("#")
    if c in ("", "none", "any", "clear"):
        return ""
    return c if c in WALLHAVEN_COLORS else ""


def read_key(provider):
    env = os.environ.get(f"{provider.upper()}_API_KEY", "").strip()
    if env:
        return env
    for base in (STATE_DIR, LIVEWALL_STATE_DIR):
        kf = base / f"{provider}.key"
        try:
            if kf.is_file():
                val = kf.read_text().strip()
                if val:
                    return val
        except OSError:
            pass
    return ""


def valid_pixabay_key(k):
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{16,120}", str(k or "")))


def valid_wallhaven_key(k):
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{16,120}", str(k or "")))


def host_ok(url, allowed_hosts):
    p = urllib.parse.urlparse(str(url or ""))
    return p.scheme == "https" and p.hostname in allowed_hosts


def _open(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def _read_limited(resp, limit):
    data = resp.read(limit + 1)
    if len(data) > limit:
        raise WRError("too_big")
    return data


def get_json(url, provider_label="API"):
    try:
        with _open(url) as r:
            return json.loads(_read_limited(r, MAX_JSON))
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403):
            raise WRError("key_rejected", provider_label)
        if e.code == 429:
            raise WRError("rate_limited")
        if e.code == 404:
            raise WRError("not_found")
        raise WRError("server_error", str(e.code))
    except urllib.error.URLError as e:
        raise WRError("network", str(getattr(e, "reason", e)))
    except ValueError:
        raise WRError("server_error", "invalid JSON")


def fetch_thumb(prefix, item_id, url):
    prepare_storage()
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "", str(item_id))[:32]
    dest = THUMB_DIR / f"{prefix}-{safe_id}.jpg"
    if dest.is_file() and dest.stat().st_size > 0:
        return str(dest)
    if not url:
        return ""
    try:
        with _open(url) as r:
            data = _read_limited(r, MAX_THUMB)
        write_private(dest, data)
        return str(dest)
    except Exception:
        return ""


def frame_thumb(video_path, dest, width=480):
    if dest.is_file() and dest.stat().st_size > 0:
        return str(dest)
    if not shutil.which("ffmpeg"):
        return ""
    prepare_storage()
    private_dir(dest.parent)
    tmp = dest.with_name(dest.stem + ".tmp.jpg")
    scale = ["-vf", f"scale={int(width)}:-2", "-q:v", "4"] if width else ["-q:v", "2"]
    try:
        subprocess.run(
            [
                "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
                "-protocol_whitelist", "file", "-ss", "1.0",
                "-i", str(video_path), "-frames:v", "1",
                *scale, str(tmp),
            ],
            capture_output=True,
            timeout=15,
            umask=0o077,
        )
        if tmp.is_file() and tmp.stat().st_size > 0:
            os.chmod(tmp, 0o600)
            tmp.replace(dest)
            return str(dest)
    except (OSError, subprocess.TimeoutExpired):
        pass
    tmp.unlink(missing_ok=True)
    return ""


def seed_existing_backgrounds(existing_history, cur=""):
    """Ensure recent history includes existing theme backgrounds if fewer than MAX_HISTORY."""
    seen = {h.get("path") for h in existing_history if h.get("path")}
    seeded = list(existing_history)
    if len(seeded) >= MAX_HISTORY:
        return seeded[:MAX_HISTORY]

    theme = theme_slug()
    dirs = [GENERIC_WALL_DIR, BG_ROOT / theme, THEME_BG_DIR, PICTURES_ROTATE_DIR]
    if BG_ROOT.is_dir():
        for sub in sorted(BG_ROOT.iterdir()):
            if sub.is_dir() and sub not in dirs:
                dirs.append(sub)
    candidates = []
    for d in dirs:
        if not d.is_dir():
            continue
        for f in d.iterdir():
            if not f.is_file() or f.name.startswith("."):
                continue
            ext = f.suffix.lower()
            if ext not in STATIC_EXTS and ext not in VIDEO_EXTS:
                continue
            real = str(_resolve(f) or f)
            if real in seen:
                continue
            try:
                mtime = int(f.stat().st_mtime)
            except OSError:
                mtime = 0
            prio = 1 if real == cur else 0
            candidates.append((prio, mtime, f, real, ext))

    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    for _, mtime, f, real, ext in candidates:
        if len(seeded) >= MAX_HISTORY:
            break
        seen.add(real)
        is_video = ext in VIDEO_EXTS
        mode = "pixabay" if is_video else "wallhaven"
        stem = neutralize_markup(re.sub(r"[-_]+", " ", f.stem), 48)
        thumb = ""
        if not is_video:
            thumb = real
        else:
            m = re.match(r"^(pixabay|pexels)-(\d+)", f.stem)
            if m:
                lw_thumb = HOME / ".cache/livewall/thumbs" / f"{m.group(1)}-{m.group(2)}.jpg"
                wr_thumb = THUMB_DIR / f"{m.group(1)}-{m.group(2)}.jpg"
                if wr_thumb.is_file():
                    thumb = str(wr_thumb)
                elif lw_thumb.is_file():
                    thumb = str(lw_thumb)
            if not thumb:
                sig = hashlib.sha256(real.encode()).hexdigest()[:16]
                thumb = frame_thumb(real, THUMB_DIR / f"video-{sig}.jpg")
        seeded.append({
            "id": neutralize_markup(f.stem, 40),
            "path": real,
            "thumb": thumb,
            "title": stem or f.name[:40],
            "mode": mode,
            "resolution": "",
            "tag": theme,
            "timestamp": mtime,
        })
    return seeded[:MAX_HISTORY]


def _raw_state():
    if STATE_FILE.is_file():
        try:
            raw = json.loads(STATE_FILE.read_text())
            if isinstance(raw, dict):
                return raw
        except (OSError, ValueError):
            pass
    return {}


def load_state():
    prepare_storage()
    raw = _raw_state()

    backend_setting = str(raw.get("backend") or "auto")
    if backend_setting not in BACKENDS:
        backend_setting = "auto"

    mode = raw.get("mode") if raw.get("mode") in VALID_MODES else "wallhaven"

    raw_period_tags = raw.get("periodTags")
    period_tags = {}
    for p in PERIODS:
        if isinstance(raw_period_tags, dict) and isinstance(raw_period_tags.get(p), list):
            cleaned = []
            for t in raw_period_tags[p]:
                ct = clean_tag(t)
                if ct and ct not in cleaned:
                    cleaned.append(ct)
                if len(cleaned) >= MAX_TAGS:
                    break
            period_tags[p] = cleaned
        else:
            period_tags[p] = list(DEFAULT_PERIOD_TAGS[p])

    raw_period_colors = raw.get("periodColors")
    period_colors = {}
    for p in PERIODS:
        if isinstance(raw_period_colors, dict):
            period_colors[p] = clean_color(raw_period_colors.get(p, ""))
        else:
            period_colors[p] = ""

    sched = raw.get("scheduleMinutes", 30)
    try:
        sched = int(sched)
    except (TypeError, ValueError):
        sched = 30
    if sched not in VALID_SCHEDULES:
        sched = 30

    running = bool(raw.get("running", False))
    if sched == 0:
        running = False

    shuffle = bool(raw.get("shuffle", True))
    try:
        tag_cursor = max(0, int(raw.get("tagCursor", 0)))
    except (TypeError, ValueError):
        tag_cursor = 0

    history = []
    for item in (raw.get("history") or [])[:MAX_HISTORY * 2]:
        if not isinstance(item, dict):
            continue
        p = str(item.get("path") or "")
        if not p or not Path(p).is_file():
            continue
        if any(h["path"] == p for h in history):
            continue
        m = item.get("mode") if item.get("mode") in VALID_MODES else (
            "pixabay" if Path(p).suffix.lower() in VIDEO_EXTS else "wallhaven"
        )
        thumb = str(item.get("thumb") or "")
        if thumb and not Path(thumb).is_file():
            thumb = p if m == "wallhaven" else ""
        elif not thumb and m == "wallhaven":
            thumb = p
        history.append({
            "id": neutralize_markup(item.get("id") or Path(p).stem, 40),
            "path": p,
            "thumb": thumb,
            "title": neutralize_markup(item.get("title") or Path(p).stem, 48),
            "mode": m,
            "resolution": neutralize_markup(item.get("resolution") or "", 24),
            "tag": clean_tag(item.get("tag") or ""),
            "timestamp": int(item.get("timestamp") or 0),
        })
        if len(history) >= MAX_HISTORY:
            break

    cur = current_background(raw.get("currentPath"), resolve_backend(backend_setting))
    history = seed_existing_backgrounds(history, cur)
    try:
        hist_idx = int(raw.get("historyIndex", 0))
    except (TypeError, ValueError):
        hist_idx = 0
    if cur:
        for i, h in enumerate(history):
            if h["path"] == cur:
                hist_idx = i
                break
    hist_idx = max(0, min(hist_idx, max(0, len(history) - 1)))

    return {
        "mode": mode,
        "running": running,
        "periodTags": period_tags,
        "periodColors": period_colors,
        "tagCursor": tag_cursor,
        "scheduleMinutes": sched,
        "shuffle": shuffle,
        "history": history,
        "historyIndex": hist_idx,
        "backend": backend_setting,
        "currentPath": cur,
    }


def save_state(state):
    prepare_storage()
    write_private(STATE_FILE, json.dumps(state, indent=2, ensure_ascii=False))


def update_state(mutator):
    """Atomically reload, mutate, and save state.json under an exclusive file lock."""
    prepare_storage()
    lock_path = STATE_DIR / ".state.lock"
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            latest = load_state()
            mutator(latest)
            save_state(latest)
            return latest
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def status_response(state=None):
    if state is None:
        state = load_state()
    px_key = read_key("pixabay")
    wh_key = read_key("wallhaven")
    period = current_period()
    backend = resolve_backend(state.get("backend", "auto"))
    return {
        "ok": True,
        "mode": state["mode"],
        "running": state["running"],
        "currentPeriod": period,
        "periodTags": state["periodTags"],
        "periodColors": state["periodColors"],
        "tags": state["periodTags"].get(period, []),
        "scheduleMinutes": state["scheduleMinutes"],
        "shuffle": state["shuffle"],
        "history": state["history"][:MAX_HISTORY],
        "historyIndex": state["historyIndex"],
        "currentPath": current_background(state.get("currentPath"), backend),
        "theme": theme_slug(),
        "hasPixabayKey": valid_pixabay_key(px_key),
        "hasWallhavenKey": valid_wallhaven_key(wh_key),
        "desktop": desktop_name(),
        "backend": backend,
        "backendSetting": state.get("backend", "auto"),
        "detectedBackend": detect_backend(),
        "availableBackends": available_backends(),
        "liveEngine": detect_live_engine(backend),
    }


def wallpaper_dir(backend):
    if backend == "omarchy":
        return BG_ROOT / theme_slug()
    return GENERIC_WALL_DIR


# ------------------------------------------------------------ desktop backends
def _desktop_tokens(env):
    raw = ":".join(
        str(env.get(k) or "") for k in ("XDG_CURRENT_DESKTOP", "XDG_SESSION_DESKTOP", "DESKTOP_SESSION")
    ).lower()
    return {t for t in re.split(r"[:;,\s/]+", raw) if t}


def _is_wayland(env):
    return bool(env.get("WAYLAND_DISPLAY")) or str(env.get("XDG_SESSION_TYPE") or "").lower() == "wayland"


def _first_tool(names, which=None):
    which = which or shutil.which
    for n in names:
        if which(n):
            return n
    return ""


def is_omarchy(which=None):
    which = which or shutil.which
    return bool(which("omarchy")) and (OMARCHY_SHARE_DIR.is_dir() or CURRENT_BG_LINK.parent.is_dir())


def desktop_name(env=None):
    """Human-readable desktop/session label for status output."""
    env = os.environ if env is None else env
    cur = neutralize_markup(env.get("XDG_CURRENT_DESKTOP") or env.get("XDG_SESSION_DESKTOP")
                            or env.get("DESKTOP_SESSION") or "", 40)
    if cur:
        return cur
    if env.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return "Hyprland"
    if env.get("SWAYSOCK"):
        return "sway"
    if _is_wayland(env):
        return "Wayland"
    if env.get("DISPLAY"):
        return "X11"
    return "unknown"


def detect_backend(env=None, which=None):
    """Pick the best wallpaper backend for the running desktop, or "none"."""
    env = os.environ if env is None else env
    which = which or shutil.which
    has = lambda b: bool(_first_tool(BACKEND_TOOLS[b], which))
    tokens = _desktop_tokens(env)

    if is_omarchy(which):
        return "omarchy"
    if tokens & {"kde", "plasma", "plasmawayland", "plasmax11"} and has("kde"):
        return "kde"
    if tokens & {"cinnamon", "x-cinnamon"} and has("cinnamon"):
        return "cinnamon"
    if "mate" in tokens and has("mate"):
        return "mate"
    if tokens & {"xfce", "xfce4", "xubuntu"} and has("xfce"):
        return "xfce"
    if "lxqt" in tokens and has("lxqt"):
        return "lxqt"
    if tokens & {"lxde", "lubuntu"} and has("lxde"):
        return "lxde"
    if tokens & {"gnome", "gnome-classic", "gnome-xorg", "ubuntu", "unity", "budgie",
                 "budgie-desktop", "pantheon", "pop", "zorin", "deepin"} and has("gnome"):
        return "gnome"

    if env.get("HYPRLAND_INSTANCE_SIGNATURE") or "hyprland" in tokens:
        for b in ("swww", "hyprpaper", "swaybg"):
            if has(b):
                return b
    if env.get("SWAYSOCK") or "sway" in tokens:
        for b in ("swww", "sway", "swaybg"):
            if has(b):
                return b
    if _is_wayland(env):
        # river, niri, wayfire, labwc, Hyprland without hyprpaper, ...
        for b in ("swww", "swaybg"):
            if has(b):
                return b
    if env.get("DISPLAY"):
        # i3, bspwm, openbox, awesome, dwm, xmonad, ...
        for b in ("feh", "xwallpaper", "nitrogen", "hsetroot"):
            if has(b):
                return b
    return "none"


def available_backends(which=None):
    which = which or shutil.which
    out_list = []
    for b in BACKENDS:
        if b == "omarchy":
            if is_omarchy(which):
                out_list.append(b)
        elif _first_tool(BACKEND_TOOLS[b], which):
            out_list.append(b)
    return out_list


def resolve_backend(setting="auto", env=None, which=None):
    env = os.environ if env is None else env
    override = str(env.get("WALLROTATOR_BACKEND") or "").strip().lower()
    if override in BACKENDS:
        return override
    if setting in BACKENDS:
        return setting
    return detect_backend(env, which)


def detect_live_engine(backend, env=None, which=None):
    env = os.environ if env is None else env
    which = which or shutil.which
    override = str(env.get("WALLROTATOR_LIVE_ENGINE") or "").strip().lower()
    if override in LIVE_ENGINES:
        return override
    if backend == "omarchy":
        return "omarchy"
    tokens = _desktop_tokens(env)
    if _is_wayland(env):
        # mpvpaper needs wlr-layer-shell, which GNOME's compositor does not provide
        if which("mpvpaper") and not (tokens & {"gnome", "ubuntu", "unity", "pantheon", "budgie", "pop"}):
            return "mpvpaper"
    elif env.get("DISPLAY") and which("xwinwrap") and which("mpv"):
        return "xwinwrap"
    return "frame"


def _run(cmd, timeout=15):
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
        )
        return r.returncode == 0, r.stdout or ""
    except (OSError, subprocess.TimeoutExpired):
        return False, ""


def _pid_file(name):
    return STATE_DIR / f"{name}.pid"


def _proc_name(pid):
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        pass
    ok, stdout = _run(["ps", "-p", str(pid), "-o", "comm="], timeout=5)
    return Path(stdout.strip()).name if ok else ""


def _kill_tracked(name, expected):
    """Stop a detached helper we started earlier, only if the pid still belongs to it."""
    pf = _pid_file(name)
    try:
        pid = int(pf.read_text().strip())
    except (OSError, ValueError):
        pf.unlink(missing_ok=True)
        return
    pf.unlink(missing_ok=True)
    if pid <= 1 or _proc_name(pid) not in expected:
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def _spawn_tracked(name, cmd):
    """Start a long-running helper (swaybg, mpvpaper, xwinwrap) detached from us."""
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return False
    time.sleep(0.4)
    if proc.poll() is not None:
        return False
    try:
        write_private(_pid_file(name), f"{proc.pid}\n")
    except OSError:
        pass
    return True


def stop_live():
    _kill_tracked("live", {"mpvpaper", "xwinwrap", "mpv"})


def safe_alias(path):
    """Return a path free of characters that command-string parsers would interpret."""
    p = str(path)
    if SAFE_PATH_RE.fullmatch(p):
        return p
    links = CACHE_DIR / "links"
    private_dir(links)
    ext = re.sub(r"[^a-z0-9.]", "", Path(p).suffix.lower())[:6]
    link = links / (hashlib.sha256(p.encode()).hexdigest()[:20] + ext)
    try:
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(p)
    except OSError:
        return ""
    s = str(link)
    return s if SAFE_PATH_RE.fullmatch(s) else ""


def _gsettings(schema, key, value):
    return _run(["gsettings", "set", schema, key, value])[0]


def _set_kde(path):
    if shutil.which("plasma-apply-wallpaperimage"):
        if _run(["plasma-apply-wallpaperimage", path], timeout=30)[0]:
            return True
    qdbus = _first_tool(("qdbus6", "qdbus"))
    if not qdbus:
        return False
    # json.dumps produces a safely escaped JS string literal
    script = (
        "var ds = desktops(); for (var i = 0; i < ds.length; i++) {"
        " var d = ds[i]; d.wallpaperPlugin = 'org.kde.image';"
        " d.currentConfigGroup = ['Wallpaper', 'org.kde.image', 'General'];"
        f" d.writeConfig('Image', {json.dumps(Path(path).as_uri())}); }}"
    )
    return _run([qdbus, "org.kde.plasmashell", "/PlasmaShell",
                 "org.kde.PlasmaShell.evaluateScript", script], timeout=30)[0]


def _set_xfce(path):
    ok, listing = _run(["xfconf-query", "-c", "xfce4-desktop", "-l"])
    props = [ln.strip() for ln in listing.splitlines() if ln.strip().endswith("/last-image")] if ok else []
    if not props:
        props = ["/backdrop/screen0/monitor0/workspace0/last-image"]
    any_ok = False
    for prop in props:
        if _run(["xfconf-query", "-c", "xfce4-desktop", "-p", prop, "-n", "-t", "string", "-s", path])[0]:
            any_ok = True
        elif _run(["xfconf-query", "-c", "xfce4-desktop", "-p", prop, "-s", path])[0]:
            any_ok = True
    return any_ok


def _set_swww(path):
    tool = _first_tool(("awww", "swww"))
    if not tool:
        return False
    if not _run([tool, "query"], timeout=5)[0]:
        daemon = f"{tool}-daemon"
        if not shutil.which(daemon) or not _spawn_tracked("swww-daemon", [daemon]):
            return False
        for _ in range(10):
            if _run([tool, "query"], timeout=5)[0]:
                break
            time.sleep(0.3)
    return _run([tool, "img", path, "--transition-type", "fade"], timeout=30)[0]


def _set_hyprpaper(path):
    safe = safe_alias(path)
    if not safe:
        return False
    if _run(["hyprctl", "hyprpaper", "reload", f",{safe}"])[0]:
        return True
    _run(["hyprctl", "hyprpaper", "preload", safe])
    ok = _run(["hyprctl", "hyprpaper", "wallpaper", f",{safe}"])[0]
    _run(["hyprctl", "hyprpaper", "unload", "unused"])
    return ok


def _set_sway(path):
    safe = safe_alias(path)
    if not safe:
        return False
    return _run(["swaymsg", "output", "*", "bg", safe, "fill"])[0]


def _set_swaybg(path):
    _kill_tracked("swaybg", {"swaybg"})
    return _spawn_tracked("swaybg", ["swaybg", "-m", "fill", "-i", path])


def set_static(backend, path):
    path = str(path)
    if backend == "gnome":
        uri = Path(path).as_uri()
        ok = _gsettings("org.gnome.desktop.background", "picture-uri", uri)
        _gsettings("org.gnome.desktop.background", "picture-uri-dark", uri)
        _gsettings("org.gnome.desktop.background", "picture-options", "zoom")
        return ok
    if backend == "cinnamon":
        ok = _gsettings("org.cinnamon.desktop.background", "picture-uri", Path(path).as_uri())
        _gsettings("org.cinnamon.desktop.background", "picture-options", "zoom")
        return ok
    if backend == "mate":
        ok = _gsettings("org.mate.background", "picture-filename", path)
        _gsettings("org.mate.background", "picture-options", "zoom")
        return ok
    if backend == "kde":
        return _set_kde(path)
    if backend == "xfce":
        return _set_xfce(path)
    if backend == "lxqt":
        return _run(["pcmanfm-qt", f"--set-wallpaper={path}", "--wallpaper-mode=zoom"])[0]
    if backend == "lxde":
        return _run(["pcmanfm", f"--set-wallpaper={path}", "--wallpaper-mode=crop"])[0]
    if backend == "hyprpaper":
        return _set_hyprpaper(path)
    if backend == "swww":
        return _set_swww(path)
    if backend == "sway":
        return _set_sway(path)
    if backend == "swaybg":
        return _set_swaybg(path)
    if backend == "feh":
        return _run(["feh", "--bg-fill", path])[0]
    if backend == "xwallpaper":
        return _run(["xwallpaper", "--zoom", path])[0]
    if backend == "nitrogen":
        return _run(["nitrogen", "--set-zoom-fill", "--save", path])[0]
    if backend == "hsetroot":
        return _run(["hsetroot", "-cover", path])[0]
    return False


def start_live(engine, path):
    stop_live()
    path = str(path)
    if engine == "mpvpaper":
        return _spawn_tracked("live", [
            "mpvpaper", "-o", "no-audio loop panscan=1.0 hwdec=auto", "ALL", path,
        ])
    if engine == "xwinwrap":
        return _spawn_tracked("live", [
            "xwinwrap", "-fs", "-ov", "-ni", "-nf", "-un", "-s", "-st", "-sp", "-b", "-fdt", "--",
            "mpv", "-wid", "WID", "--loop", "--no-audio", "--no-osc", "--no-osd-bar",
            "--no-input-default-bindings", "--panscan=1.0", "--hwdec=auto", path,
        ])
    return False


def video_still(video_path):
    sig = hashlib.sha256(str(video_path).encode()).hexdigest()[:16]
    return frame_thumb(video_path, CACHE_DIR / "frames" / f"still-{sig}.jpg", width=None)


def _set_omarchy(path):
    ok = _run(["omarchy", "theme", "bg", "set", str(path)], timeout=30)[0]
    if shutil.which("owe"):
        _run(["owe", "set", str(path)], timeout=10)
    return ok


def set_background(path, backend=None):
    """Apply a static image or live video using the resolved desktop backend."""
    if backend is None:
        backend = resolve_backend(_raw_state().get("backend", "auto"))
    if backend == "omarchy":
        return _set_omarchy(path)
    if backend not in BACKENDS:
        return False
    if Path(str(path)).suffix.lower() in VIDEO_EXTS:
        still = video_still(path)
        engine = detect_live_engine(backend)
        if engine in ("mpvpaper", "xwinwrap"):
            if still:
                set_static(backend, still)  # visible on lock screens / overview
            return start_live(engine, path)
        stop_live()
        return bool(still) and set_static(backend, still)
    stop_live()
    return set_static(backend, path)


# --------------------------------------------------------- Wallhaven & Pixabay
def search_wallhaven(tag, shuffle=False, color=""):
    prepare_storage()
    sorting = "random" if shuffle else "toplist"
    color = clean_color(color)
    params = {
        "q": tag or "nature",
        "categories": "111",
        "purity": "100",
        "atleast": "1920x1080",
        "ratios": "16x9,16x10",
        "sorting": sorting,
    }
    if color:
        params["colors"] = color
    wh_key = read_key("wallhaven")
    if valid_wallhaven_key(wh_key):
        params["apikey"] = wh_key

    cache_key = hashlib.sha256(f"wh\0{tag}\0{sorting}\0{color}".encode()).hexdigest()[:24]
    cache_file = SEARCH_DIR / f"{cache_key}.json"
    if not shuffle and cache_file.is_file():
        try:
            if time.time() - cache_file.stat().st_mtime < SEARCH_TTL:
                cached = json.loads(cache_file.read_text())
                if isinstance(cached, list) and cached:
                    return cached
        except (OSError, ValueError):
            pass

    url = WALLHAVEN_API + "?" + urllib.parse.urlencode(params)
    data = get_json(url, "Wallhaven")
    items = []
    for d in (data.get("data") or [])[:24]:
        if not isinstance(d, dict):
            continue
        wid = re.sub(r"[^a-z0-9]", "", str(d.get("id") or "").lower())[:16]
        full_url = str(d.get("path") or "")
        thumbs = d.get("thumbs") or {}
        thumb_url = str(thumbs.get("small") or thumbs.get("large") or "")
        if not wid or not host_ok(full_url, WALLHAVEN_MEDIA_HOSTS):
            continue
        if thumb_url and not host_ok(thumb_url, WALLHAVEN_THUMB_HOSTS):
            thumb_url = ""
        res = neutralize_markup(d.get("resolution") or "", 20)
        ext = Path(urllib.parse.urlparse(full_url).path).suffix.lower()
        if ext not in (".jpg", ".jpeg", ".png"):
            continue
        items.append({
            "id": wid,
            "url": full_url,
            "thumbUrl": thumb_url,
            "ext": ext,
            "resolution": res,
            "title": f"Wallhaven #{wid} ({tag})",
            "tag": tag,
        })
    if not items and color:
        # Fallback without color filter if tag+color combo had no hits
        return search_wallhaven(tag, shuffle=shuffle, color="")
    if not shuffle and items:
        try:
            write_private(cache_file, json.dumps(items))
        except OSError:
            pass
    return items


def download_image(url, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 0:
        return
    try:
        with _open(url) as r:
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype and ctype not in ("image/jpeg", "image/png", "application/octet-stream"):
                raise WRError("bad_type", ctype)
            data = _read_limited(r, MAX_IMAGE)
        if not (data.startswith(b"\xff\xd8\xff") or data.startswith(b"\x89PNG\r\n\x1a\n")):
            raise WRError("bad_type", "not JPEG/PNG")
        with tempfile.TemporaryDirectory(prefix=".wallrotator-", dir=str(dest.parent)) as tmpdir:
            part = Path(tmpdir) / "image.part"
            part.write_bytes(data)
            os.chmod(part, 0o644)
            try:
                os.link(part, dest)
            except FileExistsError:
                pass
    except WRError:
        raise
    except urllib.error.URLError as e:
        raise WRError("network", str(getattr(e, "reason", e)))
    except OSError as e:
        raise WRError("io", str(e.strerror or e))


def search_pixabay(tag, shuffle=False):
    prepare_storage()
    key = read_key("pixabay")
    if not valid_pixabay_key(key):
        raise WRError("no_key")

    cache_key = hashlib.sha256(f"px\0{tag}".encode()).hexdigest()[:24]
    cache_file = SEARCH_DIR / f"{cache_key}.json"
    if cache_file.is_file():
        try:
            if time.time() - cache_file.stat().st_mtime < SEARCH_TTL:
                cached = json.loads(cache_file.read_text())
                if isinstance(cached, list) and cached:
                    return cached
        except (OSError, ValueError):
            pass

    params = {
        "key": key,
        "q": tag or "nature",
        "per_page": 20,
        "safesearch": "true",
        "order": "popular",
    }
    url = PIXABAY_API + "?" + urllib.parse.urlencode(params)
    data = get_json(url, "Pixabay")
    items = []
    for h in (data.get("hits") or [])[:20]:
        if not isinstance(h, dict):
            continue
        try:
            vid = int(h.get("id") or 0)
        except (TypeError, ValueError):
            vid = 0
        if vid <= 0:
            continue
        vids = h.get("videos") or {}
        chosen_url, chosen_w, chosen_h = "", 0, 0
        for size in ("medium", "large", "small", "tiny"):
            v = vids.get(size) or {}
            u = str(v.get("url") or "")
            w = int(v.get("width") or 0)
            ht = int(v.get("height") or 0)
            if host_ok(u, PIXABAY_MEDIA_HOSTS) and w > 0 and ht > 0:
                chosen_url, chosen_w, chosen_h = u, w, ht
                break
        if not chosen_url:
            continue
        thumb_url = ""
        for size in ("small", "tiny", "medium"):
            t = str((vids.get(size) or {}).get("thumbnail") or "")
            if host_ok(t, PIXABAY_THUMB_HOSTS):
                thumb_url = t
                break
        raw_tags = str(h.get("tags") or "")
        title = neutralize_markup(", ".join(x.strip() for x in raw_tags.split(",")[:3]), 48)
        items.append({
            "id": str(vid),
            "url": chosen_url,
            "thumbUrl": thumb_url,
            "width": chosen_w,
            "height": chosen_h,
            "resolution": f"{chosen_w}x{chosen_h}",
            "title": title or f"Pixabay #{vid}",
            "tag": tag,
        })
    if items:
        try:
            write_private(cache_file, json.dumps(items))
        except OSError:
            pass
    return items


def download_video(url, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 0:
        return
    try:
        with tempfile.TemporaryDirectory(prefix=".wallrotator-", dir=str(dest.parent)) as tmpdir:
            part = Path(tmpdir) / "download.mp4"
            with _open(url) as r:
                ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype and ctype not in ("video/mp4", "application/octet-stream", "binary/octet-stream"):
                    raise WRError("bad_type", ctype)
                total = int(r.headers.get("Content-Length") or 0)
                if total > MAX_VIDEO:
                    raise WRError("too_big")
                done = 0
                with open(part, "wb") as f:
                    while True:
                        chunk = r.read(256 * 1024)
                        if not chunk:
                            break
                        done += len(chunk)
                        if done > MAX_VIDEO:
                            raise WRError("too_big")
                        f.write(chunk)
            with open(part, "rb") as f:
                head = f.read(12)
            if head[4:8] != b"ftyp":
                raise WRError("bad_type", "not MP4")
            final_file = part
            if shutil.which("ffmpeg"):
                muted = Path(tmpdir) / "muted.mp4"
                try:
                    r = subprocess.run(
                        [
                            "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
                            "-protocol_whitelist", "file", "-i", str(part),
                            "-c:v", "copy", "-an", str(muted),
                        ],
                        capture_output=True,
                        timeout=30,
                    )
                    if r.returncode == 0 and muted.is_file() and muted.stat().st_size > 0:
                        final_file = muted
                except (OSError, subprocess.TimeoutExpired):
                    pass
            os.chmod(final_file, 0o644)
            try:
                os.link(final_file, dest)
            except FileExistsError:
                pass
    except WRError:
        raise
    except urllib.error.URLError as e:
        raise WRError("network", str(getattr(e, "reason", e)))
    except OSError as e:
        raise WRError("io", str(e.strerror or e))


def pick_tag(state, period, force_shuffle=False):
    tags = state["periodTags"].get(period) or DEFAULT_PERIOD_TAGS.get(period) or ["nature"]
    if force_shuffle or state["shuffle"]:
        return random.choice(tags)
    idx = state["tagCursor"] % len(tags)
    state["tagCursor"] = (idx + 1) % len(tags)
    return tags[idx]


def fetch_and_apply_new(state, force_shuffle=False):
    use_shuffle = force_shuffle or state["shuffle"]
    period = current_period()
    tag = pick_tag(state, period, force_shuffle=use_shuffle)
    color = state["periodColors"].get(period, "")
    mode = state["mode"]
    backend = resolve_backend(state.get("backend", "auto"))
    if backend == "none":
        raise WRError("no_backend")
    dest_dir = wallpaper_dir(backend)
    dest_dir.mkdir(parents=True, exist_ok=True)

    seen_ids = {h.get("id") for h in state["history"]}

    if mode == "wallhaven":
        items = search_wallhaven(tag, shuffle=use_shuffle, color=color)
        if not items:
            raise WRError("not_found")
        unseen = [it for it in items if f"wallhaven-{it['id']}" not in seen_ids and it["id"] not in seen_ids]
        pool = unseen if unseen else items
        chosen = random.choice(pool) if use_shuffle else pool[0]
        dest = dest_dir / f"wallhaven-{chosen['id']}{chosen['ext']}"
        download_image(chosen["url"], dest)
        thumb = fetch_thumb("wallhaven", chosen["id"], chosen["thumbUrl"]) or str(dest)
        entry_id = f"wallhaven-{chosen['id']}"
    else:
        items = search_pixabay(tag, shuffle=use_shuffle)
        if not items:
            raise WRError("not_found")
        unseen = [it for it in items if f"pixabay-{it['id']}" not in seen_ids and it["id"] not in seen_ids]
        pool = unseen if unseen else items
        chosen = random.choice(pool) if use_shuffle else pool[0]
        dest = dest_dir / f"pixabay-{chosen['id']}-{chosen['height']}p.mp4"
        download_video(chosen["url"], dest)
        thumb = fetch_thumb("pixabay", chosen["id"], chosen["thumbUrl"])
        if not thumb:
            thumb = frame_thumb(dest, THUMB_DIR / f"pixabay-{chosen['id']}.jpg")
        entry_id = f"pixabay-{chosen['id']}"

    if not set_background(dest, backend):
        raise WRError("apply_failed", backend)

    entry = {
        "id": neutralize_markup(entry_id, 40),
        "path": str(dest),
        "thumb": thumb,
        "title": neutralize_markup(chosen["title"], 48),
        "mode": mode,
        "resolution": neutralize_markup(chosen["resolution"], 24),
        "tag": clean_tag(tag),
        "timestamp": int(time.time()),
    }
    new_tag_cursor = state["tagCursor"]

    def _commit(latest):
        latest["history"] = [entry] + [h for h in latest["history"] if h["path"] != str(dest)]
        latest["history"] = latest["history"][:MAX_HISTORY]
        latest["historyIndex"] = 0
        latest["tagCursor"] = new_tag_cursor
        latest["currentPath"] = str(dest)

    latest = update_state(_commit)
    return status_response(latest)


# ------------------------------------------------------------------ commands
def require_backend(state):
    backend = resolve_backend(state.get("backend", "auto"))
    if backend == "none":
        fail("no_backend")
    return backend


def cmd_status():
    out(status_response())


def cmd_set_backend(name):
    name = str(name or "").strip().lower()
    if name != "auto" and name not in BACKENDS:
        fail("usage")
    latest = update_state(lambda s: s.__setitem__("backend", name))
    out(status_response(latest))


def cmd_set_mode(mode):
    if mode not in VALID_MODES:
        fail("usage")
    latest = update_state(lambda s: s.__setitem__("mode", mode))
    out(status_response(latest))


def cmd_set_running(val_str):
    want = str(val_str).lower() in ("1", "true", "yes", "on", "start")

    def _mutate(s):
        s["running"] = want
        if want and s["scheduleMinutes"] == 0:
            s["scheduleMinutes"] = 30

    latest = update_state(_mutate)
    out(status_response(latest))


def cmd_set_schedule(minutes_str):
    try:
        m = int(minutes_str)
    except (TypeError, ValueError):
        fail("usage")
    if m not in VALID_SCHEDULES:
        fail("usage")

    def _mutate(s):
        s["scheduleMinutes"] = m
        s["running"] = m > 0

    latest = update_state(_mutate)
    out(status_response(latest))


def cmd_set_shuffle(val_str):
    want = str(val_str).lower() in ("1", "true", "yes", "on")
    latest = update_state(lambda s: s.__setitem__("shuffle", want))
    out(status_response(latest))


def cmd_add_tag(raw_tag, period=None):
    t = clean_tag(raw_tag)
    if not t:
        fail("usage")
    p = period if period in PERIODS else current_period()

    def _mutate(s):
        tags = s["periodTags"].get(p, [])
        if t not in tags:
            s["periodTags"][p] = (tags + [t])[:MAX_TAGS]

    latest = update_state(_mutate)
    out(status_response(latest))


def cmd_remove_tag(raw_tag, period=None):
    t = clean_tag(raw_tag)
    p = period if period in PERIODS else current_period()

    def _mutate(s):
        tags = s["periodTags"].get(p, [])
        s["periodTags"][p] = [x for x in tags if x != t]

    latest = update_state(_mutate)
    out(status_response(latest))


def cmd_set_color(period, raw_color):
    if period not in PERIODS:
        fail("usage")
    c = clean_color(raw_color)
    latest = update_state(lambda s: s["periodColors"].__setitem__(period, c))
    out(status_response(latest))


def cmd_set_key(provider):
    if provider not in VALID_MODES:
        fail("usage")
    key = sys.stdin.readline().strip()
    if provider == "pixabay":
        if not valid_pixabay_key(key):
            fail("bad_key_format")
        try:
            url = PIXABAY_API + "?" + urllib.parse.urlencode({"key": key, "q": "ocean", "per_page": 3})
            get_json(url, "Pixabay")
        except WRError as e:
            fail(e.code, e.detail)
    else:
        if not valid_wallhaven_key(key):
            fail("bad_key_format")
        try:
            url = WALLHAVEN_API + "?" + urllib.parse.urlencode({"apikey": key, "q": "nature"})
            get_json(url, "Wallhaven")
        except WRError as e:
            fail(e.code, e.detail)
    prepare_storage()
    write_private(STATE_DIR / f"{provider}.key", key + "\n")
    res = status_response()
    res["keySaved"] = provider
    out(res)


def cmd_next(force_shuffle=False):
    state = load_state()
    if not force_shuffle and state["historyIndex"] > 0 and state["historyIndex"] < len(state["history"]):
        new_idx = state["historyIndex"] - 1
        target = state["history"][new_idx]["path"]
        if Path(target).is_file():
            backend = require_backend(state)
            if not set_background(target, backend):
                fail("apply_failed", backend)

            def _commit_idx(latest):
                latest["currentPath"] = target
                for i, h in enumerate(latest["history"]):
                    if h["path"] == target:
                        latest["historyIndex"] = i
                        return
                latest["historyIndex"] = max(0, min(new_idx, max(0, len(latest["history"]) - 1)))

            latest = update_state(_commit_idx)
            out(status_response(latest))
            return
    try:
        out(fetch_and_apply_new(state, force_shuffle=force_shuffle))
    except WRError as e:
        fail(e.code, e.detail)


def cmd_prev():
    state = load_state()
    hist = state["history"]
    if not hist:
        fail("not_found")
    if len(hist) == 1:
        target_idx = 0
    else:
        target_idx = (state["historyIndex"] + 1) % len(hist)
    target = hist[target_idx]["path"]
    if not Path(target).is_file():
        fail("bad_path")
    backend = require_backend(state)
    if not set_background(target, backend):
        fail("apply_failed", backend)

    def _commit_idx(latest):
        latest["currentPath"] = target
        for i, h in enumerate(latest["history"]):
            if h["path"] == target:
                latest["historyIndex"] = i
                return
        latest["historyIndex"] = max(0, min(target_idx, max(0, len(latest["history"]) - 1)))

    latest = update_state(_commit_idx)
    out(status_response(latest))


def cmd_shuffle():
    state = update_state(lambda s: s.__setitem__("shuffle", True))
    try:
        out(fetch_and_apply_new(state, force_shuffle=True))
    except WRError as e:
        fail(e.code, e.detail)


def cmd_apply(path_arg):
    real = _resolve(path_arg)
    if not real or not real.is_file():
        fail("bad_path")
    bg_root_real = _resolve(BG_ROOT)
    theme_bg_real = _resolve(THEME_BG_DIR)
    pics_real = _resolve(PICTURES_ROTATE_DIR)
    generic_real = _resolve(GENERIC_WALL_DIR)
    state = load_state()
    in_history = any(h["path"] == str(real) for h in state["history"])
    allowed_roots = [r for r in (bg_root_real, theme_bg_real, pics_real, generic_real) if r is not None]
    if not in_history and not any(r in real.parents for r in allowed_roots):
        fail("bad_path")
    backend = require_backend(state)
    if not set_background(real, backend):
        fail("apply_failed", backend)

    def _commit_apply(latest):
        latest["currentPath"] = str(real)
        for i, h in enumerate(latest["history"]):
            if h["path"] == str(real):
                latest["historyIndex"] = i
                break

    latest = update_state(_commit_apply)
    out(status_response(latest))


def main(argv):
    if not argv:
        fail("usage")
    cmd, args = argv[0], argv[1:]
    if cmd == "status":
        cmd_status()
    elif cmd == "set-mode" and args:
        cmd_set_mode(args[0])
    elif cmd == "set-running" and args:
        cmd_set_running(args[0])
    elif cmd == "set-schedule" and args:
        cmd_set_schedule(args[0])
    elif cmd == "set-shuffle" and args:
        cmd_set_shuffle(args[0])
    elif cmd == "add-tag" and args:
        cmd_add_tag(args[0], args[1] if len(args) > 1 else None)
    elif cmd == "remove-tag" and args:
        cmd_remove_tag(args[0], args[1] if len(args) > 1 else None)
    elif cmd == "set-color" and len(args) >= 2:
        cmd_set_color(args[0], args[1])
    elif cmd == "set-key" and args:
        cmd_set_key(args[0])
    elif cmd == "set-backend" and args:
        cmd_set_backend(args[0])
    elif cmd == "next":
        cmd_next(force_shuffle="--shuffle" in args)
    elif cmd == "prev":
        cmd_prev()
    elif cmd == "shuffle":
        cmd_shuffle()
    elif cmd == "apply" and args:
        cmd_apply(args[0])
    else:
        fail("usage")


if __name__ == "__main__":
    main(sys.argv[1:])
