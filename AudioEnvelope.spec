# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec file for AudioEnvelope
# Build with:  pyinstaller AudioEnvelope.spec

import os
import sys
import site
from PyInstaller.utils.hooks import collect_all

# ---------------------------------------------------------------------------
# Build the full list of site-packages directories so PyInstaller
# finds packages installed in user-level locations (e.g. AppData\Roaming).
# ---------------------------------------------------------------------------
_site_dirs = site.getsitepackages() + [site.getusersitepackages()]

# ---------------------------------------------------------------------------
# Explicitly collect ALL files that make up dearpygui (the .pyd extension
# plus any data files and sub-packages).  This is needed because the package
# lives in the user site-packages path which PyInstaller doesn't search by
# default on some Python 3.14 configurations.
# ---------------------------------------------------------------------------
_dpg_datas, _dpg_binaries, _dpg_hidden = collect_all('dearpygui')

# ---------------------------------------------------------------------------
# Locate PortAudio DLLs that sounddevice loads at runtime via ctypes.
# ---------------------------------------------------------------------------
import _sounddevice_data as _sdd
_sdd_dir  = os.path.dirname(_sdd.__file__)
_pa_dir   = os.path.join(_sdd_dir, 'portaudio-binaries')

binaries = _dpg_binaries + [
    (os.path.join(_pa_dir, f), '_sounddevice_data/portaudio-binaries')
    for f in os.listdir(_pa_dir) if f.lower().endswith('.dll')
]

# ---------------------------------------------------------------------------
# Data files
# ---------------------------------------------------------------------------
datas = _dpg_datas + [
    (os.path.join(_sdd_dir, '__init__.py'), '_sounddevice_data'),
    (os.path.join(SPECPATH, 'bdars-logo.png'), '.'),
]

_font = os.path.join(SPECPATH, 'NicerFont.ttf')
if os.path.exists(_font):
    datas.append((_font, '.'))
    print("INFO: NicerFont.ttf found and included.")
else:
    print("INFO: NicerFont.ttf not found - app will use default font.")

# ---------------------------------------------------------------------------
# Hidden imports
# ---------------------------------------------------------------------------
hidden = _dpg_hidden + [
    'sounddevice',
    '_sounddevice_data',
    'numpy',
    'numpy.core._multiarray_umath',
    'numpy.fft',
    'numpy.fft._pocketfft',
    'ctypes',
    'ctypes.util',
    'queue',
    'threading',
]

# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
a = Analysis(
    ['main.py'],
    pathex=[SPECPATH] + _site_dirs,
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'matplotlib', 'PIL', 'tkinter', 'wx', 'PyQt5', 'PyQt6',
        'scipy', 'pandas', 'IPython', 'jupyter',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='AudioEnvelope',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
