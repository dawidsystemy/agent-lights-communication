# watchdog.ps1 - guards the heartbeat loop. Register as a Task Scheduler task that runs every
# minute (see the README "Reliability" section for the Register-ScheduledTask command).
#
# Why: the heartbeat loop (heartbeat-loop.ps1) can die mid-session, and a logon-triggered task only
# restarts at the next logon - which freezes the LED on its last state (a "the light is lying"
# failure mode). This checks the heartbeat log for a recent TICK line ("me=.. subs=.."); no tick for
# more than $maxAgeSec = the loop is dead -> restart it and log the restart.
# NOTE: only real TICK lines count, never the watchdog's own "watchdog:" line, so a dead loop can't
# mask itself. The loop's own mutex prevents a double start if it was merely slow.
$ErrorActionPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'agent-lights-config.ps1')

$logFile   = Join-Path $AL_StateDir 'heartbeat.log'
$maxAgeSec = 120
$loopTask  = 'AgentLights-Heartbeat'   # Task Scheduler task that runs heartbeat-loop.ps1

$stale = $true
if (Test-Path $logFile) {
    $lines = Get-Content $logFile -Tail 30 -ErrorAction SilentlyContinue
    $tick  = $lines | Where-Object { $_ -match 'me=\d+ subs=\d+' } | Select-Object -Last 1
    if ($tick -and $tick -match '^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})') {
        $ts = [datetime]::ParseExact($Matches[1], 'yyyy-MM-dd HH:mm:ss', $null)
        if (((Get-Date) - $ts).TotalSeconds -le $maxAgeSec) { $stale = $false }
    }
}
if ($stale) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') watchdog: restarting the heartbeat loop" | Out-File -FilePath $logFile -Append -Encoding utf8
    Start-ScheduledTask -TaskName $loopTask
}
