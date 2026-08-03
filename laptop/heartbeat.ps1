# heartbeat.ps1 - laptop -> station heartbeat for the Agent-Lights-Communication indicator.
# Sends a small JSON blob: me (orchestrator sessions), subs (subagents/background tasks),
# waiting (queued decision cards), prompt (blocking prompts on screen), mon (per-monitor
# session state) and a UTC timestamp. Run once per minute (Task Scheduler) or in a loop
# (heartbeat-loop.ps1); push-now.ps1 calls this immediately after any counter change.
#
# Reliability (self-healing, see README "Reliability"):
#   - working.count self-clean: a positive value untouched for >= 3 h is an orphaned counter from a
#     truncated session -> zeroed.
#   - live-subagent auto-count from transcripts: subs = max(auto, working.count), so a parallel
#     session that never bumped working.count still lights the right number of dips.
#   - me-marker liveness judged from the session transcript's freshness, not the marker's own mtime,
#     so a stuck marker can't keep the light blue after the session is gone.
# The transcript-based features need $AL_ClaudeProjectsDir; when it's empty/absent, plain
# mtime-based fallbacks run instead.
$ErrorActionPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'agent-lights-config.ps1')

$stateDir = $AL_StateDir
$waitFile = Join-Path $stateDir 'waiting.count'
$logFile  = Join-Path $stateDir 'heartbeat.log'
$useTranscript = ($AL_ClaudeProjectsDir -and (Test-Path $AL_ClaudeProjectsDir))

# 1) Two work fields for the laptop:
#    subs = number of live subagents / background tasks, merged from two sources with max():
#           (a) working.count = a manual override written by the orchestrator (with a 3 h staleness
#               self-clean below), and
#           (b) an auto-count of fresh subagent transcripts (mtime < 180 s).
#           The manual file can only push the count UP (the orchestrator may know about work the
#           auto-count can't see yet). Processes are used ONLY if working.count is missing entirely.
#    me   = me markers = number of active orchestrator sessions. Each session keeps its own marker in
#           me/<session>.flag (written by the Claude Code hook), judged live below.
$wcFile = Join-Path $stateDir 'working.count'
# (a) manual override, with a staleness self-clean.
$wcManual = 0
$wcHasFile = Test-Path $wcFile
if ($wcHasFile) {
    $raw = (Get-Content $wcFile -Raw).Trim()
    if ($raw -match '^\d+$') { $wcManual = [int]$raw }
    # SELF-CLEAN: a positive value in a file untouched for >= 3 h is an orphaned counter left by a
    # truncated session. Zero it and log. Set-Content refreshes the mtime, so this won't re-trigger.
    if ($wcManual -gt 0) {
        $wcAgeH = ((Get-Date) - (Get-Item $wcFile).LastWriteTime).TotalHours
        if ($wcAgeH -ge 3) {
            "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') selfclean: working $wcManual->0, stale $([math]::Round($wcAgeH,1))h" | Out-File -FilePath $logFile -Append -Encoding utf8
            Set-Content -Path $wcFile -Value '0' -NoNewline -Encoding utf8
            $wcManual = 0
        }
    }
}
# (b) auto-count of LIVE subagents = fresh subagent transcripts. Each subagent gets its own
#     <projects>\<slug>\<orchestrator-uuid>\subagents\agent-<name>-<hex>.jsonl; a live agent appends to
#     it within seconds, a finished/idle one lets the mtime age out (so N dips = N working units, an
#     idle session doesn't light). Fixed-depth glob (no -Recurse). Scanning across all orchestrators
#     catches subagents of a parallel session that never bumped working.count.
$autoSubs = 0
$scanMs = 0
if ($useTranscript) {
    $projGlob = Join-Path $AL_ClaudeProjectsDir '*\*\subagents\agent-*.jsonl'
    $scanSw = [System.Diagnostics.Stopwatch]::StartNew()
    $cutSubs = (Get-Date).AddSeconds(-180)
    $autoSubs = @(Get-ChildItem -Path $projGlob -File -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -gt $cutSubs }).Count
    $scanSw.Stop()
    $scanMs = [int]$scanSw.ElapsedMilliseconds
}
# (c) merge: the manual override only ever raises the count.
if ($wcHasFile) {
    $subs = [math]::Max($autoSubs, $wcManual)
} else {
    $anyClaude = @(Get-Process -Name claude -ErrorAction SilentlyContinue).Count
    $subs = if ($anyClaude -gt 0) { [math]::Max($autoSubs, 1) } else { $autoSubs }
}

$me = 0
$meDir = Join-Path $stateDir 'me'
if (Test-Path $meDir) {
    if ($useTranscript) {
        # SELF-CLEAN by TRANSCRIPT freshness (not marker mtime): a marker can outlive a truncated
        # session if something keeps touching it, so liveness is read from the session's transcript
        # <sid>.jsonl instead. sid = marker filename without .flag.
        #   no transcript       -> ghost session -> remove the marker (log if it was "1").
        #   marker != "1"       -> not counted (idle / already zeroed).
        #   transcript > 10 min -> the session isn't churning (truncated / died without Stop) -> zero it
        #                          (the session may still come back).
        #   transcript fresh    -> a live session -> count it.
        $cutTx = (Get-Date).AddMinutes(-10)
        $meFlags = @(Get-ChildItem -Path $meDir -Filter *.flag -File -ErrorAction SilentlyContinue)
        foreach ($mfl in $meFlags) {
            $sid = $mfl.BaseName
            $val = ''
            try { $val = (Get-Content $mfl.FullName -Raw -ErrorAction SilentlyContinue).Trim() } catch {}
            $txItem = @(Get-ChildItem -Path (Join-Path $AL_ClaudeProjectsDir ('*\' + $sid + '.jsonl')) -File -ErrorAction SilentlyContinue |
                Sort-Object LastWriteTime -Descending | Select-Object -First 1)
            if ($txItem.Count -eq 0) {
                if ($val -eq '1') { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') selfclean: me-marker $sid removed (no transcript)" | Out-File -FilePath $logFile -Append -Encoding utf8 }
                Remove-Item $mfl.FullName -Force -ErrorAction SilentlyContinue
                continue
            }
            if ($val -ne '1') { continue }
            if ($txItem[0].LastWriteTime -gt $cutTx) {
                $me++
            } else {
                "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') selfclean: me-marker $sid zeroed (transcript stale >10min)" | Out-File -FilePath $logFile -Append -Encoding utf8
                Set-Content -Path $mfl.FullName -Value '0' -NoNewline -Encoding utf8
            }
        }
    } else {
        # Fallback (no Claude projects dir): mtime-based staleness, 60 min.
        $cutMe = (Get-Date).AddMinutes(-60)
        $meFlags = @(Get-ChildItem -Path $meDir -Filter *.flag -File -ErrorAction SilentlyContinue)
        foreach ($mfl in $meFlags) {
            if ($mfl.LastWriteTime -le $cutMe) { Remove-Item $mfl.FullName -Force -ErrorAction SilentlyContinue }
        }
        $me = @($meFlags | Where-Object { $_.LastWriteTime -gt $cutMe }).Count
    }
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

# The log line must keep the "me=.. subs=.." prefix: watchdog.ps1 greps for it as the tick marker.
"$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') me=$me subs=$subs (auto=$autoSubs man=$wcManual) wait=$wait rc=$rc scanMs=$scanMs" | Out-File -FilePath $logFile -Append -Encoding utf8
