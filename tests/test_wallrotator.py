"""Unit and security regression tests for ric-v.wallrotator.

Run from the plugin folder:
  python3 -m unittest discover -s tests
"""

import importlib.util
import io
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("wallrotator", ROOT / "wallrotator.py")
wr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wr)


WALLHAVEN_SAMPLE = {
    "data": [
        {
            "id": "w5wxq7",
            "path": "https://w.wallhaven.cc/full/w5/wallhaven-w5wxq7.jpg",
            "resolution": "2560x1440",
            "thumbs": {
                "small": "https://th.wallhaven.cc/small/w5/w5wxq7.jpg",
                "large": "https://th.wallhaven.cc/lg/w5/w5wxq7.jpg",
            },
        }
    ]
}

PIXABAY_SAMPLE = {
    "hits": [
        {
            "id": 998877,
            "tags": "cyberpunk, city, <img src=x>",
            "videos": {
                "medium": {
                    "url": "https://cdn.pixabay.com/v/998877_medium.mp4",
                    "width": 1920,
                    "height": 1080,
                    "thumbnail": "https://cdn.pixabay.com/v/998877_medium.jpg",
                }
            },
        }
    ]
}


class SecurityAndSafetyTests(unittest.TestCase):
    def test_neutralize_markup_strips_html_and_control_chars(self):
        dirty = '<img src="https://evil.invalid/t"><b>Hello & "world"</b>\x00\n'
        cleaned = wr.neutralize_markup(dirty)
        self.assertNotIn("<", cleaned)
        self.assertNotIn(">", cleaned)
        self.assertNotIn("&", cleaned)
        self.assertNotIn('"', cleaned)
        self.assertIn("Hello", cleaned)

    def test_host_allowlist_enforces_https_and_exact_hosts(self):
        self.assertTrue(wr.host_ok("https://w.wallhaven.cc/full/w5/wallhaven-w5wxq7.jpg", wr.WALLHAVEN_MEDIA_HOSTS))
        self.assertFalse(wr.host_ok("http://w.wallhaven.cc/full/w5/wallhaven-w5wxq7.jpg", wr.WALLHAVEN_MEDIA_HOSTS))
        self.assertFalse(wr.host_ok("https://evil.example.com/wallhaven.jpg", wr.WALLHAVEN_MEDIA_HOSTS))

    def test_qml_uses_plaintext_and_neutralizes_dynamic_sinks(self):
        qml = (ROOT / "Panel.qml").read_text()
        self.assertIn("component PlainText: Text", qml)
        self.assertIn("textFormat: Text.PlainText", qml)
        # Ensure no raw Text { is used outside the PlainText component definition
        raw_texts = re.findall(r"\bText\s*\{", qml)
        self.assertEqual(len(raw_texts), 1, "Only component PlainText: Text { should use raw Text")
        self.assertIn("function neutralizeMarkup", qml)


class StateAndCommandsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(wr, "STATE_DIR", base / "state"),
            mock.patch.object(wr, "STATE_FILE", base / "state/state.json"),
            mock.patch.object(wr, "CACHE_DIR", base / "cache"),
            mock.patch.object(wr, "THUMB_DIR", base / "cache/thumbs"),
            mock.patch.object(wr, "SEARCH_DIR", base / "cache/search"),
            mock.patch.object(wr, "BG_ROOT", base / "backgrounds"),
            mock.patch.object(wr, "THEME_BG_DIR", base / "theme_bgs"),
            mock.patch.object(wr, "PICTURES_ROTATE_DIR", base / "pics_rotate"),
            mock.patch.object(wr, "THEME_NAME_FILE", base / "theme.name"),
            mock.patch.object(wr, "CURRENT_BG_LINK", base / "current_bg"),
            mock.patch.object(wr, "GENERIC_WALL_DIR", base / "wallpapers"),
            mock.patch.object(wr, "OMARCHY_SHARE_DIR", base / "omarchy_share"),
            mock.patch.dict(os.environ, {"WALLROTATOR_BACKEND": "feh", "WALLROTATOR_LIVE_ENGINE": ""}),
        ]
        for p in self.patches:
            p.start()
        (base / "theme.name").write_text("catppuccin\n")

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def run_json(self, fn, *args):
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            fn(*args)
        return json.loads(buf.getvalue().strip().splitlines()[-1])

    def test_time_of_day_periods_and_per_period_tags_and_colors(self):
        self.assertEqual(wr.current_period(7), "morning")
        self.assertEqual(wr.current_period(11), "morning")
        self.assertEqual(wr.current_period(12), "noon")
        self.assertEqual(wr.current_period(14), "noon")
        self.assertEqual(wr.current_period(15), "evening")
        self.assertEqual(wr.current_period(17), "evening")
        self.assertEqual(wr.current_period(18), "night")
        self.assertEqual(wr.current_period(0), "night")
        self.assertEqual(wr.current_period(6), "night")

        res = self.run_json(wr.cmd_set_running, "true")
        self.assertTrue(res["running"])
        res = self.run_json(wr.cmd_set_running, "false")
        self.assertFalse(res["running"])

        res = self.run_json(wr.cmd_add_tag, "cherry blossom", "morning")
        self.assertIn("cherry blossom", res["periodTags"]["morning"])
        res = self.run_json(wr.cmd_remove_tag, "sunrise", "morning")
        self.assertNotIn("sunrise", res["periodTags"]["morning"])

        res = self.run_json(wr.cmd_set_color, "evening", "ff6600")
        self.assertEqual(res["periodColors"]["evening"], "ff6600")
        res = self.run_json(wr.cmd_set_color, "evening", "none")
        self.assertEqual(res["periodColors"]["evening"], "")

    def test_mode_schedule_and_tags_roundtrip(self):
        res = self.run_json(wr.cmd_set_mode, "pixabay")
        self.assertEqual(res["mode"], "pixabay")

        res = self.run_json(wr.cmd_set_schedule, "30")
        self.assertEqual(res["scheduleMinutes"], 30)
        self.assertTrue(res["running"])

        res = self.run_json(wr.cmd_add_tag, "<script>anime</script>", "night")
        self.assertIn("script anime script", res["periodTags"]["night"])

        res = self.run_json(wr.cmd_remove_tag, "cyberpunk", "night")
        self.assertNotIn("cyberpunk", res["periodTags"]["night"])

    def test_recent_10_history_cap_and_prev_next_navigation(self):
        bg_dir = wr.BG_ROOT / "catppuccin"
        bg_dir.mkdir(parents=True, exist_ok=True)
        state = wr.load_state()
        history = []
        for i in range(12):
            f = bg_dir / f"wall-{i}.jpg"
            f.write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
            history.append({
                "id": f"wall-{i}",
                "path": str(f),
                "thumb": str(f),
                "title": f"Wall {i}",
                "mode": "wallhaven",
                "resolution": "1920x1080",
                "tag": "nature",
                "timestamp": 1000 + i,
            })
        state["history"] = history
        state["historyIndex"] = 0
        wr.save_state(state)

        loaded = wr.load_state()
        self.assertEqual(len(loaded["history"]), 10)

        with mock.patch.object(wr, "set_background", return_value=True):
            prev_res = self.run_json(wr.cmd_prev)
            self.assertEqual(prev_res["historyIndex"], 1)
            next_res = self.run_json(wr.cmd_next)
            self.assertEqual(next_res["historyIndex"], 0)

    def test_set_key_for_wallhaven_and_pixabay(self):
        with mock.patch.object(wr, "get_json", return_value={"data": []}), \
             mock.patch("sys.stdin", io.StringIO("wh_secret_token_1234567890\n")):
            res = self.run_json(wr.cmd_set_key, "wallhaven")
            self.assertTrue(res["hasWallhavenKey"])
            self.assertEqual(res["keySaved"], "wallhaven")
            self.assertEqual((wr.STATE_DIR / "wallhaven.key").stat().st_mode & 0o777, 0o600)

        with mock.patch.object(wr, "get_json", return_value={"hits": []}), \
             mock.patch("sys.stdin", io.StringIO("12345678-abcdef1234567890abcdef\n")):
            res = self.run_json(wr.cmd_set_key, "pixabay")
            self.assertTrue(res["hasPixabayKey"])
            self.assertEqual(res["keySaved"], "pixabay")
            self.assertEqual((wr.STATE_DIR / "pixabay.key").stat().st_mode & 0o777, 0o600)

    def test_download_image_preserves_theme_dir_permissions(self):
        bg_dir = wr.BG_ROOT / "catppuccin"
        bg_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(bg_dir, 0o755)
        dest = bg_dir / "wallhaven-abc123.jpg"

        class FakeResp:
            headers = {"Content-Type": "image/jpeg"}
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self, n=-1): return b"\xff\xd8\xff\xe0" + b"0" * 32

        with mock.patch.object(wr, "_open", return_value=FakeResp()):
            wr.download_image("https://w.wallhaven.cc/full/ab/wallhaven-abc123.jpg", dest)

        self.assertTrue(dest.is_file())
        self.assertEqual(bg_dir.stat().st_mode & 0o777, 0o755)

    def test_download_video_muting_does_not_overwrite_or_delete_existing_muted_file(self):
        bg_dir = wr.BG_ROOT / "catppuccin"
        bg_dir.mkdir(parents=True, exist_ok=True)
        existing_muted = bg_dir / "pixabay-99-1080p.muted.mp4"
        existing_muted.write_bytes(b"USER_OWNED_DATA")
        dest = bg_dir / "pixabay-99-1080p.mp4"

        class FakeVideoResp:
            headers = {"Content-Type": "video/mp4", "Content-Length": "32"}
            def __init__(self): self._done = False
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self, n=-1):
                if self._done: return b""
                self._done = True
                return b"\x00\x00\x00\x18ftypmp42" + b"0" * 20

        def fake_ffmpeg(cmd, **kwargs):
            out_path = Path(cmd[-1])
            out_path.write_bytes(b"\x00\x00\x00\x18ftypmp42MUTED")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch.object(wr, "_open", return_value=FakeVideoResp()), \
             mock.patch.object(wr.shutil, "which", return_value="/usr/bin/ffmpeg"), \
             mock.patch.object(wr.subprocess, "run", side_effect=fake_ffmpeg):
            wr.download_video("https://cdn.pixabay.com/v/99.mp4", dest)

        self.assertTrue(dest.is_file())
        self.assertTrue(existing_muted.is_file())
        self.assertEqual(existing_muted.read_bytes(), b"USER_OWNED_DATA")

    def test_concurrent_fetch_preserves_saved_stop_and_off_schedule(self):
        # Start with active schedule (running=True, scheduleMinutes=30)
        self.run_json(wr.cmd_set_schedule, "30")
        stale_state = wr.load_state()
        self.assertTrue(stale_state["running"])
        self.assertEqual(stale_state["scheduleMinutes"], 30)

        fake_item = [{
            "id": "z9y8x7",
            "url": "https://w.wallhaven.cc/full/z9/wallhaven-z9y8x7.jpg",
            "thumbUrl": "",
            "ext": ".jpg",
            "resolution": "1920x1080",
            "title": "Wallhaven #z9y8x7 (nature)",
            "tag": "nature",
        }]

        def fake_download_image(url, dest):
            # Simulate another monitor/instance clicking Off while this fetch is in flight
            self.run_json(wr.cmd_set_schedule, "0")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"\xff\xd8\xff\xe0fakejpeg")

        with mock.patch.object(wr, "search_wallhaven", return_value=fake_item), \
             mock.patch.object(wr, "download_image", side_effect=fake_download_image), \
             mock.patch.object(wr, "fetch_thumb", return_value=""), \
             mock.patch.object(wr, "set_background", return_value=True):
            result = wr.fetch_and_apply_new(stale_state)

        self.assertFalse(result["running"])
        self.assertEqual(result["scheduleMinutes"], 0)
        self.assertEqual(result["history"][0]["id"], "wallhaven-z9y8x7")

        persisted = wr.load_state()
        self.assertFalse(persisted["running"])
        self.assertEqual(persisted["scheduleMinutes"], 0)
        self.assertEqual(persisted["history"][0]["id"], "wallhaven-z9y8x7")

    def test_qml_queues_schedule_changes_during_fetch(self):
        qml = (ROOT / "Panel.qml").read_text()
        self.assertIn("function queueCommand", qml)
        self.assertIn("pendingSchedule", qml)
        self.assertIn("pendingRunning", qml)
        self.assertIn('root.queueCommand(["set-schedule", String(m)], "set-schedule")', qml)

    def test_catalog_error_codes_complete(self):
        en = json.loads((ROOT / "i18n/en.json").read_text())
        src = (ROOT / "wallrotator.py").read_text()
        codes = set(re.findall(r'(?:fail|WRError)\("([a-z_]+)"', src))
        for code in codes:
            self.assertIn(f"error.{code}", en, f"Missing error.{code} in en.json")

    def test_set_backend_validates_and_persists(self):
        res = self.run_json(wr.cmd_set_backend, "gnome")
        self.assertEqual(res["backendSetting"], "gnome")
        res = self.run_json(wr.cmd_set_backend, "auto")
        self.assertEqual(res["backendSetting"], "auto")
        with self.assertRaises(SystemExit), mock.patch("sys.stdout", io.StringIO()):
            wr.cmd_set_backend("rm -rf")

    def test_generic_desktop_downloads_to_generic_dir_and_tracks_current(self):
        fake_item = [{
            "id": "abc123", "url": "https://w.wallhaven.cc/full/ab/wallhaven-abc123.jpg",
            "thumbUrl": "", "ext": ".jpg", "resolution": "1920x1080",
            "title": "Wallhaven #abc123 (nature)", "tag": "nature",
        }]

        def fake_download_image(url, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"\xff\xd8\xff\xe0fakejpeg")

        with mock.patch.object(wr, "search_wallhaven", return_value=fake_item), \
             mock.patch.object(wr, "download_image", side_effect=fake_download_image), \
             mock.patch.object(wr, "fetch_thumb", return_value=""), \
             mock.patch.object(wr, "set_background", return_value=True) as setter:
            res = wr.fetch_and_apply_new(wr.load_state())

        expected = wr.GENERIC_WALL_DIR / "wallhaven-abc123.jpg"
        setter.assert_called_once_with(expected, "feh")
        self.assertEqual(res["currentPath"], str(expected))
        self.assertEqual(wr.load_state()["currentPath"], str(expected))
        # Generic wallpaper dir is an allowed apply root
        with mock.patch.object(wr, "set_background", return_value=True):
            res = self.run_json(wr.cmd_apply, str(expected))
        self.assertEqual(res["currentPath"], str(expected))

    def test_no_backend_fails_before_downloading(self):
        with mock.patch.dict(os.environ, {"WALLROTATOR_BACKEND": ""}), \
             mock.patch.object(wr, "detect_backend", return_value="none"), \
             mock.patch.object(wr, "search_wallhaven") as search:
            with self.assertRaises(wr.WRError) as ctx:
                wr.fetch_and_apply_new(wr.load_state())
        self.assertEqual(ctx.exception.code, "no_backend")
        search.assert_not_called()

    def test_gnome_backend_sets_file_uri(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        img = Path(self.tmp.name) / "my wall.jpg"
        img.write_bytes(b"x")
        with mock.patch.object(wr.subprocess, "run", side_effect=fake_run):
            self.assertTrue(wr.set_background(img, "gnome"))
        self.assertIn(["gsettings", "set", "org.gnome.desktop.background", "picture-uri", img.as_uri()], calls)
        self.assertIn(["gsettings", "set", "org.gnome.desktop.background", "picture-uri-dark", img.as_uri()], calls)

    def test_command_string_backends_never_see_unsafe_paths(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        evil = Path(self.tmp.name) / "a; exec rm -rf ~, x.jpg"
        evil.write_bytes(b"x")
        with mock.patch.object(wr.subprocess, "run", side_effect=fake_run):
            self.assertTrue(wr.set_background(evil, "sway"))
            self.assertTrue(wr.set_background(evil, "hyprpaper"))
        for cmd in calls:
            for arg in cmd[1:]:
                if arg in ("*",):
                    continue
                self.assertRegex(arg.lstrip(","), r"^[A-Za-z0-9._/+-]+$")
        alias = Path(calls[0][4])
        self.assertTrue(alias.is_symlink())
        self.assertEqual(alias.resolve(), evil.resolve())

    def test_video_without_live_engine_falls_back_to_still_frame(self):
        video = Path(self.tmp.name) / "pixabay-1-1080p.mp4"
        video.write_bytes(b"\x00\x00\x00\x18ftypmp42")
        still = Path(self.tmp.name) / "still.jpg"
        still.write_bytes(b"\xff\xd8\xff")
        with mock.patch.object(wr, "video_still", return_value=str(still)), \
             mock.patch.object(wr, "detect_live_engine", return_value="frame"), \
             mock.patch.object(wr, "set_static", return_value=True) as static, \
             mock.patch.object(wr, "start_live") as live:
            self.assertTrue(wr.set_background(video, "kde"))
        static.assert_called_once_with("kde", str(still))
        live.assert_not_called()

    def test_kill_tracked_ignores_unrelated_pid(self):
        wr.write_private(wr._pid_file("live"), f"{os.getpid()}\n")
        with mock.patch.object(wr.os, "killpg") as killpg, mock.patch.object(wr.os, "kill") as kill:
            wr.stop_live()
        killpg.assert_not_called()
        kill.assert_not_called()
        self.assertFalse(wr._pid_file("live").exists())


class DesktopDetectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(wr, "OMARCHY_SHARE_DIR", base / "nope"),
            mock.patch.object(wr, "CURRENT_BG_LINK", base / "nope/current/background"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    @staticmethod
    def which_for(*tools):
        return lambda name: f"/usr/bin/{name}" if name in tools else None

    def test_desktop_environments(self):
        cases = [
            ({"XDG_CURRENT_DESKTOP": "GNOME"}, ("gsettings",), "gnome"),
            ({"XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}, ("gsettings",), "gnome"),
            ({"XDG_CURRENT_DESKTOP": "Budgie:GNOME"}, ("gsettings",), "gnome"),
            ({"XDG_CURRENT_DESKTOP": "KDE"}, ("plasma-apply-wallpaperimage", "gsettings"), "kde"),
            ({"XDG_CURRENT_DESKTOP": "X-Cinnamon"}, ("gsettings",), "cinnamon"),
            ({"XDG_CURRENT_DESKTOP": "MATE"}, ("gsettings",), "mate"),
            ({"XDG_CURRENT_DESKTOP": "XFCE"}, ("xfconf-query",), "xfce"),
            ({"XDG_CURRENT_DESKTOP": "LXQt"}, ("pcmanfm-qt",), "lxqt"),
            ({"XDG_CURRENT_DESKTOP": "Hyprland", "WAYLAND_DISPLAY": "wayland-1"}, ("hyprctl", "swww"), "swww"),
            ({"XDG_CURRENT_DESKTOP": "Hyprland", "WAYLAND_DISPLAY": "wayland-1"}, ("hyprctl", "awww"), "swww"),
            ({"HYPRLAND_INSTANCE_SIGNATURE": "x", "WAYLAND_DISPLAY": "wayland-1"}, ("hyprctl",), "hyprpaper"),
            ({"XDG_CURRENT_DESKTOP": "sway", "SWAYSOCK": "/run/sway"}, ("swaymsg", "swaybg"), "sway"),
            ({"XDG_CURRENT_DESKTOP": "niri", "WAYLAND_DISPLAY": "wayland-1"}, ("swaybg",), "swaybg"),
            ({"XDG_CURRENT_DESKTOP": "i3", "DISPLAY": ":0"}, ("feh", "nitrogen"), "feh"),
            ({"DISPLAY": ":0"}, ("xwallpaper",), "xwallpaper"),
            ({"DISPLAY": ":0"}, (), "none"),
            ({}, ("feh",), "none"),
        ]
        for env, tools, expected in cases:
            with self.subTest(env=env, tools=tools):
                self.assertEqual(wr.detect_backend(env, self.which_for(*tools)), expected)

    def test_omarchy_takes_priority_when_installed(self):
        (Path(self.tmp.name) / "nope").mkdir()
        env = {"XDG_CURRENT_DESKTOP": "Hyprland", "WAYLAND_DISPLAY": "wayland-1"}
        self.assertEqual(wr.detect_backend(env, self.which_for("omarchy", "swww")), "omarchy")

    def test_override_and_setting_precedence(self):
        which = self.which_for("gsettings", "feh")
        env = {"XDG_CURRENT_DESKTOP": "GNOME"}
        self.assertEqual(wr.resolve_backend("auto", env, which), "gnome")
        self.assertEqual(wr.resolve_backend("feh", env, which), "feh")
        self.assertEqual(wr.resolve_backend("feh", {**env, "WALLROTATOR_BACKEND": "xfce"}, which), "xfce")
        self.assertEqual(wr.resolve_backend("bogus", env, which), "gnome")

    def test_live_engine_selection(self):
        wl = {"XDG_CURRENT_DESKTOP": "sway", "WAYLAND_DISPLAY": "wayland-1"}
        self.assertEqual(wr.detect_live_engine("omarchy", wl, self.which_for()), "omarchy")
        self.assertEqual(wr.detect_live_engine("sway", wl, self.which_for("mpvpaper")), "mpvpaper")
        gnome_wl = {"XDG_CURRENT_DESKTOP": "GNOME", "WAYLAND_DISPLAY": "wayland-0"}
        self.assertEqual(wr.detect_live_engine("gnome", gnome_wl, self.which_for("mpvpaper")), "frame")
        x11 = {"XDG_CURRENT_DESKTOP": "XFCE", "DISPLAY": ":0"}
        self.assertEqual(wr.detect_live_engine("xfce", x11, self.which_for("xwinwrap", "mpv")), "xwinwrap")
        self.assertEqual(wr.detect_live_engine("xfce", x11, self.which_for("mpv")), "frame")


class TrayAppStaticTests(unittest.TestCase):
    def test_tray_script_compiles_and_strings_exist(self):
        src = (ROOT / "wallrotator_tray.py").read_text()
        compile(src, "wallrotator_tray.py", "exec")
        en = json.loads((ROOT / "i18n/en.json").read_text())
        keys = set(re.findall(r'\btr?n?\("([a-zA-Z0-9_.]+)"', src))
        self.assertTrue(keys)
        for key in keys:
            self.assertIn(key, en, f"Missing {key} in en.json")

    def test_tray_never_uses_a_shell(self):
        src = (ROOT / "wallrotator_tray.py").read_text()
        self.assertNotIn("shell=True", src)
        self.assertNotIn("os.system", src)


if __name__ == "__main__":
    unittest.main()
