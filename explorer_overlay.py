"""
explorer_overlay.py - In-Place Windows File Explorer Folder Size Overlay.
Renders folder sizes directly inside the "Size" column of Windows File Explorer's Details view
using a click-through transparent layered overlay.
"""

import os
import sys
import time
import queue
import winreg
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple, List, Any
import tkinter as tk
import win32gui
import win32con
import uiautomation as auto

from scanner import FolderScanner, format_size
import explorer_tracker
import column_style
import settings
from global_hotkey import GlobalHotkey

TRANS_COLOR = "#000001"

# How many folders' results to keep for instant redisplay on revisit
MAX_CACHED_FOLDERS = 128

# Copying Explorer's style means a PrintWindow capture, which costs ~28ms. A folder
# holding nothing but subfolders has no drawn sizes to measure, so the attempt fails
# and would otherwise be repeated on every single redraw. Give up after this many
# tries and wait this long before looking again.
MAX_STYLE_ATTEMPTS = 6
STYLE_RETRY_SECONDS = 5.0

# compute_layout returns this when it could not take a consistent reading but the
# overlay should stay exactly where it is -- distinct from None, which means hide.
SKIP_UPDATE = object()

# uiautomation's default is 10s; a missing control would stall the tracker for that
# long instead of simply hiding the overlay.
auto.SetGlobalSearchTimeout(0.5)

_theme_cache: Tuple[float, bool] = (0.0, False)

def is_dark_theme() -> bool:
    """Detects whether Windows Apps are currently using Dark Theme (cached ~2s,
    since this is a registry hit on every redraw)."""
    global _theme_cache
    now = time.monotonic()
    if now - _theme_cache[0] < 2.0:
        return _theme_cache[1]
    result = _read_dark_theme()
    _theme_cache = (now, result)
    return result

def _read_dark_theme() -> bool:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        winreg.CloseKey(key)
        return val == 0
    except Exception:
        # Windows' own default is light, so that is the safer guess when the value
        # is missing or unreadable.
        return False

@dataclass
class OverlayLayout:
    """
    A plain-data snapshot of where the overlay must sit and what rows are visible.

    Produced by compute_layout() on the UIAutomation thread and consumed by
    apply_layout() on the Tk main thread, so that no UIAutomation/COM object ever
    crosses threads (doing so raises "CoInitialize has not been called").
    """
    x: int
    y: int
    width: int
    height: int
    # (item name, row center y, relative to the overlay's top edge)
    rows: List[Tuple[str, int]] = field(default_factory=list)
    # Explorer's window rect when these coordinates were captured. Everything above
    # is in screen coordinates, so a window move invalidates them; keeping the
    # baseline lets the watchdog re-anchor the overlay without a fresh UIA pass.
    window_rect: Tuple[int, int, int, int] = (0, 0, 0, 0)
    # Screen rects of Size cells belonging to file rows (which Explorer has already
    # drawn a size into), used once to measure its padding, colour and font.
    sample_cells: List[Tuple[int, int, int, int]] = field(default_factory=list)


class ExplorerSizeOverlay:
    """
    Manages the click-through transparent overlay over an Explorer window's Size column.

    Threading: every method that touches `cab_ctrl` / uiautomation must run on the
    tracker thread (inside UIAutomationInitializerInThread); every method that touches
    Tk must run on the main thread. compute_layout() is the only bridge.
    """
    def __init__(self, scanner: FolderScanner, config: Optional[Dict[str, Any]] = None):
        self.scanner = scanner
        self.config: Dict[str, Any] = config if config is not None else settings.load()
        self.style = column_style.ColumnStyle()
        # Measured text colours, kept per theme so switching themes re-uses them
        self._measured_dark: Optional[str] = None
        self._measured_light: Optional[str] = None
        # Bounded retries for the style measurement (see MAX_STYLE_ATTEMPTS)
        self._style_attempts: int = 0
        self._style_next_try: float = 0.0
        self.root: Optional[tk.Tk] = None
        self.canvas: Optional[tk.Canvas] = None
        self.overlay_hwnd: Optional[int] = None
        self.current_hwnd: Optional[int] = None
        self.current_path: str = ""
        self.folder_sizes: Dict[str, str] = {}
        self.last_items_state: str = ""
        self.is_visible: bool = False
        self.scan_lock = threading.Lock()
        self.is_scanning = False
        self.tracked_hwnd: int = 0
        self.last_layout: Optional[OverlayLayout] = None
        # Cancels the scan for the folder we just navigated away from. Without it,
        # clicking quickly through folders left one 8-worker pool per folder running.
        self._scan_token = None
        # Tk is not thread-safe and `after()` from a foreign thread is undefined
        # behaviour, so the tracker, scan and hotkey threads post work here instead
        # and the watchdog (already on the Tk thread) runs it. Repaints are a flag
        # rather than a queued call, which collapses a burst of per-subfolder
        # updates into a single redraw.
        self.ui_queue: "queue.Queue" = queue.Queue()
        self._pending_layout: Optional[OverlayLayout] = None
        self._repaint_pending: bool = False
        self._hide_pending: bool = False
        self._post_lock = threading.Lock()
        # Folder path -> {subfolder name: formatted size}, LRU. Lets a revisited
        # folder paint instantly instead of waiting on a rescan.
        self.size_cache: "OrderedDict[str, Dict[str, str]]" = OrderedDict()
        # Bumped on every change to folder_sizes so the redraw gate notices updates
        # even when the number of entries is unchanged.
        self.sizes_version: int = 0
        # hwnd -> (list view control, header control). The depth-15 searches that
        # find these cost ~170ms each, far too much to repeat every poll.
        self._ctrl_cache: Dict[int, Tuple[Any, Any]] = {}

        self._init_window()

    def _init_window(self):
        self.root = tk.Tk()
        self.root.title("SizeScanner_InPlace_Overlay")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=TRANS_COLOR)
        self.root.wm_attributes("-transparentcolor", TRANS_COLOR)

        self.canvas = tk.Canvas(self.root, bg=TRANS_COLOR, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self.root.withdraw()
        self.root.update()

        # Configure WS_EX_TRANSPARENT so mouse clicks pass straight through
        self.overlay_hwnd = self._resolve_toplevel_hwnd()
        ex_style = win32gui.GetWindowLong(self.overlay_hwnd, win32con.GWL_EXSTYLE)
        win32gui.SetWindowLong(
            self.overlay_hwnd,
            win32con.GWL_EXSTYLE,
            ex_style | win32con.WS_EX_LAYERED | win32con.WS_EX_TRANSPARENT | win32con.WS_EX_TOOLWINDOW
        )

    def _resolve_toplevel_hwnd(self) -> int:
        """
        The real toplevel HWND of the Tk window.

        Tk's winfo_id() can return an inner frame whose parent is the actual toplevel;
        the ex-styles and SetWindowPos must both target the toplevel, or the overlay
        stops being click-through or gets moved by the wrong offset.
        """
        hwnd = win32gui.FindWindow(None, "SizeScanner_InPlace_Overlay")
        if hwnd:
            return hwnd
        hwnd = self.root.winfo_id()
        try:
            parent = win32gui.GetParent(hwnd)
            if parent:
                return parent
        except Exception:
            pass
        return hwnd

    def hide(self):
        if self.is_visible and self.root:
            self.root.withdraw()
            self.is_visible = False

    # --- Cross-thread marshalling ----------------------------------------

    def post_layout(self, layout: OverlayLayout):
        """Queues a layout for the Tk thread. Safe from the tracker thread."""
        with self._post_lock:
            self._pending_layout = layout
            self._repaint_pending = True
            self._hide_pending = False

    def post_hide(self):
        """Queues a hide for the Tk thread. Safe from any thread."""
        with self._post_lock:
            self._hide_pending = True
            self._repaint_pending = False

    def post_call(self, fn, *args):
        """Queues an arbitrary callable onto the Tk thread."""
        self.ui_queue.put((fn, args))

    def drain_ui(self):
        """Runs queued work on the Tk thread. Called by the watchdog, so a burst of
        size updates costs one redraw instead of one per subfolder."""
        while True:
            try:
                fn, args = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*args)
            except Exception:
                pass

        with self._post_lock:
            hide_it = self._hide_pending
            repaint = self._repaint_pending
            layout = self._pending_layout
            self._hide_pending = False
            self._repaint_pending = False

        if hide_it:
            self.hide()
        elif repaint and layout is not None:
            self.apply_layout(layout)

    # --- Scanning ---------------------------------------------------------

    def update_sizes_for_path(self, folder_path: str):
        """
        Publishes the sizes for `folder_path`, instantly if they are already known.

        Revisiting a folder paints straight from `size_cache` with no wait, then
        revalidates in the background. A first visit paints each subfolder as it
        finishes rather than waiting for the whole scan.
        """
        if not folder_path or not os.path.exists(folder_path):
            return

        with self.scan_lock:
            if self.is_scanning and self.current_path == folder_path:
                return
            # Stop the folder we just left: its worker pool would otherwise keep
            # competing for the disk with the folder the user is actually looking at.
            if self._scan_token is not None:
                self._scan_token.cancel()
            token = self.scanner.begin_operation()
            self._scan_token = token
            self.is_scanning = True
            self.current_path = folder_path

            cached = self.size_cache.get(folder_path)
            if cached is not None:
                # Known folder: show it immediately, then confirm in the background.
                self.size_cache.move_to_end(folder_path)
                self.folder_sizes = dict(cached)
            else:
                # Unknown folder: drop the previous folder's numbers so stale sizes
                # are never drawn against the new folder's rows.
                self.folder_sizes = {}
            self.sizes_version += 1

        self._request_repaint()
        threading.Thread(target=self._scan_worker, args=(folder_path, token), daemon=True).start()

    def _scan_worker(self, folder_path: str, token):
        def _on_folder_scanned(item, done_count, total_count):
            # Publish each subfolder the moment its size is known
            with self.scan_lock:
                if self.current_path != folder_path:
                    return
                self.folder_sizes[item.name] = format_size(item.size)
                self.sizes_version += 1
            self._request_repaint()

        try:
            res = self.scanner.scan_directory(
                folder_path, on_folder_scanned=_on_folder_scanned, token=token
            )
            sizes = {it.name: format_size(it.size) for it in res.items if it.is_dir}
            with self.scan_lock:
                if res.is_complete:
                    self.size_cache[folder_path] = sizes
                    self.size_cache.move_to_end(folder_path)
                    while len(self.size_cache) > MAX_CACHED_FOLDERS:
                        self.size_cache.popitem(last=False)
                # Only the scan for the folder still on screen may publish; a scan
                # that was superseded must not overwrite the new folder's sizes.
                if self.current_path == folder_path:
                    self.folder_sizes = dict(sizes)
                    self.sizes_version += 1
                    self.is_scanning = False
            self._request_repaint()
        except Exception:
            with self.scan_lock:
                if self.current_path == folder_path:
                    self.is_scanning = False
        finally:
            self.scanner.end_operation(token)

    def _want_style_measurement(self) -> bool:
        """Whether it is worth paying for another PrintWindow capture."""
        if self.style.measured:
            return False
        if self._style_attempts >= MAX_STYLE_ATTEMPTS:
            return False
        return time.monotonic() >= self._style_next_try

    def _measure_style(self, hwnd: int, sample_cells: List[Tuple[int, int, int, int]]):
        """
        Samples Explorer's Size column once and adopts what it finds. Any value
        pinned in settings.json wins over the measurement.

        Runs on the tracker thread: the capture costs ~28ms, which is far too much
        to spend on the Tk thread in the middle of a redraw. Attempts are bounded --
        a folder with no file rows has no drawn size to copy, and retrying that on
        every redraw was a permanent 28ms-per-frame tax.
        """
        self._style_attempts += 1
        self._style_next_try = time.monotonic() + STYLE_RETRY_SECONDS

        measured = column_style.measure_from_rows(hwnd, sample_cells)
        if measured is None:
            return

        override = self.config.get("right_padding")
        if isinstance(override, int) and not isinstance(override, bool):
            measured.right_padding = override

        if is_dark_theme():
            self._measured_dark = measured.text_color
        else:
            self._measured_light = measured.text_color
        # Publish last, as one atomic rebind: the Tk thread reads this without a lock
        # and must never see a half-updated style.
        self.style = measured

    def _text_color(self) -> str:
        """The colour to draw in: an explicit setting, else what we measured from
        Explorer, else a sensible default for the current theme."""
        dark = is_dark_theme()
        key = "text_color_dark" if dark else "text_color_light"
        override = self.config.get(key)
        if isinstance(override, str) and override.startswith("#"):
            return override
        sampled = self._measured_dark if dark else self._measured_light
        if sampled:
            return sampled
        return column_style.FALLBACK_TEXT_DARK if dark else column_style.FALLBACK_TEXT_LIGHT

    def _request_repaint(self):
        """
        Repaints with the most recent layout, so new sizes appear without waiting for
        the tracker's next pass. Safe to call from any thread.

        This only raises a flag; the watchdog performs the redraw on the Tk thread.
        A scan of a folder with 500 subfolders calls this 500 times, which used to
        mean 500 full canvas rebuilds -- now they collapse into one per watchdog tick.
        """
        layout = self.last_layout
        if not layout or not self.root or not self.is_visible:
            return
        with self._post_lock:
            if self._pending_layout is None:
                self._pending_layout = layout
            self._repaint_pending = True

    def _get_view_controls(self, cab_ctrl, hwnd: int) -> Optional[Tuple[Any, Any]]:
        """
        Returns (list view, header) for an Explorer window, reusing the cached controls.

        Locating these is two depth-15 tree searches costing ~170ms each, so they are
        cached per window and only re-searched when the cached ones go stale (the view
        was rebuilt by a navigation or a view-mode switch).
        """
        cached = self._ctrl_cache.get(hwnd)
        if cached:
            try:
                # Cheap liveness probe: a dead control raises or reports an empty rect.
                if cached[0].BoundingRectangle.height() > 0 and cached[1].BoundingRectangle.height() > 0:
                    return cached
            except Exception:
                pass
            self._ctrl_cache.pop(hwnd, None)

        items_view = cab_ctrl.ListControl(searchDepth=15)
        if not items_view.Exists(0.2):
            return None
        hdr = cab_ctrl.HeaderControl(searchDepth=15)
        if not hdr.Exists(0.2):
            return None

        self._ctrl_cache[hwnd] = (items_view, hdr)
        for dead in [h for h in self._ctrl_cache if not win32gui.IsWindow(h)]:
            self._ctrl_cache.pop(dead, None)
        return items_view, hdr

    def focus_watchdog(self) -> bool:
        """
        Cheap (<0.1ms) check that the tracked Explorer window is still in front, and
        that the overlay is still anchored to it.

        Runs on the Tk main thread far more often than the tracker polls, so the
        overlay disappears the instant you alt-tab away instead of waiting out a
        full UIAutomation pass.
        """
        if not self.is_visible:
            return True
        if not explorer_tracker.is_explorer_focused(self.tracked_hwnd):
            self.hide()
            return False
        self._follow_window()
        return True

    def _follow_window(self):
        """
        Keeps the overlay glued to the window while it is dragged.

        A move shifts the list, the header and the columns rigidly, so the overlay can
        be re-anchored by the same delta using only Win32 calls -- no UIAutomation, so
        it keeps up with a drag instead of trailing a tracker pass behind. A *resize*
        reflows the columns, so the overlay hides until a fresh layout arrives.
        """
        layout = self.last_layout
        if not layout or not self.tracked_hwnd:
            return
        try:
            cur = win32gui.GetWindowRect(self.tracked_hwnd)
        except Exception:
            return

        base = layout.window_rect
        if cur == base:
            return

        same_size = (cur[2] - cur[0]) == (base[2] - base[0]) and (cur[3] - cur[1]) == (base[3] - base[1])
        if not same_size:
            self.hide()
            return

        dx, dy = cur[0] - base[0], cur[1] - base[1]
        try:
            win32gui.SetWindowPos(
                self.overlay_hwnd, 0, layout.x + dx, layout.y + dy, 0, 0,
                win32con.SWP_NOSIZE | win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE
            )
        except Exception:
            pass

    def compute_layout(self, cab_ctrl, exp_info: explorer_tracker.ExplorerInfo) -> Any:
        """
        Inspects the Explorer window via UIAutomation and returns the overlay geometry
        plus the visible rows. MUST run on the tracker thread.

        Returns an OverlayLayout, None when the overlay should be hidden (no Details
        view, no Size column, not focused), or SKIP_UPDATE when the reading was
        inconsistent and the overlay should simply stay where it is.
        """
        # 1. Verify window state. The overlay is topmost, so it must only paint while
        # Explorer is genuinely in front -- otherwise the sizes land on top of
        # whatever window is covering it.
        if exp_info.is_minimized or not exp_info.is_visible:
            return None

        if not explorer_tracker.is_explorer_focused(exp_info.hwnd):
            return None

        self.tracked_hwnd = exp_info.hwnd

        # Every BoundingRectangle below is a separate cross-process call returning
        # screen coordinates. If the window moves partway through, the header and the
        # rows come from different window positions and the text lands between rows,
        # so the whole snapshot is taken between two rect reads and discarded if the
        # window shifted underneath it.
        try:
            rect_before = win32gui.GetWindowRect(exp_info.hwnd)
        except Exception:
            return None

        # 2. Find UIItemsView & HeaderControl (cached per window)
        found = self._get_view_controls(cab_ctrl, exp_info.hwnd)
        if not found:
            return None
        items_view, hdr = found

        # Find Size column
        size_col = None
        for col in hdr.GetChildren():
            if "Size" in col.Name:
                size_col = col
                break

        if not size_col:
            # Details view with Size column not active
            return None

        # 3. Geometry

        size_rect = size_col.BoundingRectangle
        view_rect = items_view.BoundingRectangle
        # One read, reused: each BoundingRectangle is a separate cross-process call
        hdr_rect = hdr.BoundingRectangle

        if size_rect.width() <= 10 or view_rect.height() <= 10:
            return None

        overlay_x = size_rect.left
        overlay_y = hdr_rect.bottom
        overlay_w = size_rect.width()
        overlay_h = max(20, view_rect.bottom - overlay_y)

        # 4. Extract visible list items as plain (name, y) pairs
        with self.scan_lock:
            known_folders = set(self.folder_sizes)

        rows: List[Tuple[str, int]] = []
        sample_cells: List[Tuple[int, int, int, int]] = []
        need_samples = self._want_style_measurement()
        for it in items_view.GetChildren():
            if it.ControlTypeName != "ListItemControl" and it.ClassName != "UIItem":
                continue
            rect = it.BoundingRectangle
            if rect.bottom <= overlay_y or rect.top >= view_rect.bottom:
                continue
            rows.append((it.Name, rect.top - overlay_y + (rect.height() // 2)))

            # Collect a few file rows to measure Explorer's own text against. Folder
            # rows are blank, so they would teach us nothing.
            if need_samples and len(sample_cells) < 4 and it.Name not in known_folders:
                cell = next((s for s in it.GetChildren() if s.Name == "Size"), None)
                if cell is not None:
                    cr = cell.BoundingRectangle
                    sample_cells.append((cr.left, cr.top, cr.right, cr.bottom))

        try:
            rect_after = win32gui.GetWindowRect(exp_info.hwnd)
        except Exception:
            return None
        if rect_after != rect_before:
            # Window moved or resized mid-capture, so these coordinates are
            # inconsistent. Keep the overlay where it is -- _follow_window() is
            # re-anchoring it on every watchdog tick -- and retake the reading next
            # pass, rather than flickering the overlay off mid-drag.
            return SKIP_UPDATE

        # Copy Explorer's padding, colour and glyph size while still on this thread;
        # the capture is far too slow to run during a redraw.
        if sample_cells and self._want_style_measurement():
            try:
                self._measure_style(exp_info.hwnd, sample_cells)
            except Exception:
                pass

        return OverlayLayout(
            x=overlay_x, y=overlay_y, width=overlay_w, height=overlay_h,
            rows=rows, window_rect=rect_after, sample_cells=sample_cells
        )

    def apply_layout(self, layout: OverlayLayout):
        """Positions the overlay and redraws the sizes. MUST run on the Tk main thread."""
        if not self.root or not self.canvas:
            return

        self.last_layout = layout

        with self.scan_lock:
            sizes_dict = dict(self.folder_sizes)
            version = self.sizes_version

        # Skip the redraw when nothing that affects the picture has changed. The
        # version counter catches size values changing without the count changing.
        # Hashing every row rather than the first ten: a scroll that leaves the top
        # rows unchanged still moves the ones below, and that has to force a redraw.
        state_sig = (
            f"{layout.x}_{layout.y}_{layout.width}_{layout.height}"
            f"_{hash(tuple(layout.rows))}_v{version}"
        )

        if state_sig == self.last_items_state and self.is_visible:
            return

        self.last_items_state = state_sig

        try:
            self.root.geometry(f"{layout.width}x{layout.height}+{layout.x}+{layout.y}")
        except Exception:
            pass

        self.canvas.delete("all")
        text_color = self._text_color()
        # Read the style once: the tracker thread may rebind it between rows
        style = self.style
        draw_x = layout.width - style.right_padding
        font = (style.font_family, -style.font_px)

        for name, row_rel_y in layout.rows:
            size_str = sizes_dict.get(name)
            if not size_str:
                continue
            self.canvas.create_text(
                draw_x,
                row_rel_y,
                text=size_str,
                fill=text_color,
                # Negative size = pixels, so it matches Explorer's glyphs at any DPI
                font=font,
                anchor="e"
            )

        if not self.is_visible:
            self.root.deiconify()
            self.is_visible = True

        self.root.update_idletasks()

class InPlaceSizeService:
    """
    Background service that tracks Explorer windows and maintains in-place overlays.
    """
    def __init__(self):
        self.scanner = FolderScanner(max_workers=8)
        self.overlay: Optional[ExplorerSizeOverlay] = None
        self.stop_event = threading.Event()
        self.tray_icon = None
        self.config = settings.load()
        self.enabled = bool(self.config.get("start_enabled", True))
        self.hotkey: Optional[GlobalHotkey] = None

    def start(self):
        # Initialize overlay in main thread (Tkinter requirement)
        self.overlay = ExplorerSizeOverlay(self.scanner, self.config)
        settings.ensure_file()
        self._setup_hotkey()
        self._setup_tray()

        # Start tracking thread
        tracker_thread = threading.Thread(target=self._tracker_loop, daemon=True)
        tracker_thread.start()

        # Fast focus watchdog on the Tk thread, independent of the tracker's pace
        self._schedule_watchdog()

        # Run Tkinter mainloop
        try:
            self.overlay.root.mainloop()
        except KeyboardInterrupt:
            self.stop()

    def _setup_hotkey(self):
        spec = self.config.get("hotkey") or ""
        if not spec:
            return
        # Fires on the hotkey thread; bounce to the Tk thread before touching the UI
        self.hotkey = GlobalHotkey(spec, lambda: self.overlay.post_call(self.toggle))
        if not self.hotkey.start():
            print(f"Shortcut not available: {self.hotkey.error}. "
                  f"Edit {settings.settings_path()} to choose another.")

    def toggle(self, *args):
        """Turns the in-Explorer sizes on or off."""
        self.enabled = not self.enabled
        if not self.enabled:
            self.overlay.hide()
        else:
            # Repaint at the next tracker pass with fresh geometry
            self.overlay.last_items_state = ""
        self.config["start_enabled"] = self.enabled
        settings.save(self.config)
        if self.tray_icon:
            try:
                self.tray_icon.update_menu()
            except Exception:
                pass

    def _open_settings(self, *args):
        try:
            os.startfile(settings.ensure_file())
        except Exception as exc:
            print(f"Could not open settings: {exc}")

    WATCHDOG_INTERVAL_MS = 40

    def _schedule_watchdog(self):
        if not self.overlay or not self.overlay.root:
            return
        # Drain first, and drain even while stopping: this is the only place work from
        # the tracker, scan, hotkey and tray threads reaches Tk, so the shutdown's
        # queued destroy() arrives here too. A queued hide must also be honoured
        # before the focus check looks at is_visible.
        try:
            self.overlay.drain_ui()
        except Exception:
            pass
        if self.stop_event.is_set():
            return
        try:
            self.overlay.focus_watchdog()
            self.overlay.root.after(self.WATCHDOG_INTERVAL_MS, self._schedule_watchdog)
        except Exception:
            pass

    def _setup_tray(self):
        try:
            import pystray
            import PIL.Image

            icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
            img = PIL.Image.open(icon_path) if os.path.exists(icon_path) else PIL.Image.new("RGBA", (32, 32), "#3a86ff")

            hotkey_label = settings.describe_hotkey(self.config.get("hotkey") or "")

            menu = pystray.Menu(
                pystray.MenuItem(
                    lambda item: f"Show sizes in Explorer  ({hotkey_label})",
                    self.toggle,
                    checked=lambda item: self.enabled,
                    default=True
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Open Full Analyzer App", self._launch_full_app),
                pystray.MenuItem("Open Companion Sidebar", self._launch_companion_sidebar),
                pystray.MenuItem("Edit Settings…", self._open_settings),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Exit", self.stop)
            )

            self.tray_icon = pystray.Icon("SizeScannerInPlace", img, "SizeScanner - In-Explorer Sizes", menu)
            threading.Thread(target=self.tray_icon.run, daemon=True).start()
        except Exception as e:
            print(f"Tray error: {e}")

    def _relaunch(self, mode_flag: str):
        """Starts another SizeScanner instance in the given mode."""
        import shell_integration
        shell_integration.launch_mode(mode_flag)

    def _launch_full_app(self, *args):
        self._relaunch("--full")

    def _launch_companion_sidebar(self, *args):
        self._relaunch("--companion")

    def _tracker_loop(self):
        with auto.UIAutomationInitializerInThread(debug=False):
            while not self.stop_event.is_set():
                if not self.enabled:
                    # Toggled off: do no UIA work at all, just idle
                    time.sleep(0.2)
                    continue

                try:
                    layout = None
                    exp_info = explorer_tracker.get_active_explorer(preferred_hwnd=self.overlay.tracked_hwnd)
                    if exp_info and exp_info.is_visible and not exp_info.is_minimized:
                        # Scan regardless of focus, so the sizes are already known the
                        # moment Explorer comes forward. current_path is updated
                        # synchronously, so this fires once per navigation, not per tick.
                        if exp_info.path and exp_info.path != self.overlay.current_path:
                            # Navigating rebuilds the items view, so drop the cached
                            # controls rather than trusting their stale geometry.
                            self.overlay._ctrl_cache.pop(exp_info.hwnd, None)
                            self.overlay.update_sizes_for_path(exp_info.path)

                        cab = exp_info.cab_ctrl
                        if cab and cab.Exists(0.1):
                            # All UIAutomation access stays on this thread; only the
                            # plain-data layout crosses over to the Tk main thread.
                            layout = self.overlay.compute_layout(cab, exp_info)

                    if layout is SKIP_UPDATE:
                        pass  # transient; the watchdog is keeping the overlay anchored
                    elif layout is not None:
                        self.overlay.post_layout(layout)
                    elif self.overlay.is_visible:
                        self.overlay.post_hide()
                except Exception:
                    pass
                time.sleep(0.08)

    def stop(self, *args):
        self.stop_event.set()
        # Stop any scan still walking the disk before tearing the UI down
        self.scanner.request_cancel()
        if self.hotkey:
            self.hotkey.stop()
        if self.tray_icon:
            self.tray_icon.stop()
        if self.overlay and self.overlay.root:
            # Called from the tray thread, so it cannot touch Tk directly
            self.overlay.post_call(self.overlay.root.destroy)

if __name__ == "__main__":
    service = InPlaceSizeService()
    service.start()
