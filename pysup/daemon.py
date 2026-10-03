"""Detaching the supervisor from the terminal (classic double fork)."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable

ReadyCallback = Callable[[str | None], None]


def daemonize() -> ReadyCallback:
    """Detach into the background.

    The original process stays in the foreground until the daemon reports
    readiness through the returned callback (``None`` for success, or an error
    message), then exits with status 0 or 1. Only the daemon returns.
    """
    read_fd, write_fd = os.pipe()
    if os.fork() > 0:
        os.close(write_fd)
        with os.fdopen(read_fd, "rb") as pipe:
            report = pipe.read().decode(errors="replace")
        if report.startswith("ok:"):
            print(f"pysup started (pid {report[3:].strip()})")
            os._exit(0)
        print(f"error: {report or 'daemon exited during startup'}", file=sys.stderr)
        os._exit(1)

    os.close(read_fd)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    os.chdir("/")
    os.umask(0o022)
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    os.close(devnull)

    def ready(error: str | None) -> None:
        nonlocal write_fd
        if write_fd < 0:
            return
        message = f"ok:{os.getpid()}" if error is None else error
        try:
            os.write(write_fd, message.encode())
        finally:
            os.close(write_fd)
            write_fd = -1

    return ready
