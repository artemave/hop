# hop target dispatch and behavior guarantees

The current `hop` contract includes visible-output target dispatch and strict window reuse guarantees.

Per [hop_spec.md](../../hop_spec.md), users must be able to select file references or URLs from visible Kitty output and dispatch them to the shared Neovim instance or the session browser. The required target patterns are file paths, file paths with line numbers, git diff paths with `a/` or `b/` prefixes, URLs, and Rails-style references. File resolution tries absolute paths first, then the terminal working directory, then the project root, and ignores unresolved targets. All commands are intended to be idempotent, reuse existing windows whenever possible, and recreate missing session components automatically rather than creating duplicates.
