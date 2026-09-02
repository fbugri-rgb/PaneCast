"""Linux capture backend, built on X11.

NOT YET VERIFIED ON REAL HARDWARE. Written against python-xlib but never
executed on a Linux desktop. Treat it as a starting point.

Requires `python-xlib`. Windows are enumerated through the EWMH
`_NET_CLIENT_LIST` property, which any standards-compliant window manager
publishes, and captured with `XGetImage`.

**Wayland is not supported.** Wayland deliberately forbids one client from
reading another client's pixels; the sanctioned route is PipeWire via
`xdg-desktop-portal`, which requires an interactive permission prompt per
capture and a substantially different architecture. This backend detects a
Wayland session and reports itself unavailable rather than silently capturing
black frames. Applications running under XWayland remain capturable from an X11
session.

Occluded windows are handled by asking the X Composite extension to redirect
each target window to an offscreen pixmap. Without a compositing manager
running, capturing a covered window may return stale or blank content.
"""

from __future__ import annotations

import os
import sys

from PIL import Image

from .base import CaptureBackend

try:
    from Xlib import X, display as xdisplay
    from Xlib.error import XError
    HAS_XLIB = True
except Exception:
    HAS_XLIB = False


def _is_wayland():
    if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
        return True
    return bool(os.environ.get("WAYLAND_DISPLAY"))


class LinuxBackend(CaptureBackend):
    """Per-window capture via X11."""

    name = "X11 (Linux)"

    def __init__(self):
        self._display = None
        self._composite = False
        self.degraded_reason = (
            "Linux support is experimental and unverified on real hardware. "
            "Capture polls rather than streams, so framerates are lower than on "
            "Windows."
        )

    @classmethod
    def is_available(cls):
        if not sys.platform.startswith("linux"):
            return False, "not running on Linux"
        if _is_wayland():
            return False, (
                "Wayland sessions do not permit capturing another application's "
                "window. Log in to an X11/Xorg session instead."
            )
        if not HAS_XLIB:
            return False, "python-xlib is not installed - run: pip install python-xlib"
        if not os.environ.get("DISPLAY"):
            return False, "no DISPLAY set - not running under an X server"
        return True, ""

    # -- process setup ---------------------------------------------------

    def prepare_process(self):
        self._display = xdisplay.Display()
        # Composite redirection is what keeps occluded windows renderable.
        try:
            self._display.query_extension("Composite")
            self._composite = True
        except Exception:
            self._composite = False
            self.degraded_reason = (
                "The X Composite extension is unavailable, so windows hidden "
                "behind others may capture as blank or stale."
            )

    def shutdown(self):
        if self._display is not None:
            try:
                self._display.close()
            except Exception:
                pass
            self._display = None

    def _dpy(self):
        if self._display is None:
            self.prepare_process()
        return self._display

    # -- enumeration -----------------------------------------------------

    def _atom(self, name):
        return self._dpy().intern_atom(name)

    def _window_title(self, win):
        for prop in ("_NET_WM_NAME", "WM_NAME"):
            try:
                value = win.get_full_property(self._atom(prop), X.AnyPropertyType)
            except XError:
                continue
            if value and value.value:
                raw = value.value
                if isinstance(raw, bytes):
                    return raw.decode("utf-8", "replace").strip()
                return str(raw).strip()
        return ""

    def list_windows(self):
        dpy = self._dpy()
        root = dpy.screen().root
        own_pid = os.getpid()
        pid_atom = self._atom("_NET_WM_PID")

        try:
            prop = root.get_full_property(
                self._atom("_NET_CLIENT_LIST"), X.AnyPropertyType)
        except XError:
            return []
        if not prop:
            return []

        windows = []
        for wid in prop.value:
            try:
                win = dpy.create_resource_object("window", wid)
                geom = win.get_geometry()
                if geom.width < 50 or geom.height < 50:
                    continue
                pid_prop = win.get_full_property(pid_atom, X.AnyPropertyType)
                if pid_prop and pid_prop.value and int(pid_prop.value[0]) == own_pid:
                    continue
                title = self._window_title(win)
                if title:
                    windows.append((title, int(wid)))
            except XError:
                continue
        return sorted(windows, key=lambda x: x[0].lower())

    def list_monitors(self):
        dpy = self._dpy()
        monitors = []
        # RandR reports each physical output; without it we can only see the
        # single logical screen, which on a multi-head setup is the whole span.
        try:
            from Xlib.ext import randr
            root = dpy.screen().root
            resources = randr.get_screen_resources(root)
            primary = randr.get_output_primary(root).output
            for output in resources.outputs:
                info = randr.get_output_info(root, output, resources.config_timestamp)
                if info.crtc == 0:
                    continue
                crtc = randr.get_crtc_info(root, info.crtc, resources.config_timestamp)
                monitors.append({
                    "device": info.name,
                    "x": crtc.x,
                    "y": crtc.y,
                    "width": crtc.width,
                    "height": crtc.height,
                    "is_primary": bool(output == primary),
                })
        except Exception:
            pass

        if not monitors:
            screen = dpy.screen()
            monitors.append({
                "device": "Screen 0",
                "x": 0, "y": 0,
                "width": screen.width_in_pixels,
                "height": screen.height_in_pixels,
                "is_primary": True,
            })
        return monitors

    def window_bounds(self, handle):
        try:
            dpy = self._dpy()
            win = dpy.create_resource_object("window", handle)
            geom = win.get_geometry()
            # Geometry is relative to the parent, so translate to root space.
            coords = win.translate_coords(dpy.screen().root, 0, 0)
            return (-coords.x, -coords.y, geom.width, geom.height)
        except XError:
            return None

    # -- capture ---------------------------------------------------------

    def open_stream(self, handle, on_frame):
        # X11 has no push-based per-window frame stream; the render loop polls.
        # Redirecting the window to an offscreen pixmap is what allows an
        # occluded window to keep producing content.
        if self._composite:
            try:
                from Xlib.ext import composite
                win = self._dpy().create_resource_object("window", handle)
                composite.redirect_window(win, composite.RedirectAutomatic)
            except Exception:
                pass
        return None

    def grab(self, handle):
        try:
            dpy = self._dpy()
            win = dpy.create_resource_object("window", handle)
            geom = win.get_geometry()
            if geom.width <= 0 or geom.height <= 0:
                return None
            raw = win.get_image(0, 0, geom.width, geom.height, X.ZPixmap, 0xFFFFFFFF)
            data = raw.data
            if isinstance(data, str):
                data = data.encode("latin-1")
            # X gives 32-bit BGRX on virtually all modern truecolour visuals.
            return Image.frombytes(
                "RGB", (geom.width, geom.height), data, "raw", "BGRX")
        except Exception:
            return None
