"""
test_overlay_updates.py - How the overlay takes updates from its worker threads.

Covers the three things that went wrong invisibly: a burst of per-subfolder size
updates turning into a burst of full redraws, a scan of a folder the user has left
overwriting the folder they are now in, and the style measurement (a ~28ms window
capture) being retried on every single redraw when it cannot succeed.
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")

import os
import time
import shutil
import tempfile
import unittest
from unittest import mock

import explorer_overlay
from explorer_overlay import ExplorerSizeOverlay, OverlayLayout
from scanner import FolderScanner


class OverlayTestCase(unittest.TestCase):
    def setUp(self):
        self.overlay = ExplorerSizeOverlay(FolderScanner())
        self.overlay.last_layout = OverlayLayout(
            x=0, y=0, width=120, height=400,
            rows=[("Subfolder_A", 10)], window_rect=(0, 0, 800, 600)
        )
        self.overlay.is_visible = True
        self.applied = []
        self.overlay.apply_layout = lambda layout: self.applied.append(layout)

    def tearDown(self):
        try:
            self.overlay.root.destroy()
        except Exception:
            pass


class TestRepaintCoalescing(OverlayTestCase):
    def test_burst_of_updates_costs_one_redraw(self):
        """500 subfolders finishing used to mean 500 canvas rebuilds."""
        for _ in range(500):
            self.overlay._request_repaint()
        self.assertEqual(self.applied, [], "nothing may be drawn before the Tk thread runs")
        self.overlay.drain_ui()
        self.assertEqual(len(self.applied), 1)

    def test_hide_supersedes_a_queued_repaint(self):
        self.overlay._request_repaint()
        self.overlay.post_hide()
        self.overlay.drain_ui()
        self.assertEqual(self.applied, [], "a queued repaint must not resurrect a hidden overlay")

    def test_latest_layout_wins(self):
        first = self.overlay.last_layout
        second = OverlayLayout(x=5, y=5, width=120, height=400, rows=[], window_rect=(0, 0, 800, 600))
        self.overlay.post_layout(first)
        self.overlay.post_layout(second)
        self.overlay.drain_ui()
        self.assertEqual(len(self.applied), 1)
        self.assertIs(self.applied[0], second)

    def test_post_call_runs_on_drain(self):
        ran = []
        self.overlay.post_call(ran.append, "called")
        self.assertEqual(ran, [], "queued work must not run on the calling thread")
        self.overlay.drain_ui()
        self.assertEqual(ran, ["called"])


class TestStaleScanGuard(OverlayTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="sizescanner_overlay_")
        os.makedirs(os.path.join(self.tmp, "Subfolder_A"))
        with open(os.path.join(self.tmp, "Subfolder_A", "f.bin"), "wb") as fh:
            fh.write(b"0" * 2048)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    def test_superseded_scan_does_not_publish(self):
        """A worker finishing the folder we already left must leave the current
        folder's sizes alone."""
        self.overlay.current_path = r"C:\somewhere\else"
        self.overlay.folder_sizes = {"Current": "1.00 MB"}
        self.overlay.sizes_version = 7

        token = self.overlay.scanner.begin_operation()
        self.overlay._scan_worker(self.tmp, token)   # scan of the *old* folder

        self.assertEqual(self.overlay.folder_sizes, {"Current": "1.00 MB"})
        self.assertEqual(self.overlay.sizes_version, 7)
        # It may still fill the revisit cache -- those results are correct for *that*
        # folder, just not for the one on screen.
        self.assertIn(self.tmp, self.overlay.size_cache)

    def test_current_scan_does_publish(self):
        self.overlay.current_path = self.tmp
        self.overlay.folder_sizes = {}
        token = self.overlay.scanner.begin_operation()
        self.overlay._scan_worker(self.tmp, token)
        self.assertEqual(self.overlay.folder_sizes.get("Subfolder_A"), "2.00 KB")

    def test_navigating_cancels_the_previous_scan(self):
        first = self.overlay.scanner.begin_operation()
        self.overlay._scan_token = first
        self.overlay.current_path = r"C:\old"
        self.overlay.is_scanning = True

        self.overlay.update_sizes_for_path(self.tmp)
        self.assertTrue(first.cancelled, "the outgoing folder's scan must be cancelled")
        self.assertIsNot(self.overlay._scan_token, first)
        time.sleep(0.3)   # let the worker finish before the temp dir is removed


class TestStyleMeasurementBackoff(OverlayTestCase):
    def test_attempts_are_bounded(self):
        """A folder of nothing but subfolders has no drawn size to copy, so the
        measurement always fails -- and used to be retried on every redraw at ~28ms
        a time."""
        with mock.patch.object(explorer_overlay.column_style, "measure_from_rows",
                               return_value=None) as measure:
            for _ in range(50):
                if self.overlay._want_style_measurement():
                    self.overlay._measure_style(0, [(0, 0, 10, 10)])
                # Pretend the retry delay elapsed, so only the attempt cap applies
                self.overlay._style_next_try = 0.0
            self.assertEqual(measure.call_count, explorer_overlay.MAX_STYLE_ATTEMPTS)

    def test_retry_is_rate_limited(self):
        self.overlay._style_attempts = 0
        self.overlay._style_next_try = time.monotonic() + 60
        self.assertFalse(self.overlay._want_style_measurement())

    def test_measured_style_stops_further_attempts(self):
        self.overlay.style.measured = True
        self.assertFalse(self.overlay._want_style_measurement())

    def test_successful_measurement_is_adopted(self):
        import column_style
        sampled = column_style.ColumnStyle(right_padding=11, text_color="#abcdef", measured=True)
        with mock.patch.object(explorer_overlay.column_style, "measure_from_rows",
                               return_value=sampled):
            self.overlay._measure_style(0, [(0, 0, 10, 10)])
        self.assertTrue(self.overlay.style.measured)
        self.assertEqual(self.overlay.style.right_padding, 11)
        self.assertFalse(self.overlay._want_style_measurement())

    def test_settings_override_beats_the_measurement(self):
        import column_style
        self.overlay.config["right_padding"] = 3
        sampled = column_style.ColumnStyle(right_padding=11, text_color="#abcdef", measured=True)
        with mock.patch.object(explorer_overlay.column_style, "measure_from_rows",
                               return_value=sampled):
            self.overlay._measure_style(0, [(0, 0, 10, 10)])
        self.assertEqual(self.overlay.style.right_padding, 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
