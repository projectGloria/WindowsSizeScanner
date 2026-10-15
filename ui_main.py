"""
ui_main.py - Modern CustomTkinter Graphical User Interface for SizeScanner.
Provides an interactive, high-performance file and folder size explorer.
"""

import os
import sys
import time
import datetime
import threading
import queue
from typing import List, Dict, Optional, Any, Tuple
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import customtkinter as ctk

from scanner import (
    FolderScanner, ItemInfo, ScanResult,
    format_size, format_number, calculate_allocated_size, get_cluster_size
)
import shell_integration
from shell_integration import launch_mode

# Set appearance mode and color theme
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

def make_percent_bar(percent: float, length: int = 10) -> str:
    """Creates a visual progress bar string."""
    percent = max(0.0, min(100.0, percent))
    filled = int(round(percent / 100.0 * length))
    filled = max(0, min(length, filled))
    # Using block characters
    blocks = "■" * filled + "□" * (length - filled)
    return f"[{blocks}] {percent:5.1f}%"

class SizeScannerApp(ctk.CTk):
    def __init__(self, initial_path: Optional[str] = None):
        super().__init__()

        self.title("SizeScanner - Windows Folder Size Explorer")
        self.geometry("1240x780")
        self.minsize(960, 580)

        # Set App Icon
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
        if os.path.exists(icon_path):
            try:
                self.iconbitmap(icon_path)
            except Exception:
                pass

        # Set DPI Awareness
        self._set_dpi_awareness()

        # State variables
        self.scanner = FolderScanner(max_workers=8)
        self.history: List[str] = []
        self.history_index: int = -1
        self.current_path: str = ""
        self.current_items: List[ItemInfo] = []
        self.filtered_items: List[ItemInfo] = []
        self.scan_thread: Optional[threading.Thread] = None
        self.is_scanning = False
        self.sort_column = "size"
        self.sort_descending = True
        # Cancels just the folder scan (not the file-type or largest-file analyses)
        self.scan_token = None
        self.deep_token = None
        # item path -> treeview row id. Walking every row and reading its tags back
        # through Tcl to find one row cost ~1.1ms per completed folder at 800 rows,
        # and grew quadratically with the folder count.
        self.row_by_path: Dict[str, str] = {}
        # Running total, so each completed folder does not re-sum every item
        self.total_size_so_far: int = 0

        # Resolve initial path
        if initial_path and os.path.exists(initial_path):
            self.start_path = os.path.abspath(initial_path)
            if os.path.isfile(self.start_path):
                self.start_path = os.path.dirname(self.start_path)
        else:
            self.start_path = os.path.expanduser("~")

        # Thread-safe UI dispatch queue
        self.ui_queue: queue.Queue = queue.Queue()
        self._poll_after_id: Optional[str] = None
        self._poll_queue()

        # Build UI
        self._create_widgets()
        self._setup_style()
        self._bind_shortcuts()

        # Initial navigation
        self.navigate_to(self.start_path)

    def dispatch_ui(self, fn, *args):
        """Thread-safely dispatches a function call to the main Tkinter thread."""
        self.ui_queue.put((fn, args))

    def _poll_queue(self):
        """Processes worker thread events on the main thread."""
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
                        print(f"Error executing queued UI callback: {e}")
            except queue.Empty:
                pass
            self._poll_after_id = self.after(25, self._poll_queue)
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

    def _set_dpi_awareness(self):
        try:
            import ctypes
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    def _create_widgets(self):
        # Main layout container
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)  # Tabview expands

        # 1. Top Header & Navigation Frame
        self.top_frame = ctk.CTkFrame(self, corner_radius=0, fg_color=("gray85", "gray17"))
        self.top_frame.grid(row=0, column=0, sticky="ew", padx=0, pady=0)
        self._build_top_bar()

        # 2. Drive Quick Bar
        self.drive_frame = ctk.CTkFrame(self, corner_radius=0, height=38, fg_color=("gray90", "gray14"))
        self.drive_frame.grid(row=1, column=0, sticky="ew", padx=0, pady=0)
        self._build_drive_bar()

        # 3. Search & Filter Bar
        self.search_frame = ctk.CTkFrame(self, corner_radius=0, height=44, fg_color=("gray92", "gray20"))
        self.search_frame.grid(row=2, column=0, sticky="ew", padx=0, pady=(1, 1))
        self._build_search_bar()

        # 4. Central Tabs
        self.tabview = ctk.CTkTabview(self, corner_radius=6)
        self.tabview.grid(row=3, column=0, sticky="nsew", padx=10, pady=(0, 6))

        self.tab_contents = self.tabview.add("📂 Folder Contents")
        self.tab_tree = self.tabview.add("🌲 Expandable Tree")
        self.tab_largest = self.tabview.add("🏆 Top 100 Largest Files")
        self.tab_types = self.tabview.add("📊 File Types Breakdown")

        self._build_contents_tab()
        self._build_tree_tab()
        self._build_largest_tab()
        self._build_types_tab()

        # 5. Bottom Status Bar
        self.status_frame = ctk.CTkFrame(self, corner_radius=0, height=32, fg_color=("gray85", "gray17"))
        self.status_frame.grid(row=4, column=0, sticky="ew", padx=0, pady=0)
        self._build_status_bar()

    def _build_top_bar(self):
        self.top_frame.grid_columnconfigure(5, weight=1)

        # Brand / Logo
        self.brand_label = ctk.CTkLabel(
            self.top_frame,
            text="⚡ SizeScanner",
            font=ctk.CTkFont(size=17, weight="bold"),
            text_color=("#1f538d", "#3a86ff")
        )
        self.brand_label.grid(row=0, column=0, padx=(14, 10), pady=8)

        # Back Button
        self.btn_back = ctk.CTkButton(
            self.top_frame, text="◀", width=34, height=30,
            command=self.go_back, state="disabled"
        )
        self.btn_back.grid(row=0, column=1, padx=2, pady=8)

        # Forward Button
        self.btn_forward = ctk.CTkButton(
            self.top_frame, text="▶", width=34, height=30,
            command=self.go_forward, state="disabled"
        )
        self.btn_forward.grid(row=0, column=2, padx=2, pady=8)

        # Up Button
        self.btn_up = ctk.CTkButton(
            self.top_frame, text="▲ Up", width=55, height=30,
            command=self.go_up
        )
        self.btn_up.grid(row=0, column=3, padx=2, pady=8)

        # Refresh Button
        self.btn_refresh = ctk.CTkButton(
            self.top_frame, text="🔄", width=36, height=30,
            command=self.refresh_current
        )
        self.btn_refresh.grid(row=0, column=4, padx=2, pady=8)

        # Address / Path Entry
        self.path_entry = ctk.CTkEntry(
            self.top_frame, height=32, font=ctk.CTkFont(size=13),
            placeholder_text="Enter folder path (e.g. C:\\Users)..."
        )
        self.path_entry.grid(row=0, column=5, sticky="ew", padx=(6, 6), pady=8)
        self.path_entry.bind("<Return>", lambda e: self.on_path_entered())

        # Browse Button
        self.btn_browse = ctk.CTkButton(
            self.top_frame, text="📁 Browse...", width=85, height=30,
            command=self.browse_folder
        )
        self.btn_browse.grid(row=0, column=6, padx=4, pady=8)

        # Explorer Context Menu Integration Button
        self.btn_integration = ctk.CTkButton(
            self.top_frame, text="⚡ Explorer Menu", width=115, height=30,
            fg_color=("#3a7ebf", "#2b5c8f"),
            command=self.show_integration_dialog
        )
        self.btn_integration.grid(row=0, column=7, padx=4, pady=8)

        # Companion Docking Mode Button
        self.btn_companion = ctk.CTkButton(
            self.top_frame, text="⚡ Dock Companion", width=120, height=30,
            fg_color=("#2a9d8f", "#1f7a6f"),
            command=self.launch_companion_mode
        )
        self.btn_companion.grid(row=0, column=8, padx=4, pady=8)

        # Theme Switch
        self.theme_switch = ctk.CTkSwitch(
            self.top_frame, text="Dark", width=60,
            command=self.toggle_theme
        )
        self.theme_switch.select()
        self.theme_switch.grid(row=0, column=9, padx=(6, 12), pady=8)

    def launch_companion_mode(self):
        """Starts the docked companion. Only hides this window once that succeeded --
        withdrawing first left nothing on screen whenever the launch failed."""
        if launch_mode("--companion"):
            self.withdraw()
        else:
            messagebox.showerror(
                "Companion Mode",
                "Could not start the companion panel."
            )

    def _build_drive_bar(self):
        # Label
        lbl = ctk.CTkLabel(self.drive_frame, text="Drives:", font=ctk.CTkFont(size=12, weight="bold"))
        lbl.pack(side="left", padx=(14, 8), pady=4)

        # Populate drive buttons
        drives = shell_integration.get_system_drives()
        for drive in drives:
            mnt = drive['mountpoint']
            used_str = format_size(drive['used'])
            tot_str = format_size(drive['total'])
            pct = drive['percent']
            btn_text = f"💾 {mnt} ({pct:.0f}% used - {used_str} / {tot_str})"
            btn = ctk.CTkButton(
                self.drive_frame,
                text=btn_text,
                height=26,
                font=ctk.CTkFont(size=11),
                fg_color=("gray75", "gray25"),
                hover_color=("gray65", "gray35"),
                command=lambda p=mnt: self.navigate_to(p)
            )
            btn.pack(side="left", padx=4, pady=4)

    def _build_search_bar(self):
        lbl_search = ctk.CTkLabel(self.search_frame, text="🔍 Filter:", font=ctk.CTkFont(size=12, weight="bold"))
        lbl_search.pack(side="left", padx=(14, 6), pady=6)

        self.search_entry = ctk.CTkEntry(
            self.search_frame,
            width=280,
            height=28,
            placeholder_text="Type to filter items (e.g. .mp4, node_modules)..."
        )
        self.search_entry.pack(side="left", padx=2, pady=6)
        self.search_entry.bind("<KeyRelease>", lambda e: self.on_search_changed())

        self.btn_clear_search = ctk.CTkButton(
            self.search_frame, text="✕", width=28, height=28,
            command=self.clear_search
        )
        self.btn_clear_search.pack(side="left", padx=4, pady=6)

        self.search_count_label = ctk.CTkLabel(
            self.search_frame, text="", font=ctk.CTkFont(size=12)
        )
        self.search_count_label.pack(side="left", padx=10, pady=6)

        # Cancel scan button
        self.btn_cancel_scan = ctk.CTkButton(
            self.search_frame, text="⏹ Stop Scan", width=95, height=28,
            fg_color="#c1121f", hover_color="#780000",
            command=self.cancel_scan, state="disabled"
        )
        self.btn_cancel_scan.pack(side="right", padx=14, pady=6)

    def _setup_style(self):
        style = ttk.Style()
        style.theme_use("clam")

        # Dark theme configuration for Treeviews
        bg_dark = "#202020"
        fg_dark = "#f0f0f0"
        header_bg = "#2b2b2b"
        selected_bg = "#1f538d"

        style.configure(
            "Treeview",
            background=bg_dark,
            foreground=fg_dark,
            fieldbackground=bg_dark,
            rowheight=26,
            font=("Segoe UI", 10),
            borderwidth=0
        )
        style.configure(
            "Treeview.Heading",
            background=header_bg,
            foreground=fg_dark,
            relief="flat",
            font=("Segoe UI", 10, "bold"),
            padding=(6, 4)
        )
        style.map(
            "Treeview",
            background=[("selected", selected_bg)],
            foreground=[("selected", "white")]
        )
        style.map(
            "Treeview.Heading",
            background=[("active", "#383838")]
        )

    def _build_contents_tab(self):
        self.tab_contents.grid_columnconfigure(0, weight=1)
        self.tab_contents.grid_rowconfigure(0, weight=1)

        cols = ("name", "size", "percent", "items", "alloc_size", "mtime", "type")
        self.tree_contents = ttk.Treeview(
            self.tab_contents,
            columns=cols,
            show="headings",
            selectmode="extended"
        )

        self.tree_contents.heading("name", text="Name ▲▼", command=lambda: self.sort_by_column("name"))
        self.tree_contents.heading("size", text="Size ▲▼", command=lambda: self.sort_by_column("size"))
        self.tree_contents.heading("percent", text="% of Parent ▲▼", command=lambda: self.sort_by_column("percent"))
        self.tree_contents.heading("items", text="Items Inside ▲▼", command=lambda: self.sort_by_column("items"))
        self.tree_contents.heading("alloc_size", text="Size on Disk ▲▼", command=lambda: self.sort_by_column("alloc_size"))
        self.tree_contents.heading("mtime", text="Date Modified ▲▼", command=lambda: self.sort_by_column("mtime"))
        self.tree_contents.heading("type", text="Type ▲▼", command=lambda: self.sort_by_column("type"))

        self.tree_contents.column("name", width=340, minwidth=180, anchor="w")
        self.tree_contents.column("size", width=120, minwidth=80, anchor="e")
        self.tree_contents.column("percent", width=180, minwidth=140, anchor="center")
        self.tree_contents.column("items", width=160, minwidth=100, anchor="e")
        self.tree_contents.column("alloc_size", width=120, minwidth=80, anchor="e")
        self.tree_contents.column("mtime", width=150, minwidth=120, anchor="center")
        self.tree_contents.column("type", width=140, minwidth=100, anchor="w")

        # Scrollbars
        vsb = ttk.Scrollbar(self.tab_contents, orient="vertical", command=self.tree_contents.yview)
        hsb = ttk.Scrollbar(self.tab_contents, orient="horizontal", command=self.tree_contents.xview)
        self.tree_contents.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree_contents.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        # Bindings
        self.tree_contents.bind("<Double-1>", self.on_item_double_click)
        self.tree_contents.bind("<Return>", self.on_item_double_click)
        self.tree_contents.bind("<Button-3>", self.show_context_menu)

        # Context Menu
        self.context_menu = tk.Menu(self, tearoff=0, bg="#2b2b2b", fg="white", activebackground="#1f538d", activeforeground="white")
        self.context_menu.add_command(label="📂 Open", command=self.open_selected_item)
        self.context_menu.add_command(label="🔍 Open in File Explorer", command=self.open_in_explorer)
        self.context_menu.add_command(label="💻 Open Terminal Here", command=self.open_terminal_here)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="📋 Copy Full Path", command=self.copy_selected_path)
        self.context_menu.add_command(label="📋 Copy Size", command=self.copy_selected_size)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="🗑️ Move to Recycle Bin", command=self.delete_selected_item)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="ℹ️ Properties", command=self.show_item_properties)

    def _build_tree_tab(self):
        self.tab_tree.grid_columnconfigure(0, weight=1)
        self.tab_tree.grid_rowconfigure(0, weight=1)

        cols = ("size", "items", "alloc_size")
        self.tree_hier = ttk.Treeview(
            self.tab_tree,
            columns=cols,
            show="tree headings",
            selectmode="browse"
        )
        self.tree_hier.heading("#0", text="Directory Hierarchy", anchor="w")
        self.tree_hier.heading("size", text="Total Size", anchor="e")
        self.tree_hier.heading("items", text="Files / Subfolders", anchor="e")
        self.tree_hier.heading("alloc_size", text="Size on Disk", anchor="e")

        self.tree_hier.column("#0", width=450, minwidth=220, anchor="w")
        self.tree_hier.column("size", width=140, minwidth=90, anchor="e")
        self.tree_hier.column("items", width=180, minwidth=110, anchor="e")
        self.tree_hier.column("alloc_size", width=140, minwidth=90, anchor="e")

        vsb = ttk.Scrollbar(self.tab_tree, orient="vertical", command=self.tree_hier.yview)
        hsb = ttk.Scrollbar(self.tab_tree, orient="horizontal", command=self.tree_hier.xview)
        self.tree_hier.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree_hier.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        self.tree_hier.bind("<<TreeviewOpen>>", self.on_tree_node_expanded)
        self.tree_hier.bind("<Double-1>", self.on_tree_double_click)

    def _build_largest_tab(self):
        self.tab_largest.grid_columnconfigure(0, weight=1)
        self.tab_largest.grid_rowconfigure(1, weight=1)

        ctrl_frame = ctk.CTkFrame(self.tab_largest, fg_color="transparent")
        ctrl_frame.grid(row=0, column=0, sticky="ew", padx=6, pady=6)

        btn_find_largest = ctk.CTkButton(
            ctrl_frame,
            text="🚀 Scan Top 100 Largest Files",
            width=200,
            command=self.scan_top_largest_files
        )
        btn_find_largest.pack(side="left", padx=4)

        self.largest_status_lbl = ctk.CTkLabel(
            ctrl_frame,
            text="Click scan to discover the 100 largest files inside this folder tree.",
            font=ctk.CTkFont(size=12)
        )
        self.largest_status_lbl.pack(side="left", padx=12)

        cols = ("rank", "name", "size", "path", "mtime")
        self.tree_largest = ttk.Treeview(
            self.tab_largest,
            columns=cols,
            show="headings",
            selectmode="browse"
        )
        self.tree_largest.heading("rank", text="#", anchor="center")
        self.tree_largest.heading("name", text="File Name", anchor="w")
        self.tree_largest.heading("size", text="Size", anchor="e")
        self.tree_largest.heading("path", text="Folder Location", anchor="w")
        self.tree_largest.heading("mtime", text="Date Modified", anchor="center")

        self.tree_largest.column("rank", width=50, minwidth=40, anchor="center")
        self.tree_largest.column("name", width=250, minwidth=150, anchor="w")
        self.tree_largest.column("size", width=120, minwidth=80, anchor="e")
        self.tree_largest.column("path", width=500, minwidth=250, anchor="w")
        self.tree_largest.column("mtime", width=150, minwidth=120, anchor="center")

        vsb = ttk.Scrollbar(self.tab_largest, orient="vertical", command=self.tree_largest.yview)
        hsb = ttk.Scrollbar(self.tab_largest, orient="horizontal", command=self.tree_largest.xview)
        self.tree_largest.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree_largest.grid(row=1, column=0, sticky="nsew")
        vsb.grid(row=1, column=1, sticky="ns")
        hsb.grid(row=2, column=0, sticky="ew")

        self.tree_largest.bind("<Double-1>", lambda e: self.open_largest_selected())
        self.tree_largest.bind("<Button-3>", self.show_largest_context_menu)

        self.largest_menu = tk.Menu(self, tearoff=0, bg="#2b2b2b", fg="white", activebackground="#1f538d")
        self.largest_menu.add_command(label="📂 Open File", command=self.open_largest_selected)
        self.largest_menu.add_command(label="🔍 Open in File Explorer", command=self.reveal_largest_selected)
        self.largest_menu.add_command(label="📋 Copy Full Path", command=self.copy_largest_path)
        self.largest_menu.add_separator()
        self.largest_menu.add_command(label="🗑️ Move to Recycle Bin", command=self.delete_largest_selected)

    def _build_types_tab(self):
        self.tab_types.grid_columnconfigure(0, weight=1)
        self.tab_types.grid_rowconfigure(1, weight=1)

        ctrl_frame = ctk.CTkFrame(self.tab_types, fg_color="transparent")
        ctrl_frame.grid(row=0, column=0, sticky="ew", padx=6, pady=6)

        btn_scan_types = ctk.CTkButton(
            ctrl_frame,
            text="📊 Analyze File Types Breakdown",
            width=220,
            command=self.scan_file_types
        )
        btn_scan_types.pack(side="left", padx=4)

        self.types_status_lbl = ctk.CTkLabel(
            ctrl_frame,
            text="Click analyze to see which file formats (e.g. .mp4, .zip, .iso) consume the most disk space.",
            font=ctk.CTkFont(size=12)
        )
        self.types_status_lbl.pack(side="left", padx=12)

        cols = ("ext", "size", "percent", "count")
        self.tree_types = ttk.Treeview(
            self.tab_types,
            columns=cols,
            show="headings",
            selectmode="browse"
        )
        self.tree_types.heading("ext", text="Extension / Type", anchor="w")
        self.tree_types.heading("size", text="Total Space", anchor="e")
        self.tree_types.heading("percent", text="% of Total", anchor="center")
        self.tree_types.heading("count", text="File Count", anchor="e")

        self.tree_types.column("ext", width=180, minwidth=100, anchor="w")
        self.tree_types.column("size", width=150, minwidth=90, anchor="e")
        self.tree_types.column("percent", width=220, minwidth=150, anchor="center")
        self.tree_types.column("count", width=150, minwidth=90, anchor="e")

        vsb = ttk.Scrollbar(self.tab_types, orient="vertical", command=self.tree_types.yview)
        hsb = ttk.Scrollbar(self.tab_types, orient="horizontal", command=self.tree_types.xview)
        self.tree_types.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree_types.grid(row=1, column=0, sticky="nsew")
        vsb.grid(row=1, column=1, sticky="ns")
        hsb.grid(row=2, column=0, sticky="ew")

    def _build_status_bar(self):
        self.status_frame.grid_columnconfigure(1, weight=1)

        self.lbl_status = ctk.CTkLabel(
            self.status_frame, text="Ready", font=ctk.CTkFont(size=11)
        )
        self.lbl_status.grid(row=0, column=0, padx=14, pady=4, sticky="w")

        self.prog_bar = ctk.CTkProgressBar(self.status_frame, height=10, width=200)
        self.prog_bar.set(0.0)
        self.prog_bar.grid(row=0, column=1, padx=20, pady=4, sticky="ew")

        self.lbl_summary = ctk.CTkLabel(
            self.status_frame, text="", font=ctk.CTkFont(size=11)
        )
        self.lbl_summary.grid(row=0, column=2, padx=14, pady=4, sticky="e")

    def _bind_shortcuts(self):
        self.bind("<F5>", lambda e: self.refresh_current())
        self.bind("<Alt-Left>", lambda e: self.go_back())
        self.bind("<Alt-Right>", lambda e: self.go_forward())
        self.bind("<Alt-Up>", lambda e: self.go_up())
        self.bind("<BackSpace>", lambda e: self._on_backspace(e))
        self.bind("<Delete>", lambda e: self._on_delete_key(e))

    def _in_text_entry(self, widget) -> bool:
        """True while the user is typing, so window-wide keys stay out of the way."""
        return isinstance(widget, (tk.Entry, ttk.Entry))

    def _on_backspace(self, event):
        # Only go up if not currently focused in a text entry
        if not self._in_text_entry(event.widget):
            self.go_up()

    def _on_delete_key(self, event):
        # Delete is bound window-wide, so without this pressing it while editing the
        # filter or the path popped a "move to Recycle Bin" confirmation.
        if not self._in_text_entry(event.widget):
            self.delete_selected_item()

    def toggle_theme(self):
        if self.theme_switch.get():
            ctk.set_appearance_mode("dark")
            self._update_tree_theme(is_dark=True)
            self.theme_switch.configure(text="Dark")
        else:
            ctk.set_appearance_mode("light")
            self._update_tree_theme(is_dark=False)
            self.theme_switch.configure(text="Light")

    def _update_tree_theme(self, is_dark: bool):
        style = ttk.Style()
        if is_dark:
            bg = "#202020"
            fg = "#f0f0f0"
            hdr_bg = "#2b2b2b"
            sel_bg = "#1f538d"
            menu_bg = "#2b2b2b"
        else:
            bg = "#ffffff"
            fg = "#111111"
            hdr_bg = "#e0e0e0"
            sel_bg = "#3a7ebf"
            menu_bg = "#f0f0f0"

        style.configure("Treeview", background=bg, foreground=fg, fieldbackground=bg)
        style.configure("Treeview.Heading", background=hdr_bg, foreground=fg)
        style.map("Treeview", background=[("selected", sel_bg)], foreground=[("selected", "white")])
        self.context_menu.configure(bg=menu_bg, fg=fg)
        self.largest_menu.configure(bg=menu_bg, fg=fg)

    # --- Navigation Logic ---

    def navigate_to(self, target_path: str, add_history: bool = True):
        norm_path = os.path.abspath(target_path)
        if not os.path.exists(norm_path) or not os.path.isdir(norm_path):
            messagebox.showerror("Invalid Path", f"Directory does not exist:\n{norm_path}")
            return

        if self.is_scanning:
            self.cancel_scan()

        self.current_path = norm_path
        self.path_entry.delete(0, "end")
        self.path_entry.insert(0, norm_path)

        # Manage history
        if add_history:
            if self.history_index < len(self.history) - 1:
                self.history = self.history[:self.history_index + 1]
            self.history.append(norm_path)
            self.history_index = len(self.history) - 1

        self.btn_back.configure(state="normal" if self.history_index > 0 else "disabled")
        self.btn_forward.configure(state="normal" if self.history_index < len(self.history) - 1 else "disabled")

        parent = os.path.dirname(norm_path)
        self.btn_up.configure(state="normal" if parent and parent != norm_path else "disabled")

        # Clear search
        self.search_entry.delete(0, "end")
        self.search_count_label.configure(text="")

        # Start scan for this directory
        self._start_folder_scan(norm_path)
        self._populate_tree_hierarchy_root(norm_path)

    def go_back(self):
        if self.history_index > 0:
            self.history_index -= 1
            path = self.history[self.history_index]
            self.navigate_to(path, add_history=False)

    def go_forward(self):
        if self.history_index < len(self.history) - 1:
            self.history_index += 1
            path = self.history[self.history_index]
            self.navigate_to(path, add_history=False)

    def go_up(self):
        parent = os.path.dirname(self.current_path)
        if parent and parent != self.current_path and os.path.exists(parent):
            self.navigate_to(parent)

    def refresh_current(self):
        if self.current_path:
            self.scanner.clear_cache(self.current_path)
            self.navigate_to(self.current_path, add_history=False)

    def on_path_entered(self):
        val = self.path_entry.get().strip()
        if val:
            self.navigate_to(val)

    def browse_folder(self):
        picked = filedialog.askdirectory(initialdir=self.current_path or os.path.expanduser("~"))
        if picked:
            self.navigate_to(picked)

    # --- Scanning & Table Rendering ---

    def _start_folder_scan(self, folder_path: str):
        self.is_scanning = True
        self.btn_cancel_scan.configure(state="normal")
        self.lbl_status.configure(text="Reading folder contents...")
        self.prog_bar.set(0.0)

        # Clear existing table items
        for row in self.tree_contents.get_children():
            self.tree_contents.delete(row)
        self.row_by_path.clear()

        # Fast immediate load of direct children
        immediate_items = self.scanner.scan_directory_immediate(folder_path)
        self.current_items = immediate_items
        self.filtered_items = list(immediate_items)
        self.total_size_so_far = sum(it.size for it in immediate_items)

        # Render immediate items (files have size, folders start pending)
        self._render_items(self.filtered_items, total_parent_size=1)

        token = self.scanner.begin_operation()
        self.scan_token = token

        # Launch background thread to calculate recursive folder sizes
        def _worker():
            def _on_folder_done(item_info: ItemInfo, done_cnt: int, tot_cnt: int):
                self.dispatch_ui(self._update_single_folder_row, item_info, done_cnt, tot_cnt, folder_path)

            try:
                res = self.scanner.scan_directory(
                    folder_path,
                    on_folder_scanned=_on_folder_done,
                    token=token,
                    # The listing above is reused rather than scandir'ing the same
                    # folder a second time
                    items=immediate_items
                )
                self.dispatch_ui(self._on_scan_completed, res)
            finally:
                self.scanner.end_operation(token)

        self.scan_thread = threading.Thread(target=_worker, daemon=True)
        self.scan_thread.start()

    def _update_single_folder_row(self, item_info: ItemInfo, done_cnt: int, tot_cnt: int, scan_path: str):
        # A scan of a folder we have already navigated away from must not write into
        # the table showing a different folder.
        if not self.is_scanning or scan_path != self.current_path:
            return

        pct_done = done_cnt / max(1, tot_cnt)
        self.prog_bar.set(pct_done)
        self.lbl_status.configure(text=f"Scanning folders: {done_cnt} / {tot_cnt} completed...")

        # item_info IS the object in current_items (scan_directory sizes them in
        # place), so the sizes are already applied; just keep the running total.
        self.total_size_so_far += item_info.size
        total_size = self.total_size_so_far if self.total_size_so_far > 0 else 1

        item_id = self.row_by_path.get(item_info.path)
        if not item_id:
            return
        item_pct = (item_info.size / total_size) * 100.0
        items_str = f"{format_number(item_info.file_count)} files, {format_number(item_info.dir_count)} folders"
        mtime_str = datetime.datetime.fromtimestamp(item_info.mtime).strftime("%Y-%m-%d %H:%M") if item_info.mtime else ""

        try:
            self.tree_contents.set(item_id, "size", format_size(item_info.size))
            self.tree_contents.set(item_id, "percent", make_percent_bar(item_pct))
            self.tree_contents.set(item_id, "items", items_str)
            self.tree_contents.set(item_id, "alloc_size", format_size(item_info.allocated_size))
            self.tree_contents.set(item_id, "mtime", mtime_str)
        except tk.TclError:
            # Row went away (the table was rebuilt by a filter or a re-sort)
            self.row_by_path.pop(item_info.path, None)

    def _on_scan_completed(self, res: ScanResult):
        # Late result from a folder the user has already left: its items belong to a
        # different directory and must not replace what is on screen.
        if res.current_path != self.current_path:
            return

        self.is_scanning = False
        self.btn_cancel_scan.configure(state="disabled")
        self.prog_bar.set(1.0)

        if not res.is_complete:
            self.lbl_status.configure(text="Scan cancelled.")
        else:
            self.lbl_status.configure(text=f"Scan complete in {res.scan_duration:.2f}s")

        self.current_items = res.items
        self.total_size_so_far = res.total_size
        self._apply_filter_and_sort()
        self._update_summary(res)

    def cancel_scan(self):
        if self.is_scanning:
            # Only this folder scan: an in-flight file-type or largest-files analysis
            # has its own token and keeps running.
            if self.scan_token is not None:
                self.scan_token.cancel()
            self.lbl_status.configure(text="Cancelling scan...")
            self.btn_cancel_scan.configure(state="disabled")

    def _update_summary(self, res: ScanResult):
        dir_count = sum(1 for it in res.items if it.is_dir)
        file_count = sum(1 for it in res.items if not it.is_dir)
        tot_size_str = format_size(res.total_size)
        tot_alloc_str = format_size(res.total_allocated_size)

        drive_letter = os.path.splitdrive(self.current_path)[0]
        free_str = ""
        try:
            usage = shell_integration.psutil.disk_usage(self.current_path)
            free_str = f" | {drive_letter} Free: {format_size(usage.free)}"
        except Exception:
            pass

        summary_text = f"Items: {len(res.items)} ({dir_count} folders, {file_count} files) | Size: {tot_size_str} (Disk: {tot_alloc_str}){free_str}"
        self.lbl_summary.configure(text=summary_text)

    def _render_items(self, items: List[ItemInfo], total_parent_size: int = 1):
        self.tree_contents.delete(*self.tree_contents.get_children())
        self.row_by_path.clear()

        if total_parent_size <= 0:
            total_parent_size = 1

        for item in items:
            prefix = "📁 " if item.is_dir else "📄 "
            display_name = prefix + item.name

            if item.is_dir and not item.is_scanned:
                size_str = "Calculating..."
                bar_str = "[..........]  --.-%"
                items_str = "Calculating..."
                alloc_str = "Calculating..."
            else:
                size_str = format_size(item.size)
                pct = (item.size / total_parent_size) * 100.0
                bar_str = make_percent_bar(pct)
                if item.is_dir:
                    items_str = f"{format_number(item.file_count)} files, {format_number(item.dir_count)} folders"
                else:
                    items_str = "1 file"
                alloc_str = format_size(item.allocated_size)

            mtime_str = datetime.datetime.fromtimestamp(item.mtime).strftime("%Y-%m-%d %H:%M") if item.mtime else ""

            # One Tcl call per row: values and tags both go in with the insert
            row_id = self.tree_contents.insert(
                "",
                "end",
                values=(
                    display_name,
                    size_str,
                    bar_str,
                    items_str,
                    alloc_str,
                    mtime_str,
                    item.type_desc
                ),
                tags=(item.path,)
            )
            self.row_by_path[item.path] = row_id

    # --- Sorting and Filtering ---

    def sort_by_column(self, col: str):
        if self.sort_column == col:
            self.sort_descending = not self.sort_descending
        else:
            self.sort_column = col
            self.sort_descending = True  # Default to descending for numbers/sizes

        self._apply_filter_and_sort()

    def _apply_filter_and_sort(self):
        query = self.search_entry.get().strip().lower()
        if query:
            self.filtered_items = [
                it for it in self.current_items
                if query in it.name.lower() or query in it.type_desc.lower()
            ]
            self.search_count_label.configure(text=f"Showing {len(self.filtered_items)} of {len(self.current_items)}")
        else:
            self.filtered_items = list(self.current_items)
            self.search_count_label.configure(text="")

        # Sort items
        def _get_sort_key(item: ItemInfo):
            if self.sort_column == "name":
                # Keep directories on top if desired, or pure name
                return (not item.is_dir, item.name.lower())
            elif self.sort_column in ("size", "percent"):
                return item.size
            elif self.sort_column == "items":
                return item.file_count + item.dir_count
            elif self.sort_column == "alloc_size":
                return item.allocated_size
            elif self.sort_column == "mtime":
                return item.mtime
            elif self.sort_column == "type":
                return item.type_desc.lower()
            return item.size

        self.filtered_items.sort(key=_get_sort_key, reverse=self.sort_descending)

        tot_size = sum(it.size for it in self.current_items)
        self._render_items(self.filtered_items, total_parent_size=tot_size)

    def on_search_changed(self):
        self._apply_filter_and_sort()

    def clear_search(self):
        self.search_entry.delete(0, "end")
        self.on_search_changed()

    # --- Item Interaction ---

    def _get_selected_item_info(self) -> Optional[ItemInfo]:
        sel = self.tree_contents.selection()
        if not sel:
            return None
        item_id = sel[0]
        tags = self.tree_contents.item(item_id, "tags")
        if not tags:
            return None
        path = tags[0]
        for it in self.current_items:
            if it.path == path:
                return it
        return None

    def on_item_double_click(self, event=None):
        info = self._get_selected_item_info()
        if not info:
            return
        if info.is_dir:
            self.navigate_to(info.path)
        else:
            shell_integration.open_item_default(info.path)

    def show_context_menu(self, event):
        row_id = self.tree_contents.identify_row(event.y)
        if row_id:
            self.tree_contents.selection_set(row_id)
            self.context_menu.tk_popup(event.x_root, event.y_root)

    def open_selected_item(self):
        info = self._get_selected_item_info()
        if info:
            if info.is_dir:
                self.navigate_to(info.path)
            else:
                shell_integration.open_item_default(info.path)

    def open_in_explorer(self):
        info = self._get_selected_item_info()
        if info:
            shell_integration.open_in_explorer(info.path)
        elif self.current_path:
            shell_integration.open_in_explorer(self.current_path)

    def open_terminal_here(self):
        info = self._get_selected_item_info()
        target = info.path if info else self.current_path
        shell_integration.open_in_terminal(target)

    def copy_selected_path(self):
        info = self._get_selected_item_info()
        if info:
            self.clipboard_clear()
            self.clipboard_append(info.path)
            self.lbl_status.configure(text=f"Copied path to clipboard: {info.path}")

    def copy_selected_size(self):
        info = self._get_selected_item_info()
        if info:
            self.clipboard_clear()
            self.clipboard_append(f"{format_size(info.size)} ({format_number(info.size)} bytes)")
            self.lbl_status.configure(text=f"Copied size to clipboard: {format_size(info.size)}")

    def delete_selected_item(self):
        info = self._get_selected_item_info()
        if not info:
            return
        kind = "folder" if info.is_dir else "file"
        msg = f"Are you sure you want to move this {kind} to the Recycle Bin?\n\n{info.name}\n({format_size(info.size)})"
        if messagebox.askyesno("Confirm Delete", msg, icon="warning"):
            success = shell_integration.move_to_recycle_bin(info.path)
            if success:
                messagebox.showinfo("Success", f"Moved to Recycle Bin:\n{info.name}")
                self.refresh_current()
            else:
                messagebox.showerror("Error", f"Could not delete:\n{info.path}")

    def show_item_properties(self):
        info = self._get_selected_item_info()
        if not info:
            return

        prop_win = ctk.CTkToplevel(self)
        prop_win.title(f"Properties - {info.name}")
        prop_win.geometry("520x420")
        prop_win.resizable(False, False)
        prop_win.transient(self)
        prop_win.grab_set()

        frame = ctk.CTkFrame(prop_win, corner_radius=10)
        frame.pack(fill="both", expand=True, padx=16, pady=16)

        title_lbl = ctk.CTkLabel(frame, text=f"{'📁' if info.is_dir else '📄'} {info.name}", font=ctk.CTkFont(size=16, weight="bold"))
        title_lbl.pack(anchor="w", padx=16, pady=(12, 10))

        sep = ttk.Separator(frame, orient="horizontal")
        sep.pack(fill="x", padx=16, pady=6)

        details = [
            ("Type:", info.type_desc),
            ("Location:", os.path.dirname(info.path)),
            ("Full Path:", info.path),
            ("Size:", f"{format_size(info.size)} ({format_number(info.size)} bytes)"),
            ("Size on Disk:", f"{format_size(info.allocated_size)} ({format_number(info.allocated_size)} bytes)"),
        ]
        if info.is_dir:
            details.append(("Contains:", f"{format_number(info.file_count)} Files, {format_number(info.dir_count)} Folders"))

        if info.mtime:
            dt = datetime.datetime.fromtimestamp(info.mtime).strftime("%Y-%m-%d %H:%M:%S")
            details.append(("Date Modified:", dt))

        for label, val in details:
            row_frame = ctk.CTkFrame(frame, fg_color="transparent")
            row_frame.pack(fill="x", padx=16, pady=3)
            lbl1 = ctk.CTkLabel(row_frame, text=label, width=110, anchor="w", font=ctk.CTkFont(size=12, weight="bold"))
            lbl1.pack(side="left")
            lbl2 = ctk.CTkLabel(row_frame, text=val, anchor="w", font=ctk.CTkFont(size=12))
            lbl2.pack(side="left", fill="x", expand=True)

        btn_close = ctk.CTkButton(frame, text="Close", width=100, command=prop_win.destroy)
        btn_close.pack(side="bottom", pady=14)

    # --- Tab 2: Tree Hierarchy Tab ---

    def _populate_tree_hierarchy_root(self, root_path: str):
        for row in self.tree_hier.get_children():
            self.tree_hier.delete(row)

        root_name = os.path.basename(root_path) or root_path
        root_node = self.tree_hier.insert(
            "",
            "end",
            text=f"📁 {root_name}",
            values=("Calculating...", "...", "..."),
            open=True,
            tags=(root_path,)
        )
        self._load_tree_subdirs(root_node, root_path)

    def _load_tree_subdirs(self, parent_node: str, folder_path: str):
        """Lists `folder_path`'s subfolders into `parent_node`.

        The scandir runs on a worker: on a network share or a folder with tens of
        thousands of entries, doing it here froze the window."""
        try:
            self.tree_hier.delete(*self.tree_hier.get_children(parent_node))
            self.tree_hier.insert(parent_node, "end", text=self.LOADING_TEXT)
        except tk.TclError:
            return

        def _worker():
            names: List[Tuple[str, str]] = []
            try:
                with os.scandir(folder_path) as entries:
                    for e in entries:
                        try:
                            if e.is_dir(follow_symlinks=False):
                                names.append((e.name, e.path))
                        except OSError:
                            continue
            except (PermissionError, OSError):
                pass
            names.sort(key=lambda pair: pair[0].lower())
            self.dispatch_ui(self._fill_tree_subdirs, parent_node, names)

        threading.Thread(target=_worker, daemon=True).start()

    def _fill_tree_subdirs(self, parent_node: str, names: List[Tuple[str, str]]):
        try:
            self.tree_hier.delete(*self.tree_hier.get_children(parent_node))
            for name, path in names:
                # Show a size straight away when one is already cached
                size_str = items_str = alloc_str = "..."
                cached = self.scanner.get_cached(path)
                if cached is not None:
                    c_size, c_files, c_dirs, _ = cached
                    size_str = format_size(c_size)
                    items_str = f"{c_files} f, {c_dirs} d"
                    alloc_str = format_size(calculate_allocated_size(c_size))

                node = self.tree_hier.insert(
                    parent_node,
                    "end",
                    text=f"📁 {name}",
                    values=(size_str, items_str, alloc_str),
                    tags=(path,)
                )
                # Insert dummy child to enable expansion arrow
                self.tree_hier.insert(node, "end", text=self.PLACEHOLDER_TEXT)
        except tk.TclError:
            # The node was removed while we were listing it (navigation, refresh)
            pass

    # A node still holding PLACEHOLDER_TEXT has not been read yet; LOADING_TEXT marks
    # one whose worker is already running, so it is not picked up a second time.
    PLACEHOLDER_TEXT = "Loading..."
    LOADING_TEXT = "Reading…"

    def on_tree_node_expanded(self, event):
        """
        Fills in the children of whichever node was just opened.

        The old version used selection()[0], but clicking the expander arrow moves
        neither the selection nor the focus, so expanding one node reloaded a
        different one. <<TreeviewOpen>> does not say which item it fired for either,
        so the opened node is found by looking for an open node that still holds its
        "Loading..." placeholder.
        """
        for node in self._nodes_awaiting_load(""):
            tags = self.tree_hier.item(node, "tags")
            if tags:
                self._load_tree_subdirs(node, tags[0])

    def _nodes_awaiting_load(self, parent: str) -> List[str]:
        """Open nodes at or below `parent` whose only child is the placeholder."""
        pending: List[str] = []
        for node in self.tree_hier.get_children(parent):
            if not self.tree_hier.item(node, "open"):
                continue
            children = self.tree_hier.get_children(node)
            if len(children) == 1 and self.tree_hier.item(children[0], "text") == self.PLACEHOLDER_TEXT:
                pending.append(node)
            else:
                pending.extend(self._nodes_awaiting_load(node))
        return pending

    def on_tree_double_click(self, event):
        sel = self.tree_hier.selection()
        if not sel:
            return
        node = sel[0]
        tags = self.tree_hier.item(node, "tags")
        if tags and os.path.isdir(tags[0]):
            self.navigate_to(tags[0])
            self.tabview.set("📂 Folder Contents")

    # --- Tab 3: Top 100 Largest Files ---

    def scan_top_largest_files(self):
        if not self.current_path:
            return
        self.largest_status_lbl.configure(text=f"Scanning largest files in {self.current_path}...")
        for row in self.tree_largest.get_children():
            self.tree_largest.delete(row)

        # Its own token, so this analysis neither cancels nor is cancelled by the
        # folder scan running in the background.
        if self.deep_token is not None:
            self.deep_token.cancel()
        token = self.scanner.begin_operation()
        self.deep_token = token
        scan_root = self.current_path

        def _worker():
            def _prog(cnt, kept):
                self.dispatch_ui(lambda c=cnt: self.largest_status_lbl.configure(text=f"Indexed {format_number(c)} files..."))

            try:
                results = self.scanner.deep_scan_largest_files(
                    scan_root, limit=100, on_progress=_prog, token=token
                )
                if not token.cancelled:
                    self.dispatch_ui(self._on_largest_files_completed, results)
            finally:
                self.scanner.end_operation(token)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_largest_files_completed(self, results: List[Tuple[str, str, int, float]]):
        for row in self.tree_largest.get_children():
            self.tree_largest.delete(row)

        self.largest_status_lbl.configure(text=f"Found {len(results)} largest files in current tree.")

        for rank, (name, path, size, mtime) in enumerate(results, start=1):
            parent_dir = os.path.dirname(path)
            dt_str = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M") if mtime else ""
            self.tree_largest.insert(
                "",
                "end",
                values=(
                    rank,
                    f"📄 {name}",
                    format_size(size),
                    parent_dir,
                    dt_str
                ),
                tags=(path,)
            )

    def _get_largest_selected_path(self) -> Optional[str]:
        sel = self.tree_largest.selection()
        if not sel:
            return None
        tags = self.tree_largest.item(sel[0], "tags")
        return tags[0] if tags else None

    def open_largest_selected(self):
        path = self._get_largest_selected_path()
        if path and os.path.exists(path):
            shell_integration.open_item_default(path)

    def reveal_largest_selected(self):
        path = self._get_largest_selected_path()
        if path and os.path.exists(path):
            shell_integration.open_in_explorer(path)

    def copy_largest_path(self):
        path = self._get_largest_selected_path()
        if path:
            self.clipboard_clear()
            self.clipboard_append(path)
            self.lbl_status.configure(text=f"Copied path: {path}")

    def delete_largest_selected(self):
        path = self._get_largest_selected_path()
        if not path or not os.path.exists(path):
            return
        if messagebox.askyesno("Confirm Delete", f"Move this file to Recycle Bin?\n\n{path}", icon="warning"):
            if shell_integration.move_to_recycle_bin(path):
                messagebox.showinfo("Success", "Moved to Recycle Bin!")
                self.scan_top_largest_files()

    def show_largest_context_menu(self, event):
        row_id = self.tree_largest.identify_row(event.y)
        if row_id:
            self.tree_largest.selection_set(row_id)
            self.largest_menu.tk_popup(event.x_root, event.y_root)

    # --- Tab 4: File Types Breakdown ---

    def scan_file_types(self):
        if not self.current_path:
            return
        self.types_status_lbl.configure(text=f"Analyzing file formats in {self.current_path}...")
        for row in self.tree_types.get_children():
            self.tree_types.delete(row)

        token = self.scanner.begin_operation()
        scan_root = self.current_path

        def _worker():
            try:
                stats = self.scanner.deep_scan_file_types(scan_root, token=token)
                if not token.cancelled:
                    self.dispatch_ui(self._on_file_types_completed, stats)
            finally:
                self.scanner.end_operation(token)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_file_types_completed(self, stats: Dict[str, Dict[str, Any]]):
        for row in self.tree_types.get_children():
            self.tree_types.delete(row)

        tot_size = sum(data['size'] for data in stats.values())
        if tot_size <= 0:
            tot_size = 1

        sorted_types = sorted(stats.items(), key=lambda item: item[1]['size'], reverse=True)
        self.types_status_lbl.configure(text=f"Analyzed {len(sorted_types)} file formats. Total space: {format_size(tot_size)}")

        for ext, data in sorted_types:
            size = data['size']
            cnt = data['count']
            pct = (size / tot_size) * 100.0
            bar_str = make_percent_bar(pct, length=12)

            self.tree_types.insert(
                "",
                "end",
                values=(
                    ext,
                    format_size(size),
                    bar_str,
                    format_number(cnt)
                )
            )

    # --- Windows Explorer Context Menu Integration Dialog ---

    def show_integration_dialog(self):
        is_registered = shell_integration.is_context_menu_registered()

        dialog = ctk.CTkToplevel(self)
        dialog.title("Windows Explorer Integration")
        dialog.geometry("540x360")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()

        frame = ctk.CTkFrame(dialog, corner_radius=10)
        frame.pack(fill="both", expand=True, padx=16, pady=16)

        lbl_head = ctk.CTkLabel(frame, text="⚡ Windows Explorer Right-Click Menu", font=ctk.CTkFont(size=16, weight="bold"))
        lbl_head.pack(anchor="w", padx=16, pady=(12, 6))

        status_str = "Active (Registered)" if is_registered else "Inactive (Not registered)"
        status_color = "#38b000" if is_registered else "#ffb703"

        lbl_status_desc = ctk.CTkLabel(
            frame,
            text=f"Current Status: {status_str}",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=status_color
        )
        lbl_status_desc.pack(anchor="w", padx=16, pady=(0, 10))

        info_txt = (
            "When enabled, you can right-click any folder or drive in Windows File Explorer\n"
            "and click 'Open in SizeScanner' to instantly see everything's size!\n\n"
            "• Works directly for all user folders and drives.\n"
            "• No administrator privileges needed.\n"
            "• Cleanly removable with one click anytime."
        )
        lbl_info = ctk.CTkLabel(frame, text=info_txt, font=ctk.CTkFont(size=12), justify="left")
        lbl_info.pack(anchor="w", padx=16, pady=6)

        btn_box = ctk.CTkFrame(frame, fg_color="transparent")
        btn_box.pack(fill="x", padx=16, pady=16)

        def _do_register():
            success, msg = shell_integration.register_context_menu()
            if success:
                messagebox.showinfo("Explorer Integration", msg)
                dialog.destroy()
            else:
                messagebox.showerror("Explorer Integration", msg)

        def _do_unregister():
            success, msg = shell_integration.unregister_context_menu()
            if success:
                messagebox.showinfo("Explorer Integration", msg)
                dialog.destroy()
            else:
                messagebox.showerror("Explorer Integration", msg)

        if not is_registered:
            btn_act = ctk.CTkButton(
                btn_box,
                text="✅ Enable Explorer Context Menu",
                fg_color="#2a9d8f",
                hover_color="#21867a",
                command=_do_register
            )
            btn_act.pack(side="left", padx=6)
        else:
            btn_act = ctk.CTkButton(
                btn_box,
                text="❌ Remove from Explorer Context Menu",
                fg_color="#e76f51",
                hover_color="#c85a3e",
                command=_do_unregister
            )
            btn_act.pack(side="left", padx=6)

        btn_close = ctk.CTkButton(btn_box, text="Close", width=90, fg_color=("gray70", "gray30"), command=dialog.destroy)
        btn_close.pack(side="right", padx=6)
