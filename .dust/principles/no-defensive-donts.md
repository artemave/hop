# No defensive don'ts

Specify what the code does. Don't enumerate things the code "does not do" when those things aren't on the implementation path anyway.

A task that says "the executor does X" already excludes Y by omission. Restating "the executor does not do Y" adds nothing to the design but pressures the implementer to verify the absence — defensive `assert never_called(Y)` lines in the code, defensive test cases asserting Y wasn't done. The cost is real and the protection is illusory: any future change that *would* call Y is a deliberate edit, not an accident a "does not" clause would have caught.

Reserve negative scoping for genuine alternatives — designs a thoughtful implementer might reasonably choose. "Out of scope: following the moved window onto the destination workspace" is real scope: someone could reasonably build that. "Out of scope: adding Sway marks to the moved window" is *not* real scope when the design's only Sway call is `move_window_to_workspace` — nobody would add `mark_window` by accident; saying so only invites a defensive test for it.

Same rule applies to implementation: code shouldn't carry `# we deliberately do not call X` comments or `assert not called(X)` guards for things the function obviously doesn't do. State intent in identifiers and structure, not in negation.

## Parent Principle

(none)

## Sub-Principles

- (none)
