"""
main.py - Entry point for SizeScanner Windows Application.
Usage:
    python main.py                     -> Runs the in-place Explorer overlay (shows folder sizes directly in Explorer's Size column)
    python main.py --companion         -> Launches the docked Explorer companion panel
    python main.py --full              -> Launches the standalone multi-tab size analyzer app
    python main.py "C:\\Folder"         -> Opens directly focused on folder
"""

import os
import sys

def main():
    initial_path = None
    mode = "inplace"  # 'inplace' (default), 'companion', 'full'

    for arg in sys.argv[1:]:
        clean = arg.strip('"').strip("'")
        if clean in ("--full", "-f"):
            mode = "full"
        elif clean in ("--companion", "-c"):
            mode = "companion"
        elif clean in ("--inplace", "-i"):
            mode = "inplace"
        elif os.path.exists(clean):
            initial_path = os.path.abspath(clean)
            mode = "full"

    if mode == "full":
        from ui_main import SizeScannerApp
        app = SizeScannerApp(initial_path=initial_path)
        app.mainloop()
    elif mode == "companion":
        from companion_window import ExplorerCompanionWindow
        app = ExplorerCompanionWindow()
        if initial_path:
            app.load_path(initial_path)
        app.mainloop()
    else:
        # Default: In-place Explorer overlay service
        from explorer_overlay import InPlaceSizeService
        service = InPlaceSizeService()
        service.start()

if __name__ == "__main__":
    main()
