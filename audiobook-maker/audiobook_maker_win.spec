# PyInstaller spec: packaged Windows desktop build (onedir).
# Built once by a developer via build_win.bat -- end users just double-click
# AudiobookMaker.exe inside the built dist/AudiobookMaker/ folder. voices/
# and output/ are NOT bundled here; app.py resolves them next to
# sys.executable at runtime so they persist across launches instead of
# living inside the onedir extraction folder.

from PyInstaller.utils.hooks import collect_all

block_cipher = None

datas = []
binaries = []
hiddenimports = ["tkinter"]

for pkg in ("piper", "onnxruntime", "pymupdf", "ebooklib", "bs4", "imageio_ffmpeg"):
    pkg_datas, pkg_binaries, pkg_hiddenimports = collect_all(pkg)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hiddenimports

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)
pyz = PYZ(a.pure, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AudiobookMaker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="AudiobookMaker",
)
