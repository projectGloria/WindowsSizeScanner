"""Writes a PyInstaller --version-file from gloria.toml.

Without VERSIONINFO, Windows names the app after its file in Task Manager, the taskbar and
"Open with", and the GloriaSetup release check fails: FileDescription must equal the
gloria.toml name, which is the repo name.

Usage: python write_version_info.py <output path>
"""
import re
import sys
import tomllib
from pathlib import Path

root = Path(__file__).resolve().parent
manifest = tomllib.loads((root / "gloria.toml").read_text(encoding="utf-8"))
name, exe = manifest["name"], manifest["main_exe"]
parts = [int(p) for p in re.findall(r"\d+", manifest["version"])][:4]
parts += [0] * (4 - len(parts))
vt, vs = tuple(parts), ".".join(map(str, parts))

text = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={vt}, prodvers={vt}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Project Gloria'),
      StringStruct('FileDescription', {name!r}),
      StringStruct('ProductName', {name!r}),
      StringStruct('InternalName', {name!r}),
      StringStruct('OriginalFilename', {exe!r}),
      StringStruct('FileVersion', {vs!r}),
      StringStruct('ProductVersion', {vs!r})])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
out = Path(sys.argv[1])
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(text, encoding="utf-8")
