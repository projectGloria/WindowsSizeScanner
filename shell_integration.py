"""
shell_integration.py - Windows Explorer Shell Integration & System Helpers.
Provides Windows Explorer context menu registration, drive detection,
safe file operations, and native Windows shell integration.
"""

import os
import sys
import subprocess
import winreg
import ctypes
from ctypes import wintypes
from typing import List, Dict, Optional, Any, Tuple
import psutil
import send2trash

# Windows Shell File Info
class SHFILEINFO(ctypes.Structure):
    _fields_ = [
        ('hIcon', wintypes.HICON),
        ('iIcon', ctypes.c_int),
        ('dwAttributes', wintypes.DWORD),
        ('szDisplayName', wintypes.WCHAR * 260),
        ('szTypeName', wintypes.WCHAR * 80)
    ]

SHGFI_TYPENAME = 0x000000400
SHGFI_USEFILEATTRIBUTES = 0x000000010
FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_ATTRIBUTE_NORMAL = 0x00000080

# Declared explicitly: SHGetFileInfoW returns a DWORD_PTR, and ctypes' default c_int
# return type truncates it on 64-bit, so a valid result could read back as 0.
_SHGetFileInfoW = ctypes.windll.shell32.SHGetFileInfoW
_SHGetFileInfoW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(SHFILEINFO),
    wintypes.UINT, wintypes.UINT
]
_SHGetFileInfoW.restype = ctypes.c_void_p

def get_windows_type_name(path: str, is_dir: bool = False) -> str:
    """Returns the official Windows Explorer type name for a file or folder."""
    try:
        shinfo = SHFILEINFO()
        flags = SHGFI_TYPENAME | SHGFI_USEFILEATTRIBUTES
        attrs = FILE_ATTRIBUTE_DIRECTORY if is_dir else FILE_ATTRIBUTE_NORMAL
        res = _SHGetFileInfoW(
            path,
            attrs,
            ctypes.byref(shinfo),
            ctypes.sizeof(shinfo),
            flags
        )
        if res and shinfo.szTypeName:
            return shinfo.szTypeName
    except Exception:
        pass
    if is_dir:
        return "File folder"
    ext = os.path.splitext(path)[1].lower()
    return f"{ext[1:].upper()} File" if ext else "File"

def get_system_drives() -> List[Dict[str, Any]]:
    """Returns a list of drives with mountpoint, filesystem, total, used, free, and percent."""
    drives = []
    try:
        for p in psutil.disk_partitions(all=False):
            try:
                # Filter out CD-ROMs or inaccessible drives
                if 'cdrom' in p.opts or not p.fstype:
                    continue
                usage = psutil.disk_usage(p.mountpoint)
                drives.append({
                    'mountpoint': p.mountpoint,
                    'fstype': p.fstype,
                    'total': usage.total,
                    'used': usage.used,
                    'free': usage.free,
                    'percent': usage.percent
                })
            except (PermissionError, OSError):
                continue
    except Exception:
        pass
    return drives

def open_in_explorer(path: str):
    """Reveals the file or folder in Windows File Explorer."""
    try:
        norm_path = os.path.abspath(path)
        if os.path.isdir(norm_path):
            subprocess.Popen(['explorer.exe', norm_path])
        else:
            subprocess.Popen(['explorer.exe', f'/select,{norm_path}'])
    except Exception as e:
        print(f"Error opening in Explorer: {e}")

def open_item_default(path: str):
    """Opens the item with the default Windows application."""
    try:
        norm_path = os.path.abspath(path)
        os.startfile(norm_path)
    except Exception as e:
        print(f"Error opening item: {e}")

def open_in_terminal(folder_path: str):
    """Opens Command Prompt or PowerShell in the specified folder."""
    try:
        norm_path = os.path.abspath(folder_path)
        if os.path.isfile(norm_path):
            norm_path = os.path.dirname(norm_path)
    except Exception as e:
        # Resolving the path is what the cmd.exe fallback needs too, so a failure
        # here cannot fall through to it -- norm_path would be unbound.
        print(f"Error opening terminal: {e}")
        return

    try:
        subprocess.Popen(['powershell.exe', '-NoExit', '-Command', f'Set-Location -LiteralPath "{norm_path}"'], cwd=norm_path)
    except Exception:
        try:
            subprocess.Popen(['cmd.exe', '/K', f'cd /d "{norm_path}"'], cwd=norm_path)
        except Exception as e:
            print(f"Error opening terminal: {e}")

def move_to_recycle_bin(path: str) -> bool:
    """Safely moves a file or folder to the Windows Recycle Bin."""
    try:
        norm_path = os.path.abspath(path)
        send2trash.send2trash(norm_path)
        return True
    except Exception as e:
        print(f"Error sending to trash: {e}")
        return False

def launch_mode(*args: str) -> bool:
    """
    Starts another SizeScanner instance with the given arguments.

    When frozen there is no main.py on disk and sys.executable is SizeScanner.exe
    itself, so the arguments go straight to the exe. Running `[sys.executable,
    "companion_window.py"]` unconditionally -- as the UIs used to -- handed the built
    exe a path that does not exist, and it silently started in overlay mode instead.
    """
    if getattr(sys, 'frozen', False):
        cmd = [sys.executable, *args]
    else:
        script = os.path.abspath(os.path.join(os.path.dirname(__file__), "main.py"))
        cmd = [sys.executable, script, *args]
    try:
        subprocess.Popen(cmd)
        return True
    except Exception as e:
        print(f"Failed to launch {' '.join(args)}: {e}")
        return False

# --- Context Menu Integration ---

REG_TARGETS = [
    r"Software\Classes\Directory\shell\SizeScanner",
    r"Software\Classes\Directory\Background\shell\SizeScanner",
    r"Software\Classes\Drive\shell\SizeScanner"
]

def get_executable_command() -> str:
    """Determines the launch command for SizeScanner (exe or python script)."""
    if getattr(sys, 'frozen', False):
        exe_path = sys.executable
        return f'"{exe_path}" "%V"'

    dist_exe = os.path.abspath(os.path.join(os.path.dirname(__file__), "dist", "SizeScanner", "SizeScanner.exe"))
    if os.path.exists(dist_exe):
        return f'"{dist_exe}" "%V"'

    py_exe = sys.executable
    script_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "main.py"))
    pyw_exe = os.path.join(os.path.dirname(py_exe), "pythonw.exe")
    runner = pyw_exe if os.path.exists(pyw_exe) else py_exe
    return f'"{runner}" "{script_path}" "%V"'

def get_app_icon_path() -> str:
    """Returns absolute path to app icon."""
    if getattr(sys, 'frozen', False):
        return sys.executable
    icon_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "icon.ico"))
    if os.path.exists(icon_path):
        return icon_path
    dist_exe = os.path.abspath(os.path.join(os.path.dirname(__file__), "dist", "SizeScanner", "SizeScanner.exe"))
    if os.path.exists(dist_exe):
        return dist_exe
    return ""

def is_context_menu_registered() -> bool:
    """Checks if 'Open in SizeScanner' is currently registered in Windows Explorer."""
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_TARGETS[0])
        winreg.CloseKey(key)
        return True
    except FileNotFoundError:
        return False
    except Exception:
        return False

def register_context_menu() -> Tuple[bool, str]:
    """
    Registers 'Open in SizeScanner' to Windows Explorer context menu for:
    - Folders (right-click on a folder)
    - Folder backgrounds (right-click inside a folder)
    - Drives (right-click on a drive icon)
    """
    command_str = get_executable_command()
    icon_path = get_app_icon_path()
    menu_label = "Open in SizeScanner"
    try:
        for reg_path in REG_TARGETS:
            # Create shell\SizeScanner
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, reg_path) as key:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, menu_label)
                if icon_path:
                    winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, icon_path)

            # Create shell\SizeScanner\command
            cmd_key_path = f"{reg_path}\\command"
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, cmd_key_path) as cmd_key:
                winreg.SetValueEx(cmd_key, "", 0, winreg.REG_SZ, command_str)

        return True, "Successfully registered 'Open in SizeScanner' to Windows Explorer context menu!"
    except Exception as e:
        return False, f"Failed to register context menu: {e}"

def unregister_context_menu() -> Tuple[bool, str]:
    """Removes 'Open in SizeScanner' from Windows Explorer context menu."""
    try:
        for reg_path in REG_TARGETS:
            try:
                # Delete subkey 'command' first
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, f"{reg_path}\\command")
            except FileNotFoundError:
                pass
            try:
                # Delete main key
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, reg_path)
            except FileNotFoundError:
                pass
        return True, "Successfully removed from Windows Explorer context menu."
    except Exception as e:
        return False, f"Failed to remove context menu: {e}"
