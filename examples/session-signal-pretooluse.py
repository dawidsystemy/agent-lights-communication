#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""session-signal-pretooluse.py - a companion Claude Code hook for the PreToolUse event.

It lights the "me" (orchestrator working) marker on EVERY tool call, covering a blind spot in
session-signal.py: a turn woken by a background agent's message / a notification does NOT pass
through UserPromptSubmit, so me/<sid>.flag is never created and the work shows up "after green"
instead of blue. This hook fires on every tool use, so the light goes blue no matter what triggered
the turn.

Wire it into PreToolUse with matcher "*" (see claude-hooks.example.json). The sid sanitization and
marker path are identical to session-signal.py, so both hooks point at the same file.

Rules: throttle (no write once the marker is already "1", so there's no cost per tool call), no
logging, always exit 0 (a hook must never block a tool). The optional immediate push fires only on
the write transition (the first tool call of a turn), detached and fail-open.

Set AGENT_LIGHTS_STATE (env) to your state directory; defaults to ~/.agent-lights.
Set AGENT_LIGHTS_PUSH (env) to push-now.ps1 for an immediate heartbeat; empty = no push.
"""
import sys
import os
import json
import re
import subprocess

STATE = os.environ.get("AGENT_LIGHTS_STATE", os.path.join(os.path.expanduser("~"), ".agent-lights"))
ME_DIR = os.path.join(STATE, "me")
PUSH = os.environ.get("AGENT_LIGHTS_PUSH", "")  # optional path to push-now.ps1; empty = no immediate push


def _safe_sid(sid):
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(sid or "default"))[:64] or "default"


def me_marker_path(sid):
    return os.path.join(ME_DIR, _safe_sid(sid) + ".flag")


def fire_push():
    """Fire push-now.ps1 detached, without waiting. Fail-open: any error -> silence."""
    if not PUSH:
        return
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", PUSH],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            creationflags=flags,
        )
    except Exception:
        pass


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0  # fail-open: missing/bad stdin must not block the tool

    try:
        sid = data.get("session_id")
        path = me_marker_path(sid)

        # throttle: already lit -> do nothing (no per-tool-call cost)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="ascii") as f:
                    if f.read().strip() == "1":
                        return 0
            except Exception:
                pass  # unreadable -> overwrite below

        os.makedirs(ME_DIR, exist_ok=True)
        with open(path, "w", encoding="ascii") as f:
            f.write("1")
        # transition: the marker was just lit -> one push per turn (the throttle blocks re-entry)
        fire_push()
    except Exception:
        pass  # anything goes wrong -> silence, exit 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
