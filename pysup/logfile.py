"""Rotating log files for child output, plus helpers to tail them."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from pathlib import Path


class RotatingFileWriter:
    """Append bytes to a file, rotating it once it would exceed ``max_bytes``.

    Rotation renames ``app.log`` -> ``app.log.1`` -> ``app.log.2`` ... keeping at
    most ``backups`` old files. ``max_bytes=0`` disables rotation; ``backups=0``
    truncates the file instead of keeping old copies.
    """

    def __init__(self, path: str | os.PathLike[str], max_bytes: int, backups: int) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.backups = backups
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "ab")
        self._size = self._fh.tell()

    def write(self, data: bytes) -> None:
        if not data:
            return
        if self.max_bytes > 0 and self._size > 0 and self._size + len(data) > self.max_bytes:
            self.rotate()
        self._fh.write(data)
        self._fh.flush()
        self._size += len(data)

    def rotate(self) -> None:
        self._fh.close()
        if self.backups > 0:
            for index in range(self.backups - 1, 0, -1):
                src = self._backup_path(index)
                if src.exists():
                    os.replace(src, self._backup_path(index + 1))
            os.replace(self.path, self._backup_path(1))
            self._fh = open(self.path, "ab")
        else:
            self._fh = open(self.path, "wb")
        self._size = 0

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()

    def _backup_path(self, index: int) -> Path:
        return self.path.with_name(f"{self.path.name}.{index}")


def tail_lines(path: str | os.PathLike[str], lines: int) -> bytes:
    """Return the last ``lines`` lines of a file without reading all of it."""
    if lines <= 0:
        return b""
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        pos = fh.tell()
        buf = b""
        while pos > 0 and buf.count(b"\n") <= lines:
            step = min(8192, pos)
            pos -= step
            fh.seek(pos)
            buf = fh.read(step) + buf
    return b"".join(buf.splitlines(keepends=True)[-lines:])


def follow(
    path: str | os.PathLike[str],
    poll_interval: float = 0.25,
    should_stop: Callable[[], bool] = lambda: False,
) -> Iterator[bytes]:
    """Yield data appended to ``path`` from its current end, surviving rotation."""
    fh = None
    inode = None
    from_end = True
    try:
        while not should_stop():
            if fh is None:
                try:
                    fh = open(path, "rb")
                except FileNotFoundError:
                    time.sleep(poll_interval)
                    continue
                inode = os.fstat(fh.fileno()).st_ino
                if from_end:
                    fh.seek(0, os.SEEK_END)
                from_end = False
            data = fh.read(65536)
            if data:
                yield data
                continue
            try:
                st = os.stat(path)
            except FileNotFoundError:
                st = None
            if st is None or st.st_ino != inode or st.st_size < fh.tell():
                fh.close()
                fh = None
                continue
            time.sleep(poll_interval)
    finally:
        if fh is not None:
            fh.close()
