# Route binary files to a configurable system handler

Stop sending non-text files (`.png`, `.pdf`, `.mp4`, archives, ...) into nvim. Route them through a configurable per-extension handler — default `xdg-open` — so the OS picks the right viewer. Text files, source code, and anything not on a small built-in binary allowlist continue to go to the editor as today. Both `hop open` and the open-selection kitten flow through the same classifier.

## Background

Today `hop open <path>` and the kitten's path dispatch both end at `dispatch_resolved_target` (`hop/commands/open.py:36-55`):

```python
if isinstance(resolved, ResolvedUrlTarget):
    translated_url = backend.translate_localhost_url(session, resolved.url)
    browser.ensure_browser(session, url=translated_url)
    return ResolvedUrlTarget(url=translated_url)
neovim.open_target(session, target=resolved.editor_target, search=resolved.search)
return resolved
```

Two branches: URL → browser, everything else → nvim. There's no awareness that `path/email-logo.png` is a PNG. `_build_open_keystrokes` happily writes `:exec 'drop '.fnameescape('public/email-logo.png')\r` into nvim's pty and the user is staring at `<89>PNG^M ... IHDR ...` in a buffer.

The fix needs:

- An extension-based classifier so opening `foo.png` runs `xdg-open foo.png` instead of typing into nvim.
- A conservative built-in allowlist of *known binary* extensions only. Anything not on the list — including all source code, `.json`, `.yaml`, `.toml`, `.xml`, `.md`, `.txt`, `Makefile`, `Dockerfile`, files with no extension — continues to dispatch to nvim exactly as today.
- User override at both global and project config layers, mergeable per pattern.
- A single classifier shared by `hop open` and the kitten — the resolver layer is the right place; both already share `dispatch_resolved_target`.

### What stays in the editor

The classifier is an *allowlist of binary types*, not a "is this file probably text?" sniffer. Concretely:

| File                | Dispatches to | Why                                                       |
| ------------------- | ------------- | --------------------------------------------------------- |
| `config.json`       | nvim          | `.json` not in default handler set                        |
| `docker-compose.yaml` | nvim        | `.yaml` not in default handler set                        |
| `Cargo.toml`        | nvim          | `.toml` not in default handler set                        |
| `app/models/user.rb`| nvim          | `.rb` not in default handler set                          |
| `Makefile`, `Dockerfile` | nvim     | no extension, no default-handler match                    |
| `notes.md`, `README` | nvim         | `.md` / no extension                                      |
| `icon.svg`          | nvim          | Deliberately not on the binary allowlist — SVG is XML and users edit it |
| `logo.png`          | xdg-open      | known image binary                                        |
| `report.pdf`        | xdg-open      | known binary                                              |
| `archive.tar.gz`    | xdg-open      | known binary                                              |

If the user later wants `.svg` to open in an image viewer instead of nvim, they add `*.svg = "xdg-open {path}"` to their own config. Hop ships zero handlers for ambiguous-text formats.

## Design

### Config surface

A new top-level `[open_handlers]` table mapping glob patterns to command templates. The template gets `{path}` substituted with the resolved (post-`resolve_file_candidate`) absolute path, shell-quoted.

```toml
[open_handlers]
"*.pdf" = "zathura {path}"
"*.png" = "feh {path}"
```

Hop ships built-in defaults (mirroring how `_BUILTIN_HOST_BACKEND` is layered under user config in `_layer_builtin_backends`). Defaults are deliberately small and binary-only:

```python
_BUILTIN_OPEN_HANDLERS: dict[str, str] = {
    # Images
    "*.png":  "xdg-open {path}",
    "*.jpg":  "xdg-open {path}",
    "*.jpeg": "xdg-open {path}",
    "*.gif":  "xdg-open {path}",
    "*.webp": "xdg-open {path}",
    "*.bmp":  "xdg-open {path}",
    "*.tiff": "xdg-open {path}",
    "*.ico":  "xdg-open {path}",
    # Audio
    "*.mp3":  "xdg-open {path}",
    "*.wav":  "xdg-open {path}",
    "*.ogg":  "xdg-open {path}",
    "*.flac": "xdg-open {path}",
    "*.opus": "xdg-open {path}",
    "*.m4a":  "xdg-open {path}",
    # Video
    "*.mp4":  "xdg-open {path}",
    "*.mov":  "xdg-open {path}",
    "*.mkv":  "xdg-open {path}",
    "*.webm": "xdg-open {path}",
    "*.avi":  "xdg-open {path}",
    # Documents
    "*.pdf":  "xdg-open {path}",
    "*.docx": "xdg-open {path}",
    "*.xlsx": "xdg-open {path}",
    "*.pptx": "xdg-open {path}",
    "*.odt":  "xdg-open {path}",
    "*.ods":  "xdg-open {path}",
    "*.odp":  "xdg-open {path}",
    # Archives
    "*.zip":     "xdg-open {path}",
    "*.tar":     "xdg-open {path}",
    "*.tar.gz":  "xdg-open {path}",
    "*.tgz":     "xdg-open {path}",
    "*.tar.bz2": "xdg-open {path}",
    "*.7z":      "xdg-open {path}",
    "*.rar":     "xdg-open {path}",
    # Native binaries
    "*.exe":   "xdg-open {path}",
    "*.dll":   "xdg-open {path}",
    "*.so":    "xdg-open {path}",
    "*.dylib": "xdg-open {path}",
}
```

Explicitly **not** in defaults: `.svg`, `.json`, `.yaml`, `.toml`, `.xml`, `.html`, `.css`, `.md`, `.txt`, `.rs`, `.go`, `.py`, `.ts`, `.tsx`, `.rb`, `.js`, `Dockerfile`, `Makefile`, and every other source / config / text format. Anything not on the allowlist falls through to nvim. The principle: ship defaults only for formats that *cannot* be meaningfully edited in a text editor.

Users override per pattern: `*.pdf = "zathura {path}"`. Empty string disables a default: `*.png = ""` removes png from the handler set so it falls through to nvim. Per-pattern project-wins-per-field merge, same as the other config tables.

The merge surface piggybacks on the existing `merge_configs` plumbing (`hop/config.py:174-191`):

```python
HopConfig(
    backends=...,
    layouts=...,
    windows=...,
    open_handlers=merge_open_handlers(project, global_with_builtin),
    workspace_layout=...,
    debug_log=...,
)
```

`merge_open_handlers` is a per-pattern project-wins dict merge plus hop's built-in defaults underneath, analogous to `merge_backends`.

### Dispatch

`hop/commands/open.py:dispatch_resolved_target` grows a handler-lookup step between URL and nvim:

```python
def dispatch_resolved_target(resolved, *, session, backend, neovim, browser, handlers, runner):
    if isinstance(resolved, ResolvedUrlTarget):
        translated_url = backend.translate_localhost_url(session, resolved.url)
        browser.ensure_browser(session, url=translated_url)
        return ResolvedUrlTarget(url=translated_url)
    handler_command = match_handler(resolved.path, handlers)
    if handler_command is not None:
        runner.run(session, backend, handler_command, path=resolved.path)
        return resolved
    neovim.open_target(session, target=resolved.editor_target, search=resolved.search)
    return resolved
```

`match_handler` does `fnmatch.fnmatch(path.name, pattern)` against the resolved *file name*, not the typed token. So:

- Rails-resolved `app/controllers/users_controller.rb` → no `.rb` default → nvim.
- `path:42` (path-with-line) → strips the line suffix before matching; the suffix only ever applies in editor templates.
- A user-overridden `*.json = "fx {path}"` matches `app/config.json` and runs `fx`.

Empty-string templates are treated as "no handler" — fall through to nvim. That's how `*.png = ""` opts a default out.

`runner.run` builds `template.format(path=shlex.quote(str(resolved.path)))` and invokes it through `backend.inline(...)` + `subprocess.Popen` with `start_new_session=True` so the GUI viewer survives `hop`'s exit. Stdout/stderr go to `/dev/null` — these are fire-and-forget GUI launches. A non-zero exit surfaces via `HopError` from the CLI; the kitten swallows handler failures into a kitten-panel notification, mirroring how it handles other dispatch errors today.

A new `OpenHandlerRunner` Protocol on the open command captures the "run this shell command in the session backend" surface, so tests can substitute a stub instead of actually launching processes. Default implementation lives next to `dispatch_resolved_target` in `hop/commands/open.py`.

### Backend namespace

`backend.inline` already wraps the command in the backend's interactive prefix (`podman-compose exec ...`, ssh, etc.). For most backends that's the right place to run the handler. The default `xdg-open {path}` works on a host session and surfaces a clear "xdg-open: command not found" inside a backend that lacks it, prompting the user to configure. Out of scope: a host-namespace escape hatch for backend sessions. If real-world devcontainer usage shows people consistently want backend-resolved paths to open *on the host*, a follow-up task can add a `host = true` toggle per handler. Don't build it speculatively.

### Files to change

- `hop/config.py`:
  - Add `open_handlers: tuple[tuple[str, str], ...]` (ordered to preserve user declaration order; convert to dict at use sites) to `HopConfig`.
  - Add `_BUILTIN_OPEN_HANDLERS` with the binary-only default mapping above.
  - Add `merge_open_handlers` and wire it into `merge_configs` and the builtin-layering step.
  - Parse `[open_handlers]` in `_parse_top_level`.
- `hop/commands/open.py`:
  - Add `match_handler(path, handlers)` and `OpenHandlerRunner` Protocol with a default implementation that goes through `backend.inline` + `subprocess.Popen`.
  - Thread `handlers` (and the runner) through `open_target_in_session` and `dispatch_resolved_target`.
- `hop/commands/open_selection.py` — pass the merged handler set into `dispatch_resolved_target` (the kitten loads config via `hop.focused`).
- `hop/app.py:411-418` (`OpenCommand` arm) — fetch `merged_config(session).open_handlers` and pass to `open_target_in_session`.
- `hop/focused.py` — expose the merged handler set to the kitten alongside `paths_exist`.

### Tests

- `tests/test_config.py` — round-trip a `[open_handlers]` table; assert merge semantics (project overrides global, builtin lives at the bottom, empty-string template removes a default).
- `tests/test_open_command.py` — extend `dispatch_resolved_target` tests with an explicit text-files-stay-with-nvim batch:

  ```python
  @pytest.mark.parametrize("filename", [
      "config.json",
      "docker-compose.yaml",
      "Cargo.toml",
      "app/models/user.rb",
      "Makefile",
      "Dockerfile",
      "notes.md",
      "README",
      "icon.svg",
      "src/main.rs",
      "weird.unknownextension",
  ])
  def test_text_files_dispatch_to_nvim(filename): ...
  ```

  Plus:
  - `logo.png` with default handlers → handler ran with `xdg-open /tmp/.../logo.png`, nvim untouched.
  - `archive.tar.gz` → handler ran (compound extension matches `*.tar.gz`).
  - User override `*.png = "feh {path}"` → feh runs, not xdg-open.
  - User override `*.png = ""` → nvim runs (opt-out works).
  - Handler quoting: `weird name.png` → `shlex.quote`d into the template.
  - Backend wrapping: a non-host backend's `inline` prefix appears in the runner-recorded command string.
- `tests/test_open_selection_kitten.py` — kitten's dispatch path runs the handler too; assert that and that the kitten swallows a handler failure into a kitten-panel notification rather than raising.

### Docs

- `README.md` — add a section under the open command docs describing `[open_handlers]` with the default table and one override example. Explicitly call out: "Text files, JSON / YAML / TOML / Markdown / source code all open in the editor; only the binary types listed above are routed to a system handler."
- `hop_spec.md` — extend the dispatch description for `hop open <target>`: URL → browser, extension-matched path → configured handler, everything else → editor.
- `.dust/facts/hop-target-dispatch-and-behavior-guarantees.md` — describe the new classifier layer.

## Task Type

implement

## Principles

- [Role-based terminals are routing primitives](../principles/role-based-terminals-are-routing-primitives.md)
- [One shared editor per session](../principles/one-shared-editor-per-session.md)

## Blocked By

(none)

## Definition of Done

- `hop open foo.png` runs `xdg-open <abs-path>` (or whatever the user configured) instead of dispatching to nvim. The PNG opens in the system image viewer; no garbage buffer.
- `hop open foo.json`, `hop open Cargo.toml`, `hop open docker-compose.yaml`, `hop open icon.svg`, `hop open Makefile`, and `hop open notes.md` all continue to open in the session's nvim, unchanged from today's behavior.
- Rails refs (`Controller#action`) keep dispatching to nvim — their resolved `.rb` path doesn't match any default handler.
- The open-selection kitten dispatches the same way: clicking a `.png` in visible kitty output opens it through the handler, not nvim; clicking a `.json` opens in nvim.
- `[open_handlers]` in `~/.config/hop/config.toml` or a project's `.hop.toml` overrides defaults per pattern; setting a pattern to `""` removes that default and falls through to nvim.
- Built-in defaults cover only images, video, audio, archives, PDFs, office formats, and native binaries — nothing text-shaped (no `.svg`, no `.json`, no `.yaml`, no source code).
- Handler commands run through `backend.inline` so devcontainer / ssh sessions can resolve the handler inside the backend; users who want host-side launches configure their handler accordingly.
- Tests cover: default classification for both the binary list and the text-files-still-go-to-nvim batch, user overrides, empty-template opt-out, path quoting, kitten dispatch, and backend-prefix wrapping.
- README and `hop_spec.md` document the new config surface and explicitly call out that text files (JSON / YAML / TOML / Markdown / source code) stay with the editor.
- `make` is green.
