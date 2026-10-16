"""Normalising docker and podman output into one snapshot shape."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from helpers import make_config

from sshfleet.collector import parse_collect
from sshfleet.collector.alerts import evaluate, transitions
from sshfleet.collector.model import Severity
from sshfleet.config import Thresholds
from sshfleet.inventory import build_inventory

SAMPLE = Path(__file__).parent / "sshconfigs" / "sample-env"

DOCKER_DOC = {
    "v": 1,
    "host": "ip-10-99-0-10",
    "now": 1757760000,
    "uptime_s": 356400,
    "kernel": "6.8.0-1029-aws",
    "load": [0.42, 0.38, 0.35],
    "cpus": 2,
    "mem_total": 4_000_000_000,
    "mem_avail": 1_400_000_000,
    "disks": [{"mount": "/", "size": 100, "used": 71, "pct": 71}, {"mount": "/var/lib/docker", "size": 100, "used": 71, "pct": 71}],
    "runtime": "docker",
    "sudo_ok": 1,
    "runtime_error": "",
    "containers": [
        {
            "ID": "abc",
            "Names": "order-production",
            "Image": "exampleorg/order:production",
            "Networks": "host",
            "Ports": "",
            "State": "running",
            "Status": "Up 3 days",
            "CreatedAt": "2026-09-10 08:00:00 +0000 UTC",
        }
    ],
    "inspect": "/order-production\texampleorg/order:production\tsha256:aaa\t2026-09-10T08:00:00Z\t2026-09-10T08:00:05Z\t0\t0\thost\t/var/lib/docker/containers/abc/abc-json.log",
    "listening": [22, 3001],
    "probes": [{"port": 3001, "path": "/", "code": "200"}],
    "frontends": [],
    "nginx": "absent",
}

PODMAN_DOC = {
    "v": 1,
    "host": "ip-10-99-0-12",
    "now": 1757760000,
    "uptime_s": 86400,
    "kernel": "5.10.0",
    "load": [0.03, 0.02, 0.01],
    "cpus": 2,
    "mem_total": 2_000_000_000,
    "mem_avail": 1_200_000_000,
    "disks": [{"mount": "/", "size": 100, "used": 35, "pct": 35}],
    "runtime": "podman",
    "sudo_ok": 1,
    "runtime_error": "",
    "containers": [
        {
            "Id": "w1",
            "Names": ["ewallet-production"],
            "Image": "docker.io/exampleorg/ewallet:production",
            "Ports": [{"host_ip": "", "host_port": 8080, "container_port": 8080, "protocol": "tcp"}],
            "State": "running",
            "Status": "Up 5 days",
            "Created": 1757500000,
        }
    ],
    "inspect": "/ewallet-production\tdocker.io/exampleorg/ewallet:production\tsha256:ccc\t2026-09-08T08:00:00Z\t2026-09-08T08:00:05Z\t0\t0\tbridge\t/home/ec2-user/.local/share/containers/storage/w1/ctr.log",
    "listening": [22, 8080],
    "probes": [{"port": 8080, "path": "/health", "code": "200"}],
    "frontends": [],
    "nginx": "absent",
}


@pytest.fixture
def inventory():
    return build_inventory(make_config())


def test_docker_snapshot_is_parsed(inventory):
    host = inventory.host("tst-order")
    snapshot = parse_collect(json.dumps(DOCKER_DOC), host)
    assert snapshot.reachable
    assert snapshot.hostname == "ip-10-99-0-10"
    assert snapshot.cpus == 2
    assert snapshot.mem_used_pct == 65
    assert snapshot.load_per_cpu == pytest.approx(0.21)
    assert snapshot.worst_disk.pct == 71
    container = snapshot.containers[0]
    assert container.name == "order-production"
    assert container.running
    assert container.host_network  # Orders runs with --network host
    assert container.log_path.endswith("abc-json.log")


def test_podman_list_and_port_objects_are_normalised(inventory):
    host = inventory.host("tst-wallet")
    snapshot = parse_collect(json.dumps(PODMAN_DOC), host)
    container = snapshot.containers[0]
    assert container.name == "ewallet-production"  # Names was a list
    assert container.ports == "8080->8080/tcp"  # Ports was a list of objects
    assert container.running


def test_both_runtimes_produce_the_same_container_shape(inventory):
    docker = parse_collect(json.dumps(DOCKER_DOC), inventory.host("tst-order")).containers[0]
    podman = parse_collect(json.dumps(PODMAN_DOC), inventory.host("tst-wallet")).containers[0]
    assert type(docker) is type(podman)
    for field in ("state", "image", "restart_count", "network_mode"):
        assert isinstance(getattr(docker, field), type(getattr(podman, field)))


def test_the_filter_decides_what_is_looked_at(inventory):
    """`ps -a` reports everything; the host's filter says which of it is this host's business."""
    from dataclasses import replace

    host = inventory.host("tst-order")  # workloads: "order-*"
    extra = dict(DOCKER_DOC["containers"][0], ID="r1", Names="redis", State="running", Status="Up 2 days")
    document = dict(DOCKER_DOC, containers=[*DOCKER_DOC["containers"], extra])

    watched = parse_collect(json.dumps(document), host)
    assert [c.name for c in watched.containers] == ["order-production"]

    # No filter: everything the host reports is watched.
    open_host = replace(host, workload_glob=None)
    everything = parse_collect(json.dumps(document), open_host)
    assert [c.name for c in everything.containers] == ["order-production", "redis"]


def test_a_count_is_only_expected_when_it_was_set(inventory):
    """Absent means no contract at all: losing a workload must never teach a new expectation."""
    from dataclasses import replace

    host = replace(inventory.host("tst-order"), workload_glob=None)
    three = dict(DOCKER_DOC, containers=[
        dict(DOCKER_DOC["containers"][0], ID=f"c{i}", Names=f"order-{i}") for i in range(3)
    ])
    two = dict(DOCKER_DOC, containers=three["containers"][:2])

    first = parse_collect(json.dumps(three), host)
    before = parse_collect(json.dumps(three), host, previous=first)  # running twice: established
    after = parse_collect(json.dumps(two), host, previous=before)
    counts = [a for a in evaluate(after, host, Thresholds(), before) if a.code == "ABSENT" and a.container is None]
    assert counts == []  # no count alert without an explicit setting
    assert after.vanished_containers == ["order-2"]  # but the name is remembered

    counted = replace(host, expected_count=3)
    snapshot = parse_collect(json.dumps(two), counted, previous=before)
    messages = [a.message for a in evaluate(snapshot, counted, Thresholds(), before) if a.code == "ABSENT"]
    assert "2 of 3 workloads present" in messages


def test_counting_happens_after_the_filter(inventory):
    """The boundary between the two rules: unwatched workloads are not part of the count."""
    from dataclasses import replace

    host = replace(inventory.host("tst-order"), expected_count=3)  # watches order-*
    names = ["order-a", "order-b", "order-c", "redis", "node-exporter"]
    document = dict(DOCKER_DOC, containers=[
        dict(DOCKER_DOC["containers"][0], ID=f"c{i}", Names=name) for i, name in enumerate(names)
    ])
    snapshot = parse_collect(json.dumps(document), host)
    assert len(snapshot.containers) == 3
    assert not [a for a in evaluate(snapshot, host, Thresholds()) if a.code == "ABSENT"]

    fewer = dict(document, containers=document["containers"][:2] + document["containers"][3:])
    short = parse_collect(json.dumps(fewer), host)
    assert [a.message for a in evaluate(short, host, Thresholds()) if a.code == "ABSENT"] == [
        "2 of 3 workloads present"
    ]


def test_a_stopped_workload_still_counts(inventory):
    """Existence, not health: a stopped workload is one STOPPED alert, never also a missing one."""
    from dataclasses import replace

    host = replace(inventory.host("tst-order"), workload_glob=None, expected_count=2)
    document = dict(DOCKER_DOC, containers=[
        DOCKER_DOC["containers"][0],
        dict(DOCKER_DOC["containers"][0], ID="c2", Names="order-worker", State="exited", Status="Exited (0) 1 hour ago"),
    ])
    snapshot = parse_collect(json.dumps(document), host)
    alerts = evaluate(snapshot, host, Thresholds())
    assert [a.code for a in alerts if a.code == "ABSENT"] == []
    assert [a.container for a in alerts if a.code == "STOPPED"] == ["order-worker"]

    # It only becomes a count problem once it leaves ps -a entirely — and because it was already
    # stopped when it went, that is a count alert, not a "was here and is gone" one.
    gone = parse_collect(json.dumps(dict(document, containers=document["containers"][:1])), host, previous=snapshot)
    absent = [a for a in evaluate(gone, host, Thresholds(), snapshot) if a.code == "ABSENT"]
    assert [a.message for a in absent] == ["1 of 2 workloads present"]


def test_sudo_failure_does_not_produce_phantom_absent_alerts(inventory):
    host = inventory.host("tst-order")
    document = dict(DOCKER_DOC, sudo_ok=0, containers=[], inspect="")
    snapshot = parse_collect(json.dumps(document), host)
    alerts = evaluate(snapshot, host, Thresholds())
    codes = [a.code for a in alerts]
    assert "SUDO" in codes


def test_restart_between_snapshots_is_noticed(inventory):
    host = inventory.host("tst-order")
    before = parse_collect(json.dumps(DOCKER_DOC), host)
    later_doc = dict(
        DOCKER_DOC,
        inspect="/order-production\texampleorg/order:production\tsha256:aaa\t2026-09-10T08:00:00Z\t2026-09-13T09:00:00Z\t0\t1\thost\t/var/log/x.log",
    )
    after = parse_collect(json.dumps(later_doc), host)
    codes = [a.code for a in evaluate(after, host, Thresholds(), previous=before)]
    assert "RESTARTED" in codes


def test_unreachable_snapshot_reports_only_that(inventory):
    from sshfleet.collector import unreachable

    host = inventory.host("tst-order")
    snapshot = unreachable(host, "bastion_down", "via tst-bastion: timed out")
    alerts = evaluate(snapshot, host, Thresholds())
    assert [a.code for a in alerts] == ["BASTION_DOWN"]


def test_transitions_only_report_changes():
    from sshfleet.collector.model import Alert

    first = [Alert("DISK", Severity.WARN, "80%", "h1")]
    second = [Alert("DISK", Severity.WARN, "81%", "h1"), Alert("ABSENT", Severity.CRIT, "gone", "h1", "order-production")]
    appeared, cleared = transitions(first, second)
    assert [a.code for a in appeared] == ["ABSENT"]
    assert cleared == []
    appeared, cleared = transitions(second, first)
    assert [a.code for a in cleared] == ["ABSENT"]


def test_a_failing_probe_alert_says_how_old_the_reading_is(inventory):
    """A carried-forward probe must never be passed off as a reading taken just now."""
    host = inventory.host("tst-order")
    document = dict(DOCKER_DOC, probes=[{"port": 3001, "path": "/", "code": "503"}])
    snapshot = parse_collect(json.dumps(document), host)

    fresh = next(a for a in evaluate(snapshot, host, Thresholds()) if a.code == "PROBE")
    assert "last checked" not in fresh.message

    snapshot.probes[0].at = snapshot.at - 900  # skipped for a quarter of an hour
    stale = next(a for a in evaluate(snapshot, host, Thresholds()) if a.code == "PROBE")
    assert "last checked 15m ago" in stale.message


# --------------------------------------------------------- memory-based expectations


def test_a_container_that_vanishes_is_remembered_until_ignored(inventory):
    """No naming rule needed: what ps -a listed across two sweeps and has no trace now is reported."""
    from dataclasses import replace

    host = replace(inventory.host("tst-order"), workload_glob=None)
    helper = {"ID": "h1", "Names": "helper", "Image": "x", "Networks": "bridge", "Ports": "", "State": "running", "Status": "Up 1 day", "CreatedAt": "2026-09-10 08:00:00 +0000 UTC"}
    with_helper = dict(DOCKER_DOC, containers=[*DOCKER_DOC["containers"], helper])
    first = parse_collect(json.dumps(with_helper), host)
    before = parse_collect(json.dumps(with_helper), host, previous=first)  # running twice: established
    assert "helper" in before.established

    after = parse_collect(json.dumps(DOCKER_DOC), host, previous=before)  # helper has no trace now
    assert after.vanished_containers == ["helper"]
    gone = [a for a in evaluate(after, host, Thresholds()) if a.code == "ABSENT"]
    assert any("helper was here and is gone" in a.message for a in gone)

    # It stays reported across refreshes, not just for the one right after it disappeared.
    later = parse_collect(json.dumps(DOCKER_DOC), host, previous=after)
    assert later.vanished_containers == ["helper"]

    # Until the operator says it left on purpose.
    quiet_host = replace(host, ignored_containers=["helper"])
    calm = parse_collect(json.dumps(DOCKER_DOC), quiet_host, previous=later)
    assert calm.vanished_containers == []
    assert not [a for a in evaluate(calm, quiet_host, Thresholds()) if a.code == "ABSENT" and a.container == "helper"]


def test_a_job_runners_containers_are_ignored_by_glob(inventory):
    """Airbyte names a container per task and throws it away: one rule has to cover all of them."""
    from dataclasses import replace

    # Two sweeps apart, so the run would be established and its end reported as a loss.
    host = replace(inventory.host("tst-order"), workload_glob=None, ignored_containers=["destination-*"])
    job = {
        "ID": "j1", "Names": "destination-postgres-write-43138-0-nxulk", "Image": "airbyte/destination-postgres",
        "Networks": "bridge", "Ports": "", "State": "running", "Status": "Up 4 minutes",
        "CreatedAt": "2026-09-22 08:00:00 +0000 UTC",
    }
    with_job = dict(DOCKER_DOC, containers=[*DOCKER_DOC["containers"], job])
    first = parse_collect(json.dumps(with_job), host)
    running = parse_collect(json.dumps(with_job), host, previous=first)

    assert running.container("destination-postgres-write-43138-0-nxulk").ignored is True
    assert "destination-postgres-write-43138-0-nxulk" not in running.established  # never remembered

    done = parse_collect(json.dumps(DOCKER_DOC), host, previous=running)  # the job finished and was removed
    assert done.vanished_containers == []
    assert not [a for a in evaluate(done, host, Thresholds()) if a.code == "ABSENT"]

    # Nor does the exited container left behind by such a job say anything.
    exited = dict(job, State="exited", Status="Exited (0) 2 minutes ago")
    left_over = parse_collect(json.dumps(dict(DOCKER_DOC, containers=[*DOCKER_DOC["containers"], exited])), host, previous=running)
    assert not [a for a in evaluate(left_over, host, Thresholds()) if a.container == exited["Names"]]


def test_ignored_containers_do_not_pad_the_expected_count(inventory):
    """A finished job must not stand in for the service that is actually missing."""
    from dataclasses import replace

    # Two workloads are expected; one real one is here and a finished job makes up the number.
    host = replace(inventory.host("tst-order"), workload_glob=None, expected_count=2, ignored_containers=["destination-*"])
    job = {
        "ID": "j1", "Names": "destination-postgres-write-43138-0-nxulk", "Image": "airbyte/destination-postgres",
        "Networks": "bridge", "Ports": "", "State": "exited", "Status": "Exited (0) 2 minutes ago",
        "CreatedAt": "2026-09-22 08:00:00 +0000 UTC",
    }
    snapshot = parse_collect(json.dumps(dict(DOCKER_DOC, containers=[*DOCKER_DOC["containers"], job])), host)
    assert len(snapshot.containers) == 2 and len(snapshot.tracked_containers) == 1
    messages = [a.message for a in evaluate(snapshot, host, Thresholds()) if a.code == "ABSENT"]
    assert messages == ["1 of 2 workloads present"]


def test_a_fixed_expected_count_is_checked_against_ps(inventory):
    from dataclasses import replace

    host = replace(inventory.host("tst-order"), expected_count=3)
    snapshot = parse_collect(json.dumps(DOCKER_DOC), host)
    assert snapshot.expected_count == 3
    messages = [a.message for a in evaluate(snapshot, host, Thresholds()) if a.code == "ABSENT"]
    assert "1 of 3 workloads present" in messages


def test_any_stopped_container_is_reported_whatever_its_name(inventory):
    from dataclasses import replace

    host = replace(inventory.host("tst-order"), workload_glob=None)
    helper = {"Command": "sh", "CreatedAt": "2026-09-10 08:00:00 +0000 UTC", "ID": "h1", "Image": "x", "Names": "helper", "Networks": "bridge", "Ports": "", "State": "exited", "Status": "Exited (0) 1 hour ago"}
    crashed = dict(helper, ID="h2", Names="worker", Status="Exited (137) 1 hour ago")
    # collect.sh's inspect line: name, image, image id, created, started, exit code, restarts, network, log path
    inspect = DOCKER_DOC["inspect"] + "\n/helper\tx\tsha256:h\t2026-09-10T08:00:00Z\t2026-09-10T08:00:05Z\t0\t0\tbridge\t/x.log" + "\n/worker\tx\tsha256:w\t2026-09-10T08:00:00Z\t2026-09-10T08:00:05Z\t137\t0\tbridge\t/w.log"
    document = dict(DOCKER_DOC, containers=[*DOCKER_DOC["containers"], helper, crashed], inspect=inspect)
    snapshot = parse_collect(json.dumps(document), host)
    stopped = {a.container: a for a in evaluate(snapshot, host, Thresholds()) if a.code == "STOPPED"}
    assert stopped["helper"].severity is Severity.INFO  # clean exit, never seen running: an init job, noted
    assert stopped["worker"].severity is Severity.CRIT  # crashed
    assert "exit 137" in stopped["worker"].message

    # A clean exit of something that was running at the last check is worth a look.
    running_helper = dict(helper, State="running", Status="Up 1 hour")
    earlier = parse_collect(json.dumps(dict(document, containers=[*DOCKER_DOC["containers"], running_helper, crashed])), host)
    later = parse_collect(json.dumps(document), host, previous=earlier)
    stopped = {a.container: a for a in evaluate(later, host, Thresholds(), earlier) if a.code == "STOPPED"}
    assert stopped["helper"].severity is Severity.WARN


def test_short_lived_jobs_are_not_remembered_as_losses(inventory):
    """A fleet that runs one-shot jobs leaves exited containers behind; cleaning them up is not news."""
    from dataclasses import replace

    host = replace(inventory.host("tst-order"), workload_glob=None)
    job = dict(DOCKER_DOC["containers"][0], ID="j1", Names="sync-job-1", State="exited", Status="Exited (0) 5 minutes ago")
    with_job = dict(DOCKER_DOC, containers=[*DOCKER_DOC["containers"], job])

    before = parse_collect(json.dumps(with_job), host)
    after = parse_collect(json.dumps(DOCKER_DOC), host, previous=before)
    assert after.vanished_containers == []  # it was already stopped: removing it is housekeeping

    # Nor is one caught mid-run and gone by the next sweep: it lived less than one refresh
    # interval, which is what a job does and what a service never does.
    running_job = dict(job, State="running", Status="Up 5 minutes")
    with_running = dict(DOCKER_DOC, containers=[*DOCKER_DOC["containers"], running_job])
    once = parse_collect(json.dumps(with_running), host)
    assert parse_collect(json.dumps(DOCKER_DOC), host, previous=once).vanished_containers == []

    # Seen running twice, it is a workload of this host, and its disappearance is reported.
    twice = parse_collect(json.dumps(with_running), host, previous=once)
    gone = parse_collect(json.dumps(DOCKER_DOC), host, previous=twice)
    assert gone.vanished_containers == ["sync-job-1"]


def test_the_memory_of_a_loss_ages_out(inventory):
    """A name remembered forever turns every finished job into a permanent alarm. The loss is
    reported while it is news; `expected_count` is the durable contract, not this."""
    from dataclasses import replace

    host = replace(inventory.host("tst-order"), workload_glob=None)
    worker = dict(DOCKER_DOC["containers"][0], ID="w1", Names="worker", State="running", Status="Up 1 day")
    with_worker = dict(DOCKER_DOC, containers=[*DOCKER_DOC["containers"], worker])

    once = parse_collect(json.dumps(with_worker), host)
    twice = parse_collect(json.dumps(with_worker), host, previous=once)
    gone = parse_collect(json.dumps(DOCKER_DOC), host, previous=twice, memory_s=3600)
    assert gone.vanished_containers == ["worker"]

    # Still reported a while later …
    soon = parse_collect(json.dumps(DOCKER_DOC), host, previous=gone, memory_s=3600)
    assert soon.vanished_containers == ["worker"]

    # … and no longer once the window has passed.
    gone.vanished_since = {"worker": gone.at - 7200}
    later = parse_collect(json.dumps(DOCKER_DOC), host, previous=gone, memory_s=3600)
    assert later.vanished_containers == []


def test_many_losses_collapse_into_one_alert(inventory):
    from dataclasses import replace

    host = replace(inventory.host("tst-order"), workload_glob=None)
    names = [f"worker-{i}" for i in range(6)]
    running = dict(DOCKER_DOC, containers=[
        dict(DOCKER_DOC["containers"][0], ID=f"w{i}", Names=n, State="running", Status="Up 1 day")
        for i, n in enumerate(names)
    ])
    first = parse_collect(json.dumps(running), host)
    before = parse_collect(json.dumps(running), host, previous=first)
    after = parse_collect(json.dumps(dict(DOCKER_DOC, containers=[])), host, previous=before)

    absent = [a for a in evaluate(after, host, Thresholds(), before) if a.code == "ABSENT"]
    assert len(absent) == 1  # the count is the news, not six lines of it — and not both
    assert "6 workloads were here and are gone" in absent[0].message


def test_domains_are_read_from_the_web_server_that_serves_them(inventory):
    """The list is what you could paste into a browser: catch-alls, localhost, bare IPs and
    anything with a variable in it are not addresses, and the http block that only redirects to
    https is not a second site."""
    from sshfleet.collector.parse import _parse_sites

    sites = _parse_sites(
        "shop.example.com\t443\t1\tnginx\n"
        "shop.example.com\t80\t0\tnginx\n"       # the redirect block for the same name
        "api.example.com\t8443\t1\tcaddy\n"
        "plain.example.com\t8080\t0\tapache\n"
        "\t443\t1\tnginx\n"                       # nothing at all
    )
    assert [(s.name, s.url, s.source) for s in sites] == [
        ("api.example.com", "https://api.example.com:8443", "caddy"),
        ("plain.example.com", "http://plain.example.com:8080", "apache"),
        ("shop.example.com", "https://shop.example.com", "nginx"),
    ]


def test_a_host_keeps_the_domains_it_had_when_they_are_not_re_read(inventory):
    """Discovery is asked for at startup and by hand, so in between the last answer stands rather
    than the list emptying on every sweep."""
    from dataclasses import replace

    host = replace(inventory.host("tst-order"), workload_glob=None)
    with_sites = dict(DOCKER_DOC, sites="shop.example.com\t443\t1\tnginx")
    discovered = parse_collect(json.dumps(with_sites), host)
    assert [s.name for s in discovered.sites] == ["shop.example.com"]

    # A sweep that did not ask for them reports none; carrying them forward is the collector's job.
    quiet = parse_collect(json.dumps(DOCKER_DOC), host, previous=discovered)
    assert quiet.sites == []


def _worker_gone(inventory):
    """A host whose `worker` was established (running two checks in a row) and has just gone."""
    from dataclasses import replace

    host = replace(inventory.host("tst-order"), workload_glob=None)
    worker = dict(DOCKER_DOC["containers"][0], ID="w1", Names="worker", State="running", Status="Up 1 day")
    with_worker = dict(DOCKER_DOC, containers=[*DOCKER_DOC["containers"], worker])
    once = parse_collect(json.dumps(with_worker), host)
    twice = parse_collect(json.dumps(with_worker), host, previous=once)
    return host, with_worker, twice


def test_an_unreachable_check_in_between_does_not_forget_what_has_gone(inventory):
    """One check that could not reach the host used to wipe every "was here and is gone": the
    loss vanished from the page because the connection blinked, not because anything came back."""
    from sshfleet.collector.remote import unreachable

    host, _, twice = _worker_gone(inventory)
    gone = parse_collect(json.dumps(DOCKER_DOC), host, previous=twice)
    assert gone.vanished_containers == ["worker"]

    blink = unreachable(host, "timeout", "no answer")
    # What FleetService._store does with a check that did not answer.
    blink.established, blink.vanished_since = list(gone.established), dict(gone.vanished_since)
    back = parse_collect(json.dumps(DOCKER_DOC), host, previous=blink)
    assert back.vanished_containers == ["worker"]
    assert back.vanished_since == gone.vanished_since  # still aged from when it was first missed


def test_a_workload_that_leaves_while_the_host_is_unreachable_is_still_reported(inventory):
    from sshfleet.collector.remote import unreachable

    host, with_worker, twice = _worker_gone(inventory)
    blink = unreachable(host, "timeout", "no answer")
    blink.established, blink.vanished_since = list(twice.established), dict(twice.vanished_since)
    back = parse_collect(json.dumps(DOCKER_DOC), host, previous=blink)  # worker gone meanwhile
    assert back.vanished_containers == ["worker"]
    # And one still running after the outage is still known as established.
    still = parse_collect(json.dumps(with_worker), host, previous=blink)
    assert "worker" in still.established


def test_a_failed_sudo_is_not_every_workload_gone(inventory):
    """With sudo failing the container list comes back empty. That is a host we could not look
    into, not one whose services all left: no new loss, and nothing forgotten."""
    host, with_worker, twice = _worker_gone(inventory)
    blind = parse_collect(json.dumps(dict(DOCKER_DOC, sudo_ok=0, containers=[])), host, previous=twice)
    assert blind.vanished_containers == []
    assert "worker" in blind.established
    codes = {a.code for a in evaluate(blind, host, Thresholds(), twice)}
    assert codes == {"SUDO"}

    from dataclasses import replace

    counted = replace(host, expected_count=2)
    blind.expected_count = 2
    assert {a.code for a in evaluate(blind, counted, Thresholds(), twice)} == {"SUDO"}  # no "0 of 2 present"

    # Once it can look again, a workload that really left is reported.
    after = parse_collect(json.dumps(DOCKER_DOC), host, previous=blind)
    assert after.vanished_containers == ["worker"]
    # And one that is still there is simply there.
    fine = parse_collect(json.dumps(with_worker), host, previous=blind)
    assert fine.vanished_containers == [] and "worker" in fine.established
