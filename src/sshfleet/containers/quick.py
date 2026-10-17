"""Quick actions: the handful of things an operator does inside a container, one click away.

Every action is built here, from a fixed catalogue. The browser names an action by id and
nothing else — it never sends a command — so what can be run on a host is decided by this file
and stays inside :mod:`sshfleet.containers.runtime`'s allowed verbs: ``exec``, ``logs``,
``stats`` and ``inspect``. Nothing here stops, kills or removes anything.

Which actions are offered depends on what has already been discovered about the container and
its host: the image says whether there is a database client inside worth reaching for, the
host's role says whether this is a Node service or a gateway, its state says whether a shell
can be attached at all, and the host's health endpoint says whether there is something to
curl. The operator sees a short, relevant list rather than the whole catalogue.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable
from dataclasses import dataclass

from ..inventory import Host
from .names import NAME_RE
from .runtime import RemoteCommand

#: Drops the values of a multi-line ``"Env": [ … ]`` array from ``inspect`` output, as both
#: docker and podman print it; an empty or absent one is left alone. The environment is where
#: a deployment puts its database password and API keys, and this runs in a shared terminal.
HIDE_ENV = "awk " + shlex.quote(
    '/"Env": \\[$/ { sub(/\\[$/, "[ \\"(hidden by sshfleet: environment variables can hold secrets)\\" ],"); print; skip = 1; next }'
    ' skip && /^[[:space:]]*\\],?$/ { skip = 0; next }'
    " !skip"
)

#: Where a Node service's code sits in these images, and the directory the toolkit action
#: installs into when it is there.
NODE_APP_DIR = "/usr/src/app"

#: Prefer a real shell when the image has one; plenty of slim images only have ``sh``.
BEST_SHELL = "if command -v bash >/dev/null 2>&1; then exec bash; else exec sh; fi"


class UnknownAction(Exception):
    """The browser named an action that is not in the catalogue."""


@dataclass(frozen=True, slots=True)
class QuickAction:
    """One offer in the menu, and how to build it."""

    id: str
    title: str
    #: What it does, in the words the operator needs before pressing it.
    detail: str
    #: "shell" · "toolkit" · "watch" — how the menu groups them.
    group: str
    #: True when it changes something inside the container (installs, writes). Drawn apart.
    writes: bool
    #: The remote shell line, built for one host and one container.
    build: Callable[[Host, str], str]
    #: Offered only when this says so. Default: any running container.
    when: Callable[[Context], bool] = lambda ctx: ctx.running


@dataclass(frozen=True, slots=True)
class Context:
    """What is known about the container an action is being offered for."""

    host: Host
    name: str
    image: str = ""
    state: str = "running"

    @property
    def running(self) -> bool:
        return self.state == "running"

    def image_is(self, *needles: str) -> bool:
        """Does the image name mention any of these? ``mongo:7`` and ``bitnami/mongodb`` both do."""
        image = self.image.lower()
        return any(needle in image for needle in needles)


def _exec(host: Host, *args: str) -> str:
    """``docker exec …`` for this host, built through the verb allowlist and fully quoted."""
    return RemoteCommand(host, "exec", args).render()


def _shell_in(host: Host, name: str, *, root: bool = False) -> str:
    args = ["-it"]
    if root:
        args += ["-u", "0:0"]
    return _exec(host, *args, name, "sh", "-c", BEST_SHELL)


def _node_toolkit(host: Host, name: str) -> str:
    """Install ts-node and friends, then leave the operator at a prompt in the app directory.

    ``--no-save`` so ``package.json`` is not rewritten under the running service, and the
    install is reported rather than assumed: an image without npm says so instead of dropping
    the operator at a prompt that looks like the install worked. Run as uid 0: the image's own
    user commonly cannot write into ``node_modules`` or the npm cache, and that is what this
    install needs to do.
    """
    script = (
        f"if [ -d {NODE_APP_DIR} ]; then cd {NODE_APP_DIR}; fi\n"
        'if ! command -v npm >/dev/null 2>&1; then echo "npm is not in this container"; else\n'
        '  echo "installing ts-node, typescript and @types/node in $(pwd)…"\n'
        "  npm install --no-save ts-node typescript @types/node\n"
        "fi\n"
        f"{BEST_SHELL}"
    )
    return _exec(host, "-it", "-u", "0:0", name, "sh", "-c", script)


def _client(*candidates: str) -> Callable[[Host, str], str]:
    """A database client inside the container: the first one the image has, else a shell.

    Several candidates because the images rename their clients over time — ``mongosh``
    replaced ``mongo``, and an older image still only has the latter.
    """

    def build(host: Host, name: str) -> str:
        lines = [
            f"if command -v {command.split()[0]} >/dev/null 2>&1; then exec {command}; fi"
            for command in candidates
        ]
        lines.append(f'echo "{" or ".join(c.split()[0] for c in candidates)} is not in this container"')
        lines.append(BEST_SHELL)
        return _exec(host, "-it", name, "sh", "-c", "\n".join(lines))

    return build


def _health_curl(host: Host, name: str) -> str:
    """Ask this host's own health endpoint, from the host — not from inside the container.

    The one action that is not a container command at all: what it answers is whether the
    thing in front of the container is serving.
    """
    url = f"http://127.0.0.1:{host.health_port}{host.health_path or '/'}"
    return f"curl -sS -i -m 5 {shlex.quote(url)}"


#: The catalogue. Order is the order the menu shows them in.
CATALOGUE: tuple[QuickAction, ...] = (
    QuickAction(
        id="shell",
        title="Shell",
        detail="A prompt inside the container, as the user it runs as. bash when the image has it.",
        group="shell",
        writes=False,
        build=lambda host, name: _shell_in(host, name),
    ),
    QuickAction(
        id="shell-root",
        title="Root shell",
        detail="The same prompt as uid 0, for when the image's own user cannot read or write what you need.",
        group="shell",
        writes=False,
        build=lambda host, name: _shell_in(host, name, root=True),
    ),
    QuickAction(
        id="node-toolkit",
        title="Shell with ts-node",
        detail=f"As root, installs ts-node, typescript and @types/node into {NODE_APP_DIR} with --no-save, then leaves you at a prompt there.",
        group="toolkit",
        writes=True,
        build=_node_toolkit,
        when=lambda ctx: ctx.running
        and (ctx.host.role == "service" or ctx.image_is("node", "nest", "next", "strapi", "npm")),
    ),
    QuickAction(
        id="psql",
        title="psql",
        detail="The Postgres prompt inside the container, as the image's own superuser.",
        group="toolkit",
        writes=False,
        build=_client("psql -U ${POSTGRES_USER:-postgres}"),
        when=lambda ctx: ctx.running and ctx.image_is("postgres", "postgis", "timescale"),
    ),
    QuickAction(
        id="mongosh",
        title="mongosh",
        detail="The MongoDB shell inside the container; falls back to the older mongo client.",
        group="toolkit",
        writes=False,
        build=_client("mongosh", "mongo"),
        when=lambda ctx: ctx.running and ctx.image_is("mongo"),
    ),
    QuickAction(
        id="redis-cli",
        title="redis-cli",
        detail="The Redis prompt inside the container.",
        group="toolkit",
        writes=False,
        build=_client("redis-cli"),
        when=lambda ctx: ctx.running and ctx.image_is("redis", "valkey"),
    ),
    QuickAction(
        id="mysql",
        title="mysql",
        detail="The MySQL prompt as root; it asks for the password itself, and nothing here reads it.",
        group="toolkit",
        writes=False,
        build=_client("mysql -u root -p"),
        when=lambda ctx: ctx.running and ctx.image_is("mysql", "mariadb", "percona"),
    ),
    QuickAction(
        id="nginx-test",
        title="Test nginx config",
        detail="Runs nginx -t inside the container. It reads the configuration and changes nothing.",
        group="toolkit",
        writes=False,
        build=lambda host, name: _exec(host, "-it", name, "nginx", "-t"),
        when=lambda ctx: ctx.running and ctx.image_is("nginx", "openresty"),
    ),
    QuickAction(
        id="processes",
        title="What is running inside",
        detail="The container's own process table, once. Nothing is changed.",
        group="watch",
        writes=False,
        build=lambda host, name: _exec(
            host, "-it", name, "sh", "-c", "ps -ef 2>/dev/null || ps aux 2>/dev/null || ps"
        ),
    ),
    QuickAction(
        id="follow-logs",
        title="Follow the log",
        detail="The container's stdout as it is written, from the last 200 lines. Read-only; ctrl-c stops it.",
        group="watch",
        writes=False,
        build=lambda host, name: RemoteCommand(host, "logs", ("-f", "--tail", "200", name)).render(),
        # A stopped container still has a log worth reading, so this one is always offered.
        when=lambda ctx: True,
    ),
    QuickAction(
        id="stats",
        title="Live cpu and memory",
        detail="What this container is using right now, updating in place. Read-only.",
        group="watch",
        writes=False,
        build=lambda host, name: RemoteCommand(host, "stats", (name,)).render(),
    ),
    QuickAction(
        id="inspect",
        title="Inspect",
        detail="Everything the runtime knows about this container, as JSON, in a pager — except its "
        "environment variables, which often hold passwords and keys.",
        group="watch",
        writes=False,
        # A pager, so a long document can be read rather than scrolled past.
        build=lambda host, name: RemoteCommand(host, "inspect", (name,)).render() + f" | {HIDE_ENV} | ${{PAGER:-less -R}}",
        when=lambda ctx: True,
    ),
    QuickAction(
        id="health",
        title="Call the health endpoint",
        detail="curl against this host's configured health endpoint, from the host itself.",
        group="watch",
        writes=False,
        build=_health_curl,
        when=lambda ctx: bool(ctx.host.health_port) and ctx.host.watches(ctx.name),
    ),
)

BY_ID: dict[str, QuickAction] = {action.id: action for action in CATALOGUE}


def offers(host: Host, name: str, *, image: str = "", state: str = "running") -> list[dict]:
    """The actions worth showing for this container, each with the command it would run."""
    if not NAME_RE.match(name):
        return []
    context = Context(host=host, name=name, image=image, state=state)
    out: list[dict] = []
    for action in CATALOGUE:
        if not action.when(context):
            continue
        out.append(
            {
                "id": action.id,
                "title": action.title,
                "detail": action.detail,
                "group": action.group,
                "writes": action.writes,
                "command": action.build(host, name),
            }
        )
    return out


def command_for(host: Host, action_id: str, name: str) -> tuple[QuickAction, str]:
    """The action and the remote line to run, or :class:`UnknownAction`.

    Called from the terminal bridge, so it validates rather than trusts: an id outside the
    catalogue and a name the runtimes would not accept are both refused here.
    """
    action = BY_ID.get(action_id)
    if action is None:
        raise UnknownAction(f"unknown quick action {action_id!r}")
    if not NAME_RE.match(name):
        raise UnknownAction(f"{name!r} is not a container name")
    return action, action.build(host, name)
