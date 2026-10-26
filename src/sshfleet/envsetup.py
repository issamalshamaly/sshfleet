"""Register an environment from an SSH config file.

Shared by the CLI (``sshfleet env add``) and the web interface's Settings. The key file is only
ever ``stat``-ed — its permissions are checked, its content is never read.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, ConfigError, EnvironmentCfg, ProjectCfg
from .inventory import HostStanza, common_prefix, parse_ssh_config, summarize


@dataclass(slots=True)
class KeyCheck:
    path: Path
    exists: bool
    is_file: bool
    owned: bool
    mode: int
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def fix(self) -> str:
        return f"chmod 600 {self.path}"


def check_key(path: Path) -> KeyCheck:
    """Validate an identity file by metadata only — never opened, never read."""
    path = path.expanduser()
    problems: list[str] = []
    try:
        info = os.stat(path, follow_symlinks=True)
    except OSError as exc:
        return KeyCheck(path, False, False, False, 0, [f"cannot stat: {exc.strerror or exc}"])

    is_file = stat.S_ISREG(info.st_mode)
    mode = info.st_mode & 0o777
    owned = info.st_uid == os.getuid()
    if not is_file:
        problems.append("not a regular file")
    if not owned:
        problems.append("not owned by you")
    if mode & 0o077:
        problems.append(f"mode {mode:04o} is too open (ssh requires 0600 or 0400)")
    return KeyCheck(path, True, is_file, owned, mode, problems)


@dataclass(slots=True)
class EnvPreview:
    stanzas: list[HostStanza]
    summary: dict[str, object]
    prefix: str | None
    suggested_name: str
    suggested_short: str
    suggested_color: str
    bastions: list[str]
    key_candidates: list[str]

    @property
    def aliases(self) -> list[str]:
        return [s.alias for s in self.stanzas]


def ssh_dir() -> Path:
    """The user's ssh directory (``SSHFLEET_SSH_DIR`` overrides it, for tests and odd setups)."""
    return Path(os.environ.get("SSHFLEET_SSH_DIR") or "~/.ssh").expanduser()


def slug(name: str) -> str:
    text = re.sub(r"[^a-z0-9._-]+", "-", name.strip().lower()).strip("-.")
    return text or "environment"


def env_stem(project: str, short: str) -> str:
    """``<project>_<short>``: the name of an environment's own file and the start of every host
    alias added to it, so neither collides with another project's."""
    return f"{slug(project)}_{slug(short)}"


def default_env_file(project: str, short: str) -> Path:
    """Where a new environment's hosts go when the operator has no file yet:
    ``config.d/<project>_<short>``, so one project's files sort together."""
    return ssh_dir() / "config.d" / env_stem(project, short)


def environment_in(config: Config, project: str, name: str) -> EnvironmentCfg | None:
    """The environment called ``name`` in ``project``: a name is unique there and nowhere else."""
    return next((e for e in config.environments if e.project == project and e.name == name), None)


def new_env_id(config: Config, project: str, name: str) -> str:
    """The id a new environment is given: its own name while no other environment has that as
    its id, else ``<project>-<name>`` — so the second project with a ``staging`` gets
    ``mudeer-staging`` — numbered on the rare chance that is taken too."""
    return free_env_id({e.id for e in config.environments}, project, name)


def free_env_id(taken: set[str], project: str, name: str) -> str:
    """:func:`new_env_id` against a set of ids already in use."""
    if name not in taken:
        return name
    base = f"{slug(project)}-{slug(name)}"
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}-{n}", n + 1
    return candidate


def host_prefix(env: EnvironmentCfg) -> str:
    return env_stem(env.project, env.short) + "_"


def host_alias(env: EnvironmentCfg, name: str) -> str:
    """A new host's alias, ``<project>_<short>_<name>``. A name that already carries the prefix
    (a whole alias pasted in) is kept as it is rather than prefixed twice."""
    name = name.strip()
    prefix = host_prefix(env)
    return name if name.startswith(prefix) else prefix + name


def create_env_file(path: Path, name: str) -> Path:
    """An empty, private ssh config file for an environment the Hosts tab will fill in."""
    path = path.expanduser()
    if path.exists():
        raise ConfigError(f"{path} already exists")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(f"# {name}: hosts managed with sshfleet. Plain OpenSSH syntax; edit freely.\n")
    os.chmod(path, 0o600)
    return path


def include_line() -> str:
    directory = ssh_dir()
    shown = "~/.ssh" if directory == (Path.home() / ".ssh") else str(directory)
    return f"Include {shown}/config.d/*"


def include_status(user_config: Path | None = None) -> dict[str, object]:
    """Whether the user's own ``~/.ssh/config`` reads ``config.d/*``.

    sshfleet itself never needs this (it drives ssh with its own generated config), but plain
    ``ssh <alias>`` outside the tool only sees a file under ``config.d`` through that line.
    """
    config = user_config or ssh_dir() / "config"
    target = ssh_dir() / "config.d"
    covered = False
    exists = config.exists()
    if exists:
        for raw in config.read_text(errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            keyword, _, value = re.split(r"[\s=]", line, maxsplit=1)[0], "", line.split(None, 1)[1] if len(line.split(None, 1)) > 1 else ""
            if keyword.lower() != "include":
                continue
            for pattern in value.split():
                candidate = Path(pattern.strip("\"'")).expanduser()
                if not candidate.is_absolute():
                    candidate = ssh_dir() / candidate
                if candidate.parent == target and candidate.name == "*":
                    covered = True
    return {"config": str(config), "exists": exists, "includes_config_d": covered, "line": include_line()}


def add_include_line(backups_dir: Path, user_config: Path | None = None) -> Path | None:
    """Put the Include line at the top of the user's ssh config (first match wins in OpenSSH).

    Returns the backup written, or None when the line was already there and nothing changed.
    """
    from .inventory import sshedit

    config = user_config or ssh_dir() / "config"
    if include_status(config)["includes_config_d"]:
        return None
    lines = sshedit.read_lines(config)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    return sshedit.write_lines(config, [include_line() + "\n", "\n", *lines], backups_dir)


def preview(ssh_config_path: Path, config: Config | None = None, *, allow_empty: bool = False) -> EnvPreview:
    """Parse a candidate SSH config file and suggest how to register it.

    ``allow_empty`` accepts a file with no hosts, or no file at all, for an environment that
    will be filled in from the Hosts tab.
    """
    path = ssh_config_path.expanduser()
    if not path.exists() and allow_empty:
        stanzas: list[HostStanza] = []
    else:
        stanzas = parse_ssh_config(path)
    if not stanzas and not allow_empty:
        raise ConfigError(f"{path}: no usable Host entries found")

    aliases = [s.alias for s in stanzas]
    prefix = common_prefix(aliases)
    stem = (prefix or path.stem).rstrip("-")
    name, short, color = suggest_identity(stem or path.stem)

    jumps: dict[str, int] = {}
    for stanza in stanzas:
        if stanza.proxy_jump:
            jumps[stanza.proxy_jump] = jumps.get(stanza.proxy_jump, 0) + 1
    bastions = [alias for alias, _ in sorted(jumps.items(), key=lambda kv: -kv[1])]

    return EnvPreview(
        stanzas=stanzas,
        summary=summarize(stanzas),
        prefix=prefix,
        suggested_name=name,
        suggested_short=short,
        suggested_color=color,
        bastions=bastions,
        key_candidates=sorted({f for s in stanzas for f in s.identity_files}),
    )


def suggest_identity(stem: str) -> tuple[str, str, str]:
    """Guess (name, short label, colour) for an environment from its alias prefix or file name.

    Plain substring rules: ``acme-prod`` and ``eu1prod`` are production, ``eu1st`` and
    ``staging`` are staging, and anything unrecognised keeps its own name.
    """
    text = stem.lower().strip("-_ ")
    words = re.split(r"[^a-z0-9]+", text)
    if "prod" in text:
        return ("production", "prod", "red")
    if "stag" in text or "st" in words or re.search(r"(?:^|[^a-z])st$", text):
        return ("staging", "st", "yellow")
    if "dev" in text:
        return ("development", "dev", "cyan")
    if "uat" in text:
        return ("uat", "uat", "grape")
    if "qa" in words or "test" in text:
        return ("testing", "test", "green")
    if "rc" in words:
        return ("rc", "rc", "blue")
    name = text or "default"
    return (name, name[:4], "cyan")


def add_environment(
    config: Config,
    *,
    name: str,
    ssh_config_path: Path,
    key_path: Path | None = None,
    short: str | None = None,
    color: str | None = None,
    bastions: list[str] | None = None,
    project: str | None = None,
    replace: bool = False,
    create_file: bool = False,
) -> EnvironmentCfg:
    """Add (or replace) an environment in the config object. Does not save.

    The project must already exist: an environment always belongs to one. Its name must be new
    to that project only; another project may well have a ``staging`` too.

    With ``create_file`` a path that does not exist yet is created empty, so hosts can be added
    afterwards; without it the file must exist, though it may still be empty. Nothing is created
    until everything else has been checked, so a refused registration leaves no file behind.
    """
    clean_project = (project or "").strip()
    if not clean_project:
        raise ConfigError(
            "an environment belongs to a project — create one first with `sshfleet project add <name>`"
        )
    if config.project(clean_project) is None:
        known = ", ".join(p.name for p in config.projects) or "none yet"
        raise ConfigError(f"unknown project {clean_project!r} (known: {known})")

    existing = environment_in(config, clean_project, name)
    if existing and not replace:
        raise ConfigError(f"project {clean_project!r} already has an environment called {name!r} (use --replace to overwrite)")

    path = ssh_config_path.expanduser()
    if not path.exists():
        if not create_file:
            raise FileNotFoundError(f"cannot read ssh config {path}: no such file")
        create_env_file(path, name)
    info = preview(path, config, allow_empty=True)

    env_cfg = EnvironmentCfg(
        name=name,
        ssh_config_path=ssh_config_path.expanduser(),
        short=short or info.suggested_short,
        color=color or info.suggested_color,
        key_path=key_path.expanduser() if key_path else None,
        bastions=bastions if bastions is not None else "auto",
        project=clean_project,
        # Replacing keeps the id, so its hosts, tunnels and history still point at it.
        id=existing.id if existing else new_env_id(config, clean_project, name),
    )
    if existing:
        config.environments[config.environments.index(existing)] = env_cfg
    else:
        config.environments.append(env_cfg)
    return env_cfg


def remove_environment(config: Config, ref: str) -> EnvironmentCfg:
    env = config.environment(ref)
    config.environments.remove(env)
    return env


# ------------------------------------------------------------------------------------ projects


def add_project(config: Config, *, name: str, color: str | None = None, description: str = "") -> ProjectCfg:
    """Register a project. It starts empty: environments are filed under it afterwards."""
    clean = name.strip()
    if not clean:
        raise ConfigError("a project needs a name")
    if config.project(clean):
        raise ConfigError(f"project {clean!r} already exists")
    project = ProjectCfg(name=clean, color=color or "gray", description=description.strip())
    config.projects.append(project)
    return project


def rename_project(config: Config, old: str, new: str) -> ProjectCfg:
    """Rename a project and re-point every environment filed under it, so none is orphaned."""
    project = config.project(old)
    if project is None:
        raise ConfigError(f"unknown project {old!r}")
    clean = new.strip()
    if not clean:
        raise ConfigError("a project needs a name")
    if clean != old and config.project(clean):
        raise ConfigError(f"project {clean!r} already exists")
    project.name = clean
    for env in config.environments:
        if env.project == old:
            env.project = clean
    return project


def remove_project(config: Config, name: str) -> None:
    """Forget a project. It must be empty: an environment is never left belonging to nothing."""
    project = config.project(name)
    if project is None:
        raise ConfigError(f"unknown project {name!r}")
    members = [e.name for e in config.environments_of(name)]
    if members:
        raise ConfigError(
            f"project {name!r} still holds {', '.join(members)} — "
            "move them to another project, or remove them, first"
        )
    config.projects.remove(project)
