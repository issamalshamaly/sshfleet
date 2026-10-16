"""Upgrade an older ``config.yaml`` in place.

Two upgrades live here.

**v1 → v2** stamps the version; nothing else changed in the file.

**v2 → v3** is the real one. Version 2 kept a fleet-wide ``services:`` catalogue bound to hosts by
matching alias fragments; version 3 has no services at all — a host carries its own properties and
its workloads are whatever the runtime reports. The conversion therefore has to answer, for every
host, "which service did version 2 think you were", and it must keep answering that the same way
forever. So :func:`migrate_v2_to_v3` is a **pure function of the old mapping** and carries a frozen
copy of every v2 rule it needs (alias matching, container naming, field inheritance). It never
calls the current implementation, which is free to change or disappear.
"""

from __future__ import annotations

import copy
import fnmatch
import os
import re
from pathlib import Path
from typing import Any

import yaml

from .schema import CONFIG_VERSION, ConfigError

# --------------------------------------------------------------------- frozen v2 semantics


def _v2_common_prefix(aliases: list[str]) -> str | None:
    """The longest shared alias prefix ending in "-", as version 2 computed it."""
    if len(aliases) < 2:
        return None
    first, last = min(aliases), max(aliases)
    shared = ""
    for a, b in zip(first, last, strict=False):
        if a != b:
            break
        shared += a
    shared = shared[: shared.rfind("-") + 1] if "-" in shared else ""
    return shared if len(shared) >= 4 else None


def _v2_service_for_alias(alias: str, prefix: str | None, services: dict[str, Any]) -> str | None:
    """Version 2's alias matching: longest matching fragment over key + ``aliases``."""
    bare = alias[len(prefix) :] if prefix and alias.startswith(prefix) else alias
    bare = bare.lower()
    best: tuple[int, str] | None = None
    for key, raw in services.items():
        for fragment in [key, *(raw.get("aliases") or [])]:
            fragment = str(fragment).lower()
            matches = (
                bare == fragment
                or bare.startswith(fragment + "-")
                or bare.endswith("-" + fragment)
                or fragment in bare.split("-")
            )
            if matches and (best is None or len(fragment) > best[0]):
                best = (len(fragment), key)
    return best[1] if best else None


def _v2_container_name(base: str, deploy_env: str, template: str) -> str:
    return template.format(container=base, deploy_env=deploy_env)


def _v2_label(key: str, raw: dict[str, Any]) -> str:
    return str(raw.get("label") or key)


def _v2_role(raw: dict[str, Any]) -> str:
    #: Version 2's "unknown" role has no version 3 equivalent; such hosts become plain services.
    role = str(raw.get("role") or "unknown")
    return "service" if role == "unknown" else role


# ------------------------------------------------------------------------- the conversion


def migrate_v2_to_v3(raw: dict[str, Any], aliases_by_env: dict[str, list[str]]) -> dict[str, Any]:
    """Return the version-3 mapping for a version-2 one. Pure: no file access, no globals.

    ``aliases_by_env`` maps each environment name to the ssh aliases in its config file, which is
    the only thing the caller has to read from disk.
    """
    services: dict[str, Any] = dict(raw.get("services") or {})
    main_services = [str(s) for s in (raw.get("main_services") or [])]
    naming = dict(raw.get("naming") or {})
    template = str(naming.get("container_template") or "{container}-{deploy_env}")
    existing_hosts: dict[str, Any] = {k: dict(v or {}) for k, v in (raw.get("hosts") or {}).items()}

    hosts: dict[str, Any] = {}
    for env in raw.get("environments") or []:
        env_name = str(env.get("name") or "")
        deploy_envs = [str(e) for e in (env.get("deploy_envs") or ["production"])]
        primary = deploy_envs[0] if deploy_envs else "production"
        aliases = list(aliases_by_env.get(env_name) or [])
        prefix = env.get("alias_pattern")
        prefix = str(prefix).rstrip("*") if prefix and prefix != "auto" else _v2_common_prefix(aliases)

        for alias in aliases:
            override = existing_hosts.get(alias, {})
            key = override.get("service") or _v2_service_for_alias(alias, prefix, services)
            svc = dict(services.get(key) or {}) if key else {}
            entry: dict[str, Any] = {}

            entry["role"] = override.get("role") or (_v2_role(svc) if svc else "service")
            if key and key in main_services:
                entry["main"] = True
            entry["label"] = _v2_label(key, svc) if key else None

            runtime = override.get("runtime") or svc.get("runtime")
            if runtime:
                entry["runtime"] = runtime
            # Version 2 let the service decide sudo, then the host override on top.
            sudo = svc.get("sudo") if svc.get("sudo") is not None else None
            if override.get("sudo") is not None:
                sudo = override["sudo"]
            if sudo is not None:
                entry["sudo"] = bool(sudo)

            health = dict(override.get("health") or {})
            port = health.get("port") or svc.get("port")
            path = health.get("path") or svc.get("health_path")
            interval = health.get("min_interval_s")
            if interval is None:
                interval = svc.get("probe_min_interval_s") or 0
            merged_health = {k: v for k, v in (("port", port), ("path", path)) if v}
            if int(interval or 0):
                merged_health["min_interval_s"] = int(interval)
            if merged_health:
                entry["health"] = merged_health

            # The workload filter reproduces exactly what version 2 would have looked at: the glob
            # when it had one, the one container name when a single deploy env, a prefix glob when
            # the environment ran several of them.
            if svc.get("container_glob"):
                entry["workloads"] = svc["container_glob"]
            elif svc.get("deployed"):
                base = svc.get("container") or key
                if len(deploy_envs) > 1:
                    entry["workloads"] = _v2_container_name(str(base), "*", template)
                else:
                    entry["workloads"] = _v2_container_name(str(base), primary, template)

            if svc.get("log_format") and svc["log_format"] != "auto":
                entry["log_format"] = svc["log_format"]

            log_sources: list[dict[str, Any]] = []
            if svc.get("container_file"):
                log_sources.append(
                    {
                        "path": svc["container_file"],
                        "format": svc.get("container_file_format") or "auto",
                        "in_container": True,
                    }
                )
            for template_path in svc.get("log_files") or []:
                source = {"path": str(template_path).format(deploy_env=primary), "format": svc.get("log_file_format") or "auto"}
                if svc.get("log_file_rotated_glob"):
                    source["rotated_glob"] = svc["log_file_rotated_glob"]
                log_sources.append(source)
            log_sources.extend(dict(s) for s in (override.get("log_sources") or []))
            if log_sources:
                entry["log_sources"] = log_sources

            web_root = override.get("web_root") or svc.get("web_root")
            if web_root:
                entry["web_root"] = web_root
            if override.get("display"):
                entry["display"] = override["display"]
            if override.get("tags"):
                entry["tags"] = list(override["tags"])
            if override.get("expected_count") is not None:
                entry["expected_count"] = int(override["expected_count"])
            if override.get("ignored_containers"):
                entry["ignored_containers"] = list(override["ignored_containers"])

            hosts[alias] = {k: v for k, v in entry.items() if v is not None}

    # Overrides for hosts that are no longer in any ssh config file are carried over untouched,
    # minus the two keys version 3 does not have.
    for alias, override in existing_hosts.items():
        if alias not in hosts:
            kept = {k: v for k, v in override.items() if k not in {"service", "expected_containers"}}
            if kept:
                hosts[alias] = kept

    out = {k: v for k, v in raw.items() if k not in {"services", "main_services", "naming", "hosts"}}
    out["version"] = CONFIG_VERSION
    for env in out.get("environments") or []:
        env.pop("ports_by_deploy_env", None)
    if hosts:
        out["hosts"] = hosts
    return out


# --------------------------------------------------------------------------- file handling


def file_version(raw: dict[str, Any]) -> int:
    try:
        return int(raw.get("version") or 1)
    except (TypeError, ValueError):
        return 1


def _read(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML — {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")
    return raw


def write_upgraded(path: Path, data: dict[str, Any]) -> None:
    """Replace the config with ``data``, in an order that never leaves the path empty.

    The new file is written and flushed first, the current one is **copied** (never renamed) to
    ``.bak``, and only then does the atomic replace happen. A crash at any point leaves a complete
    old file or a complete new one where the tool looks for it.
    """
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)
    tmp = path.with_name(f".{path.name}.upgrading")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, 0o600)

    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        if backup.exists():
            backup.replace(backup.with_suffix(backup.suffix + ".1"))
        # Copied, never renamed: the config must stay at its own path the whole time.
        with backup.open("wb") as handle:
            handle.write(path.read_bytes())
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(backup, 0o600)

    os.replace(tmp, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def aliases_from_ssh_configs(raw: dict[str, Any]) -> dict[str, list[str]]:
    """Which ssh aliases each environment has, the one thing the conversion cannot compute."""
    from ..inventory import parse_ssh_config

    out: dict[str, list[str]] = {}
    for env in raw.get("environments") or []:
        name = str(env.get("name") or "")
        path = Path(str(env.get("ssh_config_path") or "")).expanduser()
        try:
            out[name] = [s.alias for s in parse_ssh_config(path)]
        except (OSError, FileNotFoundError):
            out[name] = []
    return out


def needs_upgrade(path: Path) -> bool:
    """Whether ``upgrade`` would rewrite this file. Never raises: an unreadable file is upgrade's
    to report."""
    try:
        return path.exists() and file_version(_read(path)) < CONFIG_VERSION
    except (ConfigError, OSError, ValueError):
        return False


def upgrade(path: Path) -> str | None:
    """Bring a config file to the current version. Returns a note for the caller, or None."""
    if not path.exists():
        return None
    raw = _read(path)
    if file_version(raw) >= CONFIG_VERSION:
        return None
    write_upgraded(path, upgrade_mapping(raw))
    return f"{path} upgraded to version {CONFIG_VERSION}: services are now properties of each host"


def upgrade_mapping(raw: dict[str, Any], aliases: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """The whole upgrade as a function of the old mapping, validated, writing nothing.

    The one input the conversion cannot compute is which ssh aliases each environment has. By
    default they are read from the SSH files the mapping names; restore and import pass them in,
    read from the SSH text they carry, because what is on disk now may not be what the old
    config was written against.
    """
    version = file_version(raw)
    if version >= CONFIG_VERSION:
        return raw
    raw = copy.deepcopy(raw)
    if version < 2:
        raw["version"] = 2
        version = 2
    if version < 3:
        raw = migrate_v2_to_v3(raw, aliases if aliases is not None else aliases_from_ssh_configs(raw))
    _validate_v3(raw)
    return raw


def _validate_v3(raw: dict[str, Any]) -> None:
    """Refuse to write anything the loader would reject. Cheap, but it runs before the replace."""
    from .loader import load_mapping

    if raw.get("version") != CONFIG_VERSION:
        raise ConfigError("upgraded config did not reach the current version")
    for key in ("services", "main_services", "naming"):
        if key in raw:
            raise ConfigError(f"upgraded config still carries {key!r}")
    for alias, entry in (raw.get("hosts") or {}).items():
        if not isinstance(entry, dict):
            raise ConfigError(f"host {alias!r}: expected a mapping")
        glob = entry.get("workloads")
        if glob is not None and not isinstance(glob, str):
            raise ConfigError(f"host {alias!r}: workloads must be a glob string")
        if glob:
            fnmatch.translate(glob)  # raises nothing, but proves it compiles
        role = entry.get("role")
        if role and not re.fullmatch(r"[a-z]+", str(role)):
            raise ConfigError(f"host {alias!r}: bad role {role!r}")
    load_mapping(raw)  # the real parser has the last word


# Kept for the v1 path, which older tests call directly.
def migrate_v1(path: Path) -> bool:
    return upgrade(path) is not None
