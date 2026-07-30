# agent-lights-config.example.ps1 - copy to agent-lights-config.ps1 (gitignored) and edit for your machine.
# Dot-sourced by heartbeat.ps1 / push-now.ps1.

# SSH private key used to reach the station host (key-only, no password).
$AL_Key = "$HOME\.ssh\agent_lights_station"

# Station SSH target: user@host (or user@100.x.y.z on a mesh VPN).
$AL_Host = 'root@station.local'

# Local directory holding the counter files (created if missing).
$AL_StateDir = "$HOME\.agent-lights"

# Directory on the station where the daemon reads the heartbeat (matches RUN_DIR in the daemon conf).
$AL_RemoteRunDir = '/run/agent-lights'
