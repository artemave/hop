# Decompose Idea: Add Typechecking and Lint for Python Code

Create one or more well-defined tasks from this idea. Prefer smaller, narrowly scoped tasks that each deliver a thin but complete vertical slice of working software -- a path through the system that can be tested end-to-end -- rather than component-oriented tasks (like "add schema" or "build endpoint") that only work once all tasks are done. Split the idea into multiple tasks if it covers more than one logical change. Run `dust principles` to link relevant principles and `dust facts` for design decisions that should inform the task. See [Add Typechecking and Lint for Python Code](../ideas/add-typechecking-and-lint-for-python-code.md).

## Resolved Questions

### Which type checker to use?

**Decision:** Option: pyright / pylance

### Which linter/formatter to use?

**Decision:** Option: ruff

### How to integrate into the check workflow?

**Decision:** Option: Add as `uv run` commands in a Makefile or shell script

### Strictness level for the type checker?

**Decision:** Option: Start strict (e.g. `mypy --strict` or `pyright` in strict mode)


## Decomposes Idea

- [Add Typechecking and Lint for Python Code](../ideas/add-typechecking-and-lint-for-python-code.md)


## Task Type

decompose

## Blocked By

(none)


## Definition of Done

- One or more new tasks are created in .dust/tasks/
- Task's Principles section links to relevant principles from .dust/principles/
- The original idea is deleted or updated to reflect remaining scope
