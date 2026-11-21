# GloriaSetup rollout

Pushing a tag like `v1.2.0` now builds the app (PyInstaller onedir) and attaches `WindowsSizeScanner_1.2.0_x64-setup.exe` to that release.

## Please check

- **Version:** the project has no version in its files, so `gloria.toml` uses 0.2.0 from the GloriaLauncher catalog. The tag overrides it at release time.
- **Icon:** `icon.ico` in the repo root (no `WindowsSizeScanner.ico` exists); it is also what `build_exe.py` embeds.
- **Data folder:** `%APPDATA%\SizeScanner` (settings.json, from `settings.py`). The app only keeps settings there.
- **Dependencies:** there is no requirements file, so the workflow pip-installs the list from CLAUDE.md unpinned (Python 3.12). Pin versions if a build breaks.
- **app-dist:** the whole `dist\SizeScanner` folder (exe plus `_internal`), since PyInstaller onedir needs all of it.
- **No legacy installer:** the app never shipped with NSIS/Inno/MSI, so `legacy_names` is empty.
- **Secrets:** `WINDOWS_PFX_BASE64` and `WINDOWS_PFX_PASSWORD` must exist in the repo, otherwise the setup is unsigned.
- Nothing was built or run locally.
