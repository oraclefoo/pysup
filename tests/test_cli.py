"""End-to-end tests: run the real daemon and drive it through the CLI."""

import json
import os
import subprocess
import sys
import time

import pytest

from pysup.cli import format_duration, format_status

from .conftest import write_config


def pysup(config, *args, check=True):
    result = subprocess.run(
        [sys.executable, "-m", "pysup", "-c", str(config), *args],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"pysup {args} failed: {result.stdout}{result.stderr}")
    return result


def wait_until(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.fixture
def daemon(workdir):
    config = write_config(
        workdir,
        """
        programs:
          echo:
            command: sh -c 'echo "hi from $NAME"; exec sleep 60'
            environment: {NAME: pysup}
            startsecs: 0.2
          manual:
            command: sleep 60
            autostart: false
            startsecs: 0.2
        """,
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "pysup", "-c", str(config), "daemon"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    assert wait_until(lambda: (workdir / "pysup.sock").exists()), "daemon did not start"
    yield config, proc
    if proc.poll() is None:
        proc.terminate()
        proc.wait(10)


def status(config):
    return {p["name"]: p for p in json.loads(pysup(config, "status", "--json").stdout)}


def test_cli_end_to_end(daemon, workdir):
    config, proc = daemon
    assert wait_until(lambda: status(config)["echo"]["state"] == "running")
    assert status(config)["manual"]["state"] == "stopped"

    out = pysup(config, "start", "manual").stdout
    assert "manual: started" in out
    table = pysup(config, "status").stdout
    assert table.splitlines()[0].split() == ["NAME", "STATE", "PID", "UPTIME", "INFO"]

    old_pid = status(config)["echo"]["pid"]
    pysup(config, "restart", "echo")
    assert status(config)["echo"]["pid"] != old_pid

    assert "hi from pysup" in pysup(config, "logs", "echo", "-n", "5").stdout

    pysup(config, "stop", "manual")
    assert status(config)["manual"]["state"] == "stopped"

    result = pysup(config, "start", "nope", check=False)
    assert result.returncode == 1 and "unknown program" in result.stderr

    # reload with an unchanged config keeps the process
    pid = status(config)["echo"]["pid"]
    assert "unchanged: echo, manual" in pysup(config, "reload").stdout
    assert status(config)["echo"]["pid"] == pid


def test_shutdown_stops_children(daemon):
    config, proc = daemon
    assert wait_until(lambda: status(config)["echo"]["state"] == "running")
    pid = status(config)["echo"]["pid"]
    pysup(config, "shutdown")
    assert proc.wait(10) == 0
    assert wait_until(lambda: not pid_alive(pid))
    result = pysup(config, "status", check=False)
    assert result.returncode == 1 and "not running" in result.stderr


def test_sigterm_stops_children(daemon):
    config, proc = daemon
    assert wait_until(lambda: status(config)["echo"]["state"] == "running")
    pid = status(config)["echo"]["pid"]
    proc.terminate()
    assert proc.wait(10) == 0
    assert wait_until(lambda: not pid_alive(pid))


def test_config_error_exit_code(workdir):
    config = write_config(workdir, "programs: {web: {}}")
    result = pysup(config, "status", check=False)
    assert result.returncode == 2 and "'command' is required" in result.stderr


def test_formatting_helpers():
    assert format_duration(5) == "00:00:05"
    assert format_duration(90061) == "1d 01:01:01"
    table = format_status(
        [
            {
                "name": "web",
                "state": "backoff",
                "pid": None,
                "uptime": None,
                "retries": 2,
                "message": "exited with status 1",
                "next_start_in": 1.5,
            }
        ]
    )
    assert "exited with status 1; retry 2 in 1.5s" in table
