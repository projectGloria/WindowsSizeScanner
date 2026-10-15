# ⚡ SizeScanner — In-Place Windows File Explorer Folder Sizes

**SizeScanner** is a Windows utility that **renders folder sizes directly inside Windows File Explorer's native "Size" column**, completely filling in the blank folder sizes right next to single items.

---

## 🎯 How It Works Directly Inside Windows File Explorer

In standard Windows File Explorer Details view, files have sizes (e.g. `7 KB`, `2 KB`), but folder sizes are left blank.

**SizeScanner solves this directly in Explorer:**
1. It detects your active Windows File Explorer window and the folder currently opened.
2. It calculates the recursive sizes of all subfolders inside.
3. Using a transparent, click-through overlay (`WS_EX_LAYERED | WS_EX_TRANSPARENT`), it **draws the exact folder sizes right inside the blank space of Windows File Explorer's Size column**!
4. **100% Click-Through**: You can click, double-click to enter folders, right-click, scroll, select items, and drag files normally. All mouse and keyboard actions pass directly to Windows Explorer.
5. **Adaptive Theme**: Automatically detects Windows Dark Mode and Light Mode (`AppsUseLightTheme`) to match Explorer's font (Segoe UI 9pt) and colors.
6. **Dynamic Tracking**: As you scroll through Explorer, resize columns, or navigate into different folders, the numbers update dynamically right in place.

---

## 🚀 How to Run SizeScanner

1. **Desktop Shortcut**:
   - Double-click the **SizeScanner** shortcut on your Desktop (`C:\Users\Nihil\Desktop\SizeScanner.lnk`).
2. **Direct Executable**:
   - Double-click `dist\SizeScanner\SizeScanner.exe`.
3. **One-Click Batch File**:
   - Double-click `Run-SizeScanner.bat`.
4. **Command Line Options**:
   - `python main.py` or `.\dist\SizeScanner\SizeScanner.exe`: Runs the **in-place Explorer overlay service** (sits in the system tray and fills in folder sizes in File Explorer).
   - `python main.py --companion`: Runs the docked companion sidebar.
   - `python main.py --full`: Opens the standalone multi-tab disk analyzer.

---

## ⌨️ Toggle Shortcut

Press **Ctrl+Alt+S** anywhere to turn the in-Explorer sizes on or off. While off, SizeScanner
does no scanning or tracking at all — it just sits in the tray until you toggle it back on.
The state is remembered between runs.

The shortcut is editable. Open the tray menu → **Edit Settings…**, or edit directly:

```
%APPDATA%\SizeScanner\settings.json
```

```json
{
  "hotkey": "ctrl+alt+s",
  "start_enabled": true
}
```

Write it as modifiers plus one key — `"ctrl+shift+f9"`, `"alt+home"`, `"ctrl+alt+z"`. At least one
modifier is required. Set `"hotkey": ""` to disable the shortcut and use the tray menu only.
Restart SizeScanner to pick up a change. If the combination is already taken by another
application, SizeScanner says so on startup and keeps running without it.

## 🎨 Matching Explorer's Look

The sizes are drawn to match Explorer's own Size column rather than using fixed values:
SizeScanner captures the window, measures a row Explorer already drew a file size into, and
copies its **right padding** and **text colour**; the **font** comes from the Windows UI font
metrics at that window's DPI. This adapts to dark/light themes automatically.

To override any of it, set these in `settings.json` (`null` means "measure it"):

```json
{
  "right_padding": 8,
  "text_color_dark": "#dedede",
  "text_color_light": "#1a1a1a"
}
```

## 🛎️ System Tray Menu

When SizeScanner is running, a blue icon appears in your Windows taskbar system tray:
- **Show sizes in Explorer**: Toggles the overlay (same as the shortcut). Shows a checkmark when
  active; left-clicking the tray icon toggles it too.
- **Open Companion Sidebar**: Opens the docking sidebar beside Explorer.
- **Open Full Analyzer App**: Opens the full multi-tab disk analyzer.
- **Edit Settings…**: Opens `settings.json`.
- **Exit**: Closes the service.

---

## 🧪 Test Suite

Run the tests at any time:
```powershell
python test_scanner.py            # scanner engine
python test_inplace_service.py    # live overlay against a real Explorer window
python test_overlay_follow.py     # overlay geometry: window move / resize
python test_settings_hotkey.py    # settings file and global shortcut
python test_column_style.py       # Explorer colour/padding measurement
```
