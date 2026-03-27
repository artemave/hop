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


@dataclass(frozen=True, slots=True)
class SwayWorkspace:
    name: str
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
        payload = f"workspace {json.dumps(workspace_name)}".encode()
        response = self._transport.request(SwayMessageType.RUN_COMMAND, payload)
        results = json.loads(response.decode())

        if not results or not all(result.get("success") for result in results):
            msg = f"Sway could not switch to workspace {workspace_name!r}."
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
