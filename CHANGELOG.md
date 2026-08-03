# Changelog

All notable changes to Agent-Lights-Communication are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-08-03

Reliability release. Four real-world failure modes - found by running this daily in production -
fixed so the light heals itself instead of getting stuck or lying.

### Added
- Loop watchdog (`laptop/watchdog.ps1`): checks the heartbeat log for a recent tick and restarts
  the heartbeat loop within a minute if it went silent, instead of waiting for the next logon.
  README documents registering it (and the loop) as named Task Scheduler tasks.
- `PreToolUse` hook (`examples/session-signal-pretooluse.py`): lights the working marker on any
  tool call, so turns woken by a background agent's message or a notification (which skip
  `UserPromptSubmit`) still show blue instead of running "after green".
- `$AL_ClaudeProjectsDir` config value pointing at the Claude Code projects/transcripts directory,
  used by the two transcript-based self-healing features.
- README "Reliability" section documenting the four self-healing behaviours.

### Changed
- `heartbeat.ps1` self-cleans an orphaned `working.count` (a positive value left untouched for
  3 h by a truncated session is zeroed).
- `heartbeat.ps1` judges orchestrator-session (`me`) markers live from the session transcript's
  freshness rather than the marker file's own mtime, so a stuck marker can't pin the light blue.
- `heartbeat.ps1` auto-counts live subagents from their transcripts (mtime < 3 min) and merges
  that with the manual `working.count` via `max()`, so a parallel session that never bumped the
  counter still lights the right number of dips. Both transcript-based features fall back to the
  previous mtime-based behaviour when `$AL_ClaudeProjectsDir` is empty or absent.

## [0.1.0] - 2026-07-30

Initial public release.

### Added
- Station daemon (`agent-lightsd.py`) driving OpenRGB LEDs with the full light language:
  blue (working, N dips = N units), green (idle / N cards), amber (fault), rainbow (full
  send), and a red poke for pending decisions.
- Never-black transitions (brightness-channel swaps and crossfades).
- Zone profiles: B (cluster units) and A (monitors), hot-reloaded on config mtime, plus a
  runtime switcher (`agent-lights-profile.sh`) and a palette demo (`agent-lights-demo.sh`).
- GPU rainbow takeover on sustained high utilisation.
- Laptop heartbeat (`heartbeat.ps1` + loop + `push-now.ps1`) pushing session/subagent/prompt
  counters to the station over SSH.
- Claude Code hook example (`session-signal.py`) and a hooks config template.
- Example configuration for the station and laptop; systemd unit.
- Documentation of the light language (`docs/SEMANTICS.md`).
- `NOTICE` documenting third-party dependencies and their licenses (OpenRGB GPL-2.0 as an
  external process; the `openrgb-python` GPL-3.0 client flag), plus a trademark disclaimer.
