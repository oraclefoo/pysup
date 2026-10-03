"""The ``pysup`` command line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Sequence
from typing import Any

from pysup import __version__, control
from pysup.config import ConfigError, SupervisorConfig, load_config
from pysup.logfile import follow, tail_lines

DEFAULT_CONFIG_FILES = ("pysup.yaml", "pysup.yml")


def find_config(explicit: str | None) -> str:
    if explicit:
        return explicit
    env = os.environ.get("PYSUP_CONFIG")
    if env:
        return env
    for candidate in DEFAULT_CONFIG_FILES:
        if os.path.exists(candidate):
            return candidate
    raise ConfigError(
        "no config file found: pass -c/--config, set PYSUP_CONFIG, or create ./pysup.yaml"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pysup", description="A small process supervisor.")
    parser.add_argument("-c", "--config", help="path to the YAML config (default: ./pysup.yaml)")
    parser.add_argument("--version", action="version", version=f"pysup {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    daemon = sub.add_parser("daemon", help="run the supervisor")
    daemon.add_argument("-d", "--detach", action="store_true", help="run in the background")

    for name, verb in (("start", "start"), ("stop", "stop"), ("restart", "restart")):
        p = sub.add_parser(name, help=f"{verb} programs")
        p.add_argument("names", nargs="+", metavar="NAME", help="program names, or 'all'")

    status = sub.add_parser("status", help="show program status")
    status.add_argument("names", nargs="*", metavar="NAME")
    status.add_argument("--json", action="store_true", help="print raw JSON")

    logs = sub.add_parser("logs", help="show a program's log")
    logs.add_argument("name", metavar="NAME")
    logs.add_argument("-n", "--lines", type=int, default=20, help="lines to show (default 20)")
    logs.add_argument("-f", "--follow", action="store_true", help="keep printing new output")
    logs.add_argument("--stderr", action="store_true", help="show the stderr log")

    sub.add_parser("reload", help="re-read the config file and apply changes")

    shutdown = sub.add_parser("shutdown", help="stop all programs and the supervisor")
    shutdown.add_argument(
        "--no-wait", action="store_true", help="don't wait for the supervisor to exit"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(find_config(args.config))
        return COMMANDS[args.command](config, args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (control.NotRunningError, control.ProtocolError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def cmd_daemon(config: SupervisorConfig, args: argparse.Namespace) -> int:
    from pysup.supervisor import run

    return run(config, detach=args.detach)


def cmd_control(config: SupervisorConfig, args: argparse.Namespace) -> int:
    response = control.call(config.socket, args.command, names=args.names)
    if not response.get("ok") and "error" in response:
        print(f"error: {response['error']}", file=sys.stderr)
        return 1
    for result in response.get("results", []):
        print(f"{result['name']}: {result['message']} [{result['state']}]")
    return 0 if response.get("ok") else 1


def cmd_status(config: SupervisorConfig, args: argparse.Namespace) -> int:
    response = control.call(config.socket, "status", names=args.names)
    if not response.get("ok"):
        print(f"error: {response.get('error')}", file=sys.stderr)
        return 1
    programs = response["programs"]
    if args.json:
        print(json.dumps(programs, indent=2))
    else:
        print(format_status(programs))
    return 0


def format_status(programs: list[dict[str, Any]]) -> str:
    rows = [("NAME", "STATE", "PID", "UPTIME", "INFO")]
    for p in programs:
        rows.append(
            (
                p["name"],
                p["state"],
                str(p["pid"]) if p["pid"] else "-",
                format_duration(p["uptime"]) if p["uptime"] is not None else "-",
                _status_info(p),
            )
        )
    widths = [max(len(row[i]) for row in rows) for i in range(4)]
    return "\n".join(
        "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row[:4])) + "  " + row[4]
        for row in rows
    ).rstrip()


def _status_info(p: dict[str, Any]) -> str:
    info = p.get("message") or ""
    if p["state"] == "backoff" and p.get("next_start_in") is not None:
        info = f"{info}; retry {p['retries']} in {p['next_start_in']:.1f}s"
    elif p["retries"] and p["state"] in ("running", "starting"):
        info = f"restarts: {p['retries']}"
    return info


def format_duration(seconds: float) -> str:
    total = int(seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    clock = f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{days}d {clock}" if days else clock


def cmd_logs(config: SupervisorConfig, args: argparse.Namespace) -> int:
    program = config.programs.get(args.name)
    if program is None:
        print(f"error: unknown program {args.name!r}", file=sys.stderr)
        return 1
    path = program.stderr_log if args.stderr else program.stdout_log
    out = sys.stdout.buffer
    try:
        out.write(tail_lines(path, args.lines))
    except FileNotFoundError:
        if not args.follow:
            print(f"error: no log file yet at {path}", file=sys.stderr)
            return 1
    out.flush()
    if args.follow:
        for chunk in follow(path):
            out.write(chunk)
            out.flush()
    return 0


def cmd_reload(config: SupervisorConfig, args: argparse.Namespace) -> int:
    response = control.call(config.socket, "reload")
    if not response.get("ok"):
        print(f"error: {response.get('error')}", file=sys.stderr)
        return 1
    for kind, names in response["changes"].items():
        if names:
            print(f"{kind}: {', '.join(names)}")
    return 0


def cmd_shutdown(config: SupervisorConfig, args: argparse.Namespace) -> int:
    response = control.call(config.socket, "shutdown")
    if not response.get("ok"):
        print(f"error: {response.get('error')}", file=sys.stderr)
        return 1
    if not args.no_wait:
        while os.path.exists(config.socket):
            time.sleep(0.1)
    print("pysup shut down")
    return 0


COMMANDS = {
    "daemon": cmd_daemon,
    "start": cmd_control,
    "stop": cmd_control,
    "restart": cmd_control,
    "status": cmd_status,
    "logs": cmd_logs,
    "reload": cmd_reload,
    "shutdown": cmd_shutdown,
}
