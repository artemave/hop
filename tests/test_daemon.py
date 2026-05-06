from pathlib import Path
from typing import Any, Iterator

import pytest

import hop.daemon as daemon
from hop.errors import HopError
from hop.sway import SwaySubscriptionError


@pytest.fixture(autouse=True)
def isolate_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop the daemon main from touching the user's real filesystem."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HOP_SESSIONS_DIR", str(tmp_path / "sessions"))


@pytest.fixture
def regen_recorder(monkeypatch: pytest.MonkeyPatch) -> list[None]:
    calls: list[None] = []

    def fake_regenerate(**_kwargs: Any) -> None:
        calls.append(None)

    monkeypatch.setattr(daemon, "regenerate", fake_regenerate)
    return calls


@pytest.fixture
def stub_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_list_sessions(*, sway: Any) -> tuple[()]:
        del sway
        return ()

    monkeypatch.setattr(daemon, "list_sessions", fake_list_sessions)


class StubSubscribingSway:
    """Replaces SwayIpcAdapter for the duration of one daemon.main call."""

    def __init__(
        self,
        *,
        events: tuple[dict[str, object], ...] = (),
        raise_on_subscribe: Exception | None = None,
    ) -> None:
        self.events = events
        self.raise_on_subscribe = raise_on_subscribe

    def get_focused_workspace(self) -> str:
        return ""

    def list_session_workspaces(self, *, prefix: str = "p:") -> tuple[str, ...]:
        del prefix
        return ()

    def subscribe_to_workspace_events(self) -> Iterator[dict[str, object]]:
        if self.raise_on_subscribe is not None:
            raise self.raise_on_subscribe
        for event in self.events:
            yield event


def test_daemon_runs_initial_regen_and_one_per_event(
    monkeypatch: pytest.MonkeyPatch,
    regen_recorder: list[None],
    stub_sessions: None,
) -> None:
    sway = StubSubscribingSway(
        events=(
            {"change": "focus", "current": {"name": "p:rails"}},
            {"change": "focus", "current": {"name": "scratch"}},
            {"change": "focus", "current": {"name": "p:other"}},
        ),
    )
    monkeypatch.setattr(daemon, "SwayIpcAdapter", lambda: sway)

    exit_code = daemon.main([])

    # Initial regen (1) plus one regen per event (3).
    assert len(regen_recorder) == 4
    # Stream ends without exception → still non-zero so the user knows
    # to revive the daemon (sway does not auto-respawn it).
    assert exit_code == 1


def test_daemon_returns_one_when_subscription_drops_with_hop_error(
    monkeypatch: pytest.MonkeyPatch,
    regen_recorder: list[None],
    stub_sessions: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sway = StubSubscribingSway(
        events=(),
        raise_on_subscribe=SwaySubscriptionError("Sway refused subscription"),
    )
    monkeypatch.setattr(daemon, "SwayIpcAdapter", lambda: sway)

    exit_code = daemon.main([])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "Sway refused subscription" in captured.err
    # Initial regen ran before the subscribe call raised.
    assert len(regen_recorder) == 1


def test_daemon_returns_one_when_initial_regen_raises(
    monkeypatch: pytest.MonkeyPatch,
    stub_sessions: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def boom(**_kwargs: Any) -> None:
        raise HopError("initial regen failed")

    monkeypatch.setattr(daemon, "regenerate", boom)
    monkeypatch.setattr(daemon, "SwayIpcAdapter", lambda: StubSubscribingSway())

    exit_code = daemon.main([])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "initial regen failed" in captured.err


def test_daemon_runs_regen_against_real_tmp_scripts_dir(
    monkeypatch: pytest.MonkeyPatch,
    stub_sessions: None,
    tmp_path: Path,
) -> None:
    """End-to-end check: regen actually invokes sessions_loader (the closure
    in daemon.main) and writes against the real scripts_dir. Replacing only
    the sway adapter and `list_sessions` exercises the rest of the path."""

    monkeypatch.setattr(daemon, "SwayIpcAdapter", lambda: StubSubscribingSway())
    scripts_dir = tmp_path / "vicinae" / "scripts"
    monkeypatch.setattr(daemon, "default_scripts_dir", lambda: scripts_dir)

    exit_code = daemon.main([])

    assert exit_code == 1
    # No focused session and no sessions → just the always-present
    # `hop-create` entry (the second-search create-or-attach script).
    assert scripts_dir.is_dir()
    assert [path.name for path in scripts_dir.iterdir()] == ["hop-create"]


def test_sweep_stale_persisted_sessions_forgets_sessions_with_no_live_workspace(
    tmp_path: Path,
) -> None:
    from hop.daemon import _sweep_stale_persisted_sessions
    from hop.state import HostBackendRecord, SessionState

    class _SwayWithWorkspaces:
        def list_session_workspaces(self, *, prefix: str = "p:") -> tuple[str, ...]:
            del prefix
            # Only `live` is on the sway side; `stale` was destroyed.
            return ("p:live",)

    forgotten: list[str] = []
    sessions = {
        "live": SessionState(name="live", project_root=tmp_path, backend=HostBackendRecord()),
        "stale": SessionState(name="stale", project_root=tmp_path, backend=HostBackendRecord()),
    }

    _sweep_stale_persisted_sessions(
        sway=_SwayWithWorkspaces(),  # type: ignore[arg-type]
        sessions_loader=lambda: sessions,
        forget=forgotten.append,
    )

    assert forgotten == ["stale"]
