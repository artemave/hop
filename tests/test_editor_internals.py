# pyright: reportPrivateUsage=false

import json
import subprocess
from pathlib import Path
from typing import Mapping, Sequence

import pytest
from hop.editor import (
    NeovimCommandError,
    SharedNeovimEditorAdapter,
    _coerce_response_data,
    _coerce_string_mapping,
    _parse_editor_window,
    _remove_stale_socket,
    _resolve_runtime_dir,
    _SubprocessRunner,
)
from hop.kitty import KittyCommandError
from hop.session import ProjectSession


class StubKittyTransport:
    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.commands: list[tuple[str, Mapping[str, object] | None]] = []

    def send_command(self, command_name: str, payload: Mapping[str, object] | None = None) -> object:
        self.commands.append((command_name, payload))
        if not self._responses:
            return {"ok": True}
        return self._responses.pop(0)


class StubProcessRunner:
    def __init__(self, responses: list[subprocess.CompletedProcess[str]]) -> None:
        self._responses = list(responses)
        self.commands: list[tuple[str, ...]] = []

    def run(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        command = tuple(args)
        self.commands.append(command)
        if not self._responses:
            raise AssertionError(f"Unexpected process command: {command}")
        return self._responses.pop(0)


def build_session() -> ProjectSession:
    project_root = Path("/tmp/demo").resolve()
    return ProjectSession(
        project_root=project_root,
        session_name="demo",
        workspace_name=f"p:{project_root}",
    )


def test_find_editor_window_rejects_invalid_listing_payload(tmp_path: Path) -> None:
    runner = StubProcessRunner([subprocess.CompletedProcess(("nvim",), 0, "", "")])
    transport = StubKittyTransport([{"ok": True, "data": {}}])
    adapter = SharedNeovimEditorAdapter(
        kitty_transport=transport,
        process_runner=runner,
        runtime_dir=tmp_path / "runtime",
    )

    with pytest.raises(KittyCommandError, match="invalid window listing"):
        adapter.focus(build_session())


def test_find_editor_window_skips_malformed_entries_and_uses_lowest_matching_id(tmp_path: Path) -> None:
    runner = StubProcessRunner([subprocess.CompletedProcess(("nvim",), 0, "", "")])
    transport = StubKittyTransport(
        [
            {
                "ok": True,
                "data": [
                    "invalid",
                    {"tabs": ["invalid"]},
                    {
                        "tabs": [
                            {
                                "windows": [
                                    "invalid",
                                    {
                                        "id": "31",
                                        "user_vars": {"hop_editor": "1"},
                                    },
                                    {
                                        "id": 29,
                                        "user_vars": {"hop_role": "shell"},
                                    },
                                    {
                                        "id": 31,
                                        "user_vars": {"hop_editor": "1"},
                                    },
                                    {
                                        "id": 30,
                                        "user_vars": {"hop_editor": "1"},
                                    },
                                ]
                            }
                        ]
                    },
                ],
            },
            {"ok": True},
        ]
    )
    adapter = SharedNeovimEditorAdapter(
        kitty_transport=transport,
        process_runner=runner,
        runtime_dir=tmp_path / "runtime",
    )

    adapter.focus(build_session())

    assert transport.commands == [
        ("ls", {"output_format": "json"}),
        ("focus-window", {"match": "id:30"}),
    ]


def test_wait_for_server_times_out_when_neovim_never_becomes_ready(tmp_path: Path) -> None:
    runner = StubProcessRunner(
        [
            subprocess.CompletedProcess(("nvim",), 1, "", ""),
            subprocess.CompletedProcess(("nvim",), 1, "", ""),
        ]
    )
    transport = StubKittyTransport([{"ok": True}])
    adapter = SharedNeovimEditorAdapter(
        kitty_transport=transport,
        process_runner=runner,
        runtime_dir=tmp_path / "runtime",
        ready_timeout_seconds=0.001,
        ready_poll_interval_seconds=0.0,
    )
    monotonic_values = iter([0.0, 0.0, 1.0])
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("hop.editor.time.monotonic", lambda: next(monotonic_values))

    try:
        with pytest.raises(NeovimCommandError, match="did not become ready"):
            adapter.focus(build_session())
    finally:
        monkeypatch.undo()


def test_open_target_raises_stderr_when_remote_send_fails(tmp_path: Path) -> None:
    address = (tmp_path / "runtime" / "hop.sock").resolve()
    runner = StubProcessRunner(
        [
            subprocess.CompletedProcess(("nvim",), 0, "", ""),
            subprocess.CompletedProcess(("nvim",), 1, "", "permission denied\n"),
        ]
    )
    transport = StubKittyTransport(
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
                                        "user_vars": {"hop_editor": "1"},
                                    }
                                ]
                            }
                        ]
                    }
                ],
            }
        ]
    )
    adapter = SharedNeovimEditorAdapter(
        kitty_transport=transport,
        process_runner=runner,
        runtime_dir=address.parent,
    )

    with pytest.raises(NeovimCommandError, match="permission denied"):
        adapter.open_target(build_session(), target="README.md")


def test_resolve_runtime_dir_prefers_xdg_runtime_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))

    assert _resolve_runtime_dir(None) == (tmp_path / "hop").resolve()


def test_resolve_runtime_dir_falls_back_to_tempdir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr("hop.editor.gettempdir", lambda: str(tmp_path))

    assert _resolve_runtime_dir(None) == (tmp_path / "hop").resolve()


def test_coerce_response_data_handles_mapping_string_and_passthrough_values() -> None:
    assert _coerce_response_data({"data": None}) == []
    assert _coerce_response_data({"data": json.dumps([1, 2])}) == [1, 2]
    assert _coerce_response_data([1, 2]) == [1, 2]


def test_parse_editor_window_and_helper_functions_cover_list_and_none_fallbacks() -> None:
    assert _parse_editor_window({"id": "not-an-int"}) is None

    parsed = _parse_editor_window({"id": 17, "vars": ["hop_editor=1", "ignored"]})

    assert parsed is not None
    assert parsed.id == 17
    assert parsed.is_editor is True
    assert _coerce_string_mapping({"one": "1", "two": 2}) == {"one": "1"}
    assert _coerce_string_mapping(["one=1", "two=2", "ignored"]) == {"one": "1", "two": "2"}
    assert _coerce_string_mapping(object()) == {}


def test_remove_stale_socket_ignores_missing_paths(tmp_path: Path) -> None:
    _remove_stale_socket(tmp_path / "missing.sock")


def test_subprocess_runner_delegates_to_subprocess_run(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = subprocess.CompletedProcess(("nvim",), 0, "ok", "")

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert args == ["nvim"]
        assert kwargs == {"capture_output": True, "text": True, "check": False}
        return expected

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert _SubprocessRunner().run(("nvim",)) == expected
