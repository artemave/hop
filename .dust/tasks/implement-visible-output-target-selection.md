# Implement visible-output target selection

Implement interactive target picking from visible Kitty output through a Kitty-native extension.

Add selection and resolution code in `kittens/open_selection/`, `hop/commands/open_selection.py`, and `hop/targets.py` or equivalent modules. Implement the interactive UX as a kitten or equivalent Kitty-native extension, support the required file, file:line, git diff, URL, and Rails-style patterns from `hop_spec.md`, resolve relative paths against the terminal working directory and then the project root, strip `a/` and `b/` prefixes from git diff paths, and send files to `hop edit` and URLs to `hop browser`.

This task implements the visible-output workflow described in [hop target dispatch and behavior guarantees](../facts/hop-target-dispatch-and-behavior-guarantees.md), which distills the relevant selection and resolution rules from [hop_spec.md](../../hop_spec.md).

## Principles

- [Keep the spec aligned](../principles/keep-the-spec-aligned.md)
- [Prefer native integrations](../principles/prefer-native-integrations.md)
- [One shared editor per session](../principles/one-shared-editor-per-session.md)
- [Role-based terminals are routing primitives](../principles/role-based-terminals-are-routing-primitives.md)

## Task Type

implement

## Blocked By

- [Implement role-based terminals and run routing](implement-role-based-terminals-and-run-routing.md)
- [Implement shared Neovim lifecycle and editing](implement-shared-neovim-lifecycle-and-editing.md)
- [Implement the session browser command](implement-the-session-browser-command.md)

## Definition of Done

- Users can trigger an interactive selection flow over visible Kitty output through a kitten or equivalent Kitty-native extension
- The selected target is resolved correctly as a file, file-plus-line, git diff path, URL, or Rails-style reference
- Files open in the shared Neovim instance and URLs open in the session browser
- Unresolvable targets are ignored without crashing or opening the wrong destination
- Any user-visible change to selection, dispatch, or target-resolution rules updates [hop_spec.md](../../hop_spec.md) and the derived facts in the same change
