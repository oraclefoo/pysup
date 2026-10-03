# pysup

A small process supervisor for POSIX systems, in the spirit of
[supervisord](http://supervisord.org/). It keeps a set of programs running,
restarts them with exponential backoff when they crash, captures their output
into rotating log files, and is controlled through a simple CLI.

- Python 3.10+, one dependency (PyYAML)
- Programs defined in one YAML file
- start / stop / restart / status / logs / reload / shutdown
- Restart policies: `never`, `always`, `on-unexpected-exit`, with exponential backoff
- Per-program stdout/stderr logs with size-based rotation
- Config reload that only restarts programs whose config actually changed
- Graceful shutdown: stopping the supervisor stops every child (SIGKILL after a timeout)

## Installation

```sh
git clone <this repo> pysup && cd pysup
python3 -m venv .venv && . .venv/bin/activate
pip install .            # or: pip install -e '.[test]' for development
pysup --version
```

## Quick start

```sh
cd examples
pysup daemon --detach          # uses ./pysup.yaml; omit --detach to stay in the foreground
pysup status
pysup logs ticker -f           # Ctrl-C to stop following
pysup restart ticker
pysup shutdown                 # stops all programs, then the supervisor
```

Example `status` output:

```
NAME     STATE    PID   UPTIME    INFO
flaky    backoff  -     -         exited with status 3; retry 2 in 1.6s
oneshot  stopped  -     -
ticker   running  3050  00:00:05
web      running  3045  00:00:06
```

## How it works

`pysup daemon` runs the supervisor: an asyncio event loop that owns all child
processes and listens on a Unix domain socket (permissions `0600`). Every other
`pysup` command is a client that sends one JSON request over that socket and
prints the reply. `pysup logs` reads log files directly, so it also works when
the daemon is not running.

Children are started in their own session/process group with stdin connected
to `/dev/null`. Their stdout and stderr are read through pipes and written to
the configured log files.

## CLI reference

```
pysup [-c CONFIG] COMMAND ...
```

The config file is taken from `-c/--config`, then the `PYSUP_CONFIG`
environment variable, then `./pysup.yaml` / `./pysup.yml`.

| Command | Description |
|---|---|
| `daemon [-d/--detach]` | Run the supervisor. Foreground by default (good for systemd, Docker, or a terminal). `--detach` forks into the background and returns once the control socket is ready. |
| `start NAME... \| all` | Start programs. Waits until each is `running` (it survived `startsecs`) or has failed/entered backoff. |
| `stop NAME... \| all` | Stop programs: send `stop_signal`, then SIGKILL after `stop_timeout`. Stops a program waiting in backoff, too. |
| `restart NAME... \| all` | Stop, then start. |
| `status [NAME...] [--json]` | Show state, PID, uptime and details (last exit, retry countdown). |
| `logs NAME [-n N] [-f] [--stderr]` | Print the last N lines (default 20) of the stdout log (or stderr log); `-f` follows, surviving rotation. |
| `reload` | Re-read the config file and apply changes (see below). Same as sending SIGHUP to the daemon. |
| `shutdown [--no-wait]` | Stop all programs, then the supervisor. Same as sending SIGTERM/SIGINT to the daemon. |

`all` selects every program. Start/stop are idempotent: starting a running
program or stopping a stopped one succeeds without doing anything.

Exit codes: `0` success, `1` an operation failed or the daemon is not running,
`2` invalid configuration or usage.

## Configuration reference

Relative paths in the file are resolved against the directory of the config
file. `~` and `$VARS` in paths are expanded.

```yaml
supervisor:
  socket: ./pysup.sock
  pidfile: ./pysup.pid
  log_dir: ./logs

programs:
  web:
    command: gunicorn app:wsgi --bind 127.0.0.1:8000
    directory: /srv/app
    environment:
      APP_ENV: production
    autostart: true
    autorestart: on-unexpected-exit
    exitcodes: [0]
    startsecs: 2
    backoff:
      initial: 1
      multiplier: 2
      max: 60
      max_retries: 5
      reset_after: 30
    stdout_log: /var/log/app/web.out.log
    stderr_log: /var/log/app/web.err.log
    log_max_bytes: 50MB
    log_backups: 5
    stop_signal: TERM
    stop_timeout: 10
```

`programs` can be a mapping of name to program (above) or a list of programs
each with a `name` key. Program names may contain letters, digits, `.`, `_` and
`-`; `all` is reserved. Unknown keys are rejected to catch typos.

### `supervisor` section

| Key | Default | Description |
|---|---|---|
| `socket` | `pysup.sock` | Path of the control socket. |
| `pidfile` | `pysup.pid` | File the daemon writes its PID to. |
| `log_dir` | `logs` | Directory for default program log files. |
| `log_file` | `<log_dir>/pysup.log` | The supervisor's own log. |
| `log_level` | `info` | `debug`, `info`, `warning`, `error`, `critical`. |
| `log_max_bytes` | `10MB` | Rotation size of the supervisor log. |
| `log_backups` | `5` | Rotated supervisor logs to keep. |

### Program keys

| Key | Default | Description |
|---|---|---|
| `command` | *required* | String (split with shell-like rules, but **not** run by a shell) or list of arguments. Use `sh -c '...'` for pipes, `&&`, globbing etc. |
| `directory` | config file directory | Working directory. |
| `environment` | `{}` | Extra environment variables. `PYSUP_PROGRAM` is always set to the program name. |
| `inherit_env` | `true` | Start from the supervisor's environment (otherwise only `environment`). |
| `autostart` | `true` | Start when the daemon starts (and when the program is added by `reload`). |
| `autorestart` | `on-unexpected-exit` | `never`, `always`, or `on-unexpected-exit`. |
| `exitcodes` | `[0]` | Exit codes considered "expected". Death by a signal (not requested by pysup) is always unexpected. |
| `startsecs` | `1` | Seconds the process must stay up to count as `running`. |
| `backoff.initial` | `1` | Delay before the first restart, in seconds. |
| `backoff.multiplier` | `2` | Each consecutive restart waits `multiplier` times longer... |
| `backoff.max` | `60` | ...up to this many seconds. |
| `backoff.max_retries` | `5` | Consecutive restarts before giving up (state `failed`). `null` = retry forever, `0` = never retry. |
| `backoff.reset_after` | `30` | If a run lasted at least this long, the retry counter starts over. |
| `stdout_log` | `<log_dir>/<name>.stdout.log` | Where stdout goes. |
| `stderr_log` | `<log_dir>/<name>.stderr.log` | Where stderr goes. May be the same file as `stdout_log`. |
| `redirect_stderr` | `false` | Send stderr into the stdout pipe/log (then `stderr_log` must not be set). |
| `log_max_bytes` | `10MB` | Rotate a log once it would exceed this size (`0` disables rotation). Accepts `512KB`, `10MB`, `1GB` or bytes. |
| `log_backups` | `5` | Rotated files to keep (`app.log.1` is the newest). `0` truncates instead. |
| `stop_signal` | `TERM` | Signal used to stop the program (`TERM`, `INT`, `QUIT`, `HUP`, `USR1`, ...). |
| `stop_timeout` | `10` | Seconds to wait after `stop_signal` before sending SIGKILL. |
| `stop_as_group` | `true` | Signal the whole process group, so children of the program (e.g. of `sh -c`) are stopped too. |
| `priority` | `100` | Lower starts first and stops last. Programs with equal priority are started/stopped concurrently. |

## Process states

| State | Meaning |
|---|---|
| `stopped` | Not running: never started, or stopped on request. |
| `starting` | Spawned, but has not yet been up for `startsecs`. |
| `running` | Up for at least `startsecs`. |
| `backoff` | Exited and waiting to be restarted (`status` shows the countdown). |
| `stopping` | `stop_signal` sent, waiting for exit. |
| `exited` | Exited with an expected code and the restart policy says not to restart. |
| `failed` | Exited unexpectedly and won't be restarted (policy `never`, or `max_retries` exhausted), or could not be spawned (e.g. missing executable). |

### Restart and backoff

When a program exits without being asked to:

1. The exit is *unexpected* if the exit code is not in `exitcodes`, or the
   process was killed by a signal.
2. `always` restarts in either case, `on-unexpected-exit` only for unexpected
   exits, `never` never does.
3. Restart *n* (counting consecutive restarts) waits
   `min(initial * multiplier^(n-1), max)` seconds. With the defaults:
   1s, 2s, 4s, 8s, 16s, then `failed`.
4. A run that lasted at least `reset_after` seconds resets the counter, so a
   long-lived service that crashes once a day restarts after `initial` seconds.

A manual `start` always resets the counter. Programs in `failed` stay there
until you `start` them.

## Reloading configuration

`pysup reload` (or `kill -HUP <pid>`) re-reads the config file and compares
each program's new definition with the running one:

| Change | Action |
|---|---|
| Program added | Started if `autostart: true`. |
| Program removed | Stopped and forgotten. |
| `command`, `directory`, `environment`, `inherit_env`, any log setting changed | Stopped and started with the new config (if it was running or has `autostart: true`). |
| Only other settings changed (`autorestart`, `exitcodes`, `startsecs`, `backoff`, `stop_*`, `priority`, `autostart`) | Applied in place, **no restart**. |
| Nothing changed | Left alone. |

If the new file is invalid, the reload is rejected and the running
configuration stays in effect. Changes to `socket`, `pidfile` and `log_file`
require restarting the daemon.

```
$ pysup reload
added: worker
changed: web
updated: ticker
unchanged: flaky, oneshot
```

## Shutdown

SIGTERM, SIGINT (Ctrl-C in the foreground) or `pysup shutdown` make the daemon
stop every program (highest `priority` value first; each gets `stop_signal`
and, after `stop_timeout`, SIGKILL), remove its socket and pidfile, and exit
with status 0.

## Running under systemd

```ini
[Unit]
Description=pysup process supervisor
After=network.target

[Service]
ExecStart=/opt/pysup/.venv/bin/pysup -c /etc/pysup/pysup.yaml daemon
ExecReload=/bin/kill -HUP $MAINPID
KillMode=mixed
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
```

## Development

```sh
pip install -e '.[test]'
pytest
```

The tests cover config validation, log rotation, the process lifecycle
(restart policies, backoff, SIGKILL escalation, spawn errors), reload diffing,
and end-to-end CLI runs against a real daemon.

## Limitations

- POSIX only (uses Unix sockets, process groups and signals).
- No privilege dropping (`user=`), `numprocs`, event listeners, or web UI.
- If the supervisor itself is killed with SIGKILL, its children keep running
  (they live in their own sessions); use your init system's cgroup handling
  (e.g. systemd `KillMode=mixed`) if that matters.
