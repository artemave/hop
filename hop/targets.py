from __future__ import annotations

import os.path
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from hop.backends import SessionBackend
    from hop.session import ProjectSession

# Permissive on purpose: any path-shaped token may match. Existence filtering
# happens outside this module (via ``backend.paths_exist`` from the focused
# hop session) so the regex stays generous and the resolver stays pure.
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


@dataclass(frozen=True, slots=True)
class SyntacticUrlTarget:
    url: str


@dataclass(frozen=True, slots=True)
class SyntacticFileTarget:
    path_text: str
    line_number: int | None = None


@dataclass(frozen=True, slots=True)
class SyntacticRailsRefTarget:
    controller: str
    action: str


SyntacticTarget = SyntacticUrlTarget | SyntacticFileTarget | SyntacticRailsRefTarget


def parse_visible_output_target(selection: str) -> SyntacticTarget | None:
    """Pure-string parse of a selection into a syntactic target.

    Returns ``None`` for empty input. The output union tells the caller
    what kind of target was recognized; turning it into a dispatchable
    ``ResolvedTarget`` (which may require backend I/O, e.g. for Rails
    refs) is the job of ``resolve_target``.
    """

    cleaned_selection = selection.strip()
    if not cleaned_selection:
        return None

    url = _normalize_url(cleaned_selection)
    if url is not None:
        return SyntacticUrlTarget(url=url)

    rails_match = RAILS_REFERENCE_PATTERN.match(cleaned_selection)
    if rails_match is not None:
        return SyntacticRailsRefTarget(
            controller=rails_match.group("controller"),
            action=rails_match.group("action"),
        )

    path_text, line_number = _split_file_target(cleaned_selection)
    return SyntacticFileTarget(path_text=path_text, line_number=line_number)


def resolve_target(
    syntactic: SyntacticTarget,
    *,
    session: ProjectSession,
    backend: SessionBackend,
    terminal_cwd: Path | str | None,
) -> ResolvedTarget | None:
    """Turn a syntactic target into a dispatchable ``ResolvedTarget``.

    ``terminal_cwd`` is the namespace against which a relative file path
    absolutizes. Pass the in-shell cwd (e.g. kitty's ``cwd_of_child``)
    when the caller knows the editor's filesystem namespace; pass ``None``
    to keep the path text untouched and let the editor resolve relatives
    against its own cwd (the CLI's case — hop runs on the host but nvim
    runs in the backend).

    For Rails refs, this reads the controller file via ``backend.read_file``
    and scans for ``def <action>`` in Python. Returns ``None`` if the file
    is missing or the action isn't defined; the caller decides whether to
    filter (kitten highlighting) or raise (CLI).
    """

    if isinstance(syntactic, SyntacticUrlTarget):
        return ResolvedUrlTarget(url=syntactic.url)

    terminal_directory = Path(terminal_cwd).expanduser().resolve(strict=False) if terminal_cwd is not None else None

    if isinstance(syntactic, SyntacticRailsRefTarget):
        path = resolve_file_candidate(
            f"app/controllers/{_underscore_constant_path(syntactic.controller)}.rb",
            terminal_cwd=terminal_directory,
        )
        # Local import to avoid a config → backends → targets cycle: this
        # module is imported by backends-adjacent code at module load.
        from hop.backends import BackendFileNotFoundError

        try:
            content = backend.read_file(session, path)
        except BackendFileNotFoundError:
            return None
        # action is parser-constrained to [A-Za-z_][A-Za-z0-9_]*, so the
        # word-boundary suffix \b plus a literal interpolation is safe.
        pattern = re.compile(rf"^\s*def\s+{syntactic.action}\b")
        for line_number, line_text in enumerate(content.splitlines(), start=1):
            if pattern.match(line_text):
                return ResolvedFileTarget(path=path, line_number=line_number)
        return None

    return ResolvedFileTarget(
        path=resolve_file_candidate(syntactic.path_text, terminal_cwd=terminal_directory),
        line_number=syntactic.line_number,
    )


def _normalize_url(selection: str) -> str | None:
    parsed = urlparse(selection)
    if parsed.scheme not in {"http", "https"}:
        return None
    if not parsed.netloc:
        return None
    return selection


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


def resolve_file_candidate(
    candidate: str,
    *,
    terminal_cwd: Path | None,
) -> Path:
    """Return the path the candidate string represents.

    With ``terminal_cwd`` set, relatives absolutize against that cwd — the
    result is a host- or backend-namespace absolute path, depending on what
    the caller supplied. With ``terminal_cwd=None`` the candidate is kept
    in its normalized-but-not-expanded form (no ``~`` expansion, no cwd
    join) so the editor resolves it against its own cwd.

    Existence is intentionally not checked here — the caller asks the active
    backend (via ``hop.focused.paths_exist``) which of its candidates exist.
    """

    normalized_candidate = _normalize_file_candidate(candidate)
    if terminal_cwd is None:
        return Path(normalized_candidate)
    expanded_candidate = Path(os.path.expanduser(normalized_candidate))
    if expanded_candidate.is_absolute():
        return expanded_candidate.resolve(strict=False)
    return (terminal_cwd / expanded_candidate).resolve(strict=False)


def _normalize_file_candidate(candidate: str) -> str:
    return _strip_git_diff_prefix(candidate)


def _strip_git_diff_prefix(candidate: str) -> str:
    if candidate.startswith("a/") or candidate.startswith("b/"):
        return candidate[2:]
    return candidate
