# RhapsodyAIAgent Setup
$ROOT = $PSScriptRoot
Write-Host "RhapsodyAIAgent Setup" -ForegroundColor Cyan
Write-Host "Root: $ROOT"
Write-Host ""

# Step 1: Python
Write-Host "[1/5] Checking Python..." -ForegroundColor Yellow
$PYTHON = $null
foreach ($cmd in "python3","python") {
    try {
        $ver = & $cmd --version 2>&1
        if ($LASTEXITCODE -eq 0) { $PYTHON = $cmd; Write-Host "  OK: $ver"; break }
    } catch {}
}
if (-not $PYTHON) {
    Write-Host "  ERROR: Python not found. Install Python 3.11 from https://python.org" -ForegroundColor Red
    Read-Host "Press Enter to exit"; exit 1
}

# Step 2: Virtual environment
Write-Host ""
Write-Host "[2/5] Setting up Python environment..." -ForegroundColor Yellow
$VENV = Join-Path $ROOT ".venv"
$PIP = Join-Path $VENV "Scripts\pip.exe"
$PY = Join-Path $VENV "Scripts\python.exe"
$USING_VENV = $false

if (-not (Test-Path $VENV)) {
    & $PYTHON -m venv $VENV 2>&1 | Out-Null
}

if (Test-Path $PY) {
    $USING_VENV = $true
    Write-Host "  OK: Using virtual environment"
} else {
    Write-Host "  WARNING: venv not available, using system Python" -ForegroundColor DarkYellow
    $PY = (Get-Command $PYTHON -ErrorAction SilentlyContinue).Source
    if (-not $PY) { $PY = $PYTHON }
    $PIP = "$PYTHON -m pip"
}

Write-Host "  Installing packages (may take 1-2 minutes)..."
$REQ = Join-Path $ROOT "requirements.txt"
if ($USING_VENV) {
    & $PIP install -r $REQ --quiet
} else {
    & $PYTHON -m pip install -r $REQ --user --quiet
}
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ERROR: pip install failed" -ForegroundColor Red
    Read-Host "Press Enter to exit"; exit 1
}
Write-Host "  OK: Packages installed"

$POSTINSTALL = Join-Path $VENV "Scripts\pywin32_postinstall.py"
if (Test-Path $POSTINSTALL) {
    & $PY $POSTINSTALL 2>&1 | Out-Null
    Write-Host "  OK: pywin32 configured"
}

# Step 3: VS Code extension
Write-Host ""
Write-Host "[3/5] Installing VS Code extension..." -ForegroundColor Yellow
$EXT = Join-Path $env:USERPROFILE ".vscode\extensions\oss.rhapsody-dd-Assist-0.0.2"
$EXTSRC = Join-Path $EXT "src"
New-Item -ItemType Directory -Path $EXTSRC -Force | Out-Null

$SRC = Join-Path $ROOT "src"
Copy-Item (Join-Path $SRC "extension.js") (Join-Path $EXTSRC "extension.js") -Force
Copy-Item (Join-Path $SRC "package.json") (Join-Path $EXT "package.json") -Force
Write-Host "  OK: Extension files copied"

# node_modules
$ZIP = Join-Path $SRC "node_modules.zip"
$FOLDER = Join-Path $SRC "node_modules"
$DEST = Join-Path $EXT "node_modules"

if (Test-Path $ZIP) {
    Write-Host "  Extracting node_modules.zip..."
    Expand-Archive -Path $ZIP -DestinationPath $EXT -Force
    Write-Host "  OK: node_modules extracted"
} elseif (Test-Path $FOLDER) {
    Write-Host "  Copying node_modules..."
    Copy-Item $FOLDER $DEST -Recurse -Force
    Write-Host "  OK: node_modules copied"
} else {
    Write-Host "  WARNING: No node_modules found. npm install may be needed." -ForegroundColor DarkYellow
}

# Step 4: Runtime directory
Write-Host ""
Write-Host "[4/5] Creating runtime directory..." -ForegroundColor Yellow
$RUNTIME = "C:\RhapsodyAIAgent_runtime"
if (-not (Test-Path $RUNTIME)) {
    New-Item -ItemType Directory -Path $RUNTIME -Force | Out-Null
}
Write-Host "  OK: $RUNTIME"

# Step 5: Config
Write-Host ""
Write-Host "[5/5] Writing config..." -ForegroundColor Yellow
$TOOLS = (Join-Path $ROOT "tools") -replace "\\","\\\\"
$PYESC = $PY -replace "\\","\\\\"
$RESC  = $RUNTIME -replace "\\","\\\\"
"{`"tools_path`":`"$TOOLS`",`"python_path`":`"$PYESC`",`"runtime_dir`":`"$RESC`"}" | Set-Content (Join-Path $ROOT "config.json") -Encoding UTF8
Write-Host "  OK: config.json written"

$EXTJS = Join-Path $EXTSRC "extension.js"
$JS = Get-Content $EXTJS -Raw
$PYESC2 = $PY -replace "\\","\\\\"
$JS = $JS -replace "const PYTHON = [^;`r`n]+;","const PYTHON = '$PYESC2';"
Set-Content $EXTJS $JS -Encoding UTF8
Write-Host "  OK: extension.js patched"

Write-Host ""
Write-Host "Setup Complete!" -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Open VS Code"
Write-Host "  2. Ctrl+Shift+P -> Developer: Reload Window"
Write-Host "  3. Open Rhapsody with your project"
Write-Host "  4. In VS Code chat: @rhapsody /design <ComponentName>"
Write-Host ""
Read-Host "Press Enter to close"
