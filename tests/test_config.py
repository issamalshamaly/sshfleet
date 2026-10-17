"""Config loading, saving and environment registration."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from sshfleet import envsetup
from sshfleet.config import Config, ConfigError, EnvironmentCfg, HostOverride
from sshfleet.config import loader as config_loader
from sshfleet.paths import Paths

SAMPLE = Path(__file__).parent / "sshconfigs" / "sample-env"


def test_missing_config_is_empty_but_valid(tmp_path: Path):
    """The tool ships with no fleet knowledge: services come from the profile in the file."""
    config = config_loader.load(tmp_path / "absent.yaml")
    assert config.environments == []
    assert config.hosts == {}
    assert config.probes.discovered_path == "/"
    assert config.logs.display_tz == "local"


def test_round_trip_preserves_environments(tmp_path: Path):
    path = tmp_path / "config.yaml"
    config = Config(
        environments=[
            EnvironmentCfg(
                name="production",
                ssh_config_path=SAMPLE,
                short="prod",
                color="red",
                key_path=Path("~/.ssh/x.pem").expanduser(),
            )
        ]
    )
    config_loader.save(config, path)
    reloaded = config_loader.load(path)
    assert [e.name for e in reloaded.environments] == ["production"]
    assert reloaded.environment("prod").color == "red"  # short name resolves too
    assert reloaded.environment("production").key_path == Path("~/.ssh/x.pem").expanduser()


def test_saved_config_is_private_and_backed_up(tmp_path: Path):
    path = tmp_path / "config.yaml"
    config = Config()
    config_loader.save(config, path)
    assert path.stat().st_mode & 0o777 == 0o600
    config.hosts["later"] = HostOverride(label="later")
    config_loader.save(config, path)
    backup = path.with_suffix(".yaml.bak")
    assert backup.exists()
    assert "later" not in (yaml.safe_load(backup.read_text()).get("hosts") or {})


def test_a_host_record_is_written_in_full(tmp_path: Path):
    """Hosts are the only place fleet knowledge lives, and it round-trips whole."""
    path = tmp_path / "config.yaml"
    config = Config()
    config.hosts["prod-api"] = HostOverride(
        main=True, label="api", role="service", workloads="api-*", log_format="pino_pretty",
        health={"port": 3000, "path": "/health"}, expected_count=2, ignored_containers=["old-api"],
    )
    config_loader.save(config, path)
    written = yaml.safe_load(path.read_text())
    assert written["version"] == 3
    assert "services" not in written and "main_services" not in written and "naming" not in written
    assert written["hosts"]["prod-api"]["workloads"] == "api-*"

    reloaded = config_loader.load(path).hosts["prod-api"]
    assert reloaded.main is True and reloaded.label == "api" and reloaded.expected_count == 2
    assert reloaded.health == {"port": 3000, "path": "/health"}
    assert reloaded.ignored_containers == ["old-api"]


def test_an_old_config_is_upgraded_once_and_atomically(tmp_path: Path):
    """A version-1 file walks all the way to the current version, keeping what it said."""
    from sshfleet.config import migrate

    path = tmp_path / "config.yaml"
    path.write_text(
        "version: 1\n"
        "environments: []\n"
        "services:\n  wallet:\n    role: service\n    port: 8080\n    runtime: podman\n"
        "refresh:\n  fast_s: 60\n"
    )
    note = migrate.upgrade(path)
    assert note and "version 3" in note
    upgraded = yaml.safe_load(path.read_text())
    assert upgraded["version"] == 3
    assert "services" not in upgraded and "main_services" not in upgraded and "naming" not in upgraded
    assert config_loader.load(path).refresh.fast_s == 60
    assert path.with_suffix(".yaml.bak").exists()

    assert migrate.upgrade(path) is None  # already current
    fresh = tmp_path / "never-existed.yaml"
    assert migrate.upgrade(fresh) is None and not fresh.exists()


def test_bad_yaml_names_the_file(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text("environments: [\n")
    with pytest.raises(ConfigError, match="invalid YAML"):
        config_loader.load(path)


def test_duplicate_environment_is_rejected(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "environments": [
                    {"name": "prod", "ssh_config_path": str(SAMPLE)},
                    {"name": "prod", "ssh_config_path": str(SAMPLE)},
                ]
            }
        )
    )
    with pytest.raises(ConfigError, match="duplicate environment"):
        config_loader.load(path)


def test_unknown_environment_lists_the_known_ones(tmp_path: Path):
    config = config_loader.load(tmp_path / "absent.yaml")
    config.environments = [EnvironmentCfg(name="staging", ssh_config_path=SAMPLE)]
    with pytest.raises(ConfigError, match="known: staging"):
        config.environment("production")


def test_unknown_keys_survive_a_round_trip(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "future_feature": {"enabled": True}}))
    config = config_loader.load(path)
    config_loader.save(config, path)
    assert yaml.safe_load(path.read_text())["future_feature"] == {"enabled": True}


# ------------------------------------------------------------------------ projects


def test_a_project_round_trips_and_groups_its_environments(tmp_path: Path):
    path = tmp_path / "config.yaml"
    config = Config()
    envsetup.add_project(config, name="acme", color="violet", description="Customer platform")
    envsetup.add_environment(config, name="production", ssh_config_path=SAMPLE, project="acme")
    config_loader.save(config, path)

    written = yaml.safe_load(path.read_text())
    assert written["projects"] == [{"name": "acme", "color": "violet", "description": "Customer platform"}]
    assert written["environments"][0]["project"] == "acme"

    reloaded = config_loader.load(path)
    assert reloaded.project("acme").color == "violet"
    assert reloaded.environment("production").project == "acme"
    assert [e.name for e in reloaded.environments_of("acme")] == ["production"]


def test_a_config_with_no_projects_writes_no_projects_key(tmp_path: Path):
    """The tool has no opinion about projects until one is created."""
    path = tmp_path / "config.yaml"
    config_loader.save(Config(), path)
    assert "projects" not in yaml.safe_load(path.read_text())
    assert config_loader.load(path).projects == []


def test_a_duplicate_project_is_rejected(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"projects": [{"name": "acme"}, {"name": "acme"}]}))
    with pytest.raises(ConfigError, match="duplicate project"):
        config_loader.load(path)


def test_a_file_that_predates_projects_is_repaired_when_it_loads(tmp_path: Path):
    """Every environment belongs to a project, so an older file is brought up to that shape
    rather than rejected — the operator renames the project afterwards."""
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "environments": [
                    {"name": "staging", "ssh_config_path": str(SAMPLE)},
                    {"name": "production", "ssh_config_path": str(SAMPLE)},
                ]
            }
        )
    )
    config = config_loader.load(path)
    assert [p.name for p in config.projects] == ["default"]
    assert [e.project for e in config.environments] == ["default", "default"]


def test_an_environment_naming_a_project_with_no_record_declares_it(tmp_path: Path):
    """A hand-edited file still loads, and config.projects is always the whole list."""
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {"environments": [{"name": "prod", "ssh_config_path": str(SAMPLE), "project": "ghost"}]}
        )
    )
    config = config_loader.load(path)
    assert [p.name for p in config.projects] == ["ghost"]
    assert config.environment("prod").project == "ghost"


def test_an_environment_cannot_be_added_without_a_project(tmp_path: Path):
    config = config_loader.load(tmp_path / "absent.yaml")
    with pytest.raises(ConfigError, match="belongs to a project"):
        envsetup.add_environment(config, name="production", ssh_config_path=SAMPLE)
    with pytest.raises(ConfigError, match="unknown project"):
        envsetup.add_environment(config, name="production", ssh_config_path=SAMPLE, project="ghost")
    assert config.environments == []


def test_a_project_holding_environments_cannot_be_removed(tmp_path: Path):
    """Removing it would leave an environment belonging to nothing."""
    config = Config()
    envsetup.add_project(config, name="acme")
    envsetup.add_environment(config, name="production", ssh_config_path=SAMPLE, project="acme")
    with pytest.raises(ConfigError, match="still holds production"):
        envsetup.remove_project(config, "acme")

    envsetup.remove_environment(config, "production")
    envsetup.remove_project(config, "acme")
    assert config.projects == []


def test_renaming_a_project_repoints_every_environment(tmp_path: Path):
    config = Config()
    envsetup.add_project(config, name="acme")
    envsetup.add_environment(config, name="production", ssh_config_path=SAMPLE, project="acme")
    envsetup.rename_project(config, "acme", "acme-platform")
    assert config.environment("production").project == "acme-platform"
    with pytest.raises(ConfigError, match="unknown project"):
        envsetup.rename_project(config, "acme", "whatever")


# ---------------------------------------------------------------- key checks


def test_key_check_reads_metadata_only(tmp_path: Path):
    key = tmp_path / "id.pem"
    key.write_text("PRIVATE KEY CONTENT THAT MUST NEVER BE READ")
    key.chmod(0o600)
    result = envsetup.check_key(key)
    assert result.ok
    assert result.mode == 0o600


def test_key_check_flags_loose_permissions(tmp_path: Path):
    key = tmp_path / "id.pem"
    key.write_text("x")
    key.chmod(0o644)
    result = envsetup.check_key(key)
    assert not result.ok
    assert "too open" in result.problems[0]
    assert result.fix == f"chmod 600 {key}"


def test_key_check_reports_a_missing_file(tmp_path: Path):
    result = envsetup.check_key(tmp_path / "nope.pem")
    assert not result.ok
    assert result.exists is False


def test_key_is_never_opened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    key = tmp_path / "id.pem"
    key.write_text("secret")
    key.chmod(0o600)

    real_open = os.open

    def guard(path, *args, **kwargs):
        if str(path) == str(key):
            raise AssertionError("sshfleet must never open a private key")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", guard)
    assert envsetup.check_key(key).ok


# ------------------------------------------------------------ env registration


def test_preview_suggests_name_prefix_and_bastions():
    result = envsetup.preview(SAMPLE)
    assert result.prefix == "tst-"
    assert result.bastions == ["tst-bastion", "tst-jenkins-proxy"]
    assert len(result.aliases) == 8


def test_add_environment_refuses_a_duplicate_without_replace(tmp_path: Path):
    config = config_loader.load(tmp_path / "absent.yaml")
    envsetup.add_project(config, name="acme")
    envsetup.add_environment(config, name="production", ssh_config_path=SAMPLE, project="acme")
    with pytest.raises(ConfigError, match="already exists"):
        envsetup.add_environment(config, name="production", ssh_config_path=SAMPLE, project="acme")
    envsetup.add_environment(config, name="production", ssh_config_path=SAMPLE, project="acme", replace=True)
    assert len(config.environments) == 1


def test_preview_of_an_empty_or_missing_file_needs_allow_empty(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.write_text("# nothing here\n")
    with pytest.raises(ConfigError, match="no usable Host entries"):
        envsetup.preview(empty)
    result = envsetup.preview(empty, allow_empty=True)
    assert result.aliases == [] and result.bastions == [] and result.suggested_name == "empty"

    missing = tmp_path / "production"
    with pytest.raises(FileNotFoundError):
        envsetup.preview(missing)
    assert envsetup.preview(missing, allow_empty=True).suggested_color == "red"


def test_add_environment_can_create_the_file(tmp_path: Path):
    config = config_loader.load(tmp_path / "absent.yaml")
    envsetup.add_project(config, name="acme")
    target = tmp_path / "config.d" / "lab"
    with pytest.raises(FileNotFoundError):
        envsetup.add_environment(config, name="lab", ssh_config_path=target, project="acme")
    env = envsetup.add_environment(config, name="lab", ssh_config_path=target, project="acme", create_file=True)
    assert env.ssh_config_path == target and target.exists()
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.read_text().startswith("# lab:")


# --------------------------------------------------------------------- paths


def test_paths_prefer_the_runtime_dir_for_sockets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    runtime = tmp_path / "run"
    runtime.mkdir()
    monkeypatch.delenv("SSHFLEET_HOME", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    paths = Paths.detect()
    assert paths.control_dir == runtime / "sshfleet"


def test_paths_fall_back_to_cache_without_a_runtime_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("SSHFLEET_HOME", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    paths = Paths.detect()
    assert paths.control_dir == tmp_path / "cache" / "sshfleet" / "mux"


def test_directories_are_created_private(tmp_path: Path):
    paths = Paths.detect(tmp_path / "home").ensure()
    assert paths.config_dir.stat().st_mode & 0o777 == 0o700
    assert paths.control_dir.is_dir()


def test_a_config_with_host_overrides_round_trips(tmp_path):
    """Regression: HostOverride is slotted, so saving used to raise on vars()."""
    from sshfleet.config.schema import HostOverride

    config = config_loader.load(tmp_path / "missing.yaml")
    config.hosts["tst-order"] = HostOverride(display="order box", label="order", tags=["edge"])
    path = tmp_path / "config.yaml"
    config_loader.save(config, path)

    again = config_loader.load(path)
    assert again.hosts["tst-order"].display == "order box"
    assert again.hosts["tst-order"].tags == ["edge"]


def test_saving_through_a_symlink_keeps_the_link(tmp_path: Path):
    real = tmp_path / "dotfiles" / "config.yaml"
    real.parent.mkdir()
    link = tmp_path / "config" / "config.yaml"
    link.parent.mkdir()
    config_loader.save(Config(), real)
    link.symlink_to(real)
    config_loader.save(Config(environments=[]), link)
    assert link.is_symlink()
    assert (real.stat().st_mode & 0o777) == 0o600


@pytest.mark.parametrize(
    "text",
    [
        "environments: [{ssh_config_path: /x}]",  # an environment with no name
        "environments: [plain-string]",  # a list item that is not a mapping
        "tunnels: {db: {env: e, via: h, remote: 'r:1', local: not-a-port}}",
        "just a string",
        "a: [unclosed",
    ],
)
def test_every_way_a_config_can_be_wrong_is_a_config_error(text: str):
    """Restore and import check before they write, and they can only check for what they catch."""
    with pytest.raises(ConfigError):
        config_loader.parse_text(text)


@pytest.mark.parametrize("port", ["1;id", "$(id)", "3001 ", -1, 65536, True])
def test_a_health_port_that_is_not_a_port_is_refused(port):
    """The port ends up in a script on the host (`bash -c "…/dev/tcp/127.0.0.1/PORT"`), so a
    hand-edited file or an imported bundle must not be able to put anything else there."""
    with pytest.raises(ConfigError, match=r"hosts\.tst-order: health\.port"):
        config_loader.parse_mapping({"hosts": {"tst-order": {"health": {"port": port, "path": "/"}}}})


def test_a_health_port_written_as_text_is_read_as_a_number():
    config = config_loader.parse_mapping({"hosts": {"tst-order": {"health": {"port": "3001", "path": "/"}}}})
    assert config.hosts["tst-order"].health == {"port": 3001, "path": "/"}


def test_the_example_config_is_a_valid_config():
    """examples/config.yaml is documentation people copy from; it must stay loadable."""
    root = Path(__file__).parents[1]
    config = config_loader.parse_text((root / "examples" / "config.yaml").read_text(), source="examples/config.yaml")
    assert [e.name for e in config.environments] == ["staging"]
    assert config.hosts["acme-stg-api"].health == {"port": 3000, "path": "/health"}


def test_the_example_ssh_file_reads_as_one_bastion_and_four_hosts():
    from sshfleet.inventory import build_inventory

    root = Path(__file__).parents[1]
    config = Config(environments=[EnvironmentCfg(name="staging", ssh_config_path=root / "examples" / "ssh-config.d" / "acme-staging")])
    inventory = build_inventory(config)
    assert inventory.environment("staging").bastions == ["acme-stg-bastion"]
    assert len(inventory.hosts("staging")) == 5


def _parsed(text: str) -> Config:
    return config_loader.parse_text("version: 2\n" + text)


def test_a_setting_of_the_wrong_kind_is_named_when_the_file_is_read():
    """It used to load as given and fail later: `fast_s: abc` became an endless "refresh failed"."""
    with pytest.raises(ConfigError, match=r"refresh\.fast_s must be a whole number"):
        _parsed("refresh:\n  fast_s: abc\n")
    with pytest.raises(ConfigError, match=r"refresh\.fast_s must be at least 1"):
        _parsed("refresh:\n  fast_s: 0\n")
    with pytest.raises(ConfigError, match=r"concurrency\.global must be at least 1"):
        _parsed("concurrency:\n  global: 0\n")
    with pytest.raises(ConfigError, match=r"ssh\.compression must be true or false"):
        _parsed("ssh:\n  compression: sometimes\n")
    with pytest.raises(ConfigError, match=r"thresholds\.load_per_cpu_warn must be a number"):
        _parsed("thresholds:\n  load_per_cpu_warn: high\n")
    with pytest.raises(ConfigError, match=r"files\.deny_extra must be a list"):
        _parsed("files:\n  deny_extra: '*.secret'\n")
    with pytest.raises(ConfigError, match=r"ssh must be a mapping"):
        _parsed("ssh: [1, 2]\n")


def test_a_setting_written_loosely_still_means_what_it_says():
    config = _parsed(
        "refresh:\n  fast_s: '90'\n"
        "thresholds:\n  load_per_cpu_warn: '2'\n"
        "ssh:\n  compression: 'no'\n  control_persist: 600\n"
        "files:\n  download_dir:\n"
    )
    assert config.refresh.fast_s == 90
    assert config.thresholds.load_per_cpu_warn == 2.0
    assert config.ssh.compression is False
    assert config.ssh.control_persist == "600"
    assert config.files.download_dir == "auto", "a key left empty keeps its default"


def test_a_bare_yes_for_host_keys_is_the_word_ssh_expects(tmp_path: Path):
    """YAML reads a bare `yes` as true; written as `StrictHostKeyChecking True`, every connection
    would fail. It is what SECURITY.md tells people to write."""
    config = _parsed("ssh:\n  strict_host_key_checking: yes\n")
    assert config.ssh.strict_host_key_checking == "yes"
    assert _parsed("ssh:\n  strict_host_key_checking: Accept-New\n").ssh.strict_host_key_checking == "accept-new"
    with pytest.raises(ConfigError, match="strict_host_key_checking must be one of"):
        _parsed("ssh:\n  strict_host_key_checking: maybe\n")

    from sshfleet.ssh import MuxManager

    paths = Paths.detect(tmp_path / "home").ensure()
    assert "    StrictHostKeyChecking yes\n" in MuxManager(config, paths, None).render_config()
