# pyright: reportPrivateUsage=false

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

import pytest

from hop.editor import (
    BossKittyEditorIO,
    IpcKittyEditorIO,
    NeovimCommandError,
    SharedNeovimEditorAdapter,
    _build_open_keystrokes,
    _coerce_ls_payload,
    _split_target,
)
from hop.kitty import session_socket_address
from hop.session import ProjectSession
from hop.sway import SwayWindow

NORMAL_MODE = "\x1b"
CR = "\r"


class StubKittyTransport:
    def __init__(self, *, on_launch: object = None) -> None:
        self._on_launch = on_launch
        self.commands: list[tuple[str, Mapping[str, object] | None]] = []

    def send_command(self, command_name: str, payload: Mapping[str, object] | None = None) -> object:
        self.commands.append((command_name, payload))
        if command_name == "launch" and callable(self._on_launch) and payload is not None:
            self._on_launch(payload)
        return {"ok": True}


class TransportFactory:
    def __init__(self, *, on_launch: object = None) -> None:
        self._on_launch = on_launch
        self.transports: dict[str, StubKittyTransport] = {}

    def __call__(self, listen_on: str) -> StubKittyTransport:
        if listen_on not in self.transports:
            self.transports[listen_on] = StubKittyTransport(on_launch=self._on_launch)
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


def build_session() -> ProjectSession:
    project_root = Path("/tmp/demo").resolve()
    return ProjectSession(
        project_root=project_root,
        session_name="demo",
        workspace_name=f"p:{project_root}",
    )


def _marked_editor(window_id: int) -> SwayWindow:
    return SwayWindow(
        id=window_id,
        workspace_name=build_session().workspace_name,
        app_id="hop:editor",
        window_class=None,
        marks=("_hop_editor:demo",),
    )


def _unmarked_editor(window_id: int, *, app_id: str = "hop:editor") -> SwayWindow:
    return SwayWindow(
        id=window_id,
        workspace_name=build_session().workspace_name,
        app_id=app_id,
        window_class=None,
    )


# --- focus / window discovery ---------------------------------------------


def test_focus_picks_lowest_id_when_multiple_marked_editor_windows_exist() -> None:
    factory = TransportFactory()
    sway = StubSwayAdapter([_marked_editor(31), _marked_editor(29), _marked_editor(30)])
    adapter = SharedNeovimEditorAdapter(sway=sway, kitty_io=IpcKittyEditorIO(transport_factory=factory))

    adapter.focus(build_session())

    assert sway.focused == [29]
    assert sway.marked == []


def test_focus_marks_unmarked_editor_on_first_sighting() -> None:
    factory = TransportFactory()
    sway = StubSwayAdapter([_unmarked_editor(42)])
    adapter = SharedNeovimEditorAdapter(sway=sway, kitty_io=IpcKittyEditorIO(transport_factory=factory))

    adapter.focus(build_session())

    assert sway.marked == [(42, "_hop_editor:demo")]
    assert sway.focused == [42]


def test_focus_skips_unmarked_editor_belonging_to_a_different_session() -> None:
    """An editor window already marked for another session must not be adopted —
    even though the launch transport adds no new window, so the launched-then-
    not-found path raises."""
    foreign_editor = SwayWindow(
        id=42,
        workspace_name=build_session().workspace_name,
        app_id="hop:editor",
        window_class=None,
        marks=("_hop_editor:other",),
    )
    factory = TransportFactory()  # on_launch deliberately does not add a window
    sway = StubSwayAdapter([foreign_editor])
    adapter = SharedNeovimEditorAdapter(
        sway=sway,
        kitty_io=IpcKittyEditorIO(transport_factory=factory),
        ready_timeout_seconds=0.05,
        ready_poll_interval_seconds=0.01,
    )

    with pytest.raises(NeovimCommandError, match="Sway did not register"):
        adapter.focus(build_session())


def test_focus_matches_xwayland_editor_via_window_class() -> None:
    factory = TransportFactory()
    sway = StubSwayAdapter(
        [
            SwayWindow(
                id=42,
                workspace_name=build_session().workspace_name,
                app_id=None,
                window_class="hop:editor",
            )
        ]
    )
    adapter = SharedNeovimEditorAdapter(sway=sway, kitty_io=IpcKittyEditorIO(transport_factory=factory))

    adapter.focus(build_session())

    assert sway.marked == [(42, "_hop_editor:demo")]
    assert sway.focused == [42]


def test_open_target_raises_when_no_editor_window_after_launch() -> None:
    """If the editor window never appears in Sway after launch, the focus
    step has nothing to focus and raises rather than silently dropping the
    keystrokes."""
    factory = TransportFactory()  # on_launch deliberately does not add a window
    adapter = SharedNeovimEditorAdapter(
        sway=StubSwayAdapter(),
        kitty_io=IpcKittyEditorIO(transport_factory=factory),
        ready_timeout_seconds=0.05,
        ready_poll_interval_seconds=0.01,
    )

    with pytest.raises(NeovimCommandError, match="Sway did not register"):
        adapter.open_target(build_session(), target="README.md")


# --- keystroke building ---------------------------------------------------


def test_build_open_keystrokes_plain_path() -> None:
    assert _build_open_keystrokes("app/models/user.rb", None) == (
        f"{NORMAL_MODE}:exec 'drop '.fnameescape('app/models/user.rb'){CR}"
    )


def test_build_open_keystrokes_with_line_jump() -> None:
    assert _build_open_keystrokes("app/models/user.rb", 42) == (
        f"{NORMAL_MODE}:exec 'drop '.fnameescape('app/models/user.rb'){CR}:42{CR}"
    )


def test_build_open_keystrokes_doubles_single_quote() -> None:
    """Vim's single-quoted string syntax doubles internal apostrophes; that's
    the only escape needed inside the literal — backslashes and the rest pass
    through to ``fnameescape`` for vim to handle."""
    assert _build_open_keystrokes("a'b/c.rb", None) == (f"{NORMAL_MODE}:exec 'drop '.fnameescape('a''b/c.rb'){CR}")


# --- target splitting -----------------------------------------------------


def test_split_target_separates_line_suffix() -> None:
    assert _split_target("path/to/file.rb:42") == ("path/to/file.rb", 42)


def test_split_target_returns_path_only_when_no_line_suffix() -> None:
    assert _split_target("path/to/file.rb") == ("path/to/file.rb", None)


def test_split_target_leaves_non_numeric_suffix_alone() -> None:
    """Unix paths can contain `:` (rare), so treat trailing-colon-then-digits
    as the line marker but leave anything else as part of the path."""
    assert _split_target("weird:name") == ("weird:name", None)


# --- IpcKittyEditorIO error paths ----------------------------------------


class _RecordingTransport:
    def __init__(self, ls_response: object) -> None:
        self._ls_response = ls_response
        self.commands: list[tuple[str, Mapping[str, object] | None]] = []

    def send_command(self, command_name: str, payload: Mapping[str, object] | None = None) -> object:
        self.commands.append((command_name, payload))
        if command_name == "ls":
            return self._ls_response
        return {"ok": True}


def _ipc(ls_response: object) -> tuple[IpcKittyEditorIO, _RecordingTransport]:
    transport = _RecordingTransport(ls_response)
    io = IpcKittyEditorIO(transport_factory=lambda _addr: transport)
    return io, transport


def test_ipc_send_text_raises_when_ls_returns_no_editor_window() -> None:
    io, _ = _ipc(ls_response=[])
    with pytest.raises(NeovimCommandError, match="No editor kitty window"):
        io.send_text_to_editor(build_session(), "irrelevant")


def test_ipc_send_text_raises_when_only_non_editor_os_windows_exist() -> None:
    io, _ = _ipc(
        ls_response=[
            {"wm_class": "kitty", "tabs": [{"windows": [{"id": 1}]}]},
            {"wm_class": "hop:shell", "tabs": [{"windows": [{"id": 2}]}]},
        ]
    )
    with pytest.raises(NeovimCommandError, match="No editor kitty window"):
        io.send_text_to_editor(build_session(), "irrelevant")


def test_ipc_send_text_skips_malformed_ls_entries_and_finds_editor() -> None:
    io, transport = _ipc(
        ls_response=[
            "not-a-mapping",
            {"wm_class": "hop:editor", "tabs": ["not-a-mapping", {"windows": ["not-a-mapping", {"id": "not-int"}]}]},
            {"wm_class": "hop:editor", "tabs": [{"windows": [{"id": 99}]}]},
        ]
    )
    io.send_text_to_editor(build_session(), "hi")
    assert transport.commands[-1] == ("send-text", {"match": "id:99", "data": "text:hi"})


def test_coerce_ls_payload_returns_empty_for_non_list_response() -> None:
    """An unexpected response shape (e.g. error envelope) yields no os
    windows rather than crashing the caller."""
    assert _coerce_ls_payload({"data": 42}) == ()


def test_coerce_ls_payload_decodes_json_string_envelope() -> None:
    response = {"data": '[{"wm_class": "hop:editor", "tabs": []}]'}
    payload = _coerce_ls_payload(response)
    assert list(payload) == [{"wm_class": "hop:editor", "tabs": []}]


# --- BossKittyEditorIO ----------------------------------------------------


def _boss_window(window_id: int, *, user_vars: dict[str, str] | None = None, os_window_id: int = 1) -> Any:
    """Build a minimal duck-typed kitty Window for the boss."""
    sent: list[bytes] = []

    ns = SimpleNamespace(
        id=window_id,
        user_vars=user_vars or {},
        os_window_id=os_window_id,
        sent_bytes=sent,
    )

    def write_to_child(data: bytes) -> None:
        sent.append(data)

    ns.write_to_child = write_to_child  # type: ignore[attr-defined]
    return ns


def _boss(*, windows: list[Any], os_window_map: dict[int, Any] | None = None) -> Any:
    return SimpleNamespace(
        window_id_map={w.id: w for w in windows},
        os_window_map=os_window_map or {},
    )


def test_boss_send_text_uses_user_var_match_first() -> None:
    other = _boss_window(1, user_vars={"hop_role": "shell"})
    editor = _boss_window(2, user_vars={"hop_role": "editor"})
    boss = _boss(windows=[other, editor])
    io = BossKittyEditorIO(boss=boss)

    io.send_text_to_editor(build_session(), "hello")

    assert other.sent_bytes == []
    assert editor.sent_bytes == [b"hello"]


def test_boss_send_text_falls_back_to_wm_class_when_no_user_var() -> None:
    """An editor launched by an older hop version has the ``hop:editor`` os
    window class but no ``hop_role`` user var. The boss path must still find
    it via the os_window_map's wm_class so the user doesn't have to relaunch
    the editor after upgrading hop."""
    legacy_editor = _boss_window(1, user_vars={}, os_window_id=10)
    boss = _boss(
        windows=[legacy_editor],
        os_window_map={10: SimpleNamespace(wm_class="hop:editor")},
    )
    io = BossKittyEditorIO(boss=boss)

    io.send_text_to_editor(build_session(), "hi")

    assert legacy_editor.sent_bytes == [b"hi"]


def test_boss_send_text_raises_when_no_editor_window_in_boss() -> None:
    boss = _boss(windows=[_boss_window(1, user_vars={"hop_role": "shell"})])
    io = BossKittyEditorIO(boss=boss)

    with pytest.raises(NeovimCommandError, match="Run `hop edit`"):
        io.send_text_to_editor(build_session(), "irrelevant")


def test_boss_send_text_skips_os_windows_with_other_wm_class() -> None:
    """A window in a non-editor os window (e.g. a session shell window) is
    skipped during the wm_class fallback search."""
    shell_window = _boss_window(1, user_vars={}, os_window_id=10)
    legacy_editor = _boss_window(2, user_vars={}, os_window_id=20)
    boss = _boss(
        windows=[shell_window, legacy_editor],
        os_window_map={
            10: SimpleNamespace(wm_class="hop:shell"),
            20: SimpleNamespace(wm_class="hop:editor"),
        },
    )
    io = BossKittyEditorIO(boss=boss)

    io.send_text_to_editor(build_session(), "hi")

    assert shell_window.sent_bytes == []
    assert legacy_editor.sent_bytes == [b"hi"]


def test_boss_send_text_skips_windows_with_unknown_os_window_id() -> None:
    """A window whose os_window_id isn't in os_window_map falls through to
    the next candidate; no crash."""
    orphan = _boss_window(1, user_vars={}, os_window_id=99)
    legacy_editor = _boss_window(2, user_vars={}, os_window_id=10)
    boss = _boss(
        windows=[orphan, legacy_editor],
        os_window_map={10: SimpleNamespace(wm_class="hop:editor")},
    )
    io = BossKittyEditorIO(boss=boss)

    io.send_text_to_editor(build_session(), "hi")

    assert orphan.sent_bytes == []
    assert legacy_editor.sent_bytes == [b"hi"]


def test_boss_send_text_skips_windows_without_os_window_id() -> None:
    weird = _boss_window(1, user_vars={}, os_window_id=10)
    weird.os_window_id = None  # simulate a window detached from any os window
    boss = _boss(windows=[weird], os_window_map={10: SimpleNamespace(wm_class="hop:editor")})
    io = BossKittyEditorIO(boss=boss)

    with pytest.raises(NeovimCommandError, match="Run `hop edit`"):
        io.send_text_to_editor(build_session(), "hi")


def test_boss_launch_editor_raises_explaining_the_constraint() -> None:
    boss = _boss(windows=[])
    io = BossKittyEditorIO(boss=boss)

    with pytest.raises(NeovimCommandError, match="run `hop edit` from a shell"):
        io.launch_editor(
            build_session(),
            args=("nvim",),
            os_window_class="hop:editor",
            var=["hop_role=editor"],
        )
