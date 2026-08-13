# Builds the distributable. Run from the lcd-panel folder:
#   .\build.ps1
# Produces dist\LcdPanel\ and dist\LcdPanel.zip (the thing to share).

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$dist = Join-Path $root "dist\LcdPanel"

Write-Output "1. Running PyInstaller..."
& (Join-Path $root "venv\Scripts\pyinstaller.exe") --noconfirm --clean (Join-Path $root "lcd-panel.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

Write-Output "2. Staging user-facing files..."
# The shipped config is the blank one; config.ini in the repo root is Daniel's
# own working copy and must never go out with his dictation path in it.
Move-Item -Path (Join-Path $dist "config.dist.ini") -Destination (Join-Path $dist "config.ini") -Force
Copy-Item -Path (Join-Path $root "README.md") -Destination $dist -Force
Copy-Item -Path (Join-Path $root "status_panel.py") -Destination $dist -Force  # GPL: ship the source

Write-Output "3. Zipping..."
$zip = Join-Path $root "dist\LcdPanel.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path $dist -DestinationPath $zip

$size = "{0:N0} MB" -f ((Get-Item $zip).Length / 1MB)
Write-Output ""
Write-Output "Done. Share dist\LcdPanel.zip ($size)."
