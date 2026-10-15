# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Windows-only Python desktop utility that fills in folder sizes in Windows File Explorer's
normally-blank "Size" column, plus two alternative UIs (a docked sidebar and a standalone
analyzer). Not a git repo; no package manifest — dependencies are installed ad hoc.

## Commands

```powershell
python main.py               # default: in-place Explorer overlay service (tray icon)
python main.py --companion   # docked sidebar next to Explorer
python main.py --full        # standalone multi-tab analyzer
python main.py "C:\Folder"   # any existing path arg implies --full, focused there
```

Tests (plain `unittest` / script, not pytest):

```powershell
python test_scanner.py                                  # full suite
python -m unittest test_scanner.TestSizeScanner.<name>  # single test
python test_inplace_service.py                          # opens a real Explorer window, runs the service ~3.5s
python test_overlay_follow.py                           # overlay geometry: hwnd, move-follow, resize-hide
python test_overlay_updates.py                          # repaint coalescing, stale-scan guard, style backoff
python test_settings_hotkey.py                          # settings parsing, hotkey register/conflict
python test_column_style.py                             # Explorer padding/colour measurement (synthetic images)
```

Build (PyInstaller, onedir, windowed → `dist/SizeScanner/SizeScanner.exe`):

```powershell
python build_exe.py
```

`SizeScanner.spec` mirrors `build_exe.py`'s flags — change both together, or the spec-based
rebuild will drop hidden imports (`pystray`, `send2trash`, `psutil`, `comtypes`, win32*) and
the `collect_all` of `customtkinter` / `uiautomation`.

Third-party deps: `customtkinter`, `uiautomation`, `pywin32`, `pystray`, `Pillow`, `psutil`,
`send2trash`, `comtypes`, `pywin32`'s `win32com` (COM path detection).

## Architecture

Three UI front-ends over one scanning engine. `main.py` picks one by flag and imports it lazily,
so a front-end's heavy imports never load in the other modes.

- **`scanner.py`** — the engine, and the only UI-free module. `FolderScanner.scan_directory()`
  lists direct children immediately (files sized from `scandir` stat), then sizes each subfolder
  in a `ThreadPoolExecutor`, invoking `on_folder_scanned` per completion so UIs can fill rows
  progressively. Recursion is an explicit stack (no `os.walk`), skips symlinks/junctions to avoid
  cycles, and swallows per-entry permission errors. Pass `items=` to reuse a listing the caller
  already made, so navigating does not `scandir` the same folder twice.

  **Cancellation is per operation, never a shared flag.** `begin_operation()` returns a
  `CancelToken`; hold it and call `token.cancel()` to stop exactly that scan, and
  `end_operation(token)` when it finishes. `request_cancel()` cancels everything currently
  running (that is the "Stop Scan" button). A single `cancel_requested` flag could not work:
  every entry point reset it on start, so launching any scan silently un-cancelled every other
  one — a scan the user had navigated away from then ran to completion, reported
  `is_complete=True`, and overwrote the folder now on screen. Every UI callback therefore also
  checks that the result still belongs to the folder being displayed before publishing it.
  `reset_cancel()` is a no-op kept for compatibility. Never kill threads.

  Results are cached per folder path, keyed on that folder's mtime **and** a
  `CACHE_TTL_SECONDS` age — the folder's own mtime does not change when a file deep inside it
  grows, so mtime alone served a stale size forever. `clear_cache(path)` invalidates `path` and
  everything under it (matching on a path boundary, so clearing `C:\foo` leaves `C:\foobar`
  alone). Read entries through `get_cached()`; the bare `cache` dict is written by pool threads.
- **`explorer_tracker.py`** — resolves which Explorer window is active and what filesystem path
  it shows. Path detection is a fallback chain: Shell.Application COM → UIAutomation breadcrumb
  items (modern Win11 Explorer nests these under a `Microsoft.UI.Content.DesktopChildSiteBridge`)
  → window title matched against `KNOWN_USER_FOLDERS`. Breadcrumbs are display names, not paths,
  so `resolve_breadcrumbs_to_path` builds candidates and verifies each with `os.path.exists`.
  Returns `ExplorerInfo`, which carries the live `uiautomation` `CabinetWClass` control (`cab_ctrl`).
  Window *discovery* is deliberately pure Win32 (`list_explorer_hwnds` via `EnumWindows`, z-ordered,
  <1ms); the UIA/COM work runs for the single selected window only. Path resolution is memoized
  per `(hwnd, title)` in `_PATH_CACHE` — a title change proves navigation, so it invalidates at
  once. It does **not** prove the reverse: Explorer titles a window with the folder's basename, so
  `C:\proj1\src` → `C:\proj2\src` leaves the title identical and the stale path would be served
  forever. Entries therefore also expire after `_PATH_CACHE_TTL`, and the COM lookup matches
  shell windows by `HWND` rather than by title (two windows showing same-named folders used to
  resolve to each other's path).
- **`explorer_overlay.py`** — the default mode. A borderless topmost Tk window with
  `-transparentcolor` plus `WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW` so all input
  passes through to Explorer. Each tick it locates the HeaderControl's "Size" column and the
  ListControl via UIAutomation, repositions itself over the column, and draws right-aligned
  Segoe UI 9pt text at each visible row's y-center. Colors follow `AppsUseLightTheme`.
  Threading rule: the UIAutomation tracker runs in its own thread wrapped in
  `auto.UIAutomationInitializerInThread`, and **nothing off the Tk thread may call Tk** — not even
  `root.after()`, which is itself unsafe from a foreign thread. The tracker, scan and hotkey
  threads call `post_layout()` / `post_hide()` / `post_call()`; the watchdog (already on the Tk
  thread) runs `drain_ui()` every tick. Repaints are a *flag*, not a queued call, so a scan
  publishing 500 subfolders one at a time collapses into one redraw per tick instead of 500 full
  canvas rebuilds. Redraws are gated by a `state_sig` string (geometry + a hash of every visible
  row + `sizes_version`); setting `last_items_state = ""` forces a redraw after a scan.
  Hides itself whenever the Size column isn't present (i.e. not Details view) or Explorer is
  not focused — it is topmost, so painting while another app is in front would draw the sizes
  over that app.

  **Latency budget.** UIAutomation calls are cross-process COM and cost 10–400ms each; the
  original tick was ~570ms, which is what made the overlay visibly lag behind alt-tab. Two rules
  keep it responsive, and new code must not violate them:
  1. *Never put a visibility decision behind UIA work.* `focus_watchdog()` is a bare
     `GetForegroundWindow` (~0ms) run every 40ms on the Tk thread, so hiding is immediate and
     independent of the tracker's pace.
  2. *Cache anything found by tree search.* `ListControl`/`HeaderControl` at `searchDepth=15`
     cost ~170ms each, so `_get_view_controls` caches them per hwnd, revalidates with a cheap
     `BoundingRectangle` probe, and is invalidated on navigation. Steady-state tick is ~30ms.
  3. *Nothing slow happens on the Tk thread.* Copying Explorer's style is a `PrintWindow`
     capture costing ~28ms, so it runs on the tracker thread inside `compute_layout`, never
     during a redraw — and it is bounded by `MAX_STYLE_ATTEMPTS` / `STYLE_RETRY_SECONDS`,
     because a folder holding only subfolders has no drawn size to copy, so the measurement
     fails every time and used to be retried on every single frame.

  `auto.SetGlobalSearchTimeout(0.5)` is set at import — the library's 10s default turns a missing
  control into a 10-second stall instead of a hidden overlay.

  **Coordinates are screen coordinates, and Explorer moves.** Each `BoundingRectangle` is a
  separate cross-process call, so a window dragged mid-capture yields a header rect and row rects
  from *different* window positions — the sizes then paint between rows. `compute_layout` brackets
  its reads with `GetWindowRect` and returns `SKIP_UPDATE` (keep current position; distinct from
  `None`, which means hide) when the window shifted underneath it. `_follow_window()` re-anchors
  the overlay by the window's delta on every 40ms watchdog tick using only Win32, so a drag is
  tracked without a UIA pass; a *resize* reflows the columns and hides the overlay until a fresh
  layout arrives. `test_overlay_follow.py` covers all three behaviors.

  **Time-to-first-number.** Sizes are published two ways so navigation feels instant: `size_cache`
  (an LRU of path → `{name: formatted size}`) paints a revisited folder synchronously before any
  scan starts, and `on_folder_scanned` publishes each subfolder of a new folder as it completes
  rather than at the end. Both call `_request_repaint()`, which pushes a redraw immediately
  instead of waiting for the tracker's next pass. Any code that changes `folder_sizes` must bump
  `sizes_version` under `scan_lock` — the redraw gate keys on it, so an update that leaves the
  entry count unchanged would otherwise be skipped.
- **`companion_window.py`** / **`ui_main.py`** — CustomTkinter front-ends. Both use the same
  pattern: scans run on background threads and push closures onto a `queue.Queue` drained by a
  polling `after()` loop (`dispatch_ui` / `_poll_ui_queue`). The companion additionally runs its
  own tracker loop and docks itself to the tracked window's rect (via `MonitorFromRect`, so a
  second monitor docks correctly — `GetSystemMetrics` reports the primary display only).

  Both keep a `row_by_path` map from item path to Treeview row id and a `total_size_so_far`
  running total. Finding a row by walking `get_children()` and reading each row's tags back
  through Tcl cost ~1.1ms per completed folder at 800 rows and was quadratic in the folder
  count; re-summing every item per completion made it worse.

  Launch another mode through `shell_integration.launch_mode()`. Spelling it as
  `Popen([sys.executable, "companion_window.py"])` breaks in the frozen build, where
  `sys.executable` is `SizeScanner.exe` and no `.py` exists to hand it.
- **`column_style.py`** — makes the overlay text indistinguishable from Explorer's. Padding and
  colour are *measured*, not hardcoded: `PrintWindow(PW_RENDERFULLCONTENT)` captures the window
  (works even when occluded — a screen grab would capture whatever covers it), then a file row's
  Size cell is analysed for background, ink pixels and right margin. Guards reject anything that
  isn't plain text (a selected row's highlight becomes the dominant colour and otherwise measures
  as padding 0); several rows vote. Font size comes from `SPI_GETNONCLIENTMETRICS`'
  `lfMessageFont` at the window's DPI — deriving it from glyph pixels fails because a comma or
  descender inflates the bounding box. Measured on Win11 dark: **8px padding, `#dedede`,
  Segoe UI 12px**; those are the fallbacks.
- **`settings.py` / `global_hotkey.py`** — JSON settings at `%APPDATA%\SizeScanner\settings.json`
  (hotkey, start state, style overrides; a malformed file falls back to defaults rather than
  blocking startup). Each known key is type-checked against `_VALIDATORS` on load — a
  hand-edited `"hotkey": 123` would otherwise reach `parse_hotkey()` and fail on `.strip()` —
  while unknown keys are preserved. `save()` writes through a temp file and `os.replace`, so an
  interrupted write cannot truncate the file. `RegisterHotKey` delivers `WM_HOTKEY` to the *registering thread's* queue, so
  registration and the message loop share one dedicated thread. Note pywin32's `RegisterHotKey`
  returns `None` on success and raises on failure — don't test its return value as a boolean.
- **`shell_integration.py`** — Win32/shell helpers: `SHGetFileInfoW` type names, drive stats via
  psutil, Recycle Bin deletes via send2trash, and HKCU context-menu registration under
  `Directory`, `Directory\Background`, and `Drive`. `get_executable_command()` resolves the
  launch command differently when frozen (`sys.frozen`) vs. running from source — keep that
  branch correct if entry points move.

## Conventions

- Windows-only by design: `winreg`, `ctypes.windll`, `win32*`, UIAutomation are used
  unconditionally. There is no cross-platform path.
- Filesystem traversal code catches `PermissionError/FileNotFoundError/OSError` per entry and
  continues; a single unreadable directory must never abort a scan.
- `format_size` is the single source of truth for size strings across all three UIs.
- Long-running work never blocks the Tk main loop — spawn a daemon thread and marshal results
  back through the UI queue. Only the Tk thread may touch Tk, `after()` included.
- A background result must prove it is still wanted before it touches the UI: compare
  `res.current_path` (or the path captured when the worker started) against the folder currently
  displayed. Stale results overwriting the current folder was the most user-visible bug here.
