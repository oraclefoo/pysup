import asyncio

from pysup.config import load_config
from pysup.process import State
from pysup.supervisor import Supervisor

from .conftest import write_config

SLEEP = "sleep 60"

BASE = """
programs:
  keep:
    command: {sleep}
    startsecs: 0.1
  tweak:
    command: {sleep}
    startsecs: 0.1
    stop_timeout: 5
  change:
    command: {sleep}
    startsecs: 0.1
  remove:
    command: {sleep}
    startsecs: 0.1
"""

UPDATED = """
programs:
  keep:
    command: {sleep}
    startsecs: 0.1
  tweak:
    command: {sleep}
    startsecs: 0.1
    stop_timeout: 2
    autorestart: always
  change:
    command: sleep 61
    startsecs: 0.1
  add:
    command: {sleep}
    startsecs: 0.1
  add_manual:
    command: {sleep}
    autostart: false
"""


def test_reload_only_restarts_changed_programs(workdir):
    path = write_config(workdir, BASE.format(sleep=SLEEP))

    async def scenario():
        sup = Supervisor(load_config(path))
        await sup.autostart()
        before = {name: p.pid for name, p in sup.programs.items()}
        assert all(before.values())
        removed_program = sup.programs["remove"]

        write_config(workdir, UPDATED.format(sleep=SLEEP))
        changes = await sup.reload()

        after = {name: p.pid for name, p in sup.programs.items()}
        states = {name: p.state for name, p in sup.programs.items()}
        await sup.stop_all()
        return changes, before, after, states, removed_program

    changes, before, after, states, removed_program = asyncio.run(scenario())
    assert changes == {
        "added": ["add", "add_manual"],
        "removed": ["remove"],
        "changed": ["change"],
        "updated": ["tweak"],
        "unchanged": ["keep"],
    }
    assert after["keep"] == before["keep"]
    assert after["tweak"] == before["tweak"]
    assert after["change"] != before["change"]
    assert "remove" not in after and removed_program.state is State.STOPPED
    assert states["add"] is State.RUNNING
    assert states["add_manual"] is State.STOPPED


def test_priority_order_and_all(workdir):
    path = write_config(
        workdir,
        """
        programs:
          b:
            command: sleep 60
            startsecs: 0.1
            priority: 20
          a:
            command: sleep 60
            startsecs: 0.1
            priority: 10
        """,
    )

    async def scenario():
        sup = Supervisor(load_config(path))
        assert [p.name for p in sup.resolve(["all"])] == ["a", "b"]
        results = await sup.start_programs(["all"])
        starts = {name: p.started_at for name, p in sup.programs.items()}
        stopped = await sup.stop_programs(["all"])
        return results, starts, stopped

    results, starts, stopped = asyncio.run(scenario())
    assert all(r["ok"] and r["state"] == "running" for r in results)
    assert starts["a"] < starts["b"]
    assert all(r["state"] == "stopped" for r in stopped)
