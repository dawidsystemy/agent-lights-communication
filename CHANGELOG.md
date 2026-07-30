# Changelog

All notable changes to Agent-Lights-Communication are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

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
