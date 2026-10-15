"""
global_hotkey.py - System-wide shortcut registration.

RegisterHotKey delivers WM_HOTKEY to the message queue of the thread that registered
it, so registration and the message loop must live on the same dedicated thread.
"""

import threading
from typing import Callable, Optional

import win32con
import win32gui
import win32api

import settings

HOTKEY_ID = 0xA51


class GlobalHotkey:
    """
    Registers one system-wide shortcut and calls `on_pressed` when it fires.

    The callback runs on the hotkey thread, so anything touching the UI must
    marshal itself back to the main thread.
    """

    def __init__(self, spec: str, on_pressed: Callable[[], None]):
        self.spec = spec
        self.on_pressed = on_pressed
        self.error: Optional[str] = None
        self.registered = False
        self._thread: Optional[threading.Thread] = None
        self._thread_id: Optional[int] = None
        self._ready = threading.Event()

    def start(self) -> bool:
        """Starts listening. Returns whether the shortcut was registered; on failure
        `error` explains why (most often another app already owns the combo)."""
        parsed = settings.parse_hotkey(self.spec)
        if parsed is None:
            self.error = f"'{self.spec}' is not a valid shortcut"
            return False

        self._thread = threading.Thread(target=self._run, args=parsed, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=2.0)
        return self.registered

    def _run(self, mods: int, vk: int):
        try:
            self._thread_id = win32api.GetCurrentThreadId()
            # pywin32 signals failure by raising; a successful call returns None.
            win32gui.RegisterHotKey(None, HOTKEY_ID, mods, vk)
            self.registered = True
        except Exception as exc:
            self.error = f"'{self.spec}' is already taken by another application ({exc})"
            self._ready.set()
            return
        finally:
            self._ready.set()

        try:
            while True:
                msg = win32gui.GetMessage(None, 0, 0)
                # GetMessage returns (result, msg-tuple); result 0 means WM_QUIT
                if not msg or msg[0] == 0:
                    break
                if msg[1][1] == win32con.WM_HOTKEY:
                    try:
                        self.on_pressed()
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            try:
                win32gui.UnregisterHotKey(None, HOTKEY_ID)
            except Exception:
                pass
            self.registered = False

    def stop(self):
        if self._thread_id:
            try:
                win32api.PostThreadMessage(self._thread_id, win32con.WM_QUIT, 0, 0)
            except Exception:
                pass
