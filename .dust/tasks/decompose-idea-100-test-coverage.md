# Decompose Idea: 100% Test Coverage

Create one or more well-defined tasks from this idea. Prefer smaller, narrowly scoped tasks that each deliver a thin but complete vertical slice of working software -- a path through the system that can be tested end-to-end -- rather than component-oriented tasks (like "add schema" or "build endpoint") that only work once all tasks are done. Split the idea into multiple tasks if it covers more than one logical change. Run `dust principles` to link relevant principles and `dust facts` for design decisions that should inform the task. See [100% Test Coverage](../ideas/100-test-coverage.md).

## Resolved Questions

### How should coverage be measured given the custom pytest runner?

**Decision:** Replace the custom runner with real pytest

### What coverage metric should be enforced?

**Decision:** line coverage 100% + branch coverage 100%. This forces you to review defensive code and rm most of it I'd imagine.

### How should legitimately untestable code be handled?

**Decision:** Refactor to push untestable paths into a thin imperative shell


## Decomposes Idea

- [100% Test Coverage](../ideas/100-test-coverage.md)


## Task Type

decompose

## Blocked By

(none)


## Definition of Done

- One or more new tasks are created in .dust/tasks/
- Task's Principles section links to relevant principles from .dust/principles/
- The original idea is deleted or updated to reflect remaining scope
