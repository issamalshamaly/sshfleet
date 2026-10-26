# Configuration

`~/.config/sshfleet/config.yaml`, written by the Settings page (or `env add`). Besides the
per-host records above, the useful keys:

```yaml
projects:
  - {name: acme, color: violet, description: Customer platform}
  - {name: beta, color: teal}
environments:
  - {name: production, ssh_config_path: ~/.ssh/config.d/production, project: acme, short: prod, production: true}
  - {name: main, id: beta-main, ssh_config_path: ~/.ssh/config.d/beta_main, project: beta, short: main, production: true}
refresh:     {fast_s: 120, connect_timeout_s: 10, collector_timeout_s: 25}
concurrency: {global: 16, per_bastion: 8}
thresholds:  {disk_warn_pct: 80, disk_crit_pct: 90, mem_warn_pct: 85, vanished_memory_s: 21600}
logs:        {default_before: 60, tail_lines: 50000, display_tz: local, allowed_paths: []}
tunnels:
  kibana-prod: {env: production, via: prod-kibana, remote: 127.0.0.1:5601, local: 15601,
                open: "http://127.0.0.1:15601"}
files:       {download_dir: auto, deny_extra: []}
ssh:         {strict_host_key_checking: accept-new, control_persist: 10m, include_user_config: false}
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

An environment's name has to be unique only inside its project, so two projects can each have a
`staging` and a `production`. Everything else — its hosts, tunnels, the audit trail, the web
interface's links and `--env` — refers to it by its `id`, which is unique across every project.
The id is the name unless another environment already had that id when this one was registered;
then it is `<project>-<name>` (`beta-main` above, had another project's `main` come first). It is written to the file only when it
differs from the name, and it never changes, not even when the project is renamed. On the command
line an environment can also be named as `project/name`; a name or short label that two projects
share is refused as ambiguous rather than guessed.

`production: true` marks an environment as production: a container restart there needs the
container's name typed first, in the web interface and on the command line, and its actions are
shown in red with a `prod` mark beside it in the sidebar. It is yours to say, and the Settings
form asks every time an environment is added; the name only suggests the answer (`prod`
anywhere, or `prd` or `live` as a word, but not `preprod` or `non-prod`), so an environment called
`main` is production only once you mark it. An environment registered before the setting has no
`production` key, and its name decides as it always did: `prod` anywhere, and now `prd` and
`live` as well. Saving it from the form, or `env add --production` / `--not-production` with
`--replace`, records the answer.

`hosts.<alias>.connect_timeout_s` overrides the fleet-wide `refresh.connect_timeout_s` for one
host. A host can answer perfectly and still take half a minute to let you in — a wedged
`systemd-logind` makes `pam_systemd` wait out a 25-second D-Bus timeout on every new login — and
that is not the same thing as being down. When it times out the message says how long it waited,
so the two are told apart.

A value of the wrong kind (`fast_s: abc`, `compression: sometimes`) is refused when the file is
read, with the key it was found under. `ssh.strict_host_key_checking` is `accept-new` (a host's
key is trusted the first time it is seen) or `yes` (only keys already in `known_hosts`);
[SECURITY.md](../SECURITY.md) explains what sshfleet sets for every connection and what it leaves to your SSH files.

`display_tz` is an IANA name or `local`. `logs.allowed_paths` extends the directories a log
source may read from (`/var/log`, `/home`, `/opt`, `/srv`, `/usr/src`, `/app`, `/data` by
default). Everything lives under `~/.config/sshfleet`, `~/.cache/sshfleet` and
`~/.local/state/sshfleet`; set `SSHFLEET_HOME` to keep it all somewhere else.

The per-host records (`hosts:`) and the rules they follow are described in
[guide.md](guide.md#what-sshfleet-knows-about-a-host).

## Extending

* **A different stack** needs nothing: its workloads are discovered. Name them with `label`,
  narrow them with `workloads`, and point `health` at whatever answers.
* **A file only one host has** goes under `hosts.<alias>.log_sources`.
* **A new log format** is a parser in `src/sshfleet/logs/parsers.py` plus a `LogFormat` value; a
  host names it under `log_format`.
* **Other search backends** (Seq, Elasticsearch) have their `SourceKind` reserved and are the
  obvious next modules.
