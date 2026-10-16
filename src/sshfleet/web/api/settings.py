"""Settings: environments, identity files, hosts and refresh behaviour.

Everything here changes files the operator owns, so three rules hold throughout: key files are
only ever ``stat``-ed, SSH config files are edited one stanza at a time with a backup, and every
change is written to the audit log and followed by a reload so the fleet view agrees with disk.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ... import envsetup
from ...config import ConfigError, EnvironmentCfg, HostRole, ProjectCfg, TunnelCfg
from ...config import defaults as D
from ...config import loader as config_loader
from ...config.schema import HostOverride
from ...inventory import parse_ssh_config, sshedit
from ...inventory.sshconfig import looks_like_key
from ...inventory.sshedit import HostEdit, SshEditError
from ..security import require_token
from ..state import WebState

router = APIRouter(prefix="/api/settings", dependencies=[Depends(require_token)])

KEY_GLOBS = ("*.pem", "id_*", "*.key")
MAX_CONFIG_BYTES = 2 * 1024 * 1024


def _state(request: Request) -> WebState:
    return request.app.state.web


def _looks_like_key(path: Path) -> bool:
    return looks_like_key(path)


def _save(state: WebState) -> None:
    config_loader.save(state.context.config, state.context.paths.config_file)


def _key_payload(path: Path | None) -> dict[str, Any] | None:
    """Metadata for an identity file. The file is stat-ed, never opened."""
    if not path:
        return None
    check = envsetup.check_key(Path(path))
    return {
        "path": str(path),
        "exists": check.exists,
        "ok": check.ok,
        "mode": f"{check.mode:04o}" if check.exists else None,
        "problems": check.problems,
        "fix": check.fix if not check.ok else None,
    }


def _project_payload(state: WebState, project: ProjectCfg) -> dict[str, Any]:
    config = state.context.config
    members = [e.name for e in config.environments if e.project == project.name]
    return {
        "name": project.name,
        "color": project.color,
        "description": project.description,
        "environments": members,
        "hosts": sum(len(state.context.inventory.hosts(name)) for name in members),
        "paused": project.paused,
    }


def _environment_payload(state: WebState, env_cfg: EnvironmentCfg) -> dict[str, Any]:
    inventory = state.context.inventory
    try:
        environment = inventory.environment(env_cfg.name)
        hosts, bastions, prefix = environment.hosts, environment.bastions, environment.alias_prefix
    except KeyError:
        hosts, bastions, prefix = [], [], None
    path = env_cfg.ssh_config_path
    return {
        "name": env_cfg.name,
        "short": env_cfg.short,
        "color": env_cfg.color,
        "ssh_config_path": str(path),
        "ssh_config_exists": path.exists(),
        "key": _key_payload(env_cfg.key_path),
        "alias_pattern": env_cfg.alias_pattern,
        "alias_prefix": prefix,
        "host_prefix": envsetup.host_prefix(env_cfg),  # every host added here starts with it
        "bastions": list(bastions),
        "bastions_auto": env_cfg.bastions == "auto",
        "hosts": len(hosts),
        "production": "prod" in env_cfg.name.lower(),
        "project": env_cfg.project,
        "paused": env_cfg.paused,  # its own flag; its project's is on the project
    }


def _host_payload(state: WebState, host) -> dict[str, Any]:
    config = state.context.config
    override = config.hosts.get(host.alias)
    return {
        "alias": host.alias,
        "env": host.env,
        "hostname": host.hostname,
        "user": host.user,
        "port": host.port,
        "proxy_jump": host.proxy_jump,
        "is_bastion": host.is_bastion,
        "main": host.main,
        "label": host.log_label,
        "role": host.role,
        "workloads": host.workload_glob,
        "log_format": host.log_format,
        "runtime": host.runtime,
        "sudo": host.sudo,
        "tags": list(host.tags),
        "display": host.display,
        "health_port": host.health_port,
        "health_path": host.health_path,
        "probe_min_interval_s": host.probe_min_interval_s,
        "expected_count": host.expected_count,
        "connect_timeout_s": host.connect_timeout_s,
        "ignored_containers": list(host.ignored_containers),
        "paused": host.paused,
        "paused_by": paused_by.to_dict() if (paused_by := state.context.inventory.paused_by(host.alias)) else None,
        "source": str(host.source) if host.source else None,
        "editable": bool(host.source) and os.access(host.source, os.W_OK),
        "override": bool(override),
    }


# ------------------------------------------------------------------------------ reading


@router.get("")
async def settings(request: Request):
    state = _state(request)
    context = state.context
    config = context.config
    return {
        "projects": [_project_payload(state, p) for p in config.projects],
        "environments": [_environment_payload(state, e) for e in config.environments],
        "refresh": {
            "fast_s": config.refresh.fast_s,
            "stats_s": config.refresh.stats_s,
            "stale_after_s": config.refresh.stale_after_s,
            "collector_timeout_s": config.refresh.collector_timeout_s,
            "connect_timeout_s": config.refresh.connect_timeout_s,
        },
        "thresholds": {
            "disk_warn_pct": config.thresholds.disk_warn_pct,
            "disk_crit_pct": config.thresholds.disk_crit_pct,
            "mem_warn_pct": config.thresholds.mem_warn_pct,
            "mem_crit_pct": config.thresholds.mem_crit_pct,
            "load_per_cpu_warn": config.thresholds.load_per_cpu_warn,
        },
        "paths": {
            "config_file": str(context.paths.config_file),
            "ssh_config": str(context.mux.ssh_config),
            "download_dir": str(context.download_dir),
            "audit_file": str(context.paths.audit_file),
            "backups": str(context.paths.ssh_backups),
        },
        "watching": [str(p) for p in state.watched_files()],
        "last_reload": state.last_reload,
        "last_reload_reason": state.last_reload_reason,
        "warnings": context.warnings,
    }


@router.get("/hosts")
async def hosts(request: Request, env: str | None = None):
    state = _state(request)
    return [_host_payload(state, h) for h in state.context.inventory.hosts(env)]


@router.get("/ssh-configs")
async def ssh_configs(request: Request):
    """SSH config files worth registering, with the host count each one would contribute."""
    state = _state(request)
    registered = {str(e.ssh_config_path): e.name for e in state.context.config.environments}
    candidates: list[Path] = []
    ssh_dir = Path.home() / ".ssh"
    for path in [ssh_dir / "config", *sorted((ssh_dir / "config.d").glob("*"))]:
        if path.is_file() and not _looks_like_key(path):
            candidates.append(path)
    candidates.extend(Path(p) for p in registered if Path(p) not in candidates)

    out = []
    for path in candidates:
        entry: dict[str, Any] = {"path": str(path), "name": path.name, "registered_to": registered.get(str(path))}
        try:
            if path.stat().st_size > MAX_CONFIG_BYTES:
                raise ValueError("file is too large to be an ssh config")
            stanzas = parse_ssh_config(path)
            entry["hosts"] = len(stanzas)
            entry["sample"] = [s.alias for s in stanzas[:4]]
        except (OSError, ValueError) as exc:
            entry["hosts"] = 0
            entry["error"] = str(exc)
        out.append(entry)
    return out


@router.get("/keys")
async def keys(request: Request):
    """Identity files found in ~/.ssh and referenced by the registered configs (stat only)."""
    state = _state(request)
    found: dict[str, dict[str, Any]] = {}
    ssh_dir = Path.home() / ".ssh"
    for pattern in KEY_GLOBS:
        for path in sorted(ssh_dir.glob(pattern)):
            if path.is_file() and not path.name.endswith(".pub"):
                found[str(path)] = _key_payload(path) or {}
    for env_cfg in state.context.config.environments:
        if env_cfg.key_path:
            found.setdefault(str(env_cfg.key_path), _key_payload(env_cfg.key_path) or {})
    return sorted(found.values(), key=lambda entry: entry["path"])


# ---------------------------------------------------------------------------- projects


class ProjectBody(BaseModel):
    name: str
    color: str | None = None
    description: str = ""


class ProjectPatch(BaseModel):
    name: str | None = None  # renaming re-points every environment filed under it
    color: str | None = None
    description: str | None = None
    paused: bool | None = None  # stop monitoring every host in it, closing what is open to them


@router.get("/projects")
async def projects(request: Request):
    state = _state(request)
    return [_project_payload(state, p) for p in state.context.config.projects]


@router.post("/projects")
async def add_project(request: Request, body: ProjectBody):
    """Create a project. It starts with no environments; they are filed under it afterwards."""
    state = _state(request)
    try:
        project = envsetup.add_project(
            state.context.config, name=body.name, color=body.color, description=body.description
        )
    except ConfigError as exc:
        raise HTTPException(400, str(exc)) from exc
    _save(state)
    state.context.audit.append("settings.project.add", target=project.name, status="ok")
    return {"ok": True, "project": _project_payload(state, project)}


@router.patch("/projects/{name}")
async def update_project(request: Request, name: str, body: ProjectPatch):
    state = _state(request)
    config = state.context.config
    project = config.project(name)
    if project is None:
        raise HTTPException(404, f"unknown project {name!r}")
    try:
        if body.name is not None and body.name.strip() != name:
            project = envsetup.rename_project(config, name, body.name)
    except ConfigError as exc:
        raise HTTPException(400, str(exc)) from exc
    if body.color is not None:
        project.color = body.color
    if body.description is not None:
        project.description = body.description.strip()
    pausing = body.paused is not None and body.paused != project.paused
    if pausing:
        project.paused = bool(body.paused)
    _save(state)
    if pausing:
        # Only a pause changes what is connected to; the reload closes or reopens it now.
        await state.reload(reason=f"project {project.name} {'paused' if project.paused else 'resumed'}")
    state.context.audit.append(
        "settings.project.update",
        target=project.name,
        status="ok",
        note=("paused" if project.paused else "resumed") if pausing else None,
    )
    return {"ok": True, "project": _project_payload(state, project)}


@router.delete("/projects/{name}")
async def remove_project(request: Request, name: str, confirm: str = ""):
    """Forget a project. It must be empty first: an environment always belongs to one."""
    state = _state(request)
    config = state.context.config
    if config.project(name) is None:
        raise HTTPException(404, f"unknown project {name!r}")
    if confirm != name:
        raise HTTPException(400, f"type {name!r} to confirm")
    try:
        envsetup.remove_project(config, name)
    except ConfigError as exc:
        raise HTTPException(400, str(exc)) from exc
    _save(state)
    state.context.audit.append("settings.project.remove", target=name, status="ok")
    return {"ok": True}


# ------------------------------------------------------------------------ environments


class PreviewBody(BaseModel):
    ssh_config_path: str = ""  # empty: the file sshfleet would create from ``project`` and ``short``
    name: str | None = None
    short: str | None = None
    project: str | None = None


class EnvironmentBody(BaseModel):
    name: str
    ssh_config_path: str | None = None  # empty: create ~/.ssh/config.d/<project>_<short> and start with no hosts
    key_path: str | None = None
    short: str | None = None
    color: str | None = None
    bastions: list[str] | None = None
    project: str  # an environment always belongs to one
    replace: bool = False


class EnvironmentPatch(BaseModel):
    short: str | None = None
    color: str | None = None
    key_path: str | None = None
    ssh_config_path: str | None = None
    create: bool = False  # allow ssh_config_path to name a file that does not exist yet
    bastions: list[str] | None = None
    project: str | None = None  # moves it to another project; never empty
    paused: bool | None = None  # stop monitoring every host in it, closing what is open to them


def _known_project(state: WebState, name: str | None) -> str:
    """Validate a project reference. An environment always belongs to one, so neither an empty
    name nor an unknown one is accepted."""
    clean = (name or "").strip()
    config = state.context.config
    if not clean:
        raise HTTPException(400, "an environment belongs to a project — create one first")
    if config.project(clean) is None:
        known = ", ".join(p.name for p in config.projects) or "none yet"
        raise HTTPException(400, f"unknown project {clean!r} (known: {known})")
    return clean


def _short_label(name: str, short: str | None) -> str:
    """The short label as it will be recorded: the one typed, else the one suggested from the name."""
    return (short or "").strip() or envsetup.suggest_identity(name)[1].strip()


def _env_path(state: WebState, raw: str | None, name: str | None, short: str | None, project: str | None) -> Path:
    text = (raw or "").strip()
    if text:
        return Path(text).expanduser()
    if not name or not name.strip():
        raise HTTPException(400, "give the environment a name, or point at an existing ssh config file")
    path = envsetup.default_env_file(_known_project(state, project), _short_label(name, short))
    # Two environments of one project with the same short label would land in one file and
    # each would show the other's hosts.
    owner = next(
        (e.name for e in state.context.config.environments if e.ssh_config_path.expanduser() == path and e.name != name.strip()),
        None,
    )
    if owner:
        raise HTTPException(400, f"{path} already holds environment {owner!r} — give this one another short label")
    return path


@router.post("/environments/preview")
async def preview_environment(request: Request, body: PreviewBody):
    state = _state(request)
    path = _env_path(state, body.ssh_config_path, body.name, body.short, body.project)
    if _looks_like_key(path):
        raise HTTPException(400, f"{path.name} looks like a key or a backup, not an ssh config")
    try:
        result = envsetup.preview(path, state.context.config, allow_empty=True)
    except (ConfigError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "path": str(path),
        "new_file": not path.exists(),
        "hosts": result.aliases,
        "summary": result.summary,
        "prefix": result.prefix,
        "bastions": result.bastions,
        "key_candidates": [_key_payload(Path(k).expanduser()) for k in result.key_candidates],
        "suggested": {
            "name": result.suggested_name,
            "short": result.suggested_short,
            "color": result.suggested_color,
        },
    }


@router.post("/environments")
async def add_environment(request: Request, body: EnvironmentBody):
    state = _state(request)
    if not body.name.strip():
        raise HTTPException(400, "the environment needs a name")
    path = _env_path(state, body.ssh_config_path, body.name, body.short, body.project)
    if _looks_like_key(path):
        raise HTTPException(400, f"{path.name} looks like a key or a backup, not an ssh config")
    created = not path.exists()
    # A file sshfleet names after the short label records that same label, so the two agree.
    auto_file = not (body.ssh_config_path or "").strip()
    try:
        envsetup.add_environment(
            state.context.config,
            name=body.name.strip(),
            ssh_config_path=path,
            key_path=Path(body.key_path).expanduser() if body.key_path else None,
            short=_short_label(body.name, body.short) if auto_file else body.short,
            color=body.color,
            # An empty list means "work them out from ProxyJump", not "this environment has none".
            bastions=body.bastions or None,
            project=_known_project(state, body.project),
            replace=body.replace,
            create_file=True,
        )
    except (ConfigError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(400, f"cannot create {path}: {exc.strerror or exc}") from exc

    _save(state)
    summary = await state.reload(reason=f"environment {body.name} registered")
    state.context.audit.append(
        "settings.env.add", env=body.name, target=str(path), status="ok", note="file created" if created else None
    )
    return {
        "ok": True,
        "environment": body.name,
        "created_file": str(path) if created else None,
        "reload": summary,
        "key": _key_payload(Path(body.key_path).expanduser() if body.key_path else None),
        "ssh_include": envsetup.include_status(),
    }


@router.patch("/environments/{name}")
async def update_environment(request: Request, name: str, body: EnvironmentPatch):
    state = _state(request)
    config = state.context.config
    try:
        env_cfg = config.environment(name)
    except ConfigError as exc:
        raise HTTPException(404, str(exc)) from exc

    if body.ssh_config_path is not None:
        path = _env_path(
            state,
            body.ssh_config_path,
            env_cfg.name,
            body.short or env_cfg.short,
            body.project or env_cfg.project,
        )
        if _looks_like_key(path):
            raise HTTPException(400, f"{path.name} looks like a key or a backup, not an ssh config")
        if not path.is_file():
            if not body.create:
                raise HTTPException(400, f"{path} is not a readable ssh config file")
            try:
                envsetup.create_env_file(path, env_cfg.name)
            except (ConfigError, OSError) as exc:
                raise HTTPException(400, f"cannot create {path}: {exc}") from exc
        env_cfg.ssh_config_path = path
    if body.short is not None:
        env_cfg.short = body.short.strip() or env_cfg.short
    if body.color is not None:
        env_cfg.color = body.color
    if body.key_path is not None:
        env_cfg.key_path = Path(body.key_path).expanduser() if body.key_path.strip() else None
    if body.bastions is not None:
        env_cfg.bastions = body.bastions or "auto"
    if body.project is not None:
        env_cfg.project = _known_project(state, body.project)
    pausing = body.paused is not None and body.paused != env_cfg.paused
    if pausing:
        env_cfg.paused = bool(body.paused)

    _save(state)
    summary = await state.reload(reason=f"environment {env_cfg.name} changed")
    state.context.audit.append(
        "settings.env.update",
        env=env_cfg.name,
        status="ok",
        note=("paused" if env_cfg.paused else "resumed") if pausing else None,
    )
    return {"ok": True, "environment": _environment_payload(state, env_cfg), "reload": summary}


@router.delete("/environments/{name}")
async def remove_environment(request: Request, name: str):
    """Forget an environment. The SSH config file it pointed at is never touched."""
    state = _state(request)
    try:
        envsetup.remove_environment(state.context.config, name)
    except ConfigError as exc:
        raise HTTPException(404, str(exc)) from exc
    _save(state)
    summary = await state.reload(reason=f"environment {name} removed")
    state.context.audit.append("settings.env.remove", env=name, status="ok")
    return {"ok": True, "reload": summary}


@router.post("/environments/{name}/test")
async def test_environment(request: Request, name: str):
    """Open the bastion connections and one host behind them, and report what happened."""
    state = _state(request)
    context = state.context
    try:
        environment = context.inventory.environment(name)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc

    if not environment.hosts:
        return {"ok": True, "bastions": {}, "elapsed": 0, "note": "nothing to test yet — add a host to this environment first"}

    started = time.time()
    results = await context.mux.warm_bastions(environment.name)
    payload: dict[str, Any] = {
        "bastions": {
            alias: {"ok": r.ok, "error": r.message, "seconds": round(r.elapsed, 2)} for alias, r in results.items()
        }
    }
    # One host behind a bastion proves the whole chain; with no bastions, the first host will do.
    target = next((h.alias for h in environment.hosts if h.proxy_jump), None)
    if target is None and not environment.bastions:
        target = environment.hosts[0].alias
    if target:
        outcome = await context.mux.warm(target)
        payload["host"] = {
            "alias": target,
            "ok": outcome.ok,
            "error": outcome.message,
            "seconds": round(outcome.elapsed, 2),
        }
    bastions_ok = all(b["ok"] for b in payload["bastions"].values())
    host_ok = payload["host"]["ok"] if "host" in payload else True
    payload["ok"] = bastions_ok and host_ok and bool(payload["bastions"] or "host" in payload)
    payload["elapsed"] = round(time.time() - started, 2)
    return payload


# --------------------------------------------------------------------- user ssh config


@router.get("/ssh-include")
async def ssh_include(request: Request):
    """Does the user's own ~/.ssh/config read the files sshfleet creates under config.d?"""
    _state(request)
    return envsetup.include_status()


@router.post("/ssh-include")
async def add_ssh_include(request: Request):
    """Add the Include line, once, with a backup. Only ever on request — never on its own."""
    state = _state(request)
    backups = state.context.paths.ssh_backups
    target = envsetup.ssh_dir() / "config"
    try:
        backup = envsetup.add_include_line(backups)
    except OSError as exc:
        raise HTTPException(400, f"cannot write {target}: {exc.strerror or exc}") from exc
    state.context.audit.append(
        "settings.ssh_include",
        target=str(target),
        status="ok",
        note=f"backup {backup.name}" if backup else "already present or new file",
    )
    return {"ok": True, "backup": str(backup) if backup else None, **envsetup.include_status()}


# -------------------------------------------------------------------------------- hosts


class HostBody(BaseModel):
    env: str
    alias: str
    hostname: str | None = None
    user: str | None = None
    port: int | None = None
    proxy_jump: str | None = None
    identity_file: str | None = None
    display: str | None = None
    label: str | None = None
    role: str | None = None
    main: bool | None = None
    workloads: str | None = None
    log_format: str | None = None
    tags: list[str] = Field(default_factory=list)
    health_port: int | None = None
    health_path: str | None = None
    probe_min_interval_s: int | None = None


class HostPatch(BaseModel):
    alias: str | None = None  # rename
    hostname: str | None = None
    user: str | None = None
    port: int | None = None
    proxy_jump: str | None = None  # "" clears it (a direct connection)
    identity_file: str | None = None
    display: str | None = None
    label: str | None = None
    role: str | None = None
    main: bool | None = None
    workloads: str | None = None  # "" clears the filter
    log_format: str | None = None
    runtime: str | None = None
    sudo: bool | None = None
    tags: list[str] | None = None
    health_port: int | None = None  # 0 falls back to the service catalogue
    health_path: str | None = None  # "" falls back to the service catalogue
    probe_min_interval_s: int | None = None  # 0 = every refresh, -1 = inherit from the service
    expected_count: int | None = None  # -1 = learn from what is seen
    ignored_containers: list[str] | None = None
    connect_timeout_s: int | None = None  # 0 = use the fleet-wide budget
    paused: bool | None = None  # stop monitoring: no connection to it, and the open one is closed


def _edit_from(body: HostBody | HostPatch) -> HostEdit:
    return HostEdit(
        hostname=body.hostname,
        user=body.user,
        port=str(body.port) if body.port is not None else None,
        proxy_jump=body.proxy_jump,
        identity_file=body.identity_file,
    )


def _check_parent(state: WebState, alias: str, parent: str | None, env: str) -> None:
    """A parent must exist in the same environment, and the chain must not loop."""
    if not parent:
        return
    inventory = state.context.inventory
    try:
        host = inventory.host(parent)
    except KeyError:
        raise HTTPException(400, f"unknown host {parent!r} to connect through") from None
    if host.env != env:
        raise HTTPException(400, f"{parent} is in {host.env}, not {env}")
    if parent == alias:
        raise HTTPException(400, "a host cannot connect through itself")
    seen = {alias}
    current: str | None = parent
    for _ in range(10):
        if current is None:
            return
        if current in seen:
            raise HTTPException(400, f"connecting through {parent} would create a loop")
        seen.add(current)
        try:
            current = inventory.host(current).proxy_jump
        except KeyError:
            return
    raise HTTPException(400, f"the chain through {parent} is too deep")


def _apply_override(state: WebState, alias: str, **fields: Any) -> None:
    """Store tool-side facts (display name, service, tags) that the SSH file has no room for."""
    config = state.context.config
    override = config.hosts.get(alias) or HostOverride()
    touched = False
    for key, value in fields.items():
        if value is None:
            continue
        if key == "main":
            override.main = bool(value)
            touched = True
        elif key == "paused":
            override.paused = True if value else None  # written only while it is set
            touched = True
        elif key == "role":
            override.role = HostRole.parse(value).value
            touched = True
        elif key in {"tags", "ignored_containers"}:
            setattr(override, key, [str(v) for v in value])
            touched = True
        elif key == "expected_count":
            if value < -1 or value > 500:
                raise HTTPException(400, "expected containers must be between 0 and 500 (-1 to learn)")
            override.expected_count = None if value < 0 else int(value)
            touched = True
        elif key == "connect_timeout_s":
            if value and (value < 2 or value > 300):
                raise HTTPException(400, "the connect timeout must be between 2 and 300 seconds")
            override.connect_timeout_s = int(value) or None
            touched = True
        elif value == "":
            setattr(override, key, None)
            touched = True
        else:
            setattr(override, key, value)
            touched = True
    if touched:
        config.hosts[alias] = override


def _apply_health(state: WebState, alias: str, body: HostBody | HostPatch) -> None:
    """Per-host probe settings. They live in the tool's own config, never in the ssh file.

    Each field has a "use the service default" value rather than being merely absent, because
    absent has to keep meaning "leave this alone" for a PATCH that only renames a host.
    """
    port, path, interval = body.health_port, body.health_path, body.probe_min_interval_s
    if port is None and path is None and interval is None:
        return
    if port is not None and not 0 <= port <= 65535:
        raise HTTPException(400, "health port must be between 0 and 65535")
    if path is not None and path != "" and not path.startswith("/"):
        raise HTTPException(400, "health path must start with /")
    if interval is not None and not -1 <= interval <= 86400:
        raise HTTPException(400, "probe interval must be between 0 and 86400 seconds (-1 to inherit)")

    config = state.context.config
    override = config.hosts.get(alias) or HostOverride()
    health = dict(override.health)
    if port is not None:
        health.pop("port", None) if port == 0 else health.update(port=port)
    if path is not None:
        health.pop("path", None) if path == "" else health.update(path=path)
    if interval is not None:
        health.pop("min_interval_s", None) if interval < 0 else health.update(min_interval_s=interval)
    override.health = health
    config.hosts[alias] = override


@router.post("/hosts")
async def add_host(request: Request, body: HostBody):
    state = _state(request)
    config = state.context.config
    try:
        env_cfg = config.environment(body.env)
    except ConfigError as exc:
        raise HTTPException(404, str(exc)) from exc
    if not body.alias.strip():
        raise HTTPException(400, "the host needs a name")
    # Aliases share one namespace across every project; the prefix keeps them apart.
    alias = envsetup.host_alias(env_cfg, body.alias)
    _check_parent(state, alias, body.proxy_jump, env_cfg.name)

    backups = state.context.paths.ssh_backups
    try:
        backup = sshedit.add_host(env_cfg.ssh_config_path, alias, _edit_from(body), backups)
    except SshEditError as exc:
        raise HTTPException(400, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(400, f"cannot write {env_cfg.ssh_config_path}: {exc}") from exc

    _apply_override(
        state, alias, display=body.display, label=body.label, role=body.role, main=body.main,
        workloads=body.workloads, log_format=body.log_format, tags=body.tags or None,
    )
    _apply_health(state, alias, body)
    _save(state)
    summary = await state.reload(reason=f"host {alias} added")
    state.context.audit.append(
        "settings.host.add",
        env=env_cfg.name,
        host=alias,
        target=str(env_cfg.ssh_config_path),
        note=f"backup {backup.name}" if backup else "file created",
        status="ok",
    )
    return {"ok": True, "alias": alias, "file": str(env_cfg.ssh_config_path), "backup": str(backup) if backup else None, "reload": summary}


@router.patch("/hosts/{alias}")
async def update_host(request: Request, alias: str, body: HostPatch):
    state = _state(request)
    try:
        host = state.context.inventory.host(alias)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc

    edit = _edit_from(body)
    renamed = body.alias.strip() if body.alias and body.alias.strip() != alias else None
    backup: Path | None = None

    if not edit.is_empty() or renamed:
        if not host.source:
            raise HTTPException(400, f"{alias} has no source file to edit")
        if body.proxy_jump:
            _check_parent(state, alias, body.proxy_jump, host.env)
        backups = state.context.paths.ssh_backups
        try:
            backup = sshedit.update_host(Path(host.source), alias, edit, backups, new_alias=renamed)
        except SshEditError as exc:
            raise HTTPException(400, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(400, f"cannot write {host.source}: {exc}") from exc

    key = renamed or alias
    if renamed and alias in state.context.config.hosts:
        state.context.config.hosts[key] = state.context.config.hosts.pop(alias)
    _apply_override(
        state,
        key,
        display=body.display,
        label=body.label,
        role=body.role,
        main=body.main,
        workloads=body.workloads,
        log_format=body.log_format,
        runtime=body.runtime,
        sudo=body.sudo,
        tags=body.tags,
        expected_count=body.expected_count,
        ignored_containers=body.ignored_containers,
        connect_timeout_s=body.connect_timeout_s,
        paused=body.paused,
    )
    _apply_health(state, key, body)
    _save(state)
    summary = await state.reload(reason=f"host {key} changed")
    state.context.audit.append(
        "settings.host.update",
        env=host.env,
        host=key,
        target=str(host.source) if host.source else None,
        note=f"backup {backup.name}" if backup else "overrides only",
        status="ok",
    )
    return {"ok": True, "alias": key, "backup": str(backup) if backup else None, "reload": summary}


@router.delete("/hosts/{alias}")
async def remove_host(request: Request, alias: str, confirm: str = ""):
    """Delete a host's stanza from the SSH config file it lives in. Requires the typed alias."""
    state = _state(request)
    try:
        host = state.context.inventory.host(alias)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    if confirm != alias:
        raise HTTPException(400, f"type {alias!r} to confirm removing it from the ssh config")
    if not host.source:
        raise HTTPException(400, f"{alias} has no source file to edit")

    backups = state.context.paths.ssh_backups
    try:
        backup = sshedit.remove_host(Path(host.source), alias, backups)
    except SshEditError as exc:
        raise HTTPException(400, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(400, f"cannot write {host.source}: {exc}") from exc

    state.context.config.hosts.pop(alias, None)
    _save(state)
    summary = await state.reload(reason=f"host {alias} removed")
    state.context.audit.append(
        "settings.host.remove",
        env=host.env,
        host=alias,
        target=str(host.source),
        note=f"backup {backup.name}" if backup else None,
        status="ok",
    )
    return {"ok": True, "backup": str(backup) if backup else None, "reload": summary}


@router.post("/hosts/{alias}/test")
async def test_host(request: Request, alias: str):
    state = _state(request)
    try:
        state.context.inventory.host(alias)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    outcome = await state.context.mux.warm(alias)
    return {"alias": alias, "ok": outcome.ok, "error": outcome.message, "seconds": round(outcome.elapsed, 2)}


# ------------------------------------------------------------------------------ tunnels


class TunnelBody(BaseModel):
    name: str
    env: str
    via: str
    remote_host: str = "127.0.0.1"
    remote_port: int = Field(ge=1, le=65535)
    local: int = Field(ge=1, le=65535)
    open_url: str | None = None


class TunnelPatch(BaseModel):
    env: str | None = None
    via: str | None = None
    remote_host: str | None = None
    remote_port: int | None = Field(default=None, ge=1, le=65535)
    local: int | None = Field(default=None, ge=1, le=65535)
    open_url: str | None = None


def _check_tunnel(state: WebState, name: str, env: str, via: str, local: int) -> None:
    if not sshedit.ALIAS_RE.match(name):
        raise HTTPException(400, f"{name!r} is not a usable tunnel name")
    try:
        state.context.config.environment(env)
    except ConfigError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        host = state.context.inventory.host(via)
    except KeyError:
        raise HTTPException(400, f"unknown host {via!r} to forward through") from None
    if host.env != env:
        raise HTTPException(400, f"{via} is in {host.env}, not {env}")
    clash = next((t for t in state.context.config.tunnels.values() if t.local == local and t.name != name), None)
    if clash:
        raise HTTPException(400, f"local port {local} is already used by the {clash.name} tunnel")


@router.post("/tunnels")
async def add_tunnel(request: Request, body: TunnelBody):
    """Define a port forward. It rides on the connection to `via`, which is usually a bastion."""
    state = _state(request)
    if body.name in state.context.config.tunnels:
        raise HTTPException(400, f"a tunnel called {body.name!r} already exists")
    if not sshedit.ADDRESS_RE.match(body.remote_host):
        raise HTTPException(400, f"{body.remote_host!r} is not a valid address on the far side")
    _check_tunnel(state, body.name, body.env, body.via, body.local)

    state.context.config.tunnels[body.name] = TunnelCfg(
        name=body.name,
        env=body.env,
        via=body.via,
        remote=f"{body.remote_host}:{body.remote_port}",
        local=body.local,
        open_url=(body.open_url or None),
    )
    _save(state)
    await state.reload(reason=f"tunnel {body.name} added")
    state.context.audit.append("settings.tunnel.add", env=body.env, host=body.via, target=body.name, status="ok")
    return {"ok": True, "tunnel": body.name}


@router.patch("/tunnels/{name}")
async def update_tunnel(request: Request, name: str, body: TunnelPatch):
    state = _state(request)
    existing = state.context.config.tunnels.get(name)
    if existing is None:
        raise HTTPException(404, f"unknown tunnel {name!r}")
    if state.tunnels.status_of(name).wanted:  # open, or dropped and about to be reopened
        raise HTTPException(400, f"{name} is open — close it before changing it")

    env = body.env or existing.env
    via = body.via or existing.via
    local = body.local or existing.local
    remote_host = body.remote_host or existing.remote_host
    remote_port = body.remote_port or existing.remote_port
    if not sshedit.ADDRESS_RE.match(remote_host):
        raise HTTPException(400, f"{remote_host!r} is not a valid address on the far side")
    _check_tunnel(state, name, env, via, local)

    state.context.config.tunnels[name] = TunnelCfg(
        name=name,
        env=env,
        via=via,
        remote=f"{remote_host}:{remote_port}",
        local=local,
        open_url=(body.open_url if body.open_url is not None else existing.open_url) or None,
    )
    _save(state)
    await state.reload(reason=f"tunnel {name} changed")
    state.context.audit.append("settings.tunnel.update", env=env, host=via, target=name, status="ok")
    return {"ok": True, "tunnel": name}


@router.delete("/tunnels/{name}")
async def remove_tunnel(request: Request, name: str):
    state = _state(request)
    if name not in state.context.config.tunnels:
        raise HTTPException(404, f"unknown tunnel {name!r}")
    if state.tunnels.status_of(name).wanted:
        raise HTTPException(400, f"{name} is open — close it before removing it")
    # Read the environment before dropping it: an entry with no environment cannot be filtered,
    # and every other tunnel action records one.
    removed = state.context.config.tunnels.pop(name)
    _save(state)
    await state.reload(reason=f"tunnel {name} removed")
    state.context.audit.append("settings.tunnel.remove", env=removed.env, host=removed.via, target=name, status="ok")
    return {"ok": True}


# --------------------------------------------------------------------- refresh & reload


class RefreshBody(BaseModel):
    fast_s: int = Field(ge=0, le=3600)
    stats_s: int | None = Field(default=None, ge=0, le=86400)
    stale_after_s: int | None = Field(default=None, ge=5, le=86400)
    collector_timeout_s: int | None = Field(default=None, ge=5, le=300)
    connect_timeout_s: int | None = Field(default=None, ge=2, le=120)


class ThresholdsBody(BaseModel):
    disk_warn_pct: int | None = Field(default=None, ge=1, le=100)
    disk_crit_pct: int | None = Field(default=None, ge=1, le=100)
    mem_warn_pct: int | None = Field(default=None, ge=1, le=100)
    mem_crit_pct: int | None = Field(default=None, ge=1, le=100)
    load_per_cpu_warn: float | None = Field(default=None, gt=0, le=64)


@router.put("/refresh")
async def set_refresh(request: Request, body: RefreshBody):
    """Change how often the fleet is polled. Takes effect immediately, not after a restart."""
    state = _state(request)
    refresh = state.context.config.refresh
    refresh.fast_s = body.fast_s
    for field_name in ("stats_s", "stale_after_s", "collector_timeout_s", "connect_timeout_s"):
        value = getattr(body, field_name)
        if value is not None:
            setattr(refresh, field_name, value)
    _save(state)
    state._fingerprints = state._fingerprint()
    state.wake()  # cut the current wait short so the new interval applies now
    state.context.audit.append("settings.refresh", status="ok", note=f"every {refresh.fast_s}s")
    return {"ok": True, "refresh": {f: getattr(refresh, f) for f in ("fast_s", "stats_s", "stale_after_s", "collector_timeout_s", "connect_timeout_s")}}


@router.put("/thresholds")
async def set_thresholds(request: Request, body: ThresholdsBody):
    state = _state(request)
    thresholds = state.context.config.thresholds
    for field_name, value in body.model_dump().items():
        if value is not None:
            setattr(thresholds, field_name, value)
    if thresholds.disk_warn_pct > thresholds.disk_crit_pct or thresholds.mem_warn_pct > thresholds.mem_crit_pct:
        raise HTTPException(400, "the warning level has to be below the critical level")
    _save(state)
    state._fingerprints = state._fingerprint()
    state.context.audit.append("settings.thresholds", status="ok")
    return {"ok": True, "thresholds": body.model_dump(exclude_none=True)}


# -------------------------------------------------------------------------------- sounds


SOUND_NAMES = {"none", "chime", "bell", "pulse", "soft", "alarm"}


class SoundsBody(BaseModel):
    enabled: bool | None = None
    crit: str | None = None
    warn: str | None = None
    done: str | None = None
    desktop: bool | None = None


@router.put("/sounds")
async def set_sounds(request: Request, body: SoundsBody):
    """Which sound the browser plays for which event. Stored in the config so it follows you."""
    state = _state(request)
    config = state.context.config
    for name in ("crit", "warn", "done"):
        value = getattr(body, name)
        if value is not None and value not in SOUND_NAMES:
            raise HTTPException(400, f"unknown sound {value!r} (one of {', '.join(sorted(SOUND_NAMES))})")
    ui = dict(config.ui or D.DEFAULT_CONFIG["ui"])
    sounds = {**D.DEFAULT_CONFIG["ui"]["sounds"], **(ui.get("sounds") or {})}
    for name in ("enabled", "crit", "warn", "done", "desktop"):
        value = getattr(body, name)
        if value is not None:
            sounds[name] = value
    ui["sounds"] = sounds
    config.ui = ui
    _save(state)
    state.context.audit.append("settings.sounds", status="ok", note=", ".join(f"{k}={v}" for k, v in sounds.items()))
    return {"ok": True, "sounds": sounds}


@router.post("/reload")
async def reload(request: Request):
    """Re-read config.yaml and the SSH config files now."""
    state = _state(request)
    summary = await state.reload(reason="reloaded from the settings page")
    if not summary.get("ok"):
        raise HTTPException(400, summary.get("error", "reload failed"))
    return summary
