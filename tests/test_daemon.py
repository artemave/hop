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
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("HOP_SESSIONS_DIR", str(tmp_path / "sessions"))


@pytest.fixture(autouse=True)
def reset_debug() -> Iterator[None]:
    from hop import debug as _debug

    _debug.configure(None)
    yield
    _debug.configure(None)


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
    in daemon.main) and writes against the real scripts_dir.

    The subscription-end path then replaces those entries with the
    "daemon stopped — restart" entry, since hopd is no longer running by
    the time the function returns. The test still exercises the full regen
    chain — the assertion below just looks at the *intermediate* state
    inside `regenerate` via a one-shot capture before the exit path
    overwrites the dir."""

    captured: list[set[str]] = []

    real_regenerate = daemon.regenerate

    def regenerate_then_capture(**kwargs: Any) -> None:
        real_regenerate(**kwargs)
        captured.append({p.name for p in kwargs["scripts_dir"].iterdir()})

    monkeypatch.setattr(daemon, "regenerate", regenerate_then_capture)
    monkeypatch.setattr(daemon, "SwayIpcAdapter", lambda: StubSubscribingSway())
    scripts_dir = tmp_path / "vicinae" / "scripts"
    monkeypatch.setattr(daemon, "default_scripts_dir", lambda: scripts_dir)

    exit_code = daemon.main([])

    assert exit_code == 1
    # The intermediate state — what regen wrote — is just `hop-create`
    # (no focused session, no sessions). After the subscription ends,
    # daemon-down rewrite replaces it.
    assert captured == [{"hop-create"}]
    assert {p.name for p in scripts_dir.iterdir()} == {"hop-_daemon-down"}


def test_daemon_logs_unhandled_exception_to_debug_log(
    monkeypatch: pytest.MonkeyPatch,
    stub_sessions: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from hop import debug as _debug

    log_path = tmp_path / "debug.log"
    config_path = tmp_path / "xdg-config" / "hop" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(f'debug_log = "{log_path}"\n')

    def boom(**_kwargs: Any) -> None:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(daemon, "regenerate", boom)
    monkeypatch.setattr(daemon, "SwayIpcAdapter", lambda: StubSubscribingSway())

    exit_code = daemon.main([])

    assert exit_code == 1
    assert _debug.is_enabled() is True
    contents = log_path.read_text()
    assert "hopd: starting" in contents
    assert "hopd: unhandled exception" in contents
    assert "RuntimeError: kaboom" in contents
    # The traceback also still goes to stderr so the parent process can see it.
    assert "kaboom" in capsys.readouterr().err


def test_daemon_logs_hop_error_to_debug_log(
    monkeypatch: pytest.MonkeyPatch,
    stub_sessions: None,
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "debug.log"
    config_path = tmp_path / "xdg-config" / "hop" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(f'debug_log = "{log_path}"\n')

    def boom(**_kwargs: Any) -> None:
        raise HopError("initial regen failed")

    monkeypatch.setattr(daemon, "regenerate", boom)
    monkeypatch.setattr(daemon, "SwayIpcAdapter", lambda: StubSubscribingSway())

    exit_code = daemon.main([])

    assert exit_code == 1
    assert "initial regen failed" in log_path.read_text()


def test_daemon_does_not_create_log_when_debug_log_disabled(
    monkeypatch: pytest.MonkeyPatch,
    regen_recorder: list[None],
    stub_sessions: None,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(daemon, "SwayIpcAdapter", lambda: StubSubscribingSway())

    log_path = tmp_path / "debug.log"

    exit_code = daemon.main([])

    assert exit_code == 1
    assert not log_path.exists()
    assert len(regen_recorder) == 1


def test_daemon_exits_when_global_config_is_invalid(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A malformed `~/.config/hop/config.toml` aborts hopd before sway IPC
    setup. The error surfaces to stderr so sway's log captures it."""
    config_path = tmp_path / "xdg-config" / "hop" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("[backends.bad]\nunknown_field = 1\n")

    exit_code = daemon.main([])

    assert exit_code == 1
    assert "hopd: failed to load config:" in capsys.readouterr().err


def test_sweep_stale_persisted_sessions_forgets_sessions_with_no_live_workspace(
    tmp_path: Path,
) -> None:
    from hop.daemon import sweep_stale_persisted_sessions
    from hop.state import CommandBackendRecord, SessionState

    host_record = CommandBackendRecord(name="host", interactive_prefix="", noninteractive_prefix="")

    class _SwayWithWorkspaces:
        def list_session_workspaces(self, *, prefix: str = "p:") -> tuple[str, ...]:
            del prefix
            # Only `live` is on the sway side; `stale` was destroyed.
            return ("p:live",)

    forgotten: list[str] = []
    sessions = {
        "live": SessionState(name="live", project_root=tmp_path, backend=host_record),
        "stale": SessionState(name="stale", project_root=tmp_path, backend=host_record),
    }

    sweep_stale_persisted_sessions(
        sway=_SwayWithWorkspaces(),  # type: ignore[arg-type]
        sessions_loader=lambda: sessions,
        forget=forgotten.append,
    )

    assert forgotten == ["stale"]


# --- daemon-down signaling ------------------------------------------------


def test_daemon_writes_daemon_down_entry_when_config_load_fails(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A malformed global config aborts hopd before sway setup. The user
    should see "daemon stopped — restart" in vicinae instead of stale
    hop-* entries from the previous successful run."""
    config_path = tmp_path / "xdg-config" / "hop" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("[backends.bad]\nunknown_field = 1\n")

    # Pre-existing hop-* entries from a previous successful run.
    scripts_dir = tmp_path / "xdg" / "vicinae" / "scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "hop-switch-old").write_text("stale")

    exit_code = daemon.main([])

    assert exit_code == 1
    remaining = sorted(p.name for p in scripts_dir.iterdir())
    assert remaining == ["hop-_daemon-down"]
    content = (scripts_dir / "hop-_daemon-down").read_text()
    assert "unknown_field" in content
    # stderr still carries the error for sway's log.
    assert "hopd: failed to load config:" in capsys.readouterr().err


def test_daemon_writes_daemon_down_entry_on_hop_error_during_subscription(
    monkeypatch: pytest.MonkeyPatch,
    regen_recorder: list[None],
    stub_sessions: None,
    tmp_path: Path,
) -> None:
    sway = StubSubscribingSway(
        events=(),
        raise_on_subscribe=SwaySubscriptionError("Sway refused subscription"),
    )
    monkeypatch.setattr(daemon, "SwayIpcAdapter", lambda: sway)

    scripts_dir = tmp_path / "xdg" / "vicinae" / "scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "hop-kill").write_text("stale")

    exit_code = daemon.main([])

    assert exit_code == 1
    remaining = sorted(p.name for p in scripts_dir.iterdir())
    assert remaining == ["hop-_daemon-down"]
    assert "Sway refused subscription" in (scripts_dir / "hop-_daemon-down").read_text()
    # Initial regen still ran before the subscription raised — fixture
    # captures regen attempts independently.
    assert len(regen_recorder) == 1


def test_daemon_writes_daemon_down_entry_on_unhandled_exception(
    monkeypatch: pytest.MonkeyPatch,
    stub_sessions: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Non-HopError crashes also trigger the daemon-down entry, with a
    short error message in the description (no full traceback)."""

    def boom(**_kwargs: Any) -> None:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(daemon, "regenerate", boom)
    monkeypatch.setattr(daemon, "SwayIpcAdapter", lambda: StubSubscribingSway())

    scripts_dir = tmp_path / "xdg" / "vicinae" / "scripts"

    exit_code = daemon.main([])

    assert exit_code == 1
    assert scripts_dir.is_dir()
    content = (scripts_dir / "hop-_daemon-down").read_text()
    assert "RuntimeError: kaboom" in content
    # Sanity: traceback still goes to stderr.
    assert "kaboom" in capsys.readouterr().err


def test_daemon_writes_daemon_down_entry_when_subscription_returns(
    monkeypatch: pytest.MonkeyPatch,
    regen_recorder: list[None],
    stub_sessions: None,
    tmp_path: Path,
) -> None:
    """Sway's subscription generator can return cleanly (rare, but it
    happens on a controlled sway exit). The daemon-down entry still shows
    up so the user knows hopd needs restarting."""
    monkeypatch.setattr(daemon, "SwayIpcAdapter", lambda: StubSubscribingSway(events=()))

    scripts_dir = tmp_path / "xdg" / "vicinae" / "scripts"

    exit_code = daemon.main([])

    assert exit_code == 1
    content = (scripts_dir / "hop-_daemon-down").read_text()
    assert "Sway IPC subscription ended" in content
    # Initial regen ran (= regen_recorder records exactly one call).
    assert len(regen_recorder) == 1


def test_daemon_swallows_daemon_down_write_errors(
    monkeypatch: pytest.MonkeyPatch,
    stub_sessions: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """If the daemon-down rewrite itself fails (read-only filesystem,
    permission error), swallow the error so we don't mask the original
    exception. The crash still surfaces through stderr and the debug log."""

    def boom(**_kwargs: Any) -> None:
        raise RuntimeError("original failure")

    def raise_on_write(scripts_dir: Path, *, error: BaseException) -> None:
        del scripts_dir, error
        raise OSError("read-only filesystem")

    monkeypatch.setattr(daemon, "regenerate", boom)
    monkeypatch.setattr(daemon, "write_daemon_down_script", raise_on_write)
    monkeypatch.setattr(daemon, "SwayIpcAdapter", lambda: StubSubscribingSway())

    exit_code = daemon.main([])

    # The original exception still drives the exit code; the suppressed
    # write error doesn't change anything else.
    assert exit_code == 1
    stderr = capsys.readouterr().err
    assert "kaboom" not in stderr  # different message — sanity check
    assert "original failure" in stderr
