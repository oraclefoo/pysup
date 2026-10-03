"""The supervisor: owns all programs, the control socket and signal handling."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import logging.handlers
import os
import signal
import sys
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from pysup import __version__, control
from pysup.config import ConfigError, SupervisorConfig, load_config
from pysup.daemon import ReadyCallback, daemonize
from pysup.process import Program

log = logging.getLogger("pysup.supervisor")


class CommandError(Exception):
    """A control command could not be carried out."""


class Supervisor:
    def __init__(self, config: SupervisorConfig) -> None:
        self.config = config
        self.programs: dict[str, Program] = {
            name: Program(cfg) for name, cfg in config.programs.items()
        }
        self._shutdown = asyncio.Event()
        self._reload_lock = asyncio.Lock()
        self._server: asyncio.AbstractServer | None = None

    # -- program operations -------------------------------------------------

    def resolve(self, names: Iterable[str] | None) -> list[Program]:
        """Map names (or ``all`` / nothing) to programs, ordered by priority."""
        names = list(names or [])
        if not names or "all" in names:
            selected = list(self.programs.values())
        else:
            unknown = [n for n in names if n not in self.programs]
            if unknown:
                raise CommandError(f"unknown program(s): {', '.join(unknown)}")
            selected = [self.programs[n] for n in dict.fromkeys(names)]
        return sorted(selected, key=lambda p: (p.config.priority, p.name))

    async def start_programs(self, names: Iterable[str] | None) -> list[dict[str, Any]]:
        return await self._by_priority(self.resolve(names), lambda p: p.start(), reverse=False)

    async def stop_programs(self, names: Iterable[str] | None) -> list[dict[str, Any]]:
        return await self._by_priority(self.resolve(names), lambda p: p.stop(), reverse=True)

    async def restart_programs(self, names: Iterable[str] | None) -> list[dict[str, Any]]:
        programs = self.resolve(names)
        await self._by_priority(programs, lambda p: p.stop(), reverse=True)
        return await self._by_priority(programs, lambda p: p.start(), reverse=False)

    def status(self, names: Iterable[str] | None = None) -> list[dict[str, Any]]:
        return [p.status() for p in sorted(self.resolve(names), key=lambda p: p.name)]

    async def autostart(self) -> None:
        names = [p.name for p in self.programs.values() if p.config.autostart]
        if names:
            await self.start_programs(names)

    async def stop_all(self) -> None:
        active = [p.name for p in self.programs.values() if p.is_active]
        if active:
            await self.stop_programs(active)

    async def reload(self) -> dict[str, list[str]]:
        """Re-read the config file and apply the differences.

        Unchanged programs are left alone. Changes that don't affect the child
        process (restart policy, backoff, stop settings, ...) are applied in
        place. Programs whose command, environment, directory or logging
        changed are restarted; removed programs are stopped.
        """
        if self.config.config_path is None:
            raise CommandError("supervisor was not started from a config file")
        async with self._reload_lock:
            new = load_config(self.config.config_path)
            for key in ("socket", "pidfile", "log_file"):
                if getattr(new, key) != getattr(self.config, key):
                    log.warning("reload: change to supervisor.%s requires a restart", key)

            old_names, new_names = set(self.programs), set(new.programs)
            changes: dict[str, list[str]] = {
                "added": sorted(new_names - old_names),
                "removed": sorted(old_names - new_names),
                "changed": [],
                "updated": [],
                "unchanged": [],
            }
            for name in sorted(old_names & new_names):
                old_cfg, new_cfg = self.programs[name].config, new.programs[name]
                if old_cfg == new_cfg:
                    changes["unchanged"].append(name)
                elif old_cfg.requires_restart(new_cfg):
                    changes["changed"].append(name)
                else:
                    self.programs[name].config = new_cfg
                    changes["updated"].append(name)

            was_active = {n for n in changes["changed"] if self.programs[n].is_active}
            to_stop = changes["removed"] + changes["changed"]
            if to_stop:
                await self.stop_programs(to_stop)
            for name in changes["removed"]:
                del self.programs[name]
            for name in changes["changed"] + changes["added"]:
                self.programs[name] = Program(new.programs[name])
            self.config = new

            to_start = [
                n
                for n in changes["changed"] + changes["added"]
                if n in was_active or new.programs[n].autostart
            ]
            if to_start:
                await self.start_programs(to_start)
            log.info(
                "reload: %s",
                ", ".join(f"{k}={','.join(v) or '-'}" for k, v in changes.items()),
            )
            return changes

    @staticmethod
    async def _by_priority(
        programs: list[Program],
        action: Callable[[Program], Awaitable[tuple[bool, str]]],
        reverse: bool,
    ) -> list[dict[str, Any]]:
        """Run ``action`` on programs group by group in priority order.

        Programs sharing a priority are handled concurrently.
        """
        results: dict[str, dict[str, Any]] = {}
        ordered = sorted(programs, key=lambda p: p.config.priority, reverse=reverse)
        for _, group in itertools.groupby(ordered, key=lambda p: p.config.priority):
            members = list(group)
            outcomes = await asyncio.gather(*(action(p) for p in members))
            for program, (ok, message) in zip(members, outcomes, strict=True):
                results[program.name] = {
                    "name": program.name,
                    "ok": ok,
                    "message": message,
                    "state": program.state.value,
                }
        return [results[p.name] for p in programs]

    # -- daemon ---------------------------------------------------------------

    def request_shutdown(self) -> None:
        if not self._shutdown.is_set():
            log.info("shutdown requested")
            self._shutdown.set()

    async def serve(self, ready: ReadyCallback | None = None) -> None:
        """Run until shutdown is requested, then stop every program."""
        loop = asyncio.get_running_loop()
        try:
            await self._start_server()
            _write_pidfile(self.config.pidfile)
        except Exception as exc:
            if ready:
                ready(str(exc))
            raise
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.request_shutdown)
        loop.add_signal_handler(signal.SIGHUP, lambda: loop.create_task(self._reload_logged()))
        log.info("pysup %s started (pid %d)", __version__, os.getpid())
        if ready:
            ready(None)

        try:
            autostart = asyncio.create_task(self.autostart())
            await self._shutdown.wait()
            log.info("stopping all programs")
            autostart.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await autostart
            await self.stop_all()
        finally:
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                loop.remove_signal_handler(sig)
            if self._server is not None:
                self._server.close()
                await self._server.wait_closed()
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.config.socket)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.config.pidfile)
            log.info("pysup stopped")

    async def _reload_logged(self) -> None:
        try:
            await self.reload()
        except (ConfigError, CommandError) as exc:
            log.error("reload failed: %s", exc)

    async def _start_server(self) -> None:
        path = self.config.socket
        if os.path.exists(path):
            if control.is_running(path):
                raise CommandError(f"another pysup is already running on {path}")
            os.unlink(path)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        old_umask = os.umask(0o077)
        try:
            self._server = await asyncio.start_unix_server(
                self._handle_client, path=path, limit=control.MAX_MESSAGE_BYTES
            )
        except OSError as exc:
            raise CommandError(f"cannot listen on {path}: {exc}") from exc
        finally:
            os.umask(old_umask)

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request = await control.read_message(reader)
            response = await self.dispatch(request)
        except (control.ProtocolError, CommandError, ConfigError) as exc:
            response = {"ok": False, "error": str(exc)}
        except Exception as exc:
            log.exception("error handling control request")
            response = {"ok": False, "error": f"internal error: {exc}"}
        try:
            await control.write_message(writer, response)
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()

    async def dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        command = request.get("cmd")
        names = request.get("names") or []
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise control.ProtocolError("'names' must be a list of strings")

        if command == "ping":
            return {"ok": True, "pid": os.getpid(), "version": __version__}
        if command == "status":
            return {"ok": True, "programs": self.status(names)}
        if command in ("start", "stop", "restart"):
            if not names:
                raise CommandError(f"{command}: specify program names or 'all'")
            handler = {
                "start": self.start_programs,
                "stop": self.stop_programs,
                "restart": self.restart_programs,
            }[command]
            results = await handler(names)
            return {"ok": all(r["ok"] for r in results), "results": results}
        if command == "reload":
            return {"ok": True, "changes": await self.reload()}
        if command == "shutdown":
            self.request_shutdown()
            return {"ok": True}
        raise CommandError(f"unknown command {command!r}")


def _write_pidfile(path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="ascii") as fh:
        fh.write(f"{os.getpid()}\n")


def setup_logging(config: SupervisorConfig, to_stderr: bool) -> None:
    logger = logging.getLogger("pysup")
    logger.setLevel(config.log_level.upper())
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    os.makedirs(os.path.dirname(config.log_file) or ".", exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        config.log_file, maxBytes=config.log_max_bytes, backupCount=config.log_backups
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    if to_stderr:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        logger.addHandler(stream)


def run(config: SupervisorConfig, detach: bool = False) -> int:
    """Run the supervisor daemon in the foreground (or detached). Blocks."""
    if os.path.exists(config.socket) and control.is_running(config.socket):
        print(f"error: pysup is already running on {config.socket}", file=sys.stderr)
        return 1
    # Fail fast on an unusable socket path before forking (AF_UNIX path limit).
    if len(os.fsencode(config.socket)) >= 104:
        print(f"error: socket path too long: {config.socket}", file=sys.stderr)
        return 1
    ready = daemonize() if detach else None
    try:
        setup_logging(config, to_stderr=not detach)
    except OSError as exc:
        if ready:
            ready(f"cannot open log file: {exc}")
        else:
            print(f"error: cannot open log file: {exc}", file=sys.stderr)
        return 1
    try:
        asyncio.run(Supervisor(config).serve(ready))
    except CommandError as exc:
        log.error("%s", exc)
        if not detach:
            print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


__all__ = ["CommandError", "Supervisor", "run", "setup_logging"]
