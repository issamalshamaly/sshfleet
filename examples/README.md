# Examples

* `ssh-config.d/acme-staging` — an environment: an ordinary SSH config file with a bastion and
  four hosts behind it. Copy it to `~/.ssh/config.d/`, put your own addresses in, and register it
  as its header says.
* `config.yaml` — what sshfleet then keeps in `~/.config/sshfleet/config.yaml` once a few hosts
  have been told what to watch. The Settings page writes it; it is here to read.

To look around without any servers, run the web interface against the test suite's fake ssh, as
described in CONTRIBUTING.md.
