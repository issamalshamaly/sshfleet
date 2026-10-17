"""Run the real ``collect.sh`` locally against command shims.

The script is the one thing that executes on every server, so it is tested as a script rather
than mocked: fake ``docker``/``podman``/``sudo``/``curl`` go on PATH and the real bash, awk, sed
and df do the work.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from sshfleet.collector import script

REMOTE_SHIMS = Path(__file__).parent / "fakes" / "remote"
COLLECT = Path(__file__).parents[1] / "src" / "sshfleet" / "collector" / "scripts" / "collect.sh"


def run_collect(
    probes: str = "", runtime: str = "auto", sudo: str = "1", *, path_extra: Path | None = None, web_root: str | None = None
) -> dict:
    env = dict(os.environ)
    env["PATH"] = f"{path_extra or REMOTE_SHIMS}{os.pathsep}{env['PATH']}"
    env["LC_ALL"] = "C"
    proc = subprocess.run(
        ["bash", str(COLLECT), probes, runtime, sudo, *([web_root] if web_root else [])],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, f"collect.sh exited {proc.returncode}: {proc.stderr}"
    assert proc.stdout.strip(), f"collect.sh printed nothing; stderr={proc.stderr}"
    return json.loads(proc.stdout)


def test_output_is_one_json_document_with_the_expected_shape():
    document = run_collect(runtime="docker")
    for key in (
        "v", "host", "now", "uptime_s", "kernel", "load", "cpus", "mem_total", "mem_avail",
        "disks", "runtime", "sudo_ok", "containers", "inspect", "listening", "probes", "frontends", "nginx",
    ):
        assert key in document, f"missing {key}"
    assert document["v"] == 1
    assert isinstance(document["load"], list) and len(document["load"]) == 3
    assert document["cpus"] >= 1
    assert document["mem_total"] > 0


def test_docker_containers_and_inspect_are_collected():
    document = run_collect(runtime="docker")
    assert document["runtime"] == "docker"
    assert document["sudo_ok"] == 1
    names = [c["Names"] for c in document["containers"]]
    assert names == ["order-production", "loyalty-production"]
    assert len(document["inspect"].splitlines()) == 2
    assert "\t" in document["inspect"]


def test_podman_array_output_is_passed_through_unchanged():
    document = run_collect(runtime="podman")
    assert document["runtime"] == "podman"
    assert isinstance(document["containers"], list)
    assert document["containers"][0]["Names"] == ["ewallet-production"]
    # podman's docker-style .LogPath is empty, so the script retries the other inspect format
    assert "ctr.log" in document["inspect"]


def test_probes_are_executed_and_reported():
    document = run_collect("3001:/health/live 9999:/health", runtime="docker")
    probes = {p["port"]: p["code"] for p in document["probes"]}
    assert probes == {3001: "200", 9999: "000"}


def test_no_probes_requested_means_no_probes_run():
    document = run_collect("", runtime="docker")
    assert document["probes"] == []


def test_rootless_runtime_skips_sudo(tmp_path: Path):
    """The wallet host runs podman as ec2-user; calling sudo there would fail."""
    shims = tmp_path / "bin"
    shims.mkdir()
    for name in ("podman", "curl", "ss", "systemctl"):
        shutil.copy(REMOTE_SHIMS / name, shims / name)
    # No sudo at all on PATH: the script must still work when told sudo is not needed.
    document = run_collect(runtime="podman", sudo="0", path_extra=shims)
    assert document["sudo_ok"] == 1
    assert document["containers"][0]["Names"] == ["ewallet-production"]


def test_sudo_denied_is_reported_without_failing(tmp_path: Path):
    shims = tmp_path / "bin"
    shims.mkdir()
    shutil.copy(REMOTE_SHIMS / "docker", shims / "docker")
    shutil.copy(REMOTE_SHIMS / "sudo-denied", shims / "sudo")
    for name in ("curl", "ss", "systemctl"):
        shutil.copy(REMOTE_SHIMS / name, shims / name)
    document = run_collect(runtime="docker", path_extra=shims)
    assert document["sudo_ok"] == 0
    assert document["containers"] == []
    # System facts are still collected — a sudo problem must not blind the whole host.
    assert document["mem_total"] > 0
    assert document["cpus"] >= 1


def test_missing_runtime_is_not_an_error(tmp_path: Path):
    shims = tmp_path / "bin"
    shims.mkdir()
    for name in ("curl", "ss", "systemctl"):
        shutil.copy(REMOTE_SHIMS / name, shims / name)
    document = run_collect(runtime="none", path_extra=shims)
    assert document["runtime"] == "none"
    assert document["containers"] == []
    assert document["disks"]


def test_disks_are_deduplicated_by_mount_point():
    document = run_collect(runtime="docker")
    mounts = [d["mount"] for d in document["disks"]]
    assert len(mounts) == len(set(mounts))
    assert all(0 <= d["pct"] <= 100 for d in document["disks"])


def test_hostname_with_shell_metacharacters_is_escaped(tmp_path: Path):
    """A host whose name contains a quote must not break the JSON."""
    shims = tmp_path / "bin"
    shims.mkdir()
    (shims / "hostname").write_text('#!/usr/bin/env bash\nprintf \'weird"name\\\\with\\\\backslash\'\n')
    (shims / "hostname").chmod(0o755)
    for name in ("curl", "ss", "systemctl"):
        shutil.copy(REMOTE_SHIMS / name, shims / name)
    document = run_collect(runtime="none", path_extra=shims)
    assert document["host"] == 'weird"name\\with\\backslash'


def test_script_depends_on_no_interpreter_or_json_tool():
    """Amazon Linux hosts have no jq, and a python start-up would cost more than the query."""
    code = [line for line in script("collect.sh").splitlines() if not line.lstrip().startswith("#")]
    body = "\n".join(code)
    for forbidden in ("jq", "python", "perl"):
        assert forbidden not in body, f"collect.sh must not depend on {forbidden}"


def test_static_sites_are_read_from_the_configured_root(tmp_path: Path):
    """The directory is a profile setting, not a constant: each subdirectory is one site."""
    (tmp_path / "shop").mkdir()
    (tmp_path / "admin").mkdir()
    (tmp_path / "index.html").write_text("not a directory\n")
    document = run_collect(runtime="none", web_root=str(tmp_path))
    assert sorted(f["name"] for f in document["frontends"]) == ["admin", "shop"]
    assert all(f["mtime"] > 0 for f in document["frontends"])
    # No fourth argument still means the conventional nginx root, which is absent here.
    assert isinstance(run_collect(runtime="none")["frontends"], list)


def test_a_probe_whose_port_is_not_a_number_is_skipped(tmp_path):
    """The port is read again as shell inside `bash -c` when curl is missing; a spec whose port is
    not digits is dropped before either branch, and the report stays valid JSON."""
    mark = tmp_path / "ran"
    document = run_collect(f"1;>{mark}:/health 3001:/health/live", runtime="docker")
    assert [p["port"] for p in document["probes"]] == [3001]
    assert not mark.exists()
