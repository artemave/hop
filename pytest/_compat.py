from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ParametrizeConfig:
    names: tuple[str, ...]
    values: tuple[tuple[Any, ...], ...]


class _Mark:
    def parametrize(
        self, argnames: str | tuple[str, ...], argvalues: list[Any] | tuple[Any, ...]
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        if isinstance(argnames, str):
            names = tuple(name.strip() for name in argnames.split(",") if name.strip())
        else:
            names = tuple(argnames)

        normalized_values: list[tuple[Any, ...]] = []
        for value in argvalues:
            if len(names) == 1:
                normalized_values.append((value,))
            else:
                normalized_values.append(tuple(value))

        config = ParametrizeConfig(names=names, values=tuple(normalized_values))

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            setattr(func, "__pytest_parametrize__", config)
            return func

        return decorator


mark = _Mark()
