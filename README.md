# PaneCast

Mirror one or two specific application windows onto a second display — a TV, a
projector, or any extended monitor — without mirroring your whole desktop.

Unlike `Win + P` screen duplication, PaneCast captures **individual
windows**. Your email, notifications and browser tabs stay private on your
laptop while only the window(s) you chose appear on the big screen.

> **Platform support:** Windows 10/11 only. See
> [Platform support](#platform-support) for why, and the
> [Roadmap](#roadmap) for cross-platform plans.

---

## Features

- **Per-window capture** — project a specific app, not the entire desktop.
- **Dual-window projection** — show two applications at once.
- **Three layout modes** — side-by-side, top/bottom, or picture-in-picture.
- **Isolated capture** — captured windows keep rendering correctly even when
  covered by other windows or moved off-screen.
- **Child-canvas detection** — automatically finds the real rendering surface
  inside a host window (e.g. a Java/AWT or OpenGL canvas), so games and IDEs
  capture cleanly instead of showing a blank frame.
- **DPI-correct** — per-monitor DPI awareness keeps pixels 1:1 on mixed-DPI
  setups.
- **Press `Esc` or `q`** on the projected screen to stop at any time.

## Screenshots

<!-- Add real screenshots before publishing:
     docs/control-panel.png and docs/projected-output.png -->

| Control panel | Projected output |
| ------------- | ---------------- |
| _screenshot_  | _screenshot_     |

---

## Requirements

| | |
| --- | --- |
| OS | Windows 10 (1903+) or Windows 11 |
| Python | 3.9 or newer |
| Displays | At least two (a second monitor set to **Extend**) |

Python **must** include Tcl/Tk. This is an option in the official installer
labelled *"tcl/tk and IDLE"* — if it was unchecked, the app cannot start.
See [Troubleshooting](#troubleshooting).

## Download

**[Download PaneCast.exe](https://github.com/fbugri-rgb/PaneCast/releases/latest)**
— a single 29 MB file, no Python installation required.

Windows SmartScreen will warn you the first time, because the binary is not
code-signed. Click **More info → Run anyway**, or run from source below if you
would rather not trust a prebuilt binary.

## Installation from source

```bash
git clone https://github.com/<your-username>/PaneCast.git
cd PaneCast

python -m venv .venv
.venv\Scripts\activate

pip install -r requirements.txt
```

## Usage

```bash
python panecast.py
```

1. Set your second display to **Extend** (`Win + P` → *Extend*).
2. Pick the window you want to project under **1st Application Window**.
   Click **🔄 Refresh List** if you opened it after launching.
3. Optionally pick a second window and choose a **layout style**.
4. Choose the target display under **Target TV / Extended Display**, using
   **🔄 Detect Screens** if it isn't listed.
5. Click **START PROJECTING**.
6. Press **`Esc`** or **`q`** on the projected screen to stop.

### Layout modes

| Mode | Behaviour |
| --- | --- |
| **Side-by-Side** | Two windows split left / right. |
| **Top / Bottom** | Two windows stacked vertically. |
| **Picture-in-Picture** | Window 1 fills the screen; window 2 floats bottom-right at 38% size. |

Selecting *"(None - Single Window Mode)"* for the second window projects a
single window scaled to fit, preserving aspect ratio.

---

## How it works

PaneCast uses two capture backends and prefers the faster one:

1. **Windows Graphics Capture (WGC)** — the modern, GPU-accelerated OS capture
   API, via the [`windows-capture`](https://pypi.org/project/windows-capture/)
   package. Runs on its own thread and delivers frames asynchronously. This is
   the path that keeps working when a window is occluded.
2. **`PrintWindow` (GDI)** — a pure `ctypes` fallback used when WGC is
   unavailable. It requires no extra dependencies but is slower and some
   applications refuse to render into it.

Window and monitor enumeration is done directly against the Win32 API
(`EnumWindows`, `EnumDisplayMonitors`, `DwmGetWindowAttribute`) through
`ctypes`, so there are no heavyweight GUI or automation dependencies. The
interface itself is plain `tkinter`, and captured frames are scaled with
Pillow and blitted onto a `tkinter.Canvas`.

Window bounds come from `DwmGetWindowAttribute(DWMWA_EXTENDED_FRAME_BOUNDS)`
rather than `GetWindowRect`, which avoids the invisible drop-shadow border that
would otherwise show up as a dark band around the projected window.

### Performance

Frames are only rescaled and re-uploaded when the capture thread actually
delivers a new one, so an idle projection costs almost nothing. The `PhotoImage`
for each slot is allocated once and reused via `.paste()` rather than rebuilt
per frame, and canvas `coords`/`itemconfigure` calls are skipped unless
something moved.

The remaining bottleneck is Tk itself. Pasting into a canvas-attached image
forces a synchronous redraw of the whole item, which dominates at high
resolutions:

| Output resolution | Cost per rendered frame | Practical ceiling |
| --- | --- | --- |
| 1920 x 1080 | ~20 ms | ~50 fps |
| 3840 x 2160 | ~78 ms | ~13 fps |

If your TV is 4K, setting it to 1080p for projection is by far the biggest
single win available. Genuinely high framerates at 4K would require replacing
the Tk canvas with a GPU-backed presentation surface.

---

## Platform support

| Platform | Status |
| --- | --- |
| Windows 10 / 11 | ✅ Supported |
| macOS | ❌ Not supported |
| Linux | ❌ Not supported |

This is **not** a packaging limitation — the capture engine is built directly
on Win32. Every core operation (window enumeration, per-window capture, monitor
geometry, DPI awareness) is a Windows-specific system call with no portable
equivalent. Supporting other platforms requires new capture backends:

- **macOS** — ScreenCaptureKit (or the older Quartz `CGWindowList` API), plus
  the user granting Screen Recording permission.
- **Linux** — X11 via XComposite, or PipeWire through `xdg-desktop-portal` on
  Wayland. Wayland deliberately restricts window capture for security, so it
  always requires an interactive permission prompt.

See the [Roadmap](#roadmap).

---

## Building a standalone executable

Users then need no Python installation.

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name PaneCast --noconfirm   --exclude-module cv2   --runtime-hook pyi_rth_stub_cv2.py   panecast.py
```

The binary appears in `dist/PaneCast.exe` (~29 MB).

- `--onefile` bundles everything into a single `.exe`.
- `--windowed` suppresses the console window.
- `--exclude-module cv2` together with the runtime hook drops OpenCV, which
  `windows-capture` imports at module scope but only uses in `save_as_image()`
  — a function PaneCast never calls. Bundling it anyway costs **45 MB**
  (70 MB → 29 MB) for code that never runs. The hook installs a stub module so
  the import still succeeds. Verified: the resulting binary still reports
  `HAS_WINDOWS_CAPTURE = True` and captures live frames.

**PyInstaller cannot cross-compile.** A Windows `.exe` must be built on
Windows, a macOS `.app` on macOS, and a Linux binary on Linux. Once other
platforms are supported, a GitHub Actions matrix build across
`windows-latest`, `macos-latest` and `ubuntu-latest` is the usual way to
publish all three from one workflow.

> Single-file builds unpack to a temporary directory at startup, so they launch
> more slowly than `--onedir`. Some antivirus engines also flag freshly-built,
> unsigned PyInstaller binaries; code-signing the executable avoids this.

---

## Troubleshooting

**`ModuleNotFoundError: No module named 'tkinter'`**
Python was installed without Tcl/Tk. Re-run the official Python installer,
choose **Modify**, and tick *tcl/tk and IDLE*. Note that *upgrading* Python
will not add it — the installer preserves your existing feature selection, so
you must explicitly use Modify.

**"Only 1 Screen Detected"**
Press `Win + P` and choose **Extend** (not *Duplicate* or *Second screen
only*), then click **🔄 Detect Screens**.

**Projected window is black or frozen**
Some applications — particularly ones using hardware-accelerated or protected
rendering — refuse to draw into the GDI fallback. Install `windows-capture` to
enable the WGC backend, which handles most of these correctly.

**The window I want isn't in the list**
Only visible, titled, non-tool windows are listed. Click **🔄 Refresh List**
after opening the app. Minimised windows may not capture reliably.

---

## Roadmap

- [ ] Cross-platform capture backends (macOS, Linux)
- [ ] Configurable target framerate instead of a fixed timer
- [ ] Remember last-used window/display selection between runs
- [ ] Adjustable picture-in-picture size and corner
- [ ] Signed release binaries via GitHub Actions

## Contributing

Issues and pull requests are welcome. The capture layer is deliberately
isolated in a handful of functions (`get_open_windows`, `get_monitors`,
`find_best_capture_hwnd`, `get_window_bounds`, `capture_printwindow`), which is
the natural seam for adding a non-Windows backend.

## License

Released under the MIT License — see [`LICENSE`](LICENSE).
