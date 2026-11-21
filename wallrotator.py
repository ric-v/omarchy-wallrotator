#!/usr/bin/env python3
"""Backend for the ric-v.wallrotator Omarchy shell plugin.

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
  wallrotator.py next
  wallrotator.py prev
  wallrotator.py shuffle
  wallrotator.py apply <path>
"""

import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HOME = Path.home()
STATE_DIR = HOME / ".local/state/wallrotator"
LIVEWALL_STATE_DIR = HOME / ".local/state/livewall"
STATE_FILE = STATE_DIR / "state.json"
THEME_NAME_FILE = HOME / ".local/state/omarchy/current/theme.name"
CURRENT_BG_LINK = HOME / ".local/state/omarchy/current/background"
THEME_BG_DIR = HOME / ".local/state/omarchy/current/theme/backgrounds"
BG_ROOT = HOME / ".config/omarchy/backgrounds"
PICTURES_ROTATE_DIR = HOME / "Pictures/wallrotate"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", HOME / ".cache")) / "wallrotator"
THUMB_DIR = CACHE_DIR / "thumbs"
SEARCH_DIR = CACHE_DIR / "search"

VERSION = "1.1.0"
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


def current_background():
    if CURRENT_BG_LINK.is_symlink() or CURRENT_BG_LINK.is_file():
        r = _resolve(CURRENT_BG_LINK)
        if r and r.is_file():
            return str(r)
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


def frame_thumb(video_path, dest):
    if dest.is_file() and dest.stat().st_size > 0:
        return str(dest)
    if not shutil.which("ffmpeg"):
        return ""
    prepare_storage()
    tmp = dest.with_name(dest.stem + ".tmp.jpg")
    try:
        subprocess.run(
            [
                "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
                "-protocol_whitelist", "file", "-ss", "1.0",
                "-i", str(video_path), "-frames:v", "1",
                "-vf", "scale=480:-2", "-q:v", "4", str(tmp),
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


def seed_existing_backgrounds(existing_history):
    """Ensure recent history includes existing theme backgrounds if fewer than MAX_HISTORY."""
    seen = {h.get("path") for h in existing_history if h.get("path")}
    seeded = list(existing_history)
    if len(seeded) >= MAX_HISTORY:
        return seeded[:MAX_HISTORY]

    theme = theme_slug()
    dirs = [BG_ROOT / theme, THEME_BG_DIR, PICTURES_ROTATE_DIR]
    if BG_ROOT.is_dir():
        for sub in sorted(BG_ROOT.iterdir()):
            if sub.is_dir() and sub not in dirs:
                dirs.append(sub)
    candidates = []
    cur = current_background()
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


def load_state():
    prepare_storage()
    raw = {}
    if STATE_FILE.is_file():
        try:
            raw = json.loads(STATE_FILE.read_text())
            if not isinstance(raw, dict):
                raw = {}
        except (OSError, ValueError):
            raw = {}

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

    history = seed_existing_backgrounds(history)

    cur = current_background()
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
    }


def save_state(state):
    prepare_storage()
    write_private(STATE_FILE, json.dumps(state, indent=2, ensure_ascii=False))


@contextlib.contextmanager
def state_lock():
    """Serialize state read-modify-write cycles across processes."""
    prepare_storage()
    fd = os.open(STATE_DIR / "state.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing releases the flock


def commit_fetch(fetched):
    """Persist a finished fetch's history/cursor onto the latest saved state.

    A fetch can run for a long time; Stop/Off (running, scheduleMinutes) or
    other preferences may have been saved meanwhile by another widget instance,
    so reload under the lock and only merge what the fetch owns.
    """
    with state_lock():
        latest = load_state()
        latest["history"] = fetched["history"]
        latest["historyIndex"] = fetched["historyIndex"]
        latest["tagCursor"] = fetched["tagCursor"]
        save_state(latest)
        return latest


def status_response(state=None):
    if state is None:
        state = load_state()
    px_key = read_key("pixabay")
    wh_key = read_key("wallhaven")
    period = current_period()
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
        "currentPath": current_background(),
        "theme": theme_slug(),
        "hasPixabayKey": valid_pixabay_key(px_key),
        "hasWallhavenKey": valid_wallhaven_key(wh_key),
    }


def set_background(path):
    ok = False
    try:
        r = subprocess.run(
            ["omarchy", "theme", "bg", "set", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        ok = r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    if shutil.which("owe"):
        try:
            subprocess.run(
                ["owe", "set", str(path)],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    return ok


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
    theme = theme_slug()
    dest_dir = BG_ROOT / theme
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

    if not set_background(dest):
        raise WRError("apply_failed")

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
    state["history"] = [entry] + [h for h in state["history"] if h["path"] != str(dest)]
    state["history"] = state["history"][:MAX_HISTORY]
    state["historyIndex"] = 0
    return status_response(commit_fetch(state))


# ------------------------------------------------------------------ commands
def cmd_status():
    out(status_response())


def cmd_set_mode(mode):
    if mode not in VALID_MODES:
        fail("usage")
    state = load_state()
    state["mode"] = mode
    save_state(state)
    out(status_response(state))


def cmd_set_running(val_str):
    want = str(val_str).lower() in ("1", "true", "yes", "on", "start")
    with state_lock():
        state = load_state()
        state["running"] = want
        if want and state["scheduleMinutes"] == 0:
            state["scheduleMinutes"] = 30
        save_state(state)
    out(status_response(state))


def cmd_set_schedule(minutes_str):
    try:
        m = int(minutes_str)
    except (TypeError, ValueError):
        fail("usage")
    if m not in VALID_SCHEDULES:
        fail("usage")
    with state_lock():
        state = load_state()
        state["scheduleMinutes"] = m
        state["running"] = m > 0
        save_state(state)
    out(status_response(state))


def cmd_set_shuffle(val_str):
    state = load_state()
    state["shuffle"] = str(val_str).lower() in ("1", "true", "yes", "on")
    save_state(state)
    out(status_response(state))


def cmd_add_tag(raw_tag, period=None):
    t = clean_tag(raw_tag)
    if not t:
        fail("usage")
    p = period if period in PERIODS else current_period()
    state = load_state()
    tags = state["periodTags"].get(p, [])
    if t not in tags:
        state["periodTags"][p] = (tags + [t])[:MAX_TAGS]
        save_state(state)
    out(status_response(state))


def cmd_remove_tag(raw_tag, period=None):
    t = clean_tag(raw_tag)
    p = period if period in PERIODS else current_period()
    state = load_state()
    tags = state["periodTags"].get(p, [])
    state["periodTags"][p] = [x for x in tags if x != t]
    save_state(state)
    out(status_response(state))


def cmd_set_color(period, raw_color):
    if period not in PERIODS:
        fail("usage")
    c = clean_color(raw_color)
    state = load_state()
    state["periodColors"][period] = c
    save_state(state)
    out(status_response(state))


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
            if not set_background(target):
                fail("apply_failed")
            state["historyIndex"] = new_idx
            save_state(state)
            out(status_response(state))
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
    if not set_background(target):
        fail("apply_failed")
    state["historyIndex"] = target_idx
    save_state(state)
    out(status_response(state))


def cmd_shuffle():
    state = load_state()
    state["shuffle"] = True
    save_state(state)
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
    state = load_state()
    in_history = any(h["path"] == str(real) for h in state["history"])
    allowed_roots = [r for r in (bg_root_real, theme_bg_real, pics_real) if r is not None]
    if not in_history and not any(r in real.parents for r in allowed_roots):
        fail("bad_path")
    if not set_background(real):
        fail("apply_failed")
    for i, h in enumerate(state["history"]):
        if h["path"] == str(real):
            state["historyIndex"] = i
            break
    save_state(state)
    out(status_response(state))


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
