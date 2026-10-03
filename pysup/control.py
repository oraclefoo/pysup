"""Control protocol between the CLI and the supervisor daemon.

Requests and responses are single JSON objects terminated by a newline,
exchanged over a Unix domain socket (one request per connection).
"""

from __future__ import annotations

import asyncio
import json
import socket
from typing import Any

MAX_MESSAGE_BYTES = 1024 * 1024


class ProtocolError(Exception):
    """A malformed control message."""


class NotRunningError(ConnectionError):
    """The supervisor daemon is not reachable on its control socket."""


def encode(message: dict[str, Any]) -> bytes:
    return json.dumps(message, separators=(",", ":")).encode() + b"\n"


def decode(line: bytes) -> dict[str, Any]:
    try:
        message = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"invalid JSON: {exc}") from None
    if not isinstance(message, dict):
        raise ProtocolError("message must be a JSON object")
    return message


async def read_message(reader: asyncio.StreamReader) -> dict[str, Any]:
    try:
        line = await reader.readline()
    except (asyncio.LimitOverrunError, ValueError):
        raise ProtocolError("message too large") from None
    if not line:
        raise ProtocolError("connection closed before a message was received")
    return decode(line)


async def write_message(writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
    writer.write(encode(message))
    await writer.drain()


def call(socket_path: str, command: str, connect_timeout: float = 5.0, **params: Any) -> dict:
    """Send one command to the daemon and return its response (blocking)."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(connect_timeout)
        try:
            sock.connect(socket_path)
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            raise NotRunningError(
                f"pysup is not running (cannot connect to {socket_path}: {exc.strerror})"
            ) from None
        # Commands like stop/restart can legitimately take a while.
        sock.settimeout(None)
        sock.sendall(encode({"cmd": command, **params}))
        chunks = []
        size = 0
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if chunk.endswith(b"\n"):
                break
            if size > MAX_MESSAGE_BYTES:
                raise ProtocolError("response too large")
    data = b"".join(chunks)
    if not data:
        raise ProtocolError("daemon closed the connection without responding")
    return decode(data)


def is_running(socket_path: str) -> bool:
    try:
        return bool(call(socket_path, "ping", connect_timeout=2.0).get("ok"))
    except (NotRunningError, ProtocolError, OSError):
        return False
