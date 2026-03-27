# Implement shared Neovim lifecycle and editing

Implement a single shared Neovim instance per session and route `hop edit` targets into that remote editor.

Add editor discovery, start, and focus logic in `hop/editor.py` and command handling in `hop/commands/edit.py`. Use a deterministic remote address or another stable lookup so `hop edit` can recreate the editor after `:qa`, focus it when already running, and open both `path` and `path:line` targets in the session editor.

This task implements the shared-editor rules described in [hop session model and command contract](../facts/hop-session-model-and-command-contract.md) and [hop target dispatch and behavior guarantees](../facts/hop-target-dispatch-and-behavior-guarantees.md), both derived from [hop_spec.md](../../hop_spec.md).

## Principles

- [Keep the spec aligned](../principles/keep-the-spec-aligned.md)
- [Prefer native integrations](../principles/prefer-native-integrations.md)
- [Session-oriented workspaces](../principles/session-oriented-workspaces.md)
- [One shared editor per session](../principles/one-shared-editor-per-session.md)

## Task Type

implement

## Blocked By

- [Implement session entry, switching, and listing](implement-session-entry-switching-and-listing.md)

## Definition of Done

- `hop edit` ensures the session Neovim instance exists and focuses it
- `hop edit <target>` opens files and file-plus-line targets in the shared editor instance
- Closing Neovim and rerunning `hop edit` recreates the editor cleanly
- Editor discovery does not create duplicate Neovim instances for the same session
- Any user-visible change to editor lifecycle or `hop edit` semantics updates [hop_spec.md](../../hop_spec.md) and the derived facts in the same change
