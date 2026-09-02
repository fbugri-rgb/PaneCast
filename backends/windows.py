"""Windows capture backend.

Two capture paths, in order of preference:

1. **Windows Graphics Capture** via the `windows-capture` package - the modern
   GPU-backed OS API. Runs on its own thread and keeps delivering frames while
   the target window is occluded or off-screen.
2. **PrintWindow (GDI)** - a pure ctypes fallback needing no extra packages,
   but slower, and some applications refuse to render into it.

Window geometry comes from `DwmGetWindowAttribute(DWMWA_EXTENDED_FRAME_BOUNDS)`
rather than `GetWindowRect`, which excludes the invisible drop-shadow border
that would otherwise appear as a dark band around the projected window.
"""

from __future__ import annotations

import atexit
import ctypes
import os
from ctypes import wintypes

from PIL import Image

from .base import CaptureBackend

try:
    from windows_capture import WindowsCapture
    HAS_WINDOWS_CAPTURE = True
except Exception:
    HAS_WINDOWS_CAPTURE = False


user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
dwmapi = ctypes.windll.dwmapi

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080

class RECT(ctypes.Structure):
    _fields_ = [
        ('left', ctypes.c_long),
        ('top', ctypes.c_long),
        ('right', ctypes.c_long),
        ('bottom', ctypes.c_long)
    ]

class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ('cbSize', wintypes.DWORD),
        ('rcMonitor', RECT),
        ('rcWork', RECT),
        ('dwFlags', wintypes.DWORD),
        ('szDevice', ctypes.c_wchar * 32)
    ]

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ('biSize', wintypes.DWORD),
        ('biWidth', wintypes.LONG),
        ('biHeight', wintypes.LONG),
        ('biPlanes', wintypes.WORD),
        ('biBitCount', wintypes.WORD),
        ('biCompression', wintypes.DWORD),
        ('biSizeImage', wintypes.DWORD),
        ('biXPelsPerMeter', wintypes.LONG),
        ('biYPelsPerMeter', wintypes.LONG),
        ('biClrUsed', wintypes.DWORD),
        ('biClrImportant', wintypes.DWORD)
    ]

def get_open_windows():
    windows = []
    own_pid = os.getpid()

    def enum_windows_callback(hwnd, lParam):
        if user32.IsWindowVisible(hwnd):
            # Skip every window owned by this process, otherwise the control
            # panel and the projector surface show up as capture targets.
            wnd_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wnd_pid))
            if wnd_pid.value == own_pid:
                return 1
            ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if not (ex_style & WS_EX_TOOLWINDOW):
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buff = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buff, length + 1)
                    title = buff.value.strip()
                    ignored = [
                        'Program Manager', 'Settings', 'Windows Input Experience',
                        'NVIDIA GeForce Overlay'
                    ]
                    if title and not any(title.startswith(ig) for ig in ignored):
                        windows.append((title, hwnd))
        return 1

    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HWND, wintypes.LPARAM)
    cb = WNDENUMPROC(enum_windows_callback)
    user32.EnumWindows(cb, 0)
    return sorted(windows, key=lambda x: x[0].lower())

def get_monitors():
    monitors = []
    def callback(hMonitor, hdcMonitor, lprcMonitor, lParam):
        mi = MONITORINFOEXW()
        mi.cbSize = ctypes.sizeof(MONITORINFOEXW)
        user32.GetMonitorInfoW(hMonitor, ctypes.byref(mi))
        r = mi.rcMonitor
        monitors.append({
            'device': mi.szDevice,
            'x': r.left,
            'y': r.top,
            'width': r.right - r.left,
            'height': r.bottom - r.top,
            'is_primary': bool(mi.dwFlags & 1)
        })
        return 1

    MONITORENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(RECT), wintypes.LPARAM)
    cb = MONITORENUMPROC(callback)
    user32.EnumDisplayMonitors(None, None, cb, 0)
    return monitors

def find_best_capture_hwnd(top_hwnd):
    """Finds the actual rendering canvas HWND (e.g. Java SunAwtCanvas) inside top_hwnd."""
    if not user32.IsWindow(top_hwnd):
        return top_hwnd

    rect = RECT()
    user32.GetClientRect(top_hwnd, ctypes.byref(rect))
    top_w = rect.right - rect.left
    top_h = rect.bottom - rect.top

    children = []
    def enum_child_proc(child_hwnd, lParam):
        if user32.IsWindowVisible(child_hwnd):
            c_rect = RECT()
            user32.GetClientRect(child_hwnd, ctypes.byref(c_rect))
            cw = c_rect.right - c_rect.left
            ch = c_rect.bottom - c_rect.top
            
            buff = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(child_hwnd, buff, 256)
            class_name = buff.value
            
            if cw > 50 and ch > 50:
                children.append((child_hwnd, class_name, cw, ch, cw * ch))
        return 1

    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HWND, wintypes.LPARAM)
    cb = WNDENUMPROC(enum_child_proc)
    user32.EnumChildWindows(top_hwnd, cb, 0)

    children.sort(key=lambda x: x[4], reverse=True)

    for chwnd, cname, cw, ch, area in children:
        if "Canvas" in cname or "Render" in cname or "Direct" in cname or "OpenGL" in cname or "Awt" in cname:
            return chwnd
        if cw >= top_w * 0.75 and ch >= top_h * 0.75:
            return chwnd

    return top_hwnd

def get_window_bounds(hwnd):
    rect = RECT()
    res = dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect))
    if res == 0:
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        if w > 50 and h > 50:
            return rect.left, rect.top, w, h
            
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top

def capture_printwindow(hwnd):
    """Isolated window capture fallback via PrintWindow."""
    if not user32.IsWindow(hwnd):
        return None

    x, y, w, h = get_window_bounds(hwnd)
    if w <= 0 or h <= 0:
        return None

    hwndDC = user32.GetWindowDC(hwnd)
    if not hwndDC:
        return None

    mfcDC = gdi32.CreateCompatibleDC(hwndDC)
    bmp = gdi32.CreateCompatibleBitmap(hwndDC, w, h)
    old_bmp = gdi32.SelectObject(mfcDC, bmp)

    res = user32.PrintWindow(hwnd, mfcDC, 2)
    if not res:
        res = user32.PrintWindow(hwnd, mfcDC, 0)

    bmi = BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.biWidth = w
    bmi.biHeight = -h
    bmi.biPlanes = 1
    bmi.biBitCount = 32
    bmi.biCompression = 0

    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mfcDC, bmp, 0, h, buf, ctypes.byref(bmi), 0)

    gdi32.SelectObject(mfcDC, old_bmp)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mfcDC)
    user32.ReleaseDC(hwnd, hwndDC)

    if not res:
        return None

    return Image.frombytes('RGB', (w, h), buf.raw, 'raw', 'BGRX')


class WindowsBackend(CaptureBackend):
    """Win32 + Windows Graphics Capture."""

    def __init__(self):
        self.name = ("Windows Graphics Capture" if HAS_WINDOWS_CAPTURE
                     else "PrintWindow (GDI fallback)")
        if not HAS_WINDOWS_CAPTURE:
            self.degraded_reason = (
                "windows-capture is not installed, so the slower GDI path is in "
                "use. Some applications will not render into it."
            )
        self._timer_raised = False

    @classmethod
    def is_available(cls):
        if os.name != "nt":
            return False, "not running on Windows"
        return True, ""

    # -- process setup ---------------------------------------------------

    def prepare_process(self):
        # Per-monitor DPI awareness keeps screen geometry and pixels 1:1.
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)
            except Exception:
                pass
        # A 1ms timer makes tkinter's after() scheduling less coarse.
        try:
            ctypes.windll.winmm.timeBeginPeriod(1)
            self._timer_raised = True
            atexit.register(self.shutdown)
        except Exception:
            pass

    def shutdown(self):
        if self._timer_raised:
            self._timer_raised = False
            try:
                ctypes.windll.winmm.timeEndPeriod(1)
            except Exception:
                pass

    # -- enumeration -----------------------------------------------------

    def list_windows(self):
        return get_open_windows()

    def list_monitors(self):
        return get_monitors()

    def window_bounds(self, handle):
        try:
            return get_window_bounds(handle)
        except Exception:
            return None

    # -- capture ---------------------------------------------------------

    def open_stream(self, handle, on_frame):
        """Start a WGC session, trying the real rendering canvas first."""
        if not HAS_WINDOWS_CAPTURE or not handle or not user32.IsWindow(handle):
            return None

        best = find_best_capture_hwnd(handle)
        hwnds = [best] + ([handle] if best != handle else [])

        # draw_border is only honoured on newer Windows builds; on older ones
        # the session raises as soon as it starts, so retry without it.
        option_sets = [
            {"cursor_capture": False, "draw_border": False},
            {"cursor_capture": False},
        ]
        reported = [False]

        for hwnd in hwnds:
            for opts in option_sets:
                try:
                    session = WindowsCapture(window_hwnd=hwnd, **opts)

                    @session.event
                    def on_frame_arrived(frame, capture_control):
                        try:
                            buf = frame.frame_buffer   # (H, W, 4) BGRA, zero-copy
                            h_px, w_px = buf.shape[0], buf.shape[1]
                            # tobytes() copies out of the native mapped frame
                            # before it is released; 'BGRX' swaps channels and
                            # drops alpha in a single C-level pass.
                            on_frame(Image.frombytes(
                                "RGB", (w_px, h_px), buf.tobytes(), "raw", "BGRX"))
                        except Exception as exc:
                            # Report once rather than silently degrading to the
                            # slow fallback with no explanation.
                            if not reported[0]:
                                reported[0] = True
                                print(f"WGC frame decode failed: "
                                      f"{type(exc).__name__}: {exc}")

                    @session.event
                    def on_closed():
                        pass

                    return session.start_free_threaded()
                except Exception as exc:
                    print(f"WGC unavailable on HWND {hwnd} with {opts}: {exc}")
        return None

    def grab(self, handle):
        return capture_printwindow(handle)
