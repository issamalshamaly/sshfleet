"""The ``ssh`` that ``sshfleet demo`` runs instead of OpenSSH.

sshfleet calls it exactly as it would call ``ssh`` (through ``SSHFLEET_SSH_BIN``), and it answers
as a host would, from :mod:`sshfleet.demo.fleet`. It never connects anywhere and never reads a
file of this machine: a script sent to it is read to see what it asks for, never run. A log
search is answered the way the host's pipeline would answer it, ``grep -n`` marks and all, so
everything downstream of ssh is the real code.

Two files hold what changes while the demo runs: the scenario (``SSHFLEET_DEMO_SCENARIO``),
which ``--story`` rewrites, and the state (``SSHFLEET_DEMO_STATE``): open connections, and the
containers restarted or removed from the page.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shlex
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from . import fleet

ERROR = "SSHFLEET_ERR"
FIRST = "SSHFLEET_FIRST"
COUNT = "SSHFLEET_COUNT"
FILES = "SSHFLEET_FILES"
STATUS = "SSHFLEET_STATUS"
#: Options of ssh that take a value.
VALUE_OPTS = {"-F", "-o", "-i", "-l", "-p", "-J", "-L", "-R", "-D", "-W", "-E", "-b", "-c", "-m", "-Q", "-S"}
#: A follow ends by itself after this long, in case nothing ever stops it.
FOLLOW_LIMIT_S = 3600


def parse(argv: list[str]) -> dict:
    out: dict = {"control": None, "control_args": [], "alias": None, "remote": None, "gflag": False}
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            index += 1
            break
        if token == "-O":
            out["control"] = argv[index + 1]
            index += 2
            continue
        if token == "-G":
            out["gflag"] = True
            index += 1
            continue
        if token in VALUE_OPTS:
            if token == "-L":
                out["control_args"].append(argv[index + 1])
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        break
    rest = argv[index:]
    if rest:
        out["alias"] = rest[0]
        out["remote"] = " ".join(rest[1:]) or None
    return out


def current_scenario() -> str:
    path = os.environ.get("SSHFLEET_DEMO_SCENARIO")
    with contextlib.suppress(OSError):
        if path:
            name = Path(path).read_text().strip()
            if name in fleet.SCENARIOS:
                return name
    return "degraded"


@contextlib.contextmanager
def state() -> Iterator[dict]:
    """The demo's state, read and written back under a lock: many of these run at once."""
    path = Path(os.environ.get("SSHFLEET_DEMO_STATE") or "/nonexistent/sshfleet-demo-state.json")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        with os.fdopen(os.dup(descriptor), "r+", encoding="utf-8") as handle:
            text = handle.read()
            try:
                data = json.loads(text) if text.strip() else {}
            except json.JSONDecodeError:
                data = {}
            before = json.dumps(data, sort_keys=True)
            yield data
            if json.dumps(data, sort_keys=True) != before:
                handle.seek(0)
                handle.truncate()
                handle.write(json.dumps(data))
    finally:
        os.close(descriptor)


def live() -> fleet.Live:
    with state() as data:
        return fleet.Live.from_dict(data)


def words(text: str) -> list[str]:
    """A script or command split the way a shell would, quoted values kept whole."""
    lexer = shlex.shlex(text.replace("\\\n", " "), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        return text.split()


def after(tokens: list[str], flag: str) -> str | None:
    with contextlib.suppress(ValueError, IndexError):
        return tokens[tokens.index(flag) + 1]
    return None


def moment(text: str | None, default: float) -> float:
    """A time as the scripts write it: RFC 3339, ``YYYY-mm-dd HH:MM:SS [UTC]``, or ``30s``/``1m``/``2h``."""
    if not text:
        return default
    text = text.strip()
    if match := re.fullmatch(r"(\d+)([smh])", text):
        return time.time() - int(match.group(1)) * {"s": 1, "m": 60, "h": 3600}[match.group(2)]
    with contextlib.suppress(ValueError):
        parsed = datetime.fromisoformat(text.removesuffix(" UTC").replace("Z", "+00:00"))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).timestamp()
    return default


def err(line: str) -> None:
    sys.stderr.write(line.rstrip("\n") + "\n")


# ------------------------------------------------------------------------- grep


class Grep:
    """``grep -n [-i] -E [-B n] [-A n] [-m n] -e PATTERN``, as the pipelines call it."""

    def __init__(self, tokens: list[str]) -> None:
        start = next(i for i, t in enumerate(tokens) if t == "grep" and "-e" in tokens[i:])
        options = tokens[start + 1 : tokens.index("--", start)]
        self.pattern = after(options, "-e") or ""
        self.before = int(after(options, "-B") or 0)
        self.after = int(after(options, "-A") or 0)
        self.max_count = int(after(options, "-m") or 0)
        self.rx = re.compile(self.pattern, re.IGNORECASE if "-i" in options else 0)

    def run(self, lines: list[str]) -> tuple[list[str], int]:
        hits = [i for i, line in enumerate(lines) if self.rx.search(line)]
        if self.max_count:
            hits = hits[: self.max_count]
        shown: set[int] = set()
        for i in hits:
            shown.update(range(max(0, i - self.before), min(len(lines), i + self.after + 1)))
        matched, out, last = set(hits), [], None
        for i in sorted(shown):
            if last is not None and i != last + 1 and (self.before or self.after):
                out.append("--")
            out.append(f"{i + 1}{':' if i in matched else '-'}{lines[i]}")
            last = i
        return out, len(hits)


def grep_for(tokens: list[str]) -> Grep | None:
    if not any(t == "grep" and "-e" in tokens[i:] for i, t in enumerate(tokens)):
        return None
    return Grep(tokens)


# ----------------------------------------------------------------------- answers


def answer_logsearch(machine: fleet.Machine, text: str, scenario: str) -> int:
    tokens = words(text)
    now = time.time()
    parts = text.split("\\\n| ")
    if "journalctl" in tokens:
        lines = fleet.journal(machine, scenario, moment(after(tokens, "--since"), now - 3600), moment(after(tokens, "--until"), now))
    elif "find" in tokens:
        directory = next((t[2:] for t in tokens if t.startswith("D=")), "")
        if directory != "/var/log/nginx" or not machine.sites:
            err(f"{ERROR} no_files")
            return 94
        kind = "access" if any("access.log" in t for t in tokens) else "error"
        since = moment(after(tokens, "-newermt"), now - 3600)
        lines = fleet.nginx_log(machine, kind, scenario, since, now)
        err(f"{FILES} 1 {sum(len(line) + 1 for line in lines)}")
    elif "logs" in tokens:
        box = fleet.find_box(machine, tokens)
        if box is None or box.name in live().removed.get(machine.alias, []):
            err(f"{ERROR} missing_container {after(tokens, 'inspect') and tokens[tokens.index('inspect') + 3]}")
            return 90
        state, _status, _code, _restarts = fleet.box_state(machine, box, scenario, live(), now)
        until = now - fleet.TROUBLE.get(machine.alias, {}).get("exited", {}).get(box.name, (0, 0))[1] if state == "exited" else now
        since = moment(after(tokens, "--since"), now - fleet.MEMORY_S)
        lines = fleet.container_log(machine, box, scenario, since, until)[-int(after(tokens, "--tail") or 50000) :]
        if "--since" in tokens:
            err(f"{FIRST} {lines[0][:35] if lines else ''}")
            err(f"{COUNT} {len(lines)}")
    else:
        err(f"{ERROR} missing_file")
        return 92

    codes = ["0"] * len(parts)
    grep = grep_for(tokens)
    if grep is not None:
        try:
            lines, hits = grep.run(lines)
        except re.error as exc:
            err(f"grep: {exc}")
            codes = ["0"] * (len(parts) - 1) + ["2"]
            err(f"{STATUS} {' '.join(codes)}")
            return 2
        index = next((i for i, part in enumerate(parts) if part.lstrip().startswith("grep ")), len(parts) - 1)
        codes[index] = "0" if hits else "1"
    if lines:
        sys.stdout.write("\n".join(lines) + "\n")
    err(f"{STATUS} {' '.join(codes)}")
    return 0


def answer_logfollow(machine: fleet.Machine, text: str, scenario: str) -> int:
    tokens = words(text)
    box = fleet.find_box(machine, tokens)
    if box is None or box.name in live().removed.get(machine.alias, []):
        err(f"{ERROR} missing_container")
        return 90
    grep, numbered = None, False
    if "grep" in tokens:
        options = tokens[tokens.index("grep") + 1 : tokens.index("--", tokens.index("grep"))]
        grep = re.compile(after(options, "-e") or "", re.IGNORECASE if "-i" in options else 0)
        numbered = "-n" in options
    now = time.time()
    seen = 0

    def emit(lines: list[str]) -> None:
        nonlocal seen
        for line in lines:
            seen += 1
            if grep is None:
                sys.stdout.write(line + "\n")
            elif grep.search(line):
                sys.stdout.write(f"{seen}:{line}\n" if numbered else line + "\n")
        sys.stdout.flush()

    emit(fleet.container_log(machine, box, scenario, moment(after(tokens, "--since"), now - 60), now)[-int(after(tokens, "--tail") or 200) :])
    last, stop = now, now + FOLLOW_LIMIT_S
    while time.time() < stop:
        time.sleep(1.0 + fleet._hash(box.name, int(time.time())) % 25 / 10)
        moment_now = time.time()
        emit(fleet.container_log(machine, box, current_scenario(), last + 1e-6, moment_now))
        last = moment_now
    return 0


def answer_script(machine: fleet.Machine, text: str, scenario: str) -> int:
    marker = next((line.split(":", 1)[1].strip() for line in text.splitlines() if line.startswith("# sshfleet:")), "collect")
    tokens = words(text)
    now = time.time()
    if marker == "collect":
        sys.stdout.write(json.dumps(fleet.collect(machine, scenario, live(), now)))
        return 0
    if marker == "status":
        box = fleet.find_box(machine, tokens)
        if box is None or box.name in live().removed.get(machine.alias, []):
            sys.stdout.write("missing\tmissing\t-\n")
            return 0
        state, status, _code, _restarts = fleet.box_state(machine, box, scenario, live(), now)
        probe = fleet.collect(machine, scenario, live(), now)["probes"]
        sys.stdout.write(f"{state}\t{status}\t{probe[0]['code'] if probe else '-'}\n")
        return 0
    if marker == "list":
        path = after(tokens, "find") or "/"
        if not fleet.is_dir(machine, path) and path.count("/") > 1 and "." in path.rsplit("/", 1)[-1]:
            err(f"find: '{path}': Not a directory")
            return 1
        sys.stdout.write(fleet.listing(machine, path, now))
        return 0
    if marker == "resolve":
        path = after(tokens, "--") or "/"
        sys.stdout.write(f"{path}\n{'directory' if fleet.is_dir(machine, path) else 'regular file'}\n")
        return 0
    if marker == "preview":
        sys.stdout.write(fleet.preview(after(tokens, "--") or ""))
        return 0
    if marker == "stat":
        sys.stdout.write(f"1490 644 {machine.user} {machine.user}\n")
        return 0
    if marker == "logsearch":
        return answer_logsearch(machine, text, scenario)
    if marker == "logfollow":
        return answer_logfollow(machine, text, scenario)
    err(f"sshfleet demo: {machine.alias} has no answer for {marker!r}")
    return 1


def answer_command(machine: fleet.Machine, remote: str, scenario: str) -> int:
    tokens = words(remote)
    box = fleet.find_box(machine, tokens)
    now = time.time()
    if box is not None and "restart" in tokens:
        with state() as data:
            data.setdefault("restarted", {}).setdefault(machine.alias, {})[box.name] = now
        sys.stdout.write(box.name + "\n")
        return 0
    if box is not None and "rm" in tokens:
        with state() as data:
            removed = data.setdefault("removed", {}).setdefault(machine.alias, [])
            if box.name not in removed:
                removed.append(box.name)
        sys.stdout.write(box.name + "\n")
        return 0
    if box is not None and "logs" in tokens and "-f" not in tokens:
        tail = int(after(tokens, "--tail") or 80)
        lines = fleet.container_log(machine, box, scenario, now - 3600, now)[-tail:]
        sys.stdout.write("\n".join(line.split(" ", 1)[1] if "-t" not in tokens else line for line in lines) + "\n")
        return 0
    if box is not None and "logs" in tokens:
        return answer_logfollow(machine, f"logs -f -t --since 5m --tail {after(tokens, '--tail') or 80} {box.name}", scenario)
    if box is not None and "stats" in tokens:
        sys.stdout.write("CONTAINER ID   NAME   CPU %   MEM USAGE / LIMIT     MEM %   NET I/O         BLOCK I/O\n")
        sys.stdout.write(f"{fleet._hash(box.name):012x}   {box.name}   3.41%   212MiB / 3.8GiB   5.45%   1.2GB / 980MB   12MB / 0B\n")
        return 0
    sys.stdout.write(f"This is the sshfleet demo: {machine.alias} is made up, so nothing ran there.\n")
    return 0


def interactive(machine: fleet.Machine) -> int:
    """The web terminal: a prompt that runs nothing."""
    out = sys.stdout
    out.write(f"\nThis is the sshfleet demo. {machine.alias} is made up, so there is no shell behind this terminal.\n")
    out.write("Whatever is typed is answered here; exit or Ctrl+D closes it.\n\n")
    prompt = f"{machine.user}@{machine.alias}:~$ "
    out.write(prompt)
    out.flush()
    for line in sys.stdin:
        command = line.strip()
        if command in {"exit", "logout"}:
            break
        if command:
            out.write(f"demo: {command.split()[0]}: nothing runs on a made-up server\n")
        out.write(prompt)
        out.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    parsed = parse(sys.argv[1:] if argv is None else argv)
    alias = parsed["alias"] or ""
    machine = fleet.BY_ALIAS.get(alias)
    if parsed["gflag"]:
        address = machine.address if machine else alias
        sys.stdout.write(f"user {machine.user if machine else 'ubuntu'}\nhostname {address}\n")
        return 0
    if machine is None:
        err(f"ssh: Could not resolve hostname {alias}: Name or service not known")
        return 255
    scenario = current_scenario()
    if fleet.bastion_is_down(machine, scenario):
        time.sleep(1.0)
        err(f"ssh: connect to host {fleet.DOWN_ADDRESS} port 22: Connection timed out")
        return 255

    if parsed["control"]:
        with state() as data:
            masters = set(data.get("masters", []))
            if parsed["control"] == "check":
                return 0 if alias in masters else 255
            if parsed["control"] == "exit":
                masters.discard(alias)
                data["masters"] = sorted(masters)
        return 0

    with state() as data:
        data["masters"] = sorted({*data.get("masters", []), alias})

    remote = parsed["remote"] or ""
    if not remote:
        return interactive(machine)
    if remote.strip() == "true":
        return 0
    if "bash -s" in remote:
        return answer_script(machine, sys.stdin.read(), scenario)
    return answer_command(machine, remote, scenario)


if __name__ == "__main__":
    sys.exit(main())
