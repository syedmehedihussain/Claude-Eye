---
name: project-notes
description: >
  Keep a project's notes in ~/Projects/<name>/_project/ up to date: STATUS.md (where we
  left off, next steps) plus documents like a proposal, PRD, DRD or decision log. Use at
  the start of a session that continues or starts a project (read the notes to resume),
  after finishing a meaningful piece of work or before ending a task (update STATUS.md),
  and whenever a plan, requirement or decision should be written down. Triggers: "where
  did we leave off", "what was I working on", "continue the project", "update the
  project status", "write a PRD", "write a proposal", "design requirements", "log this
  decision", "start a new project", "save this to the project". Not for one-off fixes
  such as a config tweak or a crash investigation.
---

# Project notes

Every project lives in `~/Projects/<name>/` and keeps its notes in `~/Projects/<name>/_project/`.
These notes are how the user picks a project back up days later, and they feed the
Projects tab in cctop. Write them for that reader: someone returning cold who needs to know
what this is, what state it is in, and what to do next.

```
~/Projects/<name>/_project/
├ STATUS.md       you keep this current
├ proposal.md     why and what, before building           (when it comes up)
├ prd.md          product requirements                    (when it comes up)
├ drd.md          design requirements                     (when it comes up)
├ decisions.md    decision log, newest first              (when it comes up)
├ notes.md        anything else worth keeping             (when it comes up)
├ sessions.json   written by the cctop hook, never edit
└ log.md          written by the cctop hook, never edit
```

Templates for every file are in `templates/` next to this skill.

## 1. Find or create the project

- If the working directory is inside `~/Projects/<name>/`, that is the project.
- Otherwise match by what is being worked on: list `~/Projects/` and read each
  `_project/STATUS.md` `summary:` and `paths:`. Code may live outside `~/Projects`
  (for example `~/.local/share/<app>`); `paths:` links it.
- If nothing matches and this is a new project, pick a short kebab-case name (confirm it with
  the user if it is not obvious) and run:

  ```sh
  bash <this skill's folder>/scripts/ensure_project.sh <name> [extra code path ...]
  ```

  It creates `~/Projects/<name>/_project/` and a STATUS.md from the template, lists the extra
  paths in the frontmatter, and keeps `_project/` out of git. It never overwrites an existing file.
  Run it for existing projects too: it is the quickest way to make sure the git exclude is in place.

## 2. Resume: read before working

When continuing a project, read `STATUS.md` first, then the newest two or three entries of
`log.md` (the hook's record of past sessions: first and last prompt, recap, files changed).
Tell the user in a sentence or two where things stand and what the next step is, then continue.

## 3. Keep STATUS.md current

Update it when a meaningful piece of work is done and before ending a task. Do not update it
for every small edit.

- **Frontmatter:** `status` is `active`, `paused`, `done` or `idea`. Bump `updated:` to today.
  Keep `summary:` to one line. Add to `paths:` when code turns up somewhere new.
- **What we are building:** a short paragraph on what the project is and what it does once
  finished. The cctop Projects tab shows it. Rewrite it only when the idea itself changes.
- **Where we left off:** 2 to 5 sentences on what was just done and the current state, including
  anything half-finished or broken. Replace the previous text; history belongs in the log.
- **Next steps:** a short checklist (`- [ ]` / `- [x]`). Tick finished items, drop stale ones,
  put the most important first. Each item should be concrete enough to start on.

Write "What we are building" and "Where we left off" in ASD-STE100 Simplified Technical English:

- One topic per sentence. Keep sentences to 20 words or fewer.
- Use the active voice and simple tenses (present, past, future).
- Use simple, common words, and use one word for one meaning. Do not use slang or idioms.
- Do not stack more than three nouns together ("the notes of the project", not
  "project session note record").
- Use articles ("the", "a"). Use a list for three or more parallel items.
- Keep paragraphs to six sentences or fewer, with a blank line between them.

Edit in place with small edits. Never rewrite the file from scratch, never delete it.

## 4. Write documents when they come up

Create a document when the user asks for one, or when a session produces something worth
keeping (a plan the user approved, requirements agreed in conversation, a design choice with
trade-offs). Start from the matching template and fill it from the conversation; leave out
sections that have nothing real to say rather than padding them.

- `proposal.md`: the problem, the idea, scope, open questions. Written before building.
- `prd.md`: what the product must do: users, goals, requirements, non-goals, success criteria.
- `drd.md`: how it should look and behave: principles, layout, components, states, tokens.
- `decisions.md`: one entry per decision that would be costly to reverse or easy to forget.
  Newest first: date, decision, why, alternatives considered. Append; never rewrite old entries.
  If a decision is reversed, add a new entry that says so.
- `notes.md`: everything else worth keeping.

Mention new or updated documents in "Where we left off" so they are easy to find.

## Rules

- Never delete anything in `_project/`. If the user deletes a project folder, that is their call.
- Never edit `sessions.json` or `log.md`.
- Never commit `_project/`, and never add it to a tracked `.gitignore`; the exclude lives in
  `.git/info/exclude` (the script handles it).
- No emojis. Plain Markdown, short lines, dates as YYYY-MM-DD.
- Notes describe the project, not the conversation: no "the user said", no chat transcripts.
