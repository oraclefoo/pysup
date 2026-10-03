import asyncio
import os
import time

from pysup.process import Program, State

from .conftest import program_config, python_cmd


def run(coro):
    return asyncio.run(coro)


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_start_stop_and_output_capture(workdir):
    cfg = program_config(
        workdir,
        command=python_cmd(
            """
            import os, sys, time
            print("hello", os.environ["GREETING"], os.getcwd())
            print("oops", file=sys.stderr)
            time.sleep(30)
            """
        ),
        environment={"GREETING": "world"},
    )

    async def scenario():
        prog = Program(cfg)
        ok, message = await prog.start()
        assert ok, message
        assert prog.state is State.RUNNING
        pid = prog.pid
        assert pid and pid_alive(pid)
        await asyncio.sleep(0.2)
        status = prog.status()
        assert status["uptime"] > 0
        ok, _ = await prog.stop()
        assert ok and prog.state is State.STOPPED and prog.pid is None
        return pid

    pid = run(scenario())
    time.sleep(0.05)
    assert not pid_alive(pid)
    assert (workdir / "logs/prog.stdout.log").read_text() == f"hello world {workdir}\n"
    assert (workdir / "logs/prog.stderr.log").read_text() == "oops\n"


def test_expected_exit_is_not_restarted(workdir):
    cfg = program_config(workdir, command="true", startsecs=0, autorestart="on-unexpected-exit")

    async def scenario():
        prog = Program(cfg)
        await prog.start()
        await prog.wait_for_state(lambda s: s is State.EXITED, 5)
        return prog

    prog = run(scenario())
    assert prog.state is State.EXITED and prog.exit_code == 0 and prog.retries == 0


def test_never_policy_marks_failure(workdir):
    cfg = program_config(workdir, command="false", startsecs=0, autorestart="never")

    async def scenario():
        prog = Program(cfg)
        await prog.start()
        await prog.wait_for_state(lambda s: s is State.FAILED, 5)
        return prog

    prog = run(scenario())
    assert prog.state is State.FAILED and prog.exit_code == 1


def test_unexpected_exit_backs_off_then_fails(workdir):
    counter = workdir / "count"
    cfg = program_config(
        workdir,
        command=["sh", "-c", f"echo x >> {counter}; exit 3"],
        backoff={"initial": 0.05, "multiplier": 2, "max": 1, "max_retries": 3},
    )

    async def scenario():
        prog = Program(cfg)
        loop = asyncio.get_running_loop()
        began = loop.time()
        ok, message = await prog.start()
        assert not ok and "exited with status 3" in message
        assert await prog.wait_for_state(lambda s: s is State.FAILED, 5)
        return prog, loop.time() - began

    prog, elapsed = run(scenario())
    # initial run + 3 retries; delays 0.05 + 0.1 + 0.2
    assert counter.read_text().count("x") == 4
    assert elapsed >= 0.35
    assert prog.retries == 4
    assert "giving up after 3" in prog.message


def test_always_policy_restarts_clean_exits(workdir):
    counter = workdir / "count"
    cfg = program_config(
        workdir,
        command=["sh", "-c", f"echo x >> {counter}"],
        startsecs=0,
        autorestart="always",
        backoff={"initial": 0.01, "max": 0.01, "max_retries": None},
    )

    async def scenario():
        prog = Program(cfg)
        await prog.start()
        while not counter.exists() or counter.read_text().count("x") < 3:
            await asyncio.sleep(0.02)
        await prog.stop()
        return prog

    prog = run(scenario())
    assert prog.state is State.STOPPED


def test_stop_during_backoff(workdir):
    cfg = program_config(workdir, command="false", backoff={"initial": 30, "max": 30})

    async def scenario():
        prog = Program(cfg)
        await prog.start()
        assert prog.state is State.BACKOFF
        assert prog.status()["next_start_in"] > 25
        await asyncio.wait_for(prog.stop(), 2)
        return prog

    assert run(scenario()).state is State.STOPPED


def test_stop_escalates_to_sigkill(workdir):
    cfg = program_config(
        workdir,
        command=python_cmd(
            """
            import signal, time
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            print("ready")
            time.sleep(60)
            """
        ),
        stop_timeout=0.5,
    )

    async def scenario():
        prog = Program(cfg)
        await prog.start()
        began = time.monotonic()
        await prog.stop()
        return prog, time.monotonic() - began

    prog, elapsed = run(scenario())
    assert prog.state is State.STOPPED
    assert prog.exit_signal == "SIGKILL"
    assert 0.4 < elapsed < 3


def test_spawn_error(workdir):
    cfg = program_config(workdir, command="/nonexistent/binary", autorestart="never", startsecs=0)

    async def scenario():
        prog = Program(cfg)
        ok, message = await prog.start()
        return prog, ok, message

    prog, ok, message = run(scenario())
    assert not ok and prog.state is State.FAILED
    assert "spawn error" in message
