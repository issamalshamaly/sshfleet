"""Build the remote pipelines that do the searching.

The filtering happens on the server: only matching lines and their context cross the network.
Three rules shape every pipeline here.

* ``--tail`` always accompanies ``--since``. The docker json-file reader seeks to the tail when
  ``--tail`` is given, but scans the file from the beginning without it, so on a gigabyte log
  ``--since 5m`` alone is the slow path.
* ``--since`` is always sent as RFC3339 with ``Z``. Docker and podman read an offset-less
  timestamp in the *client's* zone, which is not the server's.
* Colour codes are stripped **before** grep, so ``ERROR`` still matches a line that is really
  ``ESC[31mERROR ESC[39m`` and the ``-B`` context count stays right.

Every pipeline reports each stage's exit code, so "grep found nothing" can be told apart from
"docker failed" and "the byte cap cut it off".
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..inventory import Host
from .model import LogSource, ResolvedWindow, SearchRequest, SourceKind

STATUS_MARKER = "SSHFLEET_STATUS"
ERROR_MARKER = "SSHFLEET_ERR"
FIRST_MARKER = "SSHFLEET_FIRST"
COUNT_MARKER = "SSHFLEET_COUNT"
FILES_MARKER = "SSHFLEET_FILES"

#: Stage-one commands a generated pipeline may use. Anything else is a bug or an injection.
ALLOWED_COMMANDS = frozenset(
    {
        "export", "sudo", "timeout", "env", "nice", "ionice", "docker", "podman", "journalctl",
        "find", "stat", "cat", "zcat", "tail", "head", "awk", "sed", "grep", "cut", "stdbuf",
        "sort", "mapfile", "test", "echo", "exit", "wc", "printf", "command", "read", "for",
        "while", "if", "case", "esac", "fi", "done", "do", "then", "else", "elif", "local",
        "RT", "C", "D", "FILES", "total", "line", "probe", "f", "[",
    }
)

#: Never appear in a generated pipeline, whatever the input.
BANNED_FRAGMENTS = (
    "rm ", "rm\t", "mkfs", "dd ", "shutdown", "reboot", "systemctl stop", "systemctl restart",
    "docker stop", "docker rm", "docker kill", "podman stop", "podman rm", "podman kill",
    "> /dev/sd", "chmod 777", ":(){", "curl http", "wget ",
)

#: Directories a log source may read from; ``logs.allowed_paths`` in the config extends this.
ALLOWED_PREFIXES = ("/var/log/", "/home/", "/usr/src/", "/app/", "/opt/", "/srv/", "/data/", "/tmp/sshfleet")

#: How far the remote time filters reach beyond the window.
#:
#: Every filter below compares timestamps as *text*, which cannot account for the offset the
#: timestamp carries: an nginx line written at ``10:00:00 +0300`` sorts as 10:00, not as the
#: 07:00 UTC it really is. Rather than pretend, each one is widened by a day and treated as a
#: coarse pre-filter whose job is to stop reading a large file early. The exact cut is made in
#: :class:`~sshfleet.logs.runner.SourceRunner`, which parses each format's offset properly.
#:
#: ``docker logs -t`` is the exception: its prefix is always UTC with ``Z``, so the stdout
#: pipeline compares exactly and keeps its early ``exit``.
COARSE_MARGIN = timedelta(days=1)


def _coarse(window: ResolvedWindow) -> tuple[datetime, datetime]:
    """The window in UTC, widened by :data:`COARSE_MARGIN`. See that constant for why."""
    return window.since.astimezone(UTC) - COARSE_MARGIN, window.until.astimezone(UTC) + COARSE_MARGIN


class CommandRefused(Exception):
    """The generated pipeline is not something sshfleet is willing to run."""


def strip_quoted(script: str, data: Sequence[str] = ()) -> str:
    """Remove the operator's own values, leaving the shell structure the tool generated.

    Everything that comes from the operator or the config is inserted with :func:`shlex.quote`,
    so the shell treats it as one literal argument. Searching for ``rm -rf /`` is a legitimate
    query; an *unquoted* ``rm -rf /`` would be a command. Only the second is a problem.

    The values are removed by exact match on their quoted form rather than by tracking quote
    state, because shlex escapes an embedded quote as ``'"'"'``, which flips naive quote
    tracking and would leave the operator's text looking like shell code.

    Only the quoted form is removed. A value that needs no quoting is its own quoted form, so it
    goes too; one that does need quoting and still appears bare somewhere is exactly the mistake
    this check exists for, and has to stay in view. Removing the bare form as well once hid a
    container name pasted unquoted into an ``echo "…"``, where ``$(…)`` in it ran on the host.
    """
    out = script
    for value in data:
        if not value:
            continue
        out = out.replace(shlex.quote(value), " «data» ")
    return out


def assert_read_only(script: str, data: Sequence[str] = ()) -> None:
    structure = strip_quoted(script, data).lower()
    for fragment in BANNED_FRAGMENTS:
        if fragment in structure:
            raise CommandRefused(f"generated pipeline contains {fragment!r}")


def assert_path_allowed(path: str, extra: tuple[str, ...] = ()) -> None:
    from ..files.denylist import reason_denied

    if not path or not path.startswith("/") or "/../" in f"{path}/":
        raise CommandRefused(f"{path!r}: a log source needs an absolute path")
    reason = reason_denied(path)
    if reason:
        raise CommandRefused(f"{path}: {reason}")
    allowed = ALLOWED_PREFIXES + tuple(p if p.endswith("/") else p + "/" for p in extra if p.startswith("/"))
    if not path.startswith(allowed):
        raise CommandRefused(f"{path}: log sources must live under {', '.join(allowed)}")


@dataclass(slots=True)
class Pipeline:
    """A remote script plus the metadata needed to interpret its exit codes."""

    script: str
    stages: list[str]
    source: LogSource
    description: str

    def stage_of(self, index: int) -> str:
        return self.stages[index] if 0 <= index < len(self.stages) else "?"


def _missing_container(container: str) -> str:
    """Report a container that is not there, and stop. The name is printed, never evaluated."""
    return f'{{ echo "{ERROR_MARKER} missing_container "{shlex.quote(container)} >&2; exit 90; }}'


def _runtime_expr(host: Host) -> str:
    if host.runtime in {"docker", "podman"}:
        return host.runtime
    return "$(command -v podman 2>/dev/null || command -v docker 2>/dev/null)"


def _sudo(host: Host) -> str:
    return "sudo -n " if host.sudo else ""


def _grep_stage(request: SearchRequest) -> str:
    flags = ["-a", "--line-buffered", "--color=never", "-n"]
    if request.ignore_case:
        flags.append("-i")
    flags.append("-F" if request.mode == "fixed" else "-E")
    if request.before:
        flags += ["-B", str(request.before)]
    if request.after:
        flags += ["-A", str(request.after)]
    if request.max_matches:
        flags += ["-m", str(request.max_matches)]
    return f"grep {' '.join(flags)} -e {shlex.quote(request.pattern)} --"


def _tail_stages(request: SearchRequest) -> list[str]:
    stages = []
    if request.max_line_bytes:
        stages.append(f"stdbuf -oL cut -b -{request.max_line_bytes}")
    if request.max_bytes:
        stages.append(f"stdbuf -o0 head -c {request.max_bytes}")
    return stages


ANSI_SED = r"sed -u -E 's/\x1b\[[0-9;]*[A-Za-z]//g'"


def _until_awk(window: ResolvedWindow, *, emit_first: bool = True) -> str:
    """Enforce ``--until`` on the host, and report what the producer actually covered.

    Exiting early sends SIGPIPE to docker, so this costs no more than ``--until`` would, and it
    works identically on podman versions whose ``--until`` is unreliable. The date guard skips
    docker's own error lines, which carry no timestamp prefix and would otherwise compare
    greater than the window end and stop the stream.

    The two markers answer "did we see the whole window?": the first line's timestamp, and how
    many lines the producer emitted. Docker applies ``--tail`` before ``--since``, so a count
    that reached the tail cap means older lines existed and were never looked at.
    """
    first = f'NR==1{{print "{FIRST_MARKER} " substr($0,1,35) > "/dev/stderr"}} ' if emit_first else ""
    end = f'END{{print "{COUNT_MARKER} " NR > "/dev/stderr"}} ' if emit_first else ""
    return (
        f"awk -v U={shlex.quote(window.until_compare)} "
        f"'{first}{end}/^[0-9][0-9][0-9][0-9]-/{{ if (substr($0,1,19) > U) exit }} {{print}}'"
    )


def _assemble(parts: list[str]) -> str:
    return " \\\n| ".join(parts)


def _finish(script: str) -> str:
    return script + f'\necho "{STATUS_MARKER} ${{PIPESTATUS[*]}}" >&2\n'


def container_stdout_pipeline(
    host: Host, source: LogSource, request: SearchRequest, window: ResolvedWindow
) -> Pipeline:
    """``docker logs`` filtered on the host."""
    container = source.container or ""
    sudo = _sudo(host)
    stages: list[str] = []
    glob = "*" in container  # kong and other hand-deployed containers have unguessable names
    quoted = '"$C"' if glob else shlex.quote(container)

    producer_flags = ["-t", "--tail", str(request.tail_lines)]
    if not request.raw_dump:
        producer_flags += ["--since", window.since_rfc3339]
    producer = f'{sudo}timeout -k 5 {request.timeout_s} "$RT" logs {" ".join(producer_flags)} {quoted} 2>&1'
    stages.append("logs")

    parts = [producer]
    if not request.raw_dump:
        parts.append(_until_awk(window))
        stages.append("until")
    if request.strip_ansi and not request.raw_dump:
        parts.append(ANSI_SED)
        stages.append("ansi")
    if request.has_pattern:
        parts.append(_grep_stage(request))
        stages.append("grep")
    if not request.raw_dump:
        for stage in _tail_stages(request):
            parts.append(stage)
            stages.append("cut" if "cut" in stage else "head")

    if glob:
        # Resolve the newest running container whose name matches, e.g. "*kong*".
        pattern = container.replace("*", "")
        resolve = (
            f'C=$({sudo}"$RT" ps --format \'{{{{.Names}}}}\' 2>/dev/null '
            f"| grep -i -- {shlex.quote(pattern)} | head -1)\n"
            f'[ -n "$C" ] || {_missing_container(container)}\n'
        )
        check = ""
    else:
        resolve = ""
        check = (
            f'{sudo}"$RT" inspect --format \'{{{{.State.Status}}}}\' {quoted} >/dev/null 2>&1 '
            f'|| {_missing_container(container)}\n'
        )

    script = (
        "# sshfleet:logsearch\n"
        "export LC_ALL=C\n"
        f'RT={_runtime_expr(host)}\n'
        f'[ -n "$RT" ] || {{ echo "{ERROR_MARKER} no_runtime" >&2; exit 91; }}\n'
        + resolve
        + check
        + _assemble(parts)
    )
    return Pipeline(_finish(script), stages, source, f"{container} on {host.alias}")


def container_file_pipeline(
    host: Host, source: LogSource, request: SearchRequest, window: ResolvedWindow
) -> Pipeline:
    """A log file inside the container: only ``tail`` runs in there."""
    path = source.path or ""
    assert_path_allowed(path, request.allowed_paths)
    container = source.container or ""
    sudo = _sudo(host)
    inner = (
        'f=$1; test -r "$f" || { echo "' + ERROR_MARKER + ' missing_file $f" >&2; exit 92; }; '
        'echo "' + FILES_MARKER + ' 1 $(wc -c < "$f")" >&2; tail -c "$2" -- "$f"'
    )
    producer = (
        f'{sudo}timeout -k 5 {request.timeout_s} "$RT" exec {shlex.quote(container)} sh -c {shlex.quote(inner)} '
        f"sh {shlex.quote(path)} {request.max_file_bytes}"
    )
    # A coarse cut on the "time" field, so a large file stops being read early. The field may
    # carry any offset, so the bounds are widened; see COARSE_MARGIN.
    coarse_since, coarse_until = _coarse(window)
    time_awk = (
        f"awk -v S={shlex.quote(coarse_since.strftime('%Y-%m-%dT%H:%M:%S'))} "
        f"-v U={shlex.quote(coarse_until.strftime('%Y-%m-%dT%H:%M:%S'))} "
        "'match($0,/\"time\":\"[^\"]*\"/){t=substr($0,RSTART+8,RLENGTH-9); "
        'if (substr(t,1,19) < S) next; if (substr(t,1,19) > U) exit} {print}\''
    )
    parts = [producer, time_awk]
    stages = ["exec", "until"]
    if request.has_pattern:
        parts.append(_grep_stage(request))
        stages.append("grep")
    for stage in _tail_stages(request):
        parts.append(stage)
        stages.append("cap")

    script = (
        "# sshfleet:logsearch\n"
        "export LC_ALL=C\n"
        f'RT={_runtime_expr(host)}\n'
        f'[ -n "$RT" ] || {{ echo "{ERROR_MARKER} no_runtime" >&2; exit 91; }}\n'
        + _assemble(parts)
    )
    return Pipeline(_finish(script), stages, source, f"{container}:{path}")


def host_file_pipeline(
    host: Host, source: LogSource, request: SearchRequest, window: ResolvedWindow
) -> Pipeline:
    """Files on the host: an application's own log, nginx logs, anything rotated."""
    path = source.path or ""
    assert_path_allowed(path, request.allowed_paths)
    directory = path.rsplit("/", 1)[0]
    base = path.rsplit("/", 1)[-1]
    names = [f"-name {shlex.quote(base)}"]
    if source.rotated_glob:
        names.append(f"-o -name {shlex.quote(source.rotated_glob)}")
    sudo = _sudo(host)
    # The label says UTC, so the value has to be UTC: an absolute window resolved in a
    # non-UTC display zone is still an aware datetime in that zone.
    since_find = window.since.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

    header = (
        "# sshfleet:logsearch\n"
        "export LC_ALL=C\n"
        f"D={shlex.quote(directory)}\n"
        f'mapfile -t FILES < <({sudo}find "$D" -maxdepth 1 -type f \\( {" ".join(names)} \\) '
        f'-newermt {shlex.quote(since_find)} -printf \'%T@ %p\\n\' 2>/dev/null | sort -n | cut -d" " -f2-)\n'
        f'[ ${{#FILES[@]}} -gt 0 ] || {{ echo "{ERROR_MARKER} no_files" >&2; exit 94; }}\n'
        f'total=$({sudo}stat -c %s -- "${{FILES[@]}}" 2>/dev/null | awk \'{{s+=$1}} END{{print s+0}}\')\n'
        f'echo "{FILES_MARKER} ${{#FILES[@]}} $total" >&2\n'
        f'[ "$total" -le {request.max_file_bytes} ] || {{ echo "{ERROR_MARKER} too_large $total" >&2; exit 93; }}\n'
    )
    reader = (
        'for f in "${FILES[@]}"; do case "$f" in '
        f'*.gz) {sudo}nice -n 10 ionice -c 3 zcat -- "$f";; '
        f'*) {sudo}nice -n 10 ionice -c 3 cat -- "$f";; '
        "esac; done 2>/dev/null"
    )

    parts = [reader]
    stages = ["read"]
    time_filter = _file_time_awk(source, window)
    if time_filter:
        parts.append(time_filter)
        stages.append("until")
    if request.strip_ansi:
        parts.append(ANSI_SED)
        stages.append("ansi")
    if request.has_pattern:
        parts.append(_grep_stage(request))
        stages.append("grep")
    for stage in _tail_stages(request):
        parts.append(stage)
        stages.append("cap")

    return Pipeline(_finish(header + _assemble(parts)), stages, source, path)


def _file_time_awk(source: LogSource, window: ResolvedWindow) -> str:
    """A coarse remote cut for the formats worth cutting remotely; "" for the rest.

    This is an optimisation, never the window itself: a format with no branch here simply sends
    more lines than asked for, and :class:`~sshfleet.logs.runner.SourceRunner` drops them. See
    :data:`COARSE_MARGIN` for why even the branches that exist are deliberately generous.
    """
    from .model import LogFormat

    coarse_since, coarse_until = _coarse(window)
    if source.log_format in {LogFormat.ZAP_JSON, LogFormat.PINO_JSON}:
        return (
            f"awk -v S={shlex.quote(coarse_since.strftime('%Y-%m-%dT%H:%M:%S'))} "
            f"-v U={shlex.quote(coarse_until.strftime('%Y-%m-%dT%H:%M:%S'))} "
            "'match($0,/\"(ts|time|timestamp|@timestamp|@t)\":\"[^\"]*\"/){"
            't=substr($0,RSTART,RLENGTH); sub(/^[^:]*:"/,"",t); '
            "if (substr(t,1,19) < S) next; if (substr(t,1,19) > U) exit} {print}'"
        )
    if source.log_format is LogFormat.NGINX_ACCESS:
        since = coarse_since.strftime("%Y%m%d%H:%M:%S")
        until = coarse_until.strftime("%Y%m%d%H:%M:%S")
        return (
            "awk 'BEGIN{split(\"Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec\",m,\" \");"
            "for(i=1;i<=12;i++)M[m[i]]=sprintf(\"%02d\",i)} "
            "{if (match($0,/\\[[0-9][0-9]\\/[A-Za-z][A-Za-z][A-Za-z]\\/[0-9][0-9][0-9][0-9]:[0-9][0-9]:[0-9][0-9]:[0-9][0-9]/)) "
            "{d=substr($0,RSTART+1,RLENGTH-1); k=substr(d,8,4) M[substr(d,4,3)] substr(d,13,8); "
            f"if (k < {shlex.quote(since)}) next; if (k > {shlex.quote(until)}) exit}} print}}'"
        )
    return ""


def journal_pipeline(
    host: Host, source: LogSource, request: SearchRequest, window: ResolvedWindow
) -> Pipeline:
    """systemd journal. ``TZ=UTC`` via ``env`` because sudo resets the environment."""
    unit_args = f"-u {shlex.quote(source.unit)} " if source.unit and source.unit != "kernel" else ""
    if source.unit == "kernel":
        unit_args = "-k "
    sudo = _sudo(host)
    producer = (
        f"{sudo}env TZ=UTC timeout -k 5 {request.timeout_s} journalctl {unit_args}"
        f"--since {shlex.quote(window.since.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S'))} "
        f"--until {shlex.quote(window.until.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S'))} "
        "-o short-iso --no-pager -q 2>&1"
    )
    parts = [producer]
    stages = ["journalctl"]
    if request.strip_ansi:
        parts.append(ANSI_SED)
        stages.append("ansi")
    if request.has_pattern:
        parts.append(_grep_stage(request))
        stages.append("grep")
    for stage in _tail_stages(request):
        parts.append(stage)
        stages.append("cap")

    script = "# sshfleet:logsearch\nexport LC_ALL=C\n" + _assemble(parts)
    return Pipeline(_finish(script), stages, source, source.unit or "journal")


def follow_pipeline(host: Host, source: LogSource, pattern: str = "", *, since: str = "1m", tail: int = 200) -> Pipeline:
    """``docker logs -f`` with no caps: the local reader applies backpressure instead."""
    container = source.container or ""
    quoted = shlex.quote(container)
    sudo = _sudo(host)
    parts = [f'{sudo}"$RT" logs -f -t --since {shlex.quote(since)} --tail {tail} {quoted} 2>&1', ANSI_SED]
    stages = ["logs", "ansi"]
    if pattern.strip():
        parts.append(f"grep -a --line-buffered --color=never -n -i -E -e {shlex.quote(pattern)} --")
        stages.append("grep")
    script = (
        "# sshfleet:logfollow\n"
        "export LC_ALL=C\n"
        f'RT={_runtime_expr(host)}\n'
        f'[ -n "$RT" ] || {{ echo "{ERROR_MARKER} no_runtime" >&2; exit 91; }}\n'
        f'{sudo}"$RT" inspect --format \'{{{{.State.Status}}}}\' {quoted} >/dev/null 2>&1 '
        f'|| {_missing_container(container)}\n'
        + _assemble(parts)
    )
    # Follow is built apart from build(), and was never checked: the same check, the same data.
    assert_read_only(script, [pattern, container, since])
    return Pipeline(script, stages, source, f"follow {container}")


BUILDERS = {
    SourceKind.CONTAINER_STDOUT: container_stdout_pipeline,
    SourceKind.CONTAINER_FILE: container_file_pipeline,
    SourceKind.HOST_FILE: host_file_pipeline,
    SourceKind.JOURNAL: journal_pipeline,
}


def build(host: Host, source: LogSource, request: SearchRequest, window: ResolvedWindow) -> Pipeline:
    try:
        builder = BUILDERS[source.kind]
    except KeyError:
        raise CommandRefused(f"{source.kind} is not a shell source") from None
    pipeline = builder(host, source, request, window)
    # Check the structure we generated, with the operator's own values taken out.
    assert_read_only(
        pipeline.script,
        [request.pattern, source.container or "", source.path or "", source.unit or "", source.rotated_glob or ""],
    )
    return pipeline
