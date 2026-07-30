# Agent-Lights-Communication - the light language

Agent-Lights-Communication turns the aRGB LEDs already inside a desktop (the AIO pump ring and the
case/radiator fans) into an ambient status display for a fleet of AI-agent coding
sessions. This document is the full specification of what each state means.

Two principles run through everything:

- **Steady light means the system is alive.** A black frame means something died. The
  daemon never blanks the LEDs on purpose; transitions go through a brightness channel
  or a crossfade so every frame stays lit.
- **Counting is done with gentle dips, not blinking.** A "signal" is a series of N smooth
  brightness dips (baseline -> ~12% -> baseline) separated by a beat of full brightness, so
  the dips stay countable at a glance. Sharp on/off blinking is reserved for the 6+ alarm.

## Base states (single-colour semaphore, `PROFILE` empty)

| State | Colour | Pattern | Meaning |
|-------|--------|---------|---------|
| Idle | Green | Steady | No work in progress, no decisions queued. |
| Cards queued | Green | N dips | N decision "cards" waiting in the backlog. Calm - not blocking. |
| Working | Blue | Steady (orchestrator) or N dips | Work in progress. Steady blue = the orchestrator itself (no background workers). N dips = N subagents/background workers (laptop subagents + station LXC sessions); the orchestrator alone never adds a dip. |
| Full send | Rainbow | Wave | Work in progress **and** the GPU is busy (util over threshold). |
| Fault | Amber | Steady | Health is RED, or the health signal is stale. |
| Maintenance | Violet | Steady | Service mode (set manually via the override file). |

Priority when several apply: **red > amber > rainbow > blue > green.**

## The red poke (decisions waiting on a human)

A blocking prompt (a permission request on screen, or an explicit "waiting for the human"
counter) does **not** take over the whole colour. Instead it is overlaid as a brief **red
poke** on top of the current base colour, every few seconds:

- The base colour dims down its brightness channel to the floor (~12%, never full black).
- At the floor the hue swaps to a dim red, then brightens to full red and holds briefly.
- Red dims back to the floor, the hue swaps back to the base colour, and it brightens again.

The number of pokes in a series equals the number of pending decisions. Because the colour
only ever changes at the brightness floor, the eye never sees a violet in-between (a naive
colour crossfade from blue to red passes through violet) and never sees a hard cut. Work
stays visible the whole time; the decision just keeps knocking.

Counting caps at `COUNT_CAP` (default 5). Beyond that, the count is shown as "cap or more".

## Zone profiles

If your rig has enough separately addressable LED groups, you can map them to zones instead
of showing one colour for the whole system.

### Profile B - cluster

Each element (pump / individual fans) shows the state of **one** cluster unit:

| Unit | Blue (working) | Green (alive) | Amber (fault) | Dim (no data) |
|------|----------------|---------------|---------------|---------------|
| host | - | daemon alive | health RED | - |
| gpu-vm | GPU util over threshold | GPU idle | - | - |
| lxc-agent | agent session running | container up, idle | container down | unreachable |
| lxc-rag | - | up and healthy | health RED | unreachable |

When the GPU stays above the takeover threshold long enough, a software rainbow washes over
**all** zones at once (the collective full-load effect), then returns to per-zone colours.

### Profile A - screens

Each element maps to a physical monitor. The element is blue when an agent session is active
on that screen, otherwise dimmed. The per-monitor state comes from the laptop heartbeat's
`mon` field plus a `session-monitor.map` file.

Switch profiles at runtime with `agent-lights-profile.sh A|B|off`; the daemon hot-reloads on
the config file's mtime, no restart.

## Override (test / demo)

Writing a keyword to `<RUN_DIR>/override` forces a state, bypassing the profile - handy for
testing colours. Examples: `green`, `blue0`, `blue2`, `green3`, `red1`, `orange`, `rainbow`,
`violet`. Delete the file to return to the real computed state. `agent-lights-demo.sh` walks
the whole palette.
