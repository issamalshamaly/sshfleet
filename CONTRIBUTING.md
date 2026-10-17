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
