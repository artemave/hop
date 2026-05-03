from pathlib import Path
from typing import Any, Mapping, Sequence, cast

import pytest

from hop.editor import IpcKittyEditorIO, SharedNeovimEditorAdapter
from hop.kitty import HOP_ROLE_VAR, session_socket_address
from hop.session import ProjectSession
from hop.sway import SwayWindow

NORMAL_MODE = "\x1b"
CR = "\r"


class StubKittyTransport:
    """Records IPC commands and replays scripted ``ls`` responses.

    ``ls_response`` is the kitty response payload for ``ls`` — a list of OS
    windows. Other commands return ``{"ok": True}``.
    """

    def __init__(
        self,
        ls_response: object | None = None,
        on_launch: object = None,
    ) -> None:
        self._ls_response = ls_response
        self._on_launch = on_launch
        self.commands: list[tuple[str, Mapping[str, object] | None]] = []

    def send_command(self, command_name: str, payload: Mapping[str, object] | None = None) -> object:
        self.commands.append((command_name, payload))
        if command_name == "launch" and callable(self._on_launch) and payload is not None:
            self._on_launch(dict(payload))
        if command_name == "ls":
            return self._ls_response
        return {"ok": True}


class TransportFactory:
    """Per-session transport factory. Tests assert against the recorded
    command stream of the per-session transport."""

    def __init__(
        self,
        *,
        ls_response: object | None = None,
        on_launch: object = None,
    ) -> None:
        self._ls_response = ls_response
        self._on_launch = on_launch
        self.transports: dict[str, StubKittyTransport] = {}

    def __call__(self, listen_on: str) -> StubKittyTransport:
        if listen_on not in self.transports:
            self.transports[listen_on] = StubKittyTransport(
                ls_response=self._ls_response,
                on_launch=self._on_launch,
            )
        return self.transports[listen_on]

    def for_session(self, session_name: str) -> StubKittyTransport:
        return self.transports[session_socket_address(session_name)]


class StubSwayAdapter:
    def __init__(self, windows: Sequence[SwayWindow] = ()) -> None:
        self._windows: list[SwayWindow] = list(windows)
        self.focused: list[int] = []
        self.marked: list[tuple[int, str]] = []
        self.moved: list[tuple[int, str]] = []

    def list_windows(self) -> Sequence[SwayWindow]:
        return tuple(self._windows)

    def focus_window(self, window_id: int) -> None:
        self.focused.append(window_id)

    def mark_window(self, window_id: int, mark: str) -> None:
        self.marked.append((window_id, mark))
        self._windows = [
            SwayWindow(
                id=window.id,
                workspace_name=window.workspace_name,
                app_id=window.app_id,
                window_class=window.window_class,
                marks=window.marks + (mark,),
                focused=window.focused,
            )
            if window.id == window_id
            else window
            for window in self._windows
        ]

    def move_window_to_workspace(self, window_id: int, workspace_name: str) -> None:
        self.moved.append((window_id, workspace_name))
        self._windows = [
            SwayWindow(
                id=window.id,
                workspace_name=workspace_name,
                app_id=window.app_id,
                window_class=window.window_class,
                marks=window.marks,
                focused=window.focused,
            )
            if window.id == window_id
            else window
            for window in self._windows
        ]

    def add_window(self, window: SwayWindow) -> None:
        self._windows.append(window)


def build_session() -> ProjectSession:
    project_root = Path("/tmp/demo").resolve()
    return ProjectSession(
        project_root=project_root,
        session_name="demo",
        workspace_name=f"p:{project_root}",
    )


def build_marked_editor_window(window_id: int, *, mark: str = "_hop_editor:demo") -> SwayWindow:
    return SwayWindow(
        id=window_id,
        workspace_name="p:/tmp/demo",
        app_id="hop:editor",
        window_class=None,
        marks=(mark,),
    )


def make_ls_response(*, kitty_window_id: int) -> list[dict[str, object]]:
    """Synthesize a kitty ``ls`` payload with one editor OS window containing
    one inner window with the given id."""
    return [
        {
            "wm_class": "hop:editor",
            "tabs": [
                {"windows": [{"id": kitty_window_id}]},
            ],
        }
    ]


def make_adapter(
    *,
    sway: StubSwayAdapter,
    factory: TransportFactory,
    session_backend_for: Any = None,
    ready_timeout_seconds: float = 5.0,
    ready_poll_interval_seconds: float = 0.05,
) -> SharedNeovimEditorAdapter:
    return SharedNeovimEditorAdapter(
        sway=sway,
        kitty_io=IpcKittyEditorIO(transport_factory=factory),
        session_backend_for=session_backend_for,
        ready_timeout_seconds=ready_timeout_seconds,
        ready_poll_interval_seconds=ready_poll_interval_seconds,
    )


def test_ensure_returns_false_when_editor_already_running() -> None:
    """ensure() reports whether it had to launch a new editor. With a
    marked window already present, it must return False so callers
    (spawn_session_terminal) know they can fall through to spawning a
    shell rather than treating this as a resurrection."""
    factory = TransportFactory()
    sway = StubSwayAdapter([build_marked_editor_window(23)])
    adapter = make_adapter(sway=sway, factory=factory)

    was_launched = adapter.ensure(build_session())

    assert was_launched is False
    # No focus shift on ensure() — that's the whole point vs focus().
    assert sway.focused == []
    # And no kitty IPC: if we already have a window, we don't ping kitty.
    assert factory.transports == {}


def test_ensure_returns_true_when_editor_was_just_launched() -> None:
    """ensure() returns True when it has to spawn a fresh editor
    window. spawn_session_terminal short-circuits on True so the user
    gets exactly one window back — the editor — instead of editor + a
    new shell."""
    sway = StubSwayAdapter()

    def on_launch(_payload: dict[str, object]) -> None:
        sway.add_window(
            SwayWindow(
                id=101,
                workspace_name=build_session().workspace_name,
                app_id="hop:editor",
                window_class=None,
            )
        )

    factory = TransportFactory(on_launch=on_launch)
    adapter = make_adapter(sway=sway, factory=factory)

    was_launched = adapter.ensure(build_session())

    assert was_launched is True
    # ensure() doesn't call sway.focus_window — the freshly spawned shell
    # alongside should keep focus, kitty's keep_focus=True takes care of it.
    assert sway.focused == []
    transport = factory.for_session("demo")
    assert len(transport.commands) == 1
    name, payload = transport.commands[0]
    assert name == "launch"
    assert payload is not None
    assert payload["keep_focus"] is True


def test_focus_reuses_existing_editor_window_without_relaunch() -> None:
    factory = TransportFactory()
    sway = StubSwayAdapter([build_marked_editor_window(23)])
    adapter = make_adapter(sway=sway, factory=factory)

    adapter.focus(build_session())

    # No launch needed; focus alone via Sway. No kitty IPC at all.
    assert factory.transports == {}
    assert sway.focused == [23]
    assert sway.marked == []


def test_focus_launches_editor_when_window_missing() -> None:
    sway = StubSwayAdapter()

    def on_launch(_payload: dict[str, object]) -> None:
        sway.add_window(
            SwayWindow(
                id=101,
                workspace_name=build_session().workspace_name,
                app_id="hop:editor",
                window_class=None,
            )
        )

    factory = TransportFactory(on_launch=on_launch)
    adapter = make_adapter(sway=sway, factory=factory)

    adapter.focus(build_session())

    transport = factory.for_session("demo")
    assert len(transport.commands) == 1
    name, payload = transport.commands[0]
    assert name == "launch"
    assert payload is not None
    assert payload["args"] == ["sh", "-c", "nvim; ${SHELL:-sh}"]
    assert payload["os_window_class"] == "hop:editor"
    assert payload["var"] == [f"{HOP_ROLE_VAR}=editor"]

    assert sway.marked == [(101, "_hop_editor:demo")]
    assert sway.focused == [101]


def test_open_target_sends_drop_keystrokes_to_editor_window() -> None:
    factory = TransportFactory(ls_response=make_ls_response(kitty_window_id=77))
    sway = StubSwayAdapter([build_marked_editor_window(31)])
    adapter = make_adapter(sway=sway, factory=factory)

    adapter.open_target(build_session(), target="app/models/user.rb:42")

    transport = factory.for_session("demo")
    # IPC sequence: ls (find editor's kitty window id), then send-text.
    assert [name for name, _ in transport.commands] == ["ls", "send-text"]
    _, send_payload = transport.commands[1]
    assert send_payload is not None
    # send-text matches by id (the one ls returned), not by var.
    assert send_payload["match"] == "id:77"
    assert send_payload["data"] == (f"text:{NORMAL_MODE}:exec 'drop '.fnameescape('app/models/user.rb'){CR}:42{CR}")
    assert sway.focused == [31]


def test_open_target_doubles_single_quotes_for_vim_string_literal() -> None:
    factory = TransportFactory(ls_response=make_ls_response(kitty_window_id=77))
    sway = StubSwayAdapter([build_marked_editor_window(31)])
    adapter = make_adapter(sway=sway, factory=factory)

    adapter.open_target(build_session(), target="app/models/user's file.rb")

    transport = factory.for_session("demo")
    _, payload = transport.commands[-1]
    assert payload is not None
    # Vim's single-quoted strings escape an embedded `'` by doubling it.
    assert payload["data"] == (f"text:{NORMAL_MODE}:exec 'drop '.fnameescape('app/models/user''s file.rb'){CR}")


def test_open_target_omits_line_jump_when_target_has_no_line_suffix() -> None:
    factory = TransportFactory(ls_response=make_ls_response(kitty_window_id=77))
    sway = StubSwayAdapter([build_marked_editor_window(31)])
    adapter = make_adapter(sway=sway, factory=factory)

    adapter.open_target(build_session(), target="app/models/user.rb")

    transport = factory.for_session("demo")
    _, payload = transport.commands[-1]
    assert payload is not None
    assert payload["data"] == (f"text:{NORMAL_MODE}:exec 'drop '.fnameescape('app/models/user.rb'){CR}")


def test_open_target_translates_host_path_via_backend(tmp_path: Path) -> None:
    """For backends whose nvim runs in a different filesystem (e.g. devcontainer),
    `:drop <host_path>` would fail. The editor adapter must rewrite the path via
    the backend's translate_host_path before sending the keystrokes."""
    project_root = build_session().project_root

    factory = TransportFactory(ls_response=make_ls_response(kitty_window_id=77))
    sway = StubSwayAdapter([build_marked_editor_window(31)])

    class FakeBackend:
        def translate_host_path(self, _session: ProjectSession, host_path: Path) -> Path:
            try:
                relative = host_path.relative_to(project_root)
            except ValueError:
                return host_path
            return Path("/workspace") / relative

    adapter = make_adapter(
        sway=sway,
        factory=factory,
        session_backend_for=lambda _session: FakeBackend(),  # type: ignore[arg-type]
    )

    adapter.open_target(build_session(), target=str(project_root / "lib/foo.py:42"))

    transport = factory.for_session("demo")
    _, payload = transport.commands[-1]
    assert payload is not None
    data = payload["data"]
    assert isinstance(data, str)
    assert "/workspace/lib/foo.py" in data
    assert str(project_root) not in data


def test_launch_composes_editor_then_shell_through_backend_inline() -> None:
    """The editor adapter composes `<editor>; <shell>` so the kitty window
    stays usable after the editor exits. Each piece goes through
    backend.inline so the prefix wraps each one individually — preserving
    the today's two-call exec behavior for prefix backends."""
    sway = StubSwayAdapter()
    captured: list[list[Any]] = []

    def on_launch(payload: dict[str, object]) -> None:
        args = payload["args"]
        assert isinstance(args, list)
        captured.append(cast(list[Any], args))
        sway.add_window(
            SwayWindow(
                id=200,
                workspace_name=build_session().workspace_name,
                app_id="hop:editor",
                window_class=None,
            )
        )

    factory = TransportFactory(on_launch=on_launch)

    class FakeBackend:
        def inline(self, command: str, _session: ProjectSession) -> str:
            return f"podman-compose exec devcontainer {command}"

    adapter = make_adapter(
        sway=sway,
        factory=factory,
        session_backend_for=lambda _session: FakeBackend(),  # type: ignore[arg-type]
    )

    adapter.focus(build_session())

    assert captured == [
        [
            "sh",
            "-c",
            "podman-compose exec devcontainer nvim; podman-compose exec devcontainer ${SHELL:-sh}",
        ]
    ]


def test_focus_relocates_freshly_launched_editor_to_session_workspace() -> None:
    """When `hop edit` is invoked from outside the session, kitty creates the
    editor on whatever Sway workspace was focused at launch time. Hop must
    move it onto the session's workspace before focusing — otherwise the
    user lands in nvim on the wrong workspace and the kitten dispatch can't
    find the editor by Sway mark."""
    sway = StubSwayAdapter()

    def on_launch(_payload: dict[str, object]) -> None:
        sway.add_window(
            SwayWindow(
                id=101,
                workspace_name="p:other",  # caller's workspace, not the session's
                app_id="hop:editor",
                window_class=None,
            )
        )

    factory = TransportFactory(on_launch=on_launch)
    adapter = make_adapter(sway=sway, factory=factory)

    adapter.focus(build_session())

    assert sway.moved == [(101, build_session().workspace_name)]
    assert sway.marked == [(101, "_hop_editor:demo")]
    assert sway.focused == [101]


def test_focus_raises_after_launch_when_sway_window_never_appears() -> None:
    """If kitty's launch returns but Sway never registers the window (e.g.
    Wayland event lost, kitty failed silently), poll times out and we surface
    the failure instead of hanging."""
    sway = StubSwayAdapter()
    factory = TransportFactory()  # on_launch deliberately does not add a window

    adapter = make_adapter(
        sway=sway,
        factory=factory,
        ready_timeout_seconds=0.05,
        ready_poll_interval_seconds=0.01,
    )

    from hop.editor import NeovimCommandError

    with pytest.raises(NeovimCommandError, match="Sway did not register"):
        adapter.focus(build_session())
