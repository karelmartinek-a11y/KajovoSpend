# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# Include RapidOCR package data (notably config.yaml) for PyInstaller builds.
rapidocr_datas = collect_data_files('rapidocr_onnxruntime') if True else []
rapidocr_hidden = collect_submodules('rapidocr_onnxruntime') if True else []

a = Analysis(
    ['run_gui.py'],
    pathex=[],
    binaries=[],
    datas=[('assets', 'assets'), ('src', 'src')] + rapidocr_datas,
    hiddenimports=rapidocr_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='KajovoSpend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets\\app.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='KajovoSpend',
)
