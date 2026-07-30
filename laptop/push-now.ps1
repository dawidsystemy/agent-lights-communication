# push-now.ps1 - send an immediate heartbeat after any counter change,
# so the LED updates in <20 s without waiting for the 15 s loop.
& (Join-Path $PSScriptRoot 'heartbeat.ps1')
Write-Host 'push-now: heartbeat sent to the station.'
