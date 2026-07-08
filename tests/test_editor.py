from pathlib import Path
from typing import Mapping, Sequence

from hop.editor import IpcKittyEditorIO, SharedNeovimEditorAdapter
from hop.kitty import session_socket_address
from hop.session import ProjectSession
from hop.sway import SwayWindow

NORMAL_MODE = "\x1b"
CR = "\r"


class StubKittyTransport:
    """Records IPC commands and replays a scripted ``ls`` response.

    ``ls_response`` is the kitty response payload for ``ls`` — a list of OS
    windows. Other commands return ``{"ok": True}``.
    """

    def __init__(self, ls_response: object | None = None) -> None:
        self._ls_response = ls_response
        self.commands: list[tuple[str, Mapping[str, object] | None]] = []

    def send_command(self, command_name: str, payload: Mapping[str, object] | None = None) -> object:
        self.commands.append((command_name, payload))
        if command_name == "ls":
            return self._ls_response
        return {"ok": True}


class TransportFactory:
    """Per-session transport factory. Tests assert against the recorded
    command stream of the per-session transport."""

    def __init__(self, *, ls_response: object | None = None) -> None:
        self._ls_response = ls_response
        self.transports: dict[str, StubKittyTransport] = {}

    def __call__(self, listen_on: str) -> StubKittyTransport:
        if listen_on not in self.transports:
            self.transports[listen_on] = StubKittyTransport(ls_response=self._ls_response)
        return self.transports[listen_on]

    def for_session(self, session_name: str) -> StubKittyTransport:
        return self.transports[session_socket_address(session_name)]


class StubSwayAdapter:
    def __init__(self, windows: Sequence[SwayWindow] = ()) -> None:
        self._windows: tuple[SwayWindow, ...] = tuple(windows)
        self.focused: list[int] = []

    def list_windows(self) -> Sequence[SwayWindow]:
        return self._windows

    def focus_window(self, window_id: int) -> None:
        self.focused.append(window_id)


class StubTerminalAdapter:
    def __init__(self) -> None:
        self.ensured: list[str] = []

    def ensure_terminal(self, session: ProjectSession, *, role: str, already_prepared: bool = False) -> None:
        del session, already_prepared
        self.ensured.append(role)


def build_session() -> ProjectSession:
    session_root = Path("/tmp/demo").resolve()
    return ProjectSession(
        session_root=session_root,
        session_name="demo",
        workspace_name=f"p:{session_root}",
    )


def build_editor_window(window_id: int) -> SwayWindow:
    return SwayWindow(
        id=window_id,
        workspace_name="p:/tmp/demo",
        app_id="hop:editor",
        window_class=None,
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
    terminals: StubTerminalAdapter | None = None,
) -> SharedNeovimEditorAdapter:
    return SharedNeovimEditorAdapter(
        kitty_io=IpcKittyEditorIO(transport_factory=factory),
        terminals=terminals,
        sway=sway,
    )


def test_open_target_sends_drop_keystrokes_to_editor_window() -> None:
    factory = TransportFactory(ls_response=make_ls_response(kitty_window_id=77))
    sway = StubSwayAdapter([build_editor_window(31)])
    adapter = make_adapter(sway=sway, factory=factory)

    adapter.open_target(build_session(), target="app/models/user.rb:42")

    transport = factory.for_session("demo")
    # IPC sequence: ls (find editor's kitty window id), then send-text.
    assert [name for name, _ in transport.commands] == ["ls", "send-text"]
    _, send_payload = transport.commands[1]
    assert send_payload is not None
    # send-text matches by id (the one ls returned).
    assert send_payload["match"] == "id:77"
    assert send_payload["data"] == (f"text:{NORMAL_MODE}:exec 'drop '.fnameescape('app/models/user.rb'){CR}:42{CR}")
    assert sway.focused == [31]


def test_open_target_brings_up_the_editor_role_terminal() -> None:
    """CLI path: ``open_target`` ensures the editor role terminal exists
    (shell + typed-in ``nvim``, or focus the existing one) before typing the
    open-file keystrokes into it — just like `hop term --role editor`."""
    factory = TransportFactory(ls_response=make_ls_response(kitty_window_id=77))
    sway = StubSwayAdapter([build_editor_window(31)])
    terminals = StubTerminalAdapter()
    adapter = make_adapter(sway=sway, factory=factory, terminals=terminals)

    adapter.open_target(build_session(), target="app/models/user.rb")

    assert terminals.ensured == ["editor"]
    assert sway.focused == [31]


def test_open_target_without_a_terminal_adapter_does_not_launch() -> None:
    """Boss (kitten) path: no terminal adapter is wired because we can't
    launch without blocking kitty's loop. The editor must already be running;
    ``open_target`` only focuses it and types the keystrokes in."""
    factory = TransportFactory(ls_response=make_ls_response(kitty_window_id=77))
    sway = StubSwayAdapter([build_editor_window(31)])
    adapter = make_adapter(sway=sway, factory=factory, terminals=None)

    adapter.open_target(build_session(), target="app/models/user.rb")

    transport = factory.for_session("demo")
    # No launch — just find-and-send.
    assert [name for name, _ in transport.commands] == ["ls", "send-text"]
    assert sway.focused == [31]


def test_open_target_skips_sway_focus_when_no_editor_window_visible() -> None:
    factory = TransportFactory(ls_response=make_ls_response(kitty_window_id=77))
    sway = StubSwayAdapter([])
    adapter = make_adapter(sway=sway, factory=factory)

    adapter.open_target(build_session(), target="app/models/user.rb")

    assert sway.focused == []


def test_open_target_ignores_editor_windows_on_other_workspaces() -> None:
    factory = TransportFactory(ls_response=make_ls_response(kitty_window_id=77))
    other = SwayWindow(id=31, workspace_name="p:other", app_id="hop:editor", window_class=None)
    sway = StubSwayAdapter([other])
    adapter = make_adapter(sway=sway, factory=factory)

    adapter.open_target(build_session(), target="app/models/user.rb")

    assert sway.focused == []


def test_open_target_matches_editor_via_x11_window_class_fallback() -> None:
    factory = TransportFactory(ls_response=make_ls_response(kitty_window_id=77))
    xwayland_editor = SwayWindow(id=31, workspace_name="p:/tmp/demo", app_id=None, window_class="hop:editor")
    sway = StubSwayAdapter([xwayland_editor])
    adapter = make_adapter(sway=sway, factory=factory)

    adapter.open_target(build_session(), target="app/models/user.rb")

    assert sway.focused == [31]


def test_open_target_doubles_single_quotes_for_vim_string_literal() -> None:
    factory = TransportFactory(ls_response=make_ls_response(kitty_window_id=77))
    sway = StubSwayAdapter([build_editor_window(31)])
    adapter = make_adapter(sway=sway, factory=factory)

    adapter.open_target(build_session(), target="app/models/user's file.rb")

    transport = factory.for_session("demo")
    _, payload = transport.commands[-1]
    assert payload is not None
    # Vim's single-quoted strings escape an embedded `'` by doubling it.
    assert payload["data"] == (f"text:{NORMAL_MODE}:exec 'drop '.fnameescape('app/models/user''s file.rb'){CR}")


def test_open_target_omits_line_jump_when_target_has_no_line_suffix() -> None:
    factory = TransportFactory(ls_response=make_ls_response(kitty_window_id=77))
    sway = StubSwayAdapter([build_editor_window(31)])
    adapter = make_adapter(sway=sway, factory=factory)

    adapter.open_target(build_session(), target="app/models/user.rb")

    transport = factory.for_session("demo")
    _, payload = transport.commands[-1]
    assert payload is not None
    assert payload["data"] == (f"text:{NORMAL_MODE}:exec 'drop '.fnameescape('app/models/user.rb'){CR}")


def test_open_target_passes_path_through_to_nvim_unchanged() -> None:
    """The editor adapter no longer rewrites the target — paths arrive in
    the active backend's namespace (because the kitten resolved them against
    the source window's in-shell cwd and asked the backend whether they
    exist). The adapter just splits off the optional line suffix and hands
    everything to nvim's :drop unchanged."""
    factory = TransportFactory(ls_response=make_ls_response(kitty_window_id=77))
    sway = StubSwayAdapter([build_editor_window(31)])
    adapter = make_adapter(sway=sway, factory=factory)

    adapter.open_target(build_session(), target="/workspace/lib/foo.py:42")

    transport = factory.for_session("demo")
    _, payload = transport.commands[-1]
    assert payload is not None
    data = payload["data"]
    assert isinstance(data, str)
    assert "/workspace/lib/foo.py" in data
    assert ":42" in data
