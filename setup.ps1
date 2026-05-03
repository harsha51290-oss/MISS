# ============================================================
# MIS Area Setup Script
# Run once per area on each MISS PC
#
# Usage:
#   cd C:\MIS\101
#   .\setup.ps1
# ============================================================

$ErrorActionPreference = "Stop"
$AreaDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigFile = Join-Path $AreaDir "config.json"

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  MIS Area Setup" -ForegroundColor Cyan
Write-Host "  Folder: $AreaDir" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

# -- Step 1: Validate config.json --
Write-Host "Step 1: Validating config.json..." -ForegroundColor Yellow
if (-not (Test-Path $ConfigFile)) {
    Write-Host "  ERROR: config.json not found in $AreaDir" -ForegroundColor Red
    exit 1
}
try {
    $cfg = Get-Content $ConfigFile -Raw | ConvertFrom-Json
    $AreaCode = $cfg.area.code
    $AreaName = $cfg.area.name
    $ApiPort  = $cfg.area.api_port
    Write-Host "  OK  Area: $AreaCode - $AreaName  Port: $ApiPort" -ForegroundColor Green
}
catch {
    Write-Host "  ERROR: config.json is not valid JSON" -ForegroundColor Red
    exit 1
}

# -- Step 2: Create folder structure --
Write-Host "Step 2: Creating folder structure..." -ForegroundColor Yellow
$folders = @("logs", "backups", "backups\config")
foreach ($f in $folders) {
    $path = Join-Path $AreaDir $f
    if (-not (Test-Path $path)) {
        New-Item -ItemType Directory -Path $path | Out-Null
        Write-Host "  Created: $path" -ForegroundColor Green
    }
    else {
        Write-Host "  Exists:  $path" -ForegroundColor Gray
    }
}

# -- Step 3: Check required files --
Write-Host "Step 3: Checking required files..." -ForegroundColor Yellow
$required = @("collector.py", "api.py", "backup.py", "dashboard.html", "config.html", "flow.html")
$missing = @()
foreach ($f in $required) {
    $path = Join-Path $AreaDir $f
    if (Test-Path $path) {
        Write-Host "  OK  $f" -ForegroundColor Green
    }
    else {
        Write-Host "  MISSING: $f" -ForegroundColor Red
        $missing += $f
    }
}
if ($missing.Count -gt 0) {
    Write-Host "  Copy missing files to $AreaDir and re-run setup." -ForegroundColor Red
    exit 1
}

# -- Step 4: Install Python packages --
Write-Host "Step 4: Installing Python packages..." -ForegroundColor Yellow
try {
    python -m pip install --quiet pylogix fastapi uvicorn openpyxl 2>$null
    Write-Host "  OK  Python packages installed" -ForegroundColor Green
}
catch {
    Write-Host "  WARNING: pip install failed - trying offline..." -ForegroundColor Yellow
    try {
        $pipDir = "C:\MIS_Deploy\02 pip_packages"
        if (Test-Path $pipDir) {
            python -m pip install --no-index --find-links $pipDir pylogix fastapi uvicorn openpyxl
            Write-Host "  OK  Python packages installed (offline)" -ForegroundColor Green
        }
        else {
            Write-Host "  WARNING: No offline packages found either" -ForegroundColor Yellow
        }
    }
    catch {
        Write-Host "  WARNING: Package install failed" -ForegroundColor Yellow
    }
}

# -- Step 5: Register PM2 processes --
Write-Host "Step 5: Registering PM2 processes..." -ForegroundColor Yellow

$ErrorActionPreference = "SilentlyContinue"
pm2 delete "$AreaCode-collector" 2>$null | Out-Null
pm2 delete "$AreaCode-api" 2>$null | Out-Null
pm2 delete "$AreaCode-backup" 2>$null | Out-Null
$ErrorActionPreference = "Stop"

try {
    pm2 start "$AreaDir\collector.py" --name "$AreaCode-collector" --interpreter python --log "$AreaDir\logs\collector.log" --time
    pm2 start "$AreaDir\api.py" --name "$AreaCode-api" --interpreter python --log "$AreaDir\logs\api.log" --time
    pm2 start "$AreaDir\backup.py" --name "$AreaCode-backup" --interpreter python --log "$AreaDir\logs\backup.log" --time
    Write-Host "  OK  PM2 processes registered" -ForegroundColor Green
}
catch {
    Write-Host "  WARNING: PM2 not available - start processes manually:" -ForegroundColor Yellow
    Write-Host "    python collector.py" -ForegroundColor Gray
    Write-Host "    python api.py" -ForegroundColor Gray
    Write-Host "    python backup.py" -ForegroundColor Gray
}

# -- Step 6: Save PM2 --
Write-Host "Step 6: Saving PM2 config..." -ForegroundColor Yellow
$ErrorActionPreference = "SilentlyContinue"
pm2 save 2>$null | Out-Null
pm2-startup install 2>$null | Out-Null
$ErrorActionPreference = "Stop"
Write-Host "  OK  PM2 saved" -ForegroundColor Green

# -- Done --
Write-Host ""
Write-Host "============================================" -ForegroundColor Green
Write-Host "  SETUP COMPLETE - $AreaCode $AreaName" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Dashboard : http://localhost:${ApiPort}/dashboard" -ForegroundColor Cyan
Write-Host "  Config    : http://localhost:${ApiPort}/config" -ForegroundColor Cyan
Write-Host "  Health    : http://localhost:${ApiPort}/api/health" -ForegroundColor Cyan
Write-Host "  Flow      : http://localhost:${ApiPort}/flow" -ForegroundColor Cyan
Write-Host ""
Write-Host "  PM2 status: pm2 list" -ForegroundColor Gray
Write-Host "  View logs : pm2 logs ${AreaCode}-collector" -ForegroundColor Gray
Write-Host ""
