"""Export: a bundle carries the fleet, never a key, a secret or a line that runs on import."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sshfleet.bundle import REDACTED, BundleError, build, read_dir, to_json, write_dir
from sshfleet.config import EnvironmentCfg, HostOverride

IDENTITY_LINE = re.compile(r"^\s*IdentityFile\s", re.IGNORECASE | re.MULTILINE)


def _build(context, **options):
    return build(context.config, context.inventory, context.paths, **options)


def test_an_export_of_a_config_seeded_with_secrets_contains_none(context, sample_env_file):
    """Every way a secret could ride along: a credential setting, a password in a URL, a token
    under a setting of its own, the key file itself. What can be cut is cut and said; the rest of
    this test shows what cannot be cut refuses the export instead."""
    context.paths.secrets_file.write_text("ELASTIC_PASSWORD=supersecretvalue123\n")
    context.paths.secrets_file.chmod(0o600)
    context.config.logs.elastic = {"url": "https://ops:hunter2pass@es.example:9200", "password": "s3cr3t-value", "index": "logs-*"}
    context.config.extra["notify"] = {"slack_token": "xoxb-1234567890-abcdefghij", "channel": "#ops"}

    bundle, warnings = _build(context)
    raw = to_json(bundle).decode()
    for secret in ("hunter2pass", "s3cr3t-value", "xoxb-", "supersecretvalue123", "not-a-real-key", "test.pem"):
        assert secret not in raw, secret
    assert not IDENTITY_LINE.search(bundle["ssh"]["test-env"])
    assert bundle["fleet"]["logs"]["elastic"] == {"url": "https://es.example:9200", "password": REDACTED, "index": "logs-*"}
    assert bundle["fleet"]["notify"] == {"slack_token": REDACTED, "channel": "#ops"}
    assert any("logs.elastic.password" in w for w in warnings)
    # A host alias that merely ends in "auth" is a name, not a credential.
    assert "tst-auth" in bundle["fleet"]["hosts"]


@pytest.mark.parametrize(
    "plant",
    [
        # Assembled here so the source itself never holds a key-shaped literal (check-secrets reads it).
        lambda ctx, ssh: setattr(ctx.config.hosts["tst-order"], "display", "AKIA" + "ABCDEFGHIJKLMNOP"),
        lambda ctx, ssh: setattr(ctx.config.hosts["tst-order"], "display", "supersecretvalue123"),
        lambda ctx, ssh: ctx.config.extra.update(notes="ghp_" + "a" * 36),
        lambda ctx, ssh: ssh.write_text(ssh.read_text() + "\n# -----BEGIN OPENSSH PRIVATE KEY-----\n"),
    ],
    ids=["aws-key-in-a-label", "secrets-env-value-in-a-label", "github-token", "key-pasted-into-the-ssh-file"],
)
def test_a_secret_that_cannot_be_cut_refuses_the_export(context, sample_env_file, plant):
    context.paths.secrets_file.write_text("ELASTIC_PASSWORD=supersecretvalue123\n")
    plant(context, sample_env_file)
    with pytest.raises(BundleError):
        _build(context)


def test_identity_files_and_key_paths_stay_behind(context):
    bundle, _ = _build(context)
    (env,) = bundle["fleet"]["environments"]
    assert "key_path" not in env and "ssh_config_path" not in env and env["ssh_file"] == "test-env"
    assert not IDENTITY_LINE.search(bundle["ssh"]["test-env"])
    assert "local" not in bundle
    for section, key in (("files", "download_dir"), ("ssh", "control_dir"), ("ssh", "include_user_config"), ("logs", "display_tz")):
        assert key not in bundle["fleet"][section], key
    assert "ui" not in bundle["fleet"]

    with_local, _ = _build(context, with_local=True)
    assert with_local["local"]["environments"]["testing"]["key_path"].endswith("test.pem")
    assert "not-a-real-key" not in to_json(with_local).decode()  # the path, never the key


def test_pauses_are_this_operators_and_travel_only_with_local(context):
    context.config.hosts["tst-order"].paused = True
    bundle, _ = _build(context)
    assert "paused" not in bundle["fleet"]["hosts"]["tst-order"]
    assert _build(context, with_local=True)[0]["local"]["hosts"] == {"tst-order": {"paused": True}}


def test_a_proxycommand_is_refused_unless_allowed(context, sample_env_file):
    sample_env_file.write_text(sample_env_file.read_text() + "\nHost tst-extra\n    ProxyCommand nc -X connect %h %p\n")
    with pytest.raises(BundleError, match="ProxyCommand"):
        _build(context)
    bundle, warnings = _build(context, allow_commands=True)
    assert "ProxyCommand" in bundle["ssh"]["test-env"]
    assert any("run a program" in w for w in warnings)


def test_the_folder_form_and_the_file_form_hold_the_same_bundle(context, tmp_path: Path):
    bundle, _ = _build(context, with_local=True)
    folder = tmp_path / "ops" / "sshfleet"
    write_dir(bundle, folder)
    assert sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()) == [
        "fleet.yaml",
        "local.yaml",
        "manifest.json",
        "ssh/test-env",
    ]
    assert (folder / "local.yaml").stat().st_mode & 0o777 == 0o600
    assert json.loads(to_json(read_dir(folder))) == json.loads(to_json(bundle))
    # Exporting again without the local part takes local.yaml away rather than leaving it stale.
    write_dir(_build(context)[0], folder)
    assert not (folder / "local.yaml").exists()


def test_a_folder_holding_something_else_is_not_overwritten(context, tmp_path: Path):
    (tmp_path / "notes.txt").write_text("mine")
    with pytest.raises(BundleError, match="not empty"):
        write_dir(_build(context)[0], tmp_path)


def test_a_scoped_export_carries_only_its_environments(context, tmp_path: Path):
    other = tmp_path / "other-env"
    other.write_text("Host oth-app\n    HostName 10.7.0.1\n")
    context.config.environments.append(EnvironmentCfg(name="other", ssh_config_path=other, short="oth", project="lab"))
    context.config.hosts["oth-app"] = HostOverride(label="other app")
    from sshfleet.inventory import build_inventory

    context.inventory = build_inventory(context.config)
    bundle, _ = _build(context, environments=["testing"])
    assert [e["name"] for e in bundle["fleet"]["environments"]] == ["testing"]
    assert list(bundle["ssh"]) == ["test-env"]
    assert "oth-app" not in bundle["fleet"]["hosts"]
    assert bundle["manifest"]["scope"]["environments"] == ["testing"]
    with pytest.raises(BundleError):
        _build(context, environments=["nope"])


async def test_the_page_downloads_the_export_with_the_header_token(web):
    client, state = web
    response = await client.get("/api/settings/export", params={"project": "lab"})
    assert response.status_code == 200, response.text
    assert response.headers["content-disposition"].startswith('attachment; filename="sshfleet-lab-')
    assert response.json()["kind"] == "sshfleet-bundle"
    assert json.loads(response.headers["x-sshfleet-warnings"]) == []
    assert state.context.audit.tail(1)[0]["action"] == "config.export"
    # The address is not a way in, here as anywhere but the two download links.
    bare = await client.get("/api/settings/export", params={"token": state.token}, headers={"x-sshfleet-token": ""})
    assert bare.status_code == 401


def test_the_cli_exports_a_folder_for_git(configured, fake_ssh, tmp_path: Path):
    from sshfleet.cli.main import app

    folder = tmp_path / "repo" / "sshfleet"
    result = CliRunner().invoke(app, ["config", "export", "--out", str(folder)])
    assert result.exit_code == 0, result.output
    assert (folder / "ssh" / "test-env").exists() and (folder / "fleet.yaml").exists()
    printed = CliRunner().invoke(app, ["config", "export"])
    assert json.loads(printed.output)["kind"] == "sshfleet-bundle"


def test_chosen_environments_carry_their_own_projects_and_nothing_else(context, tmp_path: Path):
    """Picking two projects and then one environment exports that environment and its project —
    not the other project, empty."""
    from sshfleet.config import ProjectCfg
    from sshfleet.inventory import build_inventory

    other = tmp_path / "acme-staging"
    other.write_text("Host acme-app\n    HostName 10.8.0.1\n")
    context.config.projects.append(ProjectCfg(name="acme"))
    context.config.environments.append(EnvironmentCfg(name="Staging", ssh_config_path=other, short="stg", project="acme"))
    context.inventory = build_inventory(context.config)

    bundle, _ = _build(context, projects=["lab", "acme"], environments=["Staging"])
    assert [e["name"] for e in bundle["fleet"]["environments"]] == ["Staging"]
    assert [p["name"] for p in bundle["fleet"]["projects"]] == ["acme"]
    assert bundle["manifest"]["scope"] == {"projects": ["acme"], "environments": ["Staging"]}
    whole, _ = _build(context, projects=["acme"])
    assert [p["name"] for p in whole["fleet"]["projects"]] == ["acme"] and list(whole["ssh"]) == ["acme-staging"]
