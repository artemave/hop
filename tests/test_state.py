import json
from pathlib import Path

import pytest

from hop.session import ProjectSession
from hop.state import (
    CommandBackendRecord,
    SessionState,
    default_sessions_dir,
    forget_session,
    load_sessions,
    record_session,
)


def _host_record() -> CommandBackendRecord:
    """Hop's built-in ``host`` record — empty prefixes, no other fields."""
    return CommandBackendRecord(name="host", interactive_prefix="", noninteractive_prefix="")


def make_session(*, name: str, project_root: Path) -> ProjectSession:
    return ProjectSession(
        session_name=name,
        project_root=project_root,
        workspace_name=f"p:{name}",
    )


def test_record_session_writes_host_payload(tmp_path: Path) -> None:
    """``record_session`` with no backend uses hop's built-in host record:
    a regular command record with name=host and empty prefixes."""
    sessions_dir = tmp_path / "sessions"
    session = make_session(name="demo", project_root=tmp_path / "demo")

    record_session(session, sessions_dir=sessions_dir)

    payload = json.loads((sessions_dir / "demo.json").read_text())
    assert payload == {
        "name": "demo",
        "project_root": str(tmp_path / "demo"),
        "backend": {
            "type": "command",
            "name": "host",
            "interactive_prefix": "",
            "noninteractive_prefix": "",
        },
    }


def test_record_session_persists_command_backend_record(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    session = make_session(name="demo", project_root=tmp_path / "demo")

    record_session(
        session,
        backend=CommandBackendRecord(
            name="devcontainer",
            interactive_prefix="podman-compose -f docker-compose.dev.yml exec devcontainer",
            prepare="podman-compose up -d devcontainer",
            teardown="podman-compose down",
            noninteractive_prefix="podman-compose -f docker-compose.dev.yml exec -T devcontainer",
        ),
        sessions_dir=sessions_dir,
    )

    payload = json.loads((sessions_dir / "demo.json").read_text())
    assert payload["backend"] == {
        "type": "command",
        "name": "devcontainer",
        "interactive_prefix": "podman-compose -f docker-compose.dev.yml exec devcontainer",
        "prepare": "podman-compose up -d devcontainer",
        "teardown": "podman-compose down",
        "noninteractive_prefix": "podman-compose -f docker-compose.dev.yml exec -T devcontainer",
    }


def test_record_session_omits_optional_fields(tmp_path: Path) -> None:
    """Optional lifecycle / translate fields are dropped from the JSON when
    unset; required fields (name + both prefixes) always appear."""
    sessions_dir = tmp_path / "sessions"
    session = make_session(name="demo", project_root=tmp_path / "demo")

    record_session(
        session,
        backend=CommandBackendRecord(name="ssh", interactive_prefix="ssh host", noninteractive_prefix="ssh host"),
        sessions_dir=sessions_dir,
    )

    payload = json.loads((sessions_dir / "demo.json").read_text())
    assert payload["backend"] == {
        "type": "command",
        "name": "ssh",
        "interactive_prefix": "ssh host",
        "noninteractive_prefix": "ssh host",
    }


def test_record_session_persists_translate_commands(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    session = make_session(name="demo", project_root=tmp_path / "demo")

    record_session(
        session,
        backend=CommandBackendRecord(
            name="devcontainer",
            interactive_prefix="compose exec devcontainer",
            noninteractive_prefix="compose exec -T devcontainer",
            port_translate_command="compose port devcontainer {port}",
            host_translate_command="echo myserver",
        ),
        sessions_dir=sessions_dir,
    )

    payload = json.loads((sessions_dir / "demo.json").read_text())
    assert payload["backend"]["port_translate_command"] == "compose port devcontainer {port}"
    assert payload["backend"]["host_translate_command"] == "echo myserver"


def test_record_session_round_trips_workspace_path(tmp_path: Path) -> None:
    """``workspace_path`` (cached ``<noninteractive_prefix> pwd`` result) is
    persisted on bootstrap and restored on load — that's what lets the
    open-selection kitten fall back to the backend's default cwd when the
    in-shell shell isn't emitting OSC 7."""
    sessions_dir = tmp_path / "sessions"
    session = make_session(name="demo", project_root=tmp_path / "demo")

    record_session(
        session,
        backend=CommandBackendRecord(
            name="devcontainer",
            interactive_prefix="compose exec devcontainer",
            noninteractive_prefix="compose exec -T devcontainer",
            workspace_path="/workspace",
        ),
        sessions_dir=sessions_dir,
    )

    payload = json.loads((sessions_dir / "demo.json").read_text())
    assert payload["backend"]["workspace_path"] == "/workspace"

    loaded = load_sessions(sessions_dir=sessions_dir)
    assert loaded["demo"].backend.workspace_path == "/workspace"


def test_forget_session_removes_state_file(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    state_file = sessions_dir / "demo.json"
    state_file.write_text("{}")

    forget_session("demo", sessions_dir=sessions_dir)

    assert not state_file.exists()


def test_forget_session_is_idempotent(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    forget_session("demo", sessions_dir=sessions_dir)


def test_load_sessions_returns_empty_when_dir_missing(tmp_path: Path) -> None:
    assert load_sessions(sessions_dir=tmp_path / "missing") == {}


def test_load_sessions_decodes_command_backend_record(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "alpha.json").write_text(
        json.dumps(
            {
                "name": "alpha",
                "project_root": "/projects/alpha",
                "backend": {
                    "type": "command",
                    "name": "devcontainer",
                    "interactive_prefix": "compose exec devcontainer",
                    "prepare": "compose up -d devcontainer",
                    "teardown": "compose down",
                    "noninteractive_prefix": "compose exec -T devcontainer",
                },
            }
        )
    )

    sessions = load_sessions(sessions_dir=sessions_dir)

    assert sessions["alpha"].backend == CommandBackendRecord(
        name="devcontainer",
        interactive_prefix="compose exec devcontainer",
        prepare="compose up -d devcontainer",
        teardown="compose down",
        noninteractive_prefix="compose exec -T devcontainer",
    )


def test_load_sessions_silently_drops_legacy_workspace_keys(tmp_path: Path) -> None:
    """Old session JSONs may carry the deleted ``workspace_command`` /
    ``workspace_path`` keys *and* lack the required
    ``noninteractive_prefix``; the decoder treats them as stale and
    decodes as the built-in host so the next entry re-bootstraps fresh."""

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "alpha.json").write_text(
        json.dumps(
            {
                "name": "alpha",
                "project_root": "/projects/alpha",
                "backend": {
                    "type": "command",
                    "name": "devcontainer",
                    "interactive_prefix": "compose exec devcontainer",
                    "workspace_command": "compose exec devcontainer pwd",
                    "workspace_path": "/workspace",
                },
            }
        )
    )

    sessions = load_sessions(sessions_dir=sessions_dir)

    assert sessions["alpha"].backend == _host_record()


def test_load_sessions_falls_back_to_host_record_for_legacy_host_type(tmp_path: Path) -> None:
    """Old persisted state used a separate ``{"type": "host"}`` record; the
    new shape has a single record type with empty prefixes for the
    built-in host. Legacy records decode as that built-in host."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "alpha.json").write_text(
        json.dumps(
            {
                "name": "alpha",
                "project_root": "/projects/alpha",
                "backend": {"type": "host"},
            }
        )
    )

    sessions = load_sessions(sessions_dir=sessions_dir)

    assert sessions["alpha"].backend == _host_record()


def test_load_sessions_skips_non_json_and_wrong_shape_files(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "alpha.json").write_text(json.dumps({"name": "alpha", "project_root": "/projects/alpha"}))
    (sessions_dir / "beta.txt").write_text("not json")
    (sessions_dir / "wrong-shape.json").write_text(json.dumps({"name": 1, "project_root": "/x"}))

    sessions = load_sessions(sessions_dir=sessions_dir)

    assert sessions == {
        "alpha": SessionState(
            name="alpha",
            project_root=Path("/projects/alpha"),
            backend=_host_record(),
        )
    }


def test_load_sessions_raises_on_malformed_json(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "broken.json").write_text("{not valid json")

    with pytest.raises(json.JSONDecodeError):
        load_sessions(sessions_dir=sessions_dir)


def test_load_sessions_drops_optional_command_fields_when_not_strings(tmp_path: Path) -> None:
    """Non-string values in optional command fields decode as None. Required
    fields (the two prefixes) are still respected — if any non-string value
    appears there, the whole record falls back to host."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "alpha.json").write_text(
        json.dumps(
            {
                "name": "alpha",
                "project_root": "/projects/alpha",
                "backend": {
                    "type": "command",
                    "name": "devcontainer",
                    "interactive_prefix": "compose exec devcontainer",
                    "noninteractive_prefix": "compose exec -T devcontainer",
                    "prepare": ["legacy", "list"],
                    "teardown": None,
                    "port_translate_command": 42,
                },
            }
        )
    )

    sessions = load_sessions(sessions_dir=sessions_dir)

    assert sessions["alpha"].backend == CommandBackendRecord(
        name="devcontainer",
        interactive_prefix="compose exec devcontainer",
        noninteractive_prefix="compose exec -T devcontainer",
        prepare=None,
        teardown=None,
        port_translate_command=None,
    )


def test_load_sessions_falls_back_to_host_for_legacy_windows_array(tmp_path: Path) -> None:
    """Pre-redesign records persisted a `windows` array on the command record
    without the required two-prefixes shape. The decoder treats them as stale
    and falls back to the built-in host so the next entry re-bootstraps fresh."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "alpha.json").write_text(
        json.dumps(
            {
                "name": "alpha",
                "project_root": "/projects/alpha",
                "backend": {
                    "type": "command",
                    "name": "devcontainer",
                    "windows": [
                        {"role": "shell", "command": "zsh", "autostart": "true"},
                    ],
                },
            }
        )
    )

    sessions = load_sessions(sessions_dir=sessions_dir)

    assert sessions["alpha"].backend == _host_record()


def test_load_sessions_falls_back_to_host_for_command_record_without_string_name(tmp_path: Path) -> None:
    """A `type=command` record missing a string `name` field can't be turned
    into a usable backend; treat it as stale and decode as host so the next
    entry re-bootstraps fresh."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "alpha.json").write_text(
        json.dumps(
            {
                "name": "alpha",
                "project_root": "/projects/alpha",
                "backend": {
                    "type": "command",
                    # name is missing entirely.
                    "interactive_prefix": "compose exec devcontainer",
                },
            }
        )
    )

    sessions = load_sessions(sessions_dir=sessions_dir)

    assert sessions["alpha"].backend == _host_record()


def test_load_sessions_falls_back_to_host_for_legacy_flat_record(tmp_path: Path) -> None:
    """A pre-windows record with flat shell/editor fields and no prefixes
    decodes as the built-in host record; the next session entry
    re-bootstraps fresh state."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "alpha.json").write_text(
        json.dumps(
            {
                "name": "alpha",
                "project_root": "/projects/alpha",
                "backend": {
                    "type": "command",
                    "name": "devcontainer",
                    "shell": "zsh",
                    "editor": "nvim",
                },
            }
        )
    )

    sessions = load_sessions(sessions_dir=sessions_dir)

    assert sessions["alpha"].backend == _host_record()


def test_default_sessions_dir_honors_explicit_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOP_SESSIONS_DIR", "/custom/sessions")
    assert default_sessions_dir() == Path("/custom/sessions")


def test_default_sessions_dir_prefers_xdg_runtime_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOP_SESSIONS_DIR", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    assert default_sessions_dir() == Path("/run/user/1000/hop/sessions")


def test_default_sessions_dir_falls_back_to_tmp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOP_SESSIONS_DIR", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert default_sessions_dir() == Path("/tmp/hop/sessions")
