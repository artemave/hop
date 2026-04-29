from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

# Permissive on purpose: any path-shaped token may match. The disk-existence
# check in `_resolve_file_candidate` is what filters real targets from noise.
VISIBLE_OUTPUT_TARGET_PATTERN = re.compile(
    r"""
    (?P<url>https?://[^\s<>"'`)\]}]+)
    |
    (?P<rails>Processing\s+[A-Z][A-Za-z0-9_:]*Controller\#[A-Za-z_][A-Za-z0-9_]*)
    |
    (?P<rails_bare>[A-Z][A-Za-z0-9_:]*Controller\#[A-Za-z_][A-Za-z0-9_]*)
    |
    (?:\w+\()?
    (?P<file>
        [~./]?[-a-zA-Z0-9_./]
        [-a-zA-Z0-9_+\-,./@\[\]$]*
        (?:\([-a-zA-Z0-9_+\-,./@\[\]$]*\)[-a-zA-Z0-9_+\-,./@\[\]$]*)*
        (?:(?::|",\s+line\s+)\d+)?
    )
    \)?
    """,
    re.VERBOSE,
)

_PYTHON_TRACEBACK_LINE_SUFFIX = re.compile(r'",\s+line\s+(\d+)$')

RAILS_REFERENCE_PATTERN = re.compile(
    r"^(?:Processing\s+)?(?P<controller>[A-Z][A-Za-z0-9_:]*Controller)#(?P<action>[A-Za-z_][A-Za-z0-9_]*)$"
)


@dataclass(frozen=True, slots=True)
class ResolvedUrlTarget:
    url: str


@dataclass(frozen=True, slots=True)
class ResolvedFileTarget:
    path: Path
    line_number: int | None = None

    @property
    def editor_target(self) -> str:
        if self.line_number is None:
            return str(self.path)
        return f"{self.path}:{self.line_number}"


ResolvedTarget = ResolvedUrlTarget | ResolvedFileTarget


def resolve_visible_output_target(
    selection: str,
    *,
    terminal_cwd: Path | str,
    project_root: Path | str,
) -> ResolvedTarget | None:
    cleaned_selection = selection.strip()
    if not cleaned_selection:
        return None

    url = _normalize_url(cleaned_selection)
    if url is not None:
        return ResolvedUrlTarget(url=url)

    terminal_directory = Path(terminal_cwd).expanduser().resolve(strict=False)
    project_directory = Path(project_root).expanduser().resolve(strict=False)

    rails_target = _rails_reference_target(cleaned_selection)
    if rails_target is not None:
        resolved_path = _resolve_file_candidate(
            rails_target,
            terminal_cwd=terminal_directory,
            project_root=project_directory,
        )
        if resolved_path is None:
            return None
        return ResolvedFileTarget(path=resolved_path)

    path_text, line_number = _split_file_target(cleaned_selection)
    resolved_path = _resolve_file_candidate(
        path_text,
        terminal_cwd=terminal_directory,
        project_root=project_directory,
    )
    if resolved_path is None:
        return None

    return ResolvedFileTarget(path=resolved_path, line_number=line_number)


def _normalize_url(selection: str) -> str | None:
    parsed = urlparse(selection)
    if parsed.scheme not in {"http", "https"}:
        return None
    if not parsed.netloc:
        return None
    return selection


def _rails_reference_target(selection: str) -> str | None:
    match = RAILS_REFERENCE_PATTERN.match(selection)
    if match is None:
        return None

    controller = match.group("controller")
    return f"app/controllers/{_underscore_constant_path(controller)}.rb"


def _underscore_constant_path(value: str) -> str:
    return "/".join(_underscore_word(part) for part in value.split("::"))


def _underscore_word(value: str) -> str:
    underscored = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", value)
    underscored = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", underscored)
    return underscored.replace("-", "_").lower()


def _split_file_target(selection: str) -> tuple[str, int | None]:
    traceback_match = _PYTHON_TRACEBACK_LINE_SUFFIX.search(selection)
    if traceback_match is not None:
        return selection[: traceback_match.start()], int(traceback_match.group(1))
    path_text, separator, suffix = selection.rpartition(":")
    if separator and suffix.isdigit() and path_text:
        return path_text, int(suffix)
    return selection, None


def _resolve_file_candidate(
    candidate: str,
    *,
    terminal_cwd: Path,
    project_root: Path,
) -> Path | None:
    normalized_candidate = _normalize_file_candidate(candidate)
    expanded_candidate = Path(normalized_candidate).expanduser()
    path_candidates: list[Path] = []
    if expanded_candidate.is_absolute():
        path_candidates.append(expanded_candidate)
    else:
        path_candidates.append(terminal_cwd / expanded_candidate)
        path_candidates.append(project_root / expanded_candidate)

    for path_candidate in path_candidates:
        resolved_candidate = path_candidate.resolve(strict=False)
        if resolved_candidate.exists():
            return resolved_candidate
    return None


def _normalize_file_candidate(candidate: str) -> str:
    return _strip_git_diff_prefix(candidate)


def _strip_git_diff_prefix(candidate: str) -> str:
    if candidate.startswith("a/") or candidate.startswith("b/"):
        return candidate[2:]
    return candidate
