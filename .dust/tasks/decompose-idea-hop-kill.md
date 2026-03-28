# Decompose Idea: hop kill

Create one or more well-defined tasks from this idea. Prefer smaller, narrowly scoped tasks that each deliver a thin but complete vertical slice of working software -- a path through the system that can be tested end-to-end -- rather than component-oriented tasks (like "add schema" or "build endpoint") that only work once all tasks are done. Split the idea into multiple tasks if it covers more than one logical change. Run `dust principles` to link relevant principles and `dust facts` for design decisions that should inform the task. See [hop kill](../ideas/hop-kill.md).

## Resolved Questions

### What is the kill boundary?

**Decision:** Managed windows only

### How should users target a session?

**Decision:** Current working directory only

### What should happen to focus when killing the current session?

**Decision:** Let Sway decide


## Decomposes Idea

- [hop kill](../ideas/hop-kill.md)


## Task Type

decompose

## Blocked By

(none)


## Definition of Done

- One or more new tasks are created in .dust/tasks/
- Task's Principles section links to relevant principles from .dust/principles/
- The original idea is deleted or updated to reflect remaining scope
