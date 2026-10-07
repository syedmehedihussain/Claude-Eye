# Contributing to cctop

Thank you for helping. cctop is a small hobby project, so the process is light. This guide
explains how to report a problem, suggest a change and send a pull request.

By taking part you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Report a bug

Open an issue with the **Bug report** form. Please include:

- the cctop version (shown at the top of the window, for example `cctop-0.3.0`)
- your Linux distribution, terminal and its size, and `python3 --version`
- what you did, what you expected and what happened instead
- a screenshot if the problem is visual

Do not paste Claude Code transcripts, prompts, tokens or `~/.claude/.credentials.json` into an
issue. If the bug needs data to reproduce, describe its shape instead.

Security problems go through the private process in [SECURITY.md](SECURITY.md), not an issue.

## Suggest a feature

Open an issue with the **Feature request** form and describe the problem you want to solve
before the solution. cctop stays a single file with no dependencies, so ideas that need a
third-party package are unlikely to fit.

## Send a pull request

1. Fork the repository and create a branch from `main`.
2. Make your change. For anything larger than a small fix, open an issue first so we can agree
   on the approach.
3. Test it by hand (see below).
4. Add a line to the `Unreleased` section of [CHANGELOG.md](CHANGELOG.md), creating the
   section if needed.
5. Open the pull request and fill in the template.

### Ground rules for code

- **One file, standard library only.** `cctop.py` must run with Python 3.9 or newer and
  nothing else installed.
- **Read-only by default.** cctop only reads Claude Code's data. It writes to
  `~/.cache/cctop`, and the hook writes to `_project/` folders and `.git/info/exclude`. Do not
  add other writes.
- **The OAuth token goes to `api.anthropic.com` only.** Never log it, print it or save it.
- **Match the surrounding code.** Same naming, same comment style, same density.
- **Keep motion optional.** New animation must respect `CCTOP_MOTION=off` and stay short.

### Test by hand

There is no automated test suite yet. Before you open a pull request, check that:

- `python3 -m py_compile cctop.py` passes
- every tab draws without errors at 80 x 24 and at a larger size
- the keys for each tab still work
- `CCTOP_MOTION=off cctop` shows no animation
- `echo '{}' | python3 cctop.py hook` exits quietly, if you changed the hook

If your change affects what the README screenshots show, record them again:

```sh
python3 tools/screenshots.py
```

This uses made-up demo data only; see the script's header for what it needs.

### Commit messages

Write the subject in the imperative and keep it short, for example
`Show the push time from the reflog`. Explain the reason in the body when it is not obvious.

## Releases

The maintainer tags releases as `vX.Y.Z`, following
[Semantic Versioning](https://semver.org), and publishes them on the Releases page with the
matching CHANGELOG section. The version in `cctop.py` (`VERSION`) matches the tag.
