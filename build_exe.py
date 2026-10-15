"""
build_exe.py - Builds a standalone SizeScanner.exe executable using PyInstaller.
"""

import os
import sys
import subprocess
import shutil

def build():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)

    print("Building SizeScanner.exe...")

    # PyInstaller command
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onedir",             # directory mode for fast startup (or can do onefile)
        "--windowed",           # no console window
        "--name", "SizeScanner",
        "--icon", "icon.ico",
        "--add-data", f"icon.ico{os.pathsep}.",
        "--collect-all", "customtkinter",
        "--collect-all", "uiautomation",
        "--hidden-import", "send2trash",
        "--hidden-import", "psutil",
        "--hidden-import", "PIL",
        "--hidden-import", "pystray",
        "--hidden-import", "win32gui",
        "--hidden-import", "win32process",
        "--hidden-import", "win32api",
        # win32ui backs the column capture; pythoncom/win32com.client are imported
        # inside _com_explorer_paths(), which is where Explorer path detection starts
        "--hidden-import", "win32ui",
        "--hidden-import", "pythoncom",
        "--hidden-import", "win32com.client",
        "--hidden-import", "comtypes",
        "main.py"
    ]

    print("Running:", " ".join(cmd))
    res = subprocess.run(cmd)
    if res.returncode == 0:
        print("\nBuild successful! Output is located in dist/SizeScanner/SizeScanner.exe")
    else:
        print(f"\nBuild failed with exit code: {res.returncode}")

if __name__ == "__main__":
    build()
