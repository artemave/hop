from pathlib import Path
from typing import Mapping, Sequence

import pytest

from hop.kitty import (
    KittyCommandError,
    KittyConnectionError,
    KittyRemoteControlAdapter,
    KittyTransport,
    KittyWindowState,
    session_socket_address,
)
from hop.session import ProjectSession
from hop.sway import SwayWindow


class StubKittyFactory:
    """Records every (listen_on, command, payload) RPC against any stub transport
    it hands out, and returns canned responses in FIFO order. Each entry in
    ``responses`` may be a dict (which short-circuits the call) or a callable
    ``(listen_on, command, payload) -> response`` for selective behavior."""

    def __init__(
        self,
        responses: list[object | KittyConnectionError] | None = None,
    ) -> None:
        self.responses = list(responses or [])
        self.calls: list[tuple[str | None, str, Mapping[str, object] | None]] = []

    def __call__(self, listen_on: str | None = None) -> KittyTransport:
        return _StubTransport(listen_on, self)


class _StubTransport:
    def __init__(self, listen_on: str | None, factory: StubKittyFactory) -> None:
        self._listen_on = listen_on
        self._factory = factory

    def send_command(
        self,
        command_name: str,
        payload: Mapping[str, object] | None = None,
    ) -> object:
        self._factory.calls.append((self._listen_on, command_name, payload))
        if not self._factory.responses:
            return {"ok": True}
        next_response = self._factory.responses.pop(0)
        if isinstance(next_response, KittyConnectionError):
            raise next_response
        return next_response


class StubLauncher:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def __call__(self, args: Sequence[str], env: Mapping[str, str]) -> None:
        self.calls.append((tuple(args), dict(env)))


class StubSwayAdapter:
    """Records ``move_window_to_workspace`` calls and serves a timeline of
    ``SwayWindow`` snapshots from ``list_windows``. Each call advances to the
    next snapshot (the last is held indefinitely) so tests can model
    "pre-launch state" vs "post-launch state, kitty's new window appeared"
    without needing a side-effecting launcher hook."""

    def __init__(self, timeline: Sequence[Sequence[SwayWindow]] = ((),)) -> None:
        self._timeline: list[tuple[SwayWindow, ...]] = [tuple(s) for s in timeline] or [()]
        self.moves: list[tuple[int, str]] = []
        self.list_calls = 0

    def list_windows(self) -> Sequence[SwayWindow]:
        snapshot = self._timeline[min(self.list_calls, len(self._timeline) - 1)]
        self.list_calls += 1
        return snapshot

    def move_window_to_workspace(self, window_id: int, workspace_name: str) -> None:
        self.moves.append((window_id, workspace_name))


def build_session() -> ProjectSession:
    project_root = Path("/tmp/demo").resolve()
    return ProjectSession(
        project_root=project_root,
        session_name="demo",
        workspace_name=f"p:{project_root.name}",
    )


SESSION_SOCKET = session_socket_address("demo")


def test_ensure_terminal_focuses_existing_role_window() -> None:
    factory = StubKittyFactory(
        [
            {
                "ok": True,
                "data": [
                    {
                        "tabs": [
                            {
                                "windows": [
                                    {
                                        "id": 17,
                                        "user_vars": {
                                            "hop_session": "demo",
                                            "hop_role": "test",
                                            "hop_project_root": str(build_session().project_root),
                                        },
                                    }
                                ]
                            }
                        ]
                    }
                ],
            },
            {"ok": True},
        ]
    )
    adapter = KittyRemoteControlAdapter(transport_factory=factory, launcher=StubLauncher())

    adapter.ensure_terminal(build_session(), role="test")

    assert factory.calls == [
        (SESSION_SOCKET, "ls", {"output_format": "json"}),
        (SESSION_SOCKET, "focus-window", {"match": "id:17"}),
    ]


def test_ensure_terminal_launches_os_window_when_role_is_missing() -> None:
    factory = StubKittyFactory(
        [
            {"ok": True, "data": []},
            {"ok": True},
        ]
    )
    adapter = KittyRemoteControlAdapter(transport_factory=factory, launcher=StubLauncher())

    adapter.ensure_terminal(build_session(), role="server")

    assert factory.calls == [
        (SESSION_SOCKET, "ls", {"output_format": "json"}),
        (
            SESSION_SOCKET,
            "launch",
            {
                "args": [],
                "cwd": str(build_session().project_root),
                "type": "os-window",
                "keep_focus": False,
                "allow_remote_control": True,
                "window_title": "server",
                "os_window_title": "server",
                "os_window_class": "hop:server",
                "var": ["hop_role=server"],
            },
        ),
    ]


def test_bootstrap_invokes_on_session_bootstrap_hook_after_kitty_listens_and_tags_window() -> None:
    factory = StubKittyFactory(
        [
            KittyConnectionError("no such socket"),  # _find_window's ls
            KittyConnectionError("still not listening"),  # _launch_window's launch send
            {"ok": True, "data": []},  # poll succeeds
            {"ok": True},  # set-user-vars succeeds
        ]
    )
    bootstrapped: list[ProjectSession] = []

    def on_bootstrap(session: ProjectSession, _base: object) -> None:
        bootstrapped.append(session)

    adapter = KittyRemoteControlAdapter(
        transport_factory=factory,
        launcher=StubLauncher(),
        on_session_bootstrap=on_bootstrap,
        sleep=lambda _: None,
    )

    adapter.ensure_terminal(build_session(), role="shell")

    assert bootstrapped == [build_session()]
    # set-user-vars tags the bootstrap window with hop_role=shell so role-based
    # discovery treats it like windows added via `kitty @ launch --var=...`.
    assert (SESSION_SOCKET, "set-user-vars", {"match": "all", "var": ["hop_role=shell"]}) in factory.calls


def test_ensure_terminal_bootstraps_session_kitty_when_socket_is_not_listening() -> None:
    # First ls fails with KittyConnectionError → no session kitty yet → enter
    # the launch path → that send raises too → fall through to bootstrap.
    # After Popen, we poll until the socket comes up, then tag the window.
    factory = StubKittyFactory(
        [
            KittyConnectionError("no such socket"),  # _find_window's ls
            KittyConnectionError("still not listening"),  # _launch_window's launch send
            KittyConnectionError("socket not up yet"),  # first poll
            {"ok": True, "data": []},  # poll succeeds
            {"ok": True},  # set-user-vars
        ]
    )
    launcher = StubLauncher()
    adapter = KittyRemoteControlAdapter(
        transport_factory=factory,
        launcher=launcher,
        sleep=lambda _: None,
    )

    adapter.ensure_terminal(build_session(), role="shell")

    assert len(launcher.calls) == 1
    args, _env = launcher.calls[0]
    session = build_session()
    assert args == (
        "kitty",
        "--directory",
        str(session.project_root),
        "--listen-on",
        SESSION_SOCKET,
        "--title",
        "shell",
        "--class",
        "hop:shell",
        "--override",
        "allow_remote_control=yes",
    )


def test_ensure_terminal_raises_when_kitty_never_listens() -> None:
    responses: list[object | KittyConnectionError] = [
        KittyConnectionError("no socket"),  # _find_window
        KittyConnectionError("no socket"),  # _launch_window
    ]
    responses.extend(KittyConnectionError("still not listening") for _ in range(100))
    factory = StubKittyFactory(responses)
    launcher = StubLauncher()

    clock_value = [0.0]

    def clock() -> float:
        return clock_value[0]

    def sleep(dt: float) -> None:
        clock_value[0] += dt

    adapter = KittyRemoteControlAdapter(
        transport_factory=factory,
        launcher=launcher,
        sleep=sleep,
        clock=clock,
    )

    with pytest.raises(KittyConnectionError, match="did not start listening"):
        adapter.ensure_terminal(build_session(), role="shell")

    assert len(launcher.calls) == 1


def test_run_in_terminal_returns_window_id_for_existing_role_window() -> None:
    factory = StubKittyFactory(
        [
            {
                "ok": True,
                "data": [
                    {
                        "tabs": [
                            {
                                "windows": [
                                    {
                                        "id": 24,
                                        "user_vars": {
                                            "hop_session": "demo",
                                            "hop_role": "shell",
                                            "hop_project_root": str(build_session().project_root),
                                        },
                                    }
                                ]
                            }
                        ]
                    }
                ],
            },
            {"ok": True},
        ]
    )
    adapter = KittyRemoteControlAdapter(transport_factory=factory, launcher=StubLauncher())

    window_id = adapter.run_in_terminal(build_session(), role="shell", command="ls")

    assert window_id == 24
    assert factory.calls[-1] == (
        SESSION_SOCKET,
        "send-text",
        {"match": "id:24", "data": "text:ls\n"},
    )


def test_run_in_terminal_with_focus_focuses_existing_role_window_after_send_text() -> None:
    existing_window = {
        "ok": True,
        "data": [
            {
                "tabs": [
                    {
                        "windows": [
                            {
                                "id": 24,
                                "user_vars": {
                                    "hop_session": "demo",
                                    "hop_role": "shell",
                                    "hop_project_root": str(build_session().project_root),
                                },
                            }
                        ]
                    }
                ]
            }
        ],
    }
    factory = StubKittyFactory([existing_window, {"ok": True}, {"ok": True}])
    adapter = KittyRemoteControlAdapter(transport_factory=factory, launcher=StubLauncher())

    window_id = adapter.run_in_terminal(build_session(), role="shell", command="ls", focus=True)

    assert window_id == 24
    assert factory.calls == [
        (SESSION_SOCKET, "ls", {"output_format": "json"}),
        (SESSION_SOCKET, "send-text", {"match": "id:24", "data": "text:ls\n"}),
        (SESSION_SOCKET, "focus-window", {"match": "id:24"}),
    ]


def test_run_in_terminal_without_focus_launches_missing_window_with_keep_focus_true() -> None:
    new_window = {
        "ok": True,
        "data": [
            {
                "tabs": [
                    {
                        "windows": [
                            {
                                "id": 31,
                                "user_vars": {
                                    "hop_session": "demo",
                                    "hop_role": "server",
                                    "hop_project_root": str(build_session().project_root),
                                },
                            }
                        ]
                    }
                ]
            }
        ],
    }
    factory = StubKittyFactory(
        [
            {"ok": True, "data": []},
            {"ok": True},
            new_window,
            {"ok": True},
        ]
    )
    adapter = KittyRemoteControlAdapter(transport_factory=factory, launcher=StubLauncher())

    window_id = adapter.run_in_terminal(build_session(), role="server", command="bin/dev")

    assert window_id == 31
    launch_call = factory.calls[1]
    assert launch_call[1] == "launch"
    assert launch_call[2] is not None
    assert launch_call[2]["keep_focus"] is True
    assert factory.calls[-1] == (
        SESSION_SOCKET,
        "send-text",
        {"match": "id:31", "data": "text:bin/dev\n"},
    )
    # No focus-window for the missing-window path.
    assert all(call[1] != "focus-window" for call in factory.calls)


def test_run_in_terminal_with_focus_launches_missing_window_with_keep_focus_false() -> None:
    new_window = {
        "ok": True,
        "data": [
            {
                "tabs": [
                    {
                        "windows": [
                            {
                                "id": 31,
                                "user_vars": {
                                    "hop_session": "demo",
                                    "hop_role": "server",
                                    "hop_project_root": str(build_session().project_root),
                                },
                            }
                        ]
                    }
                ]
            }
        ],
    }
    factory = StubKittyFactory(
        [
            {"ok": True, "data": []},
            {"ok": True},
            new_window,
            {"ok": True},
        ]
    )
    adapter = KittyRemoteControlAdapter(transport_factory=factory, launcher=StubLauncher())

    window_id = adapter.run_in_terminal(build_session(), role="server", command="bin/dev", focus=True)

    assert window_id == 31
    launch_call = factory.calls[1]
    assert launch_call[1] == "launch"
    assert launch_call[2] is not None
    # Missing-window path with focus=True opts out of keep_focus so kitty
    # focuses the new OS window itself; no follow-up focus-window IPC.
    assert launch_call[2]["keep_focus"] is False
    assert factory.calls[-1] == (
        SESSION_SOCKET,
        "send-text",
        {"match": "id:31", "data": "text:bin/dev\n"},
    )
    assert all(call[1] != "focus-window" for call in factory.calls)


def test_ensure_terminal_uses_base_shell_args_in_launch_payload() -> None:
    factory = StubKittyFactory(
        [
            {"ok": True, "data": []},
            {"ok": True},
        ]
    )

    from hop.layouts import WindowSpec

    class FakeBackend:
        def wrap(self, command: str, _session: ProjectSession) -> Sequence[str]:
            # Concatenate prefix and command exactly the way the real
            # CommandBackend does. Empty command → fall back to the shell
            # path inside the prefix.
            inner = command or "${SHELL:-sh}"
            return ("sh", "-c", f"podman-compose -f docker-compose.dev.yml exec devcontainer {inner}")

        def prepare(self, _session: ProjectSession) -> None:
            return None

    adapter = KittyRemoteControlAdapter(
        session_backend_for=lambda _session: FakeBackend(),  # type: ignore[arg-type]
        session_windows_for=lambda _session: (WindowSpec(role="shell", command="/usr/bin/zsh", active=True),),
        transport_factory=factory,
        launcher=StubLauncher(),
    )

    adapter.ensure_terminal(build_session(), role="shell")

    launch_call = factory.calls[1]
    assert launch_call[1] == "launch"
    payload = launch_call[2]
    assert payload is not None
    assert payload["args"] == [
        "sh",
        "-c",
        "podman-compose -f docker-compose.dev.yml exec devcontainer /usr/bin/zsh",
    ]


def test_launch_payload_composes_command_and_shell_for_non_shell_role() -> None:
    """A custom-role window like `server` running `bin/dev` must keep the
    kitty window alive when the command exits or is Ctrl-C'd. Hop achieves
    this by composing `<command>; <shell>` inside the launch args — when
    `bin/dev` returns, the trailing shell takes over so the window stays
    usable instead of disappearing."""
    factory = StubKittyFactory(
        [
            {"ok": True, "data": []},
            {"ok": True},
        ]
    )

    from hop.layouts import WindowSpec

    class FakeBackend:
        # No prefix → inline is identity-substituted; matches a host backend.
        def inline(self, command: str, _session: ProjectSession) -> str:
            return command

        def wrap(self, command: str, _session: ProjectSession) -> Sequence[str]:
            return () if not command else ("sh", "-c", command)

    adapter = KittyRemoteControlAdapter(
        session_backend_for=lambda _session: FakeBackend(),  # type: ignore[arg-type]
        session_windows_for=lambda _session: (
            WindowSpec(role="shell", command="", active=True),
            WindowSpec(role="server", command="bin/dev", active=True),
        ),
        transport_factory=factory,
        launcher=StubLauncher(),
    )

    adapter.ensure_terminal(build_session(), role="server")

    launch_call = factory.calls[1]
    assert launch_call[1] == "launch"
    payload = launch_call[2]
    assert payload is not None
    # The shell command in the resolved windows is "" (sentinel for platform
    # default), so the post-exit shell falls back to ${SHELL:-sh}.
    assert payload["args"] == ["sh", "-c", "bin/dev; ${SHELL:-sh}"]


def test_launch_payload_composes_through_backend_prefix() -> None:
    """Same Ctrl-C-survives behavior in a prefix backend — each piece is
    wrapped by the prefix individually so the trailing shell still runs
    inside the backend's environment."""
    factory = StubKittyFactory(
        [
            {"ok": True, "data": []},
            {"ok": True},
        ]
    )

    from hop.layouts import WindowSpec

    class FakeBackend:
        def inline(self, command: str, _session: ProjectSession) -> str:
            return f"compose exec devcontainer {command}"

        def wrap(self, command: str, _session: ProjectSession) -> Sequence[str]:
            inner = command or "${SHELL:-sh}"
            return ("sh", "-c", f"compose exec devcontainer {inner}")

    adapter = KittyRemoteControlAdapter(
        session_backend_for=lambda _session: FakeBackend(),  # type: ignore[arg-type]
        session_windows_for=lambda _session: (
            WindowSpec(role="shell", command="", active=True),
            WindowSpec(role="server", command="bin/dev", active=True),
        ),
        transport_factory=factory,
        launcher=StubLauncher(),
    )

    adapter.ensure_terminal(build_session(), role="server")

    payload = factory.calls[1][2]
    assert payload is not None
    assert payload["args"] == [
        "sh",
        "-c",
        "compose exec devcontainer bin/dev; compose exec devcontainer ${SHELL:-sh}",
    ]


def test_empty_command_non_shell_role_inherits_shell_role_command() -> None:
    """A role declared with ``command = ""`` (e.g. the ``test`` window in the
    rails layout) inherits the shell role's command. The intent is that a
    user wrap on the shell role (``kitten run-shell --shell=$SHELL`` to
    enable OSC 133 inside a backend) propagates uniformly to every role
    without per-role boilerplate."""
    factory = StubKittyFactory(
        [
            {"ok": True, "data": []},
            {"ok": True},
        ]
    )

    from hop.layouts import WindowSpec

    class FakeBackend:
        def inline(self, command: str, _session: ProjectSession) -> str:
            return command

        def wrap(self, command: str, _session: ProjectSession) -> Sequence[str]:
            return ("sh", "-c", command) if command else ()

    adapter = KittyRemoteControlAdapter(
        session_backend_for=lambda _session: FakeBackend(),  # type: ignore[arg-type]
        session_windows_for=lambda _session: (
            WindowSpec(role="shell", command="kitten run-shell --shell=/usr/bin/zsh", active=True),
            WindowSpec(role="test", command="", active=True),
        ),
        transport_factory=factory,
        launcher=StubLauncher(),
    )

    adapter.ensure_terminal(build_session(), role="test")

    payload = factory.calls[1][2]
    assert payload is not None
    # The test role's empty command is replaced by the shell role's command,
    # then wrapped — no `;` composition, no ${SHELL:-sh} fallback.
    assert payload["args"] == ["sh", "-c", "kitten run-shell --shell=/usr/bin/zsh"]


def test_shell_role_empty_command_does_not_self_inherit() -> None:
    """The shell role's own empty command is the host-default sentinel that
    ``backend.wrap`` interprets as "let kitty pick the user's login shell".
    It must NOT fall through to its own empty command (no-op infinite loop in
    spirit) — instead the wrap path returns empty argv and kitty falls back
    to /etc/passwd."""
    factory = StubKittyFactory(
        [
            {"ok": True, "data": []},
            {"ok": True},
        ]
    )

    from hop.layouts import WindowSpec

    class FakeBackend:
        def inline(self, command: str, _session: ProjectSession) -> str:
            return command

        def wrap(self, command: str, _session: ProjectSession) -> Sequence[str]:
            # Mirrors host backend: empty command + empty prefix → empty argv.
            return ("sh", "-c", command) if command else ()

    adapter = KittyRemoteControlAdapter(
        session_backend_for=lambda _session: FakeBackend(),  # type: ignore[arg-type]
        session_windows_for=lambda _session: (WindowSpec(role="shell", command="", active=True),),
        transport_factory=factory,
        launcher=StubLauncher(),
    )

    adapter.ensure_terminal(build_session(), role="shell")

    payload = factory.calls[1][2]
    assert payload is not None
    # Host-default sentinel: empty argv → kitty falls back to /etc/passwd.
    assert "args" not in payload or payload.get("args") in (None, [], ())


def test_non_shell_role_with_primary_command_composes_with_shell_role_wrap() -> None:
    """A role with a real primary command (``log`` window running ``less``)
    composes ``<primary>; <shell-fallback>`` so the window survives the primary
    process exiting. The shell-fallback is the shell role's command — including
    a user wrap like ``kitten run-shell``. After ``less`` exits the user lands
    in an integrated shell with OSC 133 markers firing."""
    factory = StubKittyFactory(
        [
            {"ok": True, "data": []},
            {"ok": True},
        ]
    )

    from hop.layouts import WindowSpec

    class FakeBackend:
        def inline(self, command: str, _session: ProjectSession) -> str:
            return command

        def wrap(self, command: str, _session: ProjectSession) -> Sequence[str]:
            return ("sh", "-c", command) if command else ()

    adapter = KittyRemoteControlAdapter(
        session_backend_for=lambda _session: FakeBackend(),  # type: ignore[arg-type]
        session_windows_for=lambda _session: (
            WindowSpec(role="shell", command="kitten run-shell --shell=/usr/bin/zsh", active=True),
            WindowSpec(role="log", command="less -R log/test.log", active=True),
        ),
        transport_factory=factory,
        launcher=StubLauncher(),
    )

    adapter.ensure_terminal(build_session(), role="log")

    payload = factory.calls[1][2]
    assert payload is not None
    assert payload["args"] == [
        "sh",
        "-c",
        "less -R log/test.log; kitten run-shell --shell=/usr/bin/zsh",
    ]


def test_launch_payload_does_not_compose_for_shell_role() -> None:
    """The shell role IS the post-exit fallback — composition would just
    spawn an extra shell after exit. Verify the wrap path is used directly."""
    factory = StubKittyFactory(
        [
            {"ok": True, "data": []},
            {"ok": True},
        ]
    )

    from hop.layouts import WindowSpec

    class FakeBackend:
        def inline(self, command: str, _session: ProjectSession) -> str:
            return command  # never called when wrap is used

        def wrap(self, command: str, _session: ProjectSession) -> Sequence[str]:
            return ("sh", "-c", "/usr/bin/zsh") if command else ()

    adapter = KittyRemoteControlAdapter(
        session_backend_for=lambda _session: FakeBackend(),  # type: ignore[arg-type]
        session_windows_for=lambda _session: (WindowSpec(role="shell", command="/usr/bin/zsh", active=True),),
        transport_factory=factory,
        launcher=StubLauncher(),
    )

    adapter.ensure_terminal(build_session(), role="shell-2")  # ad-hoc shell

    payload = factory.calls[1][2]
    assert payload is not None
    # Ad-hoc shell falls through to the shell role's command via wrap,
    # not the `; <shell>` composition.
    assert payload["args"] == ["sh", "-c", "/usr/bin/zsh"]


def test_bootstrap_calls_base_prepare_and_appends_shell_args_after_dash_dash() -> None:
    factory = StubKittyFactory(
        [
            KittyConnectionError("no socket"),  # _find_window
            KittyConnectionError("still no socket"),  # _launch_window
            {"ok": True, "data": []},  # poll succeeds
            {"ok": True},  # set-user-vars
        ]
    )
    launcher = StubLauncher()

    prepared: list[ProjectSession] = []

    from hop.layouts import WindowSpec

    class FakeBackend:
        def wrap(self, command: str, _session: ProjectSession) -> Sequence[str]:
            inner = command or "${SHELL:-sh}"
            return ("sh", "-c", f"podman-compose exec devcontainer {inner}")

        def prepare(self, session: ProjectSession) -> None:
            prepared.append(session)

    adapter = KittyRemoteControlAdapter(
        session_backend_for=lambda _session: FakeBackend(),  # type: ignore[arg-type]
        session_windows_for=lambda _session: (WindowSpec(role="shell", command="/usr/bin/zsh", active=True),),
        transport_factory=factory,
        launcher=launcher,
        sleep=lambda _: None,
    )

    adapter.ensure_terminal(build_session(), role="shell")

    assert prepared == [build_session()]
    assert len(launcher.calls) == 1
    args, _env = launcher.calls[0]
    # Tail of args must be "--" then the wrapped shell args (sh -c "...").
    assert args[-4:] == ("--", "sh", "-c", "podman-compose exec devcontainer /usr/bin/zsh")


def test_close_window_addresses_session_socket() -> None:
    factory = StubKittyFactory([{"ok": True}])
    adapter = KittyRemoteControlAdapter(transport_factory=factory, launcher=StubLauncher())

    adapter.close_window("demo", 17)

    assert factory.calls == [(SESSION_SOCKET, "close-window", {"match": "id:17"})]


def test_get_window_state_extracts_at_prompt_and_exit_status() -> None:
    factory = StubKittyFactory(
        [
            {
                "ok": True,
                "data": [
                    {
                        "tabs": [
                            {
                                "windows": [
                                    {
                                        "id": 31,
                                        "at_prompt": False,
                                        "last_cmd_exit_status": 2,
                                    }
                                ]
                            }
                        ]
                    }
                ],
            }
        ]
    )
    adapter = KittyRemoteControlAdapter(transport_factory=factory, launcher=StubLauncher())

    state = adapter.get_window_state("demo", 31)

    assert factory.calls == [
        (SESSION_SOCKET, "ls", {"match": "id:31", "output_format": "json"}),
    ]
    assert state == KittyWindowState(at_prompt=False, last_cmd_exit_status=2)


def test_get_window_state_raises_when_window_missing() -> None:
    factory = StubKittyFactory([{"ok": True, "data": []}])
    adapter = KittyRemoteControlAdapter(transport_factory=factory, launcher=StubLauncher())

    with pytest.raises(KittyCommandError, match="no window with id 99"):
        adapter.get_window_state("demo", 99)


def test_get_window_state_skips_empty_tabs_empty_windows_and_other_ids() -> None:
    factory = StubKittyFactory(
        [
            {
                "ok": True,
                "data": [
                    {"tabs": []},
                    {"tabs": [{"windows": []}]},
                    {"tabs": [{"windows": [{"id": 999}]}]},
                    {
                        "tabs": [
                            {
                                "windows": [
                                    {
                                        "id": 7,
                                        "at_prompt": False,
                                        "last_cmd_exit_status": 1,
                                    }
                                ]
                            }
                        ]
                    },
                ],
            }
        ]
    )
    adapter = KittyRemoteControlAdapter(transport_factory=factory, launcher=StubLauncher())

    assert adapter.get_window_state("demo", 7) == KittyWindowState(at_prompt=False, last_cmd_exit_status=1)


def test_get_last_cmd_output_returns_data_text() -> None:
    factory = StubKittyFactory([{"ok": True, "data": "hello\nworld\n"}])
    adapter = KittyRemoteControlAdapter(transport_factory=factory, launcher=StubLauncher())

    output = adapter.get_last_cmd_output("demo", 31)

    assert factory.calls == [
        (SESSION_SOCKET, "get-text", {"match": "id:31", "extent": "last_cmd_output"}),
    ]
    assert output == "hello\nworld\n"


def test_get_last_cmd_output_handles_non_mapping_response() -> None:
    factory = StubKittyFactory(["plain text\n"])
    adapter = KittyRemoteControlAdapter(transport_factory=factory, launcher=StubLauncher())

    assert adapter.get_last_cmd_output("demo", 31) == "plain text\n"


def test_list_session_windows_returns_empty_when_socket_is_not_listening() -> None:
    factory = StubKittyFactory([KittyConnectionError("no socket")])
    adapter = KittyRemoteControlAdapter(transport_factory=factory, launcher=StubLauncher())

    assert adapter.list_session_windows(build_session()) == ()


def test_inspect_window_uses_env_driven_transport_for_kitten_callers() -> None:
    factory = StubKittyFactory(
        [
            {
                "ok": True,
                "data": [
                    {
                        "tabs": [
                            {
                                "windows": [
                                    {
                                        "id": 17,
                                        "user_vars": {
                                            "hop_session": "demo",
                                            "hop_role": "shell",
                                            "hop_project_root": str(build_session().project_root),
                                        },
                                        "cwd": str(build_session().project_root),
                                    }
                                ]
                            }
                        ]
                    }
                ],
            }
        ]
    )
    adapter = KittyRemoteControlAdapter(transport_factory=factory, launcher=StubLauncher())

    window = adapter.inspect_window(17)

    assert window is not None
    assert window.id == 17
    # No explicit listen_on: factory gets None and falls back to env
    # (KITTY_LISTEN_ON in the kitten's process).
    assert factory.calls[0][0] is None


def test_inspect_window_forwards_explicit_listen_on_to_transport_factory() -> None:
    factory = StubKittyFactory(
        [
            {
                "ok": True,
                "data": [
                    {
                        "tabs": [
                            {
                                "windows": [
                                    {
                                        "id": 17,
                                        "user_vars": {
                                            "hop_session": "demo",
                                            "hop_role": "shell",
                                            "hop_project_root": str(build_session().project_root),
                                        },
                                        "cwd": str(build_session().project_root),
                                    }
                                ]
                            }
                        ]
                    }
                ],
            }
        ]
    )
    adapter = KittyRemoteControlAdapter(transport_factory=factory, launcher=StubLauncher())

    adapter.inspect_window(17, listen_on=SESSION_SOCKET)

    assert factory.calls[0][0] == SESSION_SOCKET


def test_is_alive_returns_true_when_session_kitty_responds() -> None:
    factory = StubKittyFactory([{"ok": True, "data": []}])
    adapter = KittyRemoteControlAdapter(transport_factory=factory, launcher=StubLauncher())

    assert adapter.is_alive(build_session()) is True
    # The probe is a single `ls` against the session's socket.
    assert factory.calls == [(SESSION_SOCKET, "ls", {"output_format": "json"})]


def test_is_alive_returns_false_when_session_socket_is_unreachable() -> None:
    factory = StubKittyFactory([KittyConnectionError("Could not talk to Kitty")])
    adapter = KittyRemoteControlAdapter(transport_factory=factory, launcher=StubLauncher())

    assert adapter.is_alive(build_session()) is False


# --- get_focused_window_cwd ----------------------------------------------


def _focused_window_payload(
    *,
    cwd_of_child: str | None = None,
    cwd: str | None = None,
    kitty_id: int = 17,
) -> dict[str, object]:
    focused: dict[str, object] = {"id": kitty_id, "is_focused": True}
    if cwd is not None:
        focused["cwd"] = cwd
    if cwd_of_child is not None:
        focused["cwd_of_child"] = cwd_of_child
    return {
        "ok": True,
        "data": [
            {
                "tabs": [
                    {
                        "windows": [
                            {
                                "id": kitty_id - 1,
                                "is_focused": False,
                                "cwd": "/somewhere/else",
                            },
                            focused,
                        ]
                    }
                ]
            }
        ],
    }


def test_get_focused_window_cwd_returns_cwd_of_child_for_focused_window() -> None:
    from hop.kitty import get_focused_window_cwd

    factory = StubKittyFactory([_focused_window_payload(cwd_of_child="/workspace/src")])

    cwd = get_focused_window_cwd("demo", transport_factory=factory)

    assert cwd == Path("/workspace/src")
    assert factory.calls == [(SESSION_SOCKET, "ls", {"output_format": "json"})]


def test_get_focused_window_cwd_ignores_process_cwd_without_cwd_of_child() -> None:
    """Kitty's ``cwd`` is the kitty *process* cwd — for a container session
    that's the host launch directory (e.g. ``/home/me/projects/foo``), not
    where the in-container shell is. Returning it would make every relative
    candidate from the kitten resolve against a host path that doesn't exist
    inside the backend. The function must report ``None`` here so callers
    fall back to ``backend.workspace_path`` (or to the project root)."""
    from hop.kitty import get_focused_window_cwd

    factory = StubKittyFactory([_focused_window_payload(cwd="/home/me/projects/foo")])

    assert get_focused_window_cwd("demo", transport_factory=factory) is None


def test_get_focused_window_cwd_returns_none_when_no_window_is_focused() -> None:
    from hop.kitty import get_focused_window_cwd

    factory = StubKittyFactory(
        [
            {
                "ok": True,
                "data": [
                    {
                        "tabs": [
                            {
                                "windows": [
                                    {"id": 17, "is_focused": False, "cwd": "/x"},
                                ]
                            }
                        ]
                    }
                ],
            }
        ]
    )

    assert get_focused_window_cwd("demo", transport_factory=factory) is None


def test_get_focused_window_cwd_returns_none_on_ipc_failure() -> None:
    from hop.kitty import get_focused_window_cwd

    factory = StubKittyFactory([KittyConnectionError("Could not talk to Kitty")])

    assert get_focused_window_cwd("demo", transport_factory=factory) is None


def test_get_focused_window_cwd_returns_none_when_payload_is_not_a_list() -> None:
    from hop.kitty import get_focused_window_cwd

    # Kitty's ls normally returns a list at the top level; a malformed
    # response (here: a bare dict) is a defensive guard against future
    # kitty changes — we'd rather fall back than crash the kitten.
    factory = StubKittyFactory([{"ok": True, "data": {"unexpected": True}}])

    assert get_focused_window_cwd("demo", transport_factory=factory) is None


def test_get_focused_window_cwd_skips_malformed_entries() -> None:
    from hop.kitty import get_focused_window_cwd

    factory = StubKittyFactory(
        [
            {
                "ok": True,
                "data": [
                    "not a mapping",
                    {"tabs": ["bad tab"]},
                    {
                        "tabs": [
                            {
                                "windows": [
                                    "bad window",
                                    {"id": 42, "is_focused": True, "cwd_of_child": "/right/place"},
                                ]
                            }
                        ]
                    },
                ],
            }
        ]
    )

    assert get_focused_window_cwd("demo", transport_factory=factory) == Path("/right/place")


def test_get_focused_window_cwd_returns_none_when_focused_window_has_no_cwd() -> None:
    from hop.kitty import get_focused_window_cwd

    factory = StubKittyFactory([_focused_window_payload(cwd=None)])

    assert get_focused_window_cwd("demo", transport_factory=factory) is None


def _hop_role_window(window_id: int, role: str, workspace_name: str) -> SwayWindow:
    return SwayWindow(
        id=window_id,
        workspace_name=workspace_name,
        app_id=f"hop:{role}",
        window_class=None,
    )


def test_launch_window_moves_new_role_terminal_to_session_workspace_when_drifted() -> None:
    factory = StubKittyFactory(
        [
            {"ok": True, "data": []},  # _find_window's ls (no existing role)
            {"ok": True},  # launch
        ]
    )
    sway = StubSwayAdapter(
        timeline=[
            (),  # pre-launch snapshot — no hop:test windows yet
            (_hop_role_window(window_id=42, role="test", workspace_name="p:other"),),
        ]
    )
    adapter = KittyRemoteControlAdapter(
        transport_factory=factory,
        launcher=StubLauncher(),
        sway=sway,
        sleep=lambda _: None,
    )

    adapter.ensure_terminal(build_session(), role="test")

    assert sway.moves == [(42, "p:demo")]


def test_launch_window_skips_move_when_new_role_terminal_already_on_session_workspace() -> None:
    factory = StubKittyFactory([{"ok": True, "data": []}, {"ok": True}])
    sway = StubSwayAdapter(
        timeline=[
            (),
            (_hop_role_window(window_id=42, role="test", workspace_name="p:demo"),),
        ]
    )
    adapter = KittyRemoteControlAdapter(
        transport_factory=factory,
        launcher=StubLauncher(),
        sway=sway,
        sleep=lambda _: None,
    )

    adapter.ensure_terminal(build_session(), role="test")

    assert sway.moves == []


def test_launch_window_ignores_pre_existing_hop_role_windows_when_diffing() -> None:
    """Another session's ``hop:test`` window must not be mistaken for the
    one this launch just created. The pre-launch snapshot excludes it."""
    factory = StubKittyFactory([{"ok": True, "data": []}, {"ok": True}])
    pre_existing = _hop_role_window(window_id=10, role="test", workspace_name="p:other")
    new_window = _hop_role_window(window_id=20, role="test", workspace_name="p:other")
    sway = StubSwayAdapter(
        timeline=[
            (pre_existing,),  # pre-launch
            (pre_existing, new_window),  # post-launch
        ]
    )
    adapter = KittyRemoteControlAdapter(
        transport_factory=factory,
        launcher=StubLauncher(),
        sway=sway,
        sleep=lambda _: None,
    )

    adapter.ensure_terminal(build_session(), role="test")

    assert sway.moves == [(20, "p:demo")]
    # The pre-existing window stays put; never moved.
    assert not any(move[0] == 10 for move in sway.moves)


def test_launch_window_adopt_times_out_without_raising_when_no_new_window_appears() -> None:
    """Kitty's window may fail to register with Sway (binary crash, race
    against an unrelated Wayland hiccup). The adopt step bails after the
    timeout; it must not raise, since the kitty IPC launch already succeeded."""

    factory = StubKittyFactory([{"ok": True, "data": []}, {"ok": True}])
    sway = StubSwayAdapter(timeline=[()])  # no new windows ever appear
    # Counter-driven clock crosses the 2-second deadline within a few ticks.
    ticks = iter([0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0])
    adapter = KittyRemoteControlAdapter(
        transport_factory=factory,
        launcher=StubLauncher(),
        sway=sway,
        sleep=lambda _: None,
        clock=lambda: next(ticks),
    )

    # Must not raise.
    adapter.ensure_terminal(build_session(), role="test")

    assert sway.moves == []


def test_no_sway_dep_skips_workspace_adopt() -> None:
    """Backward compat: callers that construct the adapter without ``sway``
    get the pre-fix behavior — kitty IPC launch happens, no Sway calls fire."""

    factory = StubKittyFactory([{"ok": True, "data": []}, {"ok": True}])
    adapter = KittyRemoteControlAdapter(
        transport_factory=factory,
        launcher=StubLauncher(),
        sleep=lambda _: None,
    )

    # The absence of ``sway`` means no list_windows / move_window_to_workspace
    # calls are dispatched. Simply not raising is the contract.
    adapter.ensure_terminal(build_session(), role="test")


def test_bootstrap_path_adopts_new_role_terminal_to_session_workspace() -> None:
    """The cold-bootstrap path (no kitty listening yet) launches kitty via
    subprocess, waits for the session socket, then must also run the
    workspace-adopt step."""

    factory = StubKittyFactory(
        [
            KittyConnectionError("no such socket"),  # _find_window's ls
            KittyConnectionError("still not listening"),  # IPC launch fails
            {"ok": True, "data": []},  # _wait_for_session_kitty ls succeeds
            {"ok": True},  # set-user-vars
        ]
    )
    launcher = StubLauncher()
    sway = StubSwayAdapter(
        timeline=[
            # Two pre-launch snapshots: the IPC path takes one and discards it
            # when IPC fails, then the bootstrap path takes its own (the
            # meaningful one) after ``backend.prepare`` returns.
            (),
            (),
            (_hop_role_window(window_id=7, role="shell", workspace_name="p:other"),),
        ]
    )
    adapter = KittyRemoteControlAdapter(
        transport_factory=factory,
        launcher=launcher,
        sway=sway,
        sleep=lambda _: None,
    )

    adapter.ensure_terminal(build_session(), role="shell")

    assert sway.moves == [(7, "p:demo")]
