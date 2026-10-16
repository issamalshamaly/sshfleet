"""History: every state the configuration has been in, kept privately and without keys."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from sshfleet import history as history_module
from sshfleet.history import History, TrackedFile


def _states(root: Path) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted((root / "states").glob("*.json"))]


async def _wait_for(predicate, timeout: float = 6.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.1)
    return predicate()


async def test_every_settings_change_leaves_one_state(web):
    client, state = web
    before = len(state.history._ids())
    response = await client.patch("/api/settings/hosts/tst-order", json={"display": "Orders API"})
    assert response.status_code == 200, response.text
    ids = state.history._ids()
    assert len(ids) == before + 1
    latest = state.history.latest()
    assert latest.source == "web"
    assert "hosts.tst-order" in latest.changes["config"]
    # The reload that followed reports the state it left behind and the one to go back to.
    assert response.json()["reload"]["state"] == latest.id
    assert response.json()["reload"]["previous_state"] == ids[-2]


async def test_an_unchanged_reload_records_nothing(web, monkeypatch):
    _, state = web
    monkeypatch.setattr(state, "wake", lambda: None)
    await state.reload(reason="first")
    count = len(state.history._ids())
    await state.reload(reason="again")
    await state.reload(reason="and again")
    assert len(state.history._ids()) == count


async def test_a_hand_edit_is_recorded_as_changed_on_disk(web, sample_env_file):
    """The watcher notices an edit made in a text editor and keeps it, labelled as such."""
    _, state = web
    count = len(state.history._ids())
    sample_env_file.write_text(sample_env_file.read_text() + "\nHost tst-handmade\n    HostName 10.9.9.9\n    ProxyJump tst-bastion\n")
    assert await _wait_for(lambda: len(state.history._ids()) > count)
    latest = state.history.latest()
    assert latest.source == "watcher"
    assert "changed on disk" in latest.reason
    assert latest.changes["hosts_added"] == ["tst-handmade"]
    assert [f["path"] for f in latest.changes["files"]] == [str(sample_env_file)]


async def test_a_broken_hand_edit_is_kept_and_marked_invalid(web, configured):
    """The edit that breaks the config is the one most worth going back from."""
    _, state = web
    configured.write_text("environments: [unclosed\n")
    summary = await state.reload(reason="config.yaml changed on disk", source="watcher")
    assert summary["ok"] is False
    latest = state.history.latest()
    assert latest.id == summary["state"]
    assert latest.valid is False and "YAML" in latest.error
    config_entry = next(f for f in latest.files if f.role == "config")
    assert state.history.blob(config_entry.sha256) == b"environments: [unclosed\n"


def test_the_cli_and_the_server_record_without_racing(tmp_path: Path):
    """Two processes recording at once, each changing its own file between records: every state
    they leave is whole, and every piece of content a state names is there."""
    root = tmp_path / "history"
    script = (
        "import sys; from pathlib import Path\n"
        "from sshfleet.history import History, TrackedFile\n"
        "root, mine, other = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])\n"
        "files = [TrackedFile('config', mine), TrackedFile('ssh', other)]\n"
        "for n in range(25):\n"
        "    mine.write_text(f'{mine.name} {n}\\n')\n"
        "    History(root).record(files, reason=f'{mine.name} {n}', source='cli')\n"
    )
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_text("a start\n")
    b.write_text("b start\n")
    procs = [
        subprocess.Popen([sys.executable, "-c", script, str(root), str(a), str(b)]),
        subprocess.Popen([sys.executable, "-c", script, str(root), str(b), str(a)]),
    ]
    assert all(p.wait(timeout=60) == 0 for p in procs)
    history = History(root)
    states = _states(root)
    assert len(states) >= 25  # each process changed its file every time; most records are new
    for state in states:
        for entry in state["files"]:
            if entry["sha256"]:
                assert (root / "blobs" / entry["sha256"]).exists()
    assert len({s["id"] for s in states}) == len(states)
    assert history.latest() is not None


def test_old_states_are_pruned_and_their_blobs_collected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(history_module, "KEEP_NEWEST", 3)
    monkeypatch.setattr(history_module, "KEEP_DAYS", -1)  # nothing is young enough to keep for its age
    monkeypatch.setattr(history_module, "PRUNE_SLACK", 2)
    target = tmp_path / "config.yaml"
    history = History(tmp_path / "history")
    for n in range(12):
        target.write_text(f"version: 3\n# edit {n}\n")
        history.record([TrackedFile("config", target)], reason=f"edit {n}", source="cli")
    ids = history._ids()
    assert len(ids) <= 3 + 2
    assert history.latest().reason == "edit 11"
    referenced = {f.sha256 for i in ids for f in history.get(i).files if f.sha256}
    assert {p.name for p in history.blobs.iterdir()} == referenced


def test_history_files_are_private(tmp_path: Path):
    target = tmp_path / "config.yaml"
    target.write_text("version: 3\n")
    history = History(tmp_path / "history")
    history.record([TrackedFile("config", target)], reason="first", source="cli")
    for directory in (history.root, history.blobs, history.states):
        assert (directory.stat().st_mode & 0o777) == 0o700, directory
    for path in [*history.blobs.iterdir(), *history.states.iterdir()]:
        assert (path.stat().st_mode & 0o777) == 0o600, path


def test_a_key_file_is_never_copied_into_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """By name (never even opened) or by content (a key pasted into a file with an innocent name)."""
    from sshfleet.config import Config, EnvironmentCfg
    from sshfleet.paths import Paths

    ssh = tmp_path / "ssh"
    ssh.mkdir()
    (ssh / "id_ed25519").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nnot-a-real-key\n")
    (ssh / "notes").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nnot-a-real-key\n")
    env_file = ssh / "work"
    env_file.write_text(f"Include {ssh}/*\nHost tst-work\n    HostName 10.0.0.1\n")
    monkeypatch.setenv("SSHFLEET_HOME", str(tmp_path / "home"))
    paths = Paths.detect().ensure()
    paths.config_file.write_text("version: 3\n")
    config = Config(environments=[EnvironmentCfg(name="work", ssh_config_path=env_file)])

    state, _ = history_module.record_now(config, paths, reason="first", source="cli")
    tracked = {Path(f.path).name: f for f in state.files}
    assert "id_ed25519" not in tracked  # skipped by name before anything opened it
    assert tracked["notes"].skipped == "holds key material" and tracked["notes"].sha256 is None
    for blob in (paths.history_dir / "blobs").iterdir():
        assert b"PRIVATE KEY" not in blob.read_bytes()
        assert b"not-a-real-key" not in blob.read_bytes()
    assert os.access(paths.history_dir, os.R_OK)
