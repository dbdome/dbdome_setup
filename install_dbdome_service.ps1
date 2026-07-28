<#
  install_dbdome_service.ps1
  ---------------------------------------------------------------------------
  Installs and starts the DBDOME backend Windows service from the existing
  runtime bundle at C:\dev\grafana.dbexpert.ai\bin.

  Why this script exists:
    setup.py calls  `dbdome_service.exe --service scheduler install`  but the
    bundled dbdome_service.exe is an OLDER single-service build that does NOT
    understand `--service` (it prints "option --service not recognized").
    So it must be installed with the standard pywin32 `install` verb, which
    registers the combined service (scheduler + HTTP:8080 + Grafana watchdog).

  RUN AS ADMINISTRATOR:
    Right-click  ->  Run with PowerShell (as admin), or from an elevated
    PowerShell:   powershell -ExecutionPolicy Bypass -File C:\dev\dbdome_setup\install_dbdome_service.ps1
#>

$ErrorActionPreference = 'Stop'

# --- elevation guard ---
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "ERROR: This script must run as Administrator." -ForegroundColor Red
    Write-Host "Open PowerShell as Administrator and re-run it." -ForegroundColor Yellow
    exit 1
}

$exe = "C:\dev\grafana.dbexpert.ai\bin\dbdome_service.exe"
if (-not (Test-Path $exe)) { Write-Host "ERROR: service exe not found: $exe" -ForegroundColor Red; exit 1 }

Write-Host "==> Installing DBDOME service from:`n    $exe" -ForegroundColor Cyan

# Remove any stale registration first (ignore errors if not present)
$pre = Get-Service | Where-Object { $_.Name -like 'DBDOME*' }
foreach ($s in $pre) {
    Write-Host "    Found existing service '$($s.Name)' ($($s.Status)) - stopping & removing for a clean install."
    try { & $exe stop      | Out-Null } catch {}
    Start-Sleep 2
    try { & $exe remove    | Out-Null } catch {}
    Start-Sleep 2
}

# Install (registers the service's own _svc_name_, e.g. DBDOME_dbanalytics)
& $exe install
Start-Sleep 2

$svc = Get-Service | Where-Object { $_.Name -like 'DBDOME*' } | Select-Object -First 1
if (-not $svc) { Write-Host "ERROR: no DBDOME* service registered after install." -ForegroundColor Red; exit 1 }
$name = $svc.Name
Write-Host "==> Registered service: $name" -ForegroundColor Green

# Auto-start + auto-restart on failure (3 restarts, 5s apart, daily reset)
& sc.exe config  "$name" start= auto | Out-Null
& sc.exe failure "$name" reset= 86400 actions= restart/5000/restart/5000/restart/5000 | Out-Null

# Start it
Write-Host "==> Starting $name ..."
& $exe start
Start-Sleep 5

$svc = Get-Service $name
Write-Host ""
Write-Host "==> Result:" -ForegroundColor Cyan
$svc | Format-Table Name, Status, StartType -AutoSize

if ($svc.Status -ne 'Running') {
    Write-Host "Service is not Running. Check the Windows Event Viewer (Application log)" -ForegroundColor Yellow
    Write-Host "and the service log under C:\dev\grafana.dbexpert.ai\bin\dbdome_service.log" -ForegroundColor Yellow
    exit 1
}

# Quick port check for the HTTP backend (8080)
Start-Sleep 3
$http = Get-NetTCPConnection -State Listen -LocalPort 8080 -ErrorAction SilentlyContinue
Write-Host ("HTTP backend on :8080  -> " + $(if ($http) { 'LISTENING' } else { 'not yet (may still be warming up)' }))
Write-Host ""
Write-Host "DONE. Service '$name' installed and started." -ForegroundColor Green
