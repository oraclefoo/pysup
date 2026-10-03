import os
import signal

import pytest

from pysup.config import (
    BackoffConfig,
    ConfigError,
    RestartPolicy,
    parse_config,
    parse_signal,
    parse_size,
)

from .conftest import make_config


def test_defaults_and_path_resolution(workdir):
    cfg = make_config(
        workdir,
        """
        programs:
          web:
            command: python3 -m http.server
        """,
    )
    assert cfg.socket == str(workdir / "pysup.sock")
    assert cfg.log_file == str(workdir / "logs" / "pysup.log")
    web = cfg.programs["web"]
    assert web.command == ("python3", "-m", "http.server")
    assert web.directory == str(workdir)
    assert web.stdout_log == str(workdir / "logs" / "web.stdout.log")
    assert web.stderr_log == str(workdir / "logs" / "web.stderr.log")
    assert web.autostart is True
    assert web.autorestart is RestartPolicy.ON_UNEXPECTED_EXIT
    assert web.stop_signal is signal.SIGTERM
    assert web.backoff == BackoffConfig()


def test_full_program_options(workdir, monkeypatch):
    monkeypatch.setenv("APP_HOME", "/srv/app")
    cfg = make_config(
        workdir,
        """
        supervisor:
          log_dir: /var/tmp/pysup-logs
        programs:
          - name: worker
            command: [python3, worker.py, --queue, default]
            directory: $APP_HOME
            environment: {DEBUG: true, WORKERS: 4}
            autostart: false
            autorestart: always
            exitcodes: [0, 2]
            backoff: {initial: 0.5, multiplier: 3, max: 30, max_retries: null}
            redirect_stderr: true
            log_max_bytes: 5MB
            log_backups: 2
            stop_signal: SIGINT
            stop_timeout: 3
            priority: 5
        """,
    )
    worker = cfg.programs["worker"]
    assert worker.command == ("python3", "worker.py", "--queue", "default")
    assert worker.directory == "/srv/app"
    assert worker.environment == {"DEBUG": "true", "WORKERS": "4"}
    assert worker.autorestart is RestartPolicy.ALWAYS
    assert worker.exitcodes == (0, 2)
    assert worker.backoff.max_retries is None
    assert worker.stdout_log == worker.stderr_log == "/var/tmp/pysup-logs/worker.stdout.log"
    assert worker.log_max_bytes == 5 * 1024 * 1024
    assert worker.stop_signal is signal.SIGINT
    assert worker.priority == 5


@pytest.mark.parametrize(
    ("programs", "message"),
    [
        ({"web": {}}, "'command' is required"),
        ({"web": {"command": "x", "comand": "y"}}, "unknown key(s): comand"),
        ({"web": {"command": "x", "autorestart": "sometimes"}}, "autorestart: must be one of"),
        ({"web": {"command": "x", "stop_signal": "NOPE"}}, "unknown signal"),
        ({"web": {"command": "x", "startsecs": -1}}, "must be >= 0"),
        ({"web": {"command": "x", "autostart": "yes"}}, "expected true or false"),
        ({"bad name": {"command": "x"}}, "invalid program name"),
        ({"all": {"command": "x"}}, "'all' is reserved"),
        ({"web": {"command": ""}}, "must not be empty"),
        ({"web": {"command": "x", "backoff": {"multiplier": 0.5}}}, "multiplier: must be >= 1"),
    ],
)
def test_validation_errors(workdir, programs, message):
    with pytest.raises(ConfigError, match=None) as excinfo:
        parse_config({"programs": programs}, base_dir=workdir)
    assert message in str(excinfo.value)


def test_missing_and_invalid_files(workdir):
    from pysup.config import load_config

    with pytest.raises(ConfigError, match="cannot read config file"):
        load_config(workdir / "missing.yaml")
    bad = workdir / "bad.yaml"
    bad.write_text("programs: [unclosed")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(bad)


def test_parse_size_and_signal():
    assert parse_size(100) == 100
    assert parse_size("512KB") == 512 * 1024
    assert parse_size("10MB") == 10 * 1024**2
    assert parse_size("1g") == 1024**3
    with pytest.raises(ConfigError):
        parse_size("lots")
    assert parse_signal("TERM") is signal.SIGTERM
    assert parse_signal("sighup") is signal.SIGHUP
    assert parse_signal(9) is signal.SIGKILL


def test_backoff_delay_is_exponential_and_capped():
    backoff = BackoffConfig(initial=1, multiplier=2, max=10)
    assert [backoff.delay(n) for n in range(1, 7)] == [1, 2, 4, 8, 10, 10]
    assert backoff.delay(10_000) == 10


def test_requires_restart_only_for_process_fields(workdir):
    base = {"command": "sleep 1"}
    old = parse_config({"programs": {"p": base}}, base_dir=workdir).programs["p"]
    tweaked = parse_config(
        {"programs": {"p": {**base, "autorestart": "always", "stop_timeout": 1}}},
        base_dir=workdir,
    ).programs["p"]
    new_env = parse_config(
        {"programs": {"p": {**base, "environment": {"A": 1}}}}, base_dir=workdir
    ).programs["p"]
    assert old != tweaked and not old.requires_restart(tweaked)
    assert old.requires_restart(new_env)
    assert os.path.isabs(old.directory)
