#!/bin/bash
# =============================================================================
# build.sh  -  Full build pipeline for AudioEnvelope (Linux / macOS)
#
# Usage:
#   ./build.sh            # build binary only
#
# Requirements:
#   Python 3.11+ with dearpygui, numpy, sounddevice installed
#   PortAudio system library (Ubuntu: apt install libportaudio2)
#
# The .venv in the project folder is used automatically if present;
# otherwise the system python3 is used.
# Optional: scipy gives a smoother audio path on Linux (pip install scipy).
# =============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ---------------------------------------------------------------------------
# 1. Locate Python — prefer the project venv, fall back to system python3
# ---------------------------------------------------------------------------
PY=""
if [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
elif [ -x "venv/bin/python" ]; then
    PY="venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PY="$(command -v python3)"
fi

if [ -z "$PY" ]; then
    echo "ERROR: No Python interpreter found." >&2
    exit 1
fi

echo ""
echo "=== Checking prerequisites ==="
echo "Python      : $PY"
"$PY" --version

# ---------------------------------------------------------------------------
# 2. Ensure pyinstaller is available
# ---------------------------------------------------------------------------
if ! "$PY" -c "import PyInstaller" >/dev/null 2>&1; then
    echo ""
    echo "pyinstaller not found — installing into the current Python..."
    "$PY" -m pip install "pyinstaller>=6.0.0"
fi
PYINSTALLER="$PY -m PyInstaller"

# ---------------------------------------------------------------------------
# 3. Clean previous build artefacts
# ---------------------------------------------------------------------------
echo ""
echo "=== Cleaning previous build ==="
rm -rf build dist

# ---------------------------------------------------------------------------
# 4. Build the binary using the spec file
# ---------------------------------------------------------------------------
echo ""
echo "=== Building AudioEnvelope ==="
$PYINSTALLER AudioEnvelope.spec

if [ ! -f "dist/AudioEnvelope" ]; then
    echo "ERROR: Build failed - dist/AudioEnvelope not found." >&2
    exit 1
fi

BIN_SIZE="$(du -h "dist/AudioEnvelope" | cut -f1)"
echo ""
echo "Build successful: dist/AudioEnvelope  [$BIN_SIZE]"
echo ""
echo "Run it with:  ./dist/AudioEnvelope"
echo "(Grant audio access if asked:  sudo usermod -aG audio \$USER)"