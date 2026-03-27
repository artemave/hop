# Document and stabilize the vigun contract

Document and stabilize the `hop run --role` contract that `vigun` will call for test execution.

Add an integration note such as `docs/vigun.md` and a smoke-level caller test or fixture under `tests/` that exercises `hop run --role test "<command>"`. Keep the CLI contract small and explicit so the follow-up `vigun` repository changes are mechanical instead of exploratory.

This task documents how the role-based command routing in [hop session model and command contract](../facts/hop-session-model-and-command-contract.md) should be consumed by `vigun`, following [hop_spec.md](../../hop_spec.md).

## Principles

- [Keep the spec aligned](../principles/keep-the-spec-aligned.md)
- [Role-based terminals are routing primitives](../principles/role-based-terminals-are-routing-primitives.md)

## Task Type

implement

## Blocked By

(none)

## Definition of Done

- The `hop run --role` interface is documented with examples that `vigun` can call directly
- A smoke-level test or fixture proves the `test` role contract behaves as documented
- The remaining work that must happen in the `vigun` repository is explicitly listed
- Any user-visible change to the `hop run --role` contract updates [hop_spec.md](../../hop_spec.md) and the derived facts in the same change
