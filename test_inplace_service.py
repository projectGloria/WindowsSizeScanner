import os
import sys
import time
import subprocess
import threading
sys.stdout.reconfigure(encoding='utf-8')

from explorer_overlay import InPlaceSizeService

def test_service():
    print("Launching test Explorer window...")
    proc = subprocess.Popen(['explorer.exe', os.path.abspath('.')])
    time.sleep(1.8)

    print("Starting InPlaceSizeService...")
    service = InPlaceSizeService()

    # Run service for 3.5 seconds
    def _timer():
        time.sleep(3.5)
        print("Stopping service...")
        service.stop()

    threading.Thread(target=_timer, daemon=True).start()

    service.start()

    print(f"Overlay current path: {service.overlay.current_path}")
    print(f"Overlay folder sizes count: {len(service.overlay.folder_sizes)}")
    print(f"Overlay folder sizes: {service.overlay.folder_sizes}")

    assert os.path.exists(service.overlay.current_path), f"Path does not exist: {service.overlay.current_path}"
    assert len(service.overlay.folder_sizes) > 0, "Expected folder sizes to be calculated"

    print("IN-PLACE EXPLORER OVERLAY TEST PASSED!")

if __name__ == "__main__":
    test_service()
