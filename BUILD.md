# Build Instructions — Audio Envelope Oscilloscope

These instructions produce two distributable files from the Python source:

| Output file | Purpose |
|---|---|
| `dist\AudioEnvelope.exe` | Single executable — copy anywhere and run |
| `dist\AudioEnvelope_Setup.exe` | Windows installer with Start Menu, desktop icon and uninstaller |

---

## Prerequisites

### 1. Python 3.12

The project requires **Python 3.12** (the version PyCharm is configured to use).
Download from <https://www.python.org/downloads/release/python-3120/>

During installation tick **"Add Python to PATH"** and **"pip"**.

Verify:

```
python --version
```

Expected: `Python 3.12.x`

> **Important:** If you have multiple Python versions installed, make sure you are
> using the correct one. The build script targets Python 3.12 at its default
> installation path. Check the path constant near the top of `build.ps1` if
> your installation is in a different location.

---

### 2. Python packages

Open a terminal and install all runtime dependencies into Python 3.12:

```powershell
python -m pip install dearpygui sounddevice numpy
```

No other packages are needed at runtime.

---

### 3. PyInstaller

Install into the **same** Python 3.12 environment:

```powershell
python -m pip install pyinstaller
```

---

### 4. Inno Setup 6 *(installer only — skip if you only need the bare exe)*

Download and install the free **Inno Setup 6** compiler from:
<https://jrsoftware.org/isinfo.php>

Accept the default installation path
(`C:\Program Files (x86)\Inno Setup 6\`).

---

### 5. Asset files

Place both of these files in the same folder as `main.py`:

| File | Notes |
|---|---|
| `bdars-logo.png` | Required — branding overlay displayed in the top-right corner |
| `NicerFont.ttf` | Optional — if absent the app falls back to the system default font |

---

## Building

All commands are run from the **project root folder**
(`C:\projects\audioenvelope` or wherever you cloned the repository).

### Bare executable only

```powershell
.\build.ps1
```

Output: `dist\AudioEnvelope.exe`

### Executable **and** installer

```powershell
.\build.ps1 -Installer
```

Output:
- `dist\AudioEnvelope.exe`
- `dist\AudioEnvelope_Setup.exe`

The script will:
1. Verify prerequisites
2. Delete any previous `build\` and `dist\` folders
3. Run PyInstaller using `AudioEnvelope.spec`
4. *(With `-Installer`)* Run the Inno Setup compiler using `installer.iss`

A successful build ends with:

```
Build successful: dist\AudioEnvelope.exe  [~27 MB]
Installer ready: dist\AudioEnvelope_Setup.exe  [~29 MB]
=== Done ===
```

---

## Build files reference

| File | Purpose |
|---|---|
| `build.ps1` | PowerShell build script — entry point for all builds |
| `AudioEnvelope.spec` | PyInstaller specification — controls what is bundled |
| `installer.iss` | Inno Setup script — controls the installer package |

---

## Known issues and fixes

### "python" resolves to the wrong version

If you have Python 3.13 or 3.14 also installed, the `python` command may not
point to 3.12. The build script uses an **explicit path** to Python 3.12
(`C:\Users\<you>\AppData\Local\Programs\Python\Python312\python.exe`).
If this path differs on your machine, edit the `$Python312` and
`$PyInstaller312` variables near the top of `build.ps1`.

### PyInstaller reports: *"pathlib" package is an obsolete backport*

An old third-party `pathlib` package conflicts with PyInstaller. Remove it once:

```powershell
python -m pip uninstall pathlib
```

Then re-run the build.

### Exe starts but shows: *No module named 'dearpygui'*

PyInstaller was invoked with the wrong Python version (one that does not have
dearpygui installed). Ensure `build.ps1` points to the Python 3.12 executable
that has `dearpygui` installed. Verify with:

```powershell
& "C:\Users\<you>\AppData\Local\Programs\Python\Python312\python.exe" -c "import dearpygui; print(dearpygui.__file__)"
```

This must print a path, not an error, before the build will work.

### Exe starts but produces no audio (*PortAudio library not found*)

The PortAudio DLL was not bundled. Verify that `_sounddevice_data` is installed
in Python 3.12:

```powershell
python -m pip install sounddevice
```

The spec file locates the DLL automatically via `import _sounddevice_data`.
If the error persists, check `AudioEnvelope.spec` and confirm the
`_sounddevice_data` import succeeds when run under Python 3.12.

---

## Deploying to the museum PC

**Option A — bare exe (simplest):**

Copy `dist\AudioEnvelope.exe` to the museum PC. Double-click to run.
No installation required, no Python needed.

**Option B — installer (recommended for managed machines):**

Copy `dist\AudioEnvelope_Setup.exe` to the museum PC and run it.
The wizard installs the application, creates a Start Menu entry and desktop
shortcut, and registers an uninstaller in Windows Settings.

The installed application requires no internet connection and no additional
software.

---

## Rebuilding after source changes

Simply run `.\build.ps1 -Installer` again. The script cleans all previous
build artefacts automatically before each build.

