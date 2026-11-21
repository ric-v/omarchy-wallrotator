# Wallpaper Rotator (`ric-v.wallrotator`)

A topbar tray wallpaper manager and rotator plugin for [Omarchy](https://omarchy.org/) Shell.

## Features

- **Two Modes**:
  - **Static (Wallhaven)**: High-resolution static wallpapers via Wallhaven (`https://wallhaven.cc/api/v1/search`), with optional per-period **color palette** filtering.
  - **Live (Pixabay)**: Animated MP4 video wallpapers via Pixabay (`https://pixabay.com/api/videos/`), automatically muted and rendered by Omarchy's background engine (`owe`).
- **Start / Stop & Auto-Rotation Schedule**:
  - Dedicated **Start / Stop** toggle in the popup header.
  - Configurable auto-rotation schedule: `Off`, `5m`, `15m`, `30m`, `1h`, or `2h`.
- **Playback Actions**:
  - **Prev** / **Next** / **Shuffle** controls in the popup (and right-click / middle-click shortcuts on the tray icon).
- **Time-of-Day Tags & Color Palettes**:
  - Independent tag lists and optional Wallhaven color palettes for four time windows:
    - **Morning** (`7am–12pm`)
    - **Noon** (`12pm–3pm`)
    - **Evening** (`3pm–6pm`)
    - **Night** (`6pm–7am`)
  - Ships with curated, time-appropriate default tags for every period.
- **Recent 10 Wallpapers**:
  - 2-column thumbnail grid showing your 10 most recent static and live wallpapers (`STATIC` / `LIVE` badges and `● Active` indicator) for instant one-click switching.
- **Built-In API Key Management**:
  - Set or update your **Wallhaven** (optional) or **Pixabay** (required for live videos) API key/token directly inside the popup. Keys are stored locally with `0600` permissions in `~/.local/state/wallrotator/`.

## Installation

```bash
omarchy plugin add https://github.com/ric-v/omarchy-wallrotator.git --enable
```

To place it specifically in the topbar tray drawer:

```bash
omarchy plugin enable ric-v.wallrotator --section tray
```

## Removal

```bash
omarchy plugin disable ric-v.wallrotator
omarchy plugin remove ric-v.wallrotator
```

To also remove saved state, API keys, and cached thumbnails:

```bash
rm -rf ~/.local/state/wallrotator ~/.cache/wallrotator
```

## External Dependencies

- **Required**: `python3` (uses Python's standard library only — no `pip` packages needed) and `omarchy` (`omarchy theme bg set`).
- **Optional**:
  - `ffmpeg` — strips audio streams from downloaded live wallpaper videos and extracts local video thumbnails.
  - `owe` — Omarchy's video wallpaper engine daemon (automatically notified when installed).

## IPC & Keybindings

You can bind actions in `~/.config/hypr/bindings.lua`:

```lua
o.bind("SUPER + CTRL + W", "Wallpaper Rotator", "omarchy-shell wallrotator toggle")
o.bind("SUPER + ALT + W", "Next wallpaper", "omarchy-shell wallrotator next")
```

Available IPC commands:
- `omarchy-shell wallrotator toggle` / `open` / `close`
- `omarchy-shell wallrotator next`
- `omarchy-shell wallrotator prev`
- `omarchy-shell wallrotator shuffle`
- `omarchy-shell wallrotator start` / `stop` / `toggleRunning`

## Testing & Validation

```bash
omarchy plugin validate .
python3 -m unittest discover -s tests -v
```

## Security & Privacy

- All plugin-owned UI text uses `Text.PlainText` and neutralizes markup before reaching shared components.
- Network requests are restricted to fixed HTTPS allowlists (`wallhaven.cc`, `w.wallhaven.cc`, `th.wallhaven.cc`, `pixabay.com`, `cdn.pixabay.com`, `i.vimeocdn.com`) with explicit timeouts and response size limits.
- Background rotation only fetches media when explicitly started with an active schedule.
- No untrusted values are interpolated into shell commands.

## License

[MIT License](LICENSE)
