# Contributing

Thanks for looking. sshfleet is small enough to read in an afternoon; this page is what you need
to change it safely.

## Set up

```bash
uv sync                      # Python 3.12+, installs the package editable plus dev tools
uv run pytest                # the whole suite, no servers needed
uv run ruff check src tests  # lint; `--fix` for imports
cd web && pnpm install && pnpm typecheck && pnpm build   # only if you touch the interface
```

The tests replace `ssh` with a shim (`tests/fakes/bin/ssh`) that replays fixtures from
`tests/fixtures/`, and run the collector script under `bash` against fake `docker`, `podman`,
`sudo` and `curl` (`tests/fakes/remote/`). They expect **GNU** grep, sed and awk on `PATH`.

## Trying it without any servers

`uv run sshfleet demo` is the quickest way: the real interface on a made-up fleet
(`src/sshfleet/demo/`), whose stand-in for `ssh` answers every check, search and restart.

The test suite's own shim drives the real program too, against the fixtures the tests use, in a
home of its own that leaves your configuration alone:

```bash
export SSHFLEET_HOME=$(mktemp -d) SSHFLEET_SSH_DIR=$(mktemp -d)
export SSHFLEET_SSH_BIN=$PWD/tests/fakes/bin/ssh SSHFLEET_FAKE_FIXTURES=$PWD/tests/fixtures
export SSHFLEET_FAKE_SCENARIO=degraded      # or healthy, bastion_down
export SSHFLEET_FAKE_LOG=$SSHFLEET_HOME/ssh.log SSHFLEET_FAKE_STATE=$SSHFLEET_HOME/ssh-state.json
uv run sshfleet project add lab
uv run sshfleet env add -n testing -c tests/sshconfigs/sample-env --project lab --no-test
uv run sshfleet web --port 8799            # or: uv run sshfleet fleet
```

## Ground rules that tests enforce

* **No key is ever opened.** Identity files are `stat`-ed only.
* **Containers are only restarted.** The command layer cannot produce `stop`, `rm`, `kill`,
  `prune` or `down`; `tests/test_containers.py` greps the source tree for them.
* **Secret files are never transferred.** The deny list in `src/sshfleet/files/denylist.py` is
  enforced below the UI.
* **SSH config files are edited one stanza at a time** with a backup under the state directory.
* **Fleet knowledge is data, not code, and discovered where it can be.** Nothing in `src/` may
  name a particular service, port or path. What runs on a host comes from the runtime; what the
  operator wants watched goes under `hosts:` in `config.yaml` (`tests/conftest.py` for the suite).

## Before you commit

Run `scripts/check-secrets.sh`. It greps the tree for things that must never be published:
tokens, phone numbers, private addresses, personal paths. Browser-automation snapshot directories
are ignored by `.gitignore` for the same reason — they can capture live log output.

## Layout

```
src/sshfleet/
  config/      schema, loader, one-time upgrade of older files
  inventory/   ssh config parsing and surgical editing, the host records
  ssh/         the multiplexed ssh runner and error classification
  collector/   the per-host status script, its parser, alerts
  containers/  restart-only actions with verification
  logs/        remote pipelines, streaming search, merge, bundles
  files/       browse and transfer with the deny list
  tunnels/     port forwards riding the shared connections
  web/         FastAPI app, NDJSON/WebSocket endpoints, built frontend
  cli/         Typer commands
web/           React + Mantine + Vite source of the interface
tests/         pytest, offline
```

## Pull requests

One change per PR, with a test that fails without it. Keep the UI copy in plain words — the
interface is meant to be read by an operator at 3 a.m. Rebuild `src/sshfleet/web/static` in the
same PR as any `web/src` change; the bundle is committed on purpose so a plain install needs no
Node.
