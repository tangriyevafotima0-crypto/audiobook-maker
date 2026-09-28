# PyInstaller spec: packaged macOS desktop build (onedir + .app bundle).
# Built once by a developer via build_mac.sh -- end users just double-click
# the resulting AudiobookMaker.app. voices/ and output/ are NOT bundled
# here; app.py resolves them next to sys.executable at runtime so they
# persist across launches instead of living inside the app bundle.

from PyInstaller.utils.hooks import collect_all

block_cipher = None

datas = []
binaries = []
hiddenimports = ["tkinter"]

# onnxruntime (Piper's inference backend) and pymupdf both ship native
# extensions/data files that PyInstaller's default import scan misses, so
# collect each dependency fully rather than hand-picking hidden imports.
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
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
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

app = BUNDLE(
    coll,
    name="AudiobookMaker.app",
    icon=None,
    bundle_identifier="com.audiobookmaker.app",
    info_plist={
        "NSHighResolutionCapable": "True",
        "CFBundleShortVersionString": "1.0.0",
    },
)
