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
* The web server binds to `127.0.0.1` only. Every request must carry a per-session token that is
  minted at start-up and embedded in the page the server serves, so no other origin can call it.
* Everything it changes is written to an audit log under `~/.local/state/sshfleet/`.

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use GitHub's private vulnerability
reporting on the repository (Security → Report a vulnerability). You will get an
acknowledgement within a few days.

## Things worth knowing when you deploy it

* Log search output can contain whatever your services log. Bundles are written under
  `~/sshfleet/downloads/`; treat them like the logs they came from.
* Browser automation tools that snapshot pages will capture log output too. Keep their output
  directories out of any repository (`.playwright-cli/` is ignored here for that reason).
