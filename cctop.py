#!/usr/bin/env python3
"""cctop: Claude Code usage + laptop health, in the terminal.

Reads Claude Code's local transcripts (~/.claude/projects/**/*.jsonl) and live
session files (~/.claude/sessions/*.json). Standard library only.
"""

import curses
import glob
import json
import locale
import os
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

# Last Horizon tokens (~/Projects/smh-design-system/tokens/tokens.css)
PALETTE = {
    "fg": "#e2dddc",     # ink-100
    "muted": "#8a8588",  # ink-400
    "dim": "#6e6769",    # ink-500
    "faint": "#3a3437",  # ink-700
    "rose": "#b59790",   # rose-400, accent
    "teal": "#87a9b0",   # teal-400, ok
    "sand": "#c9ae86",   # sand-400, warn
    "clay": "#c38b7b",   # clay-400, danger
    "lav": "#a5a0b6",    # lavender-400
}
BASIC = {"fg": curses.COLOR_WHITE, "muted": curses.COLOR_WHITE, "dim": curses.COLOR_WHITE,
         "faint": curses.COLOR_WHITE, "rose": curses.COLOR_MAGENTA, "teal": curses.COLOR_CYAN,
         "sand": curses.COLOR_YELLOW, "clay": curses.COLOR_RED, "lav": curses.COLOR_BLUE}
C = {}

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
        self.models = defaultdict(int)
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
        self.n_replies = 0
        self.tokens = [0, 0, 0, 0, 0]  # input, output, cache write, cache read, thinking
        self.think_ms = 0
        self.turns = []            # turn durations, ms
        self.tools = defaultdict(int)
        self.tool_errors = 0
        self.mcp = defaultdict(int)
        self.web = 0
        self.activity = deque(maxlen=200)  # dicts: ts, name, summary, status, end
        self.inflight = {}         # tool_use id -> activity dict
        self.files = {}            # path -> {op, added, removed, ts, n}
        self.added = 0
        self.removed = 0
        self.todos = []
        self.cost = None


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
        if kind == "cost-state":
            s.cost = d
            return
        if kind == "system":
            if d.get("subtype") == "turn_duration" and d.get("durationMs"):
                s.turns.append(d["durationMs"])
            elif d.get("subtype") == "away_summary" and d.get("content"):
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

        s.think_ms += d.get("thinkingDurationMs") or 0
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
        s.tools[name] += 1
        act = {"ts": ts, "name": name, "summary": summ, "status": "run", "end": None}
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
        path = inp.get("file_path") or inp.get("notebook_path")
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
            s.added += add
            s.removed += rem

    def tool_done(self, s, b, ts):
        tid = b.get("tool_use_id")
        s.pending.pop(tid, None)
        act = s.inflight.pop(tid, None)
        err = bool(b.get("is_error"))
        if act:
            act["status"] = "err" if err else "ok"
            act["end"] = ts
        if err:
            s.tool_errors += 1
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
        think = (u.get("output_tokens_details") or {}).get("thinking_tokens", 0) or 0
        for i, v in enumerate((inp, out, cw, cr, think)):
            s.tokens[i] += v
        s.models[model] += inp + out + cw + cr
        if not sub:
            s.model = model or s.model
            s.ctx = inp + cw + cr
            s.ctx_peak = max(s.ctx_peak, s.ctx)
            s.n_replies += 1
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


# ---------------------------------------------------------------- system data

_proc_prev = {}
CLK = os.sysconf("SC_CLK_TCK")


def proc_info(pid):
    """CPU, memory, threads and child processes of one pid, from /proc."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().rsplit(")", 1)[1].split()
        status = read(f"/proc/{pid}/status")
        fds = len(os.listdir(f"/proc/{pid}/fd"))
    except OSError:
        return None
    ticks = int(fields[11]) + int(fields[12])
    now = time.time()
    prev = _proc_prev.get(pid)
    cpu = 100 * (ticks - prev[0]) / CLK / (now - prev[1]) if prev and now > prev[1] else 0.0
    if not prev or now - prev[1] >= 1.5:
        _proc_prev[pid] = (ticks, now)
    elif prev:
        cpu = _proc_prev.get((pid, "cpu"), cpu)
    _proc_prev[(pid, "cpu")] = cpu
    rss = re.search(r"VmRSS:\s+(\d+)", status)
    uptime = float(read("/proc/uptime", "0").split()[0]) - int(fields[19]) / CLK

    parents = defaultdict(list)
    names = {}
    for st in glob.glob("/proc/[0-9]*/stat"):
        try:
            with open(st) as f:
                raw = f.read()
        except OSError:
            continue
        cpid = int(st.split("/")[2])
        names[cpid] = raw[raw.find("(") + 1:raw.rfind(")")]
        parents[int(raw.rsplit(")", 1)[1].split()[1])].append(cpid)
    children = defaultdict(int)
    stack = list(parents.get(pid, []))
    while stack:
        c = stack.pop()
        children[names.get(c, "?")] += 1
        stack.extend(parents.get(c, []))
    return {"cpu": cpu, "rss": int(rss[1]) * 1024 if rss else 0, "threads": int(fields[17]),
            "fds": fds, "uptime": uptime, "children": dict(children)}

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
        path = os.path.join(meta_dir, "sessions.json")
        try:
            with open(path) as f:
                log = json.load(f)
        except (OSError, ValueError):
            log = {}
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
            "tokens": sum(s.tokens[:4]),
        }
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(log, f, indent=1)
        os.replace(tmp, path)
        write_log_md(name, meta_dir, log)
    return bool(hits)


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
    tmp = os.path.join(meta_dir, "log.md.tmp")
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
        out.append({"name": name, "folder": folder, "roots": roots, "meta": meta,
                    "sections": sections(body), "has_status": bool(status_text), "sessions": sess,
                    "docs": docs, "last": max(times), "live": live_now})
    out.sort(key=lambda p: (not p["live"], -p["last"]))
    return out


# ---------------------------------------------------------------- drawing

VERSION = "0.2.0"
ITALIC = getattr(curses, "A_ITALIC", 0)
TABS = ["Overview", "Projects", "Live", "Usage", "System"]

# 8px-tall bitmaps, rendered two pixel rows per cell with half blocks
LOGO = {
    "c": ["......", "......", ".#####", "##....", "##....", "##....", ".#####", "......"],
    "t": [".##...", ".##...", "######", ".##...", ".##...", ".##...", "..####", "......"],
    "o": ["......", "......", ".####.", "##..##", "##..##", "##..##", ".####.", "......"],
    "p": ["......", "......", "#####.", "##..##", "##..##", "#####.", "##....", "##...."],
    ".": ["..", "..", "..", "..", "..", "##", "##", ".."],
}


def logo_rows(text):
    rows = []
    for r in range(0, 8, 2):
        parts = []
        for g in (LOGO[ch] for ch in text):
            parts.append("".join(" ▀▄█"[(a == "#") + 2 * (b == "#")] for a, b in zip(g[r], g[r + 1])))
        rows.append(" ".join(parts))
    return rows


def init_colors():
    curses.start_color()
    curses.use_default_colors()
    names = list(PALETTE)
    for i, name in enumerate(names, start=1):
        h = PALETTE[name]
        r, g, b = (int(h[j:j + 2], 16) for j in (1, 3, 5))
        if curses.COLORS >= 256 and curses.can_change_color():
            idx = 232 - len(names) + i  # borrow a few cube slots; ncurses restores them on exit
            curses.init_color(idx, r * 1000 // 255, g * 1000 // 255, b * 1000 // 255)
            col = idx
        elif curses.COLORS >= 256:
            col = nearest_256(r, g, b)
        else:
            col = BASIC[name]
        curses.init_pair(i, col, -1)
        C[name] = curses.color_pair(i)
    if curses.COLORS < 256:
        for name in ("dim", "faint", "muted"):
            C[name] |= curses.A_DIM


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
    def __init__(self, win):
        self.win = win
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
        b = C["muted"]
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
        n = self.put(y, x, f"{label}:".ljust(lw), C["teal"]) + 1
        if value != "":
            room = None if maxw is None else maxw - n
            self.put(y, x + n, value, C["fg"] if attr is None else attr, room)
        return x + n

    def meter(self, y, x, cells, pct, attr):
        on = round(cells * min(max(pct, 0), 100) / 100)
        self.put(y, x, "■" * on, attr)
        self.put(y, x + on, "■" * (cells - on), C["faint"])

    def hbar(self, y, x, cells, frac, attr):
        eighths = round(cells * 8 * min(max(frac, 0), 1))
        full, rem = divmod(eighths, 8)
        self.put(y, x, "█" * full + ("▏▎▍▌▋▊▉"[rem - 1] if rem else ""), attr)
        if eighths == 0 and frac > 0:
            self.put(y, x, "▏", attr)


SPARK = " ▁▂▃▄▅▆▇█"


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
        self.projects = []
        self.projects_at = 0
        self.proj_name = None
        self.proj_idx = 0
        self.last_usage = 0
        self.last_sys = 0
        self.live = []
        self.limits = Limits()

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
        per_session = defaultdict(int)
        kinds = [0, 0, 0, 0]
        week_sessions = set()
        today = today_msgs = week = week_msgs = 0
        for e in self.usage.events:
            ts = e[0]
            v = metric_value(e, m)
            per_session[e[3]] += v
            if ts < week_start:
                continue
            days[(ts - week_start).days] += v
            week += v
            week_msgs += 1
            week_sessions.add(e[3])
            models[short_model(e[1])] += v
            projects[short_path(e[2])] += v
            for i in range(4):
                kinds[i] += e[4 + i]
            if ts >= midnight:
                today += v
                today_msgs += 1
            if sel <= ts < sel + timedelta(days=1):
                hours[ts.hour] += v

        # 5-hour usage window: starts at the hour of the first message after the
        # previous window ended
        block_end = None
        block = 0
        for e in self.usage.events:
            if block_end is None or e[0] >= block_end:
                block_end = e[0].replace(minute=0, second=0, microsecond=0) + timedelta(hours=5)
                block = 0
            block += metric_value(e, m)
        if block_end is None or block_end <= now:
            block, block_end = 0, None

        return dict(hours=hours, days=days, week_start=week_start, models=models,
                    projects=projects, per_session=per_session, kinds=kinds, today=today,
                    today_msgs=today_msgs, week=week, week_msgs=week_msgs,
                    sessions=len(week_sessions), block=block, block_end=block_end,
                    now=now, sel=sel)

    # ---- frame
    def draw(self):
        scr = Screen(self.win)
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
        a = self.a
        logo = logo_rows("cctop.")
        # full layout needs 22 rows; on short terminals the two boxes lose their
        # lowest-priority lines and the gap above them goes
        compact = h < len(logo) + 5 + 10 + 3
        box_h = 8 if compact else 10
        gap = 0 if compact else 1
        total = len(logo) + 4 + gap + box_h + 3
        r = y + max(0, (h - total) // 2)

        for line in logo:
            scr.center(r, x, w, line, C["fg"] | curses.A_BOLD)
            r += 1
        tag, accent = "Claude Code usage, ", "at a glance."
        tx = x + (w - len(tag + accent)) // 2
        scr.put(r + 1, tx, tag, C["fg"])
        scr.put(r + 1, tx + len(tag), accent, C["sand"] | ITALIC)
        scr.put(r + 2, tx, "─" * len(tag + accent), C["faint"])

        busy = sum(1 for s in self.live if s["status"] == "busy")
        agents = sum(s["agents"] for s in self.live)
        if self.live:
            bits = [f"{len(self.live)} live", f"{busy} busy"] + ([f"{agents} agents"] if agents else [])
            status = " · ".join(bits)
        else:
            status = "no live sessions"
        sx = x + (w - len(status) - 2) // 2
        scr.put(r + 3, sx, "[", C["muted"])
        scr.put(r + 3, sx + 1, status, C["teal"] if busy else C["dim"])
        scr.put(r + 3, sx + 1 + len(status), "]", C["muted"])
        r += 4 + gap

        bw = min(58, (w - 2) // 2)
        bx = x + (w - 2 * bw - 2) // 2
        cur = self.live[0] if self.live else None
        self.box_limits(scr, r, bx, box_h, bw, cur)
        self.box_session(scr, r, bx + bw + 2, box_h, bw, cur, compact)
        self.box_sysline(scr, r + box_h, bx, 3, 2 * bw + 2)

    def box_limits(self, scr, y, x, h, w, cur):
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
                win = lim.data.get(key) or {}
                used = win.get("utilization")
                if used is None:
                    scr.kv(r, ix, label, "-", C["dim"], lw)
                    r += 2
                    continue
                left = max(0.0, 100 - used)
                attr = C["teal"] if left > 50 else C["sand"] if left > 20 else C["clay"]
                cx = scr.kv(r, ix, label, lw=lw)
                pct = f"{left:3.0f}% left"
                cells = max(4, w - 4 - (cx - ix) - len(pct) - 2)
                scr.meter(r, cx, cells, left, attr)
                scr.put(r, cx + cells + 2, pct, attr | curses.A_BOLD)
                note = f"{used:.0f}% used"
                try:
                    reset = datetime.fromisoformat(win["resets_at"])
                    secs = (reset - now).total_seconds()
                    when = f"in {fmt_dur(secs)}" if secs < 86400 else reset.astimezone().strftime("%a %H:%M")
                    note += f" · {'resets ' if len(note) + len(when) + 10 < w - 2 - (cx - x) else ''}{when}"
                except (KeyError, TypeError, ValueError):
                    pass
                scr.put(r + 1, cx, note, C["dim"], w - 2 - (cx - x))
                r += 2
        s = cur["session"] if cur else None
        rows = [("Model", short_model(s.model) if s and s.model else "-"),
                ("Project", short_path(cur["cwd"]) if cur else "-"),
                ("Context", fmt_n(s.ctx) if s and s.ctx else "-"),
                ("Today", f"{fmt_n(self.a['today'])} tokens · {self.a['today_msgs']} msgs")]
        for k, v in rows:
            if r >= y + h - 1:
                break
            scr.kv(r, ix, k, v, lw=lw, maxw=w - 4)
            r += 1

    def box_sysline(self, scr, y, x, h, w):
        sy, sn = self.sys, self.sys.snap
        if y + h > scr.h - 1:
            return
        scr.box(y, x, h, w, sy.host)
        t = sn["temps"]
        mu, mt = sn["mem"]
        du, dt = sn["disk"]
        bat = sn["bat"]
        segs = [("CPU", sn["cpu"], f"{sn['cpu']:.0f}%" + (f" {t['cpu']:.0f}°" if "cpu" in t else ""), None),
                ("Mem", 100 * mu / mt, f"{100 * mu / mt:.0f}%", None),
                ("Disk", 100 * du / dt, f"{(dt - du) / 2**30:.0f}G free", None)]
        if bat:
            segs.append(("Bat", bat["pct"], f"{bat['pct']}%" + (" +" if bat["status"] == "charging" else ""),
                         C["clay"] if bat["pct"] <= 15 else C["sand"] if bat["pct"] <= 30 else C["teal"]))
        extras = [("Up", fmt_dur(sn["uptime"]))]
        if "nvme" in t:
            extras.append(("SSD", f"{t['nvme']:.0f}°"))
        if sn["fan"] is not None:
            extras.append(("Fan", f"{sn['fan']}rpm"))
        room = w - 4
        # shrink until the core segments fit: smaller meters, tighter gaps, shorter disk text
        for cells, gap, short in ((5, 3, False), (4, 2, False), (3, 2, True), (0, 2, True)):
            if short:
                segs[2] = ("Disk", segs[2][1], f"{100 * du / dt:.0f}%", None)
            widths = [len(k) + 2 + (cells + 1 if cells else 0) + len(v) for k, _, v, _ in segs]
            used = sum(widths) + gap * (len(segs) - 1)
            if used <= room:
                break
        shown = []
        for k, v in extras:
            need = len(k) + 2 + len(v) + gap
            if used + need <= room:
                shown.append((k, v))
                used += need
        cx = x + 2 + max(0, (room - used) // 2)
        for (k, pct, v, attr), sw in zip(segs, widths):
            n = scr.put(y + 1, cx, f"{k}:", C["teal"]) + 1
            temp_hot = k == "CPU" and t.get("cpu", 0) >= 85
            if cells:
                scr.meter(y + 1, cx + n, cells, pct, attr or level(pct))
            scr.put(y + 1, cx + n + (cells + 1 if cells else 0), v, C["clay"] if temp_hot else C["fg"])
            cx += sw + gap
        for k, v in shown:
            n = scr.put(y + 1, cx, f"{k}:", C["teal"]) + 1
            scr.put(y + 1, cx + n, v, C["fg"])
            cx += n + len(v) + gap

    def box_session(self, scr, y, x, h, w, cur, compact=False):
        scr.box(y, x, h, w, cur["name"] if cur else "Session")
        ix, lw = x + 2, 10
        if not cur:
            scr.put(y + 1, ix, "no claude code session running", C["dim"])
            return
        s = cur["session"]
        busy = cur["status"] == "busy"
        if busy:
            pend = list(s.pending.values())
            doing = pend[-1][1] if pend else (f"last {s.last_tool}" if s.last_tool else "thinking")
        else:
            doing = "waiting for you"
        agents = f"{cur['agents']} running" if cur["agents"] else "none"
        if cur["agents_today"]:
            agents += f" · {cur['agents_today']} today"
        g = git_info(cur["cwd"])
        if g:
            repo = g["repo"] + (f" · {g['branch']}" if g["branch"] else "")
            if g["dirty"]:
                repo += f" · {g['dirty']} changed"
        else:
            repo = "not a git repo"
        user = github_user()
        github = f"connected as {user}" if user else "not connected"
        if g and g["github"]:
            github += " · remote on github"
        status = cur["status"] + (f" · {fmt_dur(time.time() - cur['started'])}" if cur["started"] else "")
        rows = [("Status", status, C["teal"] if busy else C["dim"]),
                ("Prompt", one_line(s.prompt) or "-", C["fg"] | ITALIC),
                ("Doing", doing, C["sand"] if busy else C["dim"]),
                ("Agents", agents, C["fg"] if cur["agents"] else C["dim"]),
                ("Skills", ", ".join(s.skills[-3:]) if s.skills else "none used", C["fg"] if s.skills else C["dim"]),
                ("Directory", short_path(cur["cwd"]), C["fg"]),
                ("Repo", repo, C["fg"] if g else C["dim"]),
                ("GitHub", github, C["teal"] if user else C["dim"])]
        if compact:
            rows = [r for r in rows if r[0] not in ("Skills", "GitHub")]
        for i, (k, v, attr) in enumerate(rows[:h - 2]):
            scr.kv(y + 1 + i, ix, k, v, attr, lw, w - 4)

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
            eighths = round(v / peak * ch * 8) if peak else 0
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
        self.box_ranked(scr, by, x + 2 * cw, bottom_h, w - 2 * cw, "Projects", a["projects"])

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

    def box_ranked(self, scr, y, x, h, w, title, data):
        scr.box(y, x, h, w, title)
        items = sorted(data.items(), key=lambda kv: -kv[1])
        total = sum(data.values()) or 1
        if not items:
            scr.put(y + 1, x + 2, "no usage yet", C["dim"])
            return
        for i, (k, v) in enumerate(items[:h - 2]):
            r = y + 1 + i
            tail = f"{100 * v / total:.0f}%"
            nw = w - 5 - len(tail)
            name = k if len(k) <= nw else "…" + k[-(nw - 1):]
            scr.put(r, x + 2, name, C["fg"] if i == 0 else C["teal"])
            scr.put(r, x + w - 2 - len(tail), tail, C["dim"])

    def tab_live(self, scr, y, x, h, w):
        if not self.live:
            scr.center(y + h // 2, x, w, "no claude code sessions running", C["dim"])
            return
        sids = [r["session"].sid for r in self.live]
        self.sel = sids.index(self.sel_sid) if self.sel_sid in sids else 0
        self.sel_sid = sids[self.sel]
        cur = self.live[self.sel]

        # session picker
        cx = x
        for i, row in enumerate(self.live):
            busy = row["status"] == "busy"
            chip = f" {row['name']} "
            if cx + len(chip) + 4 > x + w:
                scr.put(y, cx, f"+{len(self.live) - i}", C["dim"])
                break
            cx += scr.put(y, cx, "●" if busy else "○", C["teal"] if busy else C["dim"])
            if i == self.sel:
                cx += scr.put(y, cx, "[", C["muted"])
                cx += scr.put(y, cx, chip.strip(), C["fg"] | curses.A_BOLD)
                cx += scr.put(y, cx, "]", C["muted"]) + 2
            else:
                cx += scr.put(y, cx, chip, C["teal"]) + 1
        info = f"session {self.sel + 1} of {len(self.live)}"
        scr.put(y, x + w - len(info), info, C["dim"])

        s = cur["session"]
        boxes = [  # title, renderer, min height, wanted height
            ("Session", self.lv_session, 8, 13),
            ("Now", self.lv_now, 6, 10),
            ("Tokens", self.lv_tokens, 7, 12),
            ("Activity", self.lv_activity, 6, 14),
            ("Tools", self.lv_tools, 5, min(12, len(s.tools) + 4)),
            ("Files", self.lv_files, 5, min(12, len(s.files) + 4)),
            ("Agents & skills", self.lv_agents, 6, min(12, len(s.agents) + 6)),
            ("Process", self.lv_process, 6, 8),
            ("History", self.lv_turns, 6, 10),
        ]
        if s.todos:
            boxes.insert(2, ("Todos", self.lv_todos, 4, min(12, len(s.todos) + 3)))

        ncol = 3 if w >= 150 else 2
        top, bottom = y + 1, y + h
        colw = [(w - (ncol - 1)) // ncol] * ncol
        colw[-1] = w - sum(colw[:-1]) - (ncol - 1)
        colx = [x + sum(colw[:i]) + i for i in range(ncol)]
        cursor = [top] * ncol
        placed = [[] for _ in range(ncol)]
        for title, fn, hmin, hwant in boxes:
            order = sorted(range(ncol), key=lambda c: cursor[c])
            for c in order:
                if cursor[c] + hmin <= bottom:
                    bh = min(hwant, bottom - cursor[c])
                    placed[c].append([title, fn, cursor[c], bh])
                    cursor[c] += bh
                    break
        for c in range(ncol):
            if placed[c]:
                placed[c][-1][3] = bottom - placed[c][-1][2]  # last box fills the column
            for title, fn, by, bh in placed[c]:
                scr.box(by, colx[c], bh, colw[c], title)
                fn(scr, by + 1, colx[c] + 2, bh - 2, colw[c] - 4, cur, s)

    def lv_session(self, scr, y, x, h, w, cur, s):
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
            ("Started", f"{started:%H:%M} · {fmt_dur((datetime.now() - started).total_seconds())} ago"
             if started else "-", None),
            ("Model", model, C["rose"]),
            ("Mode", s.perm_mode or "default", C["sand"] if s.perm_mode in ("auto", "bypassPermissions") else None),
            ("Directory", short_path(cur["cwd"]), None),
            ("Branch", (g["branch"] if g and g["branch"] else s.branch) or "-", None),
            ("Repo", repo, None if g else C["dim"]),
            ("GitHub", f"connected as {user}" if user else "not connected", C["teal"] if user else C["dim"]),
            ("Version", " · ".join(b for b in (s.version, cur["entrypoint"] or s.entrypoint, cur["kind"]) if b), C["dim"]),
            ("ID", f"{s.sid[:8]} · pid {cur['pid']}", C["dim"]),
        ]
        for i, (k, v, attr) in enumerate(rows[:h]):
            scr.kv(y + i, x, k, v, attr, 10, w)

    def lv_now(self, scr, y, x, h, w, cur, s):
        busy = cur["status"] == "busy"
        r = y
        pend = [a for a in s.inflight.values()]
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
        prompt_lines = max(1, (h - 2) // 2 + (h - 2) % 2)
        for label, text, attr, n in (("Prompt", s.prompt, C["fg"] | ITALIC, prompt_lines),
                                     ("Recap", s.recap, C["dim"], h - 2 - prompt_lines)):
            if r >= y + h or n <= 0:
                break
            scr.kv(r, x, label, lw=8)
            lines = textwrap.wrap(one_line(text, 2000), max(10, w - 9)) or ["-"]
            if len(lines) > n:
                lines = lines[:n]
                lines[-1] = lines[-1][:max(0, w - 10)] + "…"
            for ln in lines:
                if r >= y + h:
                    break
                scr.put(r, x + 9, ln, attr, w - 9)
                r += 1

    def lv_todos(self, scr, y, x, h, w, cur, s):
        done = sum(1 for t in s.todos if t.get("status") == "completed")
        for i, t in enumerate(s.todos[:h - 1]):
            st = t.get("status")
            mark, attr = {"completed": ("✓", C["teal"]), "in_progress": ("›", C["sand"])}.get(st, ("·", C["dim"]))
            scr.put(y + i, x, mark, attr)
            text = t.get("activeForm") if st == "in_progress" and t.get("activeForm") else t.get("content", "")
            scr.put(y + i, x + 2, text, C["fg"] if st == "in_progress" else C["dim"] if st == "completed" else C["muted"], w - 2)
        scr.put(y + h - 1, x, f"{done}/{len(s.todos)} done", C["dim"])

    def lv_tokens(self, scr, y, x, h, w, cur, s):
        inp, out, cw, cr, think = s.tokens
        total = inp + out + cw + cr
        hit = 100 * cr / max(1, inp + cw + cr)
        rows = [("Input", fmt_n(inp)), ("Output", f"{fmt_n(out)}" + (f" · {fmt_n(think)} thinking" if think else "")),
                ("Cache w", fmt_n(cw)), ("Cache r", fmt_n(cr)),
                ("Total", fmt_n(total)), ("Cache hit", f"{hit:.0f}%"),
                ("Context", f"{fmt_n(s.ctx)} now · {fmt_n(s.ctx_peak)} peak")]
        if len(s.models) > 1:
            rows.append(("Models", ", ".join(f"{short_model(m)} {fmt_n(v)}" for m, v in
                                              sorted(s.models.items(), key=lambda kv: -kv[1]) if m)))
        r = y
        for k, v in rows:
            if r >= y + h - (2 if h > len(rows) + 1 else 0):
                break
            scr.kv(r, x, k, v, C["fg"] | curses.A_BOLD if k == "Total" else None, 10, w)
            r += 1
        if r + 1 < y + h:
            # tokens per minute over the last hour, this session only
            m = METRICS[self.metric][0]
            now = datetime.now()
            buckets = [0] * 60
            for e in self.usage.events:
                if e[3] == s.sid:
                    age = int((now - e[0]).total_seconds() // 60)
                    if 0 <= age < 60:
                        buckets[59 - age] += metric_value(e, m)
            width = max(10, w - 11)
            step = 60 / width
            cols = [sum(buckets[int(i * step):max(int(i * step) + 1, int((i + 1) * step))]) for i in range(width)]
            peak = max(cols) or 1
            line = "".join(SPARK[min(8, round(v / peak * 8))] if v else "·" for v in cols)
            scr.kv(y + h - 2, x, "Last hour", lw=10)
            scr.put(y + h - 2, x + 11, line, C["teal"])
            scr.put(y + h - 1, x + 11, "60m ago", C["dim"])
            scr.put(y + h - 1, x + w - 3, "now", C["dim"])

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
            scr.put(r, x + 12 + n, rest, C["fg"] if i == 0 else C["muted"], w - 13 - n - len(dur))
            scr.put(r, x + w - len(dur), dur, C["dim"])

    def lv_tools(self, scr, y, x, h, w, cur, s):
        items = sorted(s.tools.items(), key=lambda kv: -kv[1])
        total = sum(s.tools.values())
        if not items:
            scr.put(y, x, "no tool calls yet", C["dim"])
            return
        peak = items[0][1]
        nw = min(14, max(len(k) for k, _ in items[:h - 1]) + 1)
        for i, (k, v) in enumerate(items[:h - 1]):
            name = k if len(k) < nw else k[:nw - 2] + "…"
            scr.put(y + i, x, name, C["teal"])
            scr.hbar(y + i, x + nw, max(3, w - nw - 6), v / peak, C["muted"])
            scr.put(y + i, x + w - len(str(v)), str(v), C["fg"])
        err = f" · {s.tool_errors} failed" if s.tool_errors else ""
        scr.put(y + h - 1, x, f"{total} calls · {len(s.tools)} kinds{err}", C["clay"] if s.tool_errors else C["dim"])

    def lv_files(self, scr, y, x, h, w, cur, s):
        files = sorted(s.files.items(), key=lambda kv: kv[1].get("ts") or datetime.min, reverse=True)
        if not files:
            scr.put(y, x, "no files touched yet", C["dim"])
            return
        root = cur["cwd"] or ""
        for i, (path, f) in enumerate(files[:h - 1]):
            mark, attr = {"edit": ("✎", C["sand"]), "write": ("+", C["teal"]), "read": ("○", C["dim"])}[f["op"]]
            rel = os.path.relpath(path, root) if root and path.startswith(root + os.sep) else short_path(path)
            if rel.count(os.sep) > 1:
                rel = os.path.join(*rel.split(os.sep)[-2:])
            delta = (f"+{f['added']} −{f['removed']}" if f["removed"] else f"+{f['added']}") if f["added"] else ""
            scr.put(y + i, x, mark, attr)
            nw = w - 3 - len(delta)
            name = rel if len(rel) <= nw else "…" + rel[-(nw - 1):]
            scr.put(y + i, x + 2, name, C["fg"] if f["op"] != "read" else C["muted"], nw)
            if delta:
                scr.put(y + i, x + w - len(delta), delta, C["teal"])
        edited = sum(1 for f in s.files.values() if f["op"] != "read")
        foot = f"{len(s.files)} files · {edited} changed"
        scr.put(y + h - 1, x, foot, C["dim"])
        lines = f"+{s.added} −{s.removed}"
        if len(foot) + len(lines) + 2 <= w:
            scr.put(y + h - 1, x + w - len(lines), lines, C["teal"])

    def lv_agents(self, scr, y, x, h, w, cur, s):
        r = y
        running = cur["agents"]
        scr.kv(r, x, "Running", str(running), C["rose"] | curses.A_BOLD if running else C["dim"], 9)
        launched = f"{len(s.agents)} this session"
        scr.put(r, x + 12, f"· {launched}", C["dim"], w - 12)
        r += 1
        lines_left = h - 4
        if not s.agents and lines_left > 0:
            scr.put(r, x + 1, "no agents launched in this session", C["dim"], w - 1)
        for a in list(s.agents.values())[-max(0, lines_left):]:
            if r >= y + h - 3:
                break
            mark, attr = {"running": ("›", C["sand"]), "background": ("◌", C["lav"]),
                          "done": ("✓", C["teal"]), "error": ("✕", C["clay"])}[a["status"]]
            scr.put(r, x + 1, mark, attr)
            n = scr.put(r, x + 3, a["type"], C["lav"])
            scr.put(r, x + 4 + n, a["desc"], C["fg"], w - 4 - n)
            r += 1
        r = max(r, y + h - 3)
        scr.kv(r, x, "Skills", ", ".join(reversed(s.skills)) if s.skills else "none used",
               C["fg"] if s.skills else C["dim"], 9, w)
        mcp = ", ".join(f"{k} ×{v}" for k, v in sorted(s.mcp.items(), key=lambda kv: -kv[1]))
        scr.kv(r + 1, x, "MCP", mcp or "none used", C["fg"] if mcp else C["dim"], 9, w)
        scr.kv(r + 2, x, "Web", f"{s.web} searches & fetches" if s.web else "none", C["fg"] if s.web else C["dim"], 9, w)

    def lv_process(self, scr, y, x, h, w, cur, s):
        p = proc_info(cur["pid"])
        if not p:
            scr.put(y, x, "process gone", C["dim"])
            return
        cx = scr.kv(y, x, "CPU", lw=9)
        cells = max(4, min(12, w - 20))
        scr.meter(y, cx, cells, p["cpu"], level(p["cpu"], 50, 85))
        scr.put(y, cx + cells + 1, f"{p['cpu']:.0f}%", C["fg"])
        rows = [("Memory", f"{p['rss'] / 2**20:.0f} MB rss"),
                ("Threads", f"{p['threads']} · {p['fds']} open files"),
                ("Uptime", fmt_dur(p["uptime"])),
                ("Children", ", ".join(f"{k} ×{v}" if v > 1 else k for k, v in p["children"].items()) or "none")]
        for i, (k, v) in enumerate(rows[:h - 1]):
            scr.kv(y + 1 + i, x, k, v, None if k != "Children" or p["children"] else C["dim"], 9, w)

    def lv_turns(self, scr, y, x, h, w, cur, s):
        t = s.turns
        avg = sum(t) / len(t) / 1000 if t else 0
        rows = [("Prompts", f"{s.n_prompts} from you · {s.n_replies} replies"),
                ("Turns", f"{len(t)} · avg {fmt_secs(avg)} · max {fmt_secs(max(t) / 1000)}" if t else "-"),
                ("Thinking", fmt_secs(s.think_ms / 1000) if s.think_ms else "-"),
                ("Last msg", f"{fmt_secs((datetime.now() - s.last_ts).total_seconds())} ago" if s.last_ts else "-")]
        r = y
        for k, v in rows:
            if r >= y + h:
                return
            scr.kv(r, x, k, v, lw=10, maxw=w)
            r += 1
        for ts, text in reversed(list(s.prompts)[:-1]):
            if r >= y + h:
                return
            stamp = ts.strftime("%H:%M") if ts else "--:--"
            scr.put(r, x, stamp, C["dim"])
            scr.put(r, x + 6, one_line(text), C["muted"] | ITALIC, w - 6)
            r += 1

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
        top_h = len(info) + 2
        scr.box(y, rx, top_h, rw, p["name"])
        for i, (k, v, attr) in enumerate(info):
            scr.kv(y + 1 + i, rx + 2, k, v, attr, 10, rw - 4)

        rest = h - top_h
        mid_h = max(4, rest // 2 + 1)
        bot_h = rest - mid_h
        left_off = sec.get("where we left off") or sec.get("status") or ""
        if not left_off and last:
            left_off = last.get("recap") or f"Last prompt: {last.get('last_prompt', '')}"
        nxt = sec.get("next steps") or sec.get("next") or ""
        my = y + top_h
        if rw >= 70:
            half = rw // 2
            self.text_box(scr, my, rx, mid_h, half, "Where we left off", left_off or "nothing recorded yet")
            self.text_box(scr, my, rx + half, mid_h, rw - half, "Next steps", nxt or "add a Next steps section to STATUS.md")
        else:
            both = left_off + (f"\n\nNext: {nxt}" if nxt else "")
            self.text_box(scr, my, rx, mid_h, rw, "Where we left off", both or "nothing recorded yet")

        if bot_h >= 3:
            by = my + mid_h
            scr.box(by, rx, bot_h, rw, "Recent sessions")
            if not sess:
                scr.put(by + 1, rx + 2, "sessions show up here after the next claude reply in this project", C["dim"], rw - 4)
            for i, e in enumerate(sess[:bot_h - 2]):
                r = by + 1 + i
                stamp = e["updated"][5:16].replace("T", " ")
                scr.put(r, rx + 2, stamp, C["dim"])
                n = scr.put(r, rx + 14, e["title"] or "untitled", C["fg"] if i == 0 else C["teal"], rw // 3)
                delta = f"+{e['added']} −{e['removed']}" if e.get("added") or e.get("removed") else ""
                scr.put(r, rx + 16 + n, e.get("last_prompt", ""), C["muted"] | ITALIC, rw - 20 - n - len(delta))
                if delta:
                    scr.put(r, rx + rw - 2 - len(delta), delta, C["teal"])

    def text_box(self, scr, y, x, h, w, title, text):
        scr.box(y, x, h, w, title)
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
            lines = lines[:room]
            lines[-1] = lines[-1][:max(0, w - 6)] + "…"
        for i, ln in enumerate(lines):
            attr = C["teal"] if ln.startswith("✓") else C["dim"] if "yet" in text and len(lines) == 1 else C["fg"]
            scr.put(y + 1 + i, x + 2, ln, attr, w - 4)

    def tab_system(self, scr, y, x, h, w):
        sy, sn = self.sys, self.sys.snap
        bw = min(w, 64)
        bx = x + (w - bw) // 2
        ix = bx + 2
        lw = 9
        cells = 16
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
        self.win.keypad(True)
        self.win.timeout(1000)
        self.tick(force=True)
        while True:
            self.tick()
            self.draw()
            self.win.refresh()
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
