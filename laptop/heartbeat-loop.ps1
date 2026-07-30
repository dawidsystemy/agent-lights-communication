# heartbeat-loop.ps1 - runs a heartbeat every 15 s (Task Scheduler at-logon).
# The body of a single beat is heartbeat.ps1 (same as push-now.ps1 fires immediately).
# A global mutex guards against duplicate loops - a second instance exits at once.
$ErrorActionPreference = 'SilentlyContinue'
$mutex = New-Object System.Threading.Mutex($false, 'Global\AgentLights-Heartbeat-Loop')
if (-not $mutex.WaitOne(0)) { exit 0 }   # another loop instance is already running
try {
    $single = Join-Path $PSScriptRoot 'heartbeat.ps1'
    while ($true) {
        & $single
        Start-Sleep -Seconds 15
    }
} finally {
    $mutex.ReleaseMutex()
}
