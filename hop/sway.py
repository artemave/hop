from __future__ import annotations

import json
import os
import socket
import struct
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Protocol

from hop.errors import HopError

IPC_MAGIC = b"i3-ipc"
IPC_HEADER_FORMAT = "<6sII"
SWAY_SOCKET_ENV_VAR = "SWAYSOCK"


class SwayError(HopError):
    """Base error for Sway IPC failures."""


class SwayConnectionError(SwayError):
    """Raised when the Sway IPC socket cannot be resolved or reached."""


class SwayCommandError(SwayError):
    """Raised when Sway rejects an IPC command."""


class SwayMessageType(IntEnum):
    RUN_COMMAND = 0
    GET_WORKSPACES = 1
    GET_TREE = 4


@dataclass(frozen=True, slots=True)
class SwayWorkspace:
    name: str
    focused: bool = False


@dataclass(frozen=True, slots=True)
class SwayWindow:
    id: int
    workspace_name: str | None
    app_id: str | None
    window_class: str | None
    marks: tuple[str, ...] = ()
    focused: bool = False


class SwayIpcTransport(Protocol):
    def request(self, message_type: SwayMessageType, payload: bytes = b"") -> bytes: ...


class UnixSocketSwayIpcTransport:
    def __init__(self, socket_path: Path | str | None = None) -> None:
        self._socket_path = socket_path

    def request(self, message_type: SwayMessageType, payload: bytes = b"") -> bytes:
        socket_path = self._resolve_socket_path()

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            try:
                client.connect(socket_path)
            except OSError as error:
                msg = f"Could not connect to the Sway IPC socket at {socket_path!s}."
                raise SwayConnectionError(msg) from error

            header = struct.pack(IPC_HEADER_FORMAT, IPC_MAGIC, len(payload), int(message_type))
            client.sendall(header + payload)

            response_header = _recv_exact(client, struct.calcsize(IPC_HEADER_FORMAT))
            magic, payload_size, _response_type = struct.unpack(IPC_HEADER_FORMAT, response_header)
            if magic != IPC_MAGIC:
                msg = "Received an invalid response from the Sway IPC socket."
                raise SwayConnectionError(msg)

            return _recv_exact(client, payload_size)

    def _resolve_socket_path(self) -> str:
        if self._socket_path is not None:
            return str(Path(self._socket_path))

        socket_path = os.environ.get(SWAY_SOCKET_ENV_VAR)
        if socket_path:
            return socket_path

        msg = (
            "Sway IPC is unavailable because SWAYSOCK is not set. "
            "Run hop inside a Sway session or set SWAYSOCK explicitly."
        )
        raise SwayConnectionError(msg)


class SwayIpcAdapter:
    def __init__(self, transport: SwayIpcTransport | None = None) -> None:
        self._transport = transport or UnixSocketSwayIpcTransport()

    def switch_to_workspace(self, workspace_name: str) -> None:
        self.run_command(f"workspace {json.dumps(workspace_name)}")

    def run_command(self, command: str) -> None:
        payload = command.encode()
        response = self._transport.request(SwayMessageType.RUN_COMMAND, payload)
        results = json.loads(response.decode())

        if not results or not all(result.get("success") for result in results):
            msg = f"Sway rejected command {command!r}."
            raise SwayCommandError(msg)

    def list_session_workspaces(self, *, prefix: str = "p:") -> tuple[str, ...]:
        response = self._transport.request(SwayMessageType.GET_WORKSPACES)
        workspace_entries = json.loads(response.decode())
        workspaces = [
            SwayWorkspace(
                name=workspace_entry["name"],
                focused=bool(workspace_entry.get("focused", False)),
            )
            for workspace_entry in workspace_entries
            if isinstance(workspace_entry.get("name"), str)
            and workspace_entry["name"].startswith(prefix)
        ]
        return tuple(sorted(workspace.name for workspace in workspaces))

    def list_windows(self) -> tuple[SwayWindow, ...]:
        response = self._transport.request(SwayMessageType.GET_TREE)
        tree = json.loads(response.decode())
        windows: list[SwayWindow] = []
        _collect_windows(tree, windows=windows)
        return tuple(windows)

    def focus_window(self, window_id: int) -> None:
        self.run_command(f"[con_id={window_id}] focus")

    def move_window_to_workspace(self, window_id: int, workspace_name: str) -> None:
        self.run_command(
            f"[con_id={window_id}] move container to workspace {json.dumps(workspace_name)}"
        )

    def mark_window(self, window_id: int, mark: str) -> None:
        self.run_command(f"[con_id={window_id}] mark --add {json.dumps(mark)}")

    def close_window(self, window_id: int) -> None:
        self.run_command(f"[con_id={window_id}] kill")

    def remove_workspace(self, workspace_name: str) -> None:
        self.run_command("workspace back_and_forth")


def _recv_exact(client: socket.socket, byte_count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = byte_count

    while remaining > 0:
        chunk = client.recv(remaining)
        if not chunk:
            msg = "The Sway IPC socket closed before the full response was received."
            raise SwayConnectionError(msg)
        chunks.append(chunk)
        remaining -= len(chunk)

    return b"".join(chunks)


def _collect_windows(
    node: object,
    *,
    windows: list[SwayWindow],
    workspace_name: str | None = None,
) -> None:
    if not isinstance(node, dict):
        return

    current_workspace_name = workspace_name
    if node.get("type") == "workspace" and isinstance(node.get("name"), str):
        current_workspace_name = node["name"]

    window_id = node.get("id")
    app_id = node.get("app_id") if isinstance(node.get("app_id"), str) else None
    window_class = _extract_window_class(node.get("window_properties"))
    marks = tuple(mark for mark in node.get("marks", ()) if isinstance(mark, str))
    focused = bool(node.get("focused", False))

    if isinstance(window_id, int) and (app_id is not None or window_class is not None):
        windows.append(
            SwayWindow(
                id=window_id,
                workspace_name=current_workspace_name,
                app_id=app_id,
                window_class=window_class,
                marks=marks,
                focused=focused,
            )
        )

    for child in node.get("nodes", ()):
        _collect_windows(child, windows=windows, workspace_name=current_workspace_name)

    for child in node.get("floating_nodes", ()):
        _collect_windows(child, windows=windows, workspace_name=current_workspace_name)


def _extract_window_class(window_properties: object) -> str | None:
    if not isinstance(window_properties, dict):
        return None

    window_class = window_properties.get("class")
    if isinstance(window_class, str):
        return window_class

    return None
