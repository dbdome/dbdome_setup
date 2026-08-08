# -*- mode: python ; coding: utf-8 -*-
import os

a = Analysis(
    ['setup.py'],
    pathex=[],
    binaries=[],
    # build stamp from build.ps1 so the installer reports 2.01.<Build_No>
    datas=([('version_build.json', '.')] if os.path.isfile('version_build.json') else []),
    hiddenimports=[
        'bcrypt', '_cffi_backend', 'jwt',
        'psycopg2', 'wmi', 'win32com', 'win32com.client',
    ],
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
    a.binaries,
    a.datas,
    [],
    name='dbdome_setup',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['c:\\dev\\dbanalytics\\icons\\dbdome.ico'],
)
