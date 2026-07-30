#!/bin/bash
# agent-lights-profile.sh A|B|off - switch the zone profile.
# Edits PROFILE= in the zones config. The daemon hot-reloads on file mtime (~<2 s), no restart.
#   A   = screens/sessions (zone -> monitor)
#   B   = cluster (zone -> VM/LXC/host)
#   off = single-colour semaphore
CONF="${AGENT_LIGHTS_ZONES_CONF:-/etc/agent-lights-zones.conf}"
case "$1" in
  A|a)   P=A ;;
  B|b)   P=B ;;
  off|OFF|"") P= ;;
  *) echo "usage: agent-lights-profile.sh A|B|off"; exit 1 ;;
esac
sed -i "s/^PROFILE=.*/PROFILE=$P/" "$CONF"
echo -n "set "; grep '^PROFILE=' "$CONF"
echo "the daemon will pick up the change in <2 s (hot-reload, no restart)."
