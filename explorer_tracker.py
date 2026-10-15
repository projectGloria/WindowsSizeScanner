"""
explorer_tracker.py - High-accuracy Windows File Explorer detector and tracker.
Detects open and focused File Explorer windows, their exact geometry, and the currently browsed path.
"""

import os
import re
import time
from dataclasses import dataclass
from typing import Optional, List, Tuple, Dict, Any
import ctypes
from ctypes import wintypes
import win32gui
import win32process
import uiautomation as auto

KNOWN_USER_FOLDERS = {
    "Desktop": os.path.join(os.path.expanduser("~"), "Desktop"),
    "Downloads": os.path.join(os.path.expanduser("~"), "Downloads"),
    "Documents": os.path.join(os.path.expanduser("~"), "Documents"),
    "Pictures": os.path.join(os.path.expanduser("~"), "Pictures"),
    "Music": os.path.join(os.path.expanduser("~"), "Music"),
    "Videos": os.path.join(os.path.expanduser("~"), "Videos"),
    "Home": os.path.expanduser("~")
}

GA_ROOTOWNER = 3

# Declared explicitly: the default int return type would truncate a 64-bit HWND.
_GetAncestor = ctypes.windll.user32.GetAncestor
_GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
_GetAncestor.restype = wintypes.HWND

def is_explorer_focused(hwnd: int) -> bool:
    """
    True when `hwnd` is the window the user is actually looking at.

    The overlay is topmost, so drawing whenever Explorer is merely *visible* paints
    the sizes over whatever app is covering it. Ownership is checked as well as
    identity, so transient popups owned by Explorer (context menus, rename edit
    boxes, drop-down panes) do not count as losing focus.
    """
    if not hwnd:
        return False
    try:
        fg = win32gui.GetForegroundWindow()
        if not fg:
            return False
        if fg == hwnd:
            return True
        owner = _GetAncestor(fg, GA_ROOTOWNER)
        return bool(owner) and owner == hwnd
    except Exception:
        return False

EXPLORER_WINDOW_CLASSES = ("CabinetWClass", "ExploreWClass")

# hwnd -> (window title, resolved path, resolved at). A title change means the window
# definitely navigated, so it invalidates immediately. The title alone is not enough
# to prove it did *not*: Explorer titles a window with the folder's basename, so
# C:\proj1\src -> C:\proj2\src leaves the title identical and the old path would be
# served forever. Entries are therefore also re-resolved once they age past
# _PATH_CACHE_TTL.
_PATH_CACHE: Dict[int, Tuple[str, str, float]] = {}
_PATH_CACHE_TTL = 1.5

def list_explorer_hwnds() -> List[int]:
    """
    Returns visible Explorer window handles, topmost first.

    Pure Win32 (EnumWindows returns z-order), which costs microseconds; walking the
    UIAutomation root to find the same windows costs ~180ms per call.
    """
    hwnds: List[int] = []

    def _collect(hwnd, _):
        try:
            if win32gui.IsWindowVisible(hwnd) and win32gui.GetClassName(hwnd) in EXPLORER_WINDOW_CLASSES:
                hwnds.append(hwnd)
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(_collect, None)
    except Exception:
        pass
    return hwnds

@dataclass
class ExplorerInfo:
    hwnd: int
    title: str
    rect: Tuple[int, int, int, int]  # (left, top, right, bottom)
    path: str
    is_minimized: bool
    is_visible: bool
    is_foreground: bool
    cab_ctrl: Any = None

def resolve_breadcrumbs_to_path(items: List[str]) -> Optional[str]:
    """Resolves UI breadcrumb items into a verified filesystem path."""
    if not items:
        return None

    # Filter out navigation meta-items
    filtered = [it for it in items if it not in ("More", "This PC", "Gallery", "Home")]
    if not filtered:
        # If it was just Home or This PC
        if "Home" in items:
            return os.path.expanduser("~")
        return None

    # Check for drive root like 'Local Disk (C:)' or 'DEPO (D:)'
    drive_match = re.search(r'\(([A-Za-z]:)\)', filtered[0])
    if drive_match:
        drive_letter = drive_match.group(1)
        candidate = os.path.join(drive_letter + "\\", *filtered[1:])
        if os.path.exists(candidate):
            return os.path.abspath(candidate)

    # Check if first item is a known user folder (e.g. 'Downloads', 'Desktop')
    first = filtered[0]
    if first in KNOWN_USER_FOLDERS:
        candidate = os.path.join(KNOWN_USER_FOLDERS[first], *filtered[1:])
        if os.path.exists(candidate):
            return os.path.abspath(candidate)

    # Check if last item matches a known folder or user home
    for name, full_path in KNOWN_USER_FOLDERS.items():
        if filtered[-1] == name and os.path.exists(full_path):
            return full_path

    # Try building under user home
    user_cand = os.path.join(os.path.expanduser("~"), *filtered)
    if os.path.exists(user_cand):
        return os.path.abspath(user_cand)

    return None

def _com_explorer_paths() -> List[Tuple[int, str]]:
    """
    (hwnd, path) for every open shell window, via Shell.Application. Costs ~80ms, so
    this only runs on a path-cache miss.

    The HWND is carried along so a window can be matched exactly. Matching on the
    title instead picked the wrong window whenever two of them showed folders with
    the same name.
    """
    paths: List[Tuple[int, str]] = []
    com_initialized = False
    try:
        import pythoncom
        import win32com.client
        # Returns S_FALSE (not an error) when the thread is already initialized,
        # e.g. inside UIAutomationInitializerInThread. Either way it must be
        # balanced by CoUninitialize, or the per-thread refcount leaks.
        pythoncom.CoInitialize()
        com_initialized = True
        shell = win32com.client.Dispatch("Shell.Application")
        for w in shell.Windows():
            try:
                p = w.Document.Folder.Self.Path
                if not p or not os.path.exists(p):
                    continue
                try:
                    win_hwnd = int(w.HWND)
                except Exception:
                    win_hwnd = 0
                paths.append((win_hwnd, os.path.abspath(p)))
            except Exception:
                pass
        shell = None
    except Exception:
        pass
    finally:
        if com_initialized:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass
    return paths

def resolve_explorer_path(hwnd: int, title: str, cab_ctrl: Any) -> str:
    """
    Resolves the filesystem path shown by an Explorer window, memoized per
    (hwnd, title). Falls back through COM -> UIA breadcrumbs -> window title.
    """
    now = time.monotonic()
    cached = _PATH_CACHE.get(hwnd)
    if cached and cached[0] == title and (now - cached[2]) < _PATH_CACHE_TTL:
        return cached[1]

    clean_title = title.replace(" - File Explorer", "").replace("File Explorer", "").strip()
    path = None

    # 1. Shell.Application, matched to this window by HWND (exact), then by title
    com_paths = _com_explorer_paths()
    if com_paths:
        for cp_hwnd, cp in com_paths:
            if cp_hwnd and cp_hwnd == hwnd:
                path = cp
                break
        if not path:
            for _cp_hwnd, cp in com_paths:
                if os.path.basename(cp).lower() == clean_title.lower() or cp.lower() == clean_title.lower():
                    path = cp
                    break
        if not path and len(com_paths) == 1:
            path = com_paths[0][1]

    # 2. UIA breadcrumbs (Win11 nests these under a DesktopChildSiteBridge)
    if not path and cab_ctrl is not None:
        try:
            bridge = cab_ctrl.Control(ClassName="Microsoft.UI.Content.DesktopChildSiteBridge")
            search_root = bridge if bridge.Exists(0.05) else cab_ctrl
            items: List[str] = []
            for c, depth in auto.WalkControl(search_root):
                if depth > 8:
                    continue
                cls = c.ClassName
                if "BreadcrumbBarItemControl" in cls or "FirstCrumb" in cls:
                    name = c.Name.strip()
                    if name and name not in items:
                        items.append(name)
            path = resolve_breadcrumbs_to_path(items)
        except Exception:
            path = None

    # 3. Window title
    if not path and clean_title:
        if clean_title in KNOWN_USER_FOLDERS:
            path = KNOWN_USER_FOLDERS[clean_title]
        elif os.path.exists(clean_title):
            path = os.path.abspath(clean_title)

    path = path or ""
    _PATH_CACHE[hwnd] = (title, path, now)
    # Keep the cache from growing as windows are opened and closed
    if len(_PATH_CACHE) > 32:
        for dead in [h for h in _PATH_CACHE if not win32gui.IsWindow(h)]:
            _PATH_CACHE.pop(dead, None)
    return path

def build_explorer_info(hwnd: int, want_control: bool = True) -> Optional[ExplorerInfo]:
    """Builds ExplorerInfo for one window. The UIA control is only resolved when
    `want_control` is set, since ControlFromHandle is the expensive part."""
    if not hwnd or not win32gui.IsWindow(hwnd):
        return None
    try:
        title = win32gui.GetWindowText(hwnd)
    except Exception:
        title = ""
    try:
        rect = win32gui.GetWindowRect(hwnd)  # (left, top, right, bottom)
    except Exception:
        rect = (0, 0, 0, 0)

    cab = None
    if want_control:
        try:
            cab = auto.ControlFromHandle(hwnd)
        except Exception:
            cab = None

    return ExplorerInfo(
        hwnd=hwnd,
        title=title,
        rect=rect,
        path=resolve_explorer_path(hwnd, title, cab),
        is_minimized=bool(win32gui.IsIconic(hwnd)),
        is_visible=bool(win32gui.IsWindowVisible(hwnd)),
        is_foreground=is_explorer_focused(hwnd),
        cab_ctrl=cab
    )

def get_active_explorer(preferred_hwnd: Optional[int] = None, want_control: bool = True) -> Optional[ExplorerInfo]:
    """
    Returns the Explorer window the user is working in.

    Only one window is inspected -- picking the target is pure Win32, so the costly
    UIA/COM work happens for that window alone instead of for every open Explorer.
    """
    hwnds = list_explorer_hwnds()
    if not hwnds:
        return None

    target = None

    # 1. The focused Explorer window (or one owning the focused popup) wins
    fg = win32gui.GetForegroundWindow()
    if fg:
        if fg in hwnds:
            target = fg
        else:
            try:
                owner = _GetAncestor(fg, GA_ROOTOWNER)
                if owner in hwnds:
                    target = owner
            except Exception:
                pass

    # 2. Stay with the window already being tracked
    if target is None and preferred_hwnd in hwnds and not win32gui.IsIconic(preferred_hwnd):
        target = preferred_hwnd

    # 3. Otherwise the topmost non-minimized window (EnumWindows is z-ordered)
    if target is None:
        for h in hwnds:
            if not win32gui.IsIconic(h):
                target = h
                break

    return build_explorer_info(target or hwnds[0], want_control=want_control)

def get_explorer_windows() -> List[ExplorerInfo]:
    """All open Explorer windows, topmost first. Kept for callers that need the full
    list; the overlay uses get_active_explorer(), which inspects only one window."""
    infos = [build_explorer_info(h) for h in list_explorer_hwnds()]
    return [i for i in infos if i is not None]

if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    act = get_active_explorer()
    print("Active Explorer:", act)
