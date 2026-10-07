#!/usr/bin/env python3
"""cctop: Claude Code usage + laptop health, in the terminal.

Reads Claude Code's local transcripts (~/.claude/projects/**/*.jsonl) and live
session files (~/.claude/sessions/*.json). Standard library only.
"""

import curses
import fcntl
import glob
import json
import locale
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque
from datetime import datetime, timedelta

CLAUDE = os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude"))
PROJECTS = os.path.join(CLAUDE, "projects")
SESSIONS = os.path.join(CLAUDE, "sessions")
HOME = os.path.expanduser("~")

# Themes. Keys are roles: fg text, muted/dim secondary text, faint empty squares and rules,
# line box borders, teal success (green), clay danger (red), sand warning, rose accent,
# lav extra, bg background. CCTOP_THEME picks one; "github" paints its own background.
THEMES = {
    "github": {  # GitHub dark
        "paint": True,
        "colors": {
            "fg": "#e6edf3", "muted": "#8b949e", "dim": "#6e7681", "faint": "#30363d",
            "line": "#30363d", "rose": "#58a6ff", "teal": "#3fb950", "sand": "#d29922",
            "clay": "#f85149", "lav": "#bc8cff", "bg": "#0d1117",
        },
    },
    "horizon": {  # Last Horizon (~/Projects/smh-design-system/tokens/tokens.css)
        "paint": False,
        "colors": {
            "fg": "#e2dddc", "muted": "#8a8588", "dim": "#6e6769", "faint": "#3a3437",
            "line": "#8a8588", "rose": "#b59790", "teal": "#87a9b0", "sand": "#c9ae86",
            "clay": "#c38b7b", "lav": "#a5a0b6", "bg": "#0c0b0c",
        },
    },
}
THEME = THEMES.get(os.environ.get("CCTOP_THEME", "github"), THEMES["github"])
PALETTE = THEME["colors"]
BASIC = {"fg": curses.COLOR_WHITE, "muted": curses.COLOR_WHITE, "dim": curses.COLOR_WHITE,
         "faint": curses.COLOR_WHITE, "line": curses.COLOR_WHITE, "rose": curses.COLOR_MAGENTA,
         "teal": curses.COLOR_GREEN, "sand": curses.COLOR_YELLOW, "clay": curses.COLOR_RED,
         "lav": curses.COLOR_BLUE, "bg": curses.COLOR_BLACK}
C = {}
COLNUM = {}

METRICS = [("all", "all tokens"), ("io", "input + output"), ("out", "output only")]
TOOL_KEYS = ("description", "command", "file_path", "pattern", "query", "url", "prompt", "skill")


# ---------------------------------------------------------------- formatting

def fmt_n(n):
    n = float(n)
    for unit, div in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(n) >= div:
            v = n / div
            return f"{v:.1f}{unit}" if v < 100 else f"{v:.0f}{unit}"
    return f"{n:.0f}"


def fmt_dur(sec):
    sec = int(max(sec, 0))
    h, m = divmod(sec // 60, 60)
    if h >= 24:
        return f"{h // 24}d{h % 24}h"
    return f"{h}h{m:02d}m" if h else f"{m}m"


def fmt_secs(sec):
    sec = int(max(sec, 0))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60:02d}s"
    return fmt_dur(sec)


# Context windows (tokens). Every current model has 1M except Haiku 4.5 (200K).
def env_fraction(name, default):
    """A 0-1 fraction from the environment; "40", "40%" and "0.4" all work, junk falls back."""
    raw = os.environ.get(name, "").strip().rstrip("%")
    try:
        v = float(raw)
    except ValueError:
        return default
    v = v / 100 if v > 1 else v
    return v if 0 < v <= 1 else default


CTX_WARN = env_fraction("CCTOP_CTX_WARN", 0.40)      # remind: clear when you switch tasks
CTX_URGENT = env_fraction("CCTOP_CTX_URGENT", 0.70)  # warn: clear or compact now


def context_window(model, peak=0):
    m = (model or "").lower()
    window = 200_000 if "haiku" in m else 1_000_000
    if "[1m]" in m:
        window = 1_000_000
    return max(window, peak)  # never report more than 100% for a model we guessed wrong


def fmt_window(n):
    return f"{n // 1_000_000}M" if n % 1_000_000 == 0 else fmt_n(n)


def context_state(s):
    """(fraction of the window in use, window size, severity 0 ok / 1 remind / 2 urgent)."""
    if not s.ctx:
        return 0.0, context_window(s.model), 0
    window = context_window(s.model, s.ctx_peak)
    frac = s.ctx / window
    return frac, window, 2 if frac >= CTX_URGENT else 1 if frac >= CTX_WARN else 0


def short_model(m):
    hit = re.match(r"claude-([a-z]+)-(\d+)-(\d+)", m or "")
    if hit:
        return f"{hit[1]} {hit[2]}.{hit[3]}"
    return (m or "?").replace("claude-", "")


def short_path(p):
    if not p:
        return "?"
    return "~" + p[len(HOME):] if p.startswith(HOME) else p


def one_line(s, n=200):
    return " ".join(str(s).split())[:n]


def tool_summary(name, inp):
    if isinstance(inp, dict):
        for k in TOOL_KEYS:
            if inp.get(k):
                v = inp[k]
                if k == "file_path":
                    v = os.path.basename(v)
                return f"{name.lower()}  {one_line(v)}"
    return name.lower()


# ---------------------------------------------------------------- claude data

def parse_ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
    except (AttributeError, ValueError):
        return None


def line_count(s):
    return s.count("\n") + 1 if s else 0


class Session:
    def __init__(self, sid):
        self.sid = sid
        self.title = ""
        self.prompt = ""
        self.prompts = deque(maxlen=6)  # (ts, text)
        self.first_prompt = ""
        self.recap = ""
        self.model = ""
        self.cwd = ""
        self.cwds = set()
        self.ctx = 0
        self.ctx_peak = 0
        self.pending = {}          # tool_use id -> (name, summary)
        self.agents_launched = []  # timestamps of Agent tool calls
        self.agents = {}           # tool_use id -> {desc, type, ts, status, bg}
        self.last_tool = ""
        self.skills = []
        self.version = ""
        self.entrypoint = ""
        self.perm_mode = ""
        self.effort = ""
        self.branch = ""
        self.first_ts = None
        self.last_ts = None
        self.n_prompts = 0
        self.tokens = [0, 0, 0, 0]  # input, output, cache write, cache read
        self.mcp = defaultdict(int)
        self.web = 0
        self.activity = deque(maxlen=200)  # dicts: ts, name, summary, status, end
        self.inflight = {}         # tool_use id -> activity dict
        self.files = {}            # path -> {op, added, removed, ts, n}
        self.todos = []


class Usage:
    """Incrementally parses every transcript; only new bytes are read each refresh."""

    def __init__(self):
        self.offsets = {}
        self.seen = set()
        self.events = []  # (local datetime, model, project, sid, inp, out, cache_w, cache_r)
        self.sessions = {}

    def session(self, sid):
        if sid not in self.sessions:
            self.sessions[sid] = Session(sid)
        return self.sessions[sid]

    def refresh(self):
        for path in glob.glob(os.path.join(PROJECTS, "**", "*.jsonl"), recursive=True):
            self.parse_file(path)
        self.events.sort(key=lambda e: e[0])

    def parse_file(self, path):
        try:
            size = os.path.getsize(path)
        except OSError:
            return
        off = self.offsets.get(path, 0)
        if size < off:
            off = 0
        if size == off:
            return
        with open(path, "rb") as f:
            f.seek(off)
            chunk = f.read()
        end = chunk.rfind(b"\n")
        if end < 0:
            return
        self.offsets[path] = off + end + 1
        rel = os.path.relpath(path, PROJECTS).split(os.sep)
        if len(rel) < 2:
            return  # a stray file outside any project folder
        sid = rel[1].removesuffix(".jsonl")
        sub = len(rel) > 2
        for line in chunk[:end].splitlines():
            try:
                self.ingest(json.loads(line), sid, sub)
            except (ValueError, TypeError, AttributeError, KeyError):
                pass

    def ingest(self, d, sid, sub):
        s = self.session(sid)
        kind = d.get("type")
        if sub:
            if kind == "assistant":
                self.count_usage(d, s, sid, sub)
            return
        if kind == "ai-title":
            s.title = d.get("aiTitle") or s.title
            return
        if kind == "last-prompt" and d.get("lastPrompt"):
            s.prompt = d["lastPrompt"]
            return
        if kind == "permission-mode":
            s.perm_mode = d.get("permissionMode") or s.perm_mode
            return
        if kind == "system":
            if d.get("subtype") == "away_summary" and d.get("content"):
                s.recap = d["content"]
            return
        if kind not in ("assistant", "user"):
            return

        ts = parse_ts(d.get("timestamp"))
        if ts:
            s.first_ts = s.first_ts or ts
            s.last_ts = ts
        if d.get("cwd"):
            s.cwds.add(d["cwd"])
            s.cwd = s.cwd or d["cwd"]  # launch dir; later cwds just follow `cd`
        s.version = d.get("version") or s.version
        s.entrypoint = d.get("entrypoint") or s.entrypoint
        s.branch = d.get("gitBranch") or s.branch
        s.perm_mode = d.get("permissionMode") or s.perm_mode
        s.effort = d.get("effort") or s.effort
        msg = d.get("message") or {}
        content = msg.get("content")

        if kind == "user":
            texts = []
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_result":
                        self.tool_done(s, b, ts)
                    elif b.get("type") == "text":
                        texts.append(b.get("text", ""))
            for t in texts:
                if t and not t.startswith("<") and not d.get("isMeta"):
                    s.prompt = t
                    s.first_prompt = s.first_prompt or t
                    s.prompts.append((ts, t))
                    s.n_prompts += 1
            return

        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    self.tool_start(s, b, ts)
        self.count_usage(d, s, sid, sub)

    def tool_start(self, s, b, ts):
        name, inp, tid = b.get("name", "?"), b.get("input") or {}, b.get("id")
        summ = tool_summary(name, inp)
        s.pending[tid] = (name, summ)
        s.last_tool = summ
        path = inp.get("file_path") or inp.get("notebook_path")
        act = {"ts": ts, "name": name, "summary": summ, "status": "run", "end": None, "path": path,
               "seen": time.monotonic()}
        s.activity.append(act)
        s.inflight[tid] = act
        if name.startswith("mcp__"):
            s.mcp[name.split("__")[1]] += 1
        if name in ("WebSearch", "WebFetch"):
            s.web += 1
        if name == "Skill" and inp.get("skill"):
            if inp["skill"] in s.skills:
                s.skills.remove(inp["skill"])
            s.skills.append(inp["skill"])
        if name in ("Agent", "Task"):
            s.agents_launched.append(ts.strftime("%Y-%m-%d") if ts else "")
            s.agents[tid] = {"desc": inp.get("description") or one_line(inp.get("prompt", ""), 60),
                             "type": inp.get("subagent_type") or "general", "ts": ts,
                             "status": "running", "bg": bool(inp.get("run_in_background"))}
        if name == "TodoWrite" and isinstance(inp.get("todos"), list):
            s.todos = inp["todos"]
        if path:
            f = s.files.setdefault(path, {"op": "read", "added": 0, "removed": 0, "n": 0})
            f["ts"] = ts
            f["n"] += 1
            add = rem = 0
            if name == "Edit":
                add, rem = line_count(inp.get("new_string")), line_count(inp.get("old_string"))
            elif name == "MultiEdit":
                for e in inp.get("edits") or []:
                    add += line_count(e.get("new_string"))
                    rem += line_count(e.get("old_string"))
            elif name == "Write":
                add = line_count(inp.get("content"))
            if name in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
                f["op"] = "write" if name == "Write" and f["op"] == "read" else "edit"
            f["added"] += add
            f["removed"] += rem

    def tool_done(self, s, b, ts):
        tid = b.get("tool_use_id")
        s.pending.pop(tid, None)
        act = s.inflight.pop(tid, None)
        err = bool(b.get("is_error"))
        if act:
            act["status"] = "err" if err else "ok"
            act["end"] = ts
        if tid in s.agents:
            text = str(b.get("content"))[:400].lower()
            s.agents[tid]["status"] = "error" if err else (
                "background" if s.agents[tid]["bg"] or "background" in text else "done")

    def count_usage(self, d, s, sid, sub):
        msg = d.get("message") or {}
        model = msg.get("model", "")
        u = msg.get("usage")
        if not u or model == "<synthetic>":
            return
        key = msg.get("id") or d.get("uuid")
        if key in self.seen:
            return
        self.seen.add(key)
        inp = u.get("input_tokens", 0) or 0
        cw = u.get("cache_creation_input_tokens", 0) or 0
        cr = u.get("cache_read_input_tokens", 0) or 0
        out = u.get("output_tokens", 0) or 0
        for i, v in enumerate((inp, out, cw, cr)):
            s.tokens[i] += v
        if not sub:
            s.model = model or s.model
            s.ctx = inp + cw + cr
            s.ctx_peak = max(s.ctx_peak, s.ctx)
        ts = parse_ts(d.get("timestamp"))
        if ts:
            self.events.append((ts, model, s.cwd, sid, inp, out, cw, cr))


def metric_value(e, metric):
    _, _, _, _, inp, out, cw, cr = e
    if metric == "out":
        return out
    if metric == "io":
        return inp + out
    return inp + out + cw + cr


def live_sessions(usage):
    rows = []
    for path in glob.glob(os.path.join(SESSIONS, "*.json")):
        try:
            with open(path) as f:
                info = json.load(f)
        except (OSError, ValueError):
            continue
        pid = info.get("pid")
        if not pid or not os.path.exists(f"/proc/{pid}"):
            continue
        sid = info.get("sessionId", "")
        s = usage.sessions.get(sid) or Session(sid)
        agents = 0
        for sa in glob.glob(os.path.join(PROJECTS, "*", sid, "subagents", "*.jsonl")):
            try:
                if time.time() - os.path.getmtime(sa) < 90:
                    agents += 1
            except OSError:
                pass
        agents = max(agents, sum(1 for n, _ in s.pending.values() if n in ("Agent", "Task")))
        today = datetime.now().strftime("%Y-%m-%d")
        rows.append({
            "name": info.get("name") or sid[:8],
            "status": info.get("status", "?"),
            "kind": info.get("kind", ""),
            "pid": pid,
            "entrypoint": info.get("entrypoint", ""),
            "status_since": (info.get("statusUpdatedAt") or 0) / 1000,
            "cwd": info.get("cwd", s.cwd),
            "started": (info.get("startedAt") or 0) / 1000,
            "updated": info.get("updatedAt") or 0,
            "session": s,
            "agents": agents,
            "agents_today": sum(1 for t in s.agents_launched if t.startswith(today)),
        })
    rows.sort(key=lambda r: (r["status"] != "busy", -r["updated"]))
    return rows


class Limits:
    """Plan limits (5h + weekly) from the same endpoint Claude Code's /usage reads.

    Uses the OAuth token Claude Code already stores; it is only ever sent to
    api.anthropic.com. The endpoint rate-limits, so every cctop instance shares one
    cache file, fetches at most every 5 minutes and backs off 15 minutes on a 429.
    """

    URL = "https://api.anthropic.com/api/oauth/usage"
    CACHE = os.path.expanduser("~/.cache/cctop/limits.json")
    EVERY, BACKOFF = 300, 900

    def __init__(self):
        self.data = None
        self.fetched = 0
        self.error = ""
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while True:
            try:
                self.tick()
            except Exception as e:  # never let the poller die
                self.error = f"limits unavailable ({type(e).__name__})"
            time.sleep(30)

    def load(self):
        try:
            with open(self.CACHE) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save(self, cache):
        os.makedirs(os.path.dirname(self.CACHE), exist_ok=True)
        tmp = self.CACHE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cache, f)
        os.replace(tmp, self.CACHE)

    def tick(self):
        cache = self.load()
        if cache.get("data"):
            self.data, self.fetched = cache["data"], cache.get("fetched", 0)
        now = time.time()
        if now < cache.get("backoff_until", 0):
            self.error = f"rate limited, retry in {fmt_dur(cache['backoff_until'] - now)}"
            return
        if now - self.fetched < self.EVERY:
            self.error = ""
            return
        with open(os.path.join(CLAUDE, ".credentials.json")) as f:
            oauth = json.load(f)["claudeAiOauth"]
        if oauth.get("expiresAt", 0) / 1000 < now:
            self.error = "token expired, open claude to refresh"
            return
        req = urllib.request.Request(self.URL, headers={
            "Authorization": f"Bearer {oauth['accessToken']}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "cctop",
        })
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                self.data, self.fetched = json.load(resp), now
            self.error = ""
            self.save({"data": self.data, "fetched": now})
        except urllib.error.HTTPError as e:
            cache["backoff_until"] = now + (self.BACKOFF if e.code == 429 else self.EVERY)
            self.save(cache)
            self.error = "rate limited, retry in 15m" if e.code == 429 else f"limits unavailable (HTTP {e.code})"
        except (OSError, ValueError) as e:
            self.error = f"limits unavailable ({type(e).__name__})"


_git_cache = {}


def git_info(cwd):
    hit = _git_cache.get(cwd)
    if hit and time.time() - hit[0] < 15:
        return hit[1]

    def git(*args):
        try:
            out = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=2)
            return out.stdout.strip() if out.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None

    info = None
    top = git("rev-parse", "--show-toplevel") if cwd else None
    if top:
        remote = git("remote", "get-url", "origin") or ""
        m = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?$", remote)
        info = {"repo": m[1] if m else os.path.basename(top) + " (local)",
                "branch": git("branch", "--show-current") or "",
                "dirty": len((git("status", "--porcelain") or "").splitlines()),
                "github": "github.com" in remote}
    _git_cache[cwd] = (time.time(), info)
    return info


def github_user():
    hosts = read(os.path.expanduser("~/.config/gh/hosts.yml"))
    m = re.search(r"^github\.com:\n(?:\s+.*\n)*?\s+user:\s*(\S+)", hosts + "\n", re.M)
    return m[1] if m else ""


_activity_cache = {}


def git_activity(cwd, days=371):
    """Commits per day, push times (from the remote-tracking reflog) and ahead/behind counts."""
    hit = _activity_cache.get(cwd)
    if hit and time.time() - hit[0] < 30:
        return hit[1]

    def git(*args):
        try:
            out = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=3)
            return out.stdout.strip() if out.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return ""

    info = None
    if git("rev-parse", "--show-toplevel"):
        per_day = {}
        commits = [int(t) for t in git("log", f"--since={days} days ago", "--format=%ct").split()]
        for t in commits:
            d = datetime.fromtimestamp(t).date()
            per_day[d] = per_day.get(d, 0) + 1
        upstream = git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        pushes = []
        if upstream:
            for line in git("reflog", "show", "--date=unix", "--format=%gs|%gd", "refs/remotes/" + upstream).splitlines():
                m = re.match(r"update by push\|.*@\{(\d+)\}", line)
                if m:
                    pushes.append(int(m[1]))
        lr = git("rev-list", "--left-right", "--count", "@{u}...HEAD").split() if upstream else []
        info = {"per_day": per_day, "commits": commits, "pushes": pushes, "upstream": upstream,
                "behind": int(lr[0]) if lr else 0, "ahead": int(lr[1]) if lr else 0}
    _activity_cache[cwd] = (time.time(), info)
    return info


# ---------------------------------------------------------------- system data

def read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def hwmon():
    temps = {}
    fan = None
    for d in glob.glob("/sys/class/hwmon/hwmon*"):
        name = read(f"{d}/name")
        if name == "coretemp":
            for lab in glob.glob(f"{d}/temp*_label"):
                if read(lab).startswith("Package"):
                    temps["cpu"] = int(read(lab.replace("_label", "_input"), "0")) / 1000
        elif name in ("k10temp", "zenpower") and "cpu" not in temps:
            temps["cpu"] = int(read(f"{d}/temp1_input", "0")) / 1000
        elif name == "nvme":
            temps["nvme"] = int(read(f"{d}/temp1_input", "0")) / 1000
        if fan is None and os.path.exists(f"{d}/fan1_input"):
            fan = int(read(f"{d}/fan1_input", "0"))
    return temps, fan


class System:
    def __init__(self):
        self.prev_cpu = None
        self.cpu_hist = deque(maxlen=60)
        self.temp_hist = deque(maxlen=60)
        un = os.uname()
        osr = dict(re.findall(r'^(\w+)="?([^"\n]*)"?', read("/etc/os-release"), re.M))
        self.host = socket.gethostname()
        self.os = osr.get("PRETTY_NAME", un.sysname).lower()
        self.kernel = un.release.split("-")[0]
        self.user = os.environ.get("USER", "")
        cpu = re.search(r"model name\s*:\s*(.*)", read("/proc/cpuinfo"))
        self.cpu_model = re.sub(r"\(R\)|\(TM\)|CPU|@.*", "", cpu[1]).split()[-1].lower() if cpu else ""
        self.snap = {}

    def refresh(self):
        fields = [int(x) for x in read("/proc/stat").splitlines()[0].split()[1:]]
        idle, total = fields[3] + fields[4], sum(fields)
        cpu = 0.0
        if self.prev_cpu:
            dt = total - self.prev_cpu[1]
            cpu = 100 * (1 - (idle - self.prev_cpu[0]) / dt) if dt else 0.0
            self.cpu_hist.append(cpu)
        self.prev_cpu = (idle, total)

        mi = dict((k, int(v.split()[0])) for k, v in
                  (l.split(":", 1) for l in read("/proc/meminfo").splitlines()))
        temps, fan = hwmon()
        if "cpu" in temps:
            self.temp_hist.append(temps["cpu"])
        disk = shutil.disk_usage("/")
        bat = {}
        for b in glob.glob("/sys/class/power_supply/BAT*"):
            bat = {"pct": int(read(f"{b}/capacity", "0")), "status": read(f"{b}/status").lower(),
                   "watts": int(read(f"{b}/power_now", "0")) / 1e6}
            break
        self.snap = {
            "cpu": cpu, "temps": temps, "fan": fan,
            "mem": ((mi["MemTotal"] - mi["MemAvailable"]) * 1024, mi["MemTotal"] * 1024),
            "swap": ((mi.get("SwapTotal", 0) - mi.get("SwapFree", 0)) * 1024, mi.get("SwapTotal", 0) * 1024),
            "disk": (disk.used, disk.total),
            "bat": bat,
            "load": os.getloadavg(),
            "uptime": float(read("/proc/uptime", "0").split()[0]),
        }


# ---------------------------------------------------------------- projects
#
# Every folder in ~/Projects is a project. Its notes live in <project>/_project/:
#   STATUS.md      written by Claude: frontmatter + "Where we left off" + "Next steps"
#   prd.md, drd.md, proposal.md, decisions.md ...   written by Claude when they exist
#   sessions.json  written by `cctop hook` after every Claude Code reply
#   log.md         rendered from sessions.json, for reading in an editor
# cctop only ever reads these; nothing in the UI can delete them.

PROJECTS_HOME = os.path.expanduser(os.environ.get("CCTOP_PROJECTS", "~/Projects"))
META = "_project"
DOC_NAMES = {"status.md": "Status", "prd.md": "PRD", "drd.md": "DRD", "proposal.md": "Proposal",
             "decisions.md": "Decisions", "log.md": "Session log", "notes.md": "Notes"}


def frontmatter(text):
    """Tiny YAML subset: `key: value`, `key: [a, b]` and `key:` followed by `- item` lines."""
    meta, body = {}, text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end > 0:
            body = text[end + 4:].lstrip("\n")
            key = None
            for line in text[3:end].splitlines():
                item = re.match(r"\s*-\s+(.*)", line)
                if item and key:
                    meta.setdefault(key, [])
                    if isinstance(meta[key], list):
                        meta[key].append(item[1].strip().strip("\"'"))
                    continue
                kv = re.match(r"([\w-]+):\s*(.*)", line)
                if kv:
                    key, val = kv[1].lower(), kv[2].strip()
                    if val.startswith("[") and val.endswith("]"):
                        meta[key] = [v.strip().strip("\"'") for v in val[1:-1].split(",") if v.strip()]
                    elif val:
                        meta[key] = val.strip("\"'")
    return meta, body


def sections(body):
    out, cur = {}, None
    for line in body.splitlines():
        if line.startswith("## "):
            cur = line[3:].strip().lower()
            out[cur] = []
        elif cur is not None:
            out[cur].append(line)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def project_dirs():
    """name -> (folder, [paths whose activity counts for this project])"""
    out = {}
    for d in sorted(glob.glob(os.path.join(PROJECTS_HOME, "*/"))):
        d = d.rstrip("/")
        name = os.path.basename(d)
        if name.startswith((".", "_")):
            continue
        meta, _ = frontmatter(read(os.path.join(d, META, "STATUS.md")))
        extra = meta.get("paths", [])
        extra = [extra] if isinstance(extra, str) else extra
        paths = [os.path.realpath(d)] + [os.path.realpath(os.path.expanduser(p)) for p in extra]
        out[name] = (d, list(dict.fromkeys(paths)))
    return out


def inside(path, roots):
    path = os.path.realpath(path)
    return any(path == r or path.startswith(r + os.sep) for r in roots)


def session_projects(s, dirs):
    """Projects a session worked on: it ran inside the folder, or edited files in it."""
    hits = {}
    for name, (_, roots) in dirs.items():
        changed = [p for p, f in s.files.items() if f["op"] != "read" and inside(p, roots)]
        if changed or any(inside(c, roots) for c in s.cwds):
            hits[name] = changed
    return hits


def record_session(s, dirs):
    if not s.first_ts or s.n_prompts == 0 or s.entrypoint.startswith("sdk"):
        return False  # sdk runs are other programs driving claude (e.g. sysdash's scan button)
    hits = session_projects(s, dirs)
    for name, changed in hits.items():
        folder = dirs[name][0]
        meta_dir = os.path.join(folder, META)
        os.makedirs(meta_dir, exist_ok=True)
        git_exclude(folder)
        # two sessions can stop at once: lock the folder so neither drops the other's entry
        lock = os.open(meta_dir, os.O_RDONLY)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            record_entry(s, name, folder, meta_dir, changed)
        except ValueError:
            pass  # sessions.json is broken: leave it for a human rather than overwrite the history
        finally:
            os.close(lock)
    return bool(hits)


def record_entry(s, name, folder, meta_dir, changed):
    path = os.path.join(meta_dir, "sessions.json")
    log = {}
    if os.path.exists(path):
        with open(path) as f:
            log = json.load(f)
    log[s.sid] = {
        "title": s.title, "started": s.first_ts.isoformat(timespec="seconds"),
        "updated": s.last_ts.isoformat(timespec="seconds"),
        "first_prompt": one_line(s.first_prompt, 300),
        "last_prompt": one_line(s.prompt, 300), "recap": one_line(s.recap, 600),
        "model": short_model(s.model), "prompts": s.n_prompts,
        "changed": [os.path.relpath(p, folder) if inside(p, [os.path.realpath(folder)]) else short_path(p)
                    for p in changed][-20:],
        "added": sum(s.files[p]["added"] for p in changed),
        "removed": sum(s.files[p]["removed"] for p in changed),
        "tokens": sum(s.tokens),
    }
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(log, f, indent=1)
    os.replace(tmp, path)
    write_log_md(name, meta_dir, log)


def write_log_md(name, meta_dir, log):
    out = [f"# {name} · session log", "",
           "Written automatically by cctop after every Claude Code reply. Put plans and status in STATUS.md.", ""]
    for sid, e in sorted(log.items(), key=lambda kv: kv[1]["updated"], reverse=True):
        when = e["updated"].replace("T", " ")[:16]
        out.append(f"## {when} · {e['title'] or 'untitled'}")
        out.append("")
        if e.get("first_prompt"):
            out.append(f"- started with: {e['first_prompt']}")
        if e.get("last_prompt") and e["last_prompt"] != e.get("first_prompt"):
            out.append(f"- last prompt: {e['last_prompt']}")
        if e.get("recap"):
            out.append(f"- recap: {e['recap']}")
        if e.get("changed"):
            out.append(f"- changed {len(e['changed'])} files (+{e['added']} −{e['removed']}): "
                       + ", ".join(e["changed"][:8]) + (" …" if len(e["changed"]) > 8 else ""))
        out.append(f"- {e['prompts']} prompts · {e['model']} · {fmt_n(e['tokens'])} tokens · session {sid[:8]}")
        out.append("")
    tmp = os.path.join(meta_dir, f"log.md.{os.getpid()}.tmp")
    with open(tmp, "w") as f:
        f.write("\n".join(out))
    os.replace(tmp, os.path.join(meta_dir, "log.md"))


def git_exclude(folder):
    """Keep _project/ out of git without touching the repo's tracked .gitignore."""
    try:
        top = subprocess.run(["git", "-C", folder, "rev-parse", "--show-toplevel", "--git-dir"],
                             capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return
    if top.returncode != 0:
        return
    toplevel, gitdir = top.stdout.split("\n")[:2]
    gitdir = gitdir if os.path.isabs(gitdir) else os.path.join(folder, gitdir)
    rel = os.path.relpath(os.path.join(folder, META), toplevel)
    excl = os.path.join(gitdir, "info", "exclude")
    entry = f"/{rel}/"
    if entry not in read(excl).splitlines():
        os.makedirs(os.path.dirname(excl), exist_ok=True)
        with open(excl, "a") as f:
            f.write(f"\n# cctop project notes\n{entry}\n")


def hook_main():
    """Stop / SessionEnd hook: record this session into its project folders. Never fails loudly."""
    try:
        data = json.load(sys.stdin)
        path = data.get("transcript_path")
        if not path or not os.path.exists(path):
            return
        u = Usage()
        u.parse_file(path)
        s = u.sessions.get(data.get("session_id")) or u.sessions.get(os.path.basename(path)[:-6])
        if s:
            record_session(s, project_dirs())
    except Exception:  # a hook must never break the Claude Code session
        pass


def backfill_main():
    u = Usage()
    u.refresh()
    dirs = project_dirs()
    n = 0
    for s in u.sessions.values():
        n += bool(record_session(s, dirs))
    print(f"recorded {n} sessions into {PROJECTS_HOME}/*/{META}/")


NO_ABOUT = "add a What we are building section to STATUS.md"
ABOUT_SECTIONS = ("what we are building", "about", "overview", "summary")


def project_about(folder, secs):
    """What the project is: STATUS.md's own section, else a proposal/PRD section, else the README intro."""
    for k in ABOUT_SECTIONS:
        if secs.get(k):
            return secs[k]
    for doc in ("proposal.md", "prd.md"):
        doc_secs = sections(frontmatter(read(os.path.join(folder, META, doc)))[1])
        for k in ABOUT_SECTIONS + ("the idea", "problem", "goals"):
            if doc_secs.get(k):
                return doc_secs[k]
    # README: the prose paragraphs before the first ## heading (no badges, nav links or tables)
    paras, cur = [], []
    for line in read(os.path.join(folder, "README.md")).splitlines() + [""]:
        if line.startswith("## "):
            break
        line = re.sub(r"<[^>]*>|!\[[^\]]*\]\([^)]*\)|\*\*|`", "", line).lstrip("> ").strip()
        if line and not line.startswith(("#", "|")):
            cur.append(line)
        elif cur:
            paras.append(" ".join(cur))
            cur = []
    return "\n\n".join([t for t in paras if len(t.replace("·", " ").split()) >= 8][:2])


def load_projects(live):
    dirs = project_dirs()
    out = []
    for name, (folder, roots) in dirs.items():
        meta_dir = os.path.join(folder, META)
        status_text = read(os.path.join(meta_dir, "STATUS.md"))
        meta, body = frontmatter(status_text)
        try:
            with open(os.path.join(meta_dir, "sessions.json")) as f:
                log = json.load(f)
        except (OSError, ValueError):
            log = {}
        sess = sorted(log.values(), key=lambda e: e["updated"], reverse=True)
        docs = []
        for f in sorted(glob.glob(os.path.join(meta_dir, "*"))):
            base = os.path.basename(f)
            if base.endswith((".json", ".tmp")) or os.path.isdir(f):
                continue
            docs.append((DOC_NAMES.get(base.lower(), base), base, os.path.getmtime(f)))
        # last real activity: newest recorded session, else newest doc Claude wrote, else the folder
        if sess:
            times = [datetime.fromisoformat(sess[0]["updated"]).timestamp()]
        else:
            times = [d[2] for d in docs if d[1] != "log.md"] or [os.path.getmtime(folder)]
        live_now = [r for r in live if any(inside(c, roots) for c in r["session"].cwds | {r["cwd"]})
                    or any(f["op"] != "read" and inside(p, roots) for p, f in r["session"].files.items())]
        secs = sections(body)
        out.append({"name": name, "folder": folder, "roots": roots, "meta": meta,
                    "sections": secs, "about": project_about(folder, secs), "has_status": bool(status_text), "sessions": sess,
                    "docs": docs, "last": max(times), "live": live_now})
    out.sort(key=lambda p: (not p["live"], -p["last"]))
    return out


_tree_cache = {}
TREE_SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", "target",
             ".next", ".cache", ".mypy_cache", ".pytest_cache", ".idea", ".vscode"}


def session_root(cur, s):
    """The folder the Live tree shows: the repo, else the session dir, else the project it edits."""
    cwd = cur["cwd"] or s.cwd or HOME
    home = os.path.realpath(HOME)
    try:
        top = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=2).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        top = ""
    if top and os.path.realpath(top) != home:
        return os.path.realpath(top)
    if os.path.realpath(cwd) != home:
        return os.path.realpath(cwd)
    # started in ~: show the project it has been editing most recently
    dirs = project_dirs()
    best, best_ts = None, None
    for name, changed in session_projects(s, dirs).items():
        ts = max((s.files[p].get("ts") or datetime.min for p in changed), default=None)
        if ts and (best_ts is None or ts > best_ts):
            best, best_ts = dirs[name][0], ts
    return os.path.realpath(best) if best else home


def _listdir(path):
    hit = _tree_cache.get(path)
    if hit and time.time() - hit[0] < 5:
        return hit[1]
    try:
        with os.scandir(path) as it:
            entries = [(e.name, e.is_dir(follow_symlinks=False)) for e in it if e.name not in TREE_SKIP]
    except OSError:
        entries = []
    entries.sort(key=lambda e: (not e[1], e[0].lower()))
    _tree_cache[path] = (time.time(), entries)
    return entries


def file_tree(root, touched, active, max_rows=600):
    """Rows for an indented tree. Shallow folders and folders holding touched files are open."""
    hot = set(touched) | ({active} if active else set())
    rows = []

    def walk(path, depth, lasts):
        entries = _listdir(path)
        for i, (name, is_dir) in enumerate(entries):
            if len(rows) >= max_rows:
                return
            full = os.path.join(path, name)
            last = i == len(entries) - 1
            prefix = "".join("   " if l else "│  " for l in lasts) + ("└─ " if last else "├─ ")
            holds_hot = is_dir and any(h.startswith(full + os.sep) for h in hot)
            open_ = is_dir and (depth < 1 or holds_hot) and depth < 6
            f = touched.get(full)
            rows.append({"prefix": prefix, "name": name, "dir": is_dir, "collapsed": is_dir and not open_,
                         "op": f["op"] if f else "", "active": full == active})
            if open_:
                walk(full, depth + 1, lasts + [last])

    walk(root, 0, [])
    return rows


_gd_cache = {}


def git_details(path):
    hit = _gd_cache.get(path)
    if hit and time.time() - hit[0] < 5:
        return hit[1]

    def git(*args, raw=False):
        try:
            out = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, timeout=3)
            if out.returncode != 0:
                return None
            return out.stdout if raw else out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None

    info = None
    if path and git("rev-parse", "--show-toplevel"):
        upstream = git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        ahead = behind = 0
        if upstream:
            counts = (git("rev-list", "--left-right", "--count", "@{u}...HEAD") or "0 0").split()
            behind, ahead = int(counts[0]), int(counts[1])
        staged = modified = untracked = 0
        for line in (git("status", "--porcelain", raw=True) or "").splitlines():  # keep leading spaces
            if line.startswith("??"):
                untracked += 1
                continue
            staged += line[0] not in " ?"
            modified += len(line) > 1 and line[1] != " "
        files, add, rem = [], 0, 0
        for line in (git("diff", "HEAD", "--numstat") or "").splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                a, d = (int(v) if v.isdigit() else 0 for v in parts[:2])  # binary files show "-"
                files.append((parts[2], a, d))
                add, rem = add + a, rem + d
        files.sort(key=lambda f: -(f[1] + f[2]))
        c_add = c_rem = 0
        for line in (git("show", "--numstat", "--format=", "HEAD") or "").splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                c_add += int(parts[0]) if parts[0].isdigit() else 0
                c_rem += int(parts[1]) if parts[1].isdigit() else 0
        remote = git("remote", "get-url", "origin") or ""
        m = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?$", remote)
        gitdir = git("rev-parse", "--absolute-git-dir") or ""
        fetch_head = os.path.join(gitdir, "FETCH_HEAD")
        fetched = ""
        if os.path.exists(fetch_head):
            fetched = fmt_dur(time.time() - os.path.getmtime(fetch_head)) + " ago"
        info = {
            "branch": git("branch", "--show-current") or "detached",
            "upstream": upstream or "",
            "ahead": ahead, "behind": behind,
            "staged": staged, "modified": modified, "untracked": untracked,
            "files": files, "add": add, "del": rem, "commit_add": c_add, "commit_del": c_rem,
            "last_commit": git("log", "-1", "--format=%h %s"),
            # reflog date of the remote-tracking ref, i.e. when it last moved (not the commit date)
            "pushed": re.sub(r".*@\{(.*)\}$", r"\1", git("log", "-g", "-1", "--date=relative", "--format=%gd",
                                                       f"refs/remotes/{upstream}") or "") if upstream else "",
            "fetched": fetched,
            "remote": (m[1] + (" on github" if "github.com" in remote else "")) if m else remote,
            "stash": len((git("stash", "list") or "").splitlines()),
        }
    _gd_cache[path] = (time.time(), info)
    return info


# ---------------------------------------------------------------- drawing

VERSION = "0.3.0"
ITALIC = getattr(curses, "A_ITALIC", 0)
TABS = ["Overview", "Projects", "Live", "Usage", "System"]

# 8px-tall bitmaps, rendered two pixel rows per cell with half blocks; lowercase only
LOGO = {
    "a": ["......", "......", ".####.", "....##", ".#####", "##..##", ".#####", "......"],
    "b": ["##....", "##....", "#####.", "##..##", "##..##", "##..##", "#####.", "......"],
    "c": ["......", "......", ".#####", "##....", "##....", "##....", ".#####", "......"],
    "d": ["....##", "....##", ".#####", "##..##", "##..##", "##..##", ".#####", "......"],
    "e": ["......", "......", ".####.", "##..##", "######", "##....", ".#####", "......"],
    "f": ["..###", ".##..", "####.", ".##..", ".##..", ".##..", ".##..", "....."],
    "g": ["......", "......", ".#####", "##..##", "##..##", ".#####", "....##", "#####."],
    "h": ["##....", "##....", "#####.", "##..##", "##..##", "##..##", "##..##", "......"],
    "i": ["##", "..", "##", "##", "##", "##", "##", ".."],
    "j": ["..##", "....", "..##", "..##", "..##", "..##", "..##", "###."],
    "k": ["##....", "##....", "##..##", "##.##.", "####..", "##.##.", "##..##", "......"],
    "l": ["##.", "##.", "##.", "##.", "##.", "##.", ".##", "..."],
    "m": ["........", "........", "#######.", "##.##.##", "##.##.##", "##.##.##", "##.##.##", "........"],
    "n": ["......", "......", "#####.", "##..##", "##..##", "##..##", "##..##", "......"],
    "o": ["......", "......", ".####.", "##..##", "##..##", "##..##", ".####.", "......"],
    "p": ["......", "......", "#####.", "##..##", "##..##", "#####.", "##....", "##...."],
    "q": ["......", "......", ".#####", "##..##", "##..##", ".#####", "....##", "....##"],
    "r": [".....", ".....", "##.##", "###..", "##...", "##...", "##...", "....."],
    "s": ["......", "......", ".#####", "##....", ".####.", "....##", "#####.", "......"],
    "t": [".##...", ".##...", "######", ".##...", ".##...", ".##...", "..####", "......"],
    "u": ["......", "......", "##..##", "##..##", "##..##", "##..##", ".#####", "......"],
    "v": ["......", "......", "##..##", "##..##", "##..##", ".####.", "..##..", "......"],
    "w": ["........", "........", "##.##.##", "##.##.##", "##.##.##", "##.##.##", ".######.", "........"],
    "x": ["......", "......", "##..##", ".####.", "..##..", ".####.", "##..##", "......"],
    "y": ["......", "......", "##..##", "##..##", "##..##", ".#####", "....##", "#####."],
    "z": ["......", "......", "######", "...##.", "..##..", ".##...", "######", "......"],
    ".": ["..", "..", "..", "..", "..", "##", "##", ".."],
    ",": ["..", "..", "..", "..", "..", "##", "##", "#."],
    "?": [".####.", "##..##", "...##.", "..##..", "..##..", "......", "..##..", "......"],
    "!": ["##", "##", "##", "##", "##", "..", "##", ".."],
    "'": ["##", "##", "..", "..", "..", "..", "..", ".."],
    " ": ["...", "...", "...", "...", "...", "...", "...", "..."],
}


def logo_rows(text):
    rows = []
    for r in range(0, 8, 2):
        parts = []
        for g in (LOGO.get(ch, LOGO[" "]) for ch in text.lower()):
            parts.append("".join(" ▀▄█"[(a == "#") + 2 * (b == "#")] for a, b in zip(g[r], g[r + 1])))
        rows.append(" ".join(parts))
    return rows



# ---- Overview title: a greeting for the time of day that turns into the logo after a few
# seconds, with motion in the style of ttfx in Omarchy's screensaver. CCTOP_MOTION=off keeps
# the greeting and drops the motion.

GREETINGS = [  # (from this hour, options, short one): one that fits is picked at random,
    # else the short one, else the shortest as plain text
    (0, ["still up?", "late one, {u}?", "one more session?"], "still up?"),
    (5, ["good morning.", "morning, {u}.", "have a good session."], "morning."),
    (12, ["good afternoon.", "afternoon, {u}.", "have a good session."], "hi there."),
    (17, ["good evening.", "evening, {u}.", "have a good session."], "evening."),
    (22, ["still up?", "late one, {u}?", "one more session?"], "still up?"),
]
MOTION = os.environ.get("CCTOP_MOTION", "on").lower() not in ("0", "off", "no", "false")
INTRO_HOLD, FX_IN, FX_OUT = 5.0, 1.2, 0.5  # seconds: greeting on screen, effect in, effect out
SCRAMBLE = "▖▗▘▝▚▞▙▛▜▟░▒▓"
GROW_S = 0.5  # seconds bars take to grow in when a view opens
SHIMMER_EVERY, SHIMMER_S = 7.0, 1.4  # the settled logo shimmers for 1.4 s every 7 s


def glow(f):
    """Effect colours from cool to bright, f from 0 to 1."""
    return (C["lav"], C["rose"], C["teal"])[min(2, max(0, int(f * 3)))]


# Effects take cells (row, col, char, random 0-1, final attr) and progress p from 0 to 1, and
# return what to draw now as (row, col, char, attr). At p = 1 every cell is in place.

def fx_decrypt(cells, p, gw, tick):
    """Each cell flickers through block shapes, then locks in at its own moment."""
    out = []
    for r, c, ch, u, final in cells:
        lock = 0.2 + 0.7 * u
        if p >= lock:
            out.append((r, c, ch, final))
        elif p >= lock * 0.4:
            out.append((r, c, SCRAMBLE[(tick + 7 * c + 13 * r) % len(SCRAMBLE)], glow(c / gw)))
    return out


def fx_beams(cells, p, gw, tick):
    """A slanted beam sweeps left to right; cells light up as it passes and cool down behind it."""
    head = p * (gw + 22)
    out = []
    for r, c, ch, u, final in cells:
        d = head - c - 2 * (r + 1)
        if d >= 12:
            out.append((r, c, ch, final))
        elif d >= 2:
            out.append((r, c, ch, glow(1 - (d - 2) / 10)))
        elif d >= 0:
            out.append((r, c, "█", C["fg"] | curses.A_BOLD))
    return out


def fx_assemble(cells, p, gw, tick):
    """Cells fly out from the centre to their places, each with a small delay of its own."""
    out = []
    cx, cy = gw / 2, 1.5
    for r, c, ch, u, final in cells:
        q = min(1.0, (p - 0.35 * u) / 0.65)
        if q > 0:
            e = 1 - (1 - q) ** 3  # ease out
            out.append((round(cy + (r - cy) * e), round(cx + (c - cx) * e), ch, final if q >= 1 else glow(u)))
    return out


def fx_slide(cells, p, gw, tick):
    """Rows slide in from alternating sides, one after another."""
    out = []
    for r, c, ch, u, final in cells:
        q = min(1.0, (p - 0.12 * (r + 1)) / 0.64)
        if q > 0:
            e = 1 - (1 - q) ** 3
            off = (1 - e) * (gw + 10) * (-1 if r % 2 else 1)
            out.append((r, round(c + off), ch, final if q >= 1 else glow(c / gw)))
    return out


def fx_rise(cells, p, gw, tick):
    """Columns rise from below the bottom edge of the title, left to right in a wave. Rows under
    the edge are not drawn, so the letters come up out of it."""
    out = []
    for r, c, ch, u, final in cells:
        q = min(1.0, (p - 0.45 * c / gw) / 0.55)
        if q > 0:
            e = 1 - (1 - q) ** 3
            row = round(r + (1 - e) * LOGO_ROWS)
            if row < LOGO_ROWS:
                out.append((row, c, ch, final if q >= 1 else glow(1 - (1 - e) * 0.9)))
    return out


def fx_shimmer(cells, p, gw, tick):
    """Idle: a wave of the rise colours passes over the settled letters; nothing moves."""
    head = p * (gw + 20)
    out = []
    for r, c, ch, u, final in cells:
        d = head - c - 2 * r
        out.append((r, c, ch, glow(1 - d / 12) if 0 <= d < 12 else final))
    return out


EFFECTS = [fx_decrypt, fx_beams, fx_assemble, fx_slide]  # for the greeting; the logo always rises
LOGO_ROWS = 4  # block letters are 4 terminal rows tall


class Intro:
    """The Overview title: the greeting animates in with a small "cctop" above its right end,
    stays until INTRO_HOLD, scrambles away with the label, then the logo rises from below."""

    def __init__(self, user):
        self.user = user or "you"
        self.motion = MOTION
        self.rng = random.Random()
        self.fx = self.rng.choice(EFFECTS)
        self.t0 = None  # starts on the first frame, after the transcripts have loaded
        self.text = None
        self.grids = {}

    def active(self):
        """True while the title is moving and wants fast frames."""
        if not self.motion:
            return False
        if self.t0 is None:
            return True
        t = time.monotonic() - self.t0 - (INTRO_HOLD + FX_OUT + FX_IN)
        return t < 0 or t % SHIMMER_EVERY >= SHIMMER_EVERY - SHIMMER_S

    def pick(self, width):
        hour = datetime.now().hour
        opts, short = [(o, sh) for start, o, sh in GREETINGS if start <= hour][-1]
        options = [o.format(u=self.user) for o in opts]
        fits = [o for o in options if len(logo_rows(o)[0]) <= width]
        if fits:
            return self.rng.choice(fits)
        return short if len(logo_rows(short)[0]) <= width else min(options, key=len)

    def grid(self, text, width, label=False):
        """Cells of the text in block letters (rows 0-3), or spaced plain text when the block
        letters are too wide. With label, a small "cctop" sits on row -1 over the right end."""
        key = (text, width, label)
        if key not in self.grids:
            rows = logo_rows(text)
            if len(rows[0]) > width:
                rows = ["", " ".join(text), "", ""]
            gw = max(len(row) for row in rows)
            big = C["fg"] | curses.A_BOLD
            cells = [(r, c, ch, self.rng.random(), big) for r, row in enumerate(rows)
                     for c, ch in enumerate(row) if ch != " "]
            if label:
                cells += [(-1, gw - 5 + i, ch, self.rng.random(), C["muted"]) for i, ch in enumerate("cctop")]
            self.grids[key] = (cells, gw)
        return self.grids[key]

    def frame(self, width):
        """(cells to draw as (row, col, char, attr), width of the text). Rows run from -1 to 3."""
        if self.t0 is None:
            self.t0 = time.monotonic()
            self.text = self.pick(width)
        t = time.monotonic() - self.t0
        fx, p = None, 1.0
        if t < INTRO_HOLD:
            text, fx, p = self.text, self.fx, t / FX_IN
        elif self.motion and t < INTRO_HOLD + FX_OUT:
            text, fx, p = self.text, fx_decrypt, 1 - (t - INTRO_HOLD) / FX_OUT  # decrypt run backwards
        elif self.motion and t < INTRO_HOLD + FX_OUT + FX_IN:
            text, fx, p = "cctop.", fx_rise, (t - INTRO_HOLD - FX_OUT) / FX_IN
        else:
            text = "cctop."
            idle = t - (INTRO_HOLD + FX_OUT + FX_IN)
            rest = SHIMMER_EVERY - SHIMMER_S  # the logo rests first, then a wave passes
            if self.motion and idle > 0 and idle % SHIMMER_EVERY >= rest:
                fx, p = fx_shimmer, (idle % SHIMMER_EVERY - rest) / SHIMMER_S
        cells, gw = self.grid(text, width, label=text != "cctop.")
        if not self.motion or p >= 1:
            return [(r, c, ch, a) for r, c, ch, _, a in cells], gw
        return fx(cells, max(0.0, p), gw, int(t * 24)), gw


def init_colors():
    curses.start_color()
    curses.use_default_colors()
    pal = dict(PALETTE, **heat_colors())
    names = list(pal)
    for i, name in enumerate(names, start=1):
        h = pal[name]
        r, g, b = (int(h[j:j + 2], 16) for j in (1, 3, 5))
        if curses.COLORS >= 256 and curses.can_change_color():
            idx = 232 - len(names) + i  # borrow a few cube slots; ncurses restores them on exit
            curses.init_color(idx, r * 1000 // 255, g * 1000 // 255, b * 1000 // 255)
            COLNUM[name] = idx
        elif curses.COLORS >= 256:
            COLNUM[name] = nearest_256(r, g, b)
        else:
            COLNUM[name] = BASIC.get(name, BASIC["teal"])
    back = COLNUM["bg"] if THEME["paint"] else -1
    for i, name in enumerate(names, start=1):
        curses.init_pair(i, COLNUM[name], back)
        C[name] = curses.color_pair(i)
    if curses.COLORS < 256:
        for name in ("dim", "faint", "muted", "line", "heat1", "heat2"):
            C[name] |= curses.A_DIM
    hl = len(names) + 1
    curses.init_pair(hl, COLNUM["bg"], COLNUM["sand"])
    C["hl"] = curses.color_pair(hl) | curses.A_BOLD


def heat_colors():
    """Four heatmap greens: the theme's green mixed into the background, GitHub style."""
    hexes = lambda h: [int(h[j:j + 2], 16) for j in (1, 3, 5)]
    bg, fg = hexes(PALETTE["bg"]), hexes(PALETTE["teal"])
    return {f"heat{k}": "#" + "".join(f"{round(b + (f - b) * mix):02x}" for b, f in zip(bg, fg))
            for k, mix in enumerate((0.3, 0.5, 0.75, 1.0), start=1)}


def nearest_256(r, g, b):
    steps = [0, 95, 135, 175, 215, 255]
    q = lambda v: min(range(6), key=lambda i: abs(steps[i] - v))
    ci = 16 + 36 * q(r) + 6 * q(g) + q(b)
    cube = (steps[q(r)], steps[q(g)], steps[q(b)])
    gi = min(23, max(0, round(((r + g + b) / 3 - 8) / 10)))
    gv = 8 + gi * 10
    d = lambda c: sum((x - y) ** 2 for x, y in zip(c, (r, g, b)))
    return ci if d(cube) <= d((gv, gv, gv)) else 232 + gi


def level(pct, warn=70, bad=90):
    return C["clay"] if pct >= bad else C["sand"] if pct >= warn else C["teal"]


class Screen:
    def __init__(self, win, grow=1.0):
        self.win = win
        self.grow = grow  # 0-1: bars draw this share of their value while a new view settles in
        self.h, self.w = win.getmaxyx()

    def put(self, y, x, s, attr=0, maxw=None):
        if y < 0 or y >= self.h or x >= self.w:
            return 0
        room = self.w - x if maxw is None else min(maxw, self.w - x)
        s = str(s)
        if len(s) > room:
            s = s[:max(room - 1, 0)] + "…" if room > 0 else ""
        try:
            self.win.addstr(y, x, s, attr)
        except curses.error:
            pass  # writing the bottom-right cell raises after a successful write
        return len(s)

    def center(self, y, x, w, s, attr=0):
        return self.put(y, x + max(0, (w - len(s)) // 2), s, attr, w)

    def box(self, y, x, h, w, title=None):
        b = C["line"]
        self.put(y, x, "┌" + "─" * (w - 2) + "┐", b)
        for r in range(1, h - 1):
            self.put(y + r, x, "│", b)
            self.put(y + r, x + w - 1, "│", b)
        self.put(y + h - 1, x, "└" + "─" * (w - 2) + "┘", b)
        if title:
            tx = x + (w - len(title) - 2) // 2
            self.put(y, tx, "│", b)
            self.put(y, tx + 1, title, C["fg"] | curses.A_BOLD)
            self.put(y, tx + 1 + len(title), "│", b)

    def hints(self, y, x, w, items):
        total = sum(len(k) + len(v) + 3 + (not set(k) & set("←→↑↓")) for k, v in items) + len(items) - 1
        cx = x + max(1, (w - total) // 2)
        for k, v in items:
            cx += self.put(y, cx, "[", C["muted"])
            cx += self.put(y, cx, k, C["sand"])
            cx += self.put(y, cx, " " if set(k) & set("←→↑↓") else "→ ", C["dim"])
            cx += self.put(y, cx, v, C["fg"])
            cx += self.put(y, cx, "]", C["muted"]) + 1

    def kv(self, y, x, label, value="", attr=None, lw=0, maxw=None):
        n = self.put(y, x, (f"{label}:" if label else "").ljust(lw), C["teal"]) + 1
        if value != "":
            room = None if maxw is None else maxw - n
            self.put(y, x + n, value, C["fg"] if attr is None else attr, room)
        return x + n

    def squares(self, y, x, parts):
        """Small squares with half-column gaps. parts: [(count, attr)]."""
        i = 0
        for count, attr in parts:
            for _ in range(count):
                self.put(y, x + 2 * i, SQ, attr)
                i += 1
        return sq_width(i)

    def meter(self, y, x, cols, pct, attr):
        """The one bar style used everywhere: lit squares, then dim ones, within `cols` columns."""
        n = sq_count(cols)
        pct = min(max(pct, 0), 100) * self.grow
        on = round(n * pct / 100)
        if pct > 0 and on == 0:
            on = 1
        self.squares(y, x, [(on, attr), (n - on, C["faint"])])

    def hbar(self, y, x, cells, frac, attr):
        self.meter(y, x, cells, 100 * frac, attr)


SPARK = " ▁▂▃▄▅▆▇█"
# one square = the Nerd Font icon md-square_rounded (U+F14FB), then a space: rounded corners,
# centred on the text, and a little wider than a column so the gap stays small. Squares sit
# two columns apart. CCTOP_SQUARE overrides it (two columns, e.g. "■ " without a Nerd Font).
SQ = os.environ.get("CCTOP_SQUARE", "\U000F14FB ")[:2].ljust(2)


def sq_count(cols):
    return max(1, cols // 2)


def sq_width(n):
    return 2 * n


def stat_text(scr, y, x, add, rem):
    """'+12 −3' in green and red; returns the width written."""
    n = scr.put(y, x, f"+{add}", C["teal"])
    n += scr.put(y, x + n, " ")
    n += scr.put(y, x + n, f"−{rem}", C["clay"])
    return n


def stat_bar(scr, y, x, cells, add, rem, scale=1.0):
    """GitHub-style diff bar of `cells` squares: green for additions, red for deletions."""
    total = add + rem
    filled = round(max(1, round(cells * min(scale, 1))) * scr.grow) if total else 0
    green = round(filled * add / total) if total else 0
    if add and not green and filled:
        green = 1
    red = filled - green
    if rem and not red and filled > 1:
        green, red = green - 1, 1
    scr.squares(y, x, [(green, C["teal"]), (red, C["clay"]), (cells - green - red, C["faint"])])


def spark(values, width, lo=None, hi=None):
    vals = list(values)[-width:]
    if not vals:
        return ""
    lo = min(vals) if lo is None else lo
    hi = max(vals) if hi is None else hi
    span = (hi - lo) or 1
    return "".join(SPARK[max(1, min(8, round(1 + (v - lo) / span * 7)))] for v in vals)


class App:
    def __init__(self, win):
        self.win = win
        self.usage = Usage()
        self.sys = System()
        self.tab = 0
        self.metric = 0
        self.day = 0  # 0 = today, 1 = yesterday ...
        self.sel = 0
        self.sel_sid = None
        self.roots = {}
        self.event_roots = {}  # sid -> (last_ts, checked at, project path) for the Usage tab
        self.projects = []
        self.projects_at = 0
        self.proj_name = None
        self.proj_idx = 0
        self.last_usage = 0
        self.last_sys = 0
        self.live = []
        self.limits = Limits()
        self.intro = Intro(self.sys.user)
        self.started = time.monotonic()
        self.view = None     # what is on screen; when it changes, bars grow in again
        self.anim_at = 0.0

    def tick(self, force=False):
        now = time.time()
        if force or now - self.last_sys >= 2:
            self.sys.refresh()
            self.last_sys = now
        if force or now - self.last_usage >= 3:
            self.usage.refresh()
            self.live = live_sessions(self.usage)
            self.last_usage = now

    # ---- aggregation
    def aggregate(self):
        m = METRICS[self.metric][0]
        now = datetime.now()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = midnight - timedelta(days=6)
        sel = midnight - timedelta(days=self.day)
        hours = [0] * 24
        days = [0] * 7
        models = defaultdict(int)
        projects = defaultdict(int)
        kinds = [0, 0, 0, 0]
        today = today_msgs = 0
        for e in self.usage.events:
            ts = e[0]
            v = metric_value(e, m)
            if ts < week_start:
                continue
            days[(ts - week_start).days] += v
            models[short_model(e[1])] += v
            projects[self.session_project(e[3])] += v
            for i in range(4):
                kinds[i] += e[4 + i]
            if ts >= midnight:
                today += v
                today_msgs += 1
            if sel <= ts < sel + timedelta(days=1):
                hours[ts.hour] += v

        return dict(hours=hours, days=days, week_start=week_start, models=models,
                    projects=projects, kinds=kinds, today=today, today_msgs=today_msgs,
                    now=now, sel=sel)

    # ---- frame
    def draw(self):
        g = min(1.0, (time.monotonic() - self.anim_at) / GROW_S) if MOTION else 1.0
        scr = Screen(self.win, 1 - (1 - g) ** 3)  # ease out
        self.win.erase()
        W, H = scr.w, scr.h
        if W < 80 or H < 24:
            scr.put(0, 0, f"cctop needs at least 80x24 (now {W}x{H})", C["sand"])
            return
        self.a = self.aggregate()

        scr.box(0, 0, 3, W, f"cctop-{VERSION}")
        x = 2
        for i, name in enumerate(TABS):
            if i:
                x += scr.put(1, x, " │ ", C["muted"])
            x += scr.put(1, x, name, C["fg"] | curses.A_BOLD if i == self.tab else C["teal"])
        clock = self.a["now"].strftime("%H:%M  %a %d %b")
        scr.put(1, W - len(clock) - 2, clock, C["fg"] | ITALIC)

        scr.box(3, 0, H - 3, W)
        name = TABS[self.tab].lower()
        getattr(self, f"tab_{name}")(scr, 4, 2, H - 5, W - 4)

        keys = []
        if name == "usage":
            keys = [("t", METRICS[self.metric][1].capitalize()), ("←→", "Day")]
        elif name == "live" and len(self.live) > 1:
            keys = [("←→", "Session")]
        elif name == "projects" and self.projects:
            keys = [("↑↓", "Project"), ("o", "Open folder")]
        scr.hints(H - 1, 0, W, keys + [("Tab", "Next"), ("r", "Reload"), ("q", "Quit")])

    # ---- tabs
    def tab_overview(self, scr, y, x, h, w):
        """Logo, then three quiet sections: limits, live, system."""
        cw = min(w - 2, 84)
        cx = x + (w - cw) // 2
        live = self.live[:max(1, min(len(self.live), 5))]
        notes = self.context_notes()
        body = 1 + 4 + 1 + 1 + max(1, len(live)) + 1 + 1 + 1 + (1 + len(notes) if notes else 0)
        # title: a label row, 4 rows of block letters and a gap of 2 (1 when the screen is tight)
        show_logo = h >= body + 1 + LOGO_ROWS + 1
        title_h = (1 + LOGO_ROWS + (2 if h >= body + 1 + LOGO_ROWS + 2 else 1)) if show_logo else 0
        r = y + max(0, (h - body - title_h) // 2)
        if show_logo:
            cells, gw = self.intro.frame(w - 2)
            lx = x + max(0, (w - gw) // 2)
            for rr, cc, ch, attr in cells:
                if x <= lx + cc < x + w:  # sliding cells stay inside the frame
                    scr.put(r + 1 + rr, lx + cc, ch, attr)
            r += title_h

        r = self.ov_header(scr, r, cx, cw, "limits")
        r = self.ov_limits(scr, r, cx, cw)
        r = self.ov_header(scr, r + 1, cx, cw, "live", f"{len(self.live)} session{'s' * (len(self.live) != 1)}")
        r = self.ov_live(scr, r, cx, cw, live)
        r = self.ov_header(scr, r + 1, cx, cw, "system", self.sys.host)
        r = self.ov_system(scr, r, cx, cw)
        for text, attr in notes:
            r += 1
            if r >= y + h:
                break
            scr.center(r, cx, cw, text, attr)
        if r < y + h - 2:
            scr.center(y + h - 1, x, w, "side quest of smh", C["dim"])

    def context_notes(self):
        """One reminder line per live session whose context is getting long."""
        out = []
        names = [self.project_name(row) for row in self.live]
        for row, name in zip(self.live, names):
            frac, window, sev = context_state(row["session"])
            if not sev:
                continue
            label = f"{name} ({row['name']})" if names.count(name) > 1 else name
            used = f"{fmt_n(row['session'].ctx)} of {fmt_window(window)}, {100 * frac:.0f}%"
            if sev == 2:
                out.append((f"{label} context {used} · /clear or /compact now", C["clay"] | curses.A_BOLD))
            else:
                out.append((f"{label} context {used} · /clear before the next task", C["sand"]))
        return out[:3]

    def ov_header(self, scr, y, x, w, label, note=""):
        n = scr.put(y, x, label, C["dim"])
        right = f" {note}" if note else ""
        scr.put(y, x + n + 1, "─" * max(0, w - n - 1 - len(right) - (1 if note else 0)), C["faint"])
        if right:
            scr.put(y, x + w - len(right), right, C["dim"])
        return y + 1

    def ov_limits(self, scr, y, x, w):
        lim = self.limits
        if lim.data is None:
            scr.put(y, x + 1, lim.error or "loading limits…", C["dim"], w - 1)
            return y + 4
        now = datetime.now().astimezone()
        lw = 7
        r = y
        for label, key, span in (("5h", "five_hour", 5 * 3600), ("week", "seven_day", 7 * 86400)):
            win = lim.data.get(key) or {}
            used = win.get("utilization")
            try:
                reset = datetime.fromisoformat(win["resets_at"])
                left_s = max(0.0, (reset - now).total_seconds())
            except (KeyError, TypeError, ValueError):
                reset, left_s = None, None
            if used is None:
                scr.put(r, x + 1, label, C["muted"])
                scr.put(r, x + 1 + lw, "-", C["dim"])
                r += 2
                continue
            used = min(max(used, 0.0), 100.0)
            attr = C["teal"] if used < 50 else C["sand"] if used < 80 else C["clay"]

            # row 1: bar of what is used, percent, reset time
            when = ""
            if reset:
                when = f"resets in {fmt_dur(left_s)}" if left_s < 86400 else "resets " + reset.astimezone().strftime("%a %H:%M").lower()
            tail = f"{used:3.0f}%"
            # same bar length for both windows, whatever the reset text says
            bar_cols = max(8, w - 1 - lw - 4 - 17 - 5)
            n_sq = sq_count(bar_cols)
            bw = sq_width(n_sq)
            scr.put(r, x + 1, label, C["fg"] | curses.A_BOLD)
            bx = x + 1 + lw
            scr.meter(r, bx, bw, used, attr)
            scr.put(r, bx + bw + 1, tail, attr | curses.A_BOLD)
            scr.put(r, x + w - len(when), when, C["dim"])

            # row 2: time track under the bar, dot at now, and where this pace ends up
            if left_s is not None:
                elapsed = min(1.0, max(0.0, 1 - left_s / span))
                dot = min(bw - 1, int(round(elapsed * (bw - 1))))
                scr.put(r + 1, bx, "━" * dot, C["muted"])
                scr.put(r + 1, bx + dot, "●", C["fg"])
                scr.put(r + 1, bx + dot + 1, "─" * (bw - dot - 1), C["faint"])
                pace, pattr = self.pace(used, elapsed, span * elapsed)
                # right-aligned under the reset time
                scr.put(r + 1, max(bx + bw + 1, x + w - len(pace)), pace, pattr, x + w - (bx + bw + 1))
            r += 2
        return r

    @staticmethod
    def pace(used, elapsed, elapsed_s):
        """Where the window ends up if usage keeps its current rate."""
        if elapsed < 0.03 or used <= 0:
            return "just started", C["dim"]
        projected = used / elapsed
        if projected < 100:
            return f"on pace for {projected:.0f}%", C["teal"] if projected < 80 else C["sand"]
        rate = used / max(elapsed_s, 1)  # percent per second
        return f"limit in ~{fmt_dur((100 - used) / rate)}", C["clay"]

    def ov_live(self, scr, y, x, w, rows):
        if not rows:
            scr.put(y, x + 1, "nothing running · start claude in a project folder", C["dim"])
            return y + 1
        spin = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(time.time() * 8) % 10]
        names = [self.project_name(row) for row in rows]
        nw = min(18, max(len(n) for n in names) + 1)
        dupes = len(set(names)) < len(names)  # two sessions on one project: show which is which
        sw = min(16, max(len(r["name"]) for r in rows) + 1) if dupes else 0
        for i, (row, name) in enumerate(zip(rows, names)):
            r = y + i
            s = row["session"]
            busy = row["status"] == "busy"
            scr.put(r, x + 1, spin if busy else "○", C["teal"] if busy else C["dim"])
            scr.put(r, x + 3, name, C["fg"] | curses.A_BOLD if busy else C["muted"], nw)
            if busy:
                pend = list(s.inflight.values())
                doing = pend[-1]["summary"] if pend else "thinking"
            else:
                doing = "waiting for you"
            since = fmt_dur(time.time() - row["status_since"]) if row["status_since"] else ""
            right = (f"{row['agents']} agents · " if row["agents"] else "") + since
            if dupes:
                scr.put(r, x + 3 + nw, row["name"], C["dim"], sw)
            dx = x + 3 + nw + sw + 1
            scr.put(r, dx, doing, C["sand"] if busy else C["dim"], x + w - dx - len(right) - 2)
            scr.put(r, x + w - len(right), right, C["rose"] if row["agents"] else C["dim"])
        return y + len(rows)

    def project_name(self, row):
        sid = row["session"].sid
        hit = self.roots.get(sid)
        if not hit or time.time() - hit[0] > 15:
            hit = (time.time(), session_root(row, row["session"]))
            self.roots[sid] = hit
        name = os.path.basename(hit[1])
        return name if hit[1] != os.path.realpath(HOME) else "~"

    def session_project(self, sid):
        """The project a session belongs to, as a short path; same rule as the Overview and Live tabs.
        Worked out again only when the session has moved on, so old sessions cost one git call."""
        s = self.usage.sessions.get(sid)
        if not s:
            return "?"
        hit = self.event_roots.get(sid)
        if not hit or (hit[0] != s.last_ts and time.time() - hit[1] > 60):
            hit = (s.last_ts, time.time(), short_path(session_root({"cwd": s.cwd}, s)))
            self.event_roots[sid] = hit
        return hit[2]

    def ov_system(self, scr, y, x, w):
        sn = self.sys.snap
        t = sn["temps"]
        mu, mt = sn["mem"]
        du, dt = sn["disk"]
        bat = sn["bat"]
        hot = "cpu" in t and t["cpu"] >= 85
        cpu_text = f"{sn['cpu']:.0f}%" + (f" {t['cpu']:.0f}°" if "cpu" in t else "")
        items = [("cpu", sn["cpu"], cpu_text, C["clay"] if hot else level(sn["cpu"]))]
        items.append(("mem", 100 * mu / mt, f"{100 * mu / mt:.0f}%", level(100 * mu / mt)))
        items.append(("disk", 100 * du / dt, f"{(dt - du) / 2**30:.0f}G free", level(100 * du / dt)))
        if bat:
            battr = C["clay"] if bat["pct"] <= 15 else C["sand"] if bat["pct"] <= 30 else C["teal"]
            items.append(("bat", bat["pct"], f"{bat['pct']}%", battr))
        items.append(("up", None, fmt_dur(sn["uptime"]), C["fg"]))
        # meters get as many squares as fit (up to four), or none on narrow screens
        for sq, gap in ((4, 3), (3, 3), (3, 2), (2, 2), (0, 3)):
            need = sum(len(k) + 1 + (sq_width(sq) + 1 if p is not None and sq else 0) + len(v)
                       for k, p, v, _ in items) + gap * (len(items) - 1)
            if need <= w - 1:
                break
        cx = x + 1
        for k, pct, v, attr in items:
            if cx >= x + w:
                break
            cx += scr.put(y, cx, k, C["dim"]) + 1
            if pct is not None and sq:
                scr.meter(y, cx, sq_width(sq), pct, attr)
                cx += sq_width(sq) + 1
            cx += scr.put(y, cx, v, attr if pct is not None else C["fg"], x + w - cx) + gap
        return y + 1

    def box_limits(self, scr, y, x, h, w, cur, project=None):
        lim = self.limits
        age = time.time() - lim.fetched if lim.fetched else 0
        scr.box(y, x, h, w, f"Usage · {fmt_dur(age)} old" if lim.data and age > 360 else "Usage")
        ix, lw = x + 2, 9
        r = y + 1
        if lim.data is None:
            msg = lim.error or "loading limits…"
            scr.put(r, ix, msg, C["dim"], w - 4)
            r += 4
        else:
            now = datetime.now().astimezone()
            for label, key in (("5h", "five_hour"), ("Week", "seven_day")):
                if r >= y + h - 1:
                    break
                win = lim.data.get(key) or {}
                used = win.get("utilization")
                if used is None:
                    scr.kv(r, ix, label, "-", C["dim"], lw)
                    r += 2
                    continue
                used = min(max(used, 0.0), 100.0)
                left = 100 - used
                # the bar fills up as the limit is used: teal, then sand past half, clay past 80%
                attr = C["teal"] if used < 50 else C["sand"] if used < 80 else C["clay"]
                cx = scr.kv(r, ix, label, lw=lw)
                pct = f"{used:3.0f}% used"
                cells = max(4, w - 4 - (cx - ix) - len(pct) - 2)
                scr.meter(r, cx, cells, used, attr)
                scr.put(r, cx + cells + 2, pct, attr | curses.A_BOLD)
                note = f"{left:.0f}% left"
                try:
                    reset = datetime.fromisoformat(win["resets_at"])
                    secs = (reset - now).total_seconds()
                    when = f"in {fmt_dur(secs)}" if secs < 86400 else reset.astimezone().strftime("%a %H:%M")
                    note += f" · {'resets ' if len(note) + len(when) + 10 < w - 2 - (cx - x) else ''}{when}"
                except (KeyError, TypeError, ValueError):
                    pass
                if r + 1 < y + h - 1:
                    scr.put(r + 1, cx, note, C["dim"], w - 2 - (cx - x))
                r += 2
        s = cur["session"] if cur else None
        rows = [("Model", short_model(s.model) if s and s.model else "-"),
                ("Project", short_path(project or (cur["cwd"] if cur else "")) or "-"),
                ("Context", fmt_n(s.ctx) if s and s.ctx else "-"),
                ("Today", f"{fmt_n(self.a['today'])} tokens · {self.a['today_msgs']} msgs")]
        for k, v in rows:
            if r >= y + h - 1:
                break
            scr.kv(r, ix, k, v, lw=lw, maxw=w - 4)
            r += 1

    def tab_usage(self, scr, y, x, h, w):
        a = self.a
        bottom_h = 9
        chart_h = h - bottom_h
        label = "Today" if self.day == 0 else a["sel"].strftime("%a %d %b")
        scr.box(y, x, chart_h, w, f"{label} by hour")
        vals = a["hours"]
        total, peak = sum(vals), max(vals)
        info = f"{fmt_n(total)} total"
        if peak:
            info += f" · peak {vals.index(peak):02d}:00"
        scr.put(y + 1, x + 2, info, C["dim"])
        k = a["kinds"]
        kinds = f"in {fmt_n(k[0])} · out {fmt_n(k[1])} · cache w {fmt_n(k[2])} · cache r {fmt_n(k[3])} (7d)"
        if len(info) + len(kinds) + 6 < w:
            scr.put(y + 1, x + w - 2 - len(kinds), kinds, C["dim"])

        ch = chart_h - 4
        slot = max(2, min(4, (w - 4) // 24))
        bw = slot - 1
        ox = x + max(2, (w - slot * 24) // 2)
        cur = a["now"].hour if self.day == 0 else -1
        base = y + chart_h - 3
        for hr, v in enumerate(vals):
            eighths = round(v / peak * ch * 8 * scr.grow) if peak else 0
            if v and not eighths:
                eighths = 1
            attr = C["sand"] if hr == cur else C["teal"]
            for row in range(ch):
                fill = min(8, max(0, eighths - row * 8))
                if fill:
                    scr.put(base - row, ox + hr * slot, SPARK[fill] * bw, attr)
                elif row == 0:
                    scr.put(base, ox + hr * slot, "·", C["faint"])
            if hr % 3 == 0:
                scr.put(base + 1, ox + hr * slot, f"{hr:02d}", C["dim"])

        by = y + chart_h
        cw = w // 3
        self.box_days(scr, by, x, bottom_h, cw)
        self.box_ranked(scr, by, x + cw, bottom_h, cw, "Models", a["models"])
        self.box_ranked(scr, by, x + 2 * cw, bottom_h, w - 2 * cw, "Projects", a["projects"], paths=True)

    def box_days(self, scr, y, x, h, w):
        a = self.a
        scr.box(y, x, h, w, "Last 7 days")
        days = a["days"]
        peak = max(days) or 1
        bar_w = w - 18
        for i in range(7):
            d = a["week_start"] + timedelta(days=i)
            r = y + 1 + i
            hl = 6 - i == self.day
            scr.put(r, x + 2, d.strftime("%a %d"), C["fg"] | curses.A_BOLD if hl else C["teal"])
            scr.hbar(r, x + 9, bar_w, days[i] / peak, C["sand"] if hl else C["muted"])
            val = fmt_n(days[i]) if days[i] else "-"
            scr.put(r, x + w - 2 - len(val), val, C["fg"] if hl else C["dim"])

    def box_ranked(self, scr, y, x, h, w, title, data, paths=False):
        scr.box(y, x, h, w, title)
        items = sorted(data.items(), key=lambda kv: -kv[1])
        total = sum(data.values()) or 1
        if not items:
            scr.put(y + 1, x + 2, "no usage yet", C["dim"])
            return
        cells = max(5, min(17, (w - 4) // 3))
        for i, (k, v) in enumerate(items[:h - 2]):
            r = y + 1 + i
            tail = f"{100 * v / total:3.0f}%"
            nw = w - 6 - len(tail) - cells
            name = "…" + k[-(nw - 1):] if paths and len(k) > nw else k  # paths keep their end
            scr.put(r, x + 2, name, C["fg"] if i == 0 else C["teal"], nw)
            scr.meter(r, x + w - 3 - len(tail) - cells, cells, 100 * v / total, C["sand"] if i == 0 else C["muted"])
            scr.put(r, x + w - 2 - len(tail), tail, C["dim"])

    def tab_live(self, scr, y, x, h, w):
        if not self.live:
            scr.center(y + h // 2, x, w, "no claude code sessions running", C["dim"])
            return
        sids = [r["session"].sid for r in self.live]
        self.sel = sids.index(self.sel_sid) if self.sel_sid in sids else 0
        self.sel_sid = sids[self.sel]
        cur = self.live[self.sel]
        s = cur["session"]
        root = session_root(cur, s)

        # left: file tree of the project, full height
        tree_w = min(44, max(26, w * 3 // 10)) if w >= 90 else 0
        if tree_w:
            self.lv_tree(scr, y, x, h, tree_w, cur, s, root)
        rx, rw = x + tree_w + (1 if tree_w else 0), w - tree_w - (1 if tree_w else 0)

        # session picker
        cx = rx
        for i, row in enumerate(self.live):
            busy = row["status"] == "busy"
            if cx + len(row["name"]) + 6 > rx + rw:
                scr.put(y, cx, f"+{len(self.live) - i}", C["dim"])
                break
            cx += scr.put(y, cx, "●" if busy else "○", C["teal"] if busy else C["dim"])
            if i == self.sel:
                cx += scr.put(y, cx, "[", C["muted"])
                cx += scr.put(y, cx, row["name"], C["fg"] | curses.A_BOLD)
                cx += scr.put(y, cx, "]", C["muted"]) + 2
            else:
                cx += scr.put(y, cx, f" {row['name']} ", C["teal"]) + 1
        if len(self.live) > 1:
            info = f"{self.sel + 1} of {len(self.live)}"
            scr.put(y, rx + rw - len(info), info, C["dim"])

        # right: session | activity, now (full width), git | agents
        avail = h - 1
        top_h = max(6, min(16, avail * 45 // 100))
        bot_h = max(6, min(12, avail * 30 // 100))
        now_h = avail - top_h - bot_h
        if now_h < 5:
            bot_h = max(4, bot_h - (5 - now_h))
            now_h = avail - top_h - bot_h
        half = rw // 2
        ty = y + 1
        scr.box(ty, rx, top_h, half, "Session")
        self.lv_session(scr, ty + 1, rx + 2, top_h - 2, half - 4, cur, s, root)
        scr.box(ty, rx + half, top_h, rw - half, "Activity")
        self.lv_activity(scr, ty + 1, rx + half + 2, top_h - 2, rw - half - 4, cur, s)

        ny = ty + top_h
        scr.box(ny, rx, now_h, rw, "Now")
        self.lv_now(scr, ny + 1, rx + 2, now_h - 2, rw - 4, cur, s)

        by = ny + now_h
        g = git_details(root)
        scr.box(by, rx, bot_h, half, "Git · GitHub")
        self.lv_git(scr, by + 1, rx + 2, bot_h - 2, half - 4, g)
        self.box_limits(scr, by, rx + half, bot_h, rw - half, cur, root)

    def lv_tree(self, scr, y, x, h, w, cur, s, root):
        scr.box(y, x, h, w)
        ix, iw = x + 2, w - 4
        # header: project / folder of the file being worked on
        active = next((a for a in reversed(s.activity) if a.get("path") and inside(a["path"], [root])), None)
        active_path = active["path"] if active else None
        sub = ""
        if active_path and inside(active_path, [root]):
            sub = os.path.relpath(os.path.dirname(os.path.realpath(active_path)), root)
            sub = "" if sub == "." else sub
        name = os.path.basename(root) or root
        n = scr.put(y + 1, ix, name, C["dim"])
        if sub:
            n += scr.put(y + 1, ix + n, " / ", C["dim"])
            scr.put(y + 1, ix + n, sub, C["fg"] | curses.A_BOLD, iw - n)
        else:
            scr.put(y + 1, ix + n, " / ", C["dim"])
        scr.put(y + 2, x, "├" + "─" * (w - 2) + "┤", C["line"])

        rows = file_tree(root, {os.path.realpath(p): f for p, f in s.files.items()},
                         os.path.realpath(active_path) if active_path else None)
        room = h - 4
        if not rows:
            scr.put(y + 3, ix, "empty folder", C["dim"])
            return
        hot = next((i for i, r in enumerate(rows) if r["active"]), 0)
        start = max(0, min(hot - room // 2, len(rows) - room))
        for i, row in enumerate(rows[start:start + room]):
            r = y + 3 + i
            prefix = row["prefix"]
            scr.put(r, ix, prefix, C["faint"])
            px = ix + len(prefix)
            label = row["name"] + ("/" if row["dir"] and row["collapsed"] else "")
            if row["active"]:
                attr = C["hl"]
                label = f" {label} "
            elif row["op"] in ("edit", "write"):
                attr = C["sand"]
            elif row["op"] == "read":
                attr = C["teal"]
            elif row["dir"]:
                attr = C["fg"] | curses.A_BOLD
            else:
                attr = C["muted"]
            n = scr.put(r, px, label, attr, iw - len(prefix))
            mark = {"edit": "✎", "write": "+"}.get(row["op"], "")
            if mark and not row["active"] and px + n + 2 <= ix + iw:
                scr.put(r, px + n + 1, mark, C["sand"])
        if start + room < len(rows):
            more = f"+{len(rows) - start - room} more"
            scr.put(y + h - 1, x + w - len(more) - 2, more, C["dim"])

    def lv_git(self, scr, y, x, h, w, g):
        user = github_user()
        if not g:
            scr.put(y, x, "not a git repo", C["dim"])
            if user and h > 1:
                scr.kv(y + 1, x, "GitHub", f"connected as {user}", C["teal"], 9, w)
            return
        bottom = y + h
        r = y

        # branch
        scr.kv(r, x, "Branch", g["branch"] + (f" → {g['upstream']}" if g["upstream"] else ""),
               C["fg"] | curses.A_BOLD, 9, w)
        r += 1

        # sync: one cell per commit to push (rose) and to pull (sand)
        if r < bottom:
            cx = scr.kv(r, x, "Sync", lw=9)
            if not g["upstream"]:
                scr.put(r, cx, "no upstream, not pushed yet", C["sand"], w - (cx - x))
            elif not g["ahead"] and not g["behind"]:
                scr.put(r, cx, "✓ up to date", C["teal"])
            else:
                end = x + w
                for arrow, n, attr, word in (("↑", g["ahead"], C["sand"], "push"), ("↓", g["behind"], C["lav"], "pull")):
                    if not n or cx >= end:
                        continue
                    cx += scr.put(r, cx, f"{arrow}{n} ", attr | curses.A_BOLD, end - cx)
                    fit = min(n, 5, max(0, (end - cx) // 2))
                    cx += scr.squares(r, cx, [(fit, attr)]) + 1 if fit else 0
                    if cx < end:
                        cx += scr.put(r, cx, f"to {word}", C["dim"], end - cx) + 2
            r += 1

        # working tree diff stat, GitHub style
        if r < bottom:
            cx = scr.kv(r, x, "Changes", lw=9)
            if g["add"] or g["del"] or g["untracked"]:
                cx += stat_text(scr, r, cx, g["add"], g["del"])
                if g["add"] or g["del"]:
                    stat_bar(scr, r, cx + 1, 5, g["add"], g["del"])
                    cx += sq_width(5) + 2
                if g["untracked"]:
                    scr.put(r, cx, f"{g['untracked']} new", C["lav"], x + w - cx)
            else:
                scr.put(r, cx, "✓ clean", C["teal"])
            r += 1

        # changed files with their own bars, scaled to the biggest change
        files = g["files"]
        left = bottom - r
        room = max(0, left - min(3, left))  # keep the commit line and a couple of others
        if files and room:
            peak = max(a + d for _, a, d in files) or 1
            for path, a, d in files[:room]:
                if r >= bottom:
                    break
                stat = f"+{a} −{d}"
                cells = sq_width(3)
                nw = w - 2 - len(stat) - cells - 2
                name = path if len(path) <= nw else "…" + path[-(nw - 1):]
                scr.put(r, x + 1, "▸", C["faint"])
                scr.put(r, x + 3, name, C["fg"], nw)
                sx = x + w - cells - 1 - len(stat)
                stat_text(scr, r, sx, a, d)
                stat_bar(scr, r, x + w - cells, 3, a, d, scale=(a + d) / peak)
                r += 1
            if len(files) > room and r < bottom:
                scr.put(r, x + 3, f"+{len(files) - room} more files", C["dim"])
                r += 1

        # last commit with its stat
        if r < bottom and g["last_commit"]:
            cx = scr.kv(r, x, "Commit", lw=9)
            h_, _, rest = g["last_commit"].partition(" ")
            cx += scr.put(r, cx, h_, C["rose"]) + 1
            stat = len(f"+{g['commit_add']} −{g['commit_del']}") + 1
            scr.put(r, cx, rest, C["fg"], x + w - cx - stat)
            stat_text(scr, r, x + w - stat + 1, g["commit_add"], g["commit_del"])
            r += 1
        tail = [("Pushed", g["pushed"] or "never", None if g["pushed"] else C["dim"]),
                ("Fetched", g["fetched"] or "never", None if g["fetched"] else C["dim"]),
                ("Remote", g["remote"] or "none", None if g["remote"] else C["dim"]),
                ("GitHub", f"connected as {user}" if user else "not connected", C["teal"] if user else C["dim"])]
        if g["stash"]:
            tail.insert(0, ("Stash", f"{g['stash']} saved", C["lav"]))
        for k, v, attr in tail:
            if r >= bottom:
                break
            scr.kv(r, x, k, v, attr, 9, w)
            r += 1

    def lv_session(self, scr, y, x, h, w, cur, s, root=None):
        busy = cur["status"] == "busy"
        since = f" for {fmt_dur(time.time() - cur['status_since'])}" if cur["status_since"] else ""
        started = datetime.fromtimestamp(cur["started"]) if cur["started"] else s.first_ts
        g = git_info(cur["cwd"])
        user = github_user()
        if g:
            repo = g["repo"] + (f" · {g['dirty']} changed" if g["dirty"] else " · clean")
        else:
            repo = "not a git repo"
        model = short_model(s.model) if s.model else "-"
        if s.effort:
            model += f" · {s.effort} effort"
        rows = [
            ("Title", s.title or "untitled", C["fg"] | curses.A_BOLD),
            ("Status", cur["status"] + since, C["teal"] if busy else C["dim"]),
            *self.context_rows(s),
            ("Started", f"{started:%H:%M} · {fmt_dur((datetime.now() - started).total_seconds())} ago"
             if started else "-", None),
            ("Model", model, C["rose"]),
            ("Mode", s.perm_mode or "default", C["sand"] if s.perm_mode in ("auto", "bypassPermissions") else None),
            ("Directory", short_path(cur["cwd"]), None),
            ("Project", short_path(root) if root else "-", C["fg"] | curses.A_BOLD),
            ("Branch", (g["branch"] if g and g["branch"] else s.branch) or "-", None),
            ("Repo", repo, None if g else C["dim"]),
            ("GitHub", f"connected as {user}" if user else "not connected", C["teal"] if user else C["dim"]),
            ("Version", " · ".join(b for b in (s.version, cur["entrypoint"] or s.entrypoint, cur["kind"]) if b), C["dim"]),
            ("ID", f"{s.sid[:8]} · pid {cur['pid']}", C["dim"]),
        ]
        if root:  # branch, repo and GitHub live in the Git box next door
            rows = [r for r in rows if r[0] not in ("Branch", "Repo", "GitHub")]
        for i, (k, v, attr) in enumerate(rows[:h]):
            scr.kv(y + i, x, k, v, attr, 10, w)

    def context_rows(self, s):
        frac, window, sev = context_state(s)
        if not s.ctx:
            return [("Context", "-", C["dim"])]
        text = f"{fmt_n(s.ctx)} of {fmt_window(window)} · {100 * frac:.0f}% full"
        rows = [("Context", text, C["clay"] if sev else C["fg"])]
        if sev == 2:
            rows.append(("", "/clear or /compact now", C["clay"] | curses.A_BOLD))
        elif sev == 1:
            rows.append(("", "/clear before your next task", C["clay"]))
        return rows

    def lv_now(self, scr, y, x, h, w, cur, s):
        busy = cur["status"] == "busy"
        r = y
        pend = list(s.inflight.values())
        if busy and pend:
            act = pend[-1]
            el = f"  {fmt_secs((datetime.now() - act['ts']).total_seconds())}" if act["ts"] else ""
            cx = scr.kv(r, x, "Doing", lw=8)
            n = scr.put(r, cx, act["summary"], C["sand"], w - (cx - x) - len(el))
            scr.put(r, cx + n, el, C["dim"])
        elif busy:
            scr.kv(r, x, "Doing", f"thinking · last {s.last_tool}" if s.last_tool else "thinking", C["sand"], 8, w)
        else:
            scr.kv(r, x, "Doing", "waiting for you", C["dim"], 8, w)
        r += 1

        def block(label, text, attr, limit):
            nonlocal r
            if r >= y + h or limit <= 0:
                return
            scr.kv(r, x, label, lw=8)
            lines = textwrap.wrap(one_line(text, 4000), max(10, w - 9)) or ["-"]
            if len(lines) > limit:
                lines = lines[:limit]
                lines[-1] = lines[-1][:max(0, w - 10)] + "…"
            for ln in lines:
                if r >= y + h:
                    return
                scr.put(r, x + 9, ln, attr, w - 9)
                r += 1

        room = y + h - r
        earlier = list(s.prompts)[:-1][::-1]
        todo_rows = min(len(s.todos), 8)
        # prompt first, then agents and skills, then recap; earlier prompts and todos take what is left
        block("Prompt", s.prompt, C["fg"] | ITALIC, max(1, min(8, (room - 2) // 2)))
        # agents and skills, one line each
        if r < y + h:
            n = cur["agents"]
            launched = len(s.agents)
            text = (f"{n} running" if n else "none running") + (f" · {launched} this session" if launched else "")
            scr.kv(r, x, "Agents", text, C["rose"] | curses.A_BOLD if n else C["dim"], 8, w)
            r += 1
        if r < y + h:
            extras = []
            if s.mcp:
                extras.append("mcp " + ", ".join(sorted(s.mcp, key=lambda k: -s.mcp[k])))
            if s.web:
                extras.append(f"{s.web} web")
            text = ", ".join(reversed(s.skills)) if s.skills else "none used"
            if extras:
                text += "  ·  " + " · ".join(extras)
            scr.kv(r, x, "Skills", text, C["fg"] if s.skills or extras else C["dim"], 8, w)
            r += 1

        if s.recap:
            block("Recap", s.recap, C["dim"], max(1, min(4, (y + h - r) // 3)))
        if s.todos and r + 1 < y + h:
            scr.kv(r, x, "Todos", lw=8)
            for t in s.todos[:min(todo_rows, y + h - r)]:
                st = t.get("status")
                mark, attr = {"completed": ("✓", C["teal"]), "in_progress": ("›", C["sand"])}.get(st, ("·", C["dim"]))
                scr.put(r, x + 9, mark, attr)
                text = t.get("activeForm") if st == "in_progress" and t.get("activeForm") else t.get("content", "")
                scr.put(r, x + 11, text, C["fg"] if st == "in_progress" else C["muted"], w - 11)
                r += 1
        if earlier and r + 1 < y + h:
            scr.kv(r, x, "Earlier", lw=8)
            for ts, text in earlier:
                if r >= y + h:
                    break
                stamp = ts.strftime("%H:%M") if ts else "--:--"
                scr.put(r, x + 9, stamp, C["dim"])
                scr.put(r, x + 15, one_line(text), C["muted"] | ITALIC, w - 15)
                r += 1

    def lv_activity(self, scr, y, x, h, w, cur, s):
        acts = list(s.activity)[-h:]
        if not acts:
            scr.put(y, x, "no tool calls yet", C["dim"])
            return
        for i, act in enumerate(reversed(acts)):
            mark, attr = {"ok": ("✓", C["teal"]), "err": ("✕", C["clay"]), "run": ("›", C["sand"])}[act["status"]]
            r = y + i
            stamp = act["ts"].strftime("%H:%M:%S") if act["ts"] else "--:--:--"
            scr.put(r, x, stamp, C["dim"])
            scr.put(r, x + 9, mark, attr)
            if act["end"] and act["ts"]:
                dur = fmt_secs((act["end"] - act["ts"]).total_seconds())
            elif act["status"] == "run" and act["ts"]:
                dur = fmt_secs((datetime.now() - act["ts"]).total_seconds())
            else:
                dur = ""
            name, _, rest = act["summary"].partition("  ")
            n = scr.put(r, x + 11, name, C["rose"] if act["status"] != "err" else C["clay"])
            fresh = MOTION and act["seen"] - self.started > 3 and time.monotonic() - act["seen"] < 1.2
            rest_attr = C["teal"] | curses.A_BOLD if fresh else C["fg"] if i == 0 else C["muted"]
            scr.put(r, x + 12 + n, rest, rest_attr, w - 13 - n - len(dur))
            scr.put(r, x + w - len(dur), dur, C["dim"])

    def tab_projects(self, scr, y, x, h, w):
        if time.time() - self.projects_at > 5:
            self.projects = load_projects(self.live)
            self.projects_at = time.time()
        projects = self.projects
        if not projects:
            scr.center(y + h // 2 - 1, x, w, f"no projects in {short_path(PROJECTS_HOME)} yet", C["dim"])
            scr.center(y + h // 2, x, w, "start a claude code session in a folder there", C["dim"])
            return
        names = [p["name"] for p in projects]
        if self.proj_name not in names:
            self.proj_name = names[0]
        self.proj_idx = names.index(self.proj_name)
        p = projects[self.proj_idx]

        # list
        lw = min(30, max(22, w // 4))
        scr.box(y, x, h, lw, "Projects")
        rows = h - 3
        start = max(0, min(self.proj_idx - rows // 2, len(projects) - rows))
        for i, q in enumerate(projects[start:start + rows]):
            r = y + 1 + i
            sel = q is p
            status = (q["meta"].get("status") or "").lower()
            dot, dattr = ("●", C["teal"]) if q["live"] else \
                ("○", {"done": C["dim"], "paused": C["sand"], "idea": C["lav"]}.get(status, C["muted"]))
            scr.put(r, x + 2, dot, dattr)
            ago = "live" if q["live"] else fmt_dur(time.time() - q["last"])
            nw = lw - 7 - len(ago)
            if sel:
                scr.put(r, x + 3, "[", C["muted"])
                scr.put(r, x + 4, q["name"], C["fg"] | curses.A_BOLD, nw - 1)
                scr.put(r, x + 4 + min(len(q["name"]), nw - 1), "]", C["muted"])
            else:
                scr.put(r, x + 4, q["name"], C["teal"], nw)
            scr.put(r, x + lw - 2 - len(ago), ago, C["teal"] if q["live"] else C["dim"])
        scr.put(y + h - 2, x + 2, f"{len(projects)} in {short_path(PROJECTS_HOME)}", C["dim"], lw - 4)

        # detail
        rx, rw = x + lw + 1, w - lw - 1
        meta, sec, sess = p["meta"], p["sections"], p["sessions"]
        g = git_info(p["folder"])
        status = meta.get("status") or ("active" if sess else "no STATUS.md yet")
        last = sess[0] if sess else None
        when = datetime.fromtimestamp(p["last"])
        live = ", ".join(r["name"] for r in p["live"])
        docs = " · ".join(d[0] for d in p["docs"]) or "none yet"
        tokens = sum(e.get("tokens", 0) for e in sess)
        extra = [short_path(r) for r in p["roots"][1:]]
        info = [
            ("Summary", meta.get("summary") or (last["title"] if last and last["title"] else "-"), C["fg"] | curses.A_BOLD),
            ("Status", status + (f" · live in {live}" if live else ""),
             C["teal"] if live or status == "active" else C["sand"] if status == "paused" else C["dim"]),
            ("Last", f"{when:%a %d %b %H:%M} · {fmt_dur(time.time() - p['last'])} ago", None),
            ("Folder", short_path(p["folder"]) + (f" · code in {', '.join(extra)}" if extra else ""), None),
            ("Repo", (g["repo"] + (f" · {g['branch']}" if g["branch"] else "")) if g else "not a git repo",
             None if g else C["dim"]),
            ("Sessions", f"{len(sess)} recorded · {fmt_n(tokens)} tokens" if sess else "none recorded yet",
             None if sess else C["dim"]),
            ("Docs", docs, None if p["docs"] else C["dim"]),
        ]
        steps = re.findall(r"^\s*[-*]\s+\[([ xX])\]", sec.get("next steps", ""), re.M)
        top_h = len(info) + 2 + (1 if steps else 0)
        scr.box(y, rx, top_h, rw, p["name"])
        for i, (k, v, attr) in enumerate(info):
            scr.kv(y + 1 + i, rx + 2, k, v, attr, 10, rw - 4)
        if steps:
            done = sum(1 for m in steps if m.lower() == "x")
            r = y + 1 + len(info)
            cx = scr.kv(r, rx + 2, "Progress", lw=10)
            cells = max(8, min(29, rw - 30))
            scr.meter(r, cx, cells, 100 * done / len(steps), C["teal"])
            scr.put(r, cx + cells + 2, f"{done}/{len(steps)} next steps done", C["dim"], rw - (cx - rx) - cells - 4)

        rest = h - top_h
        # bottom row wants 13 lines (3 stat rows, a gap, a 7-day heatmap) when the middle keeps 6
        bot_h = max(rest // 2 - 1, min(13, rest - 6))
        mid_h = rest - bot_h
        left_off = sec.get("where we left off") or sec.get("status") or ""
        if not left_off and last:
            left_off = last.get("recap") or f"Last prompt: {last.get('last_prompt', '')}"
        nxt = sec.get("next steps") or sec.get("next") or ""
        my, by = y + top_h, y + top_h + mid_h
        # reading order: what we are building, where we left off, next steps, activity
        if rw >= 70:
            half = rw // 2
            self.text_box(scr, my, rx, mid_h, half, "What we are building", p["about"], NO_ABOUT)
            self.text_box(scr, my, rx + half, mid_h, rw - half, "Where we left off", left_off, "nothing recorded yet")
            if bot_h >= 3:
                self.text_box(scr, by, rx, bot_h, half, "Next steps", nxt, "add a Next steps section to STATUS.md")
                scr.box(by, rx + half, bot_h, rw - half, "Activity")
                self.proj_activity(scr, by + 1, rx + half + 2, bot_h - 2, rw - half - 4, p)
        else:
            self.text_box(scr, my, rx, mid_h, rw, "What we are building", p["about"], NO_ABOUT)
            if bot_h >= 3:
                both = left_off + (f"\n\nNext: {nxt}" if nxt else "")
                self.text_box(scr, by, rx, bot_h, rw, "Where we left off", both, "nothing recorded yet")

    def proj_activity(self, scr, y, x, h, w, p):
        a = git_activity(p["folder"])
        if not a:
            scr.put(y, x, "not a git repo · no commits to show", C["dim"], w)
            return
        now = time.time()
        week = [t for t in a["commits"] if now - t < 7 * 86400]
        if not a["upstream"]:
            push = ("no upstream branch", C["dim"])
        else:
            state = "up to date" if not a["ahead"] and not a["behind"] else \
                " · ".join(t for t in (f"{a['ahead']} to push" if a["ahead"] else "",
                                       f"{a['behind']} to pull" if a["behind"] else "") if t)
            last_p = f" · last {fmt_dur(now - a['pushes'][0])} ago · {len(a['pushes'])} recorded" if a["pushes"] else ""
            push = (state + last_p, C["sand"] if a["ahead"] else None)
        sess = p["sessions"]
        days_on = {e["updated"][:10] for e in sess} | {e.get("started", "")[:10] for e in sess} - {""}
        commits = (f"last {fmt_dur(now - a['commits'][0])} ago · {len(week)} this week · {len(a['commits'])} in a year"
                   if a["commits"] else "none in the past year")
        rows = [("Commits", commits, None if a["commits"] else C["dim"]),
                ("Pushes", *push),
                ("Claude", f"{len(sess)} session{'s' * (len(sess) != 1)} on {len(days_on)} day{'s' * (len(days_on) != 1)}" if sess else "no sessions recorded yet",
                 None if sess else C["dim"])]
        heat_h = 7 if h >= 8 else 1  # keep the full calendar; drop stat rows first when short
        rows = rows[:max(0, h - heat_h)]
        gap = 1 if rows and h >= len(rows) + heat_h + 1 else 0
        for i, (k, v, attr) in enumerate(rows):
            scr.kv(y + i, x, k, v, attr, 9, w)
        self.heatmap(scr, y + len(rows) + gap, x, w, heat_h, a["per_day"])

    def heatmap(self, scr, y, x, w, rows, per_day):
        """GitHub-style commit calendar: one square per day, weeks as columns (or one strip of days)."""
        today = datetime.now().date()
        if rows == 7:
            lw = 4
            weeks = max(1, sq_count(w - lw - 1))
            start = today - timedelta(days=today.weekday() + 7 * (weeks - 1))
            for d, name in ((0, "Mon"), (2, "Wed"), (4, "Fri")):
                scr.put(y + d, x, name, C["dim"])
            days = [(start + timedelta(days=i), i // 7, i % 7) for i in range((today - start).days + 1)]
        else:
            lw = 0
            n = sq_count(w)
            days = [(today - timedelta(days=n - 1 - i), i, 0) for i in range(n)]
        top = max(per_day.values(), default=0)
        wave = scr.grow * (days[-1][1] + 2)  # columns fill in left to right as the view opens
        for day, col, row in days:
            c = per_day.get(day, 0) if col < wave else 0
            lvl = 0 if not c else min(4, 1 + int(3 * (c - 1) / max(1, top - 1)) if top > 1 else 4)
            scr.put(y + row, x + lw + 2 * col, SQ, C[f"heat{lvl}"] if lvl else C["faint"])

    def text_box(self, scr, y, x, h, w, title, text, empty=""):
        """Markdown-ish text in a box; `empty` is shown dimmed when there is no text."""
        scr.box(y, x, h, w, title)
        if not text.strip():
            scr.put(y + 1, x + 2, empty, C["dim"], w - 4)
            return
        # join soft-wrapped markdown lines back into paragraphs and list items
        blocks = []
        for raw in text.splitlines():
            ln = raw.strip()
            if not ln:
                blocks.append("")
            elif re.match(r"[-*]\s|#", ln) or not blocks or not blocks[-1]:
                blocks.append(ln)
            else:
                blocks[-1] += " " + ln
        lines = []
        for para in blocks:
            bullet = re.match(r"[-*]\s+(\[[ xX]\]\s+)?(.*)", para)
            if bullet:
                done = bool(bullet[1] and bullet[1].strip("[] ").lower() == "x")
                for j, ln in enumerate(textwrap.wrap(bullet[2], max(8, w - 6)) or [""]):
                    lines.append(("✓ " if done else "· ") + ln if j == 0 else "  " + ln)
            elif para.startswith("#"):
                lines.append(para.lstrip("# ").upper())
            elif para:
                lines.extend(textwrap.wrap(para, max(8, w - 4)))
            elif lines and lines[-1]:
                lines.append("")
        while lines and not lines[-1]:
            lines.pop()
        room = h - 2
        if len(lines) > room:
            # never end on the blank line between paragraphs: use that row for the next line instead
            lines = lines[:room - 1] + [lines[room]] if not lines[room - 1] else lines[:room]
            lines[-1] = lines[-1][:max(0, w - 5)].rstrip() + "…"
        for i, ln in enumerate(lines):
            attr = C["teal"] if ln.startswith("✓") else C["fg"]
            scr.put(y + 1 + i, x + 2, ln, attr, w - 4)

    def tab_system(self, scr, y, x, h, w):
        sy, sn = self.sys, self.sys.snap
        bw = min(w, 64)
        bx = x + (w - bw) // 2
        ix = bx + 2
        lw = 9
        cells = 29
        t = sn["temps"]
        rows = 10
        hist = h >= rows + 2 + 5
        r = y + max(0, (h - (rows + 2) - (5 if hist else 0)) // 2)
        scr.box(r, bx, rows + 2, bw, f"{sy.user}@{sy.host}")
        r += 1

        def meter_row(label, pct, text, attr=None):
            nonlocal r
            cx = scr.kv(r, ix, label, lw=lw)
            scr.meter(r, cx, cells, pct, attr or level(pct))
            scr.put(r, cx + cells + 2, text, C["fg"], bw - (cx - bx) - cells - 4)
            r += 1

        def text_row(label, text, attr=None):
            nonlocal r
            scr.kv(r, ix, label, text, attr, lw, bw - 4)
            r += 1

        gb = lambda b: f"{b / 2**30:.1f}"
        text_row("OS", f"{sy.os} · kernel {sy.kernel}")
        text_row("Uptime", fmt_dur(sn["uptime"]))
        meter_row("CPU", sn["cpu"], f"{sn['cpu']:.0f}%  {sy.cpu_model}")
        mu, mt = sn["mem"]
        meter_row("Memory", 100 * mu / mt, f"{gb(mu)} / {gb(mt)}G")
        su, st = sn["swap"]
        meter_row("Swap", 100 * su / st if st else 0, f"{gb(su)} / {gb(st)}G")
        du, dt = sn["disk"]
        meter_row("Disk", 100 * du / dt, f"{du / 2**30:.0f} / {dt / 2**30:.0f}G")
        bat = sn["bat"]
        if bat:
            extra = f" · {bat['watts']:.0f}W" if bat["watts"] >= 0.5 else ""
            meter_row("Battery", bat["pct"], f"{bat['pct']}% {bat['status']}{extra}",
                      C["clay"] if bat["pct"] <= 15 else C["sand"] if bat["pct"] <= 30 else C["teal"])
        else:
            text_row("Battery", "-")
        cx = scr.kv(r, ix, "Temp", lw=lw)
        for label, key, bad in (("cpu", "cpu", 90), ("nvme", "nvme", 70)):
            if key in t:
                cx += scr.put(r, cx, f"{label} ", C["dim"])
                cx += scr.put(r, cx, f"{t[key]:.0f}°", level(t[key], bad - 15, bad)) + 2
        r += 1
        text_row("Fan", f"{sn['fan']} rpm" if sn["fan"] is not None else "-")
        la = sn["load"]
        text_row("Load", f"{la[0]:.2f}  {la[1]:.2f}  {la[2]:.2f}")

        if hist:
            r += 2
            scr.box(r, bx, 4, bw, "Last 2 minutes")
            sw = bw - 4 - lw
            scr.kv(r + 1, ix, "CPU", spark(sy.cpu_hist, sw, 0, 100), C["teal"], lw)
            scr.kv(r + 2, ix, "Temp", spark(sy.temp_hist, sw, 30, 100), C["sand"], lw)

    # ---- loop
    def run(self):
        curses.curs_set(0)
        init_colors()
        if THEME["paint"]:
            self.win.bkgd(" ", C["fg"])  # fill every empty cell with the theme background
        self.win.keypad(True)
        self.tick(force=True)
        while True:
            self.tick()
            view = (self.tab, self.proj_name, self.sel_sid, self.day, self.metric)
            if view != self.view:
                self.view, self.anim_at = view, time.monotonic()
            self.draw()
            self.win.refresh()
            # 30 fps while something moves (the Overview title, bars growing in), else 4 fps
            moving = (self.tab == 0 and self.intro.active()) or \
                (MOTION and time.monotonic() - self.anim_at < GROW_S + 0.1)
            self.win.timeout(33 if moving else 250)
            key = self.win.getch()
            if key in (ord("q"), 27):
                return
            if key == 9:
                self.tab = (self.tab + 1) % len(TABS)
            elif key == curses.KEY_BTAB:
                self.tab = (self.tab - 1) % len(TABS)
            elif ord("1") <= key <= ord(str(len(TABS))):
                self.tab = key - ord("1")
            elif TABS[self.tab] == "Projects" and self.projects and key in (
                    curses.KEY_UP, ord("k"), curses.KEY_DOWN, ord("j")):
                step = -1 if key in (curses.KEY_UP, ord("k")) else 1
                self.proj_name = self.projects[max(0, min(len(self.projects) - 1, self.proj_idx + step))]["name"]
            elif TABS[self.tab] == "Projects" and self.projects and key == ord("o"):
                try:
                    subprocess.Popen(["xdg-open", self.projects[self.proj_idx]["folder"]],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                except OSError:
                    pass
            elif key == ord("t"):
                self.metric = (self.metric + 1) % len(METRICS)
            elif TABS[self.tab] == "Live" and key in (curses.KEY_LEFT, ord("h"), curses.KEY_RIGHT, ord("l")) and self.live:
                step = -1 if key in (curses.KEY_LEFT, ord("h")) else 1
                self.sel_sid = self.live[(self.sel + step) % len(self.live)]["session"].sid
            elif key in (curses.KEY_LEFT, ord("h")):
                self.day = min(6, self.day + 1)
            elif key in (curses.KEY_RIGHT, ord("l")):
                self.day = max(0, self.day - 1)
            elif key == ord("r"):
                self.usage = Usage()
                self.tick(force=True)


def main():
    if sys.argv[1:2] == ["hook"]:
        return hook_main()
    if sys.argv[1:2] == ["backfill"]:
        return backfill_main()
    if not os.path.isdir(PROJECTS):
        sys.exit(f"cctop: no Claude Code data at {PROJECTS}")
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    try:
        curses.wrapper(lambda win: App(win).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
