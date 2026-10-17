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
import difflib
import hashlib
import json
import re
import socket
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from . import __version__
from .config import loader as config_loader
from .config.schema import CONFIG_VERSION, Config, ConfigError
from .fsutil import atomic_write, real_path
from .inventory import Inventory
from .inventory.sshconfig import holds_key_material, looks_like_key
from .inventory.sshedit import _split, aliases_in, replace_file
from .inventory.sshmerge import merge_text
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


class StalePlan(BundleError):
    """The files changed between the dry run and the import; the plan has to be looked at again."""


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
    # Environments are named exactly (the CLI also takes a short label); projects narrow them.
    wanted_envs = [
        e
        for e in config.environments
        if (not environments or e.name in environments or (e.short in environments and not any(o.name == e.short for o in config.environments)))
        and (not projects or e.project in projects)
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

    # Chosen environments carry their own projects and nothing else; a project chosen alone carries
    # itself even with no environment in it.
    wanted_projects = {e.project for e in wanted_envs} | (set() if environments else set(projects or []))
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


# ---------------------------------------------------------------------------- reading one in


def check(bundle: Any) -> dict[str, Any]:
    """Refuse anything that is not a bundle this sshfleet can read, before looking further."""
    if not isinstance(bundle, dict):
        raise BundleError("not an sshfleet bundle")
    if bundle.get("kind") == "sshfleet-cloud-bundle":
        raise BundleError("this is a cloud push, not an export; make an export with `sshfleet config export`")
    if bundle.get("kind") != KIND:
        raise BundleError("not an sshfleet bundle")
    if bundle.get("version") != VERSION:
        raise BundleError(f"bundle version {bundle.get('version')!r}; this sshfleet reads version {VERSION}")
    manifest, fleet, ssh = bundle.get("manifest"), bundle.get("fleet"), bundle.get("ssh")
    if not isinstance(manifest, dict) or not isinstance(fleet, dict) or not isinstance(ssh, dict):
        raise BundleError("the bundle is missing its manifest, its fleet or its SSH files")
    config_version = manifest.get("config_version")
    if not isinstance(config_version, int) or config_version < 1:
        raise BundleError("the bundle does not say which config version it holds")
    if config_version > CONFIG_VERSION:
        raise BundleError(f"the bundle was made by a newer sshfleet (config version {config_version}); update sshfleet to import it")
    for name, text in ssh.items():
        if not isinstance(name, str) or not FILE_NAME_RE.match(name):
            raise BundleError(f"{name!r} is not a file name this machine will write")
        if not isinstance(text, str) or len(text.encode()) > MAX_FILE_BYTES:
            raise BundleError(f"{name}: not text, or larger than {MAX_FILE_BYTES // 1024} KB")
        if holds_key_material(text.encode()):
            raise BundleError(f"{name} contains key material; a bundle never carries keys")
    for env in fleet.get("environments") or []:
        if not isinstance(env, dict) or not isinstance(env.get("name"), str) or not env.get("name"):
            raise BundleError("an environment in the bundle has no name")
        if env.get("ssh_file") not in ssh:
            raise BundleError(f"environment {env['name']!r} names an SSH file the bundle does not carry")
    if "local" in bundle and not isinstance(bundle["local"], dict):
        raise BundleError("the bundle's local section is not a mapping")
    return bundle


def parse(raw: bytes) -> dict[str, Any]:
    """A single-file bundle, read and checked. Nothing is written until the whole of it passes."""
    if len(raw) > MAX_BUNDLE_BYTES:
        raise BundleError(f"the bundle is larger than {MAX_BUNDLE_BYTES // (1024 * 1024)} MB")
    try:
        bundle = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundleError(f"not a bundle: {exc}") from exc
    return check(bundle)


def load(source: Path) -> dict[str, Any]:
    """A bundle from a file or a folder."""
    source = source.expanduser()
    if source.is_dir():
        return check(read_dir(source))
    try:
        with source.open("rb") as handle:
            raw = handle.read(MAX_BUNDLE_BYTES + 1)
    except OSError as exc:
        raise BundleError(f"cannot read {source}: {exc.strerror or exc}") from exc
    return parse(raw)


# ---------------------------------------------------------------------------- planning an import


_MISSING: Any = object()
#: Sections of config.yaml merged key by key.
_SECTIONS = ("probes", "frontends", "refresh", "concurrency", "thresholds", "ssh", "files", "logs")
_NAMED = {"projects", "environments", "hosts", "tunnels"}


@dataclass(slots=True)
class Change:
    kind: str  # project, environment, stanza, host, tunnel, setting
    name: str
    action: str  # add, update, same, kept-local, conflict, remove, skipped
    note: str = ""


@dataclass(slots=True)
class ImportPlan:
    mode: str
    prefer: str
    changes: list[Change]
    warnings: list[str]
    #: Environments brought in without a key: each needs `sshfleet env key`, or a key path in Settings.
    needs_key: list[str]
    #: Settings that arrived as <redacted>: whatever this machine had is kept; these have nothing.
    needs_value: list[str]
    config_after: dict[str, Any]
    config_changed: bool
    #: Path → the full new text of each SSH file that changes or is created.
    files_after: dict[str, str]
    #: Path → content hash each written file had when the plan was made (None: did not exist).
    base: dict[str, str | None]
    diffs: dict[str, str] = field(default_factory=dict)
    digest: str = ""

    @property
    def changed(self) -> bool:
        return self.config_changed or bool(self.files_after)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "prefer": self.prefer,
            "changed": self.changed,
            "config_changed": self.config_changed,
            "changes": [asdict(c) for c in self.changes],
            "warnings": self.warnings,
            "needs_key": self.needs_key,
            "needs_value": self.needs_value,
            "files": [{"path": path, "new": self.base.get(path) is None, "diff": self.diffs.get(path, "")} for path in self.files_after],
            "digest": self.digest,
        }


def _sha(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def _inside(path: Path, root: Path) -> bool:
    try:
        return real_path(path).is_relative_to(real_path(root))
    except OSError:
        return False


class _Merger:
    """One import's bookkeeping: what it decided, and why, as it walks the bundle."""

    def __init__(self, prefer: str) -> None:
        self.bundle_wins = prefer == "bundle"
        self.changes: list[Change] = []
        self.needs_value: list[str] = []

    def resolve(self, theirs: Any, mine: Any, where: str) -> Any:
        """The bundle's value with every <redacted> in it replaced by what is here, or dropped."""
        if theirs == REDACTED:
            if mine is _MISSING or mine in (None, ""):
                self.needs_value.append(where)
                return _MISSING
            return mine
        if isinstance(theirs, dict):
            out = {}
            for key, value in theirs.items():
                resolved = self.resolve(value, mine.get(key, _MISSING) if isinstance(mine, dict) else _MISSING, f"{where}.{key}")
                if resolved is not _MISSING:
                    out[key] = resolved
            return out
        return theirs

    def entry(
        self, mine: dict[str, Any], theirs: dict[str, Any], where: str, *, local_only: frozenset[str] = frozenset(), default: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any], str, list[str]]:
        """``mine`` with ``theirs`` merged in key by key. A key that differs keeps the local value
        unless the bundle is preferred — or unless the local value is only the default, which
        nobody chose, so the bundle's is taken."""
        merged = dict(mine)
        updated = False
        differing: list[str] = []
        for key, raw in theirs.items():
            if key in local_only:
                continue
            value = self.resolve(raw, mine.get(key, _MISSING), f"{where}.{key}")
            if value is _MISSING:
                continue
            if key not in mine:
                merged[key] = value
                updated = True
            elif mine[key] != value:
                if self.bundle_wins or (default is not None and mine[key] == default.get(key, _MISSING)):
                    merged[key] = value
                    updated = True
                else:
                    differing.append(key)
        action = "update" if updated else ("kept-local" if differing else "same")
        return merged, action, differing

    def note(self, kind: str, name: str, action: str, differing: list[str] | None = None, note: str = "") -> None:
        if differing and not note:
            note = "differs here, kept: " + ", ".join(differing)
        self.changes.append(Change(kind, name, action, note))


def plan_import(
    bundle: dict[str, Any],
    *,
    config: Config,
    paths: Paths,
    mode: str = "merge",
    prefer: str = "local",
    allow_commands: bool = False,
    with_local: bool = False,
) -> ImportPlan:
    """Everything an import would change, worked out and checked, with nothing written.

    ``merge`` adds and updates and never removes; ``replace`` makes the fleet the bundle's,
    unregistering environments it does not carry (their files stay where they are). Either way
    this machine keeps its own key paths, file paths and pauses, and a <redacted> value never
    replaces a real one.
    """
    from . import envsetup
    from .config import migrate

    check(bundle)
    if mode not in {"merge", "replace"}:
        raise BundleError(f"unknown mode {mode!r}: merge or replace")
    if prefer not in {"local", "bundle"}:
        raise BundleError(f"unknown preference {prefer!r}: local or bundle")
    warnings: list[str] = []
    fleet = copy.deepcopy(bundle["fleet"])
    texts: dict[str, str] = bundle["ssh"]
    local_part: dict[str, Any] = (bundle.get("local") or {}) if with_local else {}
    if with_local and "local" not in bundle:
        warnings.append("the bundle carries no machine paths (it was exported without with_local)")

    for name, text in texts.items():
        refused = ssh_lines_refused(text)
        if refused and not allow_commands:
            raise BundleError(
                f"{name} has lines that would run on this machine ({'; '.join(refused[:3])}). "
                "Import with allow_commands only if you trust whoever made the bundle."
            )
        if refused:
            warnings.append(f"{name}: {len(refused)} line(s) that run a program are brought in")

    config_version = bundle["manifest"]["config_version"]
    if config_version < CONFIG_VERSION:
        envs = fleet.get("environments") or []
        raw = {**fleet, "version": config_version, "environments": [{**e, "ssh_config_path": f"/bundle/{e['ssh_file']}"} for e in envs]}
        aliases = {e["name"]: [p for p in aliases_in(texts[e["ssh_file"]]) if not any(c in p for c in "*?!")] for e in envs}
        try:
            upgraded = migrate.upgrade_mapping(raw, aliases)
        except (ValueError, TypeError, KeyError) as exc:
            raise BundleError(f"the bundle's settings (config version {config_version}) could not be upgraded: {exc}") from exc
        upgraded.pop("version", None)
        for env in upgraded.get("environments") or []:
            env["ssh_file"] = Path(str(env.pop("ssh_config_path"))).name
        fleet = upgraded
        warnings.append(f"the bundle holds config version {config_version}; it was upgraded on the way in")

    merger = _Merger(prefer)
    current = config_loader.to_dict(config)
    defaults = config_loader.to_dict(Config())
    after = copy.deepcopy(current)
    after.setdefault("projects", [])
    after.setdefault("environments", [])
    files_after: dict[str, str] = {}
    needs_key: list[str] = []
    skipped_aliases: set[str] = set()
    home_ssh = envsetup.ssh_dir()

    # Projects first: a new environment may be filed under one the bundle brings.
    mine_projects = {p["name"]: p for p in after["projects"]}
    for theirs in fleet.get("projects") or []:
        theirs = {k: v for k, v in theirs.items() if k != "paused"}
        name = str(theirs.get("name"))
        if name not in mine_projects:
            project = merger.resolve(theirs, _MISSING, f"projects.{name}")
            if (local_part.get("projects") or {}).get(name, {}).get("paused"):
                project["paused"] = True
            after["projects"].append(project)
            mine_projects[name] = project
            merger.note("project", name, "add")
        else:
            merged, action, differing = merger.entry(mine_projects[name], theirs, f"projects.{name}")
            after["projects"][after["projects"].index(mine_projects[name])] = merged
            mine_projects[name] = merged
            merger.note("project", name, action, differing)

    mine_envs = {e["name"]: e for e in after["environments"]}
    labels_here = {str(e.get("short") or e["name"][:4]) for e in after["environments"]} | set(mine_envs)
    incoming_envs = fleet.get("environments") or []
    for theirs in incoming_envs:
        name = theirs["name"]
        text = texts[theirs["ssh_file"]]
        shared = {k: v for k, v in theirs.items() if k not in {"ssh_file", "ssh_config_path", "key_path", "paused"}}
        if name in mine_envs:
            mine = mine_envs[name]
            merged, action, differing = merger.entry(mine, shared, f"environments.{name}")
            after["environments"][after["environments"].index(mine)] = merged
            merger.note("environment", name, action, differing)
            path = Path(str(mine["ssh_config_path"])).expanduser()
            if looks_like_key(path):
                raise BundleError(f"environment {name}: {path} looks like a key; it is not written to")
            local_text = path.read_text() if path.exists() else ""
            new_text, stanza_changes = merge_text(local_text, text, mode=mode, prefer=prefer)
            for change in stanza_changes:
                if change.action != "same":
                    merger.note("stanza", f"{name}: Host {change.label}", change.action, note=change.note)
            if new_text != local_text:
                files_after[str(path)] = new_text
            continue

        short = str(shared.get("short") or name[:4])
        if short in labels_here or name in labels_here:
            merger.note("environment", name, "conflict", note=f"its name or short label {short!r} is already used by an environment here")
            skipped_aliases.update(aliases_in(text))
            continue
        project = str(shared.get("project") or (after["projects"][0]["name"] if after["projects"] else "default"))
        if project not in mine_projects:
            mine_projects[project] = {"name": project, "color": "gray"}
            after["projects"].append(mine_projects[project])
            merger.note("project", project, "add", note=f"for environment {name}")
        local_env = (local_part.get("environments") or {}).get(name, {})
        target = envsetup.default_env_file(project, short)
        wanted = local_env.get("ssh_config_path")
        if wanted:
            candidate = Path(str(wanted)).expanduser()
            if _inside(candidate, home_ssh) and not looks_like_key(candidate):
                target = candidate
            else:
                warnings.append(f"{name}: the bundle's file path {wanted} is outside {home_ssh}; using {target} instead")
        if target.exists():
            if target.read_text() != text:
                merger.note("environment", name, "conflict", note=f"a different file is already at {target}")
                skipped_aliases.update(aliases_in(text))
                continue
        else:
            files_after[str(target)] = text
        entry = {**merger.resolve(shared, _MISSING, f"environments.{name}"), "ssh_config_path": str(target)}
        entry["project"] = project
        if local_env.get("key_path"):
            entry["key_path"] = str(local_env["key_path"])
            key = envsetup.check_key(Path(entry["key_path"]))
            if not key.ok:
                warnings.append(f"{name}: key {key.path}: {'; '.join(key.problems)}")
        else:
            needs_key.append(name)
        if local_env.get("paused"):
            entry["paused"] = True
        after["environments"].append(entry)
        mine_envs[name] = entry
        labels_here |= {short, name}
        merger.note("environment", name, "add", note=f"its hosts go to {target}")

    if mode == "replace":
        carried = {e["name"] for e in incoming_envs}
        for env in list(after["environments"]):
            if env["name"] not in carried:
                after["environments"].remove(env)
                merger.note("environment", env["name"], "remove", note=f"unregistered; {env['ssh_config_path']} stays where it is")

    # Host settings, by alias. Pauses are this operator's, whatever either side says.
    mine_hosts: dict[str, Any] = after.setdefault("hosts", {})
    incoming_hosts = fleet.get("hosts") or {}
    paused_here = (local_part.get("hosts") or {}) if with_local else {}
    for alias, theirs in incoming_hosts.items():
        if alias in skipped_aliases or not isinstance(theirs, dict):
            continue
        theirs = {k: v for k, v in theirs.items() if k != "paused"}
        if alias not in mine_hosts:
            mine_hosts[alias] = merger.resolve(theirs, _MISSING, f"hosts.{alias}")
            merger.note("host", alias, "add")
        else:
            mine = mine_hosts[alias]
            merged, action, differing = merger.entry(mine, theirs, f"hosts.{alias}", local_only=frozenset({"paused", "ignored_containers"}))
            ours, extra = list(mine.get("ignored_containers") or []), list(theirs.get("ignored_containers") or [])
            union = ours + [p for p in extra if p not in ours]
            if union != ours:
                merged["ignored_containers"] = union
                action = "update"
            mine_hosts[alias] = merged
            merger.note("host", alias, action, differing)
        if paused_here.get(alias, {}).get("paused"):
            mine_hosts[alias]["paused"] = True
    if mode == "replace":
        for alias in [a for a in mine_hosts if a not in incoming_hosts]:
            paused = mine_hosts[alias].get("paused")
            if paused:
                mine_hosts[alias] = {"paused": True}
            else:
                del mine_hosts[alias]
            merger.note("host", alias, "remove", note="its settings here are dropped" + ("; its pause is kept" if paused else ""))

    # Tunnels, by name. A local port another tunnel already uses here is never taken over.
    mine_tunnels: dict[str, Any] = after.setdefault("tunnels", {})
    env_names = {e["name"] for e in after["environments"]}
    for name, theirs in (fleet.get("tunnels") or {}).items():
        if not isinstance(theirs, dict):
            continue
        if theirs.get("env") not in env_names:
            merger.note("tunnel", name, "skipped", note=f"its environment {theirs.get('env')!r} is not here")
            continue
        theirs = dict(theirs)
        if theirs.get("open") and not re.match(r"^https?://", str(theirs["open"])):
            warnings.append(f"tunnel {name}: its link {theirs['open']!r} is not http(s) and was left out")
            theirs.pop("open")
        if name in mine_tunnels:
            merged, action, differing = merger.entry(mine_tunnels[name], theirs, f"tunnels.{name}")
            mine_tunnels[name] = merged
            merger.note("tunnel", name, action, differing)
            continue
        port = theirs.get("local")
        holder = next((other for other, t in mine_tunnels.items() if t.get("local") == port), None)
        if holder:
            merger.note("tunnel", name, "conflict", note=f"local port {port} is already used by tunnel {holder}")
            continue
        mine_tunnels[name] = merger.resolve(theirs, _MISSING, f"tunnels.{name}")
        merger.note("tunnel", name, "add")

    # Fleet-wide settings, key by key; this machine's own settings never come from the bundle.
    for section in _SECTIONS:
        theirs = fleet.get(section)
        if not isinstance(theirs, dict):
            continue
        theirs = {k: v for k, v in theirs.items() if k not in _LOCAL_SETTINGS.get(section, ())}
        merged, action, differing = merger.entry(after.get(section) or {}, theirs, section, default=defaults.get(section))
        after[section] = merged
        if action != "same":
            merger.note("setting", section, action, differing)
    known = set(_SECTIONS) | _NAMED | {"version", "ui"}
    for key, theirs in fleet.items():
        if key in known:
            continue
        value = merger.resolve(theirs, after.get(key, _MISSING), key)
        if value is _MISSING:
            continue
        if key not in after:
            after[key] = value
            merger.note("setting", key, "add")
        elif after[key] != value:
            if merger.bundle_wins:
                after[key] = value
                merger.note("setting", key, "update")
            else:
                merger.note("setting", key, "kept-local", note="differs here, kept")

    # This machine's own paths and preferences, only when asked and only where they are safe.
    if with_local:
        download_dir = (local_part.get("files") or {}).get("download_dir")
        if download_dir:
            candidate = Path(str(download_dir)).expanduser()
            if _inside(candidate, Path.home()) and not _inside(candidate, home_ssh):
                after.setdefault("files", {})["download_dir"] = str(download_dir)
            else:
                warnings.append(f"downloads folder {download_dir} is outside your home folder or inside ~/.ssh; left as it is")
        display_tz = (local_part.get("logs") or {}).get("display_tz")
        if display_tz:
            after.setdefault("logs", {})["display_tz"] = display_tz
        if isinstance(local_part.get("ui"), dict):
            after["ui"] = local_part["ui"]
        existing = {e["name"] for e in current.get("environments") or []}
        for name, entry in (local_part.get("environments") or {}).items():
            # An environment that was already here and had no key takes the bundle's key path.
            if name in existing and name in mine_envs and entry.get("key_path") and not mine_envs[name].get("key_path"):
                mine_envs[name]["key_path"] = str(entry["key_path"])
                merger.note("environment", name, "update", note="its key path from the bundle")

    after["version"] = CONFIG_VERSION
    try:
        parsed = config_loader.parse_mapping(after, source="the imported settings")
    except ConfigError as exc:
        raise BundleError(f"the settings after the import would not load: {exc}") from exc
    final = config_loader.to_dict(parsed)
    config_changed = final != current

    base = {path: _sha(Path(path)) for path in files_after}
    base[str(paths.config_file)] = _sha(paths.config_file)
    diffs: dict[str, str] = {}
    for name, text in files_after.items():
        before = Path(name).read_text() if Path(name).exists() else ""
        diffs[name] = "".join(
            difflib.unified_diff(before.splitlines(keepends=True), text.splitlines(keepends=True), fromfile=f"a/{Path(name).name}", tofile=f"b/{Path(name).name}")
        )
    digest = hashlib.sha256(
        json.dumps({"config": final, "files": files_after, "base": base}, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]
    order = {"conflict": 0, "remove": 1, "add": 2, "update": 3, "kept-local": 4, "skipped": 5, "same": 6}
    merger.changes.sort(key=lambda c: order.get(c.action, 9))
    return ImportPlan(
        mode=mode,
        prefer=prefer,
        changes=merger.changes,
        warnings=warnings,
        needs_key=needs_key,
        needs_value=merger.needs_value,
        config_after=final,
        config_changed=config_changed,
        files_after=files_after,
        base=base,
        diffs=diffs,
        digest=digest,
    )


def apply_import(plan: ImportPlan, paths: Paths) -> list[str]:
    """Write what the plan says: SSH files first (each existing one keeps a backup), config.yaml
    last. Refused, before writing anything, if any of those files changed since the plan."""
    for path, expected in plan.base.items():
        if _sha(Path(path)) != expected:
            raise StalePlan(f"{Path(path).name} changed since the import was planned; look at it again before importing")
    written: list[str] = []
    for path, text in plan.files_after.items():
        target = Path(path)
        if target.exists():
            replace_file(target, text.encode(), paths.ssh_backups)
        else:
            atomic_write(target, text, mode=0o600)
        written.append(path)
    if plan.config_changed:
        config_loader.save(config_loader.parse_mapping(plan.config_after), paths.config_file)
        written.append(str(paths.config_file))
    return written
