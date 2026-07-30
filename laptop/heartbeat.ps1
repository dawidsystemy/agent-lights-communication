# heartbeat.ps1 - laptop -> station heartbeat for the Agent-Lights-Communication indicator.
# Sends a small JSON blob: me (orchestrator sessions), subs (subagents/background tasks),
# waiting (queued decision cards), prompt (blocking prompts on screen), mon (per-monitor
# session state) and a UTC timestamp. Run once per minute (Task Scheduler) or in a loop
# (heartbeat-loop.ps1); push-now.ps1 calls this immediately after any counter change.
$ErrorActionPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'agent-lights-config.ps1')

$stateDir = $AL_StateDir
$waitFile = Join-Path $stateDir 'waiting.count'
$logFile  = Join-Path $stateDir 'heartbeat.log'

# 1) Two work fields for the laptop:
#    subs = working.count = number of subagents / background tasks. A binding file with no expiry
#           ("0" means 0 until something writes otherwise); guessed from processes ONLY if the file
#           is missing entirely.
#    me   = me markers = number of active orchestrator sessions. Each session keeps its own marker in
#           me/<session>.flag (written by the Claude Code hook). Staleness 60 min. Falls back to a
#           single me.flag if the directory is absent.
$wcFile = Join-Path $stateDir 'working.count'
$subs = 0
if (Test-Path $wcFile) {
    $raw = (Get-Content $wcFile -Raw).Trim()
    if ($raw -match '^\d+$') { $subs = [int]$raw }
} else {
    $anyClaude = @(Get-Process -Name claude -ErrorAction SilentlyContinue).Count
    $subs = if ($anyClaude -gt 0) { 1 } else { 0 }
}
$me = 0
$meDir = Join-Path $stateDir 'me'
if (Test-Path $meDir) {
    $cutMe = (Get-Date).AddMinutes(-60)
    $meFlags = @(Get-ChildItem -Path $meDir -Filter *.flag -File -ErrorAction SilentlyContinue)
    foreach ($mfl in $meFlags) {
        if ($mfl.LastWriteTime -le $cutMe) { Remove-Item $mfl.FullName -Force -ErrorAction SilentlyContinue }
    }
    $me = @($meFlags | Where-Object { $_.LastWriteTime -gt $cutMe }).Count
} else {
    $meFile = Join-Path $stateDir 'me.flag'
    if (Test-Path $meFile) {
        $mf = (Get-Content $meFile -Raw).Trim()
        if ($mf -match '^\d+$') { $me = [int]$mf }
    }
}

# 2) Two separate "waiting" components (the daemon sums them):
#    a) waiting = waiting.count = queued decision cards,
#    b) prompt  = number of active prompts/*.flag markers (per session) + red-extra.count.
$wait = 0
if (Test-Path $waitFile) {
    $raw = (Get-Content $waitFile -Raw).Trim()
    if ($raw -match '^\d+$') { $wait = [int]$raw }
}
# prompt markers: staleness 30 min (a marker older than that = a session died without cleanup -> drop it).
$prompt = 0
$promptsDir = Join-Path $stateDir 'prompts'
if (Test-Path $promptsDir) {
    $cut = (Get-Date).AddMinutes(-30)
    $flags = @(Get-ChildItem -Path $promptsDir -Filter *.flag -File -ErrorAction SilentlyContinue)
    foreach ($f in $flags) {
        if ($f.LastWriteTime -le $cut) { Remove-Item $f.FullName -Force -ErrorAction SilentlyContinue }
    }
    $prompt = @($flags | Where-Object { $_.LastWriteTime -gt $cut }).Count
}
$redExtraFile = Join-Path $stateDir 'red-extra.count'
if (Test-Path $redExtraFile) {
    $reRaw = (Get-Content $redExtraFile -Raw).Trim()
    if ($reRaw -match '^\d+$') { $prompt += [int]$reRaw }
}

# 2b) PROFILE A: per-monitor state from me markers + a session->monitor map (sid_prefix=monitor_index).
#     mon[i]=1 when a fresh session (me/<sid>.flag < 60 min) maps to monitor i. Default 4 monitors.
$monCount = 4
$mon = New-Object 'int[]' $monCount
$mapFile = Join-Path $stateDir 'session-monitor.map'
if ((Test-Path $meDir) -and (Test-Path $mapFile)) {
    $map = @{}
    foreach ($line in (Get-Content $mapFile -ErrorAction SilentlyContinue)) {
        $l = $line.Trim()
        if ($l -and -not $l.StartsWith('#') -and $l.Contains('=')) {
            $kv = $l.Split('=', 2)
            $mi = 0; if ([int]::TryParse($kv[1].Trim(), [ref]$mi)) { $map[$kv[0].Trim()] = $mi }
        }
    }
    $cutMe2 = (Get-Date).AddMinutes(-60)
    $freshMe = @(Get-ChildItem -Path $meDir -Filter *.flag -File -ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTime -gt $cutMe2 })
    foreach ($f in $freshMe) {
        $sid = $f.BaseName
        foreach ($k in $map.Keys) {
            if ($sid.StartsWith($k)) { $mi = $map[$k]; if ($mi -ge 0 -and $mi -lt $monCount) { $mon[$mi] = 1 } }
        }
    }
}
$monJson = '[' + ($mon -join ',') + ']'

# 3) UTC epoch timestamp (the station uses it for staleness: >5 min = laptop offline).
#    Note: `Get-Date -UFormat %s` has a bug (counts from local time), so use DateTimeOffset UTC.
$ts = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()

$json = '{"me":' + $me + ',"subs":' + $subs + ',"waiting":' + $wait + ',"prompt":' + $prompt + ',"mon":' + $monJson + ',"ts":' + $ts + '}'

# 4) Push to the station (create the tmpfs dir + write the file).
$json | ssh -i $AL_Key -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new $AL_Host "mkdir -p $AL_RemoteRunDir && cat > $AL_RemoteRunDir/hb-laptop" 2>$null
$rc = $LASTEXITCODE

"$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') me=$me subs=$subs wait=$wait rc=$rc" | Out-File -FilePath $logFile -Append -Encoding utf8
