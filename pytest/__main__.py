from __future__ import annotations

import importlib.util
import inspect
import sys
import tempfile
import traceback
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

from ._compat import ParametrizeConfig

ROOT = Path.cwd()
TESTS_DIR = ROOT / "tests"


def load_test_module(path: Path) -> ModuleType:
    module_name = ".".join(path.relative_to(ROOT).with_suffix("").parts)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load test module {path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def iter_test_functions(module: ModuleType) -> list[tuple[str, Callable[..., Any]]]:
    tests: list[tuple[str, Callable[..., Any]]] = []
    for name, value in vars(module).items():
        if name.startswith("test_") and callable(value):
            tests.append((name, value))
    return sorted(tests, key=lambda item: item[0])


def build_kwargs(func: Callable[..., Any], names: tuple[str, ...], values: tuple[Any, ...]) -> dict[str, Any]:
    kwargs = dict(zip(names, values, strict=True))
    for parameter_name in inspect.signature(func).parameters:
        if parameter_name == "tmp_path" and parameter_name not in kwargs:
            temp_dir = tempfile.TemporaryDirectory()
            kwargs[parameter_name] = Path(temp_dir.name)
            temp_dirs.append(temp_dir)
    return kwargs


temp_dirs: list[tempfile.TemporaryDirectory[str]] = []


def run_case(
    module_path: Path, test_name: str, func: Callable[..., Any], case_index: int | None = None
) -> tuple[bool, str]:
    label = f"{module_path.relative_to(ROOT)}::{test_name}"
    if case_index is not None:
        label = f"{label}[{case_index}]"

    parametrize: ParametrizeConfig | None = getattr(func, "__pytest_parametrize__", None)
    try:
        if parametrize is None:
            kwargs = build_kwargs(func, (), ())
            func(**kwargs)
        else:
            assert case_index is not None
            names = parametrize.names
            values = parametrize.values[case_index]
            kwargs = build_kwargs(func, names, values)
            func(**kwargs)
    except Exception:
        return False, f"{label}\n{traceback.format_exc()}"
    finally:
        while temp_dirs:
            temp_dirs.pop().cleanup()

    return True, label


def run() -> int:
    argv = [arg for arg in sys.argv[1:] if arg not in {"-q", "--quiet"}]
    if argv == ["--version"]:
        print("pytest 0.0-local")
        return 0
    if argv:
        print(f"unsupported arguments: {' '.join(argv)}", file=sys.stderr)
        return 2

    test_files = sorted(TESTS_DIR.glob("test_*.py"))
    if not test_files:
        print("no tests collected")
        return 5

    passed = 0
    failures: list[str] = []

    for path in test_files:
        module = load_test_module(path)
        for test_name, func in iter_test_functions(module):
            parametrize: ParametrizeConfig | None = getattr(func, "__pytest_parametrize__", None)
            if parametrize is None:
                ok, detail = run_case(path, test_name, func)
                if ok:
                    passed += 1
                else:
                    failures.append(detail)
                continue

            for index, _ in enumerate(parametrize.values):
                ok, detail = run_case(path, test_name, func, case_index=index)
                if ok:
                    passed += 1
                else:
                    failures.append(detail)

    if failures:
        for failure in failures:
            print(failure, file=sys.stderr)
        print(f"{len(failures)} failed, {passed} passed in 0.00s", file=sys.stderr)
        return 1

    print(f"{passed} passed in 0.00s")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
