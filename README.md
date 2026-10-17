# sshfleet

One tool for a fleet of servers you reach over SSH: see every host at a glance, look inside a
host, restart a container safely, move files, open a shell, and search several services' logs in
the same time window.

It drives the `ssh` you already have. Your SSH config files are the inventory, and the tool never
opens a private key — authentication stays entirely with OpenSSH. What runs on a host is
discovered from its container runtime rather than described in a file, so there is no catalogue
to keep in step with reality.

```
sshfleet web                   # the web interface, opens in your browser
sshfleet desktop               # the same interface in its own window
sshfleet fleet --env staging   # same data as a table, for scripts
sshfleet logs search --env production --main --since 30m --pattern 'error|timeout' -B 60 --download
```

## Install

Needs Python 3.12+ and [uv](https://docs.astral.sh/uv/). The web interface ships prebuilt, so no
Node is required to run it.

```bash
uv tool install sshfleet        # from PyPI, as a command on your PATH
# or, from a checkout:
uv sync && uv run sshfleet --help
```

## First run

Start the interface and open **Settings**:

```bash
sshfleet web
```

Create a **project** first. A project is the outermost thing sshfleet knows about — typically one
per system you run, with its staging and production inside it — and every environment belongs to
exactly one. It carries no behaviour: nothing about how a host is reached or what is watched on it
comes from a project, it only says what belongs with what.

Then add an environment to it. Two ways:

* **Use an existing SSH config file.** Point at the file that already lists the servers, say
  `~/.ssh/config.d/production`. The page parses it straight away — how many hosts, the alias
  prefix, the bastions it found in `ProxyJump`, which hosts have no `IdentityFile` — and suggests a
  name and colour.
* **Start empty.** Give it a name and sshfleet creates a private, empty file for it at
  `~/.ssh/config.d/<project>_<short label>` (say `acme_prod`), in plain OpenSSH syntax. Then add
  servers one by one on the **Hosts** tab (each alias starts with `<project>_<short label>_`, so
  `acme_prod_web` never collides with another project's `web`):
  alias, address, user, the jump host it sits behind. Every server is written into that file, so
  it stays yours and works with plain `ssh` too.

Either way, record the identity file the environment uses. Its path is stored and its permissions
are checked with `stat`; the key itself is never read. **Test** opens the bastion connection and
one host behind it so you know it works before you leave the page.

Plain `ssh <alias>` in a terminal only reads files under `~/.ssh/config.d/` through an `Include`
line in your own `~/.ssh/config`. sshfleet does not need that line (it drives ssh with its own
generated config), but the Environments tab notices when it is missing and offers a button that
adds it — with a backup of the file first, and never on its own.

The same can be done from the command line:

```bash
sshfleet project add acme
sshfleet env add --name production --ssh-config ~/.ssh/config.d/production \
    --key ~/.ssh/production.pem --project acme
sshfleet doctor
```

## What sshfleet knows about a host

Out of the box it knows nothing about your fleet, and it does not need to. Every host is shown
with its load, memory, disks and whatever its container runtime reports — `docker ps -a` or
`podman ps -a`, discovered per host. There is no catalogue to keep in step with reality.

What you may add, per host, is what you want watched:

```yaml
# ~/.config/sshfleet/config.yaml
hosts:
  prod-api:
    role: service          # service | gateway | frontend | data | ops
    main: true             # its running workloads are what the Logs page searches by default
    label: api             # short tag in merged log output
    workloads: "api-*"     # only look at these; empty means everything the host reports
    health: {port: 3000, path: /health, min_interval_s: 0}
    log_format: pino_pretty          # auto-detected when absent
    log_sources:                     # extra files, on the host or inside a workload
      - {path: /var/log/api/worker.log, format: plain, rotated_glob: "worker.log.*"}
    expected_count: 3      # alert when fewer are present; without it there is no count rule
    ignored_containers: [old-worker, "destination-*"]  # never alert on these; globs allowed
    runtime: podman        # probed on the host when absent
    sudo: false
    web_root: /var/www/html          # frontends only
```

All of it is editable on the Settings page and in the host panel, so the file is where it lands,
not where you work.

Five rules are worth knowing, because everything else follows from them:

* **`workloads` is a visibility filter and nothing else.** It scopes what is listed, counted,
  probed and searched, all alike. Leave it out and the host is watched whole.
* **`expected_count` is the only count rule.** Set it and sshfleet alerts when fewer workloads are
  present; leave it out and it never invents a number. Counting is about existence, not health:
  `ps -a` lists a stopped workload, so it still counts, and being stopped is its own alert.
* **A workload that settles in and then leaves `ps -a` is reported as gone.** Settling in means
  being seen running on two sweeps in a row: anything that comes and goes inside one refresh
  interval was a job, and a job that ends is not a loss. One that was already stopped when it went
  is housekeeping, not news. The report lasts `thresholds.vanished_memory_s` (six hours by
  default) — long enough to see it, short enough that a finished job does not become a permanent
  alarm. `expected_count` is the durable contract; this is the heuristic that notices a change.
* **`ignored_containers` silences, it does not hide.** Each entry is a name glob; a workload that
  matches one raises no alert of any kind and is left out of the count, while staying listed and
  searchable with an "ignored" mark beside it. This is what a job runner needs: Airbyte and its
  like start a container per task and give each one a fresh name
  (`destination-postgres-write-43138-0-nxulk`), so the run outlives a refresh, settles in and is
  reported as gone when it finishes — every time. Ignore `destination-*`, not the one name you
  can see. In the host panel, the Ignore button on a workload that left offers exactly that.
* **The star means stdout.** It puts a host's running workload logs into the default search;
  its files, nginx logs and journal stay a click away under "Pick hosts & containers".

The interface reads the same way round. The sidebar picks the project first, then all of its
environments or one of them, and every page — fleet, logs, files, tunnels — is scoped to that
choice, Settings included — which is why choosing one never navigates away from where you are. In
Settings, a project opens a panel holding its environments: that is where one is added,
tested, edited or removed, because a project is the only place they are looked at as a set.

## The web interface

`sshfleet web` starts a local server on `127.0.0.1:8765` and opens the interface in your browser.
`sshfleet desktop` shows the same pages in a window of their own: pywebview when it has a GUI
backend (on Linux that means GTK or Qt Python bindings, e.g. `pip install pywebview[qt]`),
otherwise a Chromium-family browser in app mode, otherwise any browser. If `sshfleet web` is
already running, the window opens onto it. Nothing listens on the network: the server binds to localhost
only, and every request must carry a random token minted at start-up. The token is embedded in the
page the server hands to your browser, so a page from any other origin cannot talk to it.

Pages:

* **Fleet** — every host with load, memory, disk, containers and health, updating row by row as
  the refresh sweeps the fleet. Filter by environment, role or text; sort alerts first; ⌘K jumps
  to a host. Click a row for the host panel: metrics, facts, containers with **Logs** and
  **Restart** actions, and the host's alerts.
* **Logs** — pick an environment and either the starred hosts (click one to leave it out) or any
  hosts and containers, type a pattern, choose lines of context and a time window, and search all
  of them at once. Results stream in as one merged timeline with per-service counters; **Follow**
  tails live; **Download bundle** saves per-service files, a merged log and a manifest. **Max
  lines** and **Max size** cap what each service may return, newest first, so an hour of a busy
  service stays fast; a service that hit the cap is marked so you know the window was not fully
  covered.
* **Files** — browse a host, preview or download a file, drag a file in to upload it. Secret files
  are shown but marked protected and cannot be fetched.
* **Sites** — the domains a host serves, read out of nginx, Apache, Caddy, Traefik or HAProxy's
  own configuration rather than declared anywhere. A globe beside the host's row lists them and
  opens one in a new tab. Only `server_name` and its port are read; a catch-all, a bare IP, a
  localhost or a name with a variable in it is not an address and is left out, and the port-80
  block that only redirects to https is not a second site. It costs about 15ms on the host, so it
  runs when a host is first seen and whenever you ask for a check by hand — never on the timer.
* **Terminal** — a real shell in the browser over the shared connection, from the host panel or at
  `/terminal/<alias>`. Minimise it and the shell keeps running while you go elsewhere: the host's
  terminal icon turns green to say one is open there, and a counter in the header lists every open
  shell from any page. Either brings it back where you left it. Closing is a separate button, and
  the only thing that ends the shell.
* **Tunnels** — define a port forward (Kibana, Elasticsearch, Seq, Grafana and Postgres are one
  click) and switch it on and off. It rides the connection that is already open, so opening one
  costs nothing.
* **Audit** — everything the tool changed, newest first, in plain words.
* **Settings** — environments, servers, health checks, how often everything is checked, and
  which sounds to play.

A restart on an environment whose name contains "prod" asks you to type the container name. A
container that is **already stopped** can be removed: you type its name on every environment,
and its last log lines are saved to the downloads folder first, because the log goes with it.
The state is checked on the host right before the command, so a container that has started in
the meantime is left alone. The interface never offers stop, kill or prune — there are no such
routes on the server either.

### On a phone or a tablet

Every page works on a phone and a tablet as well as on a desktop. A phone gets a tab bar along
the bottom (Fleet, Logs, Files, Alerts, More) and a button in the header for the project and
environment in view; hosts, containers, tunnels and audit entries are cards; Logs shows the query
or the results and switches between them; forms and panels take the whole screen. A tablet held
upright opens the sidebar from a menu button, and from 992px wide the layout is the desktop's.
On a touch screen a shell has a key bar for what a phone keyboard lacks (Esc, Tab, Ctrl, the
arrows, PgUp/PgDn, paste), anything a tooltip says opens with a tap, and the exact command
behind a quick action is shown above its Run button.

The server still listens on `127.0.0.1` only. To reach it from a phone or tablet, forward the
port over SSH from the device, for example with Termux on Android or Blink on iOS, to the
computer running sshfleet:

```bash
ssh -L 8765:127.0.0.1:8765 you@your-computer
```

then open `http://127.0.0.1:8765` in the device's browser. The page carries its token as it
always does, and nothing on the computer has to listen beyond localhost.

## Starting and stopping

`sshfleet web` runs in the foreground and prints the address it is serving. **Ctrl+C** in that
terminal stops it; shutdown takes about two seconds. Nothing on the servers is affected — the
fleet view simply stops updating. If you started it detached and have no terminal to interrupt,
stop it by name or by port:

```bash
pkill -f "sshfleet web"      # or: fuser -k 8765/tcp
```

Two things deliberately outlive the app. The SSH master connections stay open for ten minutes
(`ssh.control_persist`) so the next start is instant, and a tunnel you switched on keeps
forwarding for as long as its connection lives. To end both at once:

```bash
sshfleet mux stop            # closes the connections and every forward riding on them
```

Restarting is cheap: the last snapshot of every host is cached on disk, so the table is filled in
before the first sweep finishes, and with warm connections that sweep takes about a second.

### Running it as a background service

It is not a service by default — you start it when you need it. If you would rather have it always
running and just open the browser, let the operating system keep it up:

```bash
sshfleet service install          # starts now and at every login; --port to choose the port
sshfleet service status           # installed? running? where, since when
sshfleet service logs -f          # its output: the journal on Linux, a log file on macOS
sshfleet service stop             # also: start, restart
sshfleet service uninstall        # stop it and remove the definition
```

On Linux this writes a systemd **user** unit, `~/.config/systemd/user/sshfleet.service`; on macOS
a launch agent, `~/Library/LaunchAgents/io.github.issamalshamaly.sshfleet.plist`. Both are plain
files you can read and edit, both run as you — nothing is installed system-wide — and it binds to
`127.0.0.1` as always. The definition names the exact Python environment that ran `install`, so
install from the copy you mean to keep (`uv tool install sshfleet`, not `uvx`) and run `install`
again if you move it. `--dry-run` prints the definition without writing it; `sshfleet doctor`
reports the service once there is one.

Three things to know:

* **The port.** If `sshfleet web` is still running in a terminal when you install, the service is
  registered but not started — two servers cannot share the port. Stop the foreground one and run
  `sshfleet service start`.
* **Keys.** A service has no terminal to ask for a passphrase. On Linux it inherits the ssh agent
  socket only when that agent outlives the login — the desktop keyrings under `$XDG_RUNTIME_DIR`
  do, a plain `ssh-agent` in `/tmp` does not — and `install` says so when it cannot. Launch agents
  on macOS get the session's agent.
* **Logged out.** A user service runs while you are logged in. `sshfleet service install --at-boot`
  (Linux) turns on lingering so it keeps going; it then polls every host on the configured interval
  around the clock, so set that interval on the Checks tab with that in mind.

## What a refresh costs the servers

One `ssh` round-trip per host, running a single shell script that reads `/proc`, `df`, the
container runtime's `ps` and `inspect`, the listening ports and one `curl` against the health
endpoint. Measured on a two-container host it is about 0.12 CPU-seconds per refresh; on a
17-container host about 0.4, roughly half of it inside `dockerd`. Two side effects are worth
knowing: every refresh runs a few `sudo` commands, which each add a line to the host's auth log,
and a health endpoint that logs every call will see one call per refresh — which is what
`probe_min_interval_s` is for.

## Settings

**Environments.** Register a file or start empty, as above. Removing an environment only makes
sshfleet forget it; the SSH config file is not touched.

**Hosts.** The Hosts tab lists every stanza in your SSH config files and lets you add, edit,
rename or remove one, including which host it connects through — pick a jump host or choose a
direct connection. What you are about to write is shown as the exact stanza before you save.

Edits are surgical: only the lines of that one stanza are rewritten, so comments, ordering and
everything else in the file survive byte for byte, and the files stay plain OpenSSH that
`ssh <alias>` uses without the tool. Before each write a copy of the whole file is saved under
`~/.local/state/sshfleet/ssh-backups/` — never beside the original, because
`Include ~/.ssh/config.d/*` would read a stray `.bak` as real configuration. Removing a host
asks you to type its alias. Display name, service, tags and the health check are kept in
sshfleet's own config instead, so the SSH file stays clean.

The same form sets the health check for that one host: which port and path to call, and how often
it may be called at all. Between checks the last answer is kept and shown with its age, and an
alert raised from an old reading says how old it is.

**Editing the files by hand still works.** The registered files (and anything they `Include`)
are watched, so a host you add in an editor appears a couple of seconds after you save, without
restarting anything: the inventory is rebuilt, the new host is checked immediately, and the page
says what moved. There is a **Reload now** button on the Files tab for when you want it at once.

**Checks.** How often the fleet is polled, from 5 seconds to an hour, or off for manual refreshes
only. The default is every two minutes. It applies the moment you save — the loop is woken rather
than left sleeping on the old interval. The same tab holds the connection and status-script
timeouts, and the disk, memory and load levels that turn a host yellow or red.

**Sounds.** A short sound for a critical alert, a warning, or a finished job, each chosen from a
few built-in patterns (synthesised in the browser; no audio files), with a mute button in the
header and an optional desktop notification for critical alerts while the tab is in the
background. Browsers only allow sound after the page has been clicked once. The choice is saved
in the config, so it follows you between browsers.

## History, export and import

**Every state is kept.** Whenever `config.yaml` or one of your SSH config files changes, the
change is recorded under `~/.local/state/sshfleet/history/`. That includes edits made on the
page, from a command, or in an editor. Settings › **History** lists each state, says who made
it and what it changed, and shows the diff. Any earlier state can be put back. The state you
leave is recorded first, so a restore can itself be undone. A restore checks first that the
result loads, writes only the files that differ, and never deletes anything. A broken hand edit
is kept too, marked as not loading, so you can see what it was. Unchanged content is stored
only once, files are private (0600), and a file that looks like a key is never copied.

```
sshfleet config history                 # newest first, each with a six-character code
sshfleet config show <code> --diff      # what that state changed
sshfleet config restore <code>          # put it back (asks for the code; --dry-run to look)
```

**A whole setup in one file.** Settings › **Export & import**, or `sshfleet config export`,
writes a bundle of the fleet. It carries the projects, environments, hosts and what is watched
on them, tunnels, thresholds, and each environment's SSH file. Nothing that belongs to this
machine goes with it: key paths, `IdentityFile` lines, file paths, interface preferences and
pauses all stay behind. Settings that look like credentials go out as `<redacted>`. If the
finished file would still contain a private key, a cloud or chat token, or a value from
`secrets.env`, nothing is exported and you are told where it is. `--with-local` adds this
machine's paths, for moving your own setup to another computer.

**Import shows you everything first.** By default it merges: it adds and updates and never
removes. Where both sides differ it keeps yours unless you choose otherwise, and your keys,
paths and pauses are left alone. SSH hosts are merged one stanza at a time, so your comments and
local-only hosts survive. A bundle whose SSH text would run something on your machine
(`ProxyCommand`, `Include`, `Match … exec`) is refused unless you allow it. An import is undone
from History like any other change.

**Sharing through git.** A folder form keeps the bundle readable in a diff:

```
sshfleet config export --out ops/sshfleet/       # manifest.json, fleet.yaml, ssh/<file>
git add ops/sshfleet && git commit -m "fleet: add the payments hosts"
# a teammate:
sshfleet config import ops/sshfleet/ --dry-run   # look first
sshfleet config import ops/sshfleet/             # then set the key path once, in Settings
sshfleet config diff ops/sshfleet/               # exit 0 nothing to import, 1 changes, 2 refused
```

Importing the same bundle again changes nothing, so `config diff` works as a pre-commit check.

**The audit trail** can be filtered and exported from the Audit page, or with
`sshfleet audit export --since 7d --action 'container.*' [--jsonl]`. In a CSV, a cell that starts
like a spreadsheet formula is written with a leading quote, so it shows as text.

## What it does

**Fleet view.** Every host with load, memory, disk, containers running versus expected, a health
probe and any alerts. A refresh of 48 hosts takes about a second once connections are warm.

```
env    host                 reach     load   mem%   disk%   ctrs     health   up        age   alerts
prod   prod-api             ●         0.21   61     71      1/1      200      7d3h      6s
prod   prod-metrics         ●         0.00   87     42      1        —        116d1h    6s    MEM, RESTARTS
prod   prod-portal          ○ TIMEO   —      —      —       —        —        —         6s    UNREACH
```

Containers are discovered with `ps -a`, so stopped ones are listed too, drawn faded with their
exit code. A crashed container is an alert; a clean exit of something that was running a moment
ago is a warning; an init or migration job that finished before the tool first saw the host is
merely noted.

**Containers.** Per host: image, state, ports, restart count. Restart happens in place and is
not reported as done until the container has been seen running and answering its health endpoint
twice in a row. A stopped container can be started again or removed, with its log saved first.

**Logs.** Search one container, one host, or every main service at once. Filtering happens on the
server, so only matches and their context cross the network. Results from several services are
merged into one timeline by timestamp, each line tagged with its service.

```bash
sshfleet logs search --env production --main --since 1h --pattern 'error|timeout' -B 60 --download
```

`--download` writes a folder with one file per service, a merged `merged.log`, and a
`manifest.json` recording the window, the pattern, the exact commands and per-service counts.

**Files.** Browse, download and upload. Anything matching `*.env*`, `*.pem`, `*.key`, `id_rsa*`
and friends is refused — those are the deploy secrets that live next to the application.

**Tunnels.** Port forwards for Kibana, Seq, Elasticsearch, Grafana or a database, opened on the
connection that is already there. Define them on the Tunnels page or under `tunnels:` in the
config.

**Shell.** A real shell on any host from the host panel; because the connection is already open
the prompt appears immediately.

## Commands

| Command | What it does |
|---|---|
| `sshfleet web [--port] [--no-open]` | the web interface |
| `sshfleet desktop [--port]` | the web interface in its own window |
| `service install · uninstall · status · start · stop · restart · logs` | the web interface as a background service |
| `sshfleet` (no subcommand), `sshfleet --version` | help, and the version |
| `project add · list · rename · remove` | group environments under a project |
| `env add · list · test · remove · preview` | register and check environments |
| `fleet [--env] [--main] [--alerts] [--json]` | fleet status as a table |
| `host <alias>` | one host in detail |
| `containers <alias>` | containers on a host |
| `restart <alias> <container>` | restart in place, then verify |
| `logs search · tail · sources` | search and follow logs |
| `files ls · get · put · cat` | browse and transfer |
| `tunnel up · down · status` | port forwards |
| `ssh <alias> [--tab] [--print]` | open a shell |
| `mux warm · status · reset · stop` | manage the shared connections |
| `config history · show · restore` | every earlier state of the configuration, and putting one back |
| `config export · import · diff` | the setup as a bundle: move it, share it, check it |
| `audit [export]` | the audit trail, and its export as CSV or JSON Lines |
| `hosts · doctor · version` | inventory, self-check, version |

Every command takes `--json` for scripting. Exit codes: `0` results, `1` nothing found,
`2` usage, `3` partial failure, `4` total failure, `6` results truncated (with `--strict`).

## How it stays fast

* One `ssh_config` of its own that `Include`s yours and turns on `ControlMaster`. OpenSSH passes
  it down to the `ProxyJump` hop, so the bastion connection is shared too and later commands skip
  the handshake entirely.
* One round-trip per host per refresh: a single script that prints one JSON document. It needs no
  `jq` and no interpreter on the server.
* Bastions are opened first, then the main services, then everything else. A host whose bastion is
  down is marked without being contacted, so one dead bastion costs one timeout instead of one per
  host behind it.
* Connections open on a short deadline separate from the command deadline, so an unresponsive host
  cannot set the pace for the whole refresh.
* Log filtering runs on the server; `--tail` always accompanies `--since` because the Docker log
  reader scans from the start of the file without it.

## Safety

* **Private keys are never read.** The tool records the path, checks the permissions with `stat`,
  and lets `ssh` do the rest.
* **Running containers are only ever restarted.** `stop`, `kill`, `prune` and `down` cannot be
  built by the command layer at all, and a test greps the whole source to prove none appear.
  `rm` exists for one case: a container that is already stopped, checked on the host immediately
  before the command, with its name typed back and its last log lines saved first.
* **Secrets are not downloadable.** The deny list is enforced in the transfer and preview code,
  not in the interface, and it cannot be switched off.
* **Your SSH config files are edited one stanza at a time**, never rewritten, and a copy of the
  whole file is kept before every change. Values are checked before they are written, so nothing
  can smuggle a second keyword (a `ProxyCommand`, say) onto a line.
* **Every change is recorded.** Restarts, uploads, downloads, tunnel changes, opened shells and
  every settings edit go to `~/.local/state/sshfleet/audit.jsonl`; read it with `sshfleet audit`.
* **Every earlier configuration can be put back.** History keeps each state of `config.yaml` and
  the SSH files. Restores and imports record the state before them, so either can be undone.
* **A bundle never carries a key or a secret, and nothing in one runs on import.** Both are
  enforced in code and held by tests: an export seeded with every kind of secret contains none
  of them, and a bundle with `ProxyCommand`, `Include` or `Match exec` is refused unless allowed.
* **The page talks to the server with a session token in a header.** It answers only to
  `127.0.0.1` and `localhost`, which stops a rebound DNS name from fetching the page. The token
  is accepted in an address only on the two download links and the terminal socket.

## Configuration

`~/.config/sshfleet/config.yaml`, written by the Settings page (or `env add`). Besides the
per-host records above, the useful keys:

```yaml
projects:
  - {name: acme, color: violet, description: Customer platform}
environments:
  - {name: production, ssh_config_path: ~/.ssh/config.d/production, project: acme, short: prod}
refresh:     {fast_s: 120, connect_timeout_s: 10, collector_timeout_s: 25}
concurrency: {global: 16, per_bastion: 8}
thresholds:  {disk_warn_pct: 80, disk_crit_pct: 90, mem_warn_pct: 85, vanished_memory_s: 21600}
logs:        {default_before: 60, tail_lines: 50000, display_tz: local, allowed_paths: []}
tunnels:
  kibana-prod: {env: production, via: prod-kibana, remote: 127.0.0.1:5601, local: 15601,
                open: "http://127.0.0.1:15601"}
files:       {download_dir: auto, deny_extra: []}
hosts:
  prod-worker: {display: "worker (rootless)", health: {min_interval_s: 900}, workloads: "worker-*"}
  slow-box:    {connect_timeout_s: 45}   # a host whose login is slow, not a host that is down
```

A project is a name, a colour and a note; an environment names the one it belongs to. A file
written before projects existed, or edited by hand so that an environment names none, is repaired
when it loads rather than refused: its environments join the first project there is, or a new one
called `default` that you rename afterwards — renaming carries every environment with it. A
project has to be empty before it can be removed, so an environment is never left belonging to
nothing.

`hosts.<alias>.connect_timeout_s` overrides the fleet-wide `refresh.connect_timeout_s` for one
host. A host can answer perfectly and still take half a minute to let you in — a wedged
`systemd-logind` makes `pam_systemd` wait out a 25-second D-Bus timeout on every new login — and
that is not the same thing as being down. When it times out the message says how long it waited,
so the two are told apart.

`display_tz` is an IANA name or `local`. `logs.allowed_paths` extends the directories a log
source may read from (`/var/log`, `/home`, `/opt`, `/srv`, `/usr/src`, `/app`, `/data` by
default). Everything lives under `~/.config/sshfleet`, `~/.cache/sshfleet` and
`~/.local/state/sshfleet`; set `SSHFLEET_HOME` to keep it all somewhere else.

## Extending

* **A different stack** needs nothing: its workloads are discovered. Name them with `label`,
  narrow them with `workloads`, and point `health` at whatever answers.
* **A file only one host has** goes under `hosts.<alias>.log_sources`.
* **A new log format** is a parser in `src/sshfleet/logs/parsers.py` plus a `LogFormat` value; a
  host names it under `log_format`.
* **Other search backends** (Seq, Elasticsearch) have their `SourceKind` reserved and are the
  obvious next modules — see the issue tracker.

## Development

```bash
uv sync
uv run pytest          # ~390 tests, no servers required
uv run ruff check src tests
```

The web interface is a React + Mantine app in `web/`, built with Vite into
`src/sshfleet/web/static/`, which the Python server serves. The built files ship with the package,
so a normal install needs no Node. To change the interface:

```bash
cd web
pnpm install
pnpm dev               # Vite on :5173, proxying /api and /ws to a running `sshfleet web`
pnpm build             # rebuild src/sshfleet/web/static
```

The API is FastAPI under `src/sshfleet/web/api/`; streaming endpoints (fleet events, log search,
follow, restart progress) send newline-delimited JSON, and the terminal is a WebSocket bridging a
pty. `tests/test_web_api.py` drives the whole app in-process against the same ssh shim.

The suite replaces `ssh` with a shim that replays recorded fixtures, so connection failures,
missing containers, podman hosts and dead bastions are all exercised offline. The collector script
is tested by running it under `bash` against fake `docker`, `podman`, `sudo` and `curl`. The host
records the suite runs against live in `tests/conftest.py`. See `CONTRIBUTING.md`.

## Licence

MIT — see `LICENSE`.
