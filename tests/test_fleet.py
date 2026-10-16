"""Fleet refresh: ordering, bastion short-circuit, caching, alerts."""

from __future__ import annotations

import json

from sshfleet.fleet import FleetService


async def test_refresh_collects_every_host(context):
    fleet = FleetService(context)
    snapshots = [s async for s in fleet.iter_refresh("testing")]
    assert len(snapshots) == len(context.inventory.hosts("testing"))
    assert all(s.reachable for s in snapshots), [s.error_message for s in snapshots if not s.reachable]


async def test_one_host_crashing_does_not_end_the_sweep_for_the_others(context, monkeypatch):
    fleet = FleetService(context)
    original = fleet._collect_one

    async def crash_on_order(host, **kwargs):
        if host.alias == "tst-order":
            raise RuntimeError("unable to perform operation on <WriteUnixTransport closed=True>")
        return await original(host, **kwargs)

    monkeypatch.setattr(fleet, "_collect_one", crash_on_order)
    snapshots = {s.alias: s async for s in fleet.iter_refresh("testing")}
    assert set(snapshots) == {h.alias for h in context.inventory.hosts("testing")}
    assert snapshots["tst-order"].reachable is False
    assert "WriteUnixTransport" in snapshots["tst-order"].error_message
    assert all(s.reachable for alias, s in snapshots.items() if alias != "tst-order")


async def test_bastions_are_refreshed_before_the_hosts_behind_them(context):
    fleet = FleetService(context)
    order = [h.alias for h in fleet.ordered(fleet.hosts("testing"))]
    assert order[0] in {"tst-bastion", "tst-jenkins-proxy"}
    assert order[1] in {"tst-bastion", "tst-jenkins-proxy"}
    # main services come next, then the rest
    main_index = order.index("tst-order")
    assert main_index < order.index("tst-jenkins")


async def test_a_dead_bastion_does_not_cost_a_timeout_per_host(context, scenario, fake_ssh):
    scenario("bastion_down")
    fleet = FleetService(context)
    snapshots = {s.alias: s async for s in fleet.iter_refresh("testing")}

    behind = [h.alias for h in context.inventory.behind("tst-bastion")]
    for alias in behind:
        assert snapshots[alias].reachable is False
        assert snapshots[alias].error_kind == "bastion_down"
        assert "tst-bastion" in snapshots[alias].error_message

    # None of them were contacted: the only calls are the two bastion attempts.
    contacted = {call["alias"] for call in fake_ssh.calls() if call.get("remote")}
    assert not (set(behind) & contacted)


async def test_alerts_are_attached_to_snapshots(context, scenario):
    """A host seen for the first time makes no claim about what should be there; memory does."""
    fleet = FleetService(context)
    [s async for s in fleet.iter_refresh("testing")]  # healthy: order-production is running
    # Twice, because a workload seen on one sweep only is a job, not something this host keeps.
    [s async for s in fleet.iter_refresh("testing")]

    scenario("degraded")
    snapshots = {s.alias: s async for s in fleet.iter_refresh("testing")}

    order_codes = {a.code for a in snapshots["tst-order"].alerts}
    assert "ABSENT" in order_codes  # it was here a moment ago and now has no trace
    assert snapshots["tst-order"].vanished_containers == ["order-production"]

    auth_codes = {a.code for a in snapshots["tst-auth"].alerts}
    assert "DISK" in auth_codes

    wallet_codes = {a.code for a in snapshots["tst-wallet"].alerts}
    assert "RESTARTING" in wallet_codes
    assert "RESTARTS" in wallet_codes


async def test_state_alerts_are_sorted_most_severe_first(context, scenario):
    scenario("degraded")
    fleet = FleetService(context)
    await fleet.refresh("testing")
    alerts = fleet.state.alerts("testing")
    severities = [a.severity.value for a in alerts]
    assert severities == sorted(severities, key=lambda s: {"crit": 0, "warn": 1, "info": 2}[s])


async def test_snapshots_are_cached_and_restored(context):
    fleet = FleetService(context)
    await fleet.refresh("testing")
    fleet.save()

    cache = context.paths.fleet_state_dir / "testing.json"
    assert cache.exists()
    assert json.loads(cache.read_text())["hosts"]

    fresh = FleetService(context)
    loaded = fresh.load_cached()
    assert loaded == len(context.inventory.hosts("testing"))
    restored = fresh.snapshot("tst-order")
    assert restored is not None
    assert restored.containers[0].name == "order-production"
    assert restored.load[0] > 0


async def test_staleness_is_measured_from_the_snapshot_time(context):
    fleet = FleetService(context)
    await fleet.refresh("testing")
    assert fleet.is_stale("tst-order") is False
    fleet.state.snapshots["tst-order"].at -= context.config.refresh.stale_after_s + 10
    assert fleet.is_stale("tst-order") is True
    assert fleet.is_stale("never-refreshed") is True


async def test_probe_ports_follow_the_previous_snapshot(context):
    from sshfleet.collector.remote import probe_specs

    fleet = FleetService(context)
    await fleet.refresh("testing")
    auth = context.inventory.host("tst-auth")
    specs = probe_specs(auth, fleet.snapshot("tst-auth"), context.config)
    # Auth publishes only on 127.0.0.1; the probe runs on the host so it still reaches it.
    assert "3001:/health/live" in specs


async def test_host_network_container_falls_back_to_the_configured_port(context):
    from sshfleet.collector.remote import probe_specs

    fleet = FleetService(context)
    await fleet.refresh("testing")
    order = context.inventory.host("tst-order")
    specs = probe_specs(order, fleet.snapshot("tst-order"), context.config)
    # Orders publishes no ports (--network host), so only the configured one is probed.
    assert specs == ["3001:/"]


async def test_wallet_probe_is_rate_limited(context):
    from sshfleet.collector.remote import should_probe

    fleet = FleetService(context)
    await fleet.refresh("testing")
    wallet = context.inventory.host("tst-wallet")
    assert wallet.probe_min_interval_s == 60
    # Wallet's /health logs a noisy block per call, so a 30s refresh must not probe every time.
    assert should_probe(wallet, fleet.snapshot("tst-wallet")) is False


async def test_wallet_collection_runs_podman_without_sudo(context, fake_ssh):
    fleet = FleetService(context)
    await fleet.refresh("testing")
    wallet_calls = [c for c in fake_ssh.calls() if c["alias"] == "tst-wallet" and c.get("remote")]
    assert wallet_calls
    remote = wallet_calls[-1]["remote"]
    assert "podman 0 " in remote  # runtime override and "no sudo", then the static-site root


async def test_a_host_removed_from_the_ssh_config_does_not_come_back_from_the_cache(context, fake_ssh):
    """It used to reappear at every start and keep alerting about a server that was gone."""
    from sshfleet.fleet import FleetService

    service = FleetService(context)
    await service.refresh()
    service.save()
    assert service.state.snapshots["tst-auth"] is not None

    # The operator deletes the stanza; the inventory no longer has it.
    del context.inventory.by_alias["tst-auth"]
    for environment in context.inventory.environments:
        environment.hosts = [h for h in environment.hosts if h.alias != "tst-auth"]

    fresh = FleetService(context)
    assert fresh.load_cached() > 0
    assert "tst-auth" not in fresh.state.snapshots
    assert all(a.host != "tst-auth" for a in fresh.state.alerts())

    # ...and it is not written back out either.
    fresh.save()
    assert "tst-auth" not in (context.paths.fleet_state_dir / "testing.json").read_text()


async def test_a_rate_limited_probe_keeps_its_last_answer(context):
    """The wallet's /health writes a block of log per call, so it is probed sparingly.

    Skipping it must not blank the health column: the last answer is carried forward with its
    own timestamp, so the UI and the alerts can say how old the reading is.
    """
    from sshfleet.collector import collect

    host = context.inventory.host("tst-wallet")
    assert host.probe_min_interval_s == 60  # from the service catalogue

    first = await collect(context.runner, host, context.config)
    assert [p.code for p in first.probes] == ["200"]
    assert first.probes[0].at == first.at

    second = await collect(context.runner, host, context.config, previous=first)
    assert [(p.port, p.code) for p in second.probes] == [(8080, "200")]
    assert second.probes[0].at == first.probes[0].at  # carried, not re-measured


async def test_an_unlimited_probe_is_measured_on_every_refresh(context):
    from sshfleet.collector import collect

    host = context.inventory.host("tst-order")
    assert host.probe_min_interval_s == 0

    first = await collect(context.runner, host, context.config)
    second = await collect(context.runner, host, context.config, previous=first)
    assert second.probes and second.probes[0].at == second.at


def test_a_check_that_did_not_answer_keeps_the_hosts_memory(context):
    """The snapshot of an unreachable check carries what was known about the host's workloads,
    so the next check that does answer can still say what has gone."""
    from sshfleet.collector.model import HostSnapshot
    from sshfleet.collector.remote import unreachable

    service = FleetService(context)
    known = HostSnapshot(alias="tst-order", env="testing", at=1.0, reachable=True)
    known.established = ["order-1"]
    known.vanished_since = {"order-2": 1.0}
    service.state.snapshots["tst-order"] = known
    service._store(unreachable(context.inventory.host("tst-order"), "timeout", "no answer"))
    stored = service.state.snapshots["tst-order"]
    assert not stored.reachable
    assert stored.established == ["order-1"] and stored.vanished_since == {"order-2": 1.0}
