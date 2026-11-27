#!/usr/bin/env python3
"""Wallpaper Rotator — system tray app for any Linux/BSD desktop.

Runs in the system tray (StatusNotifierItem / XEmbed) of KDE Plasma, GNOME
(with the AppIndicator extension), XFCE, Cinnamon, MATE, LXQt, Budgie,
Hyprland/sway/niri bars (waybar, etc.), i3/bspwm trays, and others.

All work is done by wallrotator.py in the same folder, so state, API keys and
history are shared with the Omarchy shell plugin.

Usage:
  wallrotator_tray.py                 start the tray (or show the window if already running)
  wallrotator_tray.py <command>       control the running tray, e.g. from a keybinding:
                                      show | hide | toggle | next | prev | shuffle |
                                      start | stop | toggle-running | quit
  wallrotator_tray.py --autostart on|off
  wallrotator_tray.py --help

Requires Python 3 and one Qt binding: PySide6, PyQt6 or PyQt5.
"""

import importlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
BACKEND = APP_DIR / "wallrotator.py"
I18N_FILE = APP_DIR / "i18n" / "en.json"
ICON_FILE = APP_DIR / "icons" / "wallrotator.svg"
HOME = Path.home()
STATE_DIR = HOME / ".local/state/wallrotator"
CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config")
AUTOSTART_FILE = CONFIG_HOME / "autostart" / "wallrotator-tray.desktop"

COMMANDS = ("show", "hide", "toggle", "next", "prev", "shuffle", "start", "stop", "toggle-running", "quit")
# What to do for a command when no tray instance is running
DIRECT_BACKEND = {
    "next": ["next"],
    "prev": ["prev"],
    "shuffle": ["shuffle"],
    "start": ["set-running", "true"],
    "stop": ["set-running", "false"],
}
PERIODS = ("morning", "noon", "evening", "night")
SCHEDULES = (0, 5, 15, 30, 60, 120)
BACKENDS = (
    "omarchy", "gnome", "cinnamon", "mate", "kde", "xfce", "lxqt", "lxde",
    "hyprpaper", "swww", "sway", "swaybg", "feh", "xwallpaper", "nitrogen", "hsetroot",
)
WALLHAVEN_PALETTE = (
    "cc0000", "ff6600", "ff9900", "ffcc33", "ffff00",
    "77cc33", "336600", "66cccc", "0099cc", "0066cc",
    "333399", "663399", "993399", "ea4c88", "424153",
    "000000", "999999", "ffffff",
)
LIGHT_SWATCHES = {"ffffff", "ffff00", "ffcc33", "cccccc", "66cccc", "999999"}


# ------------------------------------------------------------------ strings
def _load_strings():
    try:
        data = json.loads(I18N_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


STRINGS = _load_strings()


def t(key, *args):
    val = STRINGS.get(key, key)
    if isinstance(val, dict):
        val = val.get("other", key)
    val = str(val)
    for i, a in enumerate(args, 1):
        val = val.replace(f"%{i}", str(a))
    return val


def tn(key, n, *args):
    val = STRINGS.get(key, key)
    if isinstance(val, dict):
        val = val.get("one" if n == 1 else "other", key)
    val = str(val).replace("%1", str(n))
    for i, a in enumerate(args, 2):
        val = val.replace(f"%{i}", str(a))
    return val


def neutralize(value, max_len=120):
    s = re.sub(r"[<>&\"'`\x00-\x1f\x7f]", " ", str(value or ""))
    return re.sub(r"\s+", " ", s).strip()[:max_len]


# ---------------------------------------------------------------- autostart
def _desktop_exec_arg(arg):
    """Quote one argument for a .desktop Exec= line (Desktop Entry spec)."""
    if "\n" in arg or "\r" in arg:
        raise ValueError("newline in path")
    arg = arg.replace("%", "%%")
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", arg):
        return arg
    quoted = '"' + re.sub(r'(["`$\\])', r"\\\1", arg) + '"'
    # Exec is a string value, so every backslash is escaped once more
    return quoted.replace("\\", "\\\\")


def launch_command():
    return [sys.executable, str(Path(__file__).resolve())]


def desktop_entry(exec_args, autostart=False):
    icon = str(ICON_FILE) if ICON_FILE.is_file() else "preferences-desktop-wallpaper"
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        "Name=Wallpaper Rotator",
        "GenericName=Wallpaper Changer",
        "Comment=Rotate Wallhaven and Pixabay wallpapers from the system tray",
        "Exec=" + " ".join(_desktop_exec_arg(a) for a in exec_args),
        "Icon=" + icon.replace("\\", "\\\\"),
        "Terminal=false",
        "Categories=Utility;DesktopSettings;",
        "Keywords=wallpaper;background;rotate;wallhaven;pixabay;",
        "StartupNotify=false",
    ]
    if autostart:
        lines += ["X-GNOME-Autostart-enabled=true", "X-KDE-autostart-after=panel"]
    return "\n".join(lines) + "\n"


def autostart_enabled():
    return AUTOSTART_FILE.is_file()


def set_autostart(enabled):
    if enabled:
        AUTOSTART_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = AUTOSTART_FILE.with_name(AUTOSTART_FILE.name + ".part")
        tmp.write_text(desktop_entry(launch_command(), autostart=True), encoding="utf-8")
        os.replace(tmp, AUTOSTART_FILE)
    else:
        AUTOSTART_FILE.unlink(missing_ok=True)


# -------------------------------------------------------------- single instance
def socket_path():
    candidates = []
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and Path(runtime).is_dir():
        candidates.append(str(Path(runtime) / "wallrotator-tray.sock"))
    candidates.append(str(STATE_DIR / "tray.sock"))
    for c in candidates:
        if len(c.encode()) < 104:  # sun_path limit
            return c
    return candidates[0]


def send_to_running(cmd):
    """Send a command to a running tray instance. Returns True if one answered."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2)
    try:
        s.connect(socket_path())
        s.sendall(cmd.encode("ascii") + b"\n")
        s.shutdown(socket.SHUT_WR)
        s.recv(16)
        return True
    except OSError:
        return False
    finally:
        s.close()


def run_backend_direct(args):
    r = subprocess.run([sys.executable, "-I", str(BACKEND), *args])
    return r.returncode


# ---------------------------------------------------------------------- Qt
def _import_qt():
    order = ("PySide6", "PyQt6", "PyQt5")
    forced = os.environ.get("WALLROTATOR_QT", "")
    if forced in order:
        order = (forced,)
    for name in order:
        try:
            mods = [importlib.import_module(f"{name}.{m}") for m in ("QtCore", "QtGui", "QtWidgets", "QtNetwork")]
            return (name, *mods)
        except ImportError:
            continue
    return (None, None, None, None, None)


QT_API, QtCore, QtGui, QtWidgets, QtNetwork = _import_qt()


def qe(owner, scope, name):
    """Enum lookup that works with Qt6 scoped enums and PyQt5 flat enums."""
    scoped = getattr(owner, scope, None)
    if scoped is not None and hasattr(scoped, name):
        return getattr(scoped, name)
    return getattr(owner, name)


def qexec(obj, *args):
    fn = getattr(obj, "exec", None) or getattr(obj, "exec_")
    return fn(*args)


_QWidget = QtWidgets.QWidget if QtWidgets else object
_QLayout = QtWidgets.QLayout if QtWidgets else object
QAction = (getattr(QtGui, "QAction", None) or getattr(QtWidgets, "QAction", None)) if QtWidgets else None
QActionGroup = (getattr(QtGui, "QActionGroup", None) or getattr(QtWidgets, "QActionGroup", None)) if QtWidgets else None


def make_icon(running=False, busy=False):
    """Draw the tray icon (no SVG plugin needed) with a status dot."""
    icon = QtGui.QIcon()
    aa = qe(QtGui.QPainter, "RenderHint", "Antialiasing")
    no_pen = qe(QtCore.Qt, "PenStyle", "NoPen")
    for size in (16, 22, 24, 32, 48, 64, 128):
        pm = QtGui.QPixmap(size, size)
        pm.fill(QtGui.QColor(0, 0, 0, 0))
        p = QtGui.QPainter(pm)
        p.setRenderHint(aa, True)
        m = size * 0.06
        frame = QtCore.QRectF(m, m + size * 0.08, size - 2 * m, size - 2 * m - size * 0.16)
        grad = QtGui.QLinearGradient(frame.topLeft(), frame.bottomRight())
        grad.setColorAt(0.0, QtGui.QColor("#4f8cff"))
        grad.setColorAt(1.0, QtGui.QColor("#9b5cff"))
        p.setPen(no_pen)
        p.setBrush(QtGui.QBrush(grad))
        p.drawRoundedRect(frame, size * 0.14, size * 0.14)
        # sun
        p.setBrush(QtGui.QColor("#ffd166"))
        r = size * 0.1
        p.drawEllipse(QtCore.QPointF(frame.left() + frame.width() * 0.72, frame.top() + frame.height() * 0.3), r, r)
        # mountains
        p.setBrush(QtGui.QColor(255, 255, 255, 235))
        b = frame.bottom()
        poly = QtGui.QPolygonF([
            QtCore.QPointF(frame.left(), b),
            QtCore.QPointF(frame.left() + frame.width() * 0.33, frame.top() + frame.height() * 0.38),
            QtCore.QPointF(frame.left() + frame.width() * 0.55, frame.top() + frame.height() * 0.68),
            QtCore.QPointF(frame.left() + frame.width() * 0.7, frame.top() + frame.height() * 0.52),
            QtCore.QPointF(frame.right(), b),
        ])
        p.drawPolygon(poly)
        if running or busy:
            d = size * 0.4
            dot = QtCore.QRectF(size - d - size * 0.01, size - d - size * 0.01, d, d)
            p.setBrush(QtGui.QColor("#ffffff"))
            p.drawEllipse(dot)
            p.setBrush(QtGui.QColor("#f39c12" if busy else "#2ecc71"))
            inset = d * 0.17
            p.drawEllipse(dot.adjusted(inset, inset, -inset, -inset))
        p.end()
        icon.addPixmap(pm)
    return icon


class FlowLayout(_QLayout):
    """Wrapping layout for tag chips and palette swatches."""

    def __init__(self, parent=None, spacing=6):
        super().__init__(parent)
        self._items = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._arrange(QtCore.QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QtCore.QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def clear(self):
        while self._items:
            item = self._items.pop()
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self.invalidate()

    def _arrange(self, rect, test_only):
        x, y, line_h = rect.x(), rect.y(), 0
        for item in self._items:
            if item.widget() is not None and item.widget().isHidden():
                continue
            hint = item.sizeHint()
            next_x = x + hint.width() + self._spacing
            if next_x - self._spacing > rect.right() + 1 and line_h > 0:
                x = rect.x()
                y += line_h + self._spacing
                next_x = x + hint.width() + self._spacing
                line_h = 0
            if not test_only:
                item.setGeometry(QtCore.QRect(QtCore.QPoint(x, y), hint))
            x = next_x
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y()


class ControlWindow(_QWidget):
    """Full control panel, equivalent to the Omarchy popup."""

    def __init__(self, ctrl):
        super().__init__()
        self.ctrl = ctrl
        self.selected_period = None
        self._tags_sig = None
        self._recent_sig = None
        self._thumbs = {}
        self.minimize_on_close = False

        self.setWindowTitle(t("panel.title"))
        self.setWindowIcon(make_icon())
        self.resize(480, 760)

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(qe(QtCore.Qt, "ScrollBarPolicy", "ScrollBarAlwaysOff"))
        scroll.setFrameShape(qe(QtWidgets.QFrame, "Shape", "NoFrame"))
        outer.addWidget(scroll)
        body = QtWidgets.QWidget()
        scroll.setWidget(body)
        col = QtWidgets.QVBoxLayout(body)
        col.setContentsMargins(16, 14, 16, 16)
        col.setSpacing(10)

        # Header
        head = QtWidgets.QHBoxLayout()
        titles = QtWidgets.QVBoxLayout()
        titles.setSpacing(2)
        title = QtWidgets.QLabel(t("panel.title"))
        f = title.font()
        f.setBold(True)
        f.setPointSizeF(f.pointSizeF() * 1.35)
        title.setFont(f)
        self.status_label = self._caption("")
        self.status_label.setWordWrap(False)
        titles.addWidget(title)
        titles.addWidget(self.status_label)
        head.addLayout(titles)
        head.addStretch(1)
        self.run_btn = QtWidgets.QPushButton()
        self.run_btn.clicked.connect(lambda *_: self.ctrl.toggle_running())
        head.addWidget(self.run_btn)
        col.addLayout(head)

        self.hint_label = self._caption(t("tray.noTray"))
        self.hint_label.hide()
        col.addWidget(self.hint_label)
        self.error_label = self._caption("")
        self.error_label.setStyleSheet("color: #e5484d;")
        self.error_label.hide()
        col.addWidget(self.error_label)

        # Mode + key
        col.addWidget(self._separator())
        col.addWidget(self._section(t("section.mode")))
        mode_row = QtWidgets.QHBoxLayout()
        self.mode_group = QtWidgets.QButtonGroup(self)
        self.mode_buttons = {}
        for value, label in (("wallhaven", t("mode.wallhaven")), ("pixabay", t("mode.pixabay"))):
            b = self._segment(QtWidgets.QPushButton(label))
            b.setCheckable(True)
            b.clicked.connect(lambda *_, v=value: self.ctrl.set_mode(v))
            self.mode_group.addButton(b)
            self.mode_buttons[value] = b
            mode_row.addWidget(b)
        col.addLayout(mode_row)
        key_row = QtWidgets.QHBoxLayout()
        self.key_label = self._caption("")
        self.key_label.setWordWrap(True)
        key_row.addWidget(self.key_label, 1)
        self.key_btn = QtWidgets.QPushButton()
        self.key_btn.clicked.connect(lambda *_: self.ask_key())
        key_row.addWidget(self.key_btn)
        col.addLayout(key_row)

        # Actions
        col.addWidget(self._separator())
        col.addWidget(self._section(t("section.actions")))
        act_row = QtWidgets.QHBoxLayout()
        self.prev_btn = QtWidgets.QPushButton("⏮  " + t("action.prev"))
        self.prev_btn.setToolTip(t("action.prevTip"))
        self.prev_btn.clicked.connect(lambda *_: self.ctrl.prev())
        self.next_btn = QtWidgets.QPushButton(t("action.next") + "  ⏭")
        self.next_btn.setToolTip(t("action.nextTip"))
        self.next_btn.clicked.connect(lambda *_: self.ctrl.next())
        self.shuffle_btn = QtWidgets.QPushButton("🔀  " + t("action.shuffle"))
        self.shuffle_btn.clicked.connect(lambda *_: self.ctrl.shuffle())
        for b in (self.prev_btn, self.next_btn, self.shuffle_btn):
            act_row.addWidget(self._segment(b))
        col.addLayout(act_row)
        self.shuffle_check = QtWidgets.QCheckBox(t("tray.shuffleToggle"))
        self.shuffle_check.clicked.connect(lambda checked=False: self.ctrl.set_shuffle(bool(checked)))
        col.addWidget(self.shuffle_check)

        # Time-of-day tags & palette
        col.addWidget(self._separator())
        tag_head = QtWidgets.QHBoxLayout()
        tag_head.addWidget(self._section(t("section.tags")))
        tag_head.addStretch(1)
        self.period_info = self._caption("")
        tag_head.addWidget(self.period_info)
        col.addLayout(tag_head)
        period_row = QtWidgets.QHBoxLayout()
        self.period_group = QtWidgets.QButtonGroup(self)
        self.period_buttons = {}
        for p in PERIODS:
            b = self._segment(QtWidgets.QPushButton(t(f"period.{p}")))
            b.setCheckable(True)
            b.setToolTip(t(f"period.{p}Range"))
            b.clicked.connect(lambda *_, v=p: self.pick_period(v))
            self.period_group.addButton(b)
            self.period_buttons[p] = b
            period_row.addWidget(b)
        col.addLayout(period_row)

        card = QtWidgets.QFrame()
        card.setObjectName("periodCard")
        card.setStyleSheet(
            "#periodCard { border: 1px solid rgba(127,127,127,0.35); border-radius: 8px;"
            " background: rgba(127,127,127,0.06); }"
        )
        cardl = QtWidgets.QVBoxLayout(card)
        cardl.setContentsMargins(10, 10, 10, 10)
        cardl.setSpacing(8)
        self.tags_sublabel = self._caption("", bold=True)
        cardl.addWidget(self.tags_sublabel)
        chips_host = QtWidgets.QWidget()
        self.chips = FlowLayout(chips_host, spacing=6)
        cardl.addWidget(chips_host)
        self.tags_empty = self._caption("")
        cardl.addWidget(self.tags_empty)
        add_row = QtWidgets.QHBoxLayout()
        self.tag_input = QtWidgets.QLineEdit()
        self.tag_input.setMaxLength(28)
        self.tag_input.returnPressed.connect(self.add_tag)
        self.add_btn = QtWidgets.QPushButton("+ " + t("tags.add"))
        self.add_btn.clicked.connect(lambda *_: self.add_tag())
        add_row.addWidget(self.tag_input, 1)
        add_row.addWidget(self.add_btn)
        cardl.addLayout(add_row)
        cardl.addWidget(self._separator())

        self.palette_box = QtWidgets.QWidget()
        pal = QtWidgets.QVBoxLayout(self.palette_box)
        pal.setContentsMargins(0, 0, 0, 0)
        pal.setSpacing(6)
        pal_head = QtWidgets.QHBoxLayout()
        pal_head.addWidget(self._caption(t("palette.label"), bold=True))
        pal_head.addStretch(1)
        self.palette_value = self._caption("")
        pal_head.addWidget(self.palette_value)
        pal.addLayout(pal_head)
        swatch_host = QtWidgets.QWidget()
        swatches = FlowLayout(swatch_host, spacing=5)
        self.any_btn = QtWidgets.QPushButton(t("palette.any"))
        self.any_btn.setCheckable(True)
        self.any_btn.setToolTip(t("palette.anyTip"))
        self.any_btn.clicked.connect(lambda *_: self.ctrl.set_color(self.period(), ""))
        swatches.addWidget(self.any_btn)
        self.swatches = {}
        for hexv in WALLHAVEN_PALETTE:
            sb = QtWidgets.QToolButton()
            sb.setCheckable(True)
            sb.setFixedSize(24, 24)
            sb.setToolTip(t("palette.colorTip", hexv.upper()))
            mark = "#000" if hexv in LIGHT_SWATCHES else "#fff"
            sb.setStyleSheet(
                f"QToolButton {{ background: #{hexv}; border: 1px solid rgba(127,127,127,0.6);"
                f" border-radius: 5px; color: {mark}; font-weight: bold; }}"
                "QToolButton:hover { border: 2px solid palette(highlight); }"
                "QToolButton:checked { border: 2px solid palette(highlight); }"
            )
            sb.clicked.connect(lambda *_, v=hexv: self.ctrl.set_color(
                self.period(), "" if self.ctrl.color_for(self.period()) == v else v))
            swatches.addWidget(sb)
            self.swatches[hexv] = sb
        pal.addWidget(swatch_host)
        cardl.addWidget(self.palette_box)
        self.pixabay_note = self._caption(t("palette.pixabayNote"))
        cardl.addWidget(self.pixabay_note)
        col.addWidget(card)

        # Schedule
        col.addWidget(self._separator())
        col.addWidget(self._section(t("section.schedule")))
        sched_row = QtWidgets.QHBoxLayout()
        self.sched_group = QtWidgets.QButtonGroup(self)
        self.sched_buttons = {}
        labels = {0: "schedule.off", 5: "schedule.5m", 15: "schedule.15m", 30: "schedule.30m",
                  60: "schedule.1h", 120: "schedule.2h"}
        for m in SCHEDULES:
            b = self._segment(QtWidgets.QPushButton(t(labels[m])))
            b.setCheckable(True)
            b.clicked.connect(lambda *_, v=m: self.ctrl.set_schedule(v))
            self.sched_group.addButton(b)
            self.sched_buttons[m] = b
            sched_row.addWidget(b)
        col.addLayout(sched_row)
        col.addWidget(self._caption(t("schedule.disclosure")))

        # Desktop integration
        col.addWidget(self._separator())
        col.addWidget(self._section(t("section.desktop")))
        self.desktop_label = self._caption("")
        col.addWidget(self.desktop_label)
        be_row = QtWidgets.QHBoxLayout()
        be_row.addWidget(QtWidgets.QLabel(t("desktop.backendLabel")))
        self.backend_combo = QtWidgets.QComboBox()
        self.backend_combo.setSizeAdjustPolicy(
            qe(QtWidgets.QComboBox, "SizeAdjustPolicy", "AdjustToMinimumContentsLengthWithIcon"))
        self.backend_combo.setMinimumContentsLength(12)
        self.backend_combo.activated.connect(self._backend_chosen)
        be_row.addWidget(self.backend_combo, 1)
        col.addLayout(be_row)
        self.autostart_check = QtWidgets.QCheckBox(t("tray.autostart"))
        self.autostart_check.clicked.connect(lambda checked=False: self.ctrl.set_autostart(bool(checked)))
        col.addWidget(self.autostart_check)

        # Recent wallpapers
        col.addWidget(self._separator())
        rec_head = QtWidgets.QHBoxLayout()
        rec_head.addWidget(self._section(t("section.recent")))
        rec_head.addStretch(1)
        self.recent_count = self._caption("")
        rec_head.addWidget(self.recent_count)
        col.addLayout(rec_head)
        self.recent_empty = self._caption(t("recent.empty"))
        col.addWidget(self.recent_empty)
        self.recent = QtWidgets.QListWidget()
        self.recent.setViewMode(qe(QtWidgets.QListView, "ViewMode", "IconMode"))
        self.recent.setResizeMode(qe(QtWidgets.QListView, "ResizeMode", "Adjust"))
        self.recent.setMovement(qe(QtWidgets.QListView, "Movement", "Static"))
        self.recent.setWrapping(True)
        self.recent.setWordWrap(True)
        self.recent.setUniformItemSizes(True)
        self.recent.setSpacing(4)
        self.recent.setIconSize(QtCore.QSize(184, 104))
        self.recent.setGridSize(QtCore.QSize(196, 148))
        self.recent.setFrameShape(qe(QtWidgets.QFrame, "Shape", "NoFrame"))
        self.recent.setStyleSheet("QListWidget { background: transparent; }")
        self.recent.setVerticalScrollBarPolicy(qe(QtCore.Qt, "ScrollBarPolicy", "ScrollBarAlwaysOff"))
        self.recent.itemClicked.connect(self._recent_clicked)
        col.addWidget(self.recent)
        col.addStretch(1)

    # ---- helpers
    @staticmethod
    def _segment(button):
        """Let a row of buttons share the width instead of forcing the window wider."""
        button.setMinimumWidth(0)
        button.setSizePolicy(qe(QtWidgets.QSizePolicy, "Policy", "Ignored"),
                             qe(QtWidgets.QSizePolicy, "Policy", "Fixed"))
        return button

    def _caption(self, text, bold=False):
        lab = QtWidgets.QLabel(text)
        lab.setTextFormat(qe(QtCore.Qt, "TextFormat", "PlainText"))
        lab.setWordWrap(True)
        f = lab.font()
        f.setPointSizeF(f.pointSizeF() * 0.92)
        f.setBold(bold)
        lab.setFont(f)
        if not bold:
            pal = lab.palette()
            role = qe(QtGui.QPalette, "ColorRole", "WindowText")
            c = pal.color(role)
            c.setAlphaF(0.7)
            pal.setColor(role, c)
            lab.setPalette(pal)
        return lab

    def _section(self, text):
        lab = QtWidgets.QLabel(text)
        lab.setTextFormat(qe(QtCore.Qt, "TextFormat", "PlainText"))
        f = lab.font()
        f.setBold(True)
        f.setPointSizeF(f.pointSizeF() * 0.85)
        f.setLetterSpacing(qe(QtGui.QFont, "SpacingType", "PercentageSpacing"), 108)
        lab.setFont(f)
        return lab

    def _separator(self):
        line = QtWidgets.QFrame()
        line.setFrameShape(qe(QtWidgets.QFrame, "Shape", "HLine"))
        line.setFrameShadow(qe(QtWidgets.QFrame, "Shadow", "Sunken"))
        return line

    def period(self):
        return self.selected_period or self.ctrl.state.get("currentPeriod", "night")

    def pick_period(self, p):
        self.selected_period = p
        self.render()

    def add_tag(self):
        raw = neutralize(self.tag_input.text()).lower()
        clean = re.sub(r"[^a-z0-9 _+-]", "", raw).strip()
        if clean:
            self.tag_input.clear()
            self.ctrl.add_tag(clean, self.period())

    def ask_key(self):
        provider = "Pixabay" if self.ctrl.state.get("mode") == "pixabay" else "Wallhaven"
        text, ok = QtWidgets.QInputDialog.getText(
            self, t("key.dialogTitle", provider), t("key.dialogLabel", provider),
            qe(QtWidgets.QLineEdit, "EchoMode", "Password"),
        )
        key = str(text or "").strip()
        if ok and key:
            self.ctrl.set_key(self.ctrl.state.get("mode", "wallhaven"), key)

    def _backend_chosen(self, index):
        value = self.backend_combo.itemData(index)
        if value:
            self.ctrl.set_backend(str(value))

    def _recent_clicked(self, item):
        path = item.data(qe(QtCore.Qt, "ItemDataRole", "UserRole"))
        if path:
            self.ctrl.apply(str(path))

    def thumb(self, path):
        if not path:
            return None
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            return None
        key = (path, mtime)
        if key in self._thumbs:
            return self._thumbs[key]
        w, h = 184, 104
        reader = QtGui.QImageReader(path)
        reader.setAutoTransform(True)
        size = reader.size()
        if size.isValid() and size.width() > 0 and size.height() > 0:
            scale = max(w / size.width(), h / size.height())
            reader.setScaledSize(QtCore.QSize(max(w, round(size.width() * scale)), max(h, round(size.height() * scale))))
        img = reader.read()
        icon = None
        if not img.isNull():
            img = img.copy(max(0, (img.width() - w) // 2), max(0, (img.height() - h) // 2), w, h)
            icon = QtGui.QIcon(QtGui.QPixmap.fromImage(img))
        self._thumbs[key] = icon
        return icon

    def showEvent(self, event):
        self.selected_period = None
        self.render()
        super().showEvent(event)

    def closeEvent(self, event):
        event.ignore()
        if self.minimize_on_close:
            self.showMinimized()
        else:
            self.hide()

    # ---- state -> widgets
    def render(self):
        st = self.ctrl.state
        busy = self.ctrl.busy
        running = bool(st.get("running"))
        mode = st.get("mode", "wallhaven")
        cur_period = st.get("currentPeriod", "night")
        period = self.period()

        status = t("panel.busy") if busy else (t("status.running") if running else t("status.stopped"))
        self.status_label.setText(f"{status} · {t(f'period.{cur_period}')} ({t(f'period.{cur_period}Range')})")
        self.run_btn.setText(("⏹  " + t("panel.stop")) if running else ("▶  " + t("panel.start")))
        self.run_btn.setToolTip(t("panel.stopTip") if running else t("panel.startTip"))

        err = self.ctrl.error_text()
        self.error_label.setText(err)
        self.error_label.setVisible(bool(err))

        for v, b in self.mode_buttons.items():
            b.setChecked(v == mode)
        provider = "Pixabay" if mode == "pixabay" else "Wallhaven"
        has_key = bool(st.get("hasPixabayKey") if mode == "pixabay" else st.get("hasWallhavenKey"))
        if self.ctrl.key_saved:
            self.key_label.setText(t("key.savedNotice", self.ctrl.key_saved))
        elif has_key:
            self.key_label.setText(t("key.statusSet", provider))
        else:
            self.key_label.setText(t("key.statusRequired") if mode == "pixabay" else t("key.statusOptional"))
        self.key_btn.setText(t("key.changeBtn") if has_key else t("key.setBtn"))

        for b in (self.prev_btn, self.next_btn, self.shuffle_btn):
            b.setEnabled(not busy)
        self.prev_btn.setEnabled(not busy and bool(st.get("history")))
        shuffle = bool(st.get("shuffle", True))
        self.shuffle_check.setChecked(shuffle)
        self.shuffle_btn.setToolTip(t("action.shuffleTip", t("state.on") if shuffle else t("state.off")))

        for p, b in self.period_buttons.items():
            b.setChecked(p == period)
            b.setText(("● " if p == cur_period else "") + t(f"period.{p}"))
        self.period_info.setText(t(f"period.{period}Range") + (" · " + t("period.activeNow") if period == cur_period else ""))
        self.tags_sublabel.setText(t("tags.sublabel", t(f"period.{period}")))
        self.tag_input.setPlaceholderText(t("tags.placeholder", t(f"period.{period}")))
        tags = list((st.get("periodTags") or {}).get(period) or [])
        sig = (period, tuple(tags), busy)
        if sig != self._tags_sig:
            self._tags_sig = sig
            self.chips.clear()
            for tag in tags:
                chip = QtWidgets.QPushButton(f"#{neutralize(tag, 28)}  ✕")
                chip.setToolTip(t("tags.removeTip", neutralize(tag, 28)))
                chip.setEnabled(not busy)
                chip.setCursor(QtGui.QCursor(qe(QtCore.Qt, "CursorShape", "PointingHandCursor")))
                chip.clicked.connect(lambda *_, v=tag: self.ctrl.remove_tag(v, self.period()))
                self.chips.addWidget(chip)
            self.chips.parentWidget().updateGeometry()
        self.tags_empty.setText(t("tags.empty", t(f"period.{period}")))
        self.tags_empty.setVisible(not tags)
        self.add_btn.setEnabled(not busy)

        color = self.ctrl.color_for(period)
        self.palette_box.setVisible(mode == "wallhaven")
        self.pixabay_note.setVisible(mode == "pixabay")
        self.palette_value.setText(f"#{color.upper()}" if color else t("palette.any"))
        self.any_btn.setChecked(not color)
        for hexv, sb in self.swatches.items():
            sb.setChecked(hexv == color)
            sb.setText("✓" if hexv == color else "")

        sched = int(st.get("scheduleMinutes", 30) or 0)
        for m, b in self.sched_buttons.items():
            b.setChecked(m == sched)

        live = st.get("liveEngine", "frame")
        self.desktop_label.setText(t("desktop.summary", neutralize(st.get("desktop", "unknown"), 40),
                                     t(f"live.{live}") if f"live.{live}" in STRINGS else live))
        avail = set(st.get("availableBackends") or [])
        setting = st.get("backendSetting", "auto")
        self.backend_combo.blockSignals(True)
        self.backend_combo.clear()
        self.backend_combo.addItem(t("desktop.auto", st.get("detectedBackend", "none")), "auto")
        for b in BACKENDS:
            if b == "omarchy" and b not in avail:
                continue
            self.backend_combo.addItem(b if b in avail else t("desktop.notInstalled", b), b)
        idx = self.backend_combo.findData(setting)
        self.backend_combo.setCurrentIndex(max(0, idx))
        self.backend_combo.blockSignals(False)
        self.autostart_check.setChecked(autostart_enabled())

        history = list(st.get("history") or [])
        current = st.get("currentPath", "")
        self.recent_count.setText(tn("recent.count", len(history)))
        self.recent_empty.setVisible(not history)
        self.recent.setVisible(bool(history))
        rsig = (tuple((h.get("path"), h.get("thumb"), h.get("title")) for h in history), current)
        if rsig != self._recent_sig:
            self._recent_sig = rsig
            self.recent.clear()
            role = qe(QtCore.Qt, "ItemDataRole", "UserRole")
            for h in history:
                badge = t("recent.badgeLive") if h.get("mode") == "pixabay" else t("recent.badgeStatic")
                title = neutralize(h.get("title"), 48)
                active = h.get("path") == current
                label = f"{'● ' if active else ''}{badge} · {title}"
                item = QtWidgets.QListWidgetItem(label)
                icon = self.thumb(h.get("thumb") or "")
                if icon is not None:
                    item.setIcon(icon)
                item.setData(role, h.get("path"))
                item.setToolTip(t("recent.applyTip", title, badge))
                self.recent.addItem(item)
                if active:
                    item.setSelected(True)
        self.recent.setFixedHeight(max(1, (len(history) + 1) // 2) * 148 + 12)
        self.recent.setEnabled(not busy)


class Controller:
    """Owns the tray icon, the window, the scheduler and the backend process queue."""

    def __init__(self, app):
        self.app = app
        self.state = {}
        self.busy = False
        self.queue = []  # [(args, stdin_bytes)]
        self.active = None
        self.error_code = ""
        self.error_detail = ""
        self.key_saved = ""
        self.scheduled_run = False

        self.proc = QtCore.QProcess()
        self.proc.finished.connect(lambda *_: self._on_cmd_finished())
        self.proc.errorOccurred.connect(self._on_cmd_error)
        self.status_proc = QtCore.QProcess()
        self.status_proc.finished.connect(lambda *_: self._on_status_finished())

        self.window = ControlWindow(self)

        self.tray = QtWidgets.QSystemTrayIcon(make_icon())
        self.tray.activated.connect(self._on_tray_activated)
        self.menu = QtWidgets.QMenu()
        self._build_menu()
        self.tray.setContextMenu(self.menu)

        self.schedule_timer = QtCore.QTimer()
        self.schedule_timer.timeout.connect(self._on_schedule)
        self.clock_timer = QtCore.QTimer()
        self.clock_timer.setInterval(60_000)
        self.clock_timer.timeout.connect(self.refresh)
        self.clock_timer.start()

        self.watch_debounce = QtCore.QTimer()
        self.watch_debounce.setSingleShot(True)
        self.watch_debounce.setInterval(400)
        self.watch_debounce.timeout.connect(self.refresh)
        self.watcher = QtCore.QFileSystemWatcher()
        self.watcher.directoryChanged.connect(lambda *_: self.watch_debounce.start())

        self.server = QtNetwork.QLocalServer()
        self.server.newConnection.connect(self._on_ipc)

        self._tray_wait = 0
        self.render()
        self.refresh()

    # ---- startup
    def start(self, show_window=False):
        path = socket_path()
        Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        QtNetwork.QLocalServer.removeServer(path)
        self.server.listen(path)
        if QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
            if show_window:
                self.show_window()
        else:
            # The tray host may start after us at login; wait a little before giving up.
            self._tray_timer = QtCore.QTimer()
            self._tray_timer.timeout.connect(lambda: self._wait_for_tray(show_window))
            self._tray_timer.start(1000)

    def _wait_for_tray(self, show_window):
        self._tray_wait += 1
        if QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
            self._tray_timer.stop()
            self.tray.show()
            if show_window:
                self.show_window()
        elif self._tray_wait >= 20:
            self._tray_timer.stop()
            self.window.minimize_on_close = True
            self.window.hint_label.show()
            self.show_window()

    # ---- tray menu
    def _build_menu(self):
        m = self.menu
        self.act_status = QAction("", m)
        self.act_status.setEnabled(False)
        m.addAction(self.act_status)
        m.addSeparator()
        self.act_run = QAction("", m)
        self.act_run.triggered.connect(lambda *_: self.toggle_running())
        m.addAction(self.act_run)
        self.act_next = QAction(t("action.next"), m)
        self.act_next.triggered.connect(lambda *_: self.next())
        self.act_prev = QAction(t("action.prev"), m)
        self.act_prev.triggered.connect(lambda *_: self.prev())
        self.act_shuffle = QAction(t("action.shuffle"), m)
        self.act_shuffle.triggered.connect(lambda *_: self.shuffle())
        for a in (self.act_next, self.act_prev, self.act_shuffle):
            m.addAction(a)
        m.addSeparator()

        mode_menu = m.addMenu(t("tray.mode"))
        self.mode_actions = {}
        group = QActionGroup(mode_menu)
        group.setExclusive(True)
        for value, label in (("wallhaven", t("mode.wallhaven")), ("pixabay", t("mode.pixabay"))):
            a = QAction(label, mode_menu)
            a.setCheckable(True)
            a.triggered.connect(lambda *_, v=value: self.set_mode(v))
            group.addAction(a)
            mode_menu.addAction(a)
            self.mode_actions[value] = a
        self._mode_group = group

        sched_menu = m.addMenu(t("tray.schedule"))
        self.sched_actions = {}
        sgroup = QActionGroup(sched_menu)
        sgroup.setExclusive(True)
        labels = {0: "schedule.off", 5: "schedule.5m", 15: "schedule.15m", 30: "schedule.30m",
                  60: "schedule.1h", 120: "schedule.2h"}
        for mins in SCHEDULES:
            a = QAction(t(labels[mins]), sched_menu)
            a.setCheckable(True)
            a.triggered.connect(lambda *_, v=mins: self.set_schedule(v))
            sgroup.addAction(a)
            sched_menu.addAction(a)
            self.sched_actions[mins] = a
        self._sched_group = sgroup

        self.act_shuffle_toggle = QAction(t("tray.shuffleToggle"), m)
        self.act_shuffle_toggle.setCheckable(True)
        self.act_shuffle_toggle.triggered.connect(lambda checked=False: self.set_shuffle(bool(checked)))
        m.addAction(self.act_shuffle_toggle)

        self.recent_menu = m.addMenu(t("tray.recent"))
        self.recent_menu.aboutToShow.connect(self._fill_recent_menu)
        m.addSeparator()
        self.act_open = QAction(t("tray.open"), m)
        self.act_open.triggered.connect(lambda *_: self.show_window())
        m.addAction(self.act_open)
        self.act_autostart = QAction(t("tray.autostart"), m)
        self.act_autostart.setCheckable(True)
        self.act_autostart.triggered.connect(lambda checked=False: self.set_autostart(bool(checked)))
        m.addAction(self.act_autostart)
        m.addSeparator()
        act_quit = QAction(t("tray.quit"), m)
        act_quit.triggered.connect(lambda *_: self.quit())
        m.addAction(act_quit)
        m.aboutToShow.connect(self.render)

    def _fill_recent_menu(self):
        self.recent_menu.clear()
        history = list(self.state.get("history") or [])
        if not history:
            a = self.recent_menu.addAction(t("recent.empty"))
            a.setEnabled(False)
            return
        current = self.state.get("currentPath", "")
        for h in history:
            badge = t("recent.badgeLive") if h.get("mode") == "pixabay" else t("recent.badgeStatic")
            label = f"{badge} · {neutralize(h.get('title'), 48)}"
            a = self.recent_menu.addAction(label)
            icon = self.window.thumb(h.get("thumb") or "")
            if icon is not None:
                a.setIcon(icon)
            a.setCheckable(True)
            a.setChecked(h.get("path") == current)
            a.setEnabled(not self.busy)
            a.triggered.connect(lambda *_, p=h.get("path"): self.apply(p))

    def _on_tray_activated(self, reason):
        if reason == qe(QtWidgets.QSystemTrayIcon, "ActivationReason", "Trigger"):
            self.toggle_window()
        elif reason == qe(QtWidgets.QSystemTrayIcon, "ActivationReason", "MiddleClick"):
            self.shuffle()

    # ---- window
    def show_window(self):
        self.refresh()
        self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()

    def toggle_window(self):
        if self.window.isVisible() and not self.window.isMinimized():
            self.window.hide()
        else:
            self.show_window()

    def quit(self):
        self.server.close()
        self.tray.hide()
        self.app.quit()

    # ---- IPC from `wallrotator_tray.py <command>`
    def _on_ipc(self):
        while self.server.hasPendingConnections():
            conn = self.server.nextPendingConnection()
            conn.readyRead.connect(lambda c=conn: self._read_ipc(c))
            conn.disconnected.connect(conn.deleteLater)
            if conn.bytesAvailable() > 0:
                self._read_ipc(conn)

    def _read_ipc(self, conn):
        if conn.property("handled"):
            return
        conn.setProperty("handled", True)
        raw = bytes(conn.readAll().data()).decode("ascii", "replace").strip()
        conn.write(b"ok\n")
        conn.flush()
        conn.disconnectFromServer()
        self.handle_command(raw.split("\n", 1)[0][:32])

    def handle_command(self, cmd):
        actions = {
            "show": self.show_window,
            "hide": self.window.hide,
            "toggle": self.toggle_window,
            "next": self.next,
            "prev": self.prev,
            "shuffle": self.shuffle,
            "start": lambda: self.set_running(True),
            "stop": lambda: self.set_running(False),
            "toggle-running": self.toggle_running,
            "quit": self.quit,
        }
        fn = actions.get(cmd)
        if fn:
            fn()

    # ---- backend process handling
    def _queued(self, name):
        return any(args and args[0] == name for args, _ in self.queue)

    def _start(self, args, stdin=None):
        self.busy = True
        self.active = args[0]
        self.error_code = ""
        self.proc.setProgram(sys.executable)
        self.proc.setArguments(["-I", str(BACKEND), *args])
        self.proc.start()
        if stdin is not None:
            self.proc.write(stdin)
        self.proc.closeWriteChannel()
        self.render()

    def run(self, args, stdin=None):
        """Run now if idle, otherwise ignore (for actions disabled while busy)."""
        if self.proc.state() != qe(QtCore.QProcess, "ProcessState", "NotRunning"):
            return
        self._start(args, stdin)

    def enqueue(self, args, dedupe=None, stdin=None):
        if self.proc.state() == qe(QtCore.QProcess, "ProcessState", "NotRunning") and not self.queue:
            self._start(args, stdin)
            return
        if dedupe:
            self.queue = [q for q in self.queue if not (q[0] and q[0][0] == dedupe)]
        self.queue.append((args, stdin))

    def _on_cmd_error(self, err):
        if err == qe(QtCore.QProcess, "ProcessError", "FailedToStart"):
            self.error_code, self.error_detail = "generic", "python"
            self._on_cmd_finished(read=False)

    def _on_cmd_finished(self, read=True):
        if read:
            out = bytes(self.proc.readAllStandardOutput().data()).decode("utf-8", "replace")
            self._apply_output(out, self.active)
        was_scheduled = self.scheduled_run
        self.scheduled_run = False
        if self.error_code and (was_scheduled or not self.window.isVisible()) and self.tray.isVisible():
            self.tray.showMessage(t("tray.errorTitle"), self.error_text(),
                                  qe(QtWidgets.QSystemTrayIcon, "MessageIcon", "Warning"), 6000)
        if self.queue:
            args, stdin = self.queue.pop(0)
            self._start(args, stdin)
        else:
            self.active = None
            self.busy = False
            self.render()

    def refresh(self):
        if self.busy or self.status_proc.state() != qe(QtCore.QProcess, "ProcessState", "NotRunning"):
            return
        self.status_proc.setProgram(sys.executable)
        self.status_proc.setArguments(["-I", str(BACKEND), "status"])
        self.status_proc.start()

    def _on_status_finished(self):
        out = bytes(self.status_proc.readAllStandardOutput().data()).decode("utf-8", "replace")
        if not self.busy:
            self._apply_output(out, "status")
            self.render()
        if STATE_DIR.is_dir() and str(STATE_DIR) not in self.watcher.directories():
            self.watcher.addPath(str(STATE_DIR))

    def _apply_output(self, out, source):
        res = None
        for line in reversed([ln for ln in out.splitlines() if ln.strip()]):
            try:
                res = json.loads(line)
                break
            except ValueError:
                continue
        if not isinstance(res, dict):
            return
        if res.get("ok") is False:
            self.error_code = re.sub(r"[^a-z_]", "", str(res.get("code") or "generic"))[:40] or "generic"
            self.error_detail = neutralize(res.get("detail"), 200)
            return
        if source != "status":
            self.error_code = ""
        keep_sched = self._queued("set-schedule") or self._queued("set-running")
        old = self.state
        self.state = res
        if keep_sched and old:
            self.state["running"] = old.get("running", res.get("running"))
            self.state["scheduleMinutes"] = old.get("scheduleMinutes", res.get("scheduleMinutes"))
        if res.get("keySaved"):
            self.key_saved = "Pixabay" if res["keySaved"] == "pixabay" else "Wallhaven"

    def error_text(self):
        if not self.error_code:
            return ""
        key = f"error.{self.error_code}"
        text = t(key, self.error_detail) if key in STRINGS else t("error.generic", self.error_code)
        return text

    def color_for(self, period):
        return str((self.state.get("periodColors") or {}).get(period) or "")

    # ---- actions
    def set_mode(self, mode):
        if mode == self.state.get("mode"):
            return
        self.state["mode"] = mode
        self.key_saved = ""
        self.enqueue(["set-mode", mode], dedupe="set-mode")
        self.render()

    def set_running(self, want):
        self.state["running"] = bool(want)
        if want and not self.state.get("scheduleMinutes"):
            self.state["scheduleMinutes"] = 30
        self.enqueue(["set-running", "true" if want else "false"], dedupe="set-running")
        self.render()

    def toggle_running(self):
        self.set_running(not self.state.get("running"))

    def set_schedule(self, minutes):
        self.state["scheduleMinutes"] = int(minutes)
        self.state["running"] = minutes > 0
        self.enqueue(["set-schedule", str(int(minutes))], dedupe="set-schedule")
        self.render()

    def set_shuffle(self, want):
        self.state["shuffle"] = bool(want)
        self.enqueue(["set-shuffle", "true" if want else "false"], dedupe="set-shuffle")
        self.render()

    def set_backend(self, name):
        self.state["backendSetting"] = name
        self.enqueue(["set-backend", name], dedupe="set-backend")

    def set_key(self, provider, key):
        self.key_saved = ""
        self.enqueue(["set-key", provider], stdin=(key + "\n").encode("utf-8"))

    def add_tag(self, tag, period):
        self.enqueue(["add-tag", tag, period])

    def remove_tag(self, tag, period):
        self.enqueue(["remove-tag", str(tag), period])

    def set_color(self, period, hexv):
        self.enqueue(["set-color", period, hexv or "none"])

    def next(self):
        self.run(["next"])

    def prev(self):
        if self.state.get("history"):
            self.run(["prev"])

    def shuffle(self):
        self.run(["shuffle"])

    def apply(self, path):
        if path:
            self.run(["apply", str(path)])

    def set_autostart(self, enabled):
        try:
            set_autostart(enabled)
        except (OSError, ValueError) as e:
            self.error_code, self.error_detail = "io", neutralize(str(e), 200)
        self.render()

    def _on_schedule(self):
        if self.state.get("running") and int(self.state.get("scheduleMinutes") or 0) > 0 \
                and not self.busy and not self.queue:
            self.scheduled_run = True
            self.run(["next"])

    # ---- state -> tray
    def render(self):
        st = self.state
        running = bool(st.get("running"))
        mode = st.get("mode", "wallhaven")
        sched = int(st.get("scheduleMinutes", 30) or 0)
        mode_label = t("mode.pixabay") if mode == "pixabay" else t("mode.wallhaven")
        run_label = t("status.running") if running else t("status.stopped")

        interval = max(1, sched) * 60_000
        if running and sched > 0:
            if not self.schedule_timer.isActive() or self.schedule_timer.interval() != interval:
                self.schedule_timer.start(interval)
        else:
            self.schedule_timer.stop()

        key = (running, self.busy)
        if getattr(self, "_icon_key", None) != key:
            self._icon_key = key
            self.tray.setIcon(make_icon(running, self.busy))
        self.tray.setToolTip(t("bar.tooltipBusy") if self.busy else t("bar.tooltip", mode_label, run_label))

        self.act_status.setText(t("panel.busy") if self.busy else f"{run_label} · {mode_label}")
        self.act_run.setText(t("panel.stop") if running else t("panel.start"))
        for a in (self.act_next, self.act_shuffle):
            a.setEnabled(not self.busy)
        self.act_prev.setEnabled(not self.busy and bool(st.get("history")))
        for v, a in self.mode_actions.items():
            a.setChecked(v == mode)
        for v, a in self.sched_actions.items():
            a.setChecked(v == sched)
        self.act_shuffle_toggle.setChecked(bool(st.get("shuffle", True)))
        self.act_autostart.setChecked(autostart_enabled())
        if self.window.isVisible():
            self.window.render()


def _usage():
    print(__doc__.strip())


def main(argv):
    args = list(argv)
    if args and args[0] in ("-h", "--help", "help"):
        _usage()
        return 0
    if args and args[0] == "--autostart":
        if len(args) < 2 or args[1] not in ("on", "off"):
            _usage()
            return 2
        set_autostart(args[1] == "on")
        print(f"autostart {'enabled' if args[1] == 'on' else 'disabled'}: {AUTOSTART_FILE}")
        return 0

    cmd = args[0] if args else ""
    if cmd and cmd not in COMMANDS:
        _usage()
        return 2

    # Already running? Hand the command over (default: show the window).
    if send_to_running(cmd or "show"):
        return 0
    if cmd in DIRECT_BACKEND:
        return run_backend_direct(DIRECT_BACKEND[cmd])
    if cmd in ("hide", "quit", "toggle-running"):
        print("Wallpaper Rotator tray is not running.", file=sys.stderr)
        return 1

    if QtWidgets is None:
        print(
            "Wallpaper Rotator tray needs a Qt binding for Python: PySide6, PyQt6 or PyQt5.\n"
            "  Arch:          sudo pacman -S pyside6\n"
            "  Debian/Ubuntu: sudo apt install python3-pyqt5   (or python3-pyside6.qtwidgets)\n"
            "  Fedora:        sudo dnf install python3-pyside6\n"
            "  openSUSE:      sudo zypper install python3-PySide6\n"
            "  pip:           pip install --user PySide6-Essentials",
            file=sys.stderr,
        )
        return 1

    signal.signal(signal.SIGINT, signal.SIG_DFL)
    app = QtWidgets.QApplication([sys.argv[0]])
    app.setApplicationName("Wallpaper Rotator")
    app.setApplicationDisplayName("Wallpaper Rotator")
    if hasattr(app, "setDesktopFileName"):
        app.setDesktopFileName("wallrotator")
    app.setQuitOnLastWindowClosed(False)
    ctrl = Controller(app)
    ctrl.start(show_window=(cmd in ("show", "toggle")))
    return qexec(app)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
