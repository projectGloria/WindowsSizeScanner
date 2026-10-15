"""
companion_window.py - Sleek Companion Panel that docks alongside Windows File Explorer.
Automatically detects active File Explorer windows, navigates with Explorer in real-time,
and displays recursive folder and file sizes right next to Explorer's view.
"""

import os
import time
import threading
import queue
from typing import List, Optional, Tuple, Dict, Any
import tkinter as tk
from tkinter import ttk, messagebox
import customtkinter as ctk
import win32api
import win32con

from scanner import FolderScanner, ItemInfo, ScanResult, format_size
import explorer_tracker
import shell_integration

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

def make_mini_bar(percent: float, length: int = 8) -> str:
    percent = max(0.0, min(100.0, percent))
    filled = int(round(percent / 100.0 * length))
    filled = max(0, min(length, filled))
    blocks = "■" * filled + "□" * (length - filled)
    return f"[{blocks}] {percent:4.1f}%"

class ExplorerCompanionWindow(ctk.CTk):
    def __init__(self, start_minimized_to_tray: bool = False):
        super().__init__()

        self.title("SizeScanner - Companion")
        self.geometry("400x650+100+100")
        self.minsize(320, 400)

        # State
        self.scanner = FolderScanner(max_workers=8)
        self.current_path: str = ""
        self.current_items: List[ItemInfo] = []
        self.filtered_items: List[ItemInfo] = []
        self.tracked_hwnd: Optional[int] = None
        self.last_rect: Optional[Tuple[int, int, int, int]] = None
        self.auto_dock: bool = True
        self.dock_side: str = "auto"  # 'auto', 'right', 'left'
        self.is_scanning: bool = False
        self.is_minimized_by_tracker: bool = False
        self.always_on_top: bool = False
        self.sort_col: str = "size"
        self.sort_desc: bool = True
        self.scan_token = None
        # item path -> tree row id, so a completed folder is found in O(1)
        self.row_by_path: Dict[str, str] = {}
        self.total_size_so_far: int = 0
        self.last_status_text: str = ""

        # Thread-safe queue
        self.ui_queue: queue.Queue = queue.Queue()
        self._poll_after_id: Optional[str] = None
        self._poll_ui_queue()

        # Set App Icon
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
        if os.path.exists(icon_path):
            try:
                self.iconbitmap(icon_path)
            except Exception:
                pass

        # Build UI
        self._create_widgets()
        self._setup_style()

        # Setup System Tray
        self.tray_icon = None
        self._setup_tray()

        # Start tracking thread
        self.tracker_stop = threading.Event()
        self.tracker_thread = threading.Thread(target=self._tracker_loop, daemon=True)
        self.tracker_thread.start()

        # Handle window close: minimize to tray instead of killing app
        self.protocol("WM_DELETE_WINDOW", self.hide_to_tray)

    def dispatch_ui(self, fn, *args):
        self.ui_queue.put((fn, args))

    def _poll_ui_queue(self):
        # Every Tk call here can fail once the window is destroyed -- including
        # winfo_exists() itself, and the reschedule, which otherwise leaves a dangling
        # "after" script that Tk reports as an invalid command name during shutdown.
        try:
            if not self.winfo_exists():
                return
            try:
                while True:
                    fn, args = self.ui_queue.get_nowait()
                    try:
                        fn(*args)
                    except tk.TclError:
                        return   # window went away between queueing and running
                    except Exception as e:
                        print(f"UI queue callback error: {e}")
            except queue.Empty:
                pass
            self._poll_after_id = self.after(25, self._poll_ui_queue)
        except tk.TclError:
            return

    def destroy(self):
        # Cancel the pending poll before tearing the interpreter down. Tcl reports an
        # already-scheduled "after" script whose command destroy() deleted, and it
        # does so before Python gets a chance to guard against it.
        after_id = getattr(self, "_poll_after_id", None)
        if after_id:
            try:
                self.after_cancel(after_id)
            except Exception:
                pass
            self._poll_after_id = None
        super().destroy()

    def _create_widgets(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)  # Table expands

        # 1. Header Toolbar
        self.hdr_frame = ctk.CTkFrame(self, corner_radius=0, height=38, fg_color=("gray85", "gray17"))
        self.hdr_frame.grid(row=0, column=0, sticky="ew", padx=0, pady=0)
        self._build_header()

        # 2. Tracked Path & Summary Card
        self.card_frame = ctk.CTkFrame(self, corner_radius=6, fg_color=("gray90", "gray14"))
        self.card_frame.grid(row=1, column=0, sticky="ew", padx=8, pady=(6, 4))
        self._build_path_card()

        # 3. Filter Bar
        self.filter_frame = ctk.CTkFrame(self, corner_radius=0, height=34, fg_color="transparent")
        self.filter_frame.grid(row=2, column=0, sticky="ew", padx=8, pady=(2, 4))
        self._build_filter_bar()

        # 4. Table Frame (Treeview)
        self.table_frame = ctk.CTkFrame(self, corner_radius=6, fg_color=("gray95", "gray20"))
        self.table_frame.grid(row=3, column=0, sticky="nsew", padx=8, pady=(0, 4))
        self._build_table()

        # 5. Bottom Status Bar
        self.status_frame = ctk.CTkFrame(self, corner_radius=0, height=26, fg_color=("gray85", "gray17"))
        self.status_frame.grid(row=4, column=0, sticky="ew", padx=0, pady=0)
        self._build_status_bar()

    def _build_header(self):
        # Brand
        lbl_title = ctk.CTkLabel(
            self.hdr_frame,
            text="⚡ Folder Sizes",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color=("#1f538d", "#3a86ff")
        )
        lbl_title.pack(side="left", padx=(10, 6), pady=4)

        # Auto-Dock button
        self.btn_dock = ctk.CTkButton(
            self.hdr_frame,
            text="🔗 Docked",
            width=70,
            height=26,
            font=ctk.CTkFont(size=11),
            fg_color=("#1f538d", "#2b5c8f"),
            command=self.toggle_dock
        )
        self.btn_dock.pack(side="left", padx=3, pady=4)

        # Always on top
        self.btn_pin = ctk.CTkButton(
            self.hdr_frame,
            text="📌 Top",
            width=55,
            height=26,
            font=ctk.CTkFont(size=11),
            fg_color=("gray70", "gray30"),
            command=self.toggle_always_on_top
        )
        self.btn_pin.pack(side="left", padx=3, pady=4)

        # Refresh
        btn_ref = ctk.CTkButton(
            self.hdr_frame,
            text="🔄",
            width=32,
            height=26,
            command=self.refresh_current
        )
        btn_ref.pack(side="left", padx=3, pady=4)

        # Full app button
        btn_full = ctk.CTkButton(
            self.hdr_frame,
            text="↗ Full App",
            width=75,
            height=26,
            font=ctk.CTkFont(size=11),
            fg_color=("gray60", "gray35"),
            command=self.launch_full_app
        )
        btn_full.pack(side="right", padx=(4, 8), pady=4)

    def _build_path_card(self):
        self.lbl_tracked_path = ctk.CTkLabel(
            self.card_frame,
            text="Waiting for Windows File Explorer...",
            font=ctk.CTkFont(size=12, weight="bold"),
            anchor="w",
            wraplength=360
        )
        self.lbl_tracked_path.pack(fill="x", padx=8, pady=(6, 2))

        self.lbl_tracked_info = ctk.CTkLabel(
            self.card_frame,
            text="Open any folder in File Explorer to see sizes here.",
            font=ctk.CTkFont(size=11),
            text_color="gray",
            anchor="w"
        )
        self.lbl_tracked_info.pack(fill="x", padx=8, pady=(0, 6))

    def _build_filter_bar(self):
        self.search_entry = ctk.CTkEntry(
            self.filter_frame,
            height=28,
            font=ctk.CTkFont(size=11),
            placeholder_text="🔍 Filter items in current folder..."
        )
        self.search_entry.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.search_entry.bind("<KeyRelease>", lambda e: self.on_filter_changed())

        btn_clear = ctk.CTkButton(
            self.filter_frame,
            text="✕",
            width=26,
            height=28,
            command=self.clear_filter
        )
        btn_clear.pack(side="right")

    def _setup_style(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(
            "Companion.Treeview",
            background="#202020",
            foreground="#f0f0f0",
            fieldbackground="#202020",
            rowheight=24,
            font=("Segoe UI", 9),
            borderwidth=0
        )
        style.configure(
            "Companion.Treeview.Heading",
            background="#2b2b2b",
            foreground="#f0f0f0",
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            padding=(4, 3)
        )
        style.map(
            "Companion.Treeview",
            background=[("selected", "#1f538d")],
            foreground=[("selected", "white")]
        )

    def _build_table(self):
        self.table_frame.grid_columnconfigure(0, weight=1)
        self.table_frame.grid_rowconfigure(0, weight=1)

        cols = ("name", "size", "percent", "items")
        self.tree = ttk.Treeview(
            self.table_frame,
            columns=cols,
            show="headings",
            style="Companion.Treeview",
            selectmode="browse"
        )

        self.tree.heading("name", text="Name ▲▼", command=lambda: self.sort_by("name"))
        self.tree.heading("size", text="Size ▲▼", command=lambda: self.sort_by("size"))
        self.tree.heading("percent", text="% ▲▼", command=lambda: self.sort_by("percent"))
        self.tree.heading("items", text="Items", command=lambda: self.sort_by("items"))

        self.tree.column("name", width=170, minwidth=110, anchor="w")
        self.tree.column("size", width=85, minwidth=65, anchor="e")
        self.tree.column("percent", width=110, minwidth=90, anchor="center")
        self.tree.column("items", width=80, minwidth=60, anchor="e")

        vsb = ttk.Scrollbar(self.table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        self.tree.bind("<Double-1>", self.on_item_double_click)
        self.tree.bind("<Button-3>", self.show_context_menu)

        # Context Menu
        self.menu = tk.Menu(self, tearoff=0, bg="#2b2b2b", fg="white", activebackground="#1f538d")
        self.menu.add_command(label="📂 Open in File Explorer", command=self.open_selected)
        self.menu.add_command(label="📋 Copy Full Path", command=self.copy_path)
        self.menu.add_command(label="📋 Copy Size", command=self.copy_size)
        self.menu.add_separator()
        self.menu.add_command(label="🗑️ Move to Recycle Bin", command=self.delete_selected)

    def _build_status_bar(self):
        self.status_frame.grid_columnconfigure(0, weight=1)
        self.lbl_status = ctk.CTkLabel(
            self.status_frame,
            text="🟢 Tracking Active",
            font=ctk.CTkFont(size=10),
            anchor="w"
        )
        self.lbl_status.grid(row=0, column=0, padx=8, pady=2, sticky="w")

        btn_min = ctk.CTkButton(
            self.status_frame,
            text="Hide to Tray",
            width=75,
            height=20,
            font=ctk.CTkFont(size=10),
            fg_color="transparent",
            text_color="gray",
            hover_color=("gray75", "gray30"),
            command=self.hide_to_tray
        )
        btn_min.grid(row=0, column=1, padx=6, pady=2, sticky="e")

    # --- Tracking & Docking Logic ---

    def _tracker_loop(self):
        """Continuously monitors active File Explorer windows."""
        import uiautomation as auto
        with auto.UIAutomationInitializerInThread(debug=False):
            while not self.tracker_stop.is_set():
                try:
                    exp = explorer_tracker.get_active_explorer(preferred_hwnd=self.tracked_hwnd)
                    if exp and exp.is_visible and not exp.is_minimized:
                        # Explorer is active and visible
                        self.dispatch_ui(self._on_explorer_detected, exp)
                    elif exp and exp.is_minimized:
                        # Explorer is minimized
                        self.dispatch_ui(self._on_explorer_minimized)
                except Exception as e:
                    pass
                time.sleep(0.35)

    def _on_explorer_detected(self, exp: explorer_tracker.ExplorerInfo):
        self.tracked_hwnd = exp.hwnd
        # 1. Update status (only when it actually changed -- this runs ~3x a second)
        status = f"🟢 Tracking: {exp.title}"
        if status != self.last_status_text:
            self.last_status_text = status
            self.lbl_status.configure(text=status)

        # 2. Check path change
        if exp.path and exp.path != self.current_path and os.path.exists(exp.path):
            self.load_path(exp.path)

        # 3. Check docking geometry change
        if self.auto_dock and exp.rect != self.last_rect and exp.rect != (0, 0, 0, 0):
            self.last_rect = exp.rect
            self._dock_to_rect(exp.rect)

    def _on_explorer_minimized(self):
        if self.auto_dock and not self.is_minimized_by_tracker:
            if self.last_status_text != "🟡 File Explorer Minimized":
                self.last_status_text = "🟡 File Explorer Minimized"
                self.lbl_status.configure(text=self.last_status_text)

    def _work_area_for_rect(self, rect: Tuple[int, int, int, int]) -> Tuple[int, int, int, int]:
        """
        The usable area of the monitor Explorer is on.

        GetSystemMetrics reports the *primary* display, so with Explorer on a second
        monitor the side-flip and the vertical clamp were both computed against the
        wrong screen and the panel jumped back to monitor 1.
        """
        try:
            monitor = win32api.MonitorFromRect(rect, win32con.MONITOR_DEFAULTTONEAREST)
            return win32api.GetMonitorInfo(monitor)["Work"]
        except Exception:
            return (0, 0, win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1))

    def _dock_to_rect(self, rect: Tuple[int, int, int, int]):
        """Positions companion window alongside Explorer rect (left, top, right, bottom)."""
        exp_left, exp_top, exp_right, exp_bottom = rect
        work_left, work_top, work_right, work_bottom = self._work_area_for_rect(rect)

        exp_h = max(400, min(exp_bottom - exp_top, work_bottom - work_top))
        comp_w = max(340, self.winfo_width() or 380)

        # Decide side
        dock_x = exp_right
        if self.dock_side == "left" or (self.dock_side == "auto" and dock_x + comp_w > work_right):
            dock_x = max(work_left, exp_left - comp_w)

        dock_y = max(work_top, min(work_bottom - exp_h, exp_top))
        target_geo = f"{comp_w}x{exp_h}+{dock_x}+{dock_y}"
        try:
            self.geometry(target_geo)
        except Exception:
            pass

    def toggle_dock(self):
        self.auto_dock = not self.auto_dock
        if self.auto_dock:
            self.btn_dock.configure(text="🔗 Docked", fg_color=("#1f538d", "#2b5c8f"))
            self.last_rect = None  # Force re-dock on next tracker tick
        else:
            self.btn_dock.configure(text="🔓 Free", fg_color=("gray60", "gray35"))

    def toggle_always_on_top(self):
        self.always_on_top = not self.always_on_top
        self.attributes("-topmost", self.always_on_top)
        if self.always_on_top:
            self.btn_pin.configure(fg_color=("#38b000", "#2d8a00"))
        else:
            self.btn_pin.configure(fg_color=("gray70", "gray30"))

    # --- Folder Scanning & UI Updates ---

    def load_path(self, folder_path: str):
        self.current_path = folder_path
        self.lbl_tracked_path.configure(text=f"📁 {folder_path}")
        self.lbl_tracked_info.configure(text="Calculating folder sizes...")

        # Stop the scan for the folder we just left. Its token is cancelled directly,
        # so the new scan cannot un-cancel it -- the old code set a shared flag and
        # cleared it again 20ms later (sleeping on the UI thread to do so), which left
        # the previous scan running and its results overwriting this folder's.
        if self.scan_token is not None:
            self.scan_token.cancel()
        token = self.scanner.begin_operation()
        self.scan_token = token

        # Fast immediate load
        items = self.scanner.scan_directory_immediate(folder_path)
        self.current_items = items
        self.total_size_so_far = sum(it.size for it in items)
        self._apply_filter()

        def _worker():
            def _done(item_info, d_cnt, t_cnt):
                self.dispatch_ui(self._update_folder_row, item_info, folder_path)

            try:
                res = self.scanner.scan_directory(
                    folder_path, on_folder_scanned=_done, token=token, items=items
                )
                self.dispatch_ui(self._on_scan_done, res)
            finally:
                self.scanner.end_operation(token)

        threading.Thread(target=_worker, daemon=True).start()

    def _update_folder_row(self, item_info: ItemInfo, scan_path: str):
        if scan_path != self.current_path:
            return

        # item_info is the same object scan_directory sized in place, so the running
        # total just needs the new size adding; re-summing every item and then
        # scanning every tree row for this path was quadratic in the folder count.
        self.total_size_so_far += item_info.size
        tot = self.total_size_so_far or 1

        row_id = self.row_by_path.get(item_info.path)
        if not row_id:
            return
        pct = (item_info.size / tot) * 100.0
        try:
            self.tree.set(row_id, "size", format_size(item_info.size))
            self.tree.set(row_id, "percent", make_mini_bar(pct))
            self.tree.set(row_id, "items", f"{item_info.file_count + item_info.dir_count} items")
        except tk.TclError:
            self.row_by_path.pop(item_info.path, None)

    def _on_scan_done(self, res: ScanResult):
        # Late result for a folder Explorer has already left
        if res.current_path != self.current_path:
            return
        self.current_items = res.items
        self.total_size_so_far = res.total_size
        self._apply_filter()
        # Update summary card
        dir_cnt = sum(1 for it in res.items if it.is_dir)
        file_cnt = sum(1 for it in res.items if not it.is_dir)
        tot_str = format_size(res.total_size)
        self.lbl_tracked_info.configure(text=f"{len(res.items)} items ({dir_cnt} folders, {file_cnt} files) • {tot_str} total")

    def _apply_filter(self):
        q = self.search_entry.get().strip().lower()
        if q:
            self.filtered_items = [it for it in self.current_items if q in it.name.lower()]
        else:
            self.filtered_items = list(self.current_items)

        # Sort
        def _key(item: ItemInfo):
            if self.sort_col == "name":
                return (not item.is_dir, item.name.lower())
            elif self.sort_col in ("size", "percent"):
                return item.size
            elif self.sort_col == "items":
                return item.file_count + item.dir_count
            return item.size

        self.filtered_items.sort(key=_key, reverse=self.sort_desc)
        self._render()

    def _render(self):
        self.tree.delete(*self.tree.get_children())
        self.row_by_path.clear()

        tot = self.total_size_so_far or 1

        for it in self.filtered_items:
            prefix = "📁 " if it.is_dir else "📄 "
            name_disp = prefix + it.name
            if it.is_dir and not it.is_scanned:
                size_str = "..."
                bar_str = "[........] --.-%"
                items_str = "..."
            else:
                size_str = format_size(it.size)
                pct = (it.size / tot) * 100.0
                bar_str = make_mini_bar(pct)
                items_str = f"{it.file_count + it.dir_count} items" if it.is_dir else "1 file"

            row_id = self.tree.insert(
                "",
                "end",
                values=(name_disp, size_str, bar_str, items_str),
                tags=(it.path,)
            )
            self.row_by_path[it.path] = row_id

    def sort_by(self, col: str):
        if self.sort_col == col:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_col = col
            self.sort_desc = True
        self._apply_filter()

    def on_filter_changed(self):
        self._apply_filter()

    def clear_filter(self):
        self.search_entry.delete(0, "end")
        self._apply_filter()

    def refresh_current(self):
        if self.current_path:
            self.scanner.clear_cache(self.current_path)
            self.load_path(self.current_path)

    # --- Item Interaction ---

    def _get_selected_path(self) -> Optional[str]:
        sel = self.tree.selection()
        if not sel:
            return None
        tags = self.tree.item(sel[0], "tags")
        return tags[0] if tags else None

    def on_item_double_click(self, event=None):
        path = self._get_selected_path()
        if path:
            shell_integration.open_in_explorer(path)

    def open_selected(self):
        path = self._get_selected_path()
        if path:
            shell_integration.open_in_explorer(path)

    def copy_path(self):
        path = self._get_selected_path()
        if path:
            self.clipboard_clear()
            self.clipboard_append(path)

    def copy_size(self):
        path = self._get_selected_path()
        if path:
            for it in self.current_items:
                if it.path == path:
                    self.clipboard_clear()
                    self.clipboard_append(format_size(it.size))
                    break

    def delete_selected(self):
        path = self._get_selected_path()
        if not path or not os.path.exists(path):
            return
        name = os.path.basename(path)
        if messagebox.askyesno("Delete", f"Move to Recycle Bin?\n\n{name}", icon="warning"):
            if shell_integration.move_to_recycle_bin(path):
                self.refresh_current()

    def show_context_menu(self, event):
        row_id = self.tree.identify_row(event.y)
        if row_id:
            self.tree.selection_set(row_id)
            self.menu.tk_popup(event.x_root, event.y_root)

    def launch_full_app(self, icon=None, item=None):
        # Goes through the shared launcher so it works from the frozen exe too,
        # where main.py does not exist on disk.
        if self.current_path:
            shell_integration.launch_mode("--full", self.current_path)
        else:
            shell_integration.launch_mode("--full")

    # --- System Tray ---

    def _setup_tray(self):
        try:
            import pystray
            import PIL.Image

            icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
            img = PIL.Image.open(icon_path) if os.path.exists(icon_path) else PIL.Image.new("RGBA", (32, 32), "#3a86ff")

            menu = pystray.Menu(
                pystray.MenuItem("Show Companion Panel", self.restore_from_tray, default=True),
                pystray.MenuItem("Docked to Explorer", self.toggle_dock, checked=lambda item: self.auto_dock),
                pystray.MenuItem("Always on Top", self.toggle_always_on_top, checked=lambda item: self.always_on_top),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Open Full SizeScanner", self.launch_full_app),
                pystray.MenuItem("Exit", self.quit_app)
            )

            self.tray_icon = pystray.Icon("SizeScannerCompanion", img, "SizeScanner - Explorer Companion", menu)
            threading.Thread(target=self.tray_icon.run, daemon=True).start()
        except Exception as e:
            print(f"System tray init failed: {e}")

    def hide_to_tray(self):
        # With no tray icon there would be no way to bring the window back, so the
        # close button has to actually close.
        if self.tray_icon is None:
            self.quit_app()
            return
        self.withdraw()

    def restore_from_tray(self, icon=None, item=None):
        self.dispatch_ui(self.deiconify)
        self.dispatch_ui(self.lift)

    def quit_app(self, icon=None, item=None):
        self.tracker_stop.set()
        # Stop any scan still walking the disk, or its threads keep the process busy
        self.scanner.request_cancel()
        if self.tray_icon:
            self.tray_icon.stop()
        self.dispatch_ui(self.destroy)

if __name__ == "__main__":
    app = ExplorerCompanionWindow()
    app.mainloop()
