# cctop

A terminal dashboard for Claude Code on Linux: plan limits, token usage, live sessions,
the projects you are working on, and the health of the machine underneath.

Single Python file, standard library only, nothing to install beyond Python 3.9+.

## Tabs

| Key | Tab | What it shows |
|-----|-----|---------------|
| `1` | Overview | The logo and three quiet sections: limits (5h and weekly percent used, reset times, a time track and where the current pace ends up), live sessions by project, and a one-line system summary |
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

`./install.sh --skill` also installs the project-notes skill for Claude Code (see below).

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
- **Plan limits (shown as percent used):** the endpoint Claude Code's own `/usage` command uses,
  `https://api.anthropic.com/api/oauth/usage`, called with the OAuth token Claude Code already
  stores in `~/.claude/.credentials.json`. The token is only ever sent to `api.anthropic.com`.
  Results are cached in `~/.cache/cctop/limits.json` and shared by every cctop window: at most
  one request every 5 minutes, with a 15-minute backoff when rate limited. The endpoint is not
  documented, so if it changes the panel shows "limits unavailable" and everything else keeps working.
- **Git and GitHub:** `git` in the session's directory and the user name in `~/.config/gh/hosts.yml`.
- **System:** `/proc` and `/sys` (hwmon sensors, power supply).

## Project notes

The Projects tab is fed by a small convention: each project keeps notes in
`~/Projects/<name>/_project/`. A Claude Code hook (`cctop hook`) records every session there
automatically, and the `project-notes` skill in `skills/` teaches Claude to resume from the
notes and keep `STATUS.md` and documents like a proposal, PRD, DRD or decision log up to date,
with templates for each. Notes are kept out of git.

Setup and file formats: [docs/project-notes.md](docs/project-notes.md).

## Colors

The default theme is GitHub dark: a near-black background, green for good, red for
deletions and danger, amber for warnings and blue for accents. `CCTOP_THEME=horizon` switches
to the Last Horizon palette on the terminal's own background. On terminals that allow
redefining colors, cctop sets the exact values and restores them on exit; otherwise it picks
the nearest 256-color match, and falls back to the 8 basic colors.

Bars are drawn with the Nerd Font icon `md-square_rounded` (U+F14FB): rounded corners,
centred on the text, with a small gap. Without a Nerd Font it shows as a box; set another
two-column square instead, for example `CCTOP_SQUARE="■ " cctop`.

## Context reminders

Each session's context is its latest request size (input plus cache tokens) against the
model's window: 1M for current models, 200K for Haiku 4.5. At 40% the Overview shows a reminder
at the bottom and the Live tab's Session box shows it in red ("/clear before your next task");
at 70% it asks for `/clear` or `/compact` now. Change the thresholds with `CCTOP_CTX_WARN` and
`CCTOP_CTX_URGENT` (fractions, for example `0.5`).

## Limits of the numbers

- "All tokens" is mostly cache reads, which are cheap and count far less toward plan limits.
  Press `t` for input + output.
- Lines added and removed are estimated from Claude's edits as they happen.
- Running agents are counted from subagent transcripts written in the last 90 seconds and
  foreground agent calls still in progress.

## License

MIT
