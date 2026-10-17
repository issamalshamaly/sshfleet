# Security

## What sshfleet does and does not do

* It never reads a private key. Identity files are recorded as paths and checked with `stat`.
  Authentication is OpenSSH's job, through the `ssh` binary on your machine.
* It never stops, kills or prunes a container. Restart is the only action on a running
  container. A container that is already stopped can be removed, and only then: the state is
  read on the host right before the command, the name must be typed back, and the last log
  lines are saved first. The command layer cannot build anything else.
* It never downloads or previews files that look like secrets (`*.env*`, `*.pem`, `*.key`,
  `id_rsa*`, `authorized_keys`, …). The deny list is enforced in the transfer code.
* It never adds to your `~/.ssh/config` without you pressing the button that says so, and it
  keeps a backup first.
* The web server binds to `127.0.0.1` only, and answers only to the names `127.0.0.1` and
  `localhost`. The page is served only to a browser let in by a link `sshfleet web` hands out,
  which works once, within ten minutes; other users and programs on the same computer get a note
  instead. Every request from the page must carry the session token in a header, so no other
  origin can call it, and the terminal socket refuses any page but its own. The token and the
  cookie value are kept in `~/.local/state/sshfleet/web-session.json` (0600); delete it to sign
  every browser out at the next start.
* Everything it changes, and every folder listed, file previewed and log searched, is written
  to an audit log under `~/.local/state/sshfleet/`. Each line carries the hash of the one before;
  `sshfleet audit verify` finds a line changed, removed or slipped in. Someone who can write the
  file can also rewrite the whole chain, so note the last link it prints somewhere else if that
  matters to you.

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use GitHub's private vulnerability
reporting on the repository (Security → Report a vulnerability). You will get an
acknowledgement within a few days.

## How it connects

Every connection goes through a config file sshfleet writes (`~/.config/sshfleet/ssh_config`,
0600) and passes to `ssh -F`. It starts with a `Host *` block of its own and then `Include`s
each environment's SSH file. OpenSSH keeps the first value it reads for an option, so the lines
below win over the same option in your files:

| Line | Why |
|---|---|
| `BatchMode yes` | ssh never asks for anything. A password or passphrase prompt fails instead of hanging a page. |
| `StrictHostKeyChecking accept-new` | A host's key is trusted the first time it is seen and checked from then on; a key that changes is refused. |
| `UpdateHostKeys no` | A host cannot add or swap keys in `known_hosts` behind your back. |
| `ControlMaster auto`, `ControlPersist` | One connection per host, shared by every page and command. |

**Host keys.** `accept-new` is trust on first use: whoever answers the very first connection to a
host is believed. To believe no one unseen, put the keys in `~/.ssh/known_hosts` from a source
you trust (your provisioning, or `ssh-keyscan` compared with the console of the machine) and set
`ssh.strict_host_key_checking: yes`. `sshfleet doctor` says which of the two is in effect.

**`~/.ssh/config` is not read** unless `ssh.include_user_config: true`. Only the environment
files you registered apply, so settings you keep in `~/.ssh/config` (a `UserKnownHostsFile`, an
`IdentityAgent`, hardening of your own) do not reach sshfleet's connections. With the option on,
it is included last, after sshfleet's lines and the environment files.

**Agent forwarding and local commands** are left to you. sshfleet sets neither `ForwardAgent` nor
`PermitLocalCommand`, so both are OpenSSH's default, off, unless an environment file turns them
on. Forwarding your agent to a host lets anyone who is root there use your keys for as long as
the connection is open, and sshfleet keeps connections open for minutes at a time
(`ssh.control_persist`). Turn it on only for the hosts that need it.

## What runs with sudo on a host

Every command below runs as `sudo -n` (never asking for a password) on a host where `sudo` is on,
which is the default for docker hosts and off for podman (`hosts.<alias>.sudo` in the config).
`<rt>` is `docker` or `podman`. Running a container runtime as root is root on the host, and so
is membership of the `docker` group; the least privilege is a rootless runtime with `sudo: false`.

| When | Commands |
|---|---|
| Every check of a host | `true` (can sudo be used at all), `<rt> ps -a`, `<rt> ps -aq`, `<rt> inspect` |
| Containers page and quick actions | `<rt> restart -t N <name>`, `<rt> ps`, `<rt> inspect`, `<rt> logs`, `<rt> stats`, `<rt> exec` (the quick actions: a shell, a root shell, a database client, `npm install` of a TypeScript toolkit as root inside the container, `nginx -t`, the process list, the health check), and `<rt> rm <name>` for a container already seen stopped |
| Log search and follow | `timeout … <rt> logs`, `<rt> exec <name> sh -c …` (tail or grep of a file inside the container), `<rt> ps`, `<rt> inspect`; for log files on the host `find <dir> -maxdepth 1`, `stat -c %s`, `nice ionice cat`/`zcat`; for the journal `env TZ=UTC timeout … journalctl` |
| Files, only after the same command failed without sudo | `find <dir> -mindepth 1 -maxdepth 1`, `readlink -f`, `stat`, `head -c` (preview), `cat` (download), `install -m … -o …` (upload) |

Each container command is built from a fixed list of verbs, and a container name is checked
before it is used; `tests/test_containers.py` holds both. The deny list of the Files page is
checked against what a path resolves to on the host, before anything is read.

## Things worth knowing when you deploy it

* Log search output can contain whatever your services log. Bundles are written under
  `~/sshfleet/downloads/`; treat them like the logs they came from.
* Browser automation tools that snapshot pages will capture log output too. Keep their output
  directories out of any repository (`.playwright-cli/` is ignored here for that reason).
