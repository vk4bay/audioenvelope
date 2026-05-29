# =============================================================================
# build.ps1  -  Full build pipeline for AudioEnvelope
#
# Usage:
#   .\build.ps1            # build exe only
#   .\build.ps1 -Installer # build exe then create Inno Setup installer
#
# Requirements:
#   pip install pyinstaller
#   Inno Setup 6 installed at default location (only needed for -Installer)
# =============================================================================

param(
    [switch]$Installer
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $ScriptDir

# ---------------------------------------------------------------------------
# 1. Locate the correct Python / pyinstaller
#    PyCharm uses Python 3.12 which has all packages installed.
#    The system "python" command may point to a different version.
# ---------------------------------------------------------------------------
$Python312 = "C:\Users\bob\AppData\Local\Programs\Python\Python312\python.exe"
$PyInstaller312 = "C:\Users\bob\AppData\Local\Programs\Python\Python312\Scripts\pyinstaller.exe"

Write-Host ""
Write-Host "=== Checking prerequisites ===" -ForegroundColor Cyan
if (-not (Test-Path $Python312)) {
    Write-Error "Python 3.12 not found at $Python312"
}
if (-not (Test-Path $PyInstaller312)) {
    Write-Error "pyinstaller not found. Run: & '$Python312' -m pip install pyinstaller"
}
Write-Host "Python 3.12  : $Python312"
Write-Host "pyinstaller  : $PyInstaller312"

# ---------------------------------------------------------------------------
# 2. Clean previous build artefacts
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "=== Cleaning previous build ===" -ForegroundColor Cyan
foreach ($dir in @('build', 'dist')) {
    if (Test-Path $dir) {
        Remove-Item $dir -Recurse -Force
        Write-Host "Removed $dir\"
    }
}

# ---------------------------------------------------------------------------
# 3. Build the exe using the spec file
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "=== Building AudioEnvelope.exe ===" -ForegroundColor Cyan
& $PyInstaller312 AudioEnvelope.spec

if (-not (Test-Path "dist\AudioEnvelope.exe")) {
    Write-Error "Build failed - dist\AudioEnvelope.exe not found."
}

$exeSize = [math]::Round((Get-Item "dist\AudioEnvelope.exe").Length / 1MB, 1)
$msg = "Build successful: dist\AudioEnvelope.exe  [$exeSize MB]"
Write-Host ""
Write-Host $msg -ForegroundColor Green

# ---------------------------------------------------------------------------
# 4. Optionally create Inno Setup installer
# ---------------------------------------------------------------------------
if ($Installer) {
    Write-Host ""
    Write-Host "=== Building installer via Inno Setup ===" -ForegroundColor Cyan

    $iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
    if (-not (Test-Path $iscc)) {
        $iscc = "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    }
    if (-not (Test-Path $iscc)) {
        Write-Error "Inno Setup 6 not found. Download from https://jrsoftware.org/isinfo.php"
    }

    & $iscc "installer.iss"

    if (Test-Path "dist\AudioEnvelope_Setup.exe") {
        $instSize = [math]::Round((Get-Item "dist\AudioEnvelope_Setup.exe").Length / 1MB, 1)
        $instMsg = "Installer ready: dist\AudioEnvelope_Setup.exe  [$instSize MB]"
        Write-Host ""
        Write-Host $instMsg -ForegroundColor Green
    } else {
        Write-Error "Inno Setup did not produce dist\AudioEnvelope_Setup.exe"
    }
}

Write-Host ""
Write-Host "=== Done ===" -ForegroundColor Green

