# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec file for AudioEnvelope.
# Cross-platform (Windows / Linux / macOS).
# Build with:  pyinstaller AudioEnvelope.spec
# (or use build.ps1 on Windows / build.sh on Linux)

import os
import sys
import site
from PyInstaller.utils.hooks import collect_all

# ---------------------------------------------------------------------------
# Build the full list of site-packages directories so PyInstaller
# finds packages installed in user-level or virtualenv locations.
# ---------------------------------------------------------------------------
_site_dirs = site.getsitepackages() + [site.getusersitepackages()]

# ---------------------------------------------------------------------------
# Explicitly collect ALL files that make up dearpygui (the .pyd/.so extension
# plus any data files and sub-packages).
# ---------------------------------------------------------------------------
_dpg_datas, _dpg_binaries, _dpg_hidden = collect_all('dearpygui')

# ---------------------------------------------------------------------------
# PortAudio binaries bundled with sounddevice wheels (Windows / macOS).
# Linux uses the system PortAudio library instead, so _sounddevice_data
# does not exist there — import it only if available.
# ---------------------------------------------------------------------------
_pa_binaries = []
try:
    import _sounddevice_data as _sdd
    _sdd_dir  = os.path.dirname(_sdd.__file__)
    _pa_dir   = os.path.join(_sdd_dir, 'portaudio-binaries')
    for _f in os.listdir(_pa_dir):
        if _f.lower().endswith(('.dll', '.so', '.dylib')):
            _pa_binaries.append((os.path.join(_pa_dir, _f),
                                 '_sounddevice_data/portaudio-binaries'))
    print(f"INFO: Bundling {len(_pa_binaries)} PortAudio file(s) from _sounddevice_data.")
except ImportError:
    print("INFO: _sounddevice_data not found - using system PortAudio (Linux/macOS).")

binaries = _dpg_binaries + _pa_binaries

# ---------------------------------------------------------------------------
# Data files
# ---------------------------------------------------------------------------
datas = _dpg_datas + [
    (os.path.join(SPECPATH, 'bdars-logo.png'), '.'),
]
if _pa_binaries:
    datas.append((os.path.join(_sdd_dir, '__init__.py'), '_sounddevice_data'))

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
    'numpy',
    'numpy.core._multiarray_umath',
    'numpy.fft',
    'numpy.fft._pocketfft',
    'ctypes',
    'ctypes.util',
    'queue',
    'threading',
]
if _pa_binaries:
    hidden.append('_sounddevice_data')

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
        'pandas', 'IPython', 'jupyter',
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