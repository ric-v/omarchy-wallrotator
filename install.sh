#!/bin/sh
# Install Wallpaper Rotator as a system tray app for the current user.
#
#   ./install.sh               install (or update)
#   ./install.sh --autostart   install and start on login
#   ./install.sh --uninstall   remove the app (keeps state, keys and wallpapers)
#
# Nothing is installed system-wide and no root access is needed.
set -eu

SRC_DIR=$(cd "$(dirname "$0")" && pwd)
DATA_HOME=${XDG_DATA_HOME:-"$HOME/.local/share"}
CONFIG_HOME=${XDG_CONFIG_HOME:-"$HOME/.config"}
APP_DIR="$DATA_HOME/wallrotator/app"
BIN_DIR="$HOME/.local/bin"
DESKTOP_FILE="$DATA_HOME/applications/wallrotator.desktop"
ICON_DEST="$DATA_HOME/icons/hicolor/scalable/apps/wallrotator.svg"
AUTOSTART_FILE="$CONFIG_HOME/autostart/wallrotator-tray.desktop"

autostart=0
case "${1:-}" in
  --uninstall)
    if [ -x "$BIN_DIR/wallrotator-tray" ]; then
      "$BIN_DIR/wallrotator-tray" quit >/dev/null 2>&1 || true
    fi
    rm -rf "$APP_DIR"
    rm -f "$BIN_DIR/wallrotator" "$BIN_DIR/wallrotator-tray" "$DESKTOP_FILE" "$ICON_DEST" "$AUTOSTART_FILE"
    echo "Wallpaper Rotator removed."
    echo "Saved state, API keys and downloaded wallpapers were kept. To remove them too:"
    echo "  rm -rf ~/.local/state/wallrotator ~/.cache/wallrotator \"$DATA_HOME/wallrotator\""
    exit 0
    ;;
  --autostart) autostart=1 ;;
  "") ;;
  *) sed -n '2,8p' "$0"; exit 2 ;;
esac

command -v python3 >/dev/null 2>&1 || { echo "python3 is required." >&2; exit 1; }

PYTHON=$(command -v python3)
if ! "$PYTHON" -c 'import importlib.util as u, sys; sys.exit(0 if any(u.find_spec(m) for m in ("PySide6", "PyQt6", "PyQt5")) else 1)'; then
  cat >&2 <<'EOF'
Warning: no Qt binding for Python found. The tray needs one of PySide6, PyQt6 or PyQt5:
  Arch:          sudo pacman -S pyside6
  Debian/Ubuntu: sudo apt install python3-pyqt5
  Fedora:        sudo dnf install python3-pyside6
  openSUSE:      sudo zypper install python3-PySide6
  pip:           pip install --user PySide6-Essentials
EOF
fi

mkdir -p "$APP_DIR/i18n" "$APP_DIR/icons" "$BIN_DIR" "$(dirname "$DESKTOP_FILE")" "$(dirname "$ICON_DEST")"
install -m 0755 "$SRC_DIR/wallrotator.py" "$SRC_DIR/wallrotator_tray.py" "$APP_DIR/"
install -m 0644 "$SRC_DIR/i18n/en.json" "$APP_DIR/i18n/"
install -m 0644 "$SRC_DIR/icons/wallrotator.svg" "$APP_DIR/icons/"
install -m 0644 "$SRC_DIR/icons/wallrotator.svg" "$ICON_DEST"

cat > "$BIN_DIR/wallrotator-tray" <<EOF
#!/bin/sh
exec "$PYTHON" "$APP_DIR/wallrotator_tray.py" "\$@"
EOF
cat > "$BIN_DIR/wallrotator" <<EOF
#!/bin/sh
exec "$PYTHON" -I "$APP_DIR/wallrotator.py" "\$@"
EOF
chmod 0755 "$BIN_DIR/wallrotator-tray" "$BIN_DIR/wallrotator"

# Application menu entry (Exec quoting handled by the tray script itself)
PYTHONDONTWRITEBYTECODE=1 "$PYTHON" - "$APP_DIR" "$DESKTOP_FILE" "$PYTHON" <<'EOF'
import importlib.util, sys
app_dir, dest, python = sys.argv[1:4]
spec = importlib.util.spec_from_file_location("tray", f"{app_dir}/wallrotator_tray.py")
tray = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tray)
with open(dest, "w", encoding="utf-8") as f:
    f.write(tray.desktop_entry([python, f"{app_dir}/wallrotator_tray.py"]).replace(
        f"Icon={app_dir}/icons/wallrotator.svg", "Icon=wallrotator"))
EOF
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q "$(dirname "$DESKTOP_FILE")" || true

if [ "$autostart" = 1 ]; then
  "$PYTHON" "$APP_DIR/wallrotator_tray.py" --autostart on
fi

echo "Installed Wallpaper Rotator to $APP_DIR"
echo "Start it from your app menu or run: wallrotator-tray"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "Note: $BIN_DIR is not on your PATH." ;;
esac
