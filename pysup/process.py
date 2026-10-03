"""A single supervised program: spawning, output capture, state and restarts."""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import signal
import subprocess
import time
from collections.abc import Callable
from enum import Enum
from typing import Any

from pysup.config import ProgramConfig, RestartPolicy
from pysup.logfile import RotatingFileWriter

log = logging.getLogger("pysup.process")

# How long to keep reading a child's output pipes after it exits. Grandchildren
# may keep the pipes open; we stop waiting for them after this.
DRAIN_TIMEOUT = 1.0


class State(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    BACKOFF = "backoff"
    STOPPING = "stopping"
    EXITED = "exited"
    FAILED = "failed"


def describe_exit(returncode: int) -> str:
    if returncode < 0:
        try:
            name = signal.Signals(-returncode).name
        except ValueError:
            name = f"signal {-returncode}"
        return f"terminated by {name}"
    return f"exited with status {returncode}"


class _OutputPipe:
    """Reads one end of a child's stdout/stderr pipe on the event loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop, fd: int, writer: RotatingFileWriter):
        self._loop = loop
        self._fd: int | None = fd
        self._writer = writer
        self.closed: asyncio.Future[None] = loop.create_future()
        os.set_blocking(fd, False)
        loop.add_reader(fd, self._on_readable)

    def _on_readable(self) -> None:
        assert self._fd is not None
        try:
            data = os.read(self._fd, 65536)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            self.close()
            return
        try:
            self._writer.write(data)
        except OSError as exc:
            log.error("failed to write log %s: %s", self._writer.path, exc)

    def close(self) -> None:
        if self._fd is None:
            return
        self._loop.remove_reader(self._fd)
        os.close(self._fd)
        self._fd = None
        if not self.closed.done():
            self.closed.set_result(None)


class Program:
    """Supervises one program defined by a :class:`ProgramConfig`.

    A background task (the *lifecycle*) owns the child process while the
    program is active: it spawns it, waits for it to exit, and decides whether
    to restart it (with exponential backoff) according to the restart policy.
    """

    def __init__(self, config: ProgramConfig) -> None:
        self.config = config
        self.state = State.STOPPED
        self.pid: int | None = None
        self.started_at: float | None = None
        self.retries = 0
        self.exit_code: int | None = None
        self.exit_signal: str | None = None
        self.message = ""
        self.next_start_at: float | None = None
        self._started_mono: float | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop_requested = False
        self._wakeup = asyncio.Event()
        self._state_changed = asyncio.Event()
        self._lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def is_active(self) -> bool:
        """True while the lifecycle task runs (starting/running/backoff/stopping)."""
        return self._task is not None and not self._task.done()

    @property
    def uptime(self) -> float | None:
        if self.pid is None or self._started_mono is None:
            return None
        return time.monotonic() - self._started_mono

    async def start(self, wait: bool = True) -> tuple[bool, str]:
        """Start the program. Returns ``(ok, message)``."""
        async with self._lock:
            if self.is_active:
                return True, f"already {self.state.value}"
            self._stop_requested = False
            self._wakeup = asyncio.Event()
            self.retries = 0
            self.message = ""
            self.exit_code = self.exit_signal = None
            self._set_state(State.STARTING)
            self._task = asyncio.create_task(self._lifecycle(), name=f"pysup:{self.name}")
        if not wait:
            return True, "starting"
        await self.wait_for_state(lambda s: s is not State.STARTING, self.config.startsecs + 5)
        return self._start_result()

    async def stop(self) -> tuple[bool, str]:
        """Stop the program, escalating to SIGKILL after ``stop_timeout``."""
        async with self._lock:
            if not self.is_active or self._task is None:
                return True, f"not running ({self.state.value})"
            task = self._task
            self._stop_requested = True
            self._wakeup.set()
            if self._proc is not None and self._proc.returncode is None:
                self._set_state(State.STOPPING)
                self._send_signal(self.config.stop_signal)
            try:
                await asyncio.wait_for(asyncio.shield(task), self.config.stop_timeout)
            except asyncio.TimeoutError:
                log.warning(
                    "%s: did not stop within %.1fs, sending SIGKILL",
                    self.name,
                    self.config.stop_timeout,
                )
                self._send_signal(signal.SIGKILL)
                await task
            return True, "stopped"

    async def restart(self) -> tuple[bool, str]:
        await self.stop()
        return await self.start()

    async def wait_for_state(self, predicate: Callable[[State], bool], timeout: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not predicate(self.state):
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            event = self._state_changed
            try:
                await asyncio.wait_for(event.wait(), remaining)
            except asyncio.TimeoutError:
                return predicate(self.state)
        return True

    def status(self) -> dict[str, Any]:
        next_start_in = None
        if self.state is State.BACKOFF and self.next_start_at is not None:
            next_start_in = max(0.0, self.next_start_at - asyncio.get_running_loop().time())
        return {
            "name": self.name,
            "state": self.state.value,
            "pid": self.pid,
            "uptime": self.uptime,
            "started_at": self.started_at if self.pid is not None else None,
            "retries": self.retries,
            "exit_code": self.exit_code,
            "exit_signal": self.exit_signal,
            "message": self.message,
            "next_start_in": next_start_in,
            "command": shlex.join(self.config.command),
        }

    # -- internals ---------------------------------------------------------

    def _set_state(self, state: State) -> None:
        if state is not self.state:
            log.debug("%s: %s -> %s", self.name, self.state.value, state.value)
        self.state = state
        event, self._state_changed = self._state_changed, asyncio.Event()
        event.set()

    def _start_result(self) -> tuple[bool, str]:
        state = self.state
        if state is State.RUNNING:
            return True, f"started (pid {self.pid})"
        if state is State.STARTING:
            return True, "still starting"
        if state is State.EXITED:
            return True, self.message
        if state is State.BACKOFF:
            return False, f"{self.message}; will retry (attempt {self.retries})"
        return False, self.message or state.value

    def _send_signal(self, sig: signal.Signals) -> None:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        try:
            if self.config.stop_as_group:
                os.killpg(proc.pid, sig)
            else:
                proc.send_signal(sig)
        except ProcessLookupError:
            pass

    async def _lifecycle(self) -> None:
        try:
            await self._run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("%s: internal error", self.name)
            self.message = f"internal error: {exc}"
            self._set_state(State.FAILED)
        finally:
            self._proc = None
            self.pid = None

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            self.next_start_at = None
            if self.state is not State.STARTING:
                self._set_state(State.STARTING)
            spawned_at = loop.time()
            try:
                proc, pipes, writers = await self._spawn()
            except OSError as exc:
                self.message = f"spawn error: {exc}"
                self.exit_code = self.exit_signal = None
                log.error("%s: %s", self.name, self.message)
                unexpected = True
            else:
                try:
                    returncode = await self._supervise(proc, pipes)
                finally:
                    for writer in writers:
                        writer.close()
                unexpected = self._record_exit(returncode)

            if self._stop_requested:
                self.message = ""
                self._set_state(State.STOPPED)
                return

            cfg = self.config
            restart = cfg.autorestart is RestartPolicy.ALWAYS or (
                cfg.autorestart is RestartPolicy.ON_UNEXPECTED_EXIT and unexpected
            )
            if not restart:
                self._set_state(State.FAILED if unexpected else State.EXITED)
                return

            if loop.time() - spawned_at >= cfg.backoff.reset_after:
                self.retries = 0
            self.retries += 1
            max_retries = cfg.backoff.max_retries
            if max_retries is not None and self.retries > max_retries:
                self.message += f"; giving up after {max_retries} restart attempt(s)"
                log.error("%s: %s", self.name, self.message)
                self._set_state(State.FAILED)
                return

            delay = cfg.backoff.delay(self.retries)
            self.next_start_at = loop.time() + delay
            log.info("%s: restarting in %.2fs (attempt %d)", self.name, delay, self.retries)
            self._set_state(State.BACKOFF)
            try:
                await asyncio.wait_for(self._wakeup.wait(), delay)
            except asyncio.TimeoutError:
                pass
            if self._stop_requested:
                self.message = ""
                self._set_state(State.STOPPED)
                return

    async def _spawn(
        self,
    ) -> tuple[asyncio.subprocess.Process, list[_OutputPipe], list[RotatingFileWriter]]:
        cfg = self.config
        env = dict(os.environ) if cfg.inherit_env else {}
        env.update(cfg.environment)
        env["PYSUP_PROGRAM"] = cfg.name

        writers: list[RotatingFileWriter] = []
        read_fds: list[int] = []
        write_fds: list[int] = []
        try:
            out_writer = RotatingFileWriter(cfg.stdout_log, cfg.log_max_bytes, cfg.log_backups)
            writers.append(out_writer)
            out_r, out_w = os.pipe()
            read_fds.append(out_r)
            write_fds.append(out_w)
            if cfg.redirect_stderr:
                err_writer, err_r, err_w = out_writer, None, out_w
            else:
                if cfg.stderr_log == cfg.stdout_log:
                    err_writer = out_writer
                else:
                    err_writer = RotatingFileWriter(
                        cfg.stderr_log, cfg.log_max_bytes, cfg.log_backups
                    )
                    writers.append(err_writer)
                err_r, err_w = os.pipe()
                read_fds.append(err_r)
                write_fds.append(err_w)
            proc = await asyncio.create_subprocess_exec(
                *cfg.command,
                stdin=subprocess.DEVNULL,
                stdout=out_w,
                stderr=err_w,
                cwd=cfg.directory,
                env=env,
                start_new_session=True,
            )
        except BaseException:
            for fd in read_fds:
                os.close(fd)
            for writer in writers:
                writer.close()
            raise
        finally:
            for fd in write_fds:
                os.close(fd)

        loop = asyncio.get_running_loop()
        pipes = [_OutputPipe(loop, out_r, out_writer)]
        if err_r is not None:
            pipes.append(_OutputPipe(loop, err_r, err_writer))
        self._proc = proc
        self.pid = proc.pid
        self.started_at = time.time()
        self._started_mono = time.monotonic()
        log.info("%s: spawned pid %d: %s", self.name, proc.pid, shlex.join(cfg.command))
        return proc, pipes, writers

    async def _supervise(self, proc: asyncio.subprocess.Process, pipes: list[_OutputPipe]) -> int:
        wait_task = asyncio.ensure_future(proc.wait())
        if self._stop_requested:
            self._send_signal(self.config.stop_signal)
        if self.config.startsecs > 0:
            await asyncio.wait({wait_task}, timeout=self.config.startsecs)
        if not wait_task.done() and not self._stop_requested:
            self.message = ""
            self._set_state(State.RUNNING)
        returncode = await wait_task
        self.pid = None
        await asyncio.wait([p.closed for p in pipes], timeout=DRAIN_TIMEOUT)
        for pipe in pipes:
            pipe.close()
        return returncode

    def _record_exit(self, returncode: int) -> bool:
        """Record how the process exited; return True if the exit was unexpected."""
        self.message = describe_exit(returncode)
        if returncode < 0:
            self.exit_code = None
            self.exit_signal = signal.Signals(-returncode).name if -returncode in _SIGNUMS else None
            unexpected = True
        else:
            self.exit_code = returncode
            self.exit_signal = None
            unexpected = returncode not in self.config.exitcodes
        if self._stop_requested:
            log.info("%s: stopped (%s)", self.name, self.message)
        elif unexpected:
            log.warning("%s: %s (unexpected)", self.name, self.message)
        else:
            log.info("%s: %s", self.name, self.message)
        return unexpected


_SIGNUMS = {s.value for s in signal.Signals}
