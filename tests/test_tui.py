"""The terminal interface, driven by Textual's test pilot against the fake ssh."""

from __future__ import annotations

import pytest
from textual.widgets import DataTable, Input

from sshfleet.tui.app import SshFleetApp
from sshfleet.tui.screens import (
    FileBrowserScreen,
    FleetScreen,
    HelpScreen,
    HostScreen,
    LogSearchScreen,
    RestartConfirmScreen,
    TunnelsScreen,
)

SIZE = (160, 45)


@pytest.fixture
def app(context):
    return SshFleetApp(context=context)


async def settle(pilot, times: int = 6) -> None:
    """Let the background refresh land before asserting on the table."""
    for _ in range(times):
        await pilot.pause()


async def test_fleet_table_lists_every_host(app, context):
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        table = app.screen.query_one("#fleet", DataTable)
        assert table.row_count == len(context.inventory.hosts())


async def test_snapshots_populate_the_row(app):
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, 10)
        snapshot = app.fleet.snapshot("tst-order")
        assert snapshot is not None and snapshot.reachable
        table = app.screen.query_one("#fleet", DataTable)
        row = table.get_row("tst-order")
        assert "order" in str(row[1])
        assert "●" in str(row[2])  # reachable


async def test_a_dead_bastion_shows_a_banner_without_freezing(app, scenario):
    scenario("bastion_down")
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, 10)
        banner = app.screen.query_one("#banner")
        assert not banner.has_class("hidden")
        assert "unreachable" in banner.message
        # The UI is still usable.
        await pilot.press("R")
        await pilot.pause()


async def test_filter_narrows_the_table(app):
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await pilot.press("/")
        for char in "wallet":
            await pilot.press(char)
        await pilot.pause()
        table = app.screen.query_one("#fleet", DataTable)
        assert table.row_count == 1


async def test_enter_opens_the_host_detail(app):
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, 10)
        app.screen.query_one("#fleet", DataTable).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, HostScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, FleetScreen)


async def test_host_detail_lists_containers(app):
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, 10)
        app.open_host("tst-order", "containers")
        await settle(pilot)
        table = app.screen.query_one("#containers-table", DataTable)
        assert table.row_count >= 1
        assert "order-production" in [str(table.get_row_at(i)[0]) for i in range(table.row_count)]


async def test_restart_asks_before_doing_anything(app, fake_ssh):
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, 10)
        app.confirm_restart("tst-order", "order-production")
        await pilot.pause()
        assert isinstance(app.screen, RestartConfirmScreen)
        # The exact command is shown, and nothing has run yet.
        rendered = " ".join(str(widget.content) for widget in app.screen.query("Static"))
        assert "docker restart" in rendered
        assert not any("restart" in (call.get("remote") or "") for call in fake_ssh.calls())
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, RestartConfirmScreen)


async def test_production_restart_requires_typing_the_name(app, context, fake_ssh):
    context.config.environments[0].name = "production"
    for host in context.inventory.hosts():
        host.env = "production"
    context.inventory.environments[0].name = "production"
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, 8)
        app.confirm_restart("tst-order", "order-production")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, RestartConfirmScreen)
        assert screen.is_production
        field = screen.query_one("#confirm-input", Input)
        field.value = "wrong-name"
        assert screen._confirmed() is False
        field.value = "order-production"
        assert screen._confirmed() is True


async def test_logs_screen_opens_with_the_right_sources(app):
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, 8)
        app.open_logs(host_alias="tst-order", container="order-production")
        await pilot.pause()
        assert isinstance(app.screen, LogSearchScreen)
        assert [s.container for s in app.screen.sources] == ["order-production"]


async def test_logs_screen_for_the_whole_environment(app):
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, 8)
        app.open_logs(env="testing")
        await pilot.pause()
        assert isinstance(app.screen, LogSearchScreen)
        labels = {s.label for s in app.screen.sources}
        assert "order" in labels and "auth" in labels


async def test_file_browser_marks_secrets(app, fake_ssh):
    listing = (
        "f\t100\t1757760000\t644\tubuntu\tubuntu\torder-log.txt\t\0"
        "f\t9000\t1757760000\t600\tubuntu\tubuntu\torder.env.production\t\0"
    )
    fake_ssh.write_fixture("tst-order", "list.txt", listing)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, 8)
        app.open_files("tst-order", "/home/ubuntu")
        await settle(pilot, 8)
        assert isinstance(app.screen, FileBrowserScreen)
        table = app.screen.query_one("#files", DataTable)
        rows = [str(table.get_row_at(i)[0]) for i in range(table.row_count)]
        assert any("order.env.production" in row and "🔒" in row for row in rows)


async def test_tunnels_and_help_screens_open(app):
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        app.open_tunnels()
        await pilot.pause()
        assert isinstance(app.screen, TunnelsScreen)
        await pilot.press("escape")
        await pilot.pause()
        app.open_help()
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)


async def test_alerts_only_filter(app, scenario):
    scenario("degraded")
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, 10)
        table = app.screen.query_one("#fleet", DataTable)
        everything = table.row_count
        await pilot.press("a")
        await pilot.pause()
        assert table.row_count < everything
        assert table.row_count > 0


async def test_status_bar_reports_connections(app):
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, 10)
        status = app.screen.query_one("#status-bar")
        assert status.mux_total == len(app.context.inventory.hosts())
        assert status.mux_alive > 0
