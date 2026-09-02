"""Capture backend selection.

`get_backend()` returns the first backend that reports itself available on the
current platform. Platform modules are imported lazily so that, for example, a
missing python-xlib on Linux does not stop the Windows backend from loading.

Support status:

===========  ===================================  ==================
Platform     Backend                              Verified
===========  ===================================  ==================
Windows      Windows Graphics Capture / GDI       yes
macOS        Quartz                               no - see macos.py
Linux (X11)  X11 + Composite                      no - see linux.py
Linux (Wayland)  unsupported by design            n/a
===========  ===================================  ==================
"""

from __future__ import annotations

import sys

from .base import CaptureBackend

__all__ = ["CaptureBackend", "get_backend", "describe_availability"]


def _candidates():
    """Yield backend classes that could plausibly run here, best first."""
    if sys.platform == "win32":
        try:
            from .windows import WindowsBackend
            yield WindowsBackend
        except Exception:
            pass
    elif sys.platform == "darwin":
        try:
            from .macos import MacOSBackend
            yield MacOSBackend
        except Exception:
            pass
    elif sys.platform.startswith("linux"):
        try:
            from .linux import LinuxBackend
            yield LinuxBackend
        except Exception:
            pass


class UnsupportedBackend(CaptureBackend):
    """Stand-in so the UI can start and explain itself instead of crashing."""

    name = "unsupported platform"

    def __init__(self, reason):
        self.degraded_reason = reason

    @classmethod
    def is_available(cls):
        return False, "no capture backend for this platform"

    def list_windows(self):
        return []

    def list_monitors(self):
        return []

    def window_bounds(self, handle):
        return None

    def grab(self, handle):
        return None


def describe_availability():
    """Return ``[(class name, available, reason)]`` for diagnostics."""
    rows = []
    for cls in _candidates():
        try:
            available, reason = cls.is_available()
        except Exception as exc:
            available, reason = False, f"{type(exc).__name__}: {exc}"
        rows.append((cls.__name__, available, reason))
    return rows


def get_backend():
    """Return an instantiated backend for this platform.

    Falls back to `UnsupportedBackend`, which lets the app open and show why it
    cannot capture rather than failing at import.
    """
    reasons = []
    for cls in _candidates():
        try:
            available, reason = cls.is_available()
        except Exception as exc:
            reasons.append(f"{cls.__name__}: {type(exc).__name__}: {exc}")
            continue
        if available:
            try:
                return cls()
            except Exception as exc:
                reasons.append(f"{cls.__name__}: {type(exc).__name__}: {exc}")
        else:
            reasons.append(f"{cls.__name__}: {reason}")

    if reasons:
        detail = "; ".join(reasons)
    else:
        detail = f"no backend implemented for platform {sys.platform!r}"
    return UnsupportedBackend(detail)
