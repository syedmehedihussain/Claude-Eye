# cctop

A terminal dashboard for Claude Code on Linux: plan limits, token usage, live sessions,
the projects you are working on, and the health of the machine underneath.

Single Python file, standard library only, nothing to install beyond Python 3.9+.

## Tabs

| Key | Tab | What it shows |
|-----|-----|---------------|
| `1` | Overview | 5-hour and weekly limit bars (what is left, turning red when low), current session, repo and GitHub status, a one-line system strip |
| `2` | Projects | Every project in `~/Projects`: status, where you left off, next steps, docs and recent sessions |
| `3` | Live | Everything about each running session: activity feed, tokens, tools, files touched, agents, skills, MCP servers, process stats, prompt history |
| `4` | Usage | Tokens by hour, last 7 days, models and projects |
| `5` | System | CPU, memory, swap, disk, battery, temperatures, fan, load, short history graphs |

## Install

```sh
git clone https://github.com/syedmehedihussain/Claude-Eye.git
cd Claude-Eye
./install.sh        # links cctop into ~/.local/bin
cctop
```

`./uninstall.sh` removes the link and the cache.

## Keys

| Key | Action |
|-----|--------|
| `Tab` / `Shift+Tab`, `1` to `5` | Switch tabs |
| `t` | Usage: all tokens, input + output, or output only |
| `←` `→` | Usage: previous or next day. Live: previous or next session |
| `↑` `↓` | Projects: select a project |
| `o` | Projects: open the project folder in your file manager |
| `r` | Reload everything |
| `q` | Quit |

The minimum terminal size is 80x24. Larger terminals show more.

## Where the data comes from

Everything is read locally unless noted.

- **Token usage and sessions:** Claude Code's transcripts in `~/.claude/projects/**/*.jsonl`,
  read incrementally, so only new lines are parsed on each refresh.
- **Live sessions:** `~/.claude/sessions/*.json` plus `/proc` for process stats.
- **Plan limits:** the endpoint Claude Code's own `/usage` command uses,
  `https://api.anthropic.com/api/oauth/usage`, called with the OAuth token Claude Code already
  stores in `~/.claude/.credentials.json`. The token is only ever sent to `api.anthropic.com`.
  Results are cached in `~/.cache/cctop/limits.json` and shared by every cctop window: at most
  one request every 5 minutes, with a 15-minute backoff when rate limited. The endpoint is not
  documented, so if it changes the panel shows "limits unavailable" and everything else keeps working.
- **Git and GitHub:** `git` in the session's directory and the user name in `~/.config/gh/hosts.yml`.
- **System:** `/proc` and `/sys` (hwmon sensors, power supply).

## Project notes

The Projects tab is fed by a small convention: each project keeps notes in
`~/Projects/<name>/_project/`, a Claude Code hook (`cctop hook`) records every session there
automatically, and a few lines in `~/.claude/CLAUDE.md` make Claude keep `STATUS.md` and
documents like a PRD or decision log up to date. Notes are kept out of git.

Setup and file formats: [docs/project-notes.md](docs/project-notes.md).

## Colors

The palette comes from the Last Horizon design system. On terminals that allow redefining
colors, cctop sets the exact values and restores them on exit; otherwise it picks the nearest
256-color match, and falls back to the 8 basic colors.

## Limits of the numbers

- "All tokens" is mostly cache reads, which are cheap and count far less toward plan limits.
  Press `t` for input + output.
- Lines added and removed are estimated from Claude's edits as they happen.
- Running agents are counted from subagent transcripts written in the last 90 seconds and
  foreground agent calls still in progress.

## License

MIT
