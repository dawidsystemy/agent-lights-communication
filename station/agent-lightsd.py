#!/usr/bin/env python3
# agent-lightsd.py - Agent-Lights-Communication indicator daemon.
#
# Turns the aRGB LEDs already inside your desktop (AIO pump + case/radiator fans,
# driven through OpenRGB) into an ambient status display for a fleet of AI-agent
# coding sessions running across a homelab (Proxmox host + LXC containers + a
# GPU VM).
#
# LIGHT LANGUAGE (full spec in docs/SEMANTICS.md):
#   Steady light  = the system is alive. A black frame means something died.
#   BLUE          = work in progress. N gentle dips = N BACKGROUND workers
#                   (laptop subagents + station container sessions). The
#                   orchestrator alone never adds a dip: a lone orchestrator is
#                   STEADY blue. Cap at COUNT_CAP.
#   RED dips      = decisions waiting for a human (blocking prompts). Injected as
#                   a brief "poke" on top of the base colour so work stays visible.
#   GREEN dips    = queued decision cards (a calm backlog, not blocking).
#   GREEN steady  = idle (no work, no cards).
#   AMBER static  = fault (health RED / stale health signal).
#   VIOLET static = maintenance mode.
#   RAINBOW wave  = full send (work in progress AND the GPU is busy).
# Priority: red > amber > rainbow > blue > green.
#
# ZONE PROFILES (optional, /etc/agent-lights-zones.conf):
#   PROFILE=B -> each zone (pump / individual fans) shows the state of ONE cluster
#                unit (host / gpu-vm / lxc-agent / lxc-rag).
#   PROFILE=A -> each zone maps to a physical monitor; blue when a session is
#                active on that screen (from the laptop heartbeat "mon" field).
#   PROFILE empty -> single-colour semaphore (the light language above).
# Hot-reload on file mtime, no daemon restart.
#
# Requirements: OpenRGB (SDK server on 127.0.0.1:6742), openrgb-python, a Proxmox
# host (pct/qm) for cluster probes, optional NVIDIA GPU in a VM for the rainbow
# takeover. All host-specific values live in /etc/agent-lights.conf.
import os, time, subprocess, re, sys, signal, math, colorsys
from collections import deque

CONF = os.environ.get('AGENT_LIGHTS_CONF', '/etc/agent-lights.conf')

def load_conf():
    cfg = {}
    try:
        with open(CONF) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                cfg[k.strip()] = v.strip().strip('"')
    except FileNotFoundError:
        pass
    return cfg

cfg = load_conf()
def gi(k, d):
    try: return int(cfg.get(k, d))
    except: return int(d)
def gf(k, d):
    try: return float(cfg.get(k, d))
    except: return float(d)
def hexrgb(k, d):
    s = (cfg.get(k, d) or d).strip()
    try: return (int(s[0:2],16), int(s[2:4],16), int(s[4:6],16))
    except: return (255,255,255)

RUN_DIR       = cfg.get('RUN_DIR', '/run/agent-lights')
LXC_AGENTS    = (cfg.get('LXC_AGENTS', '200')).split()
LAPTOP_STALE  = gi('LAPTOP_STALE', 300)
HEALTH_STALE  = gi('HEALTH_STALE', 2700)
HEALTH_LOG    = cfg.get('HEALTH_LOG', '/var/log/agent-lights-health.log')
HEALTH_RED_RE = cfg.get('HEALTH_RED_RE', r'\bRED\b|\bDOWN\b')
WAIT_SOURCE   = cfg.get('WAIT_SOURCE', 'panel')          # panel (LXC decisions.json) | laptop (heartbeat)
PANEL_VMID    = cfg.get('PANEL_VMID', '200')
PANEL_WAIT_FILE = cfg.get('PANEL_WAIT_FILE', '/run/panel/waiting.computed')
COUNT_CAP     = gi('COUNT_CAP', 5)
RECOMPUTE     = gi('RECOMPUTE_SEC', 20)
GPU_VMID      = cfg.get('GPU_VMID', '100')
GPU_THRESH    = gi('GPU_THRESH', 20)
LXC_AGENT_VMID = cfg.get('LXC_AGENT_VMID', '200')  # profile B: which container is the "lxc-agent" unit
LXC_RAG_VMID   = cfg.get('LXC_RAG_VMID', '201')    # profile B: which container is the "lxc-rag" unit
DIP_MS        = gf('DIP_MS', 0.60)       # duration of one gentle dip (ramp down and back up)
DIP_FLOOR     = gf('DIP_FLOOR', 0.12)    # dip floor as a fraction of baseline brightness (~12%)
DIP_STEP      = gf('DIP_STEP', 0.04)     # brightness animation step (~40 ms/frame)
DIP_GAP       = gf('DIP_GAP', 0.40)      # full brightness between dips (separates them so they stay countable)
SERIES_PAUSE  = gf('SERIES_PAUSE', 2.5)  # steady light between series
FAST_MS       = gf('FAST_MS', 0.15)      # sharp continuous pulse (6+ = attention alarm)
XFADE_MS      = gf('XFADE_MS', 0.25)     # colour->colour crossfade on state change (no black frame)
XFADE_STEP    = gf('XFADE_STEP', 0.04)   # crossfade step (~40 ms/frame), every frame lit
RED_POKE      = gi('RED_POKE', 1)        # 1=on 0=off - red poke on pending decisions (reversible)
RED_POKE_SEC  = gf('RED_POKE_SEC', 9.0)  # seconds between poke series (~8-10 s)
RED_POKE_MS   = gf('RED_POKE_MS', 0.30)  # hold of one red flash (longer = calmer)
RED_POKE_GAP  = gf('RED_POKE_GAP', 0.55) # gap (full work colour) between flashes in a series
RED_POKE_RAMP_MS = gf('RED_POKE_RAMP_MS', 0.22)  # brightness ramp edge (~220 ms "tired", not snappy)
COL_GREEN  = hexrgb('COL_GREEN', '00FF00')
COL_BLUE   = hexrgb('COL_BLUE', '0000FF')
COL_RED    = hexrgb('COL_RED', 'FF0000')
COL_ORANGE = hexrgb('COL_ORANGE', 'FF4500')
COL_VIOLET = hexrgb('COL_VIOLET', '8000FF')

# ---------- ZONE PROFILE (map screens/cluster units -> RGB zones) ----------
# Separate config /etc/agent-lights-zones.conf. PROFILE=B (cluster) | A (screens) | empty = 1-colour semaphore.
ZONES_CONF = os.environ.get('AGENT_LIGHTS_ZONES_CONF', '/etc/agent-lights-zones.conf')
def load_zones():
    z = {}
    try:
        with open(ZONES_CONF) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                v = v.split('#', 1)[0].strip().strip('"')   # strip inline comment (# ...)
                z[k.strip()] = v
    except FileNotFoundError:
        pass
    return z
def _parse_ranges(spec):
    """'41-48,81-120' -> [(41,48),(81,120)]. Tolerant of spaces/empty."""
    out = []
    for part in (spec or '').split(','):
        part = part.strip()
        if not part:
            continue
        if '-' in part:
            a, b = part.split('-', 1)
            out.append((int(a), int(b)))
        else:
            out.append((int(part), int(part)))
    return out

# Zone config state (rebuilt on hot-reload). ELEMENTS: [(name,[(a,b)...],unit)] for profile B;
# ELEMENTS_A: [(name,[(a,b)...],monitor_idx)] for profile A.
ZC = {}
PROFILE = ''
ELEMENTS = []
ELEMENTS_A = []
GPU_TAKEOVER = 'rainbow'   # 'rainbow' | 'off' - collective effect on ALL zones at GPU full-load
GPU_TK_UTIL = 70           # % util threshold (smoothed) for takeover - inference bursts, average sits lower
GPU_TK_SEC = 15.0          # seconds above threshold before takeover (longer = only big jobs, not short bursts)
GPU_WORK_UTIL = 20         # % util threshold = "working" on the gpu-vm zone (blue)
GPU_SMOOTH_N = 5           # smoothing window for util (readings; 5x2s=10s - damps 0-dips between bursts)
NODATA_SCALE = 0.30        # brightness of the "no data / dimmed" state
_zones_mtime = 0.0

def _gi_z(z, k, d):
    try: return int(z.get(k, d))
    except Exception: return int(d)

def apply_zones(z):
    global PROFILE, ELEMENTS, ELEMENTS_A, GPU_TAKEOVER, GPU_TK_UTIL, GPU_TK_SEC, GPU_WORK_UTIL, GPU_SMOOTH_N
    PROFILE = z.get('PROFILE', '').upper()
    els = []; els_a = []
    for key in ('PUMP', 'FAN_RIGHT', 'FAN_MIDDLE', 'FAN_LEFT'):
        rng = z.get('EL_' + key)
        if rng:
            r = _parse_ranges(rng)
            els.append((key, r, z.get('UNIT_' + key, 'host')))
            try: mon = int(z.get('A_' + key, '0'))
            except Exception: mon = 0
            els_a.append((key, r, mon))
    ELEMENTS = els; ELEMENTS_A = els_a
    GPU_TAKEOVER = (z.get('GPU_TAKEOVER', 'rainbow') or 'rainbow').lower()
    GPU_TK_UTIL = _gi_z(z, 'GPU_TAKEOVER_UTIL', 70)
    GPU_TK_SEC = float(_gi_z(z, 'GPU_TAKEOVER_SEC', 15))
    GPU_WORK_UTIL = _gi_z(z, 'GPU_WORK_UTIL', 20)
    GPU_SMOOTH_N = max(1, _gi_z(z, 'GPU_SMOOTH_N', 5))
    try: globals()['NODATA_SCALE'] = float(z.get('NODATA_SCALE', '0.30'))
    except Exception: globals()['NODATA_SCALE'] = 0.30

def reload_zones_if_changed():
    """Hot-reload on config mtime change -> switch profile A/B/off WITHOUT restarting the daemon (~<2 s)."""
    global _zones_mtime, ZC
    try:
        mt = os.path.getmtime(ZONES_CONF)
    except OSError:
        return False
    if mt != _zones_mtime:
        _zones_mtime = mt
        ZC = load_zones()
        apply_zones(ZC)
        return True
    return False

ZC = load_zones()
apply_zones(ZC)
try:
    _zones_mtime = os.path.getmtime(ZONES_CONF)
except OSError:
    _zones_mtime = 0.0

SERVER_HOST = cfg.get('ORGB_HOST', '127.0.0.1'); SERVER_PORT = gi('ORGB_PORT', 6742)
ORGB_BIN = cfg.get('ORGB_BIN', '/opt/openrgb/OpenRGB')   # fallback CLI binary (AppImage AppRun works too)
LOG = cfg.get('LOG', '/var/log/agent-lights.log')
os.environ.setdefault('HOME', '/root')
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from openrgb import OpenRGBClient
from openrgb.utils import RGBColor

client = None
device = None
BLACK = RGBColor(0, 0, 0)
_current_mode = None          # cache device mode - set_mode only on CHANGE (avoids blank frames)
_last_rgb = None              # last baseline colour shown in Direct = crossfade SOURCE (smooth transitions)
DBG = (cfg.get('DEBUG', '0') == '1')

def logline(msg):
    try:
        with open(LOG, 'a') as f:
            f.write(time.strftime('%F %T') + ' ' + msg + '\n')
    except Exception:
        pass

def connect():
    global client, device, _current_mode, _last_static, _last_rgb
    try:
        client = OpenRGBClient(SERVER_HOST, SERVER_PORT, name='agent-lights')
        device = client.devices[0]
        _current_mode = None      # after (re)connect the mode is unknown -> force set_mode on next action
        _last_static = None       # force re-apply of colour/mode after reconnect (incl. cached rainbow)
        _last_rgb = None          # colour on device after reconnect is unknown -> no crossfade from steady
        return True
    except Exception:
        client = None; device = None
        _current_mode = None
        _last_static = None
        _last_rgb = None
        return False

def ensure():
    if device is None:
        connect()
    return device is not None

def apprun_static(rgb):
    try:
        subprocess.run([ORGB_BIN, '--device', '0', '--mode', 'static',
                        '--color', '%02X%02X%02X' % rgb],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    except Exception:
        pass

# ---------- state sources ----------
def count_agents():
    """Agent sessions in LXC: claude processes whose parent is NOT claude (parent-based)."""
    total = 0
    for c in LXC_AGENTS:
        try:
            # pattern [c]laude - the regex matches "claude", but the string "[c]laude" in THIS
            # command's cmdline does NOT contain the substring "claude", so pgrep won't count itself
            out = subprocess.run(
                ['pct','exec',c,'--','bash','-c',
                 'for p in $(pgrep -f "[c]laude" 2>/dev/null); do echo "$p $(awk "{print \\$4}" /proc/$p/stat 2>/dev/null)"; done'],
                capture_output=True, text=True, timeout=10).stdout
            pids = set(); rows = []
            for line in out.splitlines():
                pp = line.split()
                if len(pp) >= 2:
                    rows.append((pp[0], pp[1])); pids.add(pp[0])
            total += sum(1 for pid, ppid in rows if ppid not in pids)
        except Exception:
            pass
    return total

WAIT_PERSIST = cfg.get('WAIT_PERSIST', '/var/lib/agent-lights/waiting')  # survives laptop staleness AND host reboot

def _read_persist():
    try:
        with open(WAIT_PERSIST) as f:
            t = f.read()
        w = int(re.search(r'"waiting"\s*:\s*(\d+)', t).group(1))
        st = int(re.search(r'"src_ts"\s*:\s*(\d+)', t).group(1))
        return w, st
    except Exception:
        return None

def _write_persist(w, src_ts):
    try:
        os.makedirs(os.path.dirname(WAIT_PERSIST), exist_ok=True)
        with open(WAIT_PERSIST, 'w') as f:
            f.write('{"waiting":%d,"src_ts":%d}\n' % (w, src_ts))
    except Exception:
        pass

def read_me_subs():
    """(me, subs) from the heartbeat, staleness-gated. me=orchestrator (0/1), subs=subagent count.
       Backward-compat: old heartbeat without me/subs -> working_laptop used as subs, me=0."""
    me = 0
    subs = 0
    try:
        with open(os.path.join(RUN_DIR, 'hb-laptop')) as f:
            txt = f.read()
        hb_ts = int(re.search(r'"ts"\s*:\s*(\d+)', txt).group(1))
        if time.time() - hb_ts <= LAPTOP_STALE:
            ms = re.search(r'"subs"\s*:\s*(\d+)', txt)
            if ms is not None:
                subs = int(ms.group(1))
                mm = re.search(r'"me"\s*:\s*(\d+)', txt)
                me = int(mm.group(1)) if mm else 0
            else:
                mw = re.search(r'"working_laptop"\s*:\s*(\d+)', txt)   # old format
                subs = int(mw.group(1)) if mw else 0
    except Exception:
        pass
    return me, subs

def read_prompt():
    """prompt.flag (0/1) from the laptop heartbeat - a permission prompt on screen. Staleness-gated
       (a prompt on a sleeping laptop is not 'on screen' -> 0)."""
    try:
        with open(os.path.join(RUN_DIR, 'hb-laptop')) as f:
            txt = f.read()
        ts = int(re.search(r'"ts"\s*:\s*(\d+)', txt).group(1))
        if time.time() - ts <= LAPTOP_STALE:
            m = re.search(r'"prompt"\s*:\s*(\d+)', txt)
            return int(m.group(1)) if m else 0
    except Exception:
        pass
    return 0

def _read_hb_waiting():
    """waiting from the laptop heartbeat (fallback) -> (waiting, ts) or None."""
    try:
        with open(os.path.join(RUN_DIR, 'hb-laptop')) as f:
            txt = f.read()
        ts = int(re.search(r'"ts"\s*:\s*(\d+)', txt).group(1))
        w = int(re.search(r'"waiting"\s*:\s*(\d+)', txt).group(1))
        return w, ts
    except Exception:
        return None

def read_panel_waiting():
    """waiting straight from a decision panel in an LXC (decisions.json) - laptop-independent.
       Returns (waiting, ts) or None. Contract: PANEL_WAIT_FILE = bare integer ("1\\n"); JSON tolerated too."""
    if WAIT_SOURCE != 'panel':
        return None
    try:
        out = subprocess.run(['pct','exec',PANEL_VMID,'--','cat',PANEL_WAIT_FILE],
                             capture_output=True, text=True, timeout=8).stdout
        m = re.search(r'"waiting"\s*:\s*(\d+)', out)   # JSON format (compat)
        if not m:
            m = re.search(r'(\d+)', out)               # bare integer (contract)
        if not m:
            return None                                # empty/missing (e.g. LXC restart window) -> fallback
        w = int(m.group(1))
        mt = re.search(r'"ts"\s*:\s*(\d+)', out)
        ts = int(mt.group(1)) if mt else int(time.time())
        return w, ts
    except Exception:
        return None

def read_waiting():
    """PANEL primary (authoritative, laptop-independent) -> mirrored to persist.
       Panel unavailable -> fall back to the laptop heartbeat from no-expire persist.
       Persist survives host reboot and simultaneous unavailability of panel and laptop."""
    p = read_panel_waiting()
    if p is not None:
        w, ts = p
        cur = _read_persist()
        if cur is None or cur[0] != w:        # write persist only when the value changes
            _write_persist(w, ts)
        return w
    # fallback: laptop heartbeat, waiting does NOT expire (persist)
    hb = _read_hb_waiting()
    if hb is not None:
        w, ts = hb
        cur = _read_persist()
        if cur is None or ts > cur[1]:
            _write_persist(w, ts)
    cur = _read_persist()
    return cur[0] if cur else 0

def read_health():
    hc = LXC_AGENTS[0]
    try:
        out = subprocess.run(['pct','exec',hc,'--','tail','-1',HEALTH_LOG],
                             capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return 'GREEN'   # transient pct hiccup -> do not raise a false alarm
    if not out:
        return 'RED'
    if re.search(HEALTH_RED_RE, out):
        return 'RED'
    m = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', out)
    if m:
        try:
            t = time.mktime(time.strptime(m.group(1), '%Y-%m-%d %H:%M:%S'))
            return 'RED' if (time.time() - t > HEALTH_STALE) else 'GREEN'
        except Exception:
            return 'GREEN'
    return 'RED'

def gpu_util():
    try:
        out = subprocess.run(['qm','guest','exec',GPU_VMID,'--',
                              'nvidia-smi','--query-gpu=utilization.gpu',
                              '--format=csv,noheader,nounits'],
                             capture_output=True, text=True, timeout=8).stdout
        m = re.search(r'"out-data"\s*:\s*"(\d+)', out)
        return int(m.group(1)) if m else 0
    except Exception:
        return 0

def write_state(station, me, subs, cards, prompt, health, gpu, kind, n):
    try:
        with open(os.path.join(RUN_DIR, 'state'), 'w') as f:
            # "waiting" = alias of cards (backward compat); working = total subagents (subs+station)
            f.write('{"station":%d,"me":%d,"subs":%d,"working":%d,"cards":%d,"waiting":%d,"prompt":%d,'
                    '"health":"%s","gpu":%d,"led":"%s","n":%d,"ts":%d}\n'
                    % (station, me, subs, subs + station, cards, cards, prompt, health, gpu, kind, n, int(time.time())))
    except Exception:
        pass

# ---------- PROFILE B: per cluster-unit state -> zone colour ----------
def count_lxc_one(vmid):
    """Claude sessions in ONE LXC container (parent-based, like count_agents but a single vmid)."""
    try:
        out = subprocess.run(
            ['pct', 'exec', str(vmid), '--', 'bash', '-c',
             'for p in $(pgrep -f "[c]laude" 2>/dev/null); do echo "$p $(awk "{print \\$4}" /proc/$p/stat 2>/dev/null)"; done'],
            capture_output=True, text=True, timeout=10).stdout
        pids = set(); rows = []
        for line in out.splitlines():
            pp = line.split()
            if len(pp) >= 2:
                rows.append((pp[0], pp[1])); pids.add(pp[0])
        return sum(1 for pid, ppid in rows if ppid not in pids)
    except Exception:
        return -1   # -1 = unreachable (distinct from 0 = alive-idle)

def lxc_running(vmid):
    try:
        out = subprocess.run(['pct', 'status', str(vmid)], capture_output=True, text=True, timeout=8).stdout
        return 'running' in out
    except Exception:
        return False

_gpu_reads = []   # util reading window (length = GPU_SMOOTH_N) - damps 0-dips between bursts

def gpu_util_smoothed():
    """Average util over the last GPU_SMOOTH_N readings (utilization.gpu, NOT memory). Damps inference burstiness."""
    _gpu_reads.append(gpu_util())
    while len(_gpu_reads) > GPU_SMOOTH_N:
        _gpu_reads.pop(0)
    return sum(_gpu_reads) / len(_gpu_reads) if _gpu_reads else 0.0

def unit_color(unit, health, gutil):
    """Zone colour by unit state. gutil = smoothed GPU util (from gpu_util_smoothed, passed in)."""
    dim = RGBColor(int(COL_GREEN[0]*NODATA_SCALE), int(COL_GREEN[1]*NODATA_SCALE), int(COL_GREEN[2]*NODATA_SCALE))
    if unit == 'host':
        return RGBColor(*COL_ORANGE) if health == 'RED' else RGBColor(*COL_GREEN)  # daemon alive = host alive
    if unit == 'gpu-vm':
        return RGBColor(*COL_BLUE) if gutil > GPU_WORK_UTIL else RGBColor(*COL_GREEN)  # work = UTIL, not memory
    if unit == 'lxc-agent':
        n = count_lxc_one(LXC_AGENT_VMID)
        if n < 0:
            return RGBColor(*COL_ORANGE) if not lxc_running(LXC_AGENT_VMID) else dim
        return RGBColor(*COL_BLUE) if n > 0 else RGBColor(*COL_GREEN)
    if unit == 'lxc-rag':
        n = count_lxc_one(LXC_RAG_VMID)
        if n < 0:
            return RGBColor(*COL_ORANGE) if not lxc_running(LXC_RAG_VMID) else dim
        return RGBColor(*COL_GREEN) if health != 'RED' else RGBColor(*COL_ORANGE)
    return RGBColor(*COL_GREEN)

def build_zone_colors_b(health, gutil):
    """Build a flat list of 121 colours for profile B (element -> unit colour). BLACK for empty LEDs."""
    n = len(device.colors) if (device and device.colors) else 121
    cols = [BLACK] * n
    for name, ranges, unit in ELEMENTS:
        rgb = unit_color(unit, health, gutil)
        for (a, b) in ranges:
            for i in range(a, min(b + 1, n)):
                cols[i] = rgb
    return cols

_rainbow_phase = 0.0

def build_rainbow_all():
    """TAKEOVER: SOFTWARE rainbow across ALL zone LEDs (pump + fans) - hue gradient by position,
       animated phase. Direct set_colors (not the HW Rainbow-wave, which only covered some zones)."""
    global _rainbow_phase
    n = len(device.colors) if (device and device.colors) else 121
    cols = [BLACK] * n
    idxs = []
    for name, ranges, unit in ELEMENTS:
        for (a, b) in ranges:
            idxs.extend(range(a, min(b + 1, n)))
    idxs = sorted(set(idxs))
    m = max(1, len(idxs))
    for k, i in enumerate(idxs):
        hue = ((k / m) + _rainbow_phase) % 1.0
        r, g, bl = colorsys.hsv_to_rgb(hue, 1.0, 1.0)
        cols[i] = RGBColor(int(r * 255), int(g * 255), int(bl * 255))
    _rainbow_phase = (_rainbow_phase + 0.03) % 1.0   # wave animation (hue shift per frame)
    return cols

# ---------- PROFILE A: screens/sessions -> zones ----------
def read_mon():
    """Per-monitor state from hb-laptop 'mon':[0,1,0,0] (1=session active on that monitor). Staleness-gated."""
    try:
        with open(os.path.join(RUN_DIR, 'hb-laptop')) as f:
            txt = f.read()
        ts = int(re.search(r'"ts"\s*:\s*(\d+)', txt).group(1))
        if time.time() - ts <= LAPTOP_STALE:
            m = re.search(r'"mon"\s*:\s*\[([0-9,\s]*)\]', txt)
            if m:
                return [int(x) for x in m.group(1).split(',') if x.strip() != '']
    except Exception:
        pass
    return []

def render_profile_a(mon):
    """Profile A: each element (mapped to a monitor) = blue when a session is active on that monitor, else dimmed."""
    if not ensure():
        return
    n = len(device.colors) if device.colors else 121
    cols = [BLACK] * n
    dim = RGBColor(int(COL_GREEN[0]*NODATA_SCALE), int(COL_GREEN[1]*NODATA_SCALE), int(COL_GREEN[2]*NODATA_SCALE))
    for name, ranges, monidx in ELEMENTS_A:
        active = (monidx < len(mon)) and (mon[monidx] > 0)
        rgb = RGBColor(*COL_BLUE) if active else dim   # active session on screen = blue; none = dimmed
        for (a, b) in ranges:
            for i in range(a, min(b + 1, n)):
                cols[i] = rgb
    try:
        _set_mode('Direct')
        device.set_colors(cols)
    except Exception:
        globals()['device'] = None
    time.sleep(1.0)

# ---------- decision (priority cascade) ----------
# amber(fault) > rainbow(full GPU) > blue(work) > green-blink(cards) > green-steady(idle)
# prompt (blocking decisions) does NOT dominate the cascade - it is overlaid as a RED POKE on top of
# the BASE state (work/idle) in apply(). The cascade only picks the BACKGROUND colour; red is a separate layer.
# BLUE: bg=subs+station (background workers); blue_active=(me+bg)>=1; blue_n=0 if bg==0 else bg.
#       So a lone orchestrator (me=1, bg=0) is STEADY blue, and N dips always mean N background workers.
def decide(cards, health, blue_active, blue_n, gpu):
    if health == 'RED':
        return ('orange', 0)
    if blue_active and gpu > GPU_THRESH:
        return ('rainbow', 0)
    if blue_active:
        return ('blue', blue_n)                   # work: n=background workers (0 -> steady)
    if cards >= 1:
        return ('green_blink', cards)             # global cards (panel) = green dips (not red)
    return ('green', 0)                           # steady green = zero work and zero cards

# ---------- override (test/demo) ----------
def read_override():
    try:
        with open(os.path.join(RUN_DIR, 'override')) as f:
            t = f.read().strip()
    except Exception:
        return None
    if not t:
        return None
    if t in ('green', 'green_static'): return ('green', 0)
    if t in ('orange', 'orange_static', 'amber'): return ('orange', 0)
    if t in ('rainbow',): return ('rainbow', 0)
    if t in ('violet', 'service'): return ('violet', 0)
    if t == 'red_static': return ('red', 1)
    if t == 'red_flash': return ('red', 2)
    if t == 'blue_static': return ('blue', 0)   # steady (orchestrator only)
    if t == 'blue_breath': return ('blue', 5)
    m = re.match(r'red(\d+)$', t)
    if m: return ('red', int(m.group(1)))
    m = re.match(r'blue(\d+)$', t)
    if m: return ('blue', int(m.group(1)))
    m = re.match(r'green(\d+)$', t)          # greenN = green blinking, N cards
    if m: return ('green_blink', int(m.group(1)))
    return None

# ---------- LED control ----------
_last_static = None   # ('static',rgb) | ('mode',name)
_cyc_t0 = 0.0         # start marker of the current dip_series cycle (DEBUG brightness timeline)
_cyc_frames = []      # buffer of (rel_t, factor) for one cycle - dump minima at the end (DBG only)

def _set_mode(name):
    """set_mode ONLY when the mode CHANGES (a redundant set_mode('Direct') per series blanked some MSI gear)."""
    global _current_mode
    if _current_mode == name:
        return False
    device.set_mode(name)
    _current_mode = name
    return True

def _crossfade(src, dst):
    """Smooth colour transition src->dst via Direct set_color. EVERY frame is LIT
       (interpolating between two bright colours) - no OFF/black frame on state change."""
    steps = max(1, int(XFADE_MS / XFADE_STEP))
    for i in range(1, steps + 1):
        p = i / steps
        r = int(src[0] + (dst[0] - src[0]) * p)
        g = int(src[1] + (dst[1] - src[1]) * p)
        b = int(src[2] + (dst[2] - src[2]) * p)
        device.set_color(RGBColor(r, g, b))
        time.sleep(XFADE_STEP)

def _enter_rgb(rgb, mode_changed):
    """Enter the target colour in Direct. If the previous colour is known and this is a REAL transition
       (different colour, no fresh set_mode) -> crossfade (smooth, no black). Otherwise hard set:
       after set_mode the device already had a switch frame, and a crossfade from unknown is pointless."""
    global _last_rgb
    if (not mode_changed) and _last_rgb is not None and _last_rgb != rgb and XFADE_MS > 0:
        if DBG:
            logline('DBG xfade %s->%s (every frame lit, min_lit>0)' % (_last_rgb, rgb))
        _crossfade(_last_rgb, rgb)
    else:
        device.set_color(RGBColor(*rgb))
    _last_rgb = rgb

def set_static(rgb):
    """Steady baseline colour via a DIRECT full frame. Some MSI gear ignores per-LED set_color in the
       'Static' mode (LEDs go black) - so the baseline is drawn in 'Direct' (like dip_series, which works).
       Re-applied per call (~1/s, cheap): survives an openrgb-server restart (exc -> device=None ->
       reconnect -> redraw). Colour->colour transition is smooth (crossfade)."""
    global _last_static, device, _last_rgb
    if ensure():
        try:
            mode_changed = _set_mode('Direct')
            _enter_rgb(rgb, mode_changed)     # crossfade from previous colour, or hard set after set_mode
            _last_static = ('static', rgb)
            return
        except Exception:
            device = None
    apprun_static(rgb)
    _last_static = ('static', rgb)
    _last_rgb = rgb

def set_hw_mode(mode_name):
    global _last_static, device
    sig = ('mode', mode_name)
    if sig == _last_static:
        return
    if ensure():
        try:
            _set_mode(mode_name)
            _last_static = sig
            return
        except Exception:
            device = None
    _last_static = sig

def _scaled(rgb, f):
    return RGBColor(int(rgb[0]*f), int(rgb[1]*f), int(rgb[2]*f))

def _smooth_dip(rgb):
    """One gentle dip: baseline -> ~DIP_FLOOR -> baseline, sinusoidal ease, per-frame.
       Returns (min_factor, max_frame_dt) for debugging."""
    steps = max(2, int(DIP_MS / DIP_STEP))
    min_f = 1.0; max_dt = 0.0; prev = time.time()
    for i in range(steps + 1):
        p = i / steps                      # 0..1
        f = DIP_FLOOR + (1.0 - DIP_FLOOR) * (0.5 + 0.5 * math.cos(2 * math.pi * p))
        device.set_color(_scaled(rgb, f))  # p=0 -> baseline, p=0.5 -> floor, p=1 -> baseline
        if DBG: _cyc_frames.append((time.time() - _cyc_t0, f))
        now = time.time(); dt = now - prev; prev = now
        if dt > max_dt: max_dt = dt
        if f < min_f: min_f = f
        time.sleep(DIP_STEP)
    return min_f, max_dt

def dip_series(rgb, n):
    """Gentle counting: baseline ON + N smooth dips (HARD cap COUNT_CAP, n>=cap => cap dips
       meaning 'cap or more') + a light pause. Sharp continuous blinking is not used actively."""
    global _last_static, device, _last_rgb, _cyc_t0, _cyc_frames
    _last_static = None
    if not ensure():
        apprun_static(rgb)     # failover: plain static colour (info > form)
        time.sleep(1.0)
        return
    try:
        mode_set = _set_mode('Direct')            # FIX: only on entering Direct, not per series
        col = RGBColor(*rgb)
        shown = min(max(1, n), COUNT_CAP)         # hard cap: n>=cap -> cap dips ("cap or more")
        _cyc_t0 = time.time(); _cyc_frames = []   # new cycle -> clean brightness timeline (DEBUG)
        # BASELINE = HARD SET (no crossfade). Crossfade stays only in set_static (steady states).
        # In the dip series no re-apply/crossfade cuts into the animation - the series holds the baseline
        # itself, so n dips = EXACTLY n minima (a crossfade on entry produced a second, spurious minimum).
        device.set_color(col)                     # baseline full brightness, immediately (lit->lit, no black)
        _last_rgb = rgb                           # crossfade source for the next STEADY state
        if DBG: _cyc_frames.append((0.0, 1.0))
        min_f = 1.0; max_dt = 0.0
        for _ in range(shown):
            mf, md = _smooth_dip(rgb)             # gentle dim and brighten
            if mf < min_f: min_f = mf
            if md > max_dt: max_dt = md
            device.set_color(col)                            # full brightness separates dips (countability)
            if DBG: _cyc_frames.append((time.time() - _cyc_t0, 1.0))
            time.sleep(DIP_GAP)
        device.set_color(col)                     # steady light = system is alive
        if DBG:
            fr = _cyc_frames                       # count LOCAL brightness minima in the cycle (dip < neighbours)
            mins = [round(fr[i][0], 2) for i in range(1, len(fr) - 1)
                    if fr[i][1] < fr[i-1][1] and fr[i][1] <= fr[i+1][1] and fr[i][1] < 0.9]
            logline('DBG dip series=%d minima=%d times=%s min_f=%.3f max_frame_dt=%.3f mode_set=%s floor=%.2f gap=%.2f pause=%.1f (expect minima==series)'
                    % (shown, len(mins), mins, min_f, max_dt, mode_set, DIP_FLOOR, DIP_GAP, SERIES_PAUSE))
        time.sleep(SERIES_PAUSE)
    except Exception as e:
        device = None
        if DBG: logline('DBG dip EXC: %r' % e)
        apprun_static(rgb)
        time.sleep(1.0)

_last_refresh = 0.0
_last_red_poke = 0.0  # marker of the last red-poke series
REFRESH_SEC = 30.0    # periodic forced re-apply (recover rainbow/hw-mode after a silent openrgb-server restart)


def _lum_ramp(rgb, f0, f1):
    """Brightness ramp of one colour: scale rgb from f0 to f1 (no hue change). Every frame lit."""
    steps = max(2, int(RED_POKE_RAMP_MS / DIP_STEP))
    for i in range(steps + 1):
        p = i / steps
        f = f0 + (f1 - f0) * p
        device.set_color(_scaled(rgb, f))
        time.sleep(DIP_STEP)


def red_poke(n):
    """A series of N red POKES over the current base colour (_last_rgb). N = number of pending decisions.
       The work colour stays visible (we return to it after each flash), so the decision doesn't vanish.
       BRIGHTNESS-CHANNEL TRANSITION: base -> ramp down to DIP_FLOOR (~12%, NOT 0) -> SWAP colour to dim
       red at the floor -> ramp up -> hold -> ramp down -> swap to dim base -> ramp up. The colour changes
       ONLY at the brightness floor = no violet (a colour crossfade would pass through violet), no hard
       cut, easy on the eyes. Smooth STATE crossfades stay separate."""
    global _last_rgb, device
    if not ensure():
        return
    base = _last_rgb if _last_rgb is not None else COL_GREEN
    shown = min(max(1, n), COUNT_CAP)
    try:
        _set_mode('Direct')
        for i in range(shown):
            _lum_ramp(base, 1.0, DIP_FLOOR)                  # dim the base to the floor (no black)
            device.set_color(_scaled(COL_RED, DIP_FLOOR))    # SWAP colour at the floor: dim red
            _lum_ramp(COL_RED, DIP_FLOOR, 1.0)               # brighten red
            time.sleep(RED_POKE_MS)                           # hold the flash (full red)
            _lum_ramp(COL_RED, 1.0, DIP_FLOOR)               # dim red to the floor
            device.set_color(_scaled(base, DIP_FLOOR))       # SWAP colour at the floor: dim base
            _lum_ramp(base, DIP_FLOOR, 1.0)                  # brighten base (back to work)
            if i < shown - 1:
                time.sleep(RED_POKE_GAP)
        _last_rgb = base
    except Exception:
        device = None

def apply(target, pending=0):
    global _last_refresh, _last_static, _current_mode, _last_red_poke
    now = time.time()
    if now - _last_refresh >= REFRESH_SEC:
        _last_static = None       # clear cache -> next set_hw_mode/set_static re-applies colour (and HW mode)
        # Clear _current_mode ONLY for hardware modes (rainbow) - there a re-set_mode recovers the animation
        # after a silent openrgb-server restart. In Direct, set_color re-applies every ~1 s (baseline), and a
        # needless set_mode('Direct') every 30 s BLANKED the LEDs (black frame), so we do NOT force it here.
        if _current_mode not in (None, 'Direct'):
            _current_mode = None
        _last_refresh = now
    kind, n = target
    if kind == 'red':
        dip_series(COL_RED, n)
    elif kind == 'blue':
        if n <= 0:
            set_static(COL_BLUE); time.sleep(1.0)   # steady blue = orchestrator only (0 subagents)
        else:
            dip_series(COL_BLUE, n)                  # n dips = n subagents
    elif kind == 'green_blink':
        dip_series(COL_GREEN, n)              # blinking green = panel cards (a calm queue)
    elif kind == 'rainbow':
        set_hw_mode('Rainbow wave'); time.sleep(1.0)
    elif kind == 'orange':
        set_static(COL_ORANGE); time.sleep(1.0)
    elif kind == 'violet':
        set_static(COL_VIOLET); time.sleep(1.0)
    else:
        set_static(COL_GREEN); time.sleep(1.0)
    # red POKE = ONLY blocking decisions (pending = prompt = prompt markers + red-extra).
    # Global cards -> green (green_blink above), NOT here. N pokes = number of calling sessions.
    # Skip on red/rainbow/orange (do not stack red onto a fault alarm). Every RED_POKE_SEC.
    if RED_POKE and pending > 0 and kind not in ('red', 'rainbow', 'orange'):
        if now - _last_red_poke >= RED_POKE_SEC:
            red_poke(pending)
            _last_red_poke = now

def handle_term(sig, frm):
    logline('daemon stop (signal %d)' % sig)
    sys.exit(0)

def main():
    signal.signal(signal.SIGTERM, handle_term)
    signal.signal(signal.SIGINT, handle_term)
    os.makedirs(RUN_DIR, exist_ok=True)
    connect()
    logline('agent-lightsd start (agents_lxc=%s, gpu_vm=%s, PROFILE=%s elems=%d, gpu_takeover=%s>%d%%/%.0fs smooth=%d, nodata=%.2f)'
            % (','.join(LXC_AGENTS), GPU_VMID, PROFILE or 'off', len(ELEMENTS), GPU_TAKEOVER, GPU_TK_UTIL, GPU_TK_SEC, GPU_SMOOTH_N, NODATA_SCALE))
    cur = ('green', 0)
    poke_current = 0                            # blocking decisions (prompt) - for the red poke
    health = 'GREEN'
    last_recompute = 0.0
    last_slow = 0.0                             # slow zone probe (health/lxc) - 20 s
    last_gpu = 0.0                              # fast GPU util probe - 2 s (responsiveness + takeover)
    GPU_PROBE = 2.0
    gutil = 0.0
    gpu_high_since = None
    takeover = False
    last_logged = None
    while True:
        now = time.time()
        reload_zones_if_changed()               # hot-reload: switch A/B/off without restart (mtime)
        ov = read_override()
        # ZONE PROFILE: each zone = unit state (B) or screen/session state (A). Bypasses the 1-colour semaphore.
        # Override (test/demo) STILL works (bypasses the profile) - for manual colour tests.
        if PROFILE == 'B' and ELEMENTS and ov is None:
            if now - last_slow >= RECOMPUTE:    # health/lxc change slowly -> every 20 s
                health = read_health()
                last_slow = now
                if last_logged != 'profileB':
                    logline('PROFILE B active: %s (gpu_takeover=%s>%d%%/%.0fs, work>%d%%)'
                            % (', '.join('%s->%s' % (e[0], e[2]) for e in ELEMENTS), GPU_TAKEOVER, GPU_TK_UTIL, GPU_TK_SEC, GPU_WORK_UTIL))
                    last_logged = 'profileB'
            if now - last_gpu >= GPU_PROBE:      # GPU util fast (smoothed) -> responsive work + takeover
                gutil = gpu_util_smoothed()
                last_gpu = now
                if GPU_TAKEOVER == 'rainbow' and gutil > GPU_TK_UTIL:
                    if gpu_high_since is None:
                        gpu_high_since = now
                    new_tk = (now - gpu_high_since) >= GPU_TK_SEC
                    if new_tk and not takeover:
                        logline('GPU TAKEOVER on (util=%.0f%% > %d%% for %.0fs) - rainbow on all zones' % (gutil, GPU_TK_UTIL, GPU_TK_SEC))
                    takeover = new_tk
                else:
                    if takeover:
                        logline('GPU TAKEOVER off (util=%.0f%%) - back to zones' % gutil)
                    gpu_high_since = None
                    takeover = False
            if takeover and ensure():
                try:                                           # collective effect: SOFTWARE rainbow on ALL zones
                    _set_mode('Direct'); device.set_colors(build_rainbow_all())
                except Exception:
                    globals()['device'] = None
                time.sleep(0.15)                               # fast frame = smooth rainbow wave
            elif ensure():
                try:
                    _set_mode('Direct'); device.set_colors(build_zone_colors_b(health, gutil))
                except Exception:
                    globals()['device'] = None
                time.sleep(1.0)
            else:
                time.sleep(1.0)
            continue
        if PROFILE == 'A' and ELEMENTS_A and ov is None:
            if last_logged != 'profileA':
                logline('PROFILE A active: %s' % ', '.join('%s->mon%d' % (e[0], e[2]) for e in ELEMENTS_A))
                last_logged = 'profileA'
            render_profile_a(read_mon())
            continue
        if ov is None and (now - last_recompute >= RECOMPUTE):
            station = count_agents()            # subagent sessions in LXC (added to units)
            me, subs = read_me_subs()           # me=orchestrator sessions (markers), subs=laptop subagents
            cards = read_waiting()              # global cards (panel) -> GREEN dips (NOT red)
            prompt = read_prompt()              # blocking decisions (prompt markers + red-extra) -> RED transitions
            health = read_health()
            bg = subs + station                 # BACKGROUND workers (laptop subagents + container sessions)
            blue_active = (me + bg) >= 1        # work = an orchestrator session OR any background worker
            blue_n = 0 if bg == 0 else bg       # dips count ONLY background workers; a lone orchestrator
                                                # (bg=0) stays STEADY blue instead of dipping once
            poke_current = prompt               # red poke = ONLY blocking decisions (not cards)
            gpu = gpu_util() if (prompt == 0 and health != 'RED' and blue_active) else 0
            cur = decide(cards, health, blue_active, blue_n, gpu)
            write_state(station, me, subs, cards, prompt, health, gpu, cur[0], cur[1])
            last_recompute = now
            if cur != last_logged:
                logline('state led=%s n=%d station=%d me=%d subs=%d cards=%d prompt=%d health=%s gpu=%d'
                        % (cur[0], cur[1], station, me, subs, cards, prompt, health, gpu))
                last_logged = cur
        target = ov if ov is not None else cur
        poke_pending = 0 if ov is not None else poke_current   # override (test/demo) -> no pokes
        if ov is not None and ov != last_logged:
            logline('OVERRIDE led=%s n=%d' % (ov[0], ov[1]))
            last_logged = ov
        apply(target, poke_pending)

if __name__ == '__main__':
    main()
