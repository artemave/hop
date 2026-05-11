"""Tests for ``hop.focused.paths_exist`` — the kitten's entry point.

These tests inject fake sway/sessions/cwd/backend loaders so the function
runs deterministically without a live sway socket or running session.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from hop.focused import paths_exist
from hop.session import ProjectSession
from hop.state import CommandBackendRecord, SessionState

_HOST_RECORD = CommandBackendRecord(name="host", interactive_prefix="", noninteractive_prefix="")


class _FakeBackend:
    """Minimal ``SessionBackend`` for tests — only ``paths_exist`` is meaningful;
    the rest are stubs satisfying the Protocol so pyright is happy."""

    def __init__(self, existing: set[Path]) -> None:
        self.existing = existing
        self.calls: list[Sequence[Path]] = []

    @property
    def interactive_prefix(self) -> str:
        return ""

    def prepare(self, session: ProjectSession) -> None:
        del session

    def wrap(self, command: str, session: ProjectSession) -> Sequence[str]:
        del command, session
        return ()

    def inline(self, command: str, session: ProjectSession) -> str:
        del session
        return command

    def translate_localhost_url(self, session: ProjectSession, url: str) -> str:
        del session
        return url

    def paths_exist(self, session: ProjectSession, paths: Sequence[Path]) -> set[Path]:
        del session
        self.calls.append(tuple(paths))
        return {p for p in paths if p in self.existing}

    def teardown(self, session: ProjectSession) -> None:
        del session


def _state(name: str, project_root: Path) -> SessionState:
    return SessionState(name=name, project_root=project_root, backend=_HOST_RECORD)


def test_paths_exist_resolves_relative_candidates_against_focused_cwd(tmp_path: Path) -> None:
    """Relative candidates from terminal output are resolved against the
    focused window's in-shell cwd before being checked. The injected
    ``cwd_loader`` simulates kitty's OSC 7 reply."""
    project_root = tmp_path / "demo"
    shell_cwd = project_root / "src"
    shell_cwd.mkdir(parents=True)
    expected_path = shell_cwd / "app/foo.rb"
    fake_backend = _FakeBackend(existing={expected_path.resolve()})

    result = paths_exist(
        ["app/foo.rb", "missing.rb"],
        focused_workspace=lambda: "p:demo",
        sessions_loader=lambda: {"demo": _state("demo", project_root.resolve())},
        cwd_loader=lambda _name: shell_cwd.resolve(),
        backend_loader=lambda _state: fake_backend,
    )

    assert result == {"app/foo.rb"}
    # The backend saw resolved absolute paths, not the raw input strings.
    assert fake_backend.calls
    assert expected_path.resolve() in set(fake_backend.calls[0])


def test_paths_exist_returns_input_strings_not_resolved_paths(tmp_path: Path) -> None:
    """Callers (the kitten) match by string identity. Verify the returned
    set contains the original input strings even when resolution rewrites
    the path (e.g. via ``..``)."""
    project_root = tmp_path / "demo"
    shell_cwd = project_root / "src"
    shell_cwd.mkdir(parents=True)
    expected_path = (shell_cwd / "../app/foo.rb").resolve()
    fake_backend = _FakeBackend(existing={expected_path})

    result = paths_exist(
        ["../app/foo.rb"],
        focused_workspace=lambda: "p:demo",
        sessions_loader=lambda: {"demo": _state("demo", project_root.resolve())},
        cwd_loader=lambda _name: shell_cwd.resolve(),
        backend_loader=lambda _state: fake_backend,
    )

    assert result == {"../app/foo.rb"}


def test_paths_exist_falls_back_to_local_when_workspace_not_a_hop_session(tmp_path: Path) -> None:
    """When sway reports a workspace that isn't ``p:<name>``, the function
    falls back to local ``Path.exists()`` against ``Path.cwd()``. The
    backend loader is never invoked."""
    backend_loader_calls: list[SessionState] = []

    def record_backend_loader(state: SessionState) -> _FakeBackend:
        backend_loader_calls.append(state)
        return _FakeBackend(existing=set())

    existing_file = tmp_path / "exists.txt"
    existing_file.write_text("")
    import os

    cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        result = paths_exist(
            ["exists.txt", "missing.txt"],
            focused_workspace=lambda: "some-other-workspace",
            sessions_loader=lambda: {},
            cwd_loader=lambda _name: None,
            backend_loader=record_backend_loader,
        )
    finally:
        os.chdir(cwd)

    assert result == {"exists.txt"}
    assert backend_loader_calls == []


def test_paths_exist_falls_back_when_session_state_missing(tmp_path: Path) -> None:
    """Workspace name matches ``p:<name>`` but no recorded session exists
    for it — fall back to local check."""
    existing_file = tmp_path / "exists.txt"
    existing_file.write_text("")
    import os

    cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        result = paths_exist(
            ["exists.txt", "missing.txt"],
            focused_workspace=lambda: "p:unknown",
            sessions_loader=lambda: {},
            cwd_loader=lambda _name: None,
            backend_loader=lambda _state: None,
        )
    finally:
        os.chdir(cwd)

    assert result == {"exists.txt"}


def test_paths_exist_falls_back_when_sway_raises(tmp_path: Path) -> None:
    """Any error in the focused-workspace lookup (e.g. sway socket gone)
    falls back to local behavior."""
    existing_file = tmp_path / "exists.txt"
    existing_file.write_text("")
    import os

    def raise_sway() -> str:
        raise OSError("sway socket gone")

    cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        result = paths_exist(
            ["exists.txt"],
            focused_workspace=raise_sway,
            sessions_loader=lambda: {},
            cwd_loader=lambda _name: None,
            backend_loader=lambda _state: None,
        )
    finally:
        os.chdir(cwd)

    assert result == {"exists.txt"}


def test_paths_exist_uses_real_sway_when_focused_workspace_kwarg_omitted(tmp_path: Path) -> None:
    """When no ``focused_workspace`` callable is injected, the function uses
    the default SwayIpcAdapter probe. Either succeeds and returns whatever
    the user's actual hop session can verify, or the IPC raises and we fall
    back to local Path.exists. Either way the call must not crash."""
    existing_file = tmp_path / "exists.txt"
    existing_file.write_text("")
    import os

    cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        # No focused_workspace= override — exercises _default_focused_workspace.
        result = paths_exist(
            ["exists.txt"],
            sessions_loader=lambda: {},
        )
    finally:
        os.chdir(cwd)

    # If sway/state aligned to a real hop session, the path may not exist
    # there and we'd get empty. The contract is "doesn't crash"; both
    # outcomes are valid.
    assert result in (set(), {"exists.txt"})


def test_paths_exist_empty_input_returns_empty(tmp_path: Path) -> None:
    result = paths_exist(
        [],
        focused_workspace=lambda: "p:demo",
        sessions_loader=lambda: {"demo": _state("demo", tmp_path)},
        cwd_loader=lambda _name: tmp_path,
        backend_loader=lambda _state: _FakeBackend(existing=set()),
    )

    assert result == set()


def test_paths_exist_falls_back_to_state_project_root_when_kitty_socket_dead(tmp_path: Path) -> None:
    """When the kitty per-session socket isn't reachable, ``cwd_loader``
    returns ``None`` and the function uses the persisted session project
    root as the relative-path base."""
    project_root = tmp_path / "demo"
    project_root.mkdir()
    expected_path = project_root / "foo.rb"
    expected_path.write_text("")
    fake_backend = _FakeBackend(existing={expected_path.resolve()})

    result = paths_exist(
        ["foo.rb"],
        focused_workspace=lambda: "p:demo",
        sessions_loader=lambda: {"demo": _state("demo", project_root.resolve())},
        cwd_loader=lambda _name: None,
        backend_loader=lambda _state: fake_backend,
    )

    assert result == {"foo.rb"}


def test_paths_exist_translates_rails_references_via_target_resolver(tmp_path: Path) -> None:
    """``Processing UsersController#index`` resolves to a controller path
    against the focused cwd, then gets existence-checked through the
    backend."""
    project_root = tmp_path / "demo"
    shell_cwd = project_root
    shell_cwd.mkdir(parents=True)
    expected_path = (shell_cwd / "app/controllers/users_controller.rb").resolve()
    fake_backend = _FakeBackend(existing={expected_path})

    result = paths_exist(
        ["Processing UsersController#index"],
        focused_workspace=lambda: "p:demo",
        sessions_loader=lambda: {"demo": _state("demo", project_root.resolve())},
        cwd_loader=lambda _name: shell_cwd.resolve(),
        backend_loader=lambda _state: fake_backend,
    )

    assert result == {"Processing UsersController#index"}


def test_paths_exist_falls_back_when_backend_loader_returns_none(tmp_path: Path) -> None:
    """If the backend loader returns ``None`` (e.g. a malformed record),
    fall back to local Path.exists rather than crash."""
    existing_file = tmp_path / "exists.txt"
    existing_file.write_text("")
    import os

    project_root = tmp_path / "demo"
    project_root.mkdir()

    cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        result = paths_exist(
            ["exists.txt"],
            focused_workspace=lambda: "p:demo",
            sessions_loader=lambda: {"demo": _state("demo", project_root.resolve())},
            cwd_loader=lambda _name: project_root.resolve(),
            backend_loader=lambda _state: None,
        )
    finally:
        os.chdir(cwd)

    assert result == {"exists.txt"}


def test_paths_exist_returns_empty_set_when_no_candidate_resolves_to_file(tmp_path: Path) -> None:
    """URL candidates (and any non-file resolutions) drop out of the result.
    A URL-only batch should return an empty set without calling the backend."""
    project_root = tmp_path / "demo"
    project_root.mkdir()
    fake_backend = _FakeBackend(existing=set())

    result = paths_exist(
        ["https://example.com"],
        focused_workspace=lambda: "p:demo",
        sessions_loader=lambda: {"demo": _state("demo", project_root.resolve())},
        cwd_loader=lambda _name: project_root.resolve(),
        backend_loader=lambda _state: fake_backend,
    )

    assert result == set()
    # backend.paths_exist must not be invoked when there's nothing file-shaped.
    assert fake_backend.calls == []


def test_paths_exist_round_trips_through_command_backend_record(tmp_path: Path) -> None:
    """End-to-end check that the default backend_loader path (via
    ``hop.app.backend_from_record``) reconstructs a usable backend from a
    persisted record. For the built-in ``host`` record (empty prefixes), the
    synthesized command is ``sh -c '<loop>'`` running locally."""
    project_root = tmp_path / "demo"
    project_root.mkdir(parents=True)
    existing_file = project_root / "foo.rb"
    existing_file.write_text("")

    record = CommandBackendRecord(name="host", interactive_prefix="", noninteractive_prefix="")
    state = SessionState(name="demo", project_root=project_root.resolve(), backend=record)

    result = paths_exist(
        ["foo.rb"],
        focused_workspace=lambda: "p:demo",
        sessions_loader=lambda: {"demo": state},
        cwd_loader=lambda _name: project_root.resolve(),
    )

    assert result == {"foo.rb"}
