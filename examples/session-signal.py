#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""session-signal.py - a Claude Code hook that couples agent sessions to the LED.

Wire it into several hook events (see claude-hooks.example.json). It maintains two
kinds of per-session markers in the Agent-Lights-Communication state directory, which the laptop
heartbeat then counts:

  me/<session_id>.flag      -> an active orchestrator session (blue = sum of units)
  prompts/<session_id>.flag -> a blocking permission prompt on screen (red poke)

Event mapping:
  UserPromptSubmit -> create/refresh me/<sid>.flag (session is working; blue)
                      and clean up me markers older than ME_STALE (60 min).
  Stop             -> remove ONLY this session's me marker (never touch others).
  Notification     -> a permission prompt (not idle) creates prompts/<sid>.flag (red).
  PostToolUse      -> the prompt was answered; remove this session's prompt marker.

Rules: fail-open (any error -> exit 0), push is non-blocking, STDOUT stays EMPTY
(UserPromptSubmit stdout is injected into context), no secrets.

Set AGENT_LIGHTS_STATE (env) to your state directory; defaults to ~/.agent-lights.
"""
import sys
import os
import json
import re
import time
import subprocess

STATE = os.environ.get("AGENT_LIGHTS_STATE", os.path.join(os.path.expanduser("~"), ".agent-lights"))
PROMPTS_DIR = os.path.join(STATE, "prompts")
ME_DIR = os.path.join(STATE, "me")
PUSH = os.environ.get("AGENT_LIGHTS_PUSH", "")  # optional path to push-now.ps1; empty = no immediate push
ME_STALE = 60 * 60  # an orchestrator marker older than 60 min = a session died without Stop -> clean it up
IDLE_MARKERS = ("waiting for your input", "idle")


def _safe_sid(sid):
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(sid or "default"))[:64] or "default"


def marker_path(sid):
    return os.path.join(PROMPTS_DIR, _safe_sid(sid) + ".flag")


def me_marker_path(sid):
    return os.path.join(ME_DIR, _safe_sid(sid) + ".flag")


def cleanup_stale_me():
    """Remove me/*.flag markers older than ME_STALE (a session died without Stop). Fail-open."""
    try:
        cut = time.time() - ME_STALE
        for name in os.listdir(ME_DIR):
            if not name.endswith(".flag"):
                continue
            p = os.path.join(ME_DIR, name)
            try:
                if os.path.getmtime(p) <= cut:
                    os.remove(p)
            except Exception:
                pass
    except Exception:
        pass


def write_me(sid):
    """Create/refresh this session's orchestrator marker + clean up stale ones. Fail-open."""
    try:
        os.makedirs(ME_DIR, exist_ok=True)
        with open(me_marker_path(sid), "w", encoding="ascii") as f:
            f.write("1")
        cleanup_stale_me()
    except Exception:
        pass


def clear_me(sid):
    """Remove ONLY this session's marker (end of turn). Never touch others. Fail-open."""
    try:
        p = me_marker_path(sid)
        if os.path.exists(p):
            os.remove(p)
    except Exception:
        pass


def fire_push():
    if not PUSH:
        return
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", PUSH],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        pass


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0  # fail-open

    event = str(data.get("hook_event_name", ""))
    tool = data.get("tool_name")
    sid = data.get("session_id")

    # UserPromptSubmit: the session started working on a prompt -> mark it (blue, sum of units)
    if event == "UserPromptSubmit" or (not event and "prompt" in data and tool is None):
        write_me(sid)
        fire_push()
        return 0

    # Stop: end of the session's turn -> remove THIS session's marker. working.count is separate.
    if event == "Stop":
        clear_me(sid)
        fire_push()
        return 0

    # PostToolUse: this session's prompt was answered -> remove the marker (early-exit if absent)
    if event == "PostToolUse" or (not event and tool):
        p = marker_path(sid)
        if not os.path.exists(p):
            return 0  # most common path: nothing pending in this session
        try:
            os.remove(p)
        except Exception:
            pass
        fire_push()
        return 0

    # Notification: a permission/attention prompt -> mark this session (skip idle notifications)
    if event == "Notification" or (not event and "message" in data):
        msg = str(data.get("message", "")).lower()
        if any(m in msg for m in IDLE_MARKERS):
            return 0
        try:
            os.makedirs(PROMPTS_DIR, exist_ok=True)
            with open(marker_path(sid), "w", encoding="ascii") as f:
                f.write("1")
        except Exception:
            pass
        fire_push()
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
