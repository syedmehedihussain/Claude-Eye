# Project notes

The Projects tab turns `~/Projects` into a list of what you are building with Claude Code,
where you left off, and what comes next. It has two parts: a hook that records every
session automatically, and instructions that make Claude keep the written docs current.

## Layout

Every folder in `~/Projects/` is a project. Its notes live in `<project>/_project/`:

| File | Written by | Purpose |
|------|------------|---------|
| `STATUS.md` | Claude | Frontmatter plus "Where we left off" and "Next steps" |
| `prd.md`, `drd.md`, `proposal.md`, `decisions.md`, `notes.md` | Claude | Project documents, when they exist |
| `sessions.json` | `cctop hook` | One entry per Claude Code session that worked on the project |
| `log.md` | `cctop hook` | The same log, readable in an editor |

A session counts for a project when it ran inside the project folder or edited files in it.
Code that lives elsewhere can be linked with `paths:` in the STATUS.md frontmatter.
Sessions started by other programs through the Agent SDK are skipped.

In git repositories `_project/` is added to `.git/info/exclude`, so notes are never committed.
The repository's tracked `.gitignore` is not touched.

cctop only reads these files. Deleting a project folder in your file manager removes it from the tab.

## STATUS.md format

```markdown
---
name: my-app
status: active          # active | paused | done | idea
summary: One line describing the project
paths: [~/Projects/my-app, ~/.local/share/my-app]
updated: 2026-10-07
---

## Where we left off

Two to five sentences on what was just done and the current state.

## Next steps

- [ ] The next concrete step
- [x] Something already done
```

## 1. The hook

Add to `~/.claude/settings.json` (merge with any hooks you already have):

```json
{
  "hooks": {
    "Stop": [
      { "hooks": [{ "type": "command", "command": "python3 /path/to/cctop.py hook 2>/dev/null || true", "timeout": 15 }] }
    ],
    "SessionEnd": [
      { "hooks": [{ "type": "command", "command": "python3 /path/to/cctop.py hook 2>/dev/null || true", "timeout": 15 }] }
    ]
  }
}
```

The hook reads the session transcript Claude Code passes on stdin, takes about 0.1 s and
always exits 0, so it can never interrupt a session. Open `/hooks` once or restart Claude Code
after adding it.

To record sessions that happened before the hook existed:

```sh
python3 cctop.py backfill
```

## 2. Instructions for Claude

Add something like this to `~/.claude/CLAUDE.md` so every session maintains the docs:

```markdown
# Project notes

Every project lives in `~/Projects/<name>/` and keeps its notes in `~/Projects/<name>/_project/`.

When a session works on a project (not a one-off fix):

1. Find the project in `~/Projects/`, or create `~/Projects/<name>/` even if the code lives elsewhere.
2. Make sure `_project/STATUS.md` exists (frontmatter: name, status, summary, paths, updated;
   sections "Where we left off" and "Next steps").
3. Update STATUS.md when a meaningful piece of work is finished and before ending a task.
4. Save project documents there when they come up: proposal.md, prd.md, drd.md, decisions.md, notes.md.
5. Never delete anything in `_project/`. Do not edit sessions.json or log.md; the hook writes those.
6. Never commit `_project/`.
```

The session log is reliable because code writes it. STATUS.md depends on Claude following
the instructions, so if one looks stale, ask Claude to update the project status.

## Settings

| Variable | Default | Meaning |
|----------|---------|---------|
| `CCTOP_PROJECTS` | `~/Projects` | Where projects live |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | Claude Code's config and transcripts |
