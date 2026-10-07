# Changelog

All notable changes to cctop are listed here, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and version numbers follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.0] - 2026-10-07

### Added

- Cost estimates at Claude API prices: the day and the week on the Usage tab, a cost view on
  the `t` key that shows every chart in dollars, and the cost of each project on the Projects
  tab. Prices are per model, with separate rates for cache writes (5-minute and 1-hour) and
  cache reads. The hook saves each session's cost, so it stays after old transcripts are deleted.

## [0.3.0] - 2026-10-07

### Added

- Overview greeting for the time of day, in large pixel letters, with a small `cctop` label.
  After 5 seconds it changes into the cctop logo, which rises into place.
- Terminal text effects for the greeting (decrypt, beams, assemble, slide), and a colour wave
  that passes over the settled logo every 7 seconds.
- Small animations elsewhere: bars and the Usage chart grow in when a view opens, the commit
  calendar fills in from left to right, and new tool calls on the Live tab glow briefly.
- `CCTOP_MOTION=off` to turn off all animation.
- Projects tab: a "What we are building" box and an Activity box with commit and push counts
  and a commit calendar.
- A full lowercase alphabet for the pixel font.
- `tools/screenshots.py`, which records the README screenshots from made-up demo data.
- Open-source project files: contributing guide, code of conduct, security policy, issue and
  pull request templates.

### Changed

- The Projects tab reads in order: What we are building, Where we left off, Next steps, Activity.
- Long text in a box ends on a full line with an ellipsis, not on a blank line.
- The Usage tab groups tokens by each session's project, the same way the Overview does.
- The pace text on the Overview lines up under the reset time.
- Model names are shortened at the end, not the start.
- `CCTOP_CTX_WARN` and `CCTOP_CTX_URGENT` accept `40`, `40%` or `0.4`.
- The project-notes skill asks for "What we are building" and "Where we left off" in
  Simplified Technical English.
- A new README with screenshots, install steps for each version, and sections on
  configuration, accessibility and privacy.

### Fixed

- The Live tab showed the commit date as the push time. It now shows when the branch was pushed.
- Two sessions that ended at the same time could drop each other's entry in `sessions.json`.
  A broken `sessions.json` is now left alone instead of being overwritten.
- A stray file in `~/.claude/projects` crashed cctop at startup.
- An invalid `CCTOP_CTX_WARN` or `CCTOP_CTX_URGENT` value crashed cctop and the hook.
- Text that contained the word "yet" was dimmed as if it were a placeholder.

### Removed

- Unused Live tab panels and the process statistics that only they used.

## [0.2.0] - 2026-10-07

The first public version.

### Added

- Five tabs: Overview, Projects, Live, Usage and System.
- Plan limits for the 5-hour and weekly windows, read from the same endpoint as Claude Code's
  `/usage` command, cached and shared between windows.
- Live sessions with a project file tree, activity feed, current prompt, agents, skills, todos,
  git status and plan usage.
- Token usage by hour, by day, by model and by project.
- Context reminders at 40% and 70% of the model's context window.
- The project-notes convention: a Claude Code hook that records sessions into
  `~/Projects/<name>/_project/`, and a skill that keeps `STATUS.md` up to date.
- GitHub dark theme, with the Last Horizon theme as an option.
- `install.sh` and `uninstall.sh`.

[0.4.0]: https://github.com/syedmehedihussain/cctop/releases/tag/v0.4.0
[0.3.0]: https://github.com/syedmehedihussain/cctop/releases/tag/v0.3.0
