# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)

bridge_dir = Path(SPECPATH)
version_namespace = {}
exec((bridge_dir / "version.py").read_text(encoding="utf-8"), version_namespace)
agent_version = str(version_namespace["AGENT_VERSION"])
version_parts = tuple(int(part) for part in agent_version.split(".")) + (0,)
version_parts = version_parts[:4]
windows_version = VSVersionInfo(
    ffi=FixedFileInfo(
        filevers=version_parts,
        prodvers=version_parts,
        mask=0x3F,
        flags=0x0,
        OS=0x40004,
        fileType=0x1,
        subtype=0x0,
        date=(0, 0),
    ),
    kids=[
        StringFileInfo(
            [
                StringTable(
                    "080404B0",
                    [
                        StringStruct("CompanyName", "DianAgent"),
                        StringStruct("FileDescription", "Dian Agent local commerce service"),
                        StringStruct("FileVersion", agent_version),
                        StringStruct("InternalName", "DianAgent"),
                        StringStruct("OriginalFilename", "DianAgent.exe"),
                        StringStruct("ProductName", "Dian Agent"),
                        StringStruct("ProductVersion", agent_version),
                    ],
                )
            ]
        ),
        VarFileInfo([VarStruct("Translation", [2052, 1200])]),
    ],
)
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
    version=windows_version,
)
