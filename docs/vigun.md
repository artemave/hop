# vigun integration

`vigun` should use `hop run` as a routing primitive, not as a subprocess wrapper.

## Stable call

```bash
hop run --role test "<command>"
```

Examples:

```bash
hop run --role test "python3 -m pytest tests/test_run_commands.py -q"
hop run --role test "bun test smoke.test.ts"
```

## Contract

- `hop` resolves the session from the caller's current working directory using the nearest project marker (`.git`, `.dust`, or `pyproject.toml`).
- `hop` switches to the session workspace `p:<session>`.
- `hop` targets the Kitty terminal whose role is `test`.
- If the `test` terminal does not exist yet, `hop` creates it as a session-scoped Kitty OS window and keeps the current focus while doing so.
- `hop` sends the exact `<command>` string to that terminal and appends a newline if the caller did not include one.
- `hop` exits after routing succeeds. It does not wait for the test command to finish and it does not return the test command's exit status.

## Caller requirements

- Invoke `hop run --role test` from somewhere inside the target project tree.
- Pass the full test runner command as one CLI argument. Shell callers must quote it.
- Treat role selection as fixed: `vigun` should use the literal `test` role for test execution.
- Read completion and failures from the terminal/session workflow, not from `hop`'s process exit code.

## Remaining vigun work

- Replace exploratory terminal selection with a direct `hop run --role test "<command>"` call.
- Ensure the generated test command is passed as one argv item or one quoted shell string.
- Update result handling so `vigun` treats `hop run` as asynchronous command dispatch.
- Use `hop term --role test` separately when `vigun` needs to focus the test terminal before or after dispatch.
