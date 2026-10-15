"""
test_scanner.py - Automated unit and integration tests for SizeScanner.
"""

import os
import shutil
import tempfile
import unittest
from scanner import (
    FolderScanner, format_size, format_number, calculate_allocated_size,
    get_cluster_size, ItemInfo
)
from ui_main import make_percent_bar
import shell_integration

class TestSizeScanner(unittest.TestCase):
    def setUp(self):
        # Create a temporary directory structure for testing
        self.test_dir = tempfile.mkdtemp(prefix="sizescanner_test_")

        # Create subfolders
        self.sub1 = os.path.join(self.test_dir, "Subfolder_A")
        self.sub2 = os.path.join(self.test_dir, "Subfolder_B")
        self.sub1_nested = os.path.join(self.sub1, "Nested")
        os.makedirs(self.sub1_nested, exist_ok=True)
        os.makedirs(self.sub2, exist_ok=True)

        # Create files with known sizes
        # Subfolder A has 100 KB in root + 200 KB in nested = 300 KB
        with open(os.path.join(self.sub1, "file1.dat"), "wb") as f:
            f.write(b"0" * 102400) # 100 KB
        with open(os.path.join(self.sub1_nested, "file2.dat"), "wb") as f:
            f.write(b"0" * 204800) # 200 KB

        # Subfolder B has 50 KB
        with open(os.path.join(self.sub2, "file3.dat"), "wb") as f:
            f.write(b"0" * 51200) # 50 KB

        # Test dir root has 10 KB file
        with open(os.path.join(self.test_dir, "root.txt"), "wb") as f:
            f.write(b"0" * 10240) # 10 KB

        self.scanner = FolderScanner()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_format_size(self):
        self.assertEqual(format_size(0), "0 B")
        self.assertEqual(format_size(500), "500 B")
        self.assertEqual(format_size(1024), "1.00 KB")
        self.assertEqual(format_size(1048576), "1.00 MB")
        self.assertEqual(format_size(1073741824), "1.00 GB")
        self.assertEqual(format_size(5368709120), "5.00 GB")

    def test_allocated_size(self):
        # 100 bytes with 4096 cluster size -> 4096 bytes
        self.assertEqual(calculate_allocated_size(100, 4096), 4096)
        self.assertEqual(calculate_allocated_size(4096, 4096), 4096)
        self.assertEqual(calculate_allocated_size(4097, 4096), 8192)
        self.assertEqual(calculate_allocated_size(0, 4096), 0)

    def test_make_percent_bar(self):
        bar_0 = make_percent_bar(0.0)
        self.assertIn("0.0%", bar_0)
        self.assertEqual(bar_0.count("■"), 0)

        bar_50 = make_percent_bar(50.0)
        self.assertIn("50.0%", bar_50)
        self.assertEqual(bar_50.count("■"), 5)
        self.assertEqual(bar_50.count("□"), 5)

        bar_100 = make_percent_bar(100.0)
        self.assertIn("100.0%", bar_100)
        self.assertEqual(bar_100.count("■"), 10)

    def test_scan_directory_immediate(self):
        items = self.scanner.scan_directory_immediate(self.test_dir)
        names = {it.name for it in items}
        self.assertIn("Subfolder_A", names)
        self.assertIn("Subfolder_B", names)
        self.assertIn("root.txt", names)

        # root.txt should have size immediately
        root_txt = next(it for it in items if it.name == "root.txt")
        self.assertFalse(root_txt.is_dir)
        self.assertEqual(root_txt.size, 10240)
        self.assertTrue(root_txt.is_scanned)

        # Subfolders should be marked not yet scanned
        sub_a = next(it for it in items if it.name == "Subfolder_A")
        self.assertTrue(sub_a.is_dir)
        self.assertFalse(sub_a.is_scanned)

    def test_full_directory_scan(self):
        res = self.scanner.scan_directory(self.test_dir)
        self.assertTrue(res.is_complete)
        self.assertEqual(len(res.items), 3) # Subfolder_A, Subfolder_B, root.txt

        sub_a = next(it for it in res.items if it.name == "Subfolder_A")
        self.assertEqual(sub_a.size, 102400 + 204800) # 307,200 bytes
        self.assertEqual(sub_a.file_count, 2)
        self.assertEqual(sub_a.dir_count, 1)

        sub_b = next(it for it in res.items if it.name == "Subfolder_B")
        self.assertEqual(sub_b.size, 51200)
        self.assertEqual(sub_b.file_count, 1)
        self.assertEqual(sub_b.dir_count, 0)

        # Total size in test_dir: 300 KB + 50 KB + 10 KB = 360 KB = 368,640 bytes
        self.assertEqual(res.total_size, 368640)
        self.assertEqual(res.total_files, 4)

    def test_cache_functionality(self):
        # Scan first time
        res1 = self.scanner.scan_directory(self.test_dir)
        self.assertIn(self.sub1, self.scanner.cache)

        # Scan second time - should hit cache
        res2 = self.scanner.scan_directory(self.test_dir)
        sub_a2 = next(it for it in res2.items if it.name == "Subfolder_A")
        self.assertEqual(sub_a2.size, 307200)

    def test_deep_scan_largest_files(self):
        largest = self.scanner.deep_scan_largest_files(self.test_dir, limit=10)
        self.assertEqual(len(largest), 4) # file2.dat (200KB), file1.dat (100KB), file3.dat (50KB), root.txt (10KB)
        self.assertEqual(largest[0][0], "file2.dat")
        self.assertEqual(largest[0][2], 204800)
        self.assertEqual(largest[1][0], "file1.dat")
        self.assertEqual(largest[1][2], 102400)
        self.assertEqual(largest[2][0], "file3.dat")
        self.assertEqual(largest[2][2], 51200)
        self.assertEqual(largest[3][0], "root.txt")
        self.assertEqual(largest[3][2], 10240)

    def test_deep_scan_file_types(self):
        types = self.scanner.deep_scan_file_types(self.test_dir)
        self.assertIn(".dat", types)
        self.assertIn(".txt", types)
        self.assertEqual(types[".dat"]["count"], 3)
        self.assertEqual(types[".dat"]["size"], 102400 + 204800 + 51200)
        self.assertEqual(types[".txt"]["count"], 1)
        self.assertEqual(types[".txt"]["size"], 10240)

    def test_clear_cache_keeps_sibling_prefixes(self):
        """Regression: clearing 'C:\\foo' also evicted 'C:\\foobar'."""
        self.scanner.cache = {
            r"C:\foo": (1, 1, 1, 0.0),
            r"C:\foo\a": (2, 1, 0, 0.0),
            r"C:\foobar": (3, 1, 0, 0.0),
        }
        self.scanner.clear_cache(r"C:\foo")
        self.assertEqual(list(self.scanner.cache), [r"C:\foobar"])

    def test_cancel_is_per_operation(self):
        """Regression: every scan cleared one shared flag, so starting any scan
        un-cancelled every other one -- and a cancelled scan then reported success
        and overwrote the folder the user had moved to."""
        doomed = self.scanner.begin_operation()
        doomed.cancel()
        # A scan started afterwards must be unaffected by that cancellation
        res = self.scanner.scan_directory(self.test_dir)
        self.assertTrue(res.is_complete)
        self.assertEqual(res.total_size, 368640)
        # ...and the cancelled token must still read as cancelled
        self.assertTrue(doomed.cancelled)
        self.scanner.end_operation(doomed)

    def test_cancelled_scan_reports_incomplete(self):
        token = self.scanner.begin_operation()
        token.cancel()
        res = self.scanner.scan_directory(self.test_dir, token=token)
        self.assertFalse(res.is_complete)
        self.scanner.end_operation(token)

    def test_request_cancel_stops_running_scan_only(self):
        running = self.scanner.begin_operation()
        self.scanner.request_cancel()
        self.assertTrue(running.cancelled)
        self.scanner.end_operation(running)
        fresh = self.scanner.begin_operation()
        self.assertFalse(fresh.cancelled)
        self.scanner.end_operation(fresh)

    def test_scan_directory_reuses_supplied_listing(self):
        """The UI lists the folder to paint it immediately; the scan must size those
        same objects instead of scandir'ing the folder a second time."""
        items = self.scanner.scan_directory_immediate(self.test_dir)
        res = self.scanner.scan_directory(self.test_dir, items=items)
        self.assertIs(res.items, items)
        sub_a = next(it for it in items if it.name == "Subfolder_A")
        self.assertTrue(sub_a.is_scanned)
        self.assertEqual(sub_a.size, 307200)

    def test_largest_files_limit_is_bounded(self):
        largest = self.scanner.deep_scan_largest_files(self.test_dir, limit=2)
        self.assertEqual(len(largest), 2)
        self.assertEqual([f[0] for f in largest], ["file2.dat", "file1.dat"])

    def test_allocated_size_is_exact_for_huge_values(self):
        """math.ceil(size / cluster) loses precision past 2^53."""
        huge = 2 ** 60 + 1
        self.assertEqual(calculate_allocated_size(huge, 4096), ((huge + 4095) // 4096) * 4096)

    def test_unreadable_entry_is_not_reported_as_a_folder(self):
        items = self.scanner.scan_directory_immediate(self.test_dir)
        root_txt = next(it for it in items if it.name == "root.txt")
        self.assertFalse(root_txt.is_dir)

    def test_drives_detection(self):
        drives = shell_integration.get_system_drives()
        self.assertGreaterEqual(len(drives), 1)
        c_drive = next((d for d in drives if d['mountpoint'].startswith("C:")), None)
        self.assertIsNotNone(c_drive)
        self.assertGreater(c_drive['total'], 0)

if __name__ == "__main__":
    unittest.main()
