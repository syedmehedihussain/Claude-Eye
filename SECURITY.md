# Security policy

## Supported versions

Security fixes go into the latest release only. Please update to the newest version on the
[Releases page](https://github.com/syedmehedihussain/Claude-Eye/releases) before you report.

| Version | Supported |
|---------|-----------|
| Latest release | Yes |
| Older releases | No |

## What to report

cctop reads sensitive local data, so these areas matter most:

- the Claude Code OAuth token, which cctop reads from `~/.claude/.credentials.json` and sends
  only to `api.anthropic.com`
- files cctop or its hook write, and the folders they write to
- commands cctop runs (`git`, `xdg-open`) and the arguments it passes to them
- terminal output built from transcript content, such as prompts and file names

## How to report

Report a vulnerability privately through GitHub:
[Report a vulnerability](https://github.com/syedmehedihussain/Claude-Eye/security/advisories/new).

Please do not open a public issue, and do not include real tokens or transcripts. Describe the
steps to reproduce the problem and what an attacker could do with it.

cctop is maintained by one person in their spare time. You can expect an acknowledgement within
a week. Once a fix is released, the advisory is published, with credit to you if you want it.
