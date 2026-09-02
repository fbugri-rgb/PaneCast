"""PaneCast - cast individual application windows to a second display.

Platform-specific capture lives in the `backends` package; this module is the
tkinter interface and the render loop, and talks only to the backend interface.
"""

import tkinter as tk
from tkinter import ttk, messagebox
from PIL import Image, ImageTk
import json
import os

import backends

APP_NAME = "PaneCast"

# (label, frames per second). 0 means "as fast as the render loop manages".
FPS_OPTIONS = [
    ("15 fps (lowest CPU)", 15),
    ("30 fps", 30),
    ("60 fps", 60),
    ("Unlimited", 0),
]

# (label, fraction of the screen the inset occupies)
PIP_SIZE_OPTIONS = [("Small (25%)", 0.25), ("Medium (33%)", 0.33),
                    ("Large (38%)", 0.38), ("Extra large (50%)", 0.50)]

# (label, (horizontal, vertical)) where horizontal is 'l'/'r' and vertical 't'/'b'
PIP_CORNER_OPTIONS = [("Bottom right", ("r", "b")), ("Bottom left", ("l", "b")),
                      ("Top right", ("r", "t")), ("Top left", ("l", "t"))]


def settings_path():
    """Per-user settings file, kept out of the install directory."""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_NAME, "settings.json")


def load_settings():
    """Return saved settings, or an empty dict if there are none or they are unreadable."""
    try:
        with open(settings_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_settings(data):
    """Best-effort persist. Never let a settings failure interrupt projection."""
    try:
        path = settings_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except Exception:
        pass


class PaneCastControl:
    def __init__(self, root):
        self.root = root
        self.backend = backends.get_backend()
        self.backend.prepare_process()
        self.root.title("PaneCast")
        self.root.geometry("560x660")
        self.root.resizable(False, False)
        
        style = ttk.Style()
        style.theme_use('clam')
        
        self.windows_map = {}
        self.monitors_list = []
        self.projector_window = None
        self.is_projecting = False
        
        self.target_hwnd1 = None
        self.target_hwnd2 = None
        self.target_monitor = None
        
        # PhotoImage objects are reused across frames and re-created only
        # when the target size changes; a fresh one per frame was the single
        # most expensive operation in the render loop.
        self.photos = {}

        # Frame sequence counters. The capture threads bump these; the render
        # loop compares against what it last drew so identical frames are not
        # rescaled and re-uploaded.
        self.frame_seq1 = 0
        self.frame_seq2 = 0
        self.rendered_seq1 = -1
        self.rendered_seq2 = -1
        self._frame_error_logged = False
        # Last (cx, cy) each slot was placed at, so the costly canvas
        # coords/itemconfigure calls can be skipped when nothing moved.
        self._blit_state = {}
        self._pip_visible = None
        self.frame_interval_ms = 33      # replaced from the saved settings below
        
        self.item_id1 = None
        self.item_id2 = None
        self.rect_pip_id = None
        
        self.capture_session1 = None
        self.capture_control1 = None
        self.latest_pil_img1 = None

        self.capture_session2 = None
        self.capture_control2 = None
        self.latest_pil_img2 = None

        self._build_ui()
        self.refresh_all()
        self._apply_settings()

    def _build_ui(self):
        main_frame = ttk.Frame(self.root, padding="15 15 15 15")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Header
        title_label = ttk.Label(main_frame, text="PaneCast", font=("Segoe UI", 14, "bold"))
        title_label.pack(anchor="w", pady=(0, 2))
        
        subtitle = ttk.Label(main_frame, text="Cast individual windows to a TV - the rest of your screen stays private", font=("Segoe UI", 9))
        subtitle.pack(anchor="w", pady=(0, 10))

        # Window 1 Selection Section
        win1_group = ttk.LabelFrame(main_frame, text=" 1st Application Window (Primary, e.g., IntelliJ) ", padding="8 8 8 8")
        win1_group.pack(fill=tk.X, pady=(0, 8))

        self.win1_combo = ttk.Combobox(win1_group, state="readonly", font=("Segoe UI", 9))
        self.win1_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        btn_refresh_wins = ttk.Button(win1_group, text="Refresh List", command=self.refresh_windows)
        btn_refresh_wins.pack(side=tk.RIGHT)

        # Window 2 Selection Section
        win2_group = ttk.LabelFrame(main_frame, text=" 2nd Application Window (Optional, e.g., Dammen Game) ", padding="8 8 8 8")
        win2_group.pack(fill=tk.X, pady=(0, 8))

        self.win2_combo = ttk.Combobox(win2_group, state="readonly", font=("Segoe UI", 9))
        self.win2_combo.pack(fill=tk.X, expand=True)

        # Dual Window Layout Mode
        layout_group = ttk.LabelFrame(main_frame, text=" Dual Window Layout Style ", padding="8 8 8 8")
        layout_group.pack(fill=tk.X, pady=(0, 8))

        self.layout_combo = ttk.Combobox(
            layout_group, state="readonly", font=("Segoe UI", 9),
            values=[
                "Side-by-Side (Left / Right Split)",
                "Top / Bottom Split",
                "Picture-in-Picture (Game Inset Overlay)"
            ]
        )
        self.layout_combo.current(0)
        self.layout_combo.pack(fill=tk.X)
        self.layout_combo.bind("<<ComboboxSelected>>", self._on_layout_changed)

        # Picture-in-picture tuning. Only meaningful in PiP mode, so it is
        # enabled and disabled alongside the layout selection.
        self.pip_frame = ttk.Frame(layout_group)
        self.pip_frame.pack(fill=tk.X, pady=(8, 0))

        ttk.Label(self.pip_frame, text="Inset size:", font=("Segoe UI", 9)).pack(side=tk.LEFT)
        self.pip_size_combo = ttk.Combobox(
            self.pip_frame, state="readonly", font=("Segoe UI", 9), width=16,
            values=[label for label, _ in PIP_SIZE_OPTIONS]
        )
        self.pip_size_combo.current(2)
        self.pip_size_combo.pack(side=tk.LEFT, padx=(6, 14))
        self.pip_size_combo.bind("<<ComboboxSelected>>", self._on_pip_changed)

        ttk.Label(self.pip_frame, text="Corner:", font=("Segoe UI", 9)).pack(side=tk.LEFT)
        self.pip_corner_combo = ttk.Combobox(
            self.pip_frame, state="readonly", font=("Segoe UI", 9), width=14,
            values=[label for label, _ in PIP_CORNER_OPTIONS]
        )
        self.pip_corner_combo.current(0)
        self.pip_corner_combo.pack(side=tk.LEFT, padx=(6, 0))
        self.pip_corner_combo.bind("<<ComboboxSelected>>", self._on_pip_changed)

        # Display Selection Section
        disp_group = ttk.LabelFrame(main_frame, text=" Target TV / Extended Display ", padding="8 8 8 8")
        disp_group.pack(fill=tk.X, pady=(0, 8))

        self.disp_combo = ttk.Combobox(disp_group, state="readonly", font=("Segoe UI", 9))
        self.disp_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        btn_refresh_disp = ttk.Button(disp_group, text="Detect Screens", command=self.refresh_monitors)
        btn_refresh_disp.pack(side=tk.RIGHT)

        # Target framerate
        perf_group = ttk.LabelFrame(main_frame, text=" Performance ", padding="8 8 8 8")
        perf_group.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(perf_group, text="Target framerate:", font=("Segoe UI", 9)).pack(side=tk.LEFT)
        self.fps_combo = ttk.Combobox(
            perf_group, state="readonly", font=("Segoe UI", 9), width=20,
            values=[label for label, _ in FPS_OPTIONS]
        )
        self.fps_combo.current(1)
        self.fps_combo.pack(side=tk.LEFT, padx=(6, 0))
        self.fps_combo.bind("<<ComboboxSelected>>", self._on_fps_changed)

        # Multi-monitor notice label
        self.monitor_notice = ttk.Label(main_frame, text="", font=("Segoe UI", 8, "bold"))
        self.monitor_notice.pack(anchor="w", pady=(0, 4))

        # Status indicator
        degraded = self.backend.degraded_reason
        engine_status = f"Engine: {self.backend.name}"
        status_lbl = ttk.Label(
            main_frame, text=engine_status, font=("Segoe UI", 8, "bold"),
            foreground="#d97706" if degraded else "#059669"
        )
        status_lbl.pack(anchor="w", pady=(0, 2 if degraded else 8))

        # Say plainly when a slower or unverified path is in use, rather than
        # letting the user assume the fast one is running.
        if degraded:
            ttk.Label(
                main_frame, text=degraded, font=("Segoe UI", 8),
                foreground="#d97706", wraplength=520, justify="left"
            ).pack(anchor="w", pady=(0, 8))

        # Controls Section
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill=tk.X, pady=(4, 4))

        self.btn_start = tk.Button(
            btn_frame, text="START PROJECTING", font=("Segoe UI", 11, "bold"),
            bg="#2563eb", fg="white", activebackground="#1d4ed8", activeforeground="white",
            relief=tk.FLAT, bd=0, pady=10, command=self.start_projection
        )
        self.btn_start.pack(fill=tk.X, pady=(0, 6))

        self.btn_stop = tk.Button(
            btn_frame, text="STOP PROJECTING", font=("Segoe UI", 10, "bold"),
            bg="#dc2626", fg="white", activebackground="#b91c1c", activeforeground="white",
            relief=tk.FLAT, bd=0, pady=8, command=self.stop_projection, state=tk.DISABLED
        )
        self.btn_stop.pack(fill=tk.X)

        # Footer
        footer = ttk.Label(main_frame, text="💡 Tip: Press 'Esc' on the TV screen anytime to stop projecting.", font=("Segoe UI", 8, "italic"))
        footer.pack(side=tk.BOTTOM, pady=(4, 0))

    # ---- option accessors -------------------------------------------------

    def _fps_interval_ms(self):
        """Milliseconds between render ticks for the selected target framerate."""
        idx = self.fps_combo.current()
        fps = FPS_OPTIONS[idx][1] if 0 <= idx < len(FPS_OPTIONS) else 30
        return 1 if fps <= 0 else max(1, round(1000 / fps))

    def _pip_fraction(self):
        idx = self.pip_size_combo.current()
        return PIP_SIZE_OPTIONS[idx][1] if 0 <= idx < len(PIP_SIZE_OPTIONS) else 0.38

    def _pip_corner(self):
        idx = self.pip_corner_combo.current()
        return PIP_CORNER_OPTIONS[idx][1] if 0 <= idx < len(PIP_CORNER_OPTIONS) else ("r", "b")

    def _on_fps_changed(self, event=None):
        self.frame_interval_ms = self._fps_interval_ms()

    def _on_pip_changed(self, event=None):
        # Force the next tick to repaint so the change is visible immediately
        # instead of waiting for the next captured frame.
        self.rendered_seq1 = self.rendered_seq2 = -1
        self._blit_state.clear()
        self._pip_visible = None

    def _on_layout_changed(self, event=None):
        is_pip = self.layout_combo.current() == 2
        state = "readonly" if is_pip else "disabled"
        self.pip_size_combo.config(state=state)
        self.pip_corner_combo.config(state=state)
        self._on_pip_changed()

    # ---- settings ---------------------------------------------------------

    def _apply_settings(self):
        """Restore the previous session's choices where they still make sense."""
        cfg = load_settings()

        title = cfg.get("window1")
        if title and title in self.windows_map:
            self.win1_combo.set(title)

        title2 = cfg.get("window2")
        if title2 and title2 in self.windows_map:
            self.win2_combo.set(title2)

        for key, combo, table in (
            ("layout", self.layout_combo, range(3)),
            ("fps", self.fps_combo, FPS_OPTIONS),
            ("pip_size", self.pip_size_combo, PIP_SIZE_OPTIONS),
            ("pip_corner", self.pip_corner_combo, PIP_CORNER_OPTIONS),
        ):
            idx = cfg.get(key)
            if isinstance(idx, int) and 0 <= idx < len(table):
                combo.current(idx)

        # A saved monitor index is only meaningful if that monitor still exists.
        mon = cfg.get("monitor")
        if isinstance(mon, int) and 0 <= mon < len(self.monitors_list):
            self.disp_combo.current(mon)

        self.frame_interval_ms = self._fps_interval_ms()
        self._on_layout_changed()

    def _save_settings(self):
        save_settings({
            "window1": self.win1_combo.get(),
            "window2": self.win2_combo.get(),
            "layout": self.layout_combo.current(),
            "monitor": self.disp_combo.current(),
            "fps": self.fps_combo.current(),
            "pip_size": self.pip_size_combo.current(),
            "pip_corner": self.pip_corner_combo.current(),
        })

    def refresh_all(self):
        self.refresh_windows()
        self.refresh_monitors()

    def refresh_windows(self):
        wins = self.backend.list_windows()
        self.windows_map = {f"{title}": hwnd for title, hwnd in wins}
        titles = list(self.windows_map.keys())
        
        self.win1_combo['values'] = titles
        if titles:
            self.win1_combo.current(0)
        else:
            self.win1_combo.set("No visible windows found")

        win2_titles = ["(None - Single Window Mode)"] + titles
        self.win2_combo['values'] = win2_titles
        self.win2_combo.current(0)

    def refresh_monitors(self):
        self.monitors_list = self.backend.list_monitors()
        options = []
        default_index = 0
        
        for idx, m in enumerate(self.monitors_list):
            m_type = "Main Laptop Screen" if m['is_primary'] else "TV / Extended Screen"
            label = f"Monitor {idx + 1}: {m['width']}x{m['height']} ({m_type})"
            options.append(label)
            if not m['is_primary']:
                default_index = idx

        self.disp_combo['values'] = options
        if options:
            self.disp_combo.current(default_index)

        if len(self.monitors_list) == 1:
            self.monitor_notice.config(
                text="⚠️ Only 1 Screen Detected! Press Win + P -> Select 'EXTEND' then click 'Detect Screens'.",
                foreground="#d97706"
            )
        else:
            self.monitor_notice.config(
                text=f"✅ {len(self.monitors_list)} Screens Detected! Target TV is ready.",
                foreground="#059669"
            )

    def _start_isolated_session(self, target_handle, slot):
        """Ask the backend to stream frames into slot 1 or 2.

        Returns ``(stream, stream)`` or ``(None, None)``. Backends that cannot
        stream return None here and the render loop polls ``backend.grab()``
        instead.
        """
        if not target_handle:
            return None, None

        img_attr = f'latest_pil_img{slot}'
        seq_attr = f'frame_seq{slot}'

        def on_frame(image):
            # Called from the backend's capture thread. Assignment is atomic
            # under the GIL and the sequence bump is what the render loop
            # watches, so no lock is needed for this single-producer case.
            setattr(self, img_attr, image)
            setattr(self, seq_attr, getattr(self, seq_attr) + 1)

        try:
            stream = self.backend.open_stream(target_handle, on_frame)
        except Exception as exc:
            print(f"capture stream failed: {type(exc).__name__}: {exc}")
            return None, None

        return (stream, stream) if stream is not None else (None, None)

    def start_projection(self):
        if self.is_projecting:
            return

        selected_win1_title = self.win1_combo.get()
        if not selected_win1_title or selected_win1_title not in self.windows_map:
            messagebox.showwarning("Selection Required", "Please select a valid 1st application window.")
            return

        disp_idx = self.disp_combo.current()
        if disp_idx < 0 or disp_idx >= len(self.monitors_list):
            messagebox.showwarning("Display Required", "Please select a target display.")
            return

        self.target_hwnd1 = self.windows_map[selected_win1_title]
        
        selected_win2_title = self.win2_combo.get()
        if selected_win2_title and selected_win2_title in self.windows_map:
            self.target_hwnd2 = self.windows_map[selected_win2_title]
        else:
            self.target_hwnd2 = None

        self.target_monitor = self.monitors_list[disp_idx]
        self._save_settings()
        self.latest_pil_img1 = None
        self.latest_pil_img2 = None
        self.frame_seq1 = 0
        self.frame_seq2 = 0
        self.rendered_seq1 = -1
        self.rendered_seq2 = -1
        self._frame_error_logged = False
        self.photos.clear()
        self._blit_state.clear()
        self._pip_visible = None

        # Start isolated WGC session for Window 1
        self.capture_session1, self.capture_control1 = self._start_isolated_session(self.target_hwnd1, 1)

        # Start isolated WGC session for Window 2
        if self.target_hwnd2:
            self.capture_session2, self.capture_control2 = self._start_isolated_session(self.target_hwnd2, 2)

        # Create Top-level Fullscreen Projector Window on target TV
        self.projector_window = tk.Toplevel(self.root)
        self.projector_window.title("PaneCast Display")
        
        mon = self.target_monitor
        geometry_str = f"{mon['width']}x{mon['height']}+{mon['x']}+{mon['y']}"
        self.projector_window.geometry(geometry_str)
        self.projector_window.overrideredirect(True)
        self.projector_window.configure(bg="black")

        # ESC or Q to exit
        self.projector_window.bind("<Escape>", lambda e: self.stop_projection())
        self.projector_window.bind("<q>", lambda e: self.stop_projection())
        self.projector_window.protocol("WM_DELETE_WINDOW", self.stop_projection)

        # Canvas with zero padding
        self.canvas = tk.Canvas(
            self.projector_window, width=mon['width'], height=mon['height'],
            bg="black", highlightthickness=0
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)

        # Pre-allocate canvas display item IDs for high-speed zero-alloc updates
        self.rect_pip_id = self.canvas.create_rectangle(0, 0, 0, 0, fill="#2563eb", outline="white", width=2, state="hidden")
        self.item_id1 = self.canvas.create_image(0, 0, anchor=tk.CENTER)
        self.item_id2 = self.canvas.create_image(0, 0, anchor=tk.CENTER)

        self.is_projecting = True
        self.btn_start.config(state=tk.DISABLED, bg="#94a3b8")
        self.btn_stop.config(state=tk.NORMAL, bg="#dc2626")

        # Start high-speed refresh rate update loop
        self.update_projection()

    def _blit(self, item_id, slot, pil_img, box_w, box_h, cx, cy):
        """Scale pil_img to fit box_w x box_h and centre it on (cx, cy).

        Reuses the slot's PhotoImage via .paste() whenever the output size is
        unchanged. Allocating a fresh PhotoImage every frame was the single
        most expensive operation in the old render loop.
        """
        iw, ih = pil_img.size
        if iw <= 0 or ih <= 0:
            return

        ratio = min(box_w / iw, box_h / ih)
        nw, nh = max(1, int(iw * ratio)), max(1, int(ih * ratio))

        # Downscaling with NEAREST aliases badly, and BILINEAR is cheap when the
        # output is small. Upscaling is the reverse, so choose per direction.
        resample = Image.Resampling.BILINEAR if ratio < 1.0 else Image.Resampling.NEAREST
        resized = pil_img.resize((nw, nh), resample)
        if resized.mode != 'RGB':
            resized = resized.convert('RGB')

        photo = self.photos.get(slot)
        if photo is None or photo.width() != nw or photo.height() != nh:
            photo = ImageTk.PhotoImage(resized)
            self.photos[slot] = photo
            self.canvas.itemconfigure(item_id, image=photo, state="normal")
            self._blit_state[slot] = None
        else:
            # Pasting into the existing Tk image updates the canvas in place.
            photo.paste(resized)

        # Only touch coords/itemconfigure when the item actually moved or was
        # hidden. At 4K those two calls cost more than the paste itself.
        if self._blit_state.get(slot) != (cx, cy):
            self._blit_state[slot] = (cx, cy)
            self.canvas.coords(item_id, cx, cy)
            self.canvas.itemconfigure(item_id, state="normal")

    def _hide(self, item_id, slot):
        """Hide a canvas item and invalidate its cached placement."""
        if self._blit_state.get(slot) is not None:
            self._blit_state[slot] = None
            self.canvas.itemconfigure(item_id, state="hidden")

    def _set_pip_visible(self, visible):
        if self._pip_visible != visible:
            self._pip_visible = visible
            self.canvas.itemconfigure(self.rect_pip_id,
                                      state="normal" if visible else "hidden")

    def _read_slot(self, slot):
        """Return (image, sequence) for a capture slot.

        The sequence only advances when a genuinely new frame is available, so
        the render loop can skip work when nothing has changed.
        """
        hwnd = getattr(self, f'target_hwnd{slot}')
        if not hwnd:
            return None, getattr(self, f'rendered_seq{slot}')

        if getattr(self, f'capture_control{slot}') is not None:
            img = getattr(self, f'latest_pil_img{slot}')
            if img is not None:
                return img, getattr(self, f'frame_seq{slot}')

        # No WGC stream, or it has not produced its first frame yet. PrintWindow
        # returns a fresh frame on every call, so it always counts as new.
        return self.backend.grab(hwnd), getattr(self, f'rendered_seq{slot}') + 1

    def update_projection(self):
        if not self.is_projecting or not self.projector_window:
            return

        img1, seq1 = self._read_slot(1)
        img2, seq2 = self._read_slot(2)

        # Nothing new arrived since the last paint - skip the rescale and the
        # texture upload entirely.
        if seq1 == self.rendered_seq1 and seq2 == self.rendered_seq2:
            self.root.after(self.frame_interval_ms, self.update_projection)
            return
        self.rendered_seq1, self.rendered_seq2 = seq1, seq2

        canvas_w = self.canvas.winfo_width()
        canvas_h = self.canvas.winfo_height()
        if canvas_w < 100: canvas_w = self.target_monitor['width']
        if canvas_h < 100: canvas_h = self.target_monitor['height']

        # Single Window Mode
        if img2 is None:
            self._hide(self.item_id2, 2)
            self._set_pip_visible(False)
            if img1:
                self._blit(self.item_id1, 1, img1, canvas_w, canvas_h,
                           canvas_w // 2, canvas_h // 2)

        # Dual Window Mode
        else:
            layout_idx = self.layout_combo.current()

            # Mode 0: Side-by-Side (Left / Right Split)
            if layout_idx == 0:
                self._set_pip_visible(False)
                half_w = canvas_w // 2
                if img1:
                    self._blit(self.item_id1, 1, img1, half_w, canvas_h,
                               half_w // 2, canvas_h // 2)
                if img2:
                    self._blit(self.item_id2, 2, img2, half_w, canvas_h,
                               half_w + (half_w // 2), canvas_h // 2)

            # Mode 1: Top / Bottom Split
            elif layout_idx == 1:
                self._set_pip_visible(False)
                half_h = canvas_h // 2
                if img1:
                    self._blit(self.item_id1, 1, img1, canvas_w, half_h,
                               canvas_w // 2, half_h // 2)
                if img2:
                    self._blit(self.item_id2, 2, img2, canvas_w, half_h,
                               canvas_w // 2, half_h + (half_h // 2))

            # Mode 2: Picture-in-Picture (Game Inset Overlay)
            else:
                if img1:
                    self._blit(self.item_id1, 1, img1, canvas_w, canvas_h,
                               canvas_w // 2, canvas_h // 2)
                if img2:
                    frac = self._pip_fraction()
                    horiz, vert = self._pip_corner()
                    max_pip_w = int(canvas_w * frac)
                    max_pip_h = int(canvas_h * frac)
                    padding = 20

                    w2, h2 = img2.size
                    ratio2 = min(max_pip_w / w2, max_pip_h / h2)
                    nw2, nh2 = max(1, int(w2 * ratio2)), max(1, int(h2 * ratio2))

                    if horiz == "r":
                        x1 = canvas_w - nw2 - padding
                    else:
                        x1 = padding
                    if vert == "b":
                        y1 = canvas_h - nh2 - padding
                    else:
                        y1 = padding

                    self.canvas.coords(self.rect_pip_id,
                                       x1 - 4, y1 - 4, x1 + nw2 + 4, y1 + nh2 + 4)
                    self._set_pip_visible(True)

                    self._blit(self.item_id2, 2, img2, max_pip_w, max_pip_h,
                               x1 + nw2 // 2, y1 + nh2 // 2)

        self.root.after(self.frame_interval_ms, self.update_projection)

    def stop_projection(self):
        self.is_projecting = False
        
        if hasattr(self, 'capture_control1') and self.capture_control1:
            try: self.capture_control1.stop()
            except Exception: pass
            self.capture_control1 = None

        if hasattr(self, 'capture_control2') and self.capture_control2:
            try: self.capture_control2.stop()
            except Exception: pass
            self.capture_control2 = None

        if self.projector_window:
            try:
                self.projector_window.destroy()
            except Exception:
                pass
            self.projector_window = None

        # Release the cached Tk images; they can be several MB each at 4K.
        self.photos.clear()
        self.latest_pil_img1 = None
        self.latest_pil_img2 = None

        self.btn_start.config(state=tk.NORMAL, bg="#2563eb")
        self.btn_stop.config(state=tk.DISABLED, bg="#ef4444")

    def on_close(self):
        """Tear down capture threads before the UI goes away."""
        self._save_settings()
        self.stop_projection()
        try:
            self.backend.shutdown()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass


if __name__ == "__main__":
    root = tk.Tk()
    app = PaneCastControl(root)
    # Without this, closing the control window while projecting leaves the
    # capture threads running.
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
