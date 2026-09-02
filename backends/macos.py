"""macOS capture backend, built on Quartz.

NOT YET VERIFIED ON REAL HARDWARE. This backend was written against the Quartz
and AppKit APIs but has not been executed on a Mac. Treat it as a starting
point: the shapes are right, but expect to fix details. Reports welcome.

Requires `pyobjc-framework-Quartz`, and the user must grant **Screen Recording**
permission (System Settings -> Privacy & Security -> Screen Recording).
Without it macOS silently returns desktop-wallpaper-only images rather than
raising, which this backend detects as best it can.

Quartz's `CGWindowListCreateImage` is used rather than ScreenCaptureKit. It is
formally deprecated as of macOS 14 but still functional, synchronous, and vastly
simpler; ScreenCaptureKit is the right long-term target and is the natural next
step for this file.
"""

from __future__ import annotations

import os
import sys

from PIL import Image

from .base import CaptureBackend

try:
    import Quartz
    from Quartz import (
        CGWindowListCopyWindowInfo,
        CGWindowListCreateImage,
        kCGNullWindowID,
        kCGWindowListOptionOnScreenOnly,
        kCGWindowListExcludeDesktopElements,
        kCGWindowListOptionIncludingWindow,
        kCGWindowImageBoundsIgnoreFraming,
        CGRectNull,
    )
    HAS_QUARTZ = True
except Exception:
    HAS_QUARTZ = False


def _cgimage_to_pil(cgimage):
    """Convert a CGImageRef to a PIL RGB image."""
    if cgimage is None:
        return None
    width = Quartz.CGImageGetWidth(cgimage)
    height = Quartz.CGImageGetHeight(cgimage)
    if width == 0 or height == 0:
        return None

    provider = Quartz.CGImageGetDataProvider(cgimage)
    data = Quartz.CGDataProviderCopyData(provider)
    buf = bytes(data)

    # Rows are padded to a stride, so the buffer is usually wider than
    # width * 4 and must be cropped rather than reshaped blindly.
    stride = Quartz.CGImageGetBytesPerRow(cgimage)
    bpp = Quartz.CGImageGetBitsPerPixel(cgimage) // 8
    if bpp < 3:
        return None

    raw_mode = "BGRA" if bpp == 4 else "BGR"
    img = Image.frombytes("RGB", (stride // bpp, height), buf, "raw", raw_mode)
    if img.width != width:
        img = img.crop((0, 0, width, height))
    return img


class MacOSBackend(CaptureBackend):
    """Per-window capture via Quartz window lists and images."""

    name = "Quartz (macOS)"

    def __init__(self):
        self.degraded_reason = (
            "macOS support is experimental and unverified on real hardware. "
            "Capture polls rather than streams, so framerates are lower than on "
            "Windows."
        )

    @classmethod
    def is_available(cls):
        if sys.platform != "darwin":
            return False, "not running on macOS"
        if not HAS_QUARTZ:
            return False, ("pyobjc-framework-Quartz is not installed - "
                           "run: pip install pyobjc-framework-Quartz")
        return True, ""

    # -- enumeration -----------------------------------------------------

    def list_windows(self):
        own_pid = os.getpid()
        options = kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements
        info = CGWindowListCopyWindowInfo(options, kCGNullWindowID) or []

        windows = []
        for entry in info:
            # Layer 0 is the normal application layer; menu bars, docks and
            # overlays live above it and are not useful capture targets.
            if entry.get("kCGWindowLayer", 1) != 0:
                continue
            if entry.get("kCGWindowOwnerPID") == own_pid:
                continue
            title = (entry.get("kCGWindowName") or "").strip()
            owner = (entry.get("kCGWindowOwnerName") or "").strip()
            wid = entry.get("kCGWindowNumber")
            if wid is None:
                continue
            bounds = entry.get("kCGWindowBounds") or {}
            if bounds.get("Width", 0) < 50 or bounds.get("Height", 0) < 50:
                continue
            # Window names are often empty unless Screen Recording is granted,
            # so fall back to the owning application's name.
            label = f"{owner} - {title}" if title and owner else (title or owner)
            if label:
                windows.append((label, int(wid)))
        return sorted(windows, key=lambda x: x[0].lower())

    def list_monitors(self):
        monitors = []
        err, display_ids, count = Quartz.CGGetActiveDisplayList(16, None, None)
        if err:
            return monitors
        main = Quartz.CGMainDisplayID()
        for did in list(display_ids)[:count]:
            rect = Quartz.CGDisplayBounds(did)
            monitors.append({
                "device": f"Display {did}",
                "x": int(rect.origin.x),
                "y": int(rect.origin.y),
                "width": int(rect.size.width),
                "height": int(rect.size.height),
                "is_primary": bool(did == main),
            })
        return monitors

    def window_bounds(self, handle):
        info = CGWindowListCopyWindowInfo(
            kCGWindowListOptionIncludingWindow, handle) or []
        for entry in info:
            b = entry.get("kCGWindowBounds")
            if b:
                return (int(b["X"]), int(b["Y"]), int(b["Width"]), int(b["Height"]))
        return None

    # -- capture ---------------------------------------------------------

    def open_stream(self, handle, on_frame):
        # Quartz has no push-based per-window stream. ScreenCaptureKit does, and
        # is where a streaming implementation belongs; until then the render
        # loop polls grab().
        return None

    def grab(self, handle):
        try:
            cgimage = CGWindowListCreateImage(
                CGRectNull,
                kCGWindowListOptionIncludingWindow,
                handle,
                kCGWindowImageBoundsIgnoreFraming,
            )
            return _cgimage_to_pil(cgimage)
        except Exception:
            return None
