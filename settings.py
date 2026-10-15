"""
settings.py - User settings for SizeScanner, stored as editable JSON.

Lives at %APPDATA%\\SizeScanner\\settings.json so it survives reinstalls and can be
edited by hand (the tray menu opens it). Unknown keys are preserved on save, and a
malformed file falls back to defaults rather than preventing startup.
"""

import os
import json
import copy
from typing import Any, Dict, Optional, Tuple

import win32con

DEFAULTS: Dict[str, Any] = {
    # Global shortcut that toggles the in-Explorer sizes on and off.
    # Format: modifiers joined by "+", then one key, e.g. "ctrl+alt+s",
    # "ctrl+shift+f9", "alt+oem_3". Set to "" to disable the shortcut.
    "hotkey": "ctrl+alt+s",
    # Whether sizes are shown when the app starts.
    "start_enabled": True,
    # Pixels between the size text and the right edge of the Size column.
    # null = measure it from Explorer's own file rows (recommended).
    "right_padding": None,
    # Text colours as "#rrggbb". null = sample them from Explorer.
    "text_color_dark": None,
    "text_color_light": None,
}

MODIFIER_FLAGS = {
    "ctrl": win32con.MOD_CONTROL,
    "control": win32con.MOD_CONTROL,
    "alt": win32con.MOD_ALT,
    "shift": win32con.MOD_SHIFT,
    "win": win32con.MOD_WIN,
    "super": win32con.MOD_WIN,
}

# Keys that aren't a single character
NAMED_KEYS = {
    "space": win32con.VK_SPACE, "tab": win32con.VK_TAB, "enter": win32con.VK_RETURN,
    "return": win32con.VK_RETURN, "esc": win32con.VK_ESCAPE, "escape": win32con.VK_ESCAPE,
    "backspace": win32con.VK_BACK, "insert": win32con.VK_INSERT, "delete": win32con.VK_DELETE,
    "home": win32con.VK_HOME, "end": win32con.VK_END,
    "pgup": win32con.VK_PRIOR, "pageup": win32con.VK_PRIOR,
    "pgdn": win32con.VK_NEXT, "pagedown": win32con.VK_NEXT,
    "left": win32con.VK_LEFT, "right": win32con.VK_RIGHT,
    "up": win32con.VK_UP, "down": win32con.VK_DOWN,
    "oem_3": 0xC0, "backtick": 0xC0, "tilde": 0xC0,
}
for _i in range(1, 25):
    NAMED_KEYS[f"f{_i}"] = win32con.VK_F1 + _i - 1


def settings_path() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "SizeScanner", "settings.json")


# What each known key is allowed to be. A value of the wrong type is dropped in
# favour of the default rather than being handed to code that assumes otherwise --
# "hotkey": 123 used to reach parse_hotkey() and blow up on .strip().
_VALIDATORS = {
    "hotkey": lambda v: isinstance(v, str),
    "start_enabled": lambda v: isinstance(v, bool),
    "right_padding": lambda v: v is None or (isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 200),
    "text_color_dark": lambda v: v is None or _is_hex_colour(v),
    "text_color_light": lambda v: v is None or _is_hex_colour(v),
}


def _is_hex_colour(value: Any) -> bool:
    return (isinstance(value, str) and len(value) == 7 and value.startswith("#")
            and all(c in "0123456789abcdefABCDEF" for c in value[1:]))


def load() -> Dict[str, Any]:
    """Returns settings, with defaults filled in for anything missing or invalid."""
    values = copy.deepcopy(DEFAULTS)
    try:
        with open(settings_path(), "r", encoding="utf-8") as fh:
            stored = json.load(fh)
        if isinstance(stored, dict):
            for key, value in stored.items():
                check = _VALIDATORS.get(key)
                # Unknown keys are kept untouched so hand-added notes survive a save
                if check is None or check(value):
                    values[key] = value
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        # A hand-edited file with a typo must not stop the app from starting
        pass
    return values


def save(values: Dict[str, Any]) -> bool:
    """Writes the settings file atomically, so an interrupted write cannot leave a
    truncated file that the next start would have to fall back out of."""
    path = settings_path()
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(values, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return True
    except (OSError, TypeError, ValueError):
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def ensure_file() -> str:
    """Writes the settings file if absent, so there is always something to open
    and edit. Returns its path."""
    path = settings_path()
    if not os.path.exists(path):
        save(load())
    return path


def parse_hotkey(spec: str) -> Optional[Tuple[int, int]]:
    """
    Turns "ctrl+alt+s" into (modifier flags, virtual key code) for RegisterHotKey.
    Returns None if the string is empty or cannot be understood.
    """
    if not spec or not spec.strip():
        return None

    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    if not parts:
        return None

    mods = 0
    key = None
    for part in parts:
        if part in MODIFIER_FLAGS:
            mods |= MODIFIER_FLAGS[part]
        elif key is None:
            key = part
        else:
            return None  # more than one non-modifier key

    if key is None:
        return None

    if key in NAMED_KEYS:
        vk = NAMED_KEYS[key]
    elif len(key) == 1 and (key.isalnum()):
        vk = ord(key.upper())
    else:
        return None

    if not mods:
        return None  # refuse to swallow a bare key system-wide

    # MOD_NOREPEAT: holding the combo fires once, not continuously
    return mods | 0x4000, vk


def describe_hotkey(spec: str) -> str:
    """Human-readable form of a hotkey spec, e.g. 'Ctrl+Alt+S'."""
    if not spec:
        return "none"
    return "+".join(p.strip().capitalize() for p in spec.split("+") if p.strip())
