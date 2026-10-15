"""
test_overlay_follow.py - Geometry tests for the in-place overlay.

Covers the parts that go wrong silently: that overlay_hwnd is the window actually
being positioned, that a window move re-anchors the overlay by the same delta, and
that a resize hides it instead of leaving it mis-anchored.
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
import win32gui
from explorer_overlay import ExplorerSizeOverlay, OverlayLayout
from scanner import FolderScanner

ov = ExplorerSizeOverlay(FolderScanner())

# 1. Is overlay_hwnd really the toplevel we position?
ov.root.geometry("200x100+300+400")
ov.root.deiconify(); ov.is_visible = True
ov.root.update()
r = win32gui.GetWindowRect(ov.overlay_hwnd)
print(f"overlay_hwnd={ov.overlay_hwnd} rect={r}")
ok_hwnd = abs(r[0] - 300) <= 2 and abs(r[1] - 400) <= 2
print(f"  toplevel hwnd correct: {ok_hwnd}  (expected origin 300,400)")
assert ok_hwnd, "overlay_hwnd is not the window actually being positioned"

# 2. _follow_window re-anchors by the window delta
base = (1000, 500, 1800, 1100)
ov.tracked_hwnd = ov.overlay_hwnd          # stand in for Explorer; any real window works
ov.last_layout = OverlayLayout(x=300, y=400, width=200, height=100, rows=[], window_rect=base)

# Simulate Explorer having moved: pretend its baseline was 40,25 up-left of now
cur = win32gui.GetWindowRect(ov.tracked_hwnd)
dx, dy = 40, 25
ov.last_layout.window_rect = (cur[0] - dx, cur[1] - dy, cur[2] - dx, cur[3] - dy)
ov._follow_window()
ov.root.update()
moved = win32gui.GetWindowRect(ov.overlay_hwnd)
print(f"  after follow: {moved[:2]}  expected ~({300+dx},{400+dy})")
assert abs(moved[0] - (300 + dx)) <= 2 and abs(moved[1] - (400 + dy)) <= 2, "follow offset wrong"

# 3. A resize must hide rather than mis-anchor
ov.is_visible = True
ov.last_layout.window_rect = (cur[0], cur[1], cur[2] + 200, cur[3])   # different size
ov._follow_window()
print(f"  hidden on resize: {not ov.is_visible}")
assert not ov.is_visible, "resize should hide until a fresh layout"

ov.root.destroy()
print("\nFOLLOW-WINDOW TESTS PASSED")
