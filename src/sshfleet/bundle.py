"""Export and import: a whole sshfleet setup as one file, or as a folder to keep in git.

A bundle carries what describes the fleet — projects, environments, what is watched on each host,
tunnels, thresholds, log settings — and each environment's SSH config text. It does not carry
what describes this machine: key paths and the keys themselves, where files live, interface
preferences, and which hosts this operator has paused. Those travel only when asked for
(``with_local``), in a section of their own, for moving a setup between one's own machines.

Two guarantees, each held by a test:

- **Nothing secret leaves.** ``IdentityFile`` and similar lines are taken out of the SSH text,
  credential-looking settings are replaced with ``<redacted>``, and the finished bundle is scanned
  for key material, cloud keys, tokens and every value in ``secrets.env``. A hit refuses the
  export rather than silently cutting it: the operator should know where the secret was.
- **Nothing runs on whoever imports it.** SSH lines that run a program or reach into the
  importing machine (``ProxyCommand``, ``Match … exec``, ``Include``, ``IdentityAgent`` …) are
  refused both ways unless explicitly allowed.

Single-file form::

    {"kind": "sshfleet-bundle", "version": 2,
     "manifest": {sshfleet_version, config_version, created_at, created_on, scope, with_local},
     "fleet": {...config, environments name an ssh_file instead of a path...},
     "ssh": {"<file name>": "<text>"},
     "local": {...}}                                    # only with_local

Folder form: ``manifest.json``, ``fleet.yaml``, ``ssh/<file name>``, and ``local.yaml`` only
with ``with_local`` — so a diff in git reads as a diff of the fleet.
"""

from __future__ import annotations

import copy
import json
import re
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from . import __version__
from .config import loader as config_loader
from .config.schema import CONFIG_VERSION, Config
from .inventory import Inventory
from .inventory.sshconfig import holds_key_material, looks_like_key
from .inventory.sshedit import _split
from .paths import Paths

KIND = "sshfleet-bundle"
VERSION = 2
MAX_BUNDLE_BYTES = 5 * 1024 * 1024
MAX_FILE_BYTES = 1024 * 1024

#: An SSH config file is written under ``config.d`` by this name: no directories, no dot files.
FILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: SSH keywords that name files on the exporting machine. Taken out of the text that is shared;
#: ``render_config`` puts each environment's own key back from its ``key_path``.
LOCAL_SSH_KEYWORDS = frozenset({"identityfile", "certificatefile", "userknownhostsfile"})

#: SSH keywords that run a program, or read from the machine the file ends up on. Carried in a
#: bundle, they would run whatever the bundle's author wrote on everyone who imports it.
COMMAND_KEYWORDS = frozenset(
    {"proxycommand", "localcommand", "knownhostscommand", "identityagent", "pkcs11provider", "securitykeyprovider", "include"}
)

#: A setting whose name says it is a credential. Anchored to the end of the name, so a host alias
#: such as ``tst-auth`` or a key like ``token_ttl`` is not caught.
_CREDENTIAL_KEY = re.compile(
    r"(^|[_\-.])(password|passwd|pass|pwd|secret|token|api[_\-]?key|apikey|access[_\-]?key|private[_\-]?key|credentials?|auth)$",
    re.IGNORECASE,
)
_URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-z][a-z0-9+.\-]*://)[^/@\s:]+:[^/@\s]+@", re.IGNORECASE)
REDACTED = "<redacted>"

#: What a secret looks like inside the finished bundle, whatever field it hid in.
_SECRET_SHAPES = {
    "private key": re.compile(rb"PRIVATE KEY-----|PuTTY-User-Key-File|-----BEGIN OPENSSH"),
    "AWS access key": re.compile(rb"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),
    "GitHub token": re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "GitLab token": re.compile(rb"\bglpat-[A-Za-z0-9_\-]{20,}\b"),
    "Slack token": re.compile(rb"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b"),
}


class BundleError(Exception):
    """The bundle could not be built, or will not be applied. Nothing was written."""


# ---------------------------------------------------------------------------- reading SSH text


def ssh_lines_refused(text: str) -> list[str]:
    """Each line that runs a program or reaches into the importing machine, as ``line N: …``."""
    out: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        keyword, value = _split(line)
        risky = keyword in COMMAND_KEYWORDS
        risky = risky or (keyword == "match" and "exec" in value.lower().split())
        risky = risky or (keyword == "permitlocalcommand" and value.lower() == "yes")
        if risky:
            out.append(f"line {number}: {line.strip()}")
    return out


def strip_local_lines(text: str) -> str:
    """The SSH text without the lines that name files on this machine (keys, known_hosts)."""
    lines = text.splitlines(keepends=True)
    return "".join(line for line in lines if _split(line)[0] not in LOCAL_SSH_KEYWORDS)


# ---------------------------------------------------------------------------- redaction


def redact(value: Any, *, path: str = "") -> tuple[Any, list[str]]:
    """``value`` with credential-looking settings replaced, and the dotted names of what was."""
    found: list[str] = []
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            here = f"{path}.{key}" if path else str(key)
            # The names under hosts and tunnels are host aliases and tunnel names, not settings.
            named_collection = path in {"hosts", "tunnels"}
            if not named_collection and isinstance(key, str) and _CREDENTIAL_KEY.search(key) and item not in (None, "", [], {}):
                out[key] = REDACTED
                found.append(here)
                continue
            out[key], more = redact(item, path=here)
            found.extend(more)
        return out, found
    if isinstance(value, list):
        items = []
        for index, item in enumerate(value):
            cleaned, more = redact(item, path=f"{path}[{index}]")
            items.append(cleaned)
            found.extend(more)
        return items, found
    if isinstance(value, str) and _URL_CREDENTIALS.search(value):
        found.append(path)
        return _URL_CREDENTIALS.sub(r"\g<scheme>", value), found
    return value, found


def secret_values(paths: Paths) -> list[str]:
    """The values in ``secrets.env`` long enough to recognise: none of them may leave in a bundle."""
    try:
        text = paths.secrets_file.read_text()
    except OSError:
        return []
    values = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        value = line.partition("=")[2].strip().strip("'\"")
        if len(value) >= 8:
            values.append(value)
    return values


def assert_clean(raw: bytes, secrets: list[str]) -> None:
    """Refuse a finished bundle that contains anything shaped like a secret."""
    for label, shape in _SECRET_SHAPES.items():
        if shape.search(raw):
            raise BundleError(f"the export would contain what looks like a {label}; nothing was exported. Remove it from the settings or the SSH file it is in.")
    for value in secrets:
        if value.encode() in raw:
            raise BundleError("the export would contain a value from secrets.env; nothing was exported. Find where it was copied into the settings and take it out.")


# ---------------------------------------------------------------------------- building


#: config.yaml settings that describe this machine rather than the fleet.
_LOCAL_SETTINGS = {"files": ("download_dir",), "ssh": ("control_dir", "include_user_config"), "logs": ("display_tz",)}


def build(
    config: Config,
    inventory: Inventory,
    paths: Paths,
    *,
    projects: list[str] | None = None,
    environments: list[str] | None = None,
    with_local: bool = False,
    allow_commands: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    """The bundle for this setup (or the part of it chosen), and what is worth telling about it."""
    warnings: list[str] = []
    data = config_loader.to_dict(config)
    wanted_envs = [
        e
        for e in config.environments
        if (not environments or e.name in environments or e.short in environments) and (not projects or e.project in projects)
    ]
    if (projects or environments) and not wanted_envs:
        raise BundleError("no environment matches what was asked to export")
    env_names = {e.name for e in wanted_envs}
    scoped = bool(projects or environments)

    local: dict[str, Any] = {"environments": {}, "projects": {}, "hosts": {}}
    ssh: dict[str, str] = {}
    origins: dict[str, Path] = {}
    fleet_envs: list[dict[str, Any]] = []
    for env_cfg, env_dict in zip(config.environments, data.get("environments") or [], strict=True):
        if env_cfg.name not in env_names:
            continue
        path = env_cfg.ssh_config_path.expanduser()
        name = path.name
        if not FILE_NAME_RE.match(name):
            raise BundleError(f"{env_cfg.name}: {name!r} cannot be carried in a bundle (a plain file name is needed)")
        if name in origins and origins[name] != path:
            raise BundleError(f"{env_cfg.name}: two environments use different files called {name!r}")
        if looks_like_key(path):
            raise BundleError(f"{env_cfg.name}: {name} looks like a key, not an SSH config; it is not read")
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise BundleError(f"{env_cfg.name}: cannot read {path}: {exc.strerror or exc}") from exc
        if len(raw) > MAX_FILE_BYTES:
            raise BundleError(f"{env_cfg.name}: {name} is larger than {MAX_FILE_BYTES // 1024} KB")
        if holds_key_material(raw):
            raise BundleError(f"{env_cfg.name}: {name} contains key material; nothing was exported")
        text = strip_local_lines(raw.decode(errors="replace"))
        refused = ssh_lines_refused(text)
        if refused and not allow_commands:
            raise BundleError(
                f"{env_cfg.name}: {name} has lines that would run on whoever imports it ({'; '.join(refused[:3])}). "
                "Export with allow_commands only if every importer should run them."
            )
        if refused:
            warnings.append(f"{env_cfg.name}: {name} carries {len(refused)} line(s) that run a program on import")
        origins[name] = path
        ssh[name] = text
        shared = {k: v for k, v in env_dict.items() if k not in {"key_path", "ssh_config_path", "paused"}}
        shared["ssh_file"] = name
        fleet_envs.append(shared)
        local["environments"][env_cfg.name] = {
            "ssh_config_path": str(env_cfg.ssh_config_path),
            **({"key_path": str(env_cfg.key_path)} if env_cfg.key_path else {}),
            **({"paused": True} if env_cfg.paused else {}),
        }
    data["environments"] = fleet_envs

    wanted_projects = {e.project for e in wanted_envs} | set(projects or [])
    fleet_projects = []
    for project in data.get("projects") or []:
        if scoped and project["name"] not in wanted_projects:
            continue
        if project.pop("paused", False):
            local["projects"][project["name"]] = {"paused": True}
        fleet_projects.append(project)
    data["projects"] = fleet_projects

    if scoped:
        aliases = {h.alias for h in inventory.hosts() if h.env in env_names}
        data["hosts"] = {alias: entry for alias, entry in (data.get("hosts") or {}).items() if alias in aliases}
        data["tunnels"] = {name: t for name, t in (data.get("tunnels") or {}).items() if t.get("env") in env_names}
    for alias, entry in (data.get("hosts") or {}).items():
        if entry.pop("paused", False):
            local["hosts"][alias] = {"paused": True}

    for section, keys in _LOCAL_SETTINGS.items():
        for key in keys:
            value = (data.get(section) or {}).pop(key, None)
            if value is not None:
                local.setdefault(section, {})[key] = value
    local["ui"] = data.pop("ui", None)
    data.pop("version", None)

    fleet, redacted = redact(data)
    for name in redacted:
        warnings.append(f"{name} looks like a credential and was left out (it is exported as {REDACTED})")

    bundle: dict[str, Any] = {
        "kind": KIND,
        "version": VERSION,
        "manifest": {
            "sshfleet_version": __version__,
            "config_version": CONFIG_VERSION,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "created_on": socket.gethostname().split(".")[0],
            "scope": {"projects": sorted(wanted_projects) if scoped else [], "environments": sorted(env_names) if scoped else []},
            "with_local": with_local,
        },
        "fleet": fleet,
        "ssh": ssh,
    }
    if with_local:
        bundle["local"] = local
    assert_clean(to_json(bundle), secret_values(paths))
    return bundle, warnings


# ---------------------------------------------------------------------------- serialising


def to_json(bundle: dict[str, Any]) -> bytes:
    return json.dumps(bundle, indent=1, ensure_ascii=False).encode() + b"\n"


def write_dir(bundle: dict[str, Any], directory: Path) -> list[Path]:
    """Write the folder form. Files a previous export left in ``ssh/`` that this one does not carry
    are removed, so git sees an environment leaving; nothing else in the folder is touched."""
    if directory.exists() and not directory.is_dir():
        raise BundleError(f"{directory} exists and is not a folder")
    manifest_path = directory / "manifest.json"
    if directory.is_dir() and any(directory.iterdir()) and not manifest_path.exists():
        raise BundleError(f"{directory} is not empty and holds no bundle; choose an empty folder")
    ssh_dir = directory / "ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    def put(path: Path, text: str, mode: int = 0o644) -> None:
        path.write_text(text)
        path.chmod(mode)
        written.append(path)

    put(manifest_path, json.dumps({"kind": bundle["kind"], "version": bundle["version"], **bundle["manifest"]}, indent=1) + "\n")
    put(directory / "fleet.yaml", yaml.safe_dump(bundle["fleet"], sort_keys=False, allow_unicode=True, width=100))
    for name, text in bundle["ssh"].items():
        put(ssh_dir / name, text)
    for stale in ssh_dir.iterdir():
        if stale.is_file() and FILE_NAME_RE.match(stale.name) and stale.name not in bundle["ssh"]:
            stale.unlink()
    local_path = directory / "local.yaml"
    if "local" in bundle:
        put(local_path, yaml.safe_dump(bundle["local"], sort_keys=False, allow_unicode=True, width=100), mode=0o600)
    elif local_path.exists():
        local_path.unlink()
    return written


def read_dir(directory: Path) -> dict[str, Any]:
    """The folder form back as one bundle. Symlinks and anything oversized are refused."""
    def read(path: Path) -> str:
        if path.is_symlink():
            raise BundleError(f"{path.name} is a symlink; a bundle folder holds plain files only")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise BundleError(f"{path.name} is larger than {MAX_FILE_BYTES // 1024} KB")
        return path.read_text()

    try:
        manifest = json.loads(read(directory / "manifest.json"))
        fleet = yaml.safe_load(read(directory / "fleet.yaml")) or {}
        ssh: dict[str, str] = {}
        ssh_dir = directory / "ssh"
        if ssh_dir.is_symlink():
            raise BundleError("ssh/ is a symlink; a bundle folder holds plain files only")
        if ssh_dir.is_dir():
            for path in sorted(ssh_dir.iterdir()):
                if not FILE_NAME_RE.match(path.name):
                    raise BundleError(f"ssh/{path.name}: not a file name this machine will write")
                ssh[path.name] = read(path)
        local_path = directory / "local.yaml"
        local = yaml.safe_load(read(local_path)) if local_path.exists() else None
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise BundleError(f"not a bundle folder: {exc}") from exc
    if not isinstance(manifest, dict):
        raise BundleError("manifest.json is not a bundle manifest")
    bundle: dict[str, Any] = {
        "kind": manifest.pop("kind", None),
        "version": manifest.pop("version", None),
        "manifest": manifest,
        "fleet": fleet,
        "ssh": ssh,
    }
    if local is not None:
        bundle["local"] = local
    return copy.deepcopy(bundle)
