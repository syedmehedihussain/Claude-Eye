#!/usr/bin/env python3
"""Record the README screenshots and animation from a made-up demo home folder.

    python3 tools/screenshots.py

Builds a fake $HOME with demo projects, git history, Claude Code transcripts, live sessions and
plan limits, runs cctop against it in a hidden tmux window, and draws what is on screen to
docs/media/*.png and docs/media/overview.gif. Nothing from your own ~/.claude or ~/Projects is
read or shown.

Needs: tmux, rsvg-convert (librsvg), ffmpeg, and a JetBrains Mono Nerd Font installed.
"""

import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "media")
COLS, ROWS = 120, 34
CW, CH, FONT_PX = 9.6, 20, 16          # cell size and font size in pixels
FONT = "JetBrainsMono Nerd Font"
SESSION = f"cctop-demo-{os.getpid()}"

sys.path.insert(0, ROOT)
import cctop  # noqa: E402  (theme colours and the 256-colour mapping)

rng = random.Random(7)
now = datetime.now().astimezone()


# ---------------------------------------------------------------- demo data

PROJECTS = {
    "atlas-api": {
        "status": "active",
        "summary": "REST API for the Atlas field survey app",
        "building": "A small REST API that stores field surveys, photos and GPS tracks for the "
                    "Atlas mobile app. It runs on FastAPI and Postgres and syncs when a device "
                    "comes back online.",
        "left_off": "Cursor pagination for GET /surveys is in place and the tests pass. The "
                    "photo upload endpoint still returns 500 for files over 10 MB.",
        "next": ["[x] Cursor pagination for /surveys", "[x] Index surveys by project and date",
                 "[ ] Stream large photo uploads", "[ ] Rate limit the sync endpoint",
                 "[ ] Write the deploy runbook"],
        "docs": ["prd.md", "decisions.md"],
        "files": ["src/app.py", "src/routes/surveys.py", "src/routes/photos.py", "src/models.py",
                  "src/db.py", "tests/test_surveys.py", "tests/test_photos.py", "pyproject.toml",
                  "README.md"],
        "commits": 70,
    },
    "pixel-garden": {
        "status": "active",
        "summary": "A pixel-art garden that grows with your commits",
        "building": "A small web toy: every commit you push plants a seed, and the garden grows "
                    "over the week. Canvas rendering, no framework.",
        "left_off": "Seeds now sprout in the right season. The sunflower sprite still flickers "
                    "on Safari.",
        "next": ["[x] Seasons", "[ ] Fix the Safari flicker", "[ ] Share a garden as a PNG"],
        "docs": ["drd.md"],
        "files": ["index.html", "src/garden.js", "src/sprites.js", "src/seasons.js",
                  "assets/sunflower.png", "README.md"],
        "commits": 38,
    },
    "dotfiles": {
        "status": "paused",
        "summary": "Shell, editor and window manager config",
        "building": "My shell, Neovim and Hyprland setup, installed with one script.",
        "left_off": "The install script links everything; the Neovim LSP setup moved to lazy.nvim.",
        "next": ["[ ] Split the Hyprland keybinds into their own file"],
        "docs": [],
        "files": ["install.sh", "zshrc", "nvim/init.lua", "hypr/hyprland.conf"],
        "commits": 25,
    },
    "recipe-box": {
        "status": "idea",
        "summary": "Plain-text recipes with a shopping list generator",
        "building": "Recipes as Markdown files, and a command that turns a week of meals into "
                    "one shopping list.",
        "left_off": "Only an idea so far.",
        "next": ["[ ] Pick a recipe file format"],
        "docs": ["proposal.md"],
        "files": None,
        "commits": 0,
    },
}

PROMPTS = {
    "atlas-api": [
        ("Add cursor pagination to survey list", "Add cursor pagination to GET /surveys and keep the old page parameter working for now"),
        ("Fix large photo uploads", "Photo uploads over 10 MB return 500. Stream them to disk instead of reading the body into memory"),
        ("Index surveys by project", "The survey list is slow for big projects, add the right index and check the query plan"),
    ],
    "pixel-garden": [
        ("Seasonal sprouting", "Seeds should only sprout in the season they were planted for"),
        ("Sunflower flicker on Safari", "The sunflower sprite flickers on Safari, find out why"),
    ],
    "dotfiles": [("Move Neovim LSP to lazy.nvim", "Move the LSP setup to lazy.nvim and drop packer")],
}


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def git(cwd, *args, when=None):
    env = dict(os.environ, GIT_AUTHOR_NAME="Demo", GIT_AUTHOR_EMAIL="demo@example.com",
               GIT_COMMITTER_NAME="Demo", GIT_COMMITTER_EMAIL="demo@example.com",
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
    if when:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when.isoformat()
    subprocess.run(["git", "-C", cwd, *args], env=env, check=True, capture_output=True)


def make_projects(home):
    for name, p in PROJECTS.items():
        folder = os.path.join(home, "Projects", name)
        steps = "\n".join(f"- {s}" for s in p["next"])
        write(os.path.join(folder, "_project", "STATUS.md"), f"""---
name: {name}
status: {p['status']}
summary: {p['summary']}
paths: [~/Projects/{name}]
updated: {now:%Y-%m-%d}
---

## What we are building

{p['building']}

## Where we left off

{p['left_off']}

## Next steps

{steps}
""")
        for doc in p["docs"]:
            write(os.path.join(folder, "_project", doc), f"# {name}: {doc[:-3]}\n")
        if not p["files"]:
            continue
        for f in p["files"]:
            write(os.path.join(folder, f), f"# {f}\n")
        git(folder, "init", "-q", "-b", "main")
        write(os.path.join(folder, ".git", "info", "exclude"), "/_project/\n")
        # commits spread over the last months, busier lately
        days = sorted(int(rng.betavariate(1.2, 3.5) * 150) for _ in range(p["commits"]))[::-1]
        for i, d in enumerate(days):
            when = now - timedelta(days=d, hours=rng.randint(0, 9), minutes=rng.randint(0, 59))
            f = rng.choice(p["files"])
            with open(os.path.join(folder, f), "a") as fh:
                fh.write(f"# change {i}\n")
            git(folder, "add", "-A")
            git(folder, "commit", "-q", "-m", f"Update {os.path.basename(f)}", when=when)
        remote = os.path.join(home, "remotes", name + ".git")
        subprocess.run(["git", "init", "-q", "--bare", remote], check=True)
        git(folder, "remote", "add", "origin", remote)
        git(folder, "push", "-q", "-u", "origin", "main")
        git(folder, "remote", "set-url", "origin", f"git@github.com:demo/{name}.git")
    # uncommitted work in the busy project, for the Git box
    atlas = os.path.join(home, "Projects", "atlas-api")
    with open(os.path.join(atlas, "src", "routes", "photos.py"), "a") as f:
        f.write("".join(f"# streaming upload line {i}\n" for i in range(34)))
    with open(os.path.join(atlas, "tests", "test_photos.py"), "a") as f:
        f.write("".join(f"# test line {i}\n" for i in range(12)))


class Transcript:
    def __init__(self, home, project, model, title):
        self.sid = str(uuid.UUID(int=rng.getrandbits(128)))
        self.cwd = os.path.join(home, "Projects", project)
        self.model = model
        self.lines = [{"type": "ai-title", "aiTitle": title}]
        self.path = os.path.join(home, ".claude", "projects", self.cwd.replace("/", "-"), self.sid + ".jsonl")
        self.ctx = 30_000

    def base(self, kind, ts):
        return {"type": kind, "timestamp": ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                "cwd": self.cwd, "sessionId": self.sid, "version": "2.1.30", "entrypoint": "cli",
                "gitBranch": "main", "permissionMode": "default"}

    def prompt(self, ts, text):
        self.lines.append({**self.base("user", ts), "message": {"role": "user", "content": text}})

    def reply(self, ts, tools=(), text="Done."):
        self.ctx += rng.randint(4_000, 16_000)
        content = [{"type": "text", "text": text}]
        ids = []
        for name, inp in tools:
            tid = "toolu_" + uuid.UUID(int=rng.getrandbits(128)).hex[:20]
            ids.append(tid)
            content.append({"type": "tool_use", "id": tid, "name": name, "input": inp})
        usage = {"input_tokens": rng.randint(3, 40), "output_tokens": rng.randint(300, 2600),
                 "cache_creation_input_tokens": rng.randint(800, 6000),
                 "cache_read_input_tokens": self.ctx}
        self.lines.append({**self.base("assistant", ts), "message": {
            "id": "msg_" + uuid.UUID(int=rng.getrandbits(128)).hex[:20], "role": "assistant",
            "model": self.model, "content": content, "usage": usage}})
        return ids

    def results(self, ts, ids):
        self.lines.append({**self.base("user", ts), "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": t, "content": "ok"} for t in ids]}})

    def save(self):
        write(self.path, "".join(json.dumps(line) + "\n" for line in self.lines))


def work(t, start, minutes, files):
    """A believable run of tool calls: read, edit, test, repeated."""
    ts = start
    end = start + timedelta(minutes=minutes)
    while ts < end:
        f = os.path.join(t.cwd, rng.choice(files))
        step = rng.choice(["read", "edit", "edit", "bash", "grep"])
        if step == "read":
            call = ("Read", {"file_path": f})
        elif step == "edit":
            call = ("Edit", {"file_path": f, "old_string": "x\n" * rng.randint(1, 6),
                             "new_string": "y\n" * rng.randint(2, 14)})
        elif step == "grep":
            call = ("Grep", {"pattern": rng.choice(["cursor", "upload", "season", "sprite"])})
        else:
            call = ("Bash", {"description": rng.choice(["Run the test suite", "Check the query plan",
                                                        "Run the linter", "Start the dev server"]),
                             "command": "pytest -q"})
        ids = t.reply(ts, [call])
        ts += timedelta(seconds=rng.randint(4, 40))
        t.results(ts, ids)
        ts += timedelta(seconds=rng.randint(10, 90))
    return ts


def make_sessions(home):
    """Past sessions over the last week, plus two live ones."""
    for day in range(6, 0, -1):
        for _ in range(rng.randint(1, 3) if day > 1 else 7):
            name = rng.choice(["atlas-api", "atlas-api", "pixel-garden", "dotfiles"])
            title, text = rng.choice(PROMPTS[name])
            t = Transcript(home, name, rng.choice(["claude-opus-5-5", "claude-opus-5-5", "claude-sonnet-5-5"]), title)
            start = (now - timedelta(days=day)).replace(hour=rng.randint(8, 22), minute=rng.randint(0, 59))
            t.prompt(start, text)
            work(t, start + timedelta(seconds=20), rng.randint(20, 80), PROJECTS[name]["files"])
            t.save()

    live = []
    # busy: working in atlas-api right now, a test run in flight
    t = Transcript(home, "atlas-api", "claude-opus-5-5", "Stream large photo uploads")
    start = now - timedelta(minutes=34)
    t.prompt(start - timedelta(minutes=40), "Add cursor pagination to GET /surveys and keep the old page parameter working for now")
    ts = work(t, start - timedelta(minutes=39), 30, PROJECTS["atlas-api"]["files"])
    t.prompt(start, "Photo uploads over 10 MB return 500. Stream them to disk instead of reading the body into memory")
    ids = t.reply(start + timedelta(seconds=15), [("Skill", {"skill": "project-notes"})])
    t.results(start + timedelta(seconds=17), ids)
    ids = t.reply(start + timedelta(seconds=30), [("TodoWrite", {"todos": [
        {"content": "Reproduce the 500 with a 12 MB photo", "status": "completed"},
        {"content": "Stream the upload body to a temp file", "status": "completed"},
        {"content": "Run the photo tests", "status": "in_progress", "activeForm": "Running the photo tests"},
        {"content": "Update the API docs", "status": "pending"}]})])
    t.results(start + timedelta(seconds=31), ids)
    ids = t.reply(start + timedelta(minutes=2), [("Agent", {"description": "Find every place that reads the request body",
                                                            "subagent_type": "Explore", "prompt": "..."})])
    t.results(start + timedelta(minutes=4), ids)
    ts = work(t, start + timedelta(minutes=5), 27, ["src/routes/photos.py", "tests/test_photos.py", "src/app.py"])
    t.ctx = 236_000
    t.reply(now - timedelta(seconds=9), [("Bash", {"description": "Run the photo tests", "command": "pytest tests/test_photos.py -q"})])
    t.save()
    live.append((t, "busy", "photo-uploads", start - timedelta(minutes=40)))

    # waiting: pixel-garden, with a long context to show the reminder
    t = Transcript(home, "pixel-garden", "claude-opus-5-5", "Sunflower flicker on Safari")
    start = now - timedelta(hours=1, minutes=10)
    t.prompt(start, "The sunflower sprite flickers on Safari, find out why")
    work(t, start + timedelta(seconds=20), 55, PROJECTS["pixel-garden"]["files"])
    t.ctx = 446_000
    t.reply(now - timedelta(minutes=6), text="The flicker comes from redrawing on every frame; it is fixed.")
    t.save()
    live.append((t, "idle", "garden", start))
    return live


def make_home(home):
    make_projects(home)
    live = make_sessions(home)
    sleepers = []
    for t, status, name, started in live:
        p = subprocess.Popen(["sleep", "600"])
        sleepers.append(p)
        ms = lambda d: int(d.timestamp() * 1000)
        write(os.path.join(home, ".claude", "sessions", f"{p.pid}.json"), json.dumps({
            "pid": p.pid, "sessionId": t.sid, "name": name, "status": status, "kind": "interactive",
            "entrypoint": "cli", "cwd": t.cwd, "startedAt": ms(started),
            "statusUpdatedAt": ms(now - timedelta(minutes=2 if status == "busy" else 6)),
            "updatedAt": ms(now)}))
    reset5 = now + timedelta(hours=2, minutes=14)
    reset7 = (now + timedelta(days=3)).replace(hour=9, minute=0, second=0, microsecond=0)
    write(os.path.join(home, ".cache", "cctop", "limits.json"), json.dumps({
        "data": {"five_hour": {"utilization": 41.0, "resets_at": reset5.isoformat()},
                 "seven_day": {"utilization": 33.0, "resets_at": reset7.isoformat()}},
        "fetched": time.time()}))
    env = dict(os.environ, HOME=home)
    env.pop("CLAUDE_CONFIG_DIR", None)
    env.pop("CCTOP_PROJECTS", None)
    subprocess.run([sys.executable, os.path.join(ROOT, "cctop.py"), "backfill"], env=env,
                   check=True, capture_output=True)
    return env, sleepers


# ---------------------------------------------------------------- terminal to image

def palette():
    """256-colour index -> hex: the theme's own colours where cctop mapped them, else xterm."""
    table = {}
    steps = [0, 95, 135, 175, 215, 255]
    for i in range(16, 232):
        n = i - 16
        table[i] = "#%02x%02x%02x" % (steps[n // 36], steps[n // 6 % 6], steps[n % 6])
    for i in range(232, 256):
        v = 8 + (i - 232) * 10
        table[i] = "#%02x%02x%02x" % (v, v, v)
    for name, hexv in list(cctop.PALETTE.items()) + list(cctop.heat_colors().items()):
        r, g, b = (int(hexv[j:j + 2], 16) for j in (1, 3, 5))
        table.setdefault(("theme", cctop.nearest_256(r, g, b)), hexv)
    return table


PAL = palette()
FG, BG = cctop.PALETTE["fg"], cctop.PALETTE["bg"]


def colour(i):
    return PAL.get(("theme", i)) or PAL.get(i) or FG


def parse(ansi):
    """tmux capture-pane -e output -> rows of cells (char, fg, bg, bold, italic)."""
    rows = []
    fg, bg, bold, italic = FG, BG, False, False
    for line in ansi.split("\n")[:ROWS]:
        cells = []
        for esc, ch in re.findall(r"\x1b\[([0-9;:]*)m|(.)", line):
            if ch:
                cells.append((ch, fg, bg, bold, italic))
                continue
            codes = [int(c) if c else 0 for c in re.split("[;:]", esc)] or [0]
            i = 0
            while i < len(codes):
                c = codes[i]
                if c == 0:
                    fg, bg, bold, italic = FG, BG, False, False
                elif c == 1:
                    bold = True
                elif c == 3:
                    italic = True
                elif c == 22:
                    bold = False
                elif c == 23:
                    italic = False
                elif c == 39:
                    fg = FG
                elif c == 49:
                    bg = BG
                elif c in (38, 48) and i + 2 < len(codes) and codes[i + 1] == 5:
                    if c == 38:
                        fg = colour(codes[i + 2])
                    else:
                        bg = colour(codes[i + 2])
                    i += 2
                elif 30 <= c <= 37:
                    fg = colour(c - 30)
                i += 1
        rows.append(cells)
    return rows


BLOCKS = {"█": (0, 1), "▀": (0, 0.5), "▄": (0.5, 1)}
EIGHTHS = {ch: (8 - n) / 8 for n, ch in enumerate(" ▁▂▃▄▅▆▇█") if n}
BOX = {"─": "h", "━": "H", "│": "v", "┌": "dr", "┐": "dl", "└": "ur", "┘": "ul", "├": "vr", "┤": "vl"}


def svg(rows):
    w, h = COLS * CW, ROWS * CH
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">',
           f'<rect width="100%" height="100%" fill="{BG}"/>',
           f'<g font-family="{FONT}" font-size="{FONT_PX}">']
    for r, cells in enumerate(rows):
        y0 = r * CH
        run = []

        def flush():
            if not run:
                return
            _, fg, _, bold, italic = run[0][1]
            xs = " ".join(f"{c * CW:.1f}" for c, _ in run)
            text = "".join(cell[0] for _, cell in run).replace("&", "&amp;").replace("<", "&lt;")
            weight = ' font-weight="bold"' if bold else ""
            style = ' font-style="italic"' if italic else ""
            out.append(f'<text x="{xs}" y="{y0 + 15}" fill="{fg}"{weight}{style} xml:space="preserve">{text}</text>')
            run.clear()

        for c, cell in enumerate(cells):
            ch, fg, bg, bold, italic = cell
            x0 = c * CW
            if bg != BG:
                out.append(f'<rect x="{x0:.1f}" y="{y0}" width="{CW + 0.5:.1f}" height="{CH}" fill="{bg}"/>')
            if ch in BLOCKS:
                flush()
                a, b = BLOCKS[ch]
                out.append(f'<rect x="{x0:.1f}" y="{y0 + a * CH:.1f}" width="{CW + 0.5:.1f}" height="{(b - a) * CH + 0.5:.1f}" fill="{fg}"/>')
            elif ch in EIGHTHS:
                flush()
                top = EIGHTHS[ch]
                out.append(f'<rect x="{x0:.1f}" y="{y0 + top * CH:.1f}" width="{CW + 0.5:.1f}" height="{(1 - top) * CH + 0.5:.1f}" fill="{fg}"/>')
            elif ch in BOX:
                flush()
                kind, mx, my = BOX[ch], x0 + CW / 2, y0 + CH / 2
                sw = 2 if kind == "H" else 1
                seg = {"h": [(x0, my, x0 + CW, my)], "H": [(x0, my, x0 + CW, my)], "v": [(mx, y0, mx, y0 + CH)],
                       "dr": [(mx, my, x0 + CW, my), (mx, my, mx, y0 + CH)], "dl": [(x0, my, mx, my), (mx, my, mx, y0 + CH)],
                       "ur": [(mx, y0, mx, my), (mx, my, x0 + CW, my)], "ul": [(mx, y0, mx, my), (x0, my, mx, my)],
                       "vr": [(mx, y0, mx, y0 + CH), (mx, my, x0 + CW, my)], "vl": [(mx, y0, mx, y0 + CH), (x0, my, mx, my)]}[kind]
                for x1, y1, x2, y2 in seg:
                    out.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{fg}" stroke-width="{sw}"/>')
            elif ch == " ":
                flush()
            else:
                if run and run[0][1][1:] != cell[1:]:
                    flush()
                run.append((c, cell))
        flush()
    out.append("</g></svg>")
    return "\n".join(out)


def render(ansi, png, zoom=1.0):
    path = png[:-4] + ".svg"
    with open(path, "w") as f:
        f.write(svg(parse(ansi)))
    subprocess.run(["rsvg-convert", "--zoom", str(zoom), "-o", png, path], check=True)
    os.remove(path)


# ---------------------------------------------------------------- recording

def tmux(*args):
    return subprocess.run(["tmux", *args], check=True, capture_output=True, text=True).stdout


def capture():
    return tmux("capture-pane", "-e", "-p", "-t", SESSION)


def main():
    for tool in ("tmux", "rsvg-convert", "ffmpeg"):
        if not shutil.which(tool):
            sys.exit(f"needs {tool}")
    os.makedirs(OUT, exist_ok=True)
    home = tempfile.mkdtemp(prefix="cctop-demo-")
    frames = tempfile.mkdtemp(prefix="cctop-frames-")
    sleepers = []
    try:
        env, sleepers = make_home(home)
        envs = " ".join(f"{k}={v}" for k, v in (("HOME", home), ("CCTOP_THEME", "github")))
        tmux("new-session", "-d", "-s", SESSION, "-x", str(COLS), "-y", str(ROWS),
             f"env -u CLAUDE_CONFIG_DIR -u CCTOP_PROJECTS {envs} {sys.executable} {ROOT}/cctop.py")

        # the Overview animation: greeting, the logo rising, one shimmer
        fps, seconds = 15, 14.5
        start = time.monotonic()
        shots = []
        while time.monotonic() - start < seconds:
            shots.append(capture())
            time.sleep(max(0.0, start + len(shots) / fps - time.monotonic()))
        # still screenshots of every tab, between two shimmers of the logo
        time.sleep(max(0.0, start + 17 - time.monotonic()))
        stills = [("overview", capture())]
        for key, name, extra in (("2", "projects", ()), ("3", "live", ()), ("4", "usage", ("Left",)), ("5", "system", ())):
            tmux("send-keys", "-t", SESSION, key)
            for k in extra:
                time.sleep(0.3)
                tmux("send-keys", "-t", SESSION, k)
            time.sleep(1.6)
            stills.append((name, capture()))
        for name, ansi in stills:
            render(ansi, os.path.join(OUT, f"{name}.png"), 2)  # 2x for sharp text
        # start on the first frame cctop has drawn, so a paused GIF or its first frame is not blank
        while shots and "Overview" not in shots[0]:
            shots.pop(0)
        for i, ansi in enumerate(shots):
            render(ansi, os.path.join(frames, f"f{i:04d}.png"))
        palette_filter = "split[a][b];[a]palettegen=max_colors=64:stats_mode=full[p];[b][p]paletteuse=dither=none"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(fps), "-i",
                        os.path.join(frames, "f%04d.png"), "-vf", palette_filter, "-loop", "0",
                        os.path.join(OUT, "overview.gif")], check=True)

        print("wrote", ", ".join(sorted(os.listdir(OUT))), "to", OUT)
    finally:
        subprocess.run(["tmux", "kill-session", "-t", SESSION], capture_output=True)
        for p in sleepers:
            p.kill()
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(frames, ignore_errors=True)


if __name__ == "__main__":
    main()
