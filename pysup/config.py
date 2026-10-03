"""Loading and validating the YAML configuration file."""

from __future__ import annotations

import os
import re
import shlex
import signal
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_LOG_BACKUPS = 5
LOG_LEVELS = ("debug", "info", "warning", "error", "critical")


class ConfigError(ValueError):
    """The configuration file is missing or invalid."""


class RestartPolicy(str, Enum):
    NEVER = "never"
    ALWAYS = "always"
    ON_UNEXPECTED_EXIT = "on-unexpected-exit"


@dataclass(frozen=True)
class BackoffConfig:
    initial: float = 1.0
    multiplier: float = 2.0
    max: float = 60.0
    max_retries: int | None = 5
    reset_after: float = 30.0

    def delay(self, attempt: int) -> float:
        """Seconds to wait before restart number ``attempt`` (1-based)."""
        try:
            value = self.initial * self.multiplier ** max(attempt - 1, 0)
        except OverflowError:
            return self.max
        return min(value, self.max)


@dataclass(frozen=True)
class ProgramConfig:
    name: str
    command: tuple[str, ...]
    directory: str
    stdout_log: str
    stderr_log: str
    environment: Mapping[str, str] = field(default_factory=dict)
    inherit_env: bool = True
    autostart: bool = True
    autorestart: RestartPolicy = RestartPolicy.ON_UNEXPECTED_EXIT
    exitcodes: tuple[int, ...] = (0,)
    startsecs: float = 1.0
    backoff: BackoffConfig = field(default_factory=BackoffConfig)
    redirect_stderr: bool = False
    log_max_bytes: int = DEFAULT_LOG_MAX_BYTES
    log_backups: int = DEFAULT_LOG_BACKUPS
    stop_signal: signal.Signals = signal.SIGTERM
    stop_timeout: float = 10.0
    stop_as_group: bool = True
    priority: int = 100

    def requires_restart(self, other: ProgramConfig) -> bool:
        """True if switching from this config to ``other`` needs a new process."""
        return any(getattr(self, f) != getattr(other, f) for f in RESTART_FIELDS)


# Fields that affect how the child process is spawned. Changing any other field
# on reload is applied in place without restarting the process.
RESTART_FIELDS = (
    "command",
    "directory",
    "environment",
    "inherit_env",
    "stdout_log",
    "stderr_log",
    "redirect_stderr",
    "log_max_bytes",
    "log_backups",
)


@dataclass(frozen=True)
class SupervisorConfig:
    socket: str
    pidfile: str
    log_dir: str
    log_file: str
    log_level: str = "info"
    log_max_bytes: int = DEFAULT_LOG_MAX_BYTES
    log_backups: int = DEFAULT_LOG_BACKUPS
    programs: Mapping[str, ProgramConfig] = field(default_factory=dict)
    config_path: str | None = None


_SUPERVISOR_KEYS = {
    "socket",
    "pidfile",
    "log_dir",
    "log_file",
    "log_level",
    "log_max_bytes",
    "log_backups",
}
_PROGRAM_KEYS = {
    "name",
    "command",
    "directory",
    "environment",
    "inherit_env",
    "autostart",
    "autorestart",
    "exitcodes",
    "startsecs",
    "backoff",
    "stdout_log",
    "stderr_log",
    "redirect_stderr",
    "log_max_bytes",
    "log_backups",
    "stop_signal",
    "stop_timeout",
    "stop_as_group",
    "priority",
}
_BACKOFF_KEYS = {"initial", "multiplier", "max", "max_retries", "reset_after"}
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMG]?)B?\s*$", re.IGNORECASE)
_SIZE_UNITS = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3}


def load_config(path: str | os.PathLike[str]) -> SupervisorConfig:
    """Read and validate a YAML config file.

    Relative paths inside the file are resolved against the file's directory.
    """
    config_path = Path(path).expanduser().resolve()
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config file {config_path}: {exc.strerror}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {config_path}: {exc}") from exc
    return parse_config(data, base_dir=config_path.parent, config_path=config_path)


def parse_config(
    data: Any, base_dir: str | os.PathLike[str], config_path: str | os.PathLike[str] | None = None
) -> SupervisorConfig:
    """Validate already-parsed YAML data and build a :class:`SupervisorConfig`."""
    base = Path(base_dir)
    data = _mapping({} if data is None else data, "top level")
    _check_keys(data, {"supervisor", "programs"}, "top level")

    sup = _mapping(data.get("supervisor") or {}, "supervisor")
    _check_keys(sup, _SUPERVISOR_KEYS, "supervisor")
    log_dir = _path(sup.get("log_dir", "logs"), base, "supervisor.log_dir")
    log_level = _str(sup.get("log_level", "info"), "supervisor.log_level").lower()
    if log_level not in LOG_LEVELS:
        raise ConfigError(f"supervisor.log_level: must be one of {', '.join(LOG_LEVELS)}")

    programs = _parse_programs(data.get("programs"), base, log_dir)

    return SupervisorConfig(
        socket=_path(sup.get("socket", "pysup.sock"), base, "supervisor.socket"),
        pidfile=_path(sup.get("pidfile", "pysup.pid"), base, "supervisor.pidfile"),
        log_dir=log_dir,
        log_file=_path(
            sup.get("log_file", os.path.join(log_dir, "pysup.log")), base, "supervisor.log_file"
        ),
        log_level=log_level,
        log_max_bytes=parse_size(
            sup.get("log_max_bytes", DEFAULT_LOG_MAX_BYTES), "supervisor.log_max_bytes"
        ),
        log_backups=_int(sup.get("log_backups", DEFAULT_LOG_BACKUPS), "supervisor.log_backups"),
        programs=programs,
        config_path=str(config_path) if config_path is not None else None,
    )


def _parse_programs(raw: Any, base: Path, log_dir: str) -> dict[str, ProgramConfig]:
    if raw is None:
        return {}
    items: list[tuple[Any, Any]]
    if isinstance(raw, dict):
        items = list(raw.items())
    elif isinstance(raw, list):
        items = []
        for index, entry in enumerate(raw):
            entry = _mapping(entry, f"programs[{index}]")
            if "name" not in entry:
                raise ConfigError(f"programs[{index}]: 'name' is required")
            items.append((entry["name"], entry))
    else:
        raise ConfigError("programs: must be a mapping of name -> program or a list of programs")

    programs: dict[str, ProgramConfig] = {}
    for name, entry in items:
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise ConfigError(
                f"programs: invalid program name {name!r} (use letters, digits, '.', '_' and '-')"
            )
        if name == "all":
            raise ConfigError("programs: 'all' is reserved and cannot be used as a program name")
        if name in programs:
            raise ConfigError(f"programs: duplicate program name {name!r}")
        programs[name] = _parse_program(name, entry, base, log_dir)
    return programs


def _parse_program(name: str, raw: Any, base: Path, log_dir: str) -> ProgramConfig:
    where = f"programs.{name}"
    data = _mapping(raw, where)
    _check_keys(data, _PROGRAM_KEYS, where)
    if data.get("name", name) != name:
        raise ConfigError(f"{where}: 'name' does not match the program key")
    if "command" not in data:
        raise ConfigError(f"{where}: 'command' is required")

    redirect_stderr = _bool(data.get("redirect_stderr", False), f"{where}.redirect_stderr")
    stdout_log = _path(
        data.get("stdout_log", os.path.join(log_dir, f"{name}.stdout.log")),
        base,
        f"{where}.stdout_log",
    )
    if redirect_stderr:
        if "stderr_log" in data:
            raise ConfigError(f"{where}: 'stderr_log' cannot be set with redirect_stderr: true")
        stderr_log = stdout_log
    else:
        stderr_log = _path(
            data.get("stderr_log", os.path.join(log_dir, f"{name}.stderr.log")),
            base,
            f"{where}.stderr_log",
        )

    env_raw = _mapping(data.get("environment") or {}, f"{where}.environment")
    environment: dict[str, str] = {}
    for key, value in env_raw.items():
        if not isinstance(key, str) or not key or "=" in key:
            raise ConfigError(f"{where}.environment: invalid variable name {key!r}")
        if value is None or isinstance(value, (dict, list)):
            raise ConfigError(f"{where}.environment.{key}: must be a scalar value")
        environment[key] = _env_value(value)

    policy_raw = data.get("autorestart", RestartPolicy.ON_UNEXPECTED_EXIT.value)
    try:
        autorestart = RestartPolicy(policy_raw)
    except ValueError:
        choices = ", ".join(p.value for p in RestartPolicy)
        raise ConfigError(f"{where}.autorestart: must be one of {choices}") from None

    exitcodes_raw = data.get("exitcodes", [0])
    if isinstance(exitcodes_raw, int) and not isinstance(exitcodes_raw, bool):
        exitcodes_raw = [exitcodes_raw]
    if not isinstance(exitcodes_raw, list) or not exitcodes_raw:
        raise ConfigError(f"{where}.exitcodes: must be a non-empty list of integers")
    exitcodes = tuple(_int(c, f"{where}.exitcodes", maximum=255) for c in exitcodes_raw)

    return ProgramConfig(
        name=name,
        command=_command(data["command"], f"{where}.command"),
        directory=_path(data.get("directory", "."), base, f"{where}.directory"),
        stdout_log=stdout_log,
        stderr_log=stderr_log,
        environment=environment,
        inherit_env=_bool(data.get("inherit_env", True), f"{where}.inherit_env"),
        autostart=_bool(data.get("autostart", True), f"{where}.autostart"),
        autorestart=autorestart,
        exitcodes=exitcodes,
        startsecs=_number(data.get("startsecs", 1.0), f"{where}.startsecs"),
        backoff=_parse_backoff(data.get("backoff"), f"{where}.backoff"),
        redirect_stderr=redirect_stderr,
        log_max_bytes=parse_size(
            data.get("log_max_bytes", DEFAULT_LOG_MAX_BYTES), f"{where}.log_max_bytes"
        ),
        log_backups=_int(data.get("log_backups", DEFAULT_LOG_BACKUPS), f"{where}.log_backups"),
        stop_signal=parse_signal(data.get("stop_signal", "TERM"), f"{where}.stop_signal"),
        stop_timeout=_number(data.get("stop_timeout", 10.0), f"{where}.stop_timeout"),
        stop_as_group=_bool(data.get("stop_as_group", True), f"{where}.stop_as_group"),
        priority=_int(data.get("priority", 100), f"{where}.priority", minimum=None),
    )


def _parse_backoff(raw: Any, where: str) -> BackoffConfig:
    data = _mapping(raw or {}, where)
    _check_keys(data, _BACKOFF_KEYS, where)
    defaults = BackoffConfig()
    max_retries_raw = data.get("max_retries", defaults.max_retries)
    backoff = BackoffConfig(
        initial=_number(data.get("initial", defaults.initial), f"{where}.initial"),
        multiplier=_number(data.get("multiplier", defaults.multiplier), f"{where}.multiplier"),
        max=_number(data.get("max", defaults.max), f"{where}.max"),
        max_retries=None
        if max_retries_raw is None
        else _int(max_retries_raw, f"{where}.max_retries"),
        reset_after=_number(data.get("reset_after", defaults.reset_after), f"{where}.reset_after"),
    )
    if backoff.multiplier < 1:
        raise ConfigError(f"{where}.multiplier: must be >= 1")
    if backoff.max < backoff.initial:
        raise ConfigError(f"{where}.max: must be >= initial")
    return backoff


def parse_size(value: Any, where: str = "size") -> int:
    """Parse ``1048576``, ``"512KB"``, ``"10MB"`` or ``"1G"`` into bytes."""
    if isinstance(value, int) and not isinstance(value, bool):
        if value < 0:
            raise ConfigError(f"{where}: must be >= 0")
        return value
    if isinstance(value, str):
        match = _SIZE_RE.match(value)
        if match:
            return int(float(match.group(1)) * _SIZE_UNITS[match.group(2).upper()])
    raise ConfigError(f"{where}: invalid size {value!r} (examples: 1048576, 512KB, 10MB, 1GB)")


def parse_signal(value: Any, where: str = "stop_signal") -> signal.Signals:
    """Parse ``"TERM"``, ``"SIGTERM"`` or ``15`` into a :class:`signal.Signals`."""
    try:
        if isinstance(value, int) and not isinstance(value, bool):
            return signal.Signals(value)
        if isinstance(value, str):
            name = value.strip().upper()
            if not name.startswith("SIG"):
                name = "SIG" + name
            return signal.Signals[name]
    except (KeyError, ValueError):
        pass
    raise ConfigError(f"{where}: unknown signal {value!r}")


def _mapping(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: expected a mapping, got {type(value).__name__}")
    return value


def _check_keys(data: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(str(k) for k in data if k not in allowed)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s): {', '.join(unknown)}")


def _str(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{where}: expected a non-empty string")
    return value


def _bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: expected true or false")
    return value


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}: expected a number")
    if value < 0:
        raise ConfigError(f"{where}: must be >= 0")
    return float(value)


def _int(value: Any, where: str, minimum: int | None = 0, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}: expected an integer")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{where}: must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{where}: must be <= {maximum}")
    return value


def _env_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _path(value: Any, base: Path, where: str) -> str:
    text = os.path.expandvars(os.path.expanduser(_str(value, where)))
    return os.path.normpath(os.path.join(base, text))


def _command(value: Any, where: str) -> tuple[str, ...]:
    if isinstance(value, str):
        try:
            argv = shlex.split(value)
        except ValueError as exc:
            raise ConfigError(f"{where}: {exc}") from None
    elif isinstance(value, list) and all(isinstance(a, (str, int, float)) for a in value):
        argv = [str(a) for a in value]
    else:
        raise ConfigError(f"{where}: expected a string or a list of strings")
    if not argv:
        raise ConfigError(f"{where}: must not be empty")
    return tuple(argv)
