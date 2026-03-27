# hop spec is canonical product context

The current product contract for `hop` is defined in [hop_spec.md](../../hop_spec.md).

This repository is still in the planning and scaffolding stage, so `hop_spec.md` is the canonical source for intended behavior and scope. Dust tasks should implement thin slices of that contract, while dust principles capture decision-making constraints and dust facts summarize stable parts of the specification for reuse by future agents. If implementation changes the product contract, `hop_spec.md` and any derived dust facts should be updated in the same change so the spec does not drift.
