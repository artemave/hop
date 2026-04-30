# Translate localhost URLs from container sessions

Translate `localhost`/`127.0.0.1`/`0.0.0.0` URLs from a container-backend session into a hostname/port the host's browser can actually reach.

## Background

The kitten in `kittens/open_selection/main.py` matches URLs in visible terminal output and dispatches them through `hop.commands.open_selection.open_selection_in_window`. For a container backend the source terminal lives inside the container's network namespace, so a printed `http://localhost:3000` (or `http://0.0.0.0:3000`, which dev servers love printing) refers to a container-local port. The browser runs on the host — handing it `localhost:3000` either lands on the wrong service or hits a dead port. Compose publishes the container's port on a different (often random) host port, and that's the value the browser needs.

`hop` already has the right shape for translation: per-backend command-list templates with discovery commands like `workspace`, plus a backend method that the rest of the codebase calls into. Cwd translation lives at `hop/backends.py::CommandBackend.translate_terminal_cwd`; this task adds the URL-translation equivalent alongside it. The change is fully scoped to the kitten dispatch path; `hop browser <url>` invoked directly from the host doesn't go through this pipeline and is intentionally left alone.

The abstraction is intentionally URL→URL (not port→port) so a future ssh backend can fit behind the same method without changing the call site — open-ports ssh needs a hostname swap, tunneled ssh needs both a hostname and a chosen local port, container needs a port swap. All three are the same shape.

## Design

### Backend method: `translate_localhost_url`

Add to the `SessionBackend` Protocol in `hop/backends.py`:

```python
def translate_localhost_url(self, session: ProjectSession, url: str) -> str: ...
```

Behavior:

- `HostBackend.translate_localhost_url`: identity (return `url` unchanged).
- `CommandBackend.translate_localhost_url`:
  1. Parse `url` with `urllib.parse.urlsplit`.
  2. If hostname is **not** in `{"localhost", "127.0.0.1", "0.0.0.0"}`, return `url` unchanged.
  3. If `host_translate_command` is set, substitute placeholders, run via `self.runner(args, session.project_root)`, take stripped stdout as the new hostname.
  4. If `port_translate_command` is set, substitute placeholders (with `{port}` taken from the original URL — empty string when the URL has no port), run via `self.runner(args, session.project_root)`, take stripped stdout as the new port.
  5. Rebuild and return the URL with `urlunsplit`, keeping scheme / userinfo / path / query / fragment intact and replacing only host and/or port with the translated values.
  6. If neither command is configured, return `url` unchanged.
  7. Empty stripped stdout from either command → `SessionBackendError`. Non-zero exit from either → `SessionBackendError` with the command's stderr/stdout, mirroring `discover_workspace`.

Both commands run when both are configured (covers a "container on a remote host over ssh" composition). The order is host-then-port so the port translation can in principle key off the rewritten host if a future backend wants that — but for the recipes this task ships, the two are independent.

### New backend command-list fields: `port_translate`, `host_translate`

Add two optional command lists to backend configs. Both are independently optional; the absence of one does not affect the other.

- `port_translate` — stripped stdout is the translated port (a number string, e.g. `"35231"`, or empty for failure). Substitution placeholders inside the argv: `{port}`, `{project_root}`.
- `host_translate` — stripped stdout is the translated hostname (e.g. `"myserver.example.com"`). Substitution placeholders inside the argv: `{project_root}`.

`{port}` is substituted with the port from the original URL, or the empty string when the URL has no explicit port.

Example (devcontainer with podman-compose):

```toml
[backends.devcontainer]
port_translate = [
  "sh", "-c",
  "podman ps -q --filter label=io.podman.compose.project=$(basename {project_root}) --filter label=io.podman.compose.service=devcontainer | head -1 | xargs -r -I{} podman port {} {port} | cut -d: -f2",
]
```

A bare `host_translate` recipe for a hypothetical "ssh to a known host" backend:

```toml
host_translate = ["echo", "myserver.example.com"]
```

### Wire into the dispatch path

In `hop/commands/open_selection.py::open_selection_in_window`, after resolving the target and before `browser.ensure_browser(...)`, run the URL branch through the backend:

```python
if isinstance(resolved_target, ResolvedUrlTarget):
    translated_url = backend.translate_localhost_url(session, resolved_target.url)
    logger.info("dispatching url %r to session %r", translated_url, session_name)
    browser.ensure_browser(session, url=translated_url)
```

The existing dispatch log line should report the translated URL, not the original, so debugging from `open-selection.log` reflects what the browser actually got.

### Persist `port_translate` and `host_translate` in session state

`CommandBackendRecord` in `hop/state.py` already persists the resolved command lists (`shell`, `editor`, `prepare`, `teardown`, `workspace_command`). Add:

- `port_translate_command: tuple[str, ...] | None = None`
- `host_translate_command: tuple[str, ...] | None = None`

…to:

- `CommandBackendRecord` (with matching `to_json` / decode in `_decode_backend_record`),
- `CommandBackend` dataclass + `with_workspace_path` copy + `backend_from_config`,
- the round-trip in `hop/app.py::_backend_from_record` / `_record_for_backend`.

Unlike `workspace_path`, there is no value to discover at session bootstrap — the translate commands run lazily per URL with the URL components substituted in. So no `discover_*` method, no extra step in the bootstrap pipeline.

### Config plumbing

In `hop/config.py`:

- Add `port_translate: tuple[str, ...] | None = None` and `host_translate: tuple[str, ...] | None = None` to `BackendConfig`.
- Add `"port_translate"` and `"host_translate"` to `_BACKEND_FIELDS` and the `_parse_backend` call.
- Add both fields to `_merge_pair` (project field wins over same-named global field, independently per field).

In `hop/backends.py::backend_from_config`, pass both through to `CommandBackend`.

## Files to change

- `hop/config.py` — two new fields, parser, merge.
- `hop/backends.py` — two new fields on `CommandBackend`; `{port}` substitution path for the translate argvs; new method on `SessionBackend` / `HostBackend` / `CommandBackend`; plumb both through `backend_from_config` / `with_workspace_path`.
- `hop/state.py` — persist `port_translate_command` and `host_translate_command` on `CommandBackendRecord`.
- `hop/app.py` — round-trip both fields in `_backend_from_record` / `_record_for_backend`.
- `hop/commands/open_selection.py` — call `translate_localhost_url` on the resolved URL before dispatch; update the dispatch log line.
- `hop_spec.md` — document `port_translate` and `host_translate` in the backend section (alongside `workspace`) and add localhost-URL translation to the kitten dispatch description, parallel to the existing cwd-translation paragraph.
- `docs/devcontainer.md` — document `port_translate` in the global config example, the verify section, and the troubleshooting list (analogous to the current "Open-selection kitten can't open files" entry). `host_translate` gets a short note that it exists and is for hostname-swap backends, without inventing a sample backend type beyond the devcontainer one this doc is about.
- `README.md` — add `port_translate` and `host_translate` to the "Fields per backend" list in the Global config section.

## Tests

Real subprocesses where possible (no mocks per project convention). Patterns to follow:

- For `CommandBackend.translate_localhost_url`: drive it via the existing `CommandRunner` injection point — pass a fake runner that records args and returns a synthesized `subprocess.CompletedProcess`. Same pattern `tests/test_backends.py` uses for `discover_workspace` / `prepare` / `teardown`. Cover:
  - non-localhost URL (e.g. `https://example.com`) → returned unchanged, runner not invoked,
  - both commands `None` → returned unchanged, runner not invoked,
  - `localhost` URL with port + only `port_translate` configured + stdout `"35231"` → URL port replaced; host unchanged; path/query/fragment preserved,
  - `127.0.0.1` URL treated the same as `localhost`,
  - `0.0.0.0` URL treated the same as `localhost`,
  - `localhost` URL with port + only `host_translate` configured + stdout `"myserver"` → URL host replaced; port unchanged,
  - both commands configured → both runners invoked; both replacements applied,
  - `localhost` URL **without** port + `port_translate` configured → command is still invoked with `{port}` substituted as empty string,
  - `{port}` and `{project_root}` placeholders substituted into the argv passed to the runner for `port_translate`; `{project_root}` substituted for `host_translate`,
  - non-zero exit from either → `SessionBackendError` with stderr in the message,
  - empty stdout from either → `SessionBackendError`.
- For `HostBackend.translate_localhost_url`: identity, smoke test.
- For `open_selection_in_window`: extend `tests/test_open_selection_commands.py` with a `FakeBackend` that overrides `translate_localhost_url` to return a fixed translated URL, assert `StubBrowserAdapter` received the translated value (mirrors the existing `test_open_selection_in_window_translates_terminal_cwd_via_base` shape).
- For config parsing in `tests/test_config.py`: round-trip both `port_translate` and `host_translate`, plus the merge precedence (project > global) per-field, plus rejection of a non-list value for either.
- For state round-trip in `tests/test_state.py`: assert `port_translate_command` and `host_translate_command` survive `to_json` → `load_sessions`.

## Out of scope

- Discovering ports at session bootstrap or caching translated URLs across kitten dispatches. Each click runs the configured commands once. If this becomes a measurable latency problem, caching can be added later.
- Translating non-`localhost` / non-`127.0.0.1` / non-`0.0.0.0` URLs (e.g. service names, container IPs). The matcher already only picks up things that look like URLs, and host-network-namespace addresses other than the three sentinels aren't reliably reachable from the host.
- An ssh backend. The `translate_localhost_url` *method* and the two-knob config (`host_translate` + `port_translate`) are shaped to fit a future ssh implementation (open-ports → host_translate; tunneled → port_translate with stateful tunnel setup behind it), but neither lands in this task.
- Changing how `hop browser <url>` (host CLI) works. That command isn't dispatched from inside the container — the URL is already in the host's network namespace.

## Task Type

implement

## Principles

- [Session-oriented workspaces](../principles/session-oriented-workspaces.md)
- [Keep the spec aligned](../principles/keep-the-spec-aligned.md)

## Blocked By

(none)

## Definition of Done

- `port_translate` and `host_translate` are parsed from global and project hop configs and merged correctly (project field wins over same-named global field, independently per field).
- `BackendConfig`, `CommandBackend`, `CommandBackendRecord`, and the `_record_for_backend` / `_backend_from_record` round-trip in `hop/app.py` all carry both command lists.
- `SessionBackend` Protocol exposes `translate_localhost_url(session, url)`. `HostBackend` returns the URL unchanged. `CommandBackend` rewrites only URLs whose host is `localhost`, `127.0.0.1`, or `0.0.0.0`, applies `host_translate` (if set) to the host and `port_translate` (if set) to the port, and substitutes the result back into the URL preserving scheme / userinfo / path / query / fragment.
- `{port}` and `{project_root}` placeholders are substituted into the `port_translate` argv at call time (with `{port}` as empty string when the URL has no port). `{project_root}` is substituted into the `host_translate` argv. Other existing placeholders continue to work where applicable.
- Failure modes (non-zero exit, empty stdout) on either command raise `SessionBackendError` with a useful message; both commands are invoked through the existing `CommandRunner` injection point.
- The kitten dispatch path in `open_selection_in_window` calls `translate_localhost_url` on resolved URL targets before `browser.ensure_browser`, and the dispatch log line shows the translated URL.
- New unit tests cover the cases listed in the Tests section, follow the existing no-mock conventions in this repo, and pass under `uv run pytest -q`.
- `hop_spec.md` documents `port_translate` and `host_translate` alongside `workspace` and notes localhost-URL translation in the kitten dispatch description.
- `docs/devcontainer.md` and `README.md` are updated as listed in the Files-to-change section.
- `bunx dust lint` passes for the task file.
