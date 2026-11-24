# Installs the newest Rhapsody Model Assistant VSIX found next to this script
# (or in .\dist) into VS Code. The VSIX contains its own Python and packages,
# so no Python, pip or virtual environment is needed.
$ROOT = $PSScriptRoot
Write-Host "Rhapsody Model Assistant - VSIX install" -ForegroundColor Cyan
Write-Host ""

# 1. Newest VSIX (by version in the file name, then by date)
$vsix = @(Get-ChildItem -Path $ROOT, (Join-Path $ROOT "dist") -Filter "*.vsix" -File -ErrorAction SilentlyContinue |
          Sort-Object @{ Expression = {
                            if ($_.Name -match "(\d+)\.(\d+)\.(\d+)") {
                                [int]$Matches[1] * 1000000 + [int]$Matches[2] * 1000 + [int]$Matches[3]
                            } else { 0 } }; Descending = $true },
                      @{ Expression = { $_.LastWriteTime }; Descending = $true })
if ($vsix.Count -eq 0) {
    Write-Host "  No .vsix file found in $ROOT or $ROOT\dist" -ForegroundColor Red
    exit 2
}
$file = $vsix[0].FullName
Write-Host "[1/3] VSIX: $($vsix[0].Name)"

# 2. VS Code command line ('code')
$code = $null
$cmd = Get-Command code -ErrorAction SilentlyContinue
if ($cmd) { $code = $cmd.Source }
if (-not $code) {
    foreach ($p in @("$env:LOCALAPPDATA\Programs\Microsoft VS Code\bin\code.cmd",
                     "$env:ProgramFiles\Microsoft VS Code\bin\code.cmd",
                     "${env:ProgramFiles(x86)}\Microsoft VS Code\bin\code.cmd")) {
        if ($p -and (Test-Path $p)) { $code = $p; break }
    }
}
if (-not $code) {
    Write-Host "  VS Code command line ('code') not found." -ForegroundColor Red
    Write-Host "  Install the VSIX manually: VS Code -> Extensions view -> '...' menu ->"
    Write-Host "  'Install from VSIX...' -> $file"
    exit 1
}
Write-Host "[2/3] VS Code CLI: $code"

# 3. Remove folder copies made by the developer setup (same extension identity,
#    VS Code would load only one of them), then install the VSIX
$EXT_ROOT = Join-Path $env:USERPROFILE ".vscode\extensions"
Get-ChildItem $EXT_ROOT -Directory -ErrorAction SilentlyContinue |
    Where-Object { ($_.Name -like "oss.rhapsody-dd-Assist-*" -or $_.Name -like "oss.rhapsody-design-assistant-*") -and
                   $_.Name -notmatch "-win32-|-linux-|-darwin-" } |
    ForEach-Object {
        Write-Host "  Removing developer copy: $($_.Name)"
        Remove-Item $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
    }

Write-Host "[3/3] Installing (this can take a minute - the VSIX contains Python)..."
& $code --install-extension $file --force
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ERROR: installation failed (exit code $LASTEXITCODE)." -ForegroundColor Red
    Write-Host "  Try manually: VS Code -> Extensions view -> '...' -> 'Install from VSIX...'"
    exit 1
}

if (-not (Test-Path "C:\RhapsodyAIAgent_runtime")) {
    New-Item -ItemType Directory -Path "C:\RhapsodyAIAgent_runtime" -Force | Out-Null
}

Write-Host ""
Write-Host "Installed!" -ForegroundColor Green
Write-Host "Next steps:"
Write-Host "  1. In VS Code: Ctrl+Shift+P -> Developer: Reload Window (or restart VS Code)"
Write-Host "  2. Open Rhapsody with your project"
Write-Host "  3. In VS Code chat: @rhapsody /design <ComponentName>"
exit 0
