<#
  start_laserlab.ps1  --  RUN ON THE LASER-LAB COMPUTER (10.54.6.1, user 'admin')

  Brings up the two background servers the DAQ depends on:
    1. laser_server.py    XML-RPC :8000  ->  Matisse Commander :30000
    2. wavemeter_testing.py  pcaspy IOC     ->  LaserLab:wavenumber_1..4 (HighFinesse)

  Each launches in its OWN window and must stay open for the whole run.

  DO THESE BY HAND FIRST (this script does not):
    - Matisse Commander running; Comm Options > Enable Server (port 30000)
    - Position Display Mode = nm  AND  CounterDrift Unit = nm   (cm^-1 = laser off target)
    - Laser CD-positioned at the start wavenumber, then CD turned OFF, CD tab closed
    - HighFinesse WLM software running (the IOC reads its DLL)
#>

$ErrorActionPreference = 'Stop'

# --- EDIT IF PATHS DIFFER ON THIS MACHINE ---------------------------------
$LaserServer  = 'C:\Users\admin\Documents\ControllerC663_testing\laser_server.py'
$WavemeterIoc = 'C:\Users\admin\Documents\Wavemeter\wavemeter_testing.py'
# --------------------------------------------------------------------------

if (-not (Test-Path $LaserServer))  { Write-Host "MISSING: $LaserServer"  -ForegroundColor Red; return }
if (-not (Test-Path $WavemeterIoc)) { Write-Host "MISSING: $WavemeterIoc (edit `$WavemeterIoc in this script)" -ForegroundColor Red; return }

Write-Host '== Clearing stale Python / freeing port 8000 ==' -ForegroundColor Cyan
Get-Process python, pythonw -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 500

# 1. Laser bridge ----------------------------------------------------------
Write-Host '== Starting laser_server.py ==' -ForegroundColor Cyan
$serverDir = Split-Path $LaserServer
Start-Process powershell -ArgumentList @(
  '-NoExit','-Command',
  "`$env:MATISSE_HOST='127.0.0.1'; `$env:MATISSE_PORT='30000'; `$env:SIMULATION='0'; Set-Location '$serverDir'; python '$LaserServer'"
)

Write-Host '   waiting for port 8000 ' -NoNewline
$up = $false
for ($i = 0; $i -lt 40; $i++) {
  if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) { $up = $true; break }
  Start-Sleep -Milliseconds 500; Write-Host '.' -NoNewline
}
Write-Host ''
if (-not $up) { Write-Host '   WARNING: port 8000 never came up -- check the laser_server window.' -ForegroundColor Yellow }

# 2. Wavemeter EPICS IOC ---------------------------------------------------
Write-Host '== Starting wavemeter_testing.py (EPICS IOC) ==' -ForegroundColor Cyan
$iocDir = Split-Path $WavemeterIoc
Start-Process powershell -ArgumentList @(
  '-NoExit','-Command',
  "Set-Location '$iocDir'; python '$WavemeterIoc'"
)
Start-Sleep -Seconds 3

# 3. Verify ----------------------------------------------------------------
Write-Host '== Verifying ==' -ForegroundColor Cyan
try { python -c "import xmlrpc.client as x; print('  laser ping   :', x.ServerProxy('http://127.0.0.1:8000').ping())" } catch { Write-Host '  laser ping   : FAILED' -ForegroundColor Red }
try { python -c "import epics; print('  wavenumber_1 :', epics.caget('LaserLab:wavenumber_1'))" } catch { Write-Host '  wavenumber_1 : FAILED' -ForegroundColor Red }

Write-Host ''
Write-Host 'Two server windows are now open -- LEAVE THEM OPEN for the whole run.' -ForegroundColor Green
Write-Host 'If wavenumber_1 is 0.0 / None, the HighFinesse WLM software is not running.' -ForegroundColor Yellow
Write-Host 'Now run start_beamline.ps1 on the beamline computer (10.54.5.139).' -ForegroundColor Green
