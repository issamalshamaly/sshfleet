"""The demo's made-up fleet: Acme, an online shop, on a production and a staging environment.

Everything ``sshfleet demo`` shows comes from here: the hosts and their SSH files, the config,
what each host answers when it is checked, the logs it has written and the files it holds.
Nothing is read from this machine. Addresses are documentation ranges (RFC 5737) and names are
``example.com``.

Logs are made up on demand and the same each time they are asked for: a line belongs to a slot
of time and its text comes from a hash of that slot, so two searches over the same window agree,
and a window that moves on finds new lines at its end.

Standard library only: the fake ``ssh`` imports this module once per call.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

SCENARIOS = ("healthy", "degraded", "bastion_down")
#: The scenarios ``--story`` walks through, and for how many seconds each.
STORY = (("healthy", 75), ("degraded", 90), ("bastion_down", 45))
PROJECT = "acme"
DOWN_BASTION = "prod-bastion"
DOWN_ADDRESS = "203.0.113.10"


@dataclass(frozen=True)
class Box:
    """A container, and how it talks."""

    name: str
    image: str
    voice: str
    ports: str = ""
    network: str = "bridge"
    #: How long it has been up, in hours, when nothing has happened to it.
    up_hours: int = 72


@dataclass(frozen=True)
class Machine:
    alias: str
    env: str
    address: str
    user: str = "ubuntu"
    via: str | None = None
    runtime: str = "docker"
    boxes: tuple[Box, ...] = ()
    cpus: int = 2
    mem_gb: int = 4
    mem_pct: int = 52
    disk_pct: int = 41
    load: float = 0.3
    listening: tuple[int, ...] = (22,)
    health: tuple[int, str] | None = None
    #: The record under ``hosts:`` in config.yaml.
    record: dict = field(default_factory=dict)
    sites: tuple[str, ...] = ()
    frontends: tuple[str, ...] = ()


FLEET: tuple[Machine, ...] = (
    Machine("prod-bastion", "production", DOWN_ADDRESS, runtime="none", cpus=1, mem_gb=1, mem_pct=31, disk_pct=22,
            load=0.02, record={"label": "bastion", "role": "ops"}),
    Machine("prod-web", "production", "10.0.1.20", via="prod-bastion", runtime="none", listening=(22, 80, 443),
            sites=("shop.example.com", "admin.example.com"), frontends=("storefront", "admin"),
            record={"label": "web", "role": "frontend", "web_root": "/var/www"}),
    Machine("prod-api", "production", "10.0.1.21", via="prod-bastion", listening=(22, 3000), health=(3000, "/health"),
            boxes=(Box("api", "ghcr.io/acme/api:1.42.0", "api", "0.0.0.0:3000->3000/tcp"),),
            record={"main": True, "label": "api", "role": "service", "workloads": "api",
                    "health": {"port": 3000, "path": "/health"}, "log_format": "pino_json"}),
    Machine("prod-orders", "production", "10.0.1.22", via="prod-bastion", listening=(22, 3100), health=(3100, "/health"),
            boxes=(Box("orders", "ghcr.io/acme/orders:2.7.3", "orders", "0.0.0.0:3100->3100/tcp"),
                   Box("orders-worker", "ghcr.io/acme/orders:2.7.3", "worker")),
            record={"main": True, "label": "orders", "role": "service", "workloads": "orders*",
                    "health": {"port": 3100, "path": "/health"}, "log_format": "pino_pretty"}),
    Machine("prod-payments", "production", "10.0.1.23", user="deploy", via="prod-bastion", runtime="podman",
            listening=(22, 8080), health=(8080, "/healthz"),
            boxes=(Box("payments", "ghcr.io/acme/payments:0.19.1", "payments", "0.0.0.0:8080->8080/tcp"),),
            record={"main": True, "label": "payments", "role": "service", "runtime": "podman", "sudo": False,
                    "health": {"port": 8080, "path": "/healthz"}, "log_format": "zap_console"}),
    Machine("prod-search", "production", "10.0.1.24", via="prod-bastion", mem_gb=8, mem_pct=71, listening=(22, 9200),
            boxes=(Box("search", "opensearchproject/opensearch:2.17.1", "search", "0.0.0.0:9200->9200/tcp"),),
            record={"label": "search", "role": "service"}),
    Machine("prod-db", "production", "10.0.1.25", via="prod-bastion", mem_gb=8, mem_pct=64, disk_pct=78,
            listening=(22, 5432), boxes=(Box("postgres", "postgres:16.4", "postgres", "0.0.0.0:5432->5432/tcp", up_hours=900),),
            record={"label": "db", "role": "database"}),
    Machine("prod-jobs", "production", "10.0.1.26", via="prod-bastion", load=0.1,
            boxes=(Box("scheduler", "ghcr.io/acme/scheduler:1.3.0", "scheduler"),),
            record={"label": "jobs", "role": "service"}),
    Machine("prod-metrics", "production", "10.0.1.27", via="prod-bastion", listening=(22, 3000, 9090), mem_pct=58,
            boxes=(Box("prometheus", "prom/prometheus:v2.54.1", "prometheus", "0.0.0.0:9090->9090/tcp"),
                   Box("grafana", "grafana/grafana:11.2.0", "grafana", "0.0.0.0:3000->3000/tcp")),
            record={"label": "metrics", "role": "ops"}),
    Machine("stg-bastion", "staging", "203.0.113.20", runtime="none", cpus=1, mem_gb=1, mem_pct=28, disk_pct=19,
            load=0.01, record={"label": "bastion", "role": "ops"}),
    Machine("stg-web", "staging", "10.0.2.20", via="stg-bastion", runtime="none", listening=(22, 80, 443),
            sites=("staging.shop.example.com",), frontends=("storefront",),
            record={"label": "web", "role": "frontend", "web_root": "/var/www"}),
    Machine("stg-api", "staging", "10.0.2.21", via="stg-bastion", listening=(22, 3000), health=(3000, "/health"),
            boxes=(Box("api", "ghcr.io/acme/api:main", "api", "0.0.0.0:3000->3000/tcp", up_hours=5),),
            record={"main": True, "label": "api", "role": "service", "workloads": "api",
                    "health": {"port": 3000, "path": "/health"}, "log_format": "pino_json"}),
)
BY_ALIAS = {machine.alias: machine for machine in FLEET}
ENVIRONMENTS = (
    {"name": "production", "short": "prod", "color": "red", "file": "acme-production"},
    {"name": "staging", "short": "stg", "color": "yellow", "file": "acme-staging"},
)

#: What goes wrong in ``degraded``, host by host.
TROUBLE: dict[str, dict[str, Any]] = {
    "prod-api": {"load": 3.6},
    "prod-orders": {"exited": {"orders-worker": (1, 240)}},
    "prod-payments": {"restarting": {"payments": 6}, "probe": "503"},
    "prod-search": {"mem_pct": 95},
    "prod-db": {"disk_pct": 93},
}


# ------------------------------------------------------------------------------- config


def ssh_config(env: str) -> str:
    """An environment's SSH file, as an operator would keep it."""
    lines = [f"# Acme {env}: a made-up fleet for `sshfleet demo`. Nothing here is a real server.\n"]
    for machine in FLEET:
        if machine.env != env:
            continue
        lines += [f"\nHost {machine.alias}\n", f"    HostName {machine.address}\n", f"    User {machine.user}\n"]
        if machine.via:
            lines.append(f"    ProxyJump {machine.via}\n")
    return "".join(lines)


# ------------------------------------------------------------------------------ helpers


def _hash(*parts: object) -> int:
    return int(hashlib.blake2b(":".join(map(str, parts)).encode(), digest_size=8).hexdigest(), 16)


def _compact(value: object) -> str:
    """JSON the way pino writes it, with no spaces."""
    return json.dumps(value, separators=(",", ":"))


def _iso(ts: float, digits: int = 3) -> str:
    moment = datetime.fromtimestamp(ts, UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond:06d}"[:digits] + "Z"


def _docker_ts(ts: float, nudge: int = 0) -> str:
    """docker ``logs -t`` prefix: nanoseconds, UTC."""
    moment = datetime.fromtimestamp(ts, UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond:06d}{nudge:03d}" + "Z"


def _ago(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} seconds"
    if seconds < 3600:
        return f"{seconds // 60} minutes"
    if seconds < 86400 * 2:
        return f"{seconds // 3600} hours"
    return f"{seconds // 86400} days"


def bastion_is_down(machine: Machine, scenario: str) -> bool:
    return scenario == "bastion_down" and DOWN_BASTION in (machine.alias, machine.via)


# ---------------------------------------------------------------------- the checks


@dataclass
class Live:
    """What happened in this demo since it started: containers restarted and removed."""

    restarted: dict[str, dict[str, float]] = field(default_factory=dict)
    removed: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> Live:
        return cls(restarted=dict(data.get("restarted") or {}), removed=dict(data.get("removed") or {}))


def box_state(machine: Machine, box: Box, scenario: str, live: Live, now: float) -> tuple[str, str, int, int]:
    """``(state, status, exit_code, restart_count)`` of a container right now."""
    trouble = TROUBLE.get(machine.alias, {}) if scenario == "degraded" else {}
    restarted_at = live.restarted.get(machine.alias, {}).get(box.name)
    restarts = trouble.get("restarting", {}).get(box.name, 0)
    if restarted_at is not None:
        return "running", f"Up {_ago(now - restarted_at)}", 0, restarts
    if box.name in trouble.get("exited", {}):
        code, ago = trouble["exited"][box.name]
        return "exited", f"Exited ({code}) {_ago(ago)} ago", code, restarts
    if box.name in trouble.get("restarting", {}):
        return "restarting", "Restarting (1) 12 seconds ago", 1, restarts
    return "running", f"Up {_ago(box.up_hours * 3600)}", 0, 0


def collect(machine: Machine, scenario: str, live: Live, now: float | None = None) -> dict:
    """What the collector script prints for this host: see collector/scripts/collect.sh."""
    now = time.time() if now is None else now
    trouble = TROUBLE.get(machine.alias, {}) if scenario == "degraded" else {}
    wobble = math.sin(now / 47 + _hash(machine.alias) % 7)
    load = max(trouble.get("load", machine.load) * (1 + 0.15 * wobble), 0.0)
    mem_total = machine.mem_gb * 1_000_000_000
    mem_pct = min(trouble.get("mem_pct", machine.mem_pct + 2 * wobble), 99)
    disk_pct = trouble.get("disk_pct", machine.disk_pct)

    containers, inspect = [], []
    for box in machine.boxes:
        if box.name in live.removed.get(machine.alias, []):
            continue
        state, status, code, restarts = box_state(machine, box, scenario, live, now)
        created = datetime.fromtimestamp(now - box.up_hours * 3600 - 600, UTC)
        started = now - box.up_hours * 3600
        containers.append({
            "ID": f"{_hash(machine.alias, box.name):012x}"[:12],
            "Names": box.name,
            "Image": box.image,
            "Networks": box.network,
            "Ports": box.ports,
            "State": state,
            "Status": status,
            "CreatedAt": created.strftime("%Y-%m-%d %H:%M:%S +0000 UTC"),
        })
        inspect.append("\t".join([
            f"/{box.name}", box.image, f"sha256:{_hash(box.image):016x}", created.strftime("%Y-%m-%dT%H:%M:%SZ"),
            _iso(started), str(code), str(restarts), box.network,
            f"/var/lib/docker/containers/{_hash(box.name):016x}/{_hash(box.name):016x}-json.log",
        ]))
    if machine.alias == "prod-jobs":
        # A job runner leaves finished one-shot containers behind; they are noted, not alarmed.
        finished = datetime.fromtimestamp(now - 6 * 3600, UTC)
        containers.append({
            "ID": "4f1c0de2a9b1", "Names": f"report-{finished:%Y%m%d}-0400", "Image": "ghcr.io/acme/reports:1.3.0",
            "Networks": "bridge", "Ports": "", "State": "exited", "Status": "Exited (0) 6 hours ago",
            "CreatedAt": finished.strftime("%Y-%m-%d %H:%M:%S +0000 UTC"),
        })

    probes = []
    if machine.health and machine.boxes:
        # The first container serves the health port: nothing answers while it is down, and a
        # restart is what clears a failing probe.
        port, path = machine.health
        serving = machine.boxes[0]
        state = box_state(machine, serving, scenario, live, now)[0]
        if serving.name in live.removed.get(machine.alias, []) or state != "running":
            answer = "000"
        elif serving.name in live.restarted.get(machine.alias, {}):
            answer = "200"
        else:
            answer = trouble.get("probe", "200")
        probes.append({"port": port, "path": path, "code": answer})

    sites = "".join(f"{name}\t443\t1\t/etc/nginx/sites-enabled/{name.split('.')[0]}\n" for name in machine.sites)
    return {
        "v": 1,
        "host": "ip-" + machine.address.replace(".", "-"),
        "now": int(now),
        "uptime_s": 41 * 86400 + int(now) % 86400,
        "kernel": "6.8.0-1036-aws",
        "load": [round(load, 2), round(load * 0.9, 2), round(load * 0.8, 2)],
        "cpus": machine.cpus,
        "mem_total": mem_total,
        "mem_avail": int(mem_total * (100 - mem_pct) / 100),
        "disks": [{"mount": "/", "size": 80, "used": round(80 * disk_pct / 100), "pct": disk_pct}],
        "runtime": machine.runtime,
        "sudo_ok": 1,
        "runtime_error": "",
        "containers": containers,
        "inspect": "\n".join(inspect),
        "listening": list(machine.listening),
        "probes": probes,
        "frontends": [{"name": name, "mtime": int(now) - 3 * 86400 - _hash(name) % 86400} for name in machine.frontends],
        "nginx": "active" if machine.sites else "absent",
        "sites": sites,
    }


def find_box(machine: Machine, words: list[str]) -> Box | None:
    """The container a script or command names, among the words it is made of."""
    named = set(words)
    for box in machine.boxes:
        if box.name in named:
            return box
    return None


# --------------------------------------------------------------------------- logs


def _api(h: int, ts: float, bad: bool) -> list[str]:
    if h % (9 if bad else 31) == 0:
        return [_compact({"level": 50, "time": _iso(ts), "service": "api", "route": "/api/checkout",
                            "msg": "upstream timeout calling payments",
                            "err": {"type": "TimeoutError", "message": "timeout of 3000ms exceeded"}})]
    if h % 43 == 1:
        return [_compact({"level": 40, "time": _iso(ts), "service": "api", "msg": "slow query",
                            "query": "products.search", "ms": 1200 + h % 900})]
    routes = (("GET", "/api/products", 200), ("GET", "/api/products/{id}", 200), ("POST", "/api/cart", 201),
              ("GET", "/api/cart", 200), ("POST", "/api/checkout", 201), ("GET", "/api/products/{id}", 404))
    method, path, status = routes[h % len(routes)]
    return [_compact({"level": 30, "time": _iso(ts), "service": "api", "req": {"method": method,
                        "url": path.replace("{id}", str(1000 + h % 900))}, "res": {"statusCode": status},
                        "responseTime": 4 + h % 90, "msg": "request completed"})]


def _pretty(ts: float, level: str, message: str) -> str:
    moment = datetime.fromtimestamp(ts, UTC)
    return f"[{moment:%Y-%m-%d %H:%M:%S}.{moment.microsecond // 1000:03d} +0000] {level}: {message}"


def _orders(h: int, ts: float, bad: bool) -> list[str]:
    order = f"A-{10000 + h % 89999}"
    if h % (13 if bad else 41) == 0:
        return [_pretty(ts, "ERROR", f"could not reserve stock for {order}"), f'    orderId: "{order}"',
                "    err: {", '      "type": "Error",', '      "message": "connect ECONNREFUSED 10.0.1.25:5432"', "    }"]
    if h % 7 == 0:
        return [_pretty(ts, "INFO", f"payment captured for {order}"), f"    amountCents: {500 + h % 30000}"]
    return [_pretty(ts, "INFO", f"order {order} created"), f"    items: {1 + h % 6}"]


def _worker(h: int, ts: float, bad: bool) -> list[str]:
    if bad and h % 17 == 0:
        return [_pretty(ts, "FATAL", "lost connection to the queue, exiting"), "    err: {",
                '      "message": "Connection closed: 320 (CONNECTION-FORCED) - broker forced connection closure"', "    }"]
    return [_pretty(ts, "INFO", f"sent confirmation email for A-{10000 + h % 89999}")]


def _payments(h: int, ts: float, bad: bool) -> list[str]:
    order = f"A-{10000 + h % 89999}"
    if h % (5 if bad else 37) == 0:
        lines = [f'{_iso(ts)}\tERROR\tcharge/provider.go:88\tprovider timeout\t{{"order": "{order}", "after": "3s"}}']
        if bad and h % 3 == 0:
            lines += ["goroutine 41 [running]:", "acme/payments/charge.(*Client).Capture(...)",
                      "\t/src/charge/provider.go:88 +0x1c4"]
        return lines
    return [f'{_iso(ts)}\tINFO\tcharge/handler.go:51\tcharge captured\t{{"order": "{order}", "cents": {500 + h % 30000}}}']


def _search(h: int, ts: float, bad: bool) -> list[str]:
    stamp = datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%S,%f")[:-3]
    if bad and h % 6 == 0:
        return [f"[{stamp}][WARN ][o.o.m.j.JvmGcMonitorService] [search-1] [gc][{h % 9000}] overhead, "
                f"spent [{1 + h % 4}.{h % 10}s] collecting in the last [{2 + h % 4}s]"]
    return [f"[{stamp}][INFO ][o.o.i.IndexingMemoryController] [search-1] products: indexed {h % 400} documents"]


def _postgres(h: int, ts: float, bad: bool) -> list[str]:
    stamp = datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    if bad and h % 9 == 0:
        return [f"{stamp} UTC [{61 + h % 400}] WARNING:  database disk is 93% full",
                f"{stamp} UTC [{61 + h % 400}] HINT:  free space or grow the volume before writes start to fail"]
    if h % 29 == 0:
        return [f'{stamp} UTC [{61 + h % 400}] ERROR:  duplicate key value violates unique constraint "orders_pkey"',
                f"{stamp} UTC [{61 + h % 400}] DETAIL:  Key (id)=(A-{10000 + h % 89999}) already exists."]
    return [f"{stamp} UTC [61] LOG:  checkpoint complete: wrote {h % 900} buffers ({h % 30 / 10:.1f}%)"]


def _scheduler(h: int, ts: float, bad: bool) -> list[str]:
    return [f"{_iso(ts, 0)[:-1]}Z scheduler: job {('sync-stock', 'expire-carts', 'send-digest')[h % 3]} "
            f"finished in {1 + h % 50}s"]


def _prometheus(h: int, ts: float, bad: bool) -> list[str]:
    return [f'ts={_iso(ts)} caller=compact.go:519 level=info component=tsdb msg="write block" duration={h % 900}ms']


def _grafana(h: int, ts: float, bad: bool) -> list[str]:
    return [f'logger=context t={_iso(ts)} level=info msg="Request Completed" method=GET path=/api/dashboards '
            f"status=200 duration={h % 80}ms"]


VOICES: dict[str, tuple[Callable[[int, float, bool], list[str]], float]] = {
    # voice: (what it says, seconds between the slots it may speak in)
    "api": (_api, 4.0),
    "orders": (_orders, 9.0),
    "worker": (_worker, 25.0),
    "payments": (_payments, 7.0),
    "search": (_search, 30.0),
    "postgres": (_postgres, 20.0),
    "scheduler": (_scheduler, 60.0),
    "prometheus": (_prometheus, 45.0),
    "grafana": (_grafana, 15.0),
}
#: The furthest back a log reaches.
MEMORY_S = 24 * 3600


def _slots(key: str, since: float, until: float, step: float) -> Iterator[tuple[float, int]]:
    since = max(since, until - MEMORY_S)
    for slot in range(math.ceil(since / step), math.floor(until / step) + 1):
        h = _hash(key, slot)
        if h % 5 == 0:
            continue  # not every slot speaks: real logs are uneven
        ts = slot * step + (h % 997) / 997 * step * 0.9
        if since <= ts <= until:
            # What is said comes from other bits than whether anything is: a voice that speaks up
            # on ``h % 5 == 0`` would otherwise never be heard.
            yield ts, h >> 20


def _bad(machine: Machine, box: Box | None, scenario: str) -> bool:
    return scenario == "degraded" and machine.alias in TROUBLE


def container_log(machine: Machine, box: Box, scenario: str, since: float, until: float) -> list[str]:
    """``docker logs -t`` output between two times, oldest first."""
    speak, step = VOICES[box.voice]
    bad = _bad(machine, box, scenario)
    out: list[str] = []
    for ts, h in _slots(f"{machine.alias}/{box.name}", since, until, step):
        for nudge, text in enumerate(speak(h, ts, bad)):
            out.append(f"{_docker_ts(ts, nudge)} {text}")
    return out


def nginx_log(machine: Machine, kind: str, scenario: str, since: float, until: float) -> list[str]:
    bad = scenario == "degraded"
    out = []
    for ts, h in _slots(f"{machine.alias}/nginx-{kind}", since, until, 3.0 if kind == "access" else 40.0):
        moment = datetime.fromtimestamp(ts, UTC)
        if kind == "access":
            path = ("/", "/products", "/products/1042", "/cart", "/api/checkout", "/static/app.js")[h % 6]
            status = 504 if bad and path == "/api/checkout" and h % 3 == 0 else (200 if h % 23 else 404)
            out.append(f'198.51.100.{h % 250} - - [{moment:%d/%b/%Y:%H:%M:%S} +0000] "GET {path} HTTP/1.1" '
                       f'{status} {300 + h % 9000} "-" "Mozilla/5.0"')
        else:
            what = "upstream timed out (110: Connection timed out) while reading response header from upstream" \
                if bad and h % 2 == 0 else "client closed connection while waiting for request"
            out.append(f"{moment:%Y/%m/%d %H:%M:%S} [{'error' if 'timed out' in what else 'warn'}] 811#811: "
                       f"*{h % 900000} {what}, client: 198.51.100.{h % 250}, server: shop.example.com")
    return out


def journal(machine: Machine, scenario: str, since: float, until: float) -> list[str]:
    host = "ip-" + machine.address.replace(".", "-")
    out = []
    for ts, h in _slots(f"{machine.alias}/journal", since, until, 120.0):
        moment = datetime.fromtimestamp(ts, UTC)
        message = ("level=info msg=\"Container health status changed\"", "level=info msg=\"Pulled image\"",
                   "level=info msg=\"ignoring event\" topic=/tasks/delete")[h % 3]
        out.append(f"{moment:%Y-%m-%dT%H:%M:%S}+0000 {host} dockerd[812]: {message}")
    return out


# ---------------------------------------------------------------------------- files

#: A small file tree per host, the same everywhere: enough to browse, preview and be refused.
#: (kind, size, mode, name) per directory; names on the deny list are listed and refused.
TREE: dict[str, list[tuple[str, int, str, str]]] = {
    "/home/{user}": [("d", 4096, "700", ".ssh"), ("d", 4096, "755", "deploy"), ("f", 3771, "644", ".bashrc"),
                     ("f", 214, "600", ".env"), ("f", 912, "644", "notes.md"),
                     ("f", 18_400_000, "640", "backup-orders.sql.gz")],
    "/home/{user}/deploy": [("f", 1490, "644", "compose.yaml"), ("f", 388, "600", ".env.production"),
                            ("f", 640, "644", "README.md"), ("f", 2210, "755", "deploy.sh")],
    "/home/{user}/.ssh": [("f", 402, "600", "authorized_keys"), ("f", 1679, "600", "id_rsa")],
    "/var/log": [("d", 4096, "755", "nginx"), ("f", 812_000, "640", "syslog"), ("f", 96_000, "640", "auth.log")],
    "/var/log/nginx": [("f", 6_120_000, "640", "access.log"), ("f", 48_000, "640", "error.log")],
}
CONTENT = {
    "compose.yaml": "services:\n  api:\n    image: ghcr.io/acme/api:1.42.0\n    ports: [\"3000:3000\"]\n"
                    "    env_file: .env.production\n    restart: \"no\"\n",
    "README.md": "# Deploy\n\nJenkins runs `deploy.sh <tag>` here after every merge to main.\n",
    "deploy.sh": "#!/bin/sh\nset -e\ndocker pull \"ghcr.io/acme/api:$1\"\ndocker rm -f api || true\n"
                 "docker run -d --name api --env-file .env.production -p 3000:3000 \"ghcr.io/acme/api:$1\"\n",
    "notes.md": "- the payments provider times out under load; retries are on since 0.19\n"
                "- orders-worker needs the queue up before it starts\n",
    ".bashrc": "# ~/.bashrc\nalias ll='ls -alF'\n",
}


def _tree_key(path: str, machine: Machine) -> str | None:
    home = f"/home/{machine.user}"
    for key in TREE:
        if key.replace("/home/{user}", home) == path.rstrip("/"):
            return key
    return None


def listing(machine: Machine, path: str, now: float) -> str:
    """NUL-separated ``kind size mtime mode owner group name link`` records, as files/browse.py reads."""
    key = _tree_key(path, machine)
    entries = TREE[key] if key else [("f", 1200, "644", "README.md")]
    records = []
    for kind, size, mode, name in entries:
        owner = "root" if path.startswith("/var/log") else machine.user
        mtime = now - 3600 - _hash(path, name) % (30 * 86400)
        records.append(f"{kind}\t{size}\t{mtime:.1f}\t{mode}\t{owner}\t{owner}\t{name}\t")
    return "\0".join(records) + "\0"


def is_dir(machine: Machine, path: str) -> bool:
    return _tree_key(path, machine) is not None


def preview(path: str) -> str:
    name = path.rstrip("/").rsplit("/", 1)[-1]
    return CONTENT.get(name, f"This file is part of the sshfleet demo; {name} has no content here.\n")
