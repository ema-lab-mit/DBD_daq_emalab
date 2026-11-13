<#
  start_beamline.ps1  --  RUN ON THE BEAMLINE / DAQ COMPUTER (10.54.5.139, user 'EMALAB')

  Points EPICS at the laser-lab IOC, checks the laser + wavemeter are live,
  then launches the DAQ. Run start_laserlab.ps1 on 10.54.6.1 FIRST.
#>

$ErrorActionPreference = 'Stop'

$Daq     = 'C:\Users\EMALAB\Desktop\DBD_daq_emalab\main.py'
$IocHost = '10.54.6.1'

# Lab switch blocks UDP broadcast -> point Channel Access straight at the IOC host.
$env:EPICS_CA_ADDR_LIST      = $IocHost
$env:EPICS_CA_AUTO_ADDR_LIST = 'NO'

Write-Host '== Checking laser server (10.54.6.1:8000) ==' -ForegroundColor Cyan
$ping = python -c "import xmlrpc.client as x; print(x.ServerProxy('http://10.54.6.1:8000').ping())"
Write-Host "   laser ping = $ping"
if ($ping -ne 'True') {
  Write-Host '   ERROR: laser server not reachable. Start start_laserlab.ps1 on 10.54.6.1 first. Aborting.' -ForegroundColor Red
  return
}

Write-Host '== Checking wavemeter PV ==' -ForegroundColor Cyan
$wn = python -c "import epics; print(epics.caget('LaserLab:wavenumber_1'))"
Write-Host "   LaserLab:wavenumber_1 = $wn"
if (-not $wn -or $wn -eq 'None' -or $wn -match '^0\.0+$') {
  Write-Host '   ERROR: wavenumber reads 0.0/None -- wavemeter IOC not serving. Data would log 0.0 SILENTLY.' -ForegroundColor Red
  Write-Host '   Fix the laser-lab side (HighFinesse WLM + wavemeter_testing.py) before scanning. Aborting.' -ForegroundColor Red
  return
}

Write-Host '== All checks passed. Launching DAQ ==' -ForegroundColor Green
Set-Location (Split-Path $Daq)
python $Daq
