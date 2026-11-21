# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import sys
import tkinterdnd2
from PyInstaller.utils.hooks import collect_data_files, copy_metadata

root = Path(SPECPATH)
resources = [(str(root / name), ".") for name in ("studio.ico", "ego_douyin.mjs", "使用说明.md", "CHANGELOG.md")]
resources += collect_data_files("langdetect")
resources += copy_metadata("resvg-py")
resources += copy_metadata("svg.path")
resources.append((str(Path(tkinterdnd2.__file__).parent / "tkdnd"), "tkinterdnd2/tkdnd"))
a = Analysis([str(root / "studio.py")], pathex=[str(root)], datas=resources)
pyz = PYZ(a.pure)
name = "VAT"
if sys.platform == "darwin":
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=name, console=False)
    folder = COLLECT(exe, a.binaries, a.datas, name=name)
    app = BUNDLE(folder, name="VAT.app", bundle_identifier="studio.vat.editor")
else:
    icon = str(root / "studio.ico") if sys.platform == "win32" else None
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name=name, console=False, icon=icon)
