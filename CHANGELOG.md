# Changelog

sshfleet follows [Semantic Versioning](https://semver.org). Before 1.0 a minor release may change
the configuration file's format; when it does, the file is upgraded on first start and the old
one is kept beside it.

## 0.1.0 — unreleased

The first public release. Nothing was published before it, so everything below is new in it;
the sections after the summary say how each part works.

### In short

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

### Command line

* `sshfleet` on its own prints help and says where to start (`web`, `fleet`, `doctor`); it used
  to open the older terminal interface, which is gone — the web interface does everything it
  did and much more, and `textual` is no longer a dependency. `sshfleet --version` prints the
  version.
* `sshfleet hosts` crashed on any inventory; it lists the hosts again, with their label and a
  star for the ones in the default log search.
* `sshfleet web` on a taken port says whether sshfleet is already running there (and opens it),
  whether it is an older one to stop first, or which option picks another port — instead of
  uvicorn's `[Errno 98]` under a line announcing the address.
* `env add --test` exits with 4 when a bastion could not be reached, and `doctor` says to set
  `ssh.control_dir` to a short directory when the socket path is too long, rather than
  suggesting a `chmod` that cannot help.
* A host that cannot be reached shows why in words in `sshfleet fleet` ("○ bastion down") instead
  of five clipped capitals ("○ BASTI"); `env list` shows paths relative to your home.
* `examples/` holds an environment's SSH file and the config sshfleet keeps for it (both checked
  by the tests), CONTRIBUTING explains how to run everything without servers, and the package
  ships `py.typed`.

### Phones and tablets

* The whole web interface works on phones and tablets. Below 768px the sidebar, which used to
  cover every page, gives way to a tab bar (Fleet, Logs, Files, Alerts, More) and a header button
  for the project and environment; a tablet held upright opens the sidebar from a burger; from
  992px the layout is the desktop's, unchanged.
* Fleet hosts, a host's containers, tunnels, audit entries, the hosts in Settings and the
  configuration history are cards on a phone, with what the table's tooltips held written out
  and their buttons labelled. Fleet is two columns of cards on an upright tablet.
* Logs shows the query or the results below 992px, both kept as they were while you switch, and
  a phone puts each line's source and time above its text. Files keeps the name readable at any
  width; on a phone a file opens a sheet with its details, Preview and Download.
* A shell on a touch screen has a key bar (Esc, Tab, sticky Ctrl, arrows, Home/End, PgUp/PgDn,
  paste) and takes the part of the screen the keyboard leaves. Leaving the page while a shell
  runs asks first on a touch screen.
* Quick actions on a touch screen show each exact command above its Run button. Tooltips open
  with a tap. Forms and panels fill a phone with their buttons pinned at the bottom; targets are
  at least 40px on a touch screen; fields no longer auto-capitalise or auto-correct.
* Turning the device or resizing the window keeps every page as it was: filters, the Logs pane
  and line, the Files position, a running shell.
* The README explains how to reach sshfleet from a phone over `ssh -L`; the server still binds
  to localhost only.

### History and restore

* Every state of `config.yaml` and the SSH config files is kept, whoever changed them: this page,
  a command, or an editor (noticed by the server's watcher, or by the next command you run).
  Settings › History lists each state with who made it and what changed — config sections, hosts
  added or removed, SSH files — and shows the diff against the state before or against the files
  now. Any state can be put back, from the page (confirmed by typing its six-character code, with
  an Undo in the notification) or with `sshfleet config restore <code>`. A restore checks that the
  result loads before writing, writes only the files that differ, deletes nothing, keeps a backup
  of each file it writes, and records the state it replaces, so it can be undone. A hand edit that
  breaks the config is kept too, marked as not loading. Storage is content-addressed under
  `~/.local/state/sshfleet/history/` (0700/0600): the newest 200 states and everything from the
  last 14 days. A file that looks like a key is never copied. `sshfleet config history` and
  `config show <code> --diff` read it from the command line.

### Export and import

* `sshfleet config export` and Settings › Export & import write the setup as a bundle: one JSON
  file, or a folder for git (`manifest.json`, `fleet.yaml`, `ssh/<file>`). It carries the fleet
  and each environment's SSH text. It never carries key paths or `IdentityFile` lines, file paths,
  interface preferences or pauses, unless `--with-local` is given for moving your own setup.
  Credential-looking settings and passwords in URLs go out as `<redacted>`. If the finished file
  would contain a private key, an AWS, GitHub, GitLab or Slack token, or a value from
  `secrets.env`, the export is refused. SSH lines that would run on the importer (`ProxyCommand`,
  `Include`, `Match … exec`, `IdentityAgent` …) are refused unless allowed. On the page, the
  environments offered are exactly those of the chosen projects, and a line says what will go.
* `sshfleet config import` and the same tab bring a bundle in. Everything it would change is shown
  first. By default it merges: it adds and updates and never removes. Where both sides differ it
  keeps the local version unless told to take the bundle's — except where the local value is only
  the default. Keys, paths and pauses stay as they are here, and `<redacted>` never replaces a real
  value. SSH hosts are merged a stanza at a time, keeping comments, local-only hosts and
  `IdentityFile` lines. A new environment's hosts go to `~/.ssh/config.d/<project>_<short>`.
  Clashing labels, ports already in use and partly overlapping stanzas are reported as conflicts
  and left out. Replace mode unregisters what the bundle does not carry, deleting no file. The
  page applies exactly the plan you looked at, or says what changed meanwhile. An import is undone
  from History. `sshfleet config diff` exits 0 when there is nothing to import, 1 when there is,
  and 2 when the bundle would be refused, for a pre-commit check.

### Audit trail

* The Audit page filters on the server, over the whole trail: by time, by action, and by the
  environments in view. It also exports what it shows as CSV or JSON Lines, as does
  `sshfleet audit export [--since 7d] [--action 'container.*'] [--env] [--host] [--status] [--jsonl]`.
  In a CSV, a cell that starts like a spreadsheet formula gets a leading quote. The bare
  `sshfleet audit` still lists the trail.
* Each line carries the hash of the line before it; `sshfleet audit verify` names any line that
  was changed, removed or slipped in. The trail is set aside at 10 MB, five files kept, and the
  chain runs on across them. Two writers at once (the CLI and the web server) take turns.
* Reads are recorded too: folders listed, files previewed or refused, log searches and follows
  whether or not they are saved, and the end of a quick action as well as its start. A container
  removed from the page is ignored from then on, and that now shows in the trail. The actor is the
  account sshfleet runs as, not the `USER` variable, and each line names the process.

### Security

* The page, and the session token in it, is only served to a browser you have let in. Before,
  any user or program on the computer could ask for `http://127.0.0.1:8765/`, read the token out
  of the page and have the API, terminal included. `sshfleet web` now opens the page through a
  link that works once, within ten minutes, and the browser keeps a cookie from it; a bookmark
  keeps working across restarts, and `sshfleet web` run again lets in another browser. Everyone
  else gets a note saying how to get in. A terminal socket opened by any other page is refused,
  and every response says the page may not be framed, may only load from itself and sends no
  referrer.
* The web server answers only to `127.0.0.1` and `localhost`, so a page that points its own name
  at this machine (DNS rebinding) cannot fetch the page that carries the session token. The token
  is compared in constant time and read from the request header. An address is accepted only on
  the two download links and the terminal socket.
* An `Include` glob in an SSH config file never opens a key, a public key or a backup. Before
  this, `Include ~/.ssh/*` read the private keys as if they were configuration.
* A container name in a log search or a follow could run a command on the host: it was put
  unquoted into the message for a container that is not there, so `x$(cmd)` ran `cmd`. Names are
  now quoted there too, a name that is not a container name is refused before anything connects,
  the read-only check no longer overlooks a value left unquoted, and follow is checked as well.
* After a restart, the check that the container is back put its name into a filter unquoted. It
  is quoted now, and every container command (restart, remove, inspect, logs) refuses a name that
  docker or podman could not have given, with a 400 from the page and a message on the command
  line.
* A host's health port from a hand-edited config or an imported bundle reached a script on the
  host unchecked, where a value like `1;cmd` ran `cmd` on a host without curl. It must now be a
  port number when the config is read, and the script and the restart check accept only digits.
* When rsync is missing on a host, a download or upload falls back to scp, whose older protocol
  hands the remote path to the host's shell. A path with characters a shell acts on is no longer
  given to it (the result says why), and the copy staged in /tmp for a root-owned upload gets a
  name made of safe characters.
* Files and logs no longer trust a name: a path is resolved on the host first and the deny list
  checks what it leads to, so a link to an `.env` is refused; a directory is refused rather than
  copied whole with every secret inside; a log source's rotated glob never picks up a name on the
  list. The list now also covers `/proc` (where `environ` is), docker, kube and gnupg folders,
  `.git-credentials`, shell histories, `gshadow` and backups such as `server.key.bak`, and the
  Inspect action hides a container's environment variables.
* Downloads, their folders and log bundles are private to you (0600/0700), like the config and
  the audit log; a download folder named after an environment can never climb out of the
  downloads directory.
* A root-owned upload waits on the host in a private file `mktemp` made, not at a predictable
  `/tmp` name another user there could plant a link at before `sudo install` reads it.
* A tunnel's "open in browser" address must be http or https; `javascript:` and `file:` links are
  refused from the settings page and from the config file.
* A file that holds a private key is never read as SSH configuration, whatever it is called.
* `secrets.env` is kept inside sshfleet, not put in its environment, which every ssh and every
  terminal session opened from the page inherited — `LD_PRELOAD=…` in it reached them all.
* SECURITY.md lists every command sshfleet runs with `sudo -n` on a host, says what the generated
  SSH config sets for every connection and what it leaves to your files (agent forwarding, local
  commands, `~/.ssh/config`), and how to stop trusting host keys on first use. `sshfleet doctor`
  says which host-key policy is in effect.
* The generated SSH config is written line by line, so a key path, SSH file path or environment
  name with a line break in it could add a line of its own, such as a `ProxyCommand` run on this
  machine. Such a value is refused when the config is written, and by the settings page when it
  is entered.

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
* A whole environment or a whole project can be paused the same way, from its card in Settings ›
  Projects (after a confirmation, since it closes every connection to its hosts at once), and
  resumed there or from the banner the fleet page shows while it is paused. Everything above
  holds for each of its hosts, including one added while it is paused. The flags are
  `environments[].paused: true` and `projects[].paused: true`. Each keeps its own: resuming a
  project leaves an environment or host that was paused on its own still paused, and a host's
  status names the widest pause holding it (project, then environment, then the host).

### Fixes

* SSH config backups are named after the file's full path. Pruning `prod` no longer deletes
  `prod.old`'s copies, two files called `config` keep separate backups, and edits landing in the
  same millisecond no longer overwrite each other's copy. Writes go through a symlink to the real
  file instead of replacing the link, and are flushed to disk before the rename.
* Two reloads at once could interleave and report one another's changes; they now take turns.
* A container that was on a host and is gone now counts in the fleet summary, which always said
  none were, and turns the host red in `sshfleet fleet` and `sshfleet containers`. All three read
  a field the collector never filled.
* A new `config.yaml` no longer carries settings nothing reads (`ui.theme`, `ui.vim_keys`,
  `ui.notify`, `refresh.stats_s`, `refresh.age_tick_s`); a file that has them still loads.
* Following logs no longer retries for ever. A paused host is let go at once, a container gone
  for ten minutes and six failed connections in a row are let go too, and each source says so
  once instead of adding the same error every few seconds.
* `config.yaml` is checked when it is read: a setting of the wrong kind (`refresh.fast_s: abc`)
  is refused with the key it was found under, instead of failing later, over and over, inside the
  refresh loop. A bare `strict_host_key_checking: yes`, which YAML reads as true, is the `yes`
  OpenSSH expects rather than a `True` that failed every connection.
* A check still in flight for a host that was removed or paused meanwhile no longer brings it back
  to the fleet page.
* A refused change to the alert levels no longer stays in the running config until the next save.
* Hosts of an environment registered after another one with a key showed as unreachable
  ("Could not resolve hostname"), although plain `ssh` reached them. OpenSSH reads an `Include`
  inside a `Host` block only for hosts matching that block, and each environment's file was
  included right under the previous environment's key block. Every `Include` in the generated
  config now starts from `Match all`.

### Every page

* Every view now comes back as you left it, the way the Logs page already did. The fleet keeps
  its filter, role, Alerts only switch, sort, how far the table was widened, and the host drawer
  that was open, on the tab it was on. Files keeps the server, the directory, the hidden-files
  switch and your place in a long listing. Settings keeps its tab and the Hosts filter, and Audit
  keeps its action filter and the older entries already loaded. The main column also returns
  to where each page was scrolled, by the sidebar or by Back and Forward. A Files or Logs icon
  beside a host is still a new question and opens fresh. All of this lives in memory for as long
  as the tab is open, and nothing is written anywhere.
* Search and filter boxes get a clear button once something is typed in them, and Escape clears
  them too. The Logs pattern field has the button but ignores Escape, so a regex that took a
  while to write is not lost to a stray keypress.
* Files: opening another directory, or toggling hidden files, now always starts at the top. A
  directory that had to load used to open at the previous one's scroll offset.

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

* The bell, the alerts drawer and the fleet's Alerts card count the same thing: critical and
  warning. The drawer used to count the informational entries too (a container that exited
  cleanly before sshfleet first saw it, a restart count from a run over a day old), so it said 16
  where the card said 8. Those are now listed below as Notes, folded and counted apart. The bell and
  drawer also read each host's alerts live, as the card does, instead of a list that was only
  replaced when a whole sweep ended. Before, after checking one host the two disagreed until the
  next sweep.
* A gone or stopped container can be ignored from the alerts drawer: this container, or every
  container named like it, with the glob shown before you choose (`destination-postgres-write-*`
  for a job runner's per-run names).
* One check that could not reach a host no longer wipes its "was here and is gone" alerts. A
  check where sudo or docker failed no longer reports every workload on the host as gone. Either
  kind of check now carries what was known through unchanged. A loss still clears when the
  container comes back, when it is ignored, or 6 hours after it was first missed
  (`thresholds.vanished_memory_s`).

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
