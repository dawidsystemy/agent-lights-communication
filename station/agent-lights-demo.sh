#!/bin/bash
# Show the LED palette by writing to the daemon's override file, then return to real state.
# Steady blue = orchestrator only; N dips = N subagents.
RUN_DIR="${AGENT_LIGHTS_RUN_DIR:-/run/agent-lights}"; mkdir -p "$RUN_DIR"
show() { echo "SHOW: $1 ($2 s)"; echo "$1" > "$RUN_DIR/override"; sleep "$2"; }
show green 8         # steady GREEN  = idle (0 work, 0 cards)
show blue0 8         # steady BLUE   = only the orchestrator is working (0 subagents)
show blue2 14        # BLUE 2 dips   = 2 subagents (orchestrator + 2)
show green3 14       # blinking GREEN 3 dips = 3 decision cards queued (a calm backlog)
show red1 12         # RED           = hard stop (a prompt/permission is waiting, work is blocked)
show orange 8        # AMBER         = fault (health RED)
show rainbow 8       # RAINBOW       = full send (work + GPU busy)
show violet 5        # VIOLET        = maintenance
rm -f "$RUN_DIR/override"
echo 'Back to real state'
