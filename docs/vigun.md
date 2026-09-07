# vigun integration

`vigun` should use `hop run` as a routing primitive, not as a subprocess wrapper. `hop wait` is the optional second step that collects the result.

## Stable call

```bash
id=$(hop run --role test "<command>")
hop wait "$id"
```

Examples:

```bash
id=$(hop run --role test "python3 -m pytest tests/test_run_commands.py -q")
hop wait "$id"
```

## Contract

- `hop` resolves the session from the caller's current working directory — the resolved directory is the session root and its basename is the session name. There is no marker-file or ancestor search.
- `hop` switches to the session workspace `s:<session>`.
- `hop` targets the Kitty terminal whose role is `test`.
- If the `test` terminal does not exist yet, `hop` creates it as a session-scoped Kitty OS window and keeps the current focus while doing so.
- `hop` sends the exact `<command>` string to that terminal and appends a newline if the caller did not include one.
- `hop run` exits after routing succeeds, printing an opaque run id to stdout. It does not wait for the test command to finish.
- `hop wait <id>` blocks until that command returns to its prompt, writes its output to stdout, and exits with the command's own exit status — or `124` if it gives up first (10 minutes by default).

## Caller requirements

- Invoke `hop run --role test` from the session root itself (where `hop` was started), not a subdirectory — resolution is the exact working directory, so a nested path resolves to a different session.
- Pass the full test runner command as one CLI argument. Shell callers must quote it.
- Treat role selection as fixed: `vigun` should use the literal `test` role for test execution.
- Capture the run id from `hop run` stdout and pass it to `hop wait` to get the pass/fail result. `hop run`'s own exit status only reports whether routing succeeded.

## Remaining vigun work

- Replace exploratory terminal selection with a direct `hop run --role test "<command>"` call.
- Ensure the generated test command is passed as one argv item or one quoted shell string.
- Capture the `hop run` run id and read the result with `hop wait "$id"` (output on stdout, exit status is the command's).
- Use `hop term --role test` separately when `vigun` needs to focus the test terminal before or after dispatch.
