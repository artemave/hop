---
name: hop-run
description: >-
  When the `HOP_SESSION` environment variable is set you are inside a hop
  session: route one-shot commands through `hop run --wait` instead of your own
  shell tool so the user watches them run. Test runs go to the session's `test`
  window; every other command (lint, typecheck, build, git, package managers,
  scripts) goes to the `shell` window. Never for servers, watch modes, or REPLs.
---

# Running commands in the hop session

This applies **only when the `HOP_SESSION` environment variable is set**. If it
is unset, you are not in a hop session — ignore this skill and use your normal
shell tool.

When `HOP_SESSION` is set, stop using your own shell tool for one-shot commands.
Send them to a session window with `hop run --wait`, and pick the window by what
the command *is*:

- **A test run** → `hop run --wait --role test "<command>"`
  (`pytest`, `jest`, `vitest`, `go test`, `rspec`, `make test`, a single test
  file or case, ...)
- **Anything else that exits on its own** → `hop run --wait --role shell "<command>"`
  (linters, type checkers, builds, `git`, `npm` / `bun` / `uv`, migrations,
  one-off scripts)

Only an actual test run goes to `test`. A `git` command, a linter, or a build
goes to `shell` even when you are running it as part of testing something.

`hop run --wait` types the command into that window, blocks until it returns to
the prompt, prints the combined output, and exits with the command's own status.
Use the exit status for pass/fail and the output for detail.

Rules:

- One `hop run --wait` at a time — wait for it to return before sending the
  next. Each window runs one command at a time.
- Never start servers, `--watch` loops, REPLs, or anything that does not exit on
  its own this way. Run those the normal way, or ask the user.
- The `shell` window may also be in use by the user — keep commands there short
  and non-destructive.
