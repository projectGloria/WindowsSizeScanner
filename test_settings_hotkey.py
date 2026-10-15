"""
test_settings_hotkey.py - Tests for the settings file, hotkey parsing/registration,
and the Explorer column-style measurement.
"""

import sys
import time
import threading
import unittest

sys.stdout.reconfigure(encoding="utf-8")

import win32con
import settings
from global_hotkey import GlobalHotkey


class TestHotkeyParsing(unittest.TestCase):
    def test_basic_combo(self):
        mods, vk = settings.parse_hotkey("ctrl+alt+s")
        self.assertTrue(mods & win32con.MOD_CONTROL)
        self.assertTrue(mods & win32con.MOD_ALT)
        self.assertFalse(mods & win32con.MOD_SHIFT)
        self.assertEqual(vk, ord("S"))

    def test_case_and_spacing_ignored(self):
        self.assertEqual(settings.parse_hotkey("CTRL + Shift + F9"),
                         settings.parse_hotkey("ctrl+shift+f9"))

    def test_function_and_named_keys(self):
        self.assertEqual(settings.parse_hotkey("ctrl+alt+f12")[1], win32con.VK_F12)
        self.assertEqual(settings.parse_hotkey("ctrl+alt+space")[1], win32con.VK_SPACE)
        self.assertEqual(settings.parse_hotkey("alt+home")[1], win32con.VK_HOME)

    def test_no_repeat_flag_set(self):
        mods, _ = settings.parse_hotkey("ctrl+alt+s")
        self.assertTrue(mods & 0x4000, "MOD_NOREPEAT should be set")

    def test_rejects_bad_specs(self):
        for bad in ["", "   ", "s", "ctrl", "ctrl+", "ctrl+alt+notakey", "ctrl+a+b"]:
            self.assertIsNone(settings.parse_hotkey(bad), f"should reject {bad!r}")

    def test_describe(self):
        self.assertEqual(settings.describe_hotkey("ctrl+alt+s"), "Ctrl+Alt+S")
        self.assertEqual(settings.describe_hotkey(""), "none")


class TestSettingsFile(unittest.TestCase):
    def test_defaults_present(self):
        cfg = settings.load()
        for key in settings.DEFAULTS:
            self.assertIn(key, cfg)

    def test_malformed_file_falls_back(self):
        import json, os, tempfile
        original = settings.settings_path
        tmp = os.path.join(tempfile.mkdtemp(), "settings.json")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("{ this is not valid json ")
        settings.settings_path = lambda: tmp
        try:
            cfg = settings.load()          # must not raise
            self.assertEqual(cfg["hotkey"], settings.DEFAULTS["hotkey"])
        finally:
            settings.settings_path = original


class TestGlobalHotkey(unittest.TestCase):
    def test_registers_and_releases(self):
        hk = GlobalHotkey("ctrl+alt+f24", lambda: None)
        self.assertTrue(hk.start(), f"registration failed: {hk.error}")
        try:
            # A second listener on the same combo must fail, not crash
            clash = GlobalHotkey("ctrl+alt+f24", lambda: None)
            self.assertFalse(clash.start())
            self.assertIn("already taken", clash.error or "")
        finally:
            hk.stop()
            time.sleep(0.3)
        self.assertFalse(hk.registered)

    def test_invalid_spec_reports_error(self):
        hk = GlobalHotkey("not+a+key", lambda: None)
        self.assertFalse(hk.start())
        self.assertIn("not a valid", hk.error or "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
