# RhapsodyAIAgent Setup Script
# Run this once to install everything
# Usage: Right-click setup.ps1 → Run with PowerShell

$ErrorActionPreference = "Stop"
$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "═══════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "  RhapsodyAIAgent — One-Click Setup" -ForegroundColor Cyan
Write-Host "═══════════════════════════════════════════════" -ForegroundColor Cyan

# ── 1. Check Python ──────────────────────────────────────────────────────────
Write-Host "`n[1/5] Checking Python..." -ForegroundColor Yellow
try {
    $pyver = python --version 2>&1
    Write-Host "  ✅ $pyver" -ForegroundColor Green
} catch {
    Write-Host "  ❌ Python not found. Install from https://python.org" -ForegroundColor Red
    exit 1
}

# ── 2. Check Node.js ─────────────────────────────────────────────────────────
Write-Host "`n[2/5] Checking Node.js..." -ForegroundColor Yellow
try {
    $nodever = node --version 2>&1
    Write-Host "  ✅ Node.js $nodever" -ForegroundColor Green
} catch {
    Write-Host "  ❌ Node.js not found. Install from https://nodejs.org" -ForegroundColor Red
    exit 1
}

# ── 3. Python virtual environment ────────────────────────────────────────────
Write-Host "`n[3/5] Setting up Python virtual environment..." -ForegroundColor Yellow
$venv = Join-Path $ROOT ".venv"
if (-not (Test-Path $venv)) {
    python -m venv $venv
    Write-Host "  ✅ Created .venv" -ForegroundColor Green
} else {
    Write-Host "  ✅ .venv already exists" -ForegroundColor Green
}

$pip = Join-Path $venv "Scripts\pip.exe"
$python = Join-Path $venv "Scripts\python.exe"

Write-Host "  Installing Python packages..." -ForegroundColor Yellow
& $pip install -r (Join-Path $ROOT "requirements.txt") --quiet
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ❌ pip install failed" -ForegroundColor Red
    exit 1
}

# Install pywin32 post-install (required for COM)
Write-Host "  Running pywin32 post-install..." -ForegroundColor Yellow
& $python (Join-Path $venv "Scripts\pywin32_postinstall.py") -install 2>&1 | Out-Null
Write-Host "  ✅ Python packages installed" -ForegroundColor Green

# ── 4. VS Code extension ─────────────────────────────────────────────────────
Write-Host "`n[4/5] Installing VS Code extension..." -ForegroundColor Yellow

# Find VS Code extensions directory
$extBase = Join-Path $env:USERPROFILE ".vscode\extensions"
$extDir  = Join-Path $extBase "oss.rhapsody-dd-Assist-0.0.2"

if (-not (Test-Path $extDir)) {
    New-Item -ItemType Directory -Path $extDir -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $extDir "src") -Force | Out-Null
}

# Copy extension files
$srcDir = Join-Path $ROOT "src"
Copy-Item (Join-Path $srcDir "extension.js") (Join-Path $extDir "src\extension.js") -Force
Copy-Item (Join-Path $srcDir "package.json") (Join-Path $extDir "package.json") -Force

# Install npm dependencies
Write-Host "  Installing npm packages..." -ForegroundColor Yellow
Push-Location $extDir
npm install express@4 --save --quiet 2>&1 | Out-Null
Pop-Location
Write-Host "  ✅ VS Code extension installed" -ForegroundColor Green

# ── 5. Runtime directory ─────────────────────────────────────────────────────
Write-Host "`n[5/5] Creating runtime directory..." -ForegroundColor Yellow
$runtime = "C:\RhapsodyAIAgent_runtime"
if (-not (Test-Path $runtime)) {
    New-Item -ItemType Directory -Path $runtime -Force | Out-Null
}
Write-Host "  ✅ Runtime dir: $runtime" -ForegroundColor Green

# ── Write config file ─────────────────────────────────────────────────────────
$config = @{
    tools_path  = Join-Path $ROOT "tools"
    python_path = $python
    runtime_dir = $runtime
} | ConvertTo-Json

$config | Set-Content (Join-Path $ROOT "config.json")

# Update extension.js to point to correct paths
$extJs = Join-Path $extDir "src\extension.js"
$content = Get-Content $extJs -Raw
$content = $content -replace "const ROOT_DIR = .*", "const ROOT_DIR = '$($ROOT -replace '\\','\\\\')'; // auto-configured"
$content = $content -replace "const PYTHON = .*", "const PYTHON = '$($python -replace '\\','\\\\')'; // auto-configured"
Set-Content $extJs $content

Write-Host "`n═══════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "  ✅ Setup Complete!" -ForegroundColor Green
Write-Host "═══════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "`nNext steps:" -ForegroundColor White
Write-Host "  1. Reload VS Code window (Ctrl+Shift+P → Developer: Reload Window)"
Write-Host "  2. Open Rhapsody with your project"
Write-Host "  3. In VS Code chat: @rhapsody /design <ComponentName>"
Write-Host "`nSee INSTALLATION.md for details."
