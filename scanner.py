"""
scanner.py - High performance directory size scanning engine for Windows.
Provides fast multi-threaded folder size calculation, caching, and progress reporting.
"""

import os
import time
import math
import heapq
import ctypes
import threading
from ctypes import wintypes
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple, Callable, Any, Set
from dataclasses import dataclass, field

# Windows cluster size cache per drive letter (e.g., 'C:\\': 4096)
_CLUSTER_SIZE_CACHE: Dict[str, int] = {}

def get_cluster_size(path: str) -> int:
    """Returns the cluster size (allocation unit) in bytes for the drive containing path."""
    try:
        drive = os.path.splitdrive(os.path.abspath(path))[0]
        if not drive:
            return 4096
        drive_root = drive + "\\"
        if drive_root in _CLUSTER_SIZE_CACHE:
            return _CLUSTER_SIZE_CACHE[drive_root]

        sectors_per_cluster = wintypes.DWORD()
        bytes_per_sector = wintypes.DWORD()
        free_clusters = wintypes.DWORD()
        total_clusters = wintypes.DWORD()

        res = ctypes.windll.kernel32.GetDiskFreeSpaceW(
            drive_root,
            ctypes.byref(sectors_per_cluster),
            ctypes.byref(bytes_per_sector),
            ctypes.byref(free_clusters),
            ctypes.byref(total_clusters)
        )
        if res:
            cluster_size = sectors_per_cluster.value * bytes_per_sector.value
            if cluster_size > 0:
                _CLUSTER_SIZE_CACHE[drive_root] = cluster_size
                return cluster_size
    except Exception:
        pass
    return 4096

def calculate_allocated_size(size: int, cluster_size: int = 4096) -> int:
    """Calculates size on disk based on filesystem cluster size."""
    if size <= 0:
        return 0
    if cluster_size <= 0:
        return size
    # Integer ceiling division: math.ceil(size / cluster) loses precision above 2^53
    return -(-size // cluster_size) * cluster_size

def format_size(bytes_val: int) -> str:
    """Formats bytes into human readable string (B, KB, MB, GB, TB)."""
    if bytes_val < 0:
        return "0 B"
    if bytes_val < 1024:
        return f"{bytes_val} B"
    units = ["KB", "MB", "GB", "TB", "PB"]
    val = float(bytes_val)
    for unit in units:
        val /= 1024.0
        if val < 1024.0 or unit == "PB":
            # Keep the string a consistent width: three significant digits
            return f"{val:.1f} {unit}" if val >= 100 else f"{val:.2f} {unit}"
    return f"{bytes_val} B"

def format_number(n: int) -> str:
    """Formats integer with thousands separators."""
    return f"{n:,}"


# A folder's own mtime does not change when a file three levels below it grows, so
# an mtime-only check can serve a stale size forever. Entries are re-walked once they
# are older than this; a revisit inside the window still paints instantly.
CACHE_TTL_SECONDS = 300.0


class CancelToken:
    """
    Cancellation for one scan, so cancelling a scan cannot stop an unrelated one.

    A single shared flag could not express "stop the folder scan I navigated away
    from, but keep the file-type analysis running" -- and worse, every operation
    used to clear that flag on entry, so starting any scan silently un-cancelled
    every other one.
    """
    __slots__ = ("_cancelled",)

    def __init__(self):
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled


@dataclass
class ItemInfo:
    path: str
    name: str
    is_dir: bool
    size: int = 0
    allocated_size: int = 0
    file_count: int = 0
    dir_count: int = 0
    mtime: float = 0.0
    type_desc: str = ""
    is_scanned: bool = False
    error: Optional[str] = None

@dataclass
class ScanResult:
    current_path: str
    items: List[ItemInfo] = field(default_factory=list)
    total_size: int = 0
    total_allocated_size: int = 0
    total_files: int = 0
    total_dirs: int = 0
    scan_duration: float = 0.0
    is_complete: bool = False

class FolderScanner:
    """
    Scans directories with multi-threading and caching.

    Every long operation takes (or creates) its own CancelToken; callers that need
    to stop just their own scan keep the token `begin_operation()` handed them.
    `request_cancel()` stops everything currently running, which is what the global
    "Stop Scan" button wants.
    """
    def __init__(self, max_workers: int = 8, cache_ttl: float = CACHE_TTL_SECONDS):
        self.max_workers = max_workers
        self.cache: Dict[str, Tuple[int, int, int, float]] = {}  # path -> (size, files, dirs, mtime)
        # When each entry was computed, kept separately so `cache` keeps its shape
        self._cache_time: Dict[str, float] = {}
        self.cache_ttl = cache_ttl
        # Worker threads write the cache concurrently and clear_cache() iterates it,
        # so both need to be serialised.
        self._cache_lock = threading.Lock()
        self._tokens: Set[CancelToken] = set()
        self._tokens_lock = threading.Lock()

    # --- Cancellation -----------------------------------------------------

    def begin_operation(self) -> CancelToken:
        """A token for one operation, live until end_operation()."""
        token = CancelToken()
        with self._tokens_lock:
            self._tokens.add(token)
        return token

    def end_operation(self, token: CancelToken) -> None:
        with self._tokens_lock:
            self._tokens.discard(token)

    def request_cancel(self):
        """Cancels every operation running right now. Operations started afterwards
        get a fresh token and are unaffected."""
        with self._tokens_lock:
            live = list(self._tokens)
        for token in live:
            token.cancel()

    def reset_cancel(self):
        """Kept for compatibility: cancellation is per-operation now, so a new scan
        starts uncancelled without anything having to be reset."""
        return

    @property
    def cancel_requested(self) -> bool:
        """True when every live operation has been cancelled (or none is running and
        one was cancelled). Reporting only; the scans themselves check their token."""
        with self._tokens_lock:
            if not self._tokens:
                return False
            return all(t.cancelled for t in self._tokens)

    # --- Cache ------------------------------------------------------------

    def clear_cache(self, path: Optional[str] = None):
        """Drops cached sizes for `path` and everything beneath it."""
        with self._cache_lock:
            if path:
                prefix = os.path.normpath(path)
                prefix_sep = prefix + os.sep
                # Plain startswith would also evict "C:\\foobar" when clearing "C:\\foo"
                keys_to_remove = [
                    k for k in self.cache
                    if k == prefix or k.startswith(prefix_sep)
                ]
                for k in keys_to_remove:
                    self.cache.pop(k, None)
                    self._cache_time.pop(k, None)
            else:
                self.cache.clear()
                self._cache_time.clear()

    def get_cached(self, folder_path: str) -> Optional[Tuple[int, int, int, float]]:
        """Cached (size, files, dirs, mtime) for a folder, or None.

        Public because UIs read it to show a known size instantly; going through
        here avoids the `if path in cache: cache[path]` race against clear_cache().
        """
        return self._cache_get(folder_path)

    def _cache_get(self, folder_path: str, honour_ttl: bool = False) -> Optional[Tuple[int, int, int, float]]:
        with self._cache_lock:
            entry = self.cache.get(folder_path)
            if entry is None:
                return None
            if honour_ttl and self.cache_ttl > 0:
                age = time.monotonic() - self._cache_time.get(folder_path, 0.0)
                if age > self.cache_ttl:
                    return None
            return entry

    def _cache_put(self, folder_path: str, value: Tuple[int, int, int, float]) -> None:
        with self._cache_lock:
            self.cache[folder_path] = value
            self._cache_time[folder_path] = time.monotonic()

    # --- Scanning ---------------------------------------------------------

    def _scan_folder_recursive(self, folder_path: str, token: CancelToken) -> Tuple[int, int, int]:
        """
        Recursively calculates folder size, file count, and dir count.
        Uses scandir for high performance.
        Returns (size, file_count, dir_count).
        """
        if token.cancelled:
            return 0, 0, 0

        # Check cache if folder mtime hasn't changed
        try:
            current_mtime = os.path.getmtime(folder_path)
            # honour_ttl: an unchanged mtime does not prove the subtree is unchanged
            cached = self._cache_get(folder_path, honour_ttl=True)
            if cached is not None:
                cached_size, cached_files, cached_dirs, cached_mtime = cached
                if abs(cached_mtime - current_mtime) < 0.001:
                    return cached_size, cached_files, cached_dirs
        except (OSError, PermissionError):
            current_mtime = 0.0

        total_size = 0
        file_count = 0
        dir_count = 0

        stack = [folder_path]

        while stack:
            if token.cancelled:
                break
            curr = stack.pop()
            try:
                with os.scandir(curr) as entries:
                    for entry in entries:
                        try:
                            # Avoid following symlinks / reparse points (e.g. junctions) to prevent infinite loops
                            if entry.is_symlink():
                                continue
                            if entry.is_file(follow_symlinks=False):
                                stat = entry.stat(follow_symlinks=False)
                                total_size += stat.st_size
                                file_count += 1
                            elif entry.is_dir(follow_symlinks=False):
                                dir_count += 1
                                stack.append(entry.path)
                        except (PermissionError, FileNotFoundError, OSError):
                            continue
            except (PermissionError, FileNotFoundError, OSError):
                continue

        # Store in cache
        if not token.cancelled:
            self._cache_put(folder_path, (total_size, file_count, dir_count, current_mtime))

        return total_size, file_count, dir_count

    def scan_directory_immediate(self, folder_path: str) -> List[ItemInfo]:
        """
        Quickly reads direct children of folder_path.
        Files have their sizes set immediately.
        Folders are returned with size 0 and is_scanned=False.
        """
        items: List[ItemInfo] = []
        cluster_size = get_cluster_size(folder_path)

        try:
            with os.scandir(folder_path) as entries:
                for entry in entries:
                    try:
                        is_symlink = entry.is_symlink()
                        is_dir = entry.is_dir(follow_symlinks=False)
                        stat = entry.stat(follow_symlinks=False)
                        mtime = stat.st_mtime

                        if is_dir:
                            info = ItemInfo(
                                path=entry.path,
                                name=entry.name,
                                is_dir=True,
                                size=0,
                                allocated_size=0,
                                file_count=0,
                                dir_count=0,
                                mtime=mtime,
                                type_desc="File folder" if not is_symlink else "Folder junction",
                                is_scanned=False
                            )
                        else:
                            fsize = stat.st_size
                            alloc = calculate_allocated_size(fsize, cluster_size)
                            # Get extension
                            ext = os.path.splitext(entry.name)[1].lower()
                            type_desc = f"{ext[1:].upper()} File" if ext else "File"
                            info = ItemInfo(
                                path=entry.path,
                                name=entry.name,
                                is_dir=False,
                                size=fsize,
                                allocated_size=alloc,
                                file_count=1,
                                dir_count=0,
                                mtime=mtime,
                                type_desc=type_desc,
                                is_scanned=True
                            )
                        items.append(info)
                    except (PermissionError, FileNotFoundError, OSError) as e:
                        # Report what the entry actually is; assuming "directory" here
                        # gave unreadable files a folder icon and queued them for a
                        # recursive size scan.
                        try:
                            entry_is_dir = entry.is_dir(follow_symlinks=False)
                        except (PermissionError, FileNotFoundError, OSError):
                            entry_is_dir = False
                        items.append(ItemInfo(
                            path=entry.path,
                            name=entry.name,
                            is_dir=entry_is_dir,
                            type_desc="File folder" if entry_is_dir else "File",
                            error=str(e)
                        ))
        except (PermissionError, FileNotFoundError, OSError):
            pass

        return items

    def scan_directory(
        self,
        folder_path: str,
        on_folder_scanned: Optional[Callable[[ItemInfo, int, int], None]] = None,
        token: Optional[CancelToken] = None,
        items: Optional[List[ItemInfo]] = None
    ) -> ScanResult:
        """
        Full scan of folder_path.
        First lists immediate children, then computes folder sizes concurrently.
        Calls on_folder_scanned(item_info, completed_count, total_folders) as each folder finishes.

        `token` cancels just this scan; pass the one from begin_operation() to be able
        to stop it when the user navigates away. `items` reuses a listing the caller
        already has, so a navigation does not scandir the same folder twice.
        """
        start_time = time.perf_counter()
        own_token = token is None
        if own_token:
            token = self.begin_operation()

        try:
            if items is None:
                items = self.scan_directory_immediate(folder_path)
            cluster_size = get_cluster_size(folder_path)

            dir_items = [item for item in items if item.is_dir and not item.error]
            total_dirs = len(dir_items)
            completed_dirs = 0

            # Run folder sizing in thread pool
            def _process_dir(dir_item: ItemInfo) -> ItemInfo:
                if token.cancelled:
                    return dir_item
                size, files, dirs = self._scan_folder_recursive(dir_item.path, token)
                dir_item.size = size
                dir_item.allocated_size = calculate_allocated_size(size, cluster_size)
                dir_item.file_count = files
                dir_item.dir_count = dirs
                dir_item.is_scanned = True
                return dir_item

            if dir_items and not token.cancelled:
                with ThreadPoolExecutor(max_workers=min(self.max_workers, max(1, len(dir_items)))) as executor:
                    futures = {executor.submit(_process_dir, item): item for item in dir_items}
                    for future in as_completed(futures):
                        if token.cancelled:
                            break
                        try:
                            scanned_item = future.result()
                            completed_dirs += 1
                            if on_folder_scanned:
                                on_folder_scanned(scanned_item, completed_dirs, total_dirs)
                        except Exception as e:
                            item = futures[future]
                            item.error = str(e)

            # Compute totals
            total_size = sum(item.size for item in items)
            total_alloc = sum(item.allocated_size for item in items)
            total_files = sum(item.file_count for item in items)
            total_folders = len(dir_items) + sum(item.dir_count for item in dir_items)

            duration = time.perf_counter() - start_time
            return ScanResult(
                current_path=folder_path,
                items=items,
                total_size=total_size,
                total_allocated_size=total_alloc,
                total_files=total_files,
                total_dirs=total_folders,
                scan_duration=duration,
                is_complete=not token.cancelled
            )
        finally:
            if own_token:
                self.end_operation(token)

    def deep_scan_largest_files(
        self,
        folder_path: str,
        limit: int = 100,
        on_progress: Optional[Callable[[int, int], None]] = None,
        token: Optional[CancelToken] = None
    ) -> List[Tuple[str, str, int, float]]:
        """
        Performs recursive traversal to find top largest files.
        Returns list of (filename, full_path, size, mtime) sorted by size descending.

        Only `limit` entries are ever held, via a bounded min-heap -- keeping every
        file meant hundreds of MB of tuples (and an O(n log n) sort) when pointed at
        a whole drive.
        """
        own_token = token is None
        if own_token:
            token = self.begin_operation()

        try:
            if limit <= 0:
                return []

            heap: List[Tuple[int, int, str, str, float]] = []
            stack = [folder_path]
            files_count = 0
            seq = 0  # tie-breaker, so equal sizes never compare the remaining fields

            while stack and not token.cancelled:
                curr = stack.pop()
                try:
                    with os.scandir(curr) as entries:
                        for entry in entries:
                            if token.cancelled:
                                break
                            try:
                                if entry.is_symlink():
                                    continue
                                if entry.is_file(follow_symlinks=False):
                                    stat = entry.stat(follow_symlinks=False)
                                    seq += 1
                                    record = (stat.st_size, seq, entry.name, entry.path, stat.st_mtime)
                                    if len(heap) < limit:
                                        heapq.heappush(heap, record)
                                    elif record[0] > heap[0][0]:
                                        heapq.heapreplace(heap, record)
                                    files_count += 1
                                    if files_count % 2000 == 0 and on_progress:
                                        on_progress(files_count, len(heap))
                                elif entry.is_dir(follow_symlinks=False):
                                    stack.append(entry.path)
                            except (PermissionError, FileNotFoundError, OSError):
                                continue
                except (PermissionError, FileNotFoundError, OSError):
                    continue

            ordered = sorted(heap, key=lambda r: r[0], reverse=True)
            return [(name, path, size, mtime) for size, _seq, name, path, mtime in ordered]
        finally:
            if own_token:
                self.end_operation(token)

    def deep_scan_file_types(
        self,
        folder_path: str,
        token: Optional[CancelToken] = None
    ) -> Dict[str, Dict[str, Any]]:
        """
        Recursively groups files by extension.
        Returns dict: {'.ext': {'count': int, 'size': int}}
        """
        own_token = token is None
        if own_token:
            token = self.begin_operation()

        try:
            type_stats: Dict[str, Dict[str, Any]] = {}
            stack = [folder_path]

            while stack and not token.cancelled:
                curr = stack.pop()
                try:
                    with os.scandir(curr) as entries:
                        for entry in entries:
                            if token.cancelled:
                                break
                            try:
                                if entry.is_symlink():
                                    continue
                                if entry.is_file(follow_symlinks=False):
                                    stat = entry.stat(follow_symlinks=False)
                                    ext = os.path.splitext(entry.name)[1].lower()
                                    if not ext:
                                        ext = "(no extension)"
                                    bucket = type_stats.get(ext)
                                    if bucket is None:
                                        bucket = type_stats[ext] = {'count': 0, 'size': 0}
                                    bucket['count'] += 1
                                    bucket['size'] += stat.st_size
                                elif entry.is_dir(follow_symlinks=False):
                                    stack.append(entry.path)
                            except (PermissionError, FileNotFoundError, OSError):
                                continue
                except (PermissionError, FileNotFoundError, OSError):
                    continue

            return type_stats
        finally:
            if own_token:
                self.end_operation(token)

