# Changelog

## Unreleased

### Environments

* An environment that starts empty gets its file at `~/.ssh/config.d/<project>_<short label>`
  instead of one named after the environment, so a project's files sort together. The dialog shows
  the exact path as you type, and refuses a short label whose file already belongs to another
  environment, since the two would otherwise share their hosts.
* A host added on the Hosts tab is named `<project>_<short label>_<name>`: the prefix sits fixed
  in the Alias field and you type only the name, so two projects can both have a `web` without
  colliding. Hosts that predate this keep their names.

### Pausing a host

* A host can be paused — from its drawer on the fleet page, or the Pause monitoring switch on
  the Hosts tab — and resumed the same way. While it is paused sshfleet makes no connection to
  it of any kind: no checks, shells, log searches, file transfers or tunnels, and the refusal
  sits in the ssh layer itself, so the CLI is held to it too. Pausing closes the connection
  already open (`ssh -O exit` on the local control socket, which ends every shell and forward
  riding on it) and drops the host's alerts. A host reached through a paused jump host is out of
  reach as well, since reaching it would connect through the paused one. The flag is
  `hosts.<alias>.paused: true` in `config.yaml`; editing it there by hand works the same way.
  Paused hosts are counted apart on the fleet page — neither up nor down.

### Fixes

* Hosts of an environment registered after another one with a key showed as unreachable
  ("Could not resolve hostname"), although plain `ssh` reached them. OpenSSH reads an `Include`
  inside a `Host` block only for hosts matching that block, and each environment's file was
  included right under the previous environment's key block. Every `Include` in the generated
  config now starts from `Match all`.

### Fleet page

* The refresh button reads what is in view — the chosen environment, or every environment of the
  current project — instead of the whole fleet. The Connections card and the sidebar's connection
  count are counted over the same hosts as the cards beside them.

### Service

* `sshfleet service install` keeps the web interface running in the background: a systemd user
  unit on Linux, a launch agent on macOS, both written as plain files that name the exact Python
  environment that ran the command. `status`, `start`, `stop`, `restart`, `logs` and `uninstall`
  drive it; `--at-boot` turns on lingering on Linux; `--dry-run` shows the definition first. The
  install says when the port is already taken (it registers for the next login instead of failing
  over the running server), when the ssh agent will not outlive the login, and when the install
  lives on a drive mounted after boot. `sshfleet doctor` reports the service once there is one.

### Logs page

* Walking off to another view and back no longer loses anything. The sidebar's Logs link used to
  start a new search every time it was pressed from elsewhere; now only a Logs icon beside a host
  or a container does, and the browser's Back and Forward resume the page too. The search itself
  moved above the routes, so one still running when you leave keeps going and is whole when you
  return, and your place in the results is kept.

### Alerts

* A host running a job scheduler can be kept quiet. Airbyte and its like start a container per
  task and give each one a name of its own, so the container settles in — seen running on two
  sweeps — and its ordinary end was reported as a workload that vanished, again for every job.
  `ignored_containers` entries are now name globs rather than exact names, so `destination-*`
  covers the whole family, and a match now silences every alert about it (gone, stopped,
  restarts, log size) instead of only the "was here and is gone" one. Ignored workloads are still
  collected, listed and searchable, marked as ignored, and are left out of the counts a host is
  measured against — so a finished job can no longer stand in for a service that is missing.
* The Ignore button in the host panel offers "every container named like it", which saves the
  glob the generated name belongs to instead of the single name on screen.

### Terminal

* A large paste no longer stalls. Input was handed to the pseudo-terminal in one write, but a
  pty holds only a few kilobytes: the remote could accept the rest only as we kept reading what
  it wrote back, and the write was stopping us from reading. Past roughly six hundred lines the
  paste simply stopped, with nothing said. Input is now buffered and fed in as the pty takes it.
* A paste of more than one line asks first, the way a desktop terminal does, and shows what it
  is about to send. At a shell prompt every newline in a paste runs that line.

### Log search

Log search fixes. Each of these returned a plausible-looking result rather than an error, so
none of them announced itself.

* A file or journal picked by hand is now looked up instead of rebuilt from its path. Rebuilding
  lost what the source was: a journal has no path and was refused outright, a file declared with
  `in_container` was looked for on the host instead of inside the container, and the format and
  rotated glob both fell back to defaults — so the time filter and yesterday's file went with them.
* The time window is now enforced when the lines are read, not only by the remote pipeline. Host
  files in `auto`, `plain`, `pino_pretty`, `zap_console` or `nginx_error` format had no remote
  time filter at all and returned the whole file whatever window was asked for.
* Absolute windows (`--since 10:00..11:30`) are sent to `journalctl` and to `find -newermt` in
  UTC. They were sent as a wall clock in `logs.display_tz`, which defaults to the machine's own
  zone, so the window silently slid by the UTC offset.
* The remote time filters compare timestamps as text and cannot read the offset one carries, so
  they now run a day wide and only pre-filter; the exact cut is made where the offset is known.
  An nginx access log written in a non-UTC zone was cut at the wrong place.
* "Matches only" keeps whole matching entries. It kept only lines grep had marked *and* that were
  not continuations — but in a pino entry the match is usually the indented body, so the switch
  routinely emptied the view while the header still read "1 match".
* `logs.default_before` reaches the browser; a custom range with no start time says so instead of
  failing the search; source chips stop spinning when a follow or a stopped search ends.

## 0.1.0

First public release.

* Fleet view over SSH with one round-trip per host per refresh, shared `ControlMaster`
  connections and per-bastion ordering.
* Containers: state, ports, restart counts; restart in place with verification; removal of a
  container that is already stopped, with its log saved first. A container that disappears
  from `ps -a` is reported as gone, once it has been seen running twice, for as long as that is
  news rather than history.
* Logs: server-side search across many services in one time window, merged timeline, live follow,
  bundles; per-service line and byte caps.
* Files: browse, preview, download, upload; secret files refused.
* Sites: the domains each host serves, discovered from nginx, Apache, Caddy, Traefik or HAProxy,
  opened from the fleet row.
* Terminal in the browser, minimisable: the shell keeps running while you work elsewhere and
  comes back where you left it. Tunnels riding the shared connections; audit trail.
* Projects: the outermost thing sshfleet knows about. A project is created first and every
  environment belongs to exactly one. Grouping only — nothing about how a host is reached or what
  is watched on it comes from it.
* Settings: environments from an existing SSH config file or started empty; hosts added, edited
  and removed one stanza at a time with backups; per-host health checks; refresh interval and
  thresholds.
* Sounds for critical alerts, warnings and finished jobs, chosen per event, with a mute button
  and optional desktop notifications.
* No service catalogue: a host is the unit, its workloads are discovered from the runtime, and
  what you want watched is written on the host — a role, a star for the default log search, a
  short label, an optional workload filter, a health endpoint and any extra log files.
