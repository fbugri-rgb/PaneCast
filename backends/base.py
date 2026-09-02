"""Platform-independent capture interface.

Every backend exposes the same small surface. The UI never imports a platform
module directly; it asks `backends.get_backend()` for whichever one reports
itself available on the current system.

A backend supplies frames in one of two ways:

* **Streaming** — `open_stream()` returns an object with a `stop()` method and
  pushes PIL images to a callback from its own thread. This is what keeps a
  window rendering while it is occluded or off-screen.
* **Polling** — `grab()` returns a single PIL image on demand.

`open_stream()` may return ``None``, in which case the render loop falls back
to calling `grab()` each tick. Implementing only `grab()` is a valid backend.
"""

from __future__ import annotations


class CaptureBackend:
    """Interface every platform backend implements."""

    #: Short name shown in the UI, e.g. "Windows Graphics Capture".
    name = "unimplemented"

    #: Set by subclasses when the fast path is unavailable and a slower
    #: fallback is in use, so the UI can say so honestly.
    degraded_reason = None

    @classmethod
    def is_available(cls):
        """Return ``(available, reason)``.

        ``reason`` explains the failure when unavailable, and is shown to the
        user, so it should name the missing dependency or unsupported
        configuration rather than being a generic message.
        """
        return False, "not implemented"

    # -- process setup ---------------------------------------------------

    def prepare_process(self):
        """Apply process-wide settings such as DPI awareness. Optional."""

    def shutdown(self):
        """Release anything `prepare_process` claimed. Optional."""

    # -- enumeration -----------------------------------------------------

    def list_windows(self):
        """Return ``[(title, handle)]`` for user-visible windows.

        Handles are opaque to the UI and only ever passed back to the backend
        that produced them. Windows belonging to this process must be excluded
        so the app cannot capture itself.
        """
        raise NotImplementedError

    def list_monitors(self):
        """Return ``[{x, y, width, height, is_primary, device}]``.

        Coordinates are in the platform's virtual-desktop space and are used
        directly to position the fullscreen projection window.
        """
        raise NotImplementedError

    def window_bounds(self, handle):
        """Return ``(x, y, width, height)`` for a window, or ``None``."""
        raise NotImplementedError

    # -- capture ---------------------------------------------------------

    def open_stream(self, handle, on_frame):
        """Begin streaming frames for `handle`, calling ``on_frame(image)``.

        Returns an object with a ``stop()`` method, or ``None`` if this backend
        cannot stream and the caller should poll `grab()` instead.
        """
        return None

    def grab(self, handle):
        """Return a single PIL image for `handle`, or ``None`` on failure."""
        raise NotImplementedError
