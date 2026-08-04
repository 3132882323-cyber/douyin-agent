# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

bridge_dir = Path(SPECPATH)
knowledge_dir = bridge_dir.parent / "assets" / "knowledge"
datas = []
if knowledge_dir.exists():
    datas.append((str(knowledge_dir), "assets/knowledge"))

a = Analysis(
    [str(bridge_dir / "http_receiver.py")],
    pathex=[str(bridge_dir)],
    binaries=[],
    datas=datas,
    hiddenimports=["cryptography.hazmat.primitives.asymmetric.ed25519"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="DianAgent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
)
