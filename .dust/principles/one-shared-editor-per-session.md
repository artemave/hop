# One shared editor per session

Use exactly one shared Neovim instance per session and route all editor-opening actions into it.

The editor belongs to the session, not to a particular terminal window. `hop edit` should ensure the session editor exists, focus it when it already exists, recreate it after shutdown, and direct file and file-plus-line targets into that shared instance. Designs that create multiple competing editors for one project session violate the intended workflow.

## Parent Principle

- [Session-oriented workspaces](session-oriented-workspaces.md)

## Sub-Principles

- (none)
