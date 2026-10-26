"""Build the inventory from the registered SSH config files.

The SSH config files are the source of truth: the tool never keeps its own host list, so adding
a server to the environment's file is all that is needed for it to appear.
"""

from __future__ import annotations

from ..config import Config, EnvironmentCfg
from ..config.schema import HostRole
from .model import LABEL_WIDTH, Environment, Host, Inventory, strip_env_prefix
from .sshconfig import HostStanza, common_prefix, parse_ssh_config


def _bastion_aliases(stanzas: list[HostStanza], env_cfg: EnvironmentCfg) -> list[str]:
    if env_cfg.bastions != "auto":
        return list(env_cfg.bastions)
    seen: dict[str, int] = {}
    for stanza in stanzas:
        if stanza.proxy_jump:
            seen[stanza.proxy_jump] = seen.get(stanza.proxy_jump, 0) + 1
    return [alias for alias, _ in sorted(seen.items(), key=lambda kv: -kv[1])]


def _host_from_stanza(
    stanza: HostStanza,
    env_cfg: EnvironmentCfg,
    config: Config,
    prefix: str | None,
    bastions: set[str],
) -> Host:
    """Everything known about one host, which is only what the operator wrote plus two defaults."""
    alias = stanza.alias
    override = config.host_override(alias)
    health = override.health or {}
    runtime = override.runtime
    # Rootless podman needs no sudo; anything else is assumed to. Carried over from version 2
    # rather than justified: the real answer is a probe on the host, which is a separate change.
    sudo = override.sudo if override.sudo is not None else runtime != "podman"

    return Host(
        alias=alias,
        env=env_cfg.id,
        hostname=stanza.hostname,
        user=stanza.user,
        port=stanza.port,
        proxy_jump=stanza.proxy_jump,
        is_bastion=alias in bastions,
        role=HostRole.parse(override.role),
        runtime=runtime,
        sudo=sudo,
        display=override.display,
        log_label=override.label or strip_env_prefix(alias, prefix)[:LABEL_WIDTH],
        main=override.main,
        workload_glob=override.workloads,
        log_format=override.log_format,
        tags=list(override.tags),
        health_port=health.get("port"),
        health_path=health.get("path"),
        probe_min_interval_s=int(health.get("min_interval_s") or 0),
        web_root=override.web_root,
        expected_count=override.expected_count,
        ignored_containers=list(override.ignored_containers),
        connect_timeout_s=override.connect_timeout_s,
        log_sources=[dict(entry) for entry in override.log_sources],
        paused=bool(override.paused),
        source=stanza.source,
    )


def build_environment(env_cfg: EnvironmentCfg, config: Config) -> Environment:
    stanzas = parse_ssh_config(env_cfg.ssh_config_path)
    aliases = [s.alias for s in stanzas]
    prefix = None if env_cfg.alias_pattern == "auto" else env_cfg.alias_pattern.rstrip("*")
    if prefix is None:
        prefix = common_prefix(aliases)

    bastions = _bastion_aliases(stanzas, env_cfg)
    bastion_set = set(bastions)
    hosts = [_host_from_stanza(s, env_cfg, config, prefix, bastion_set) for s in stanzas]

    return Environment(
        name=env_cfg.name,
        id=env_cfg.id,
        short=env_cfg.short or env_cfg.name[:4],
        color=env_cfg.color,
        project=env_cfg.project,
        paused=env_cfg.paused,
        hosts=hosts,
        bastions=bastions,
        alias_prefix=prefix,
        key_path=env_cfg.key_path,
        ssh_config_path=env_cfg.ssh_config_path,
    )


def build_inventory(config: Config) -> Inventory:
    """Parse every registered environment.

    An environment whose file cannot be read comes back empty with a warning attached, so one
    deleted file costs that environment its hosts and nothing else its fleet.
    """
    inventory = Inventory(paused_projects={p.name for p in config.projects if p.paused})
    for env_cfg in config.environments:
        try:
            environment = build_environment(env_cfg, config)
        except OSError as exc:
            inventory.warnings.append(f"environment {env_cfg.name}: {exc}")
            environment = Environment(
                name=env_cfg.name,
                id=env_cfg.id,
                short=env_cfg.short or env_cfg.name[:4],
                color=env_cfg.color,
                project=env_cfg.project,
                paused=env_cfg.paused,
                key_path=env_cfg.key_path,
                ssh_config_path=env_cfg.ssh_config_path,
            )
        inventory.environments.append(environment)
        for host in environment.hosts:
            inventory.by_alias.setdefault(host.alias, host)
    return inventory
