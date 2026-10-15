import json
from pathlib import Path
import subprocess

import pytest

from cplane import state
from cplane.cli import change_for, main


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True, text=True, capture_output=True).stdout.strip()


@pytest.fixture
def git_state(tmp_path, slice_data, monkeypatch):
    # Avoid host identity, hooks, signing and global settings in these fixtures.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Fixture")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "fixture@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Fixture")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "fixture@example.invalid")
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)], check=True, capture_output=True)
    local = tmp_path / "local"
    subprocess.run(["git", "clone", str(remote), str(local)], check=True, capture_output=True)
    path = local / "slices.json"
    path.write_text(json.dumps({"version": 1, "slices": [slice_data]}))
    (local / ".gitignore").write_text("*.lock\n")
    git(local, "add", ".")
    git(local, "commit", "-m", "fixture")
    git(local, "push", "-u", "origin", "main")
    other = tmp_path / "other"
    subprocess.run(["git", "clone", str(remote), str(other)], check=True, capture_output=True)
    return path, remote, other


def test_commit_and_push_after_other_clone(git_state):
    path, remote, other = git_state
    (other / "other.txt").write_text("another writer\n")
    git(other, "add", "other.txt")
    git(other, "commit", "-m", "other commit")
    git(other, "push")
    assert main(["--state", str(path), "--commit", "slot", "voice/W1", "2026-10-19T03:00:00+02:00"]) == 0
    assert git(path.parent, "log", "-1", "--format=%s") == "cplane: slot voice/W1"
    assert git(remote, "rev-parse", "main") == git(path.parent, "rev-parse", "HEAD")
    assert (path.parent / "other.txt").read_text() == "another writer\n"
    assert set(git(path.parent, "show", "--format=", "--name-only", "HEAD").splitlines()) == {"slices.json", "slices.json.outbox.jsonl"}
    assert json.loads(git(remote, "show", "main:slices.json"))["slices"][0]["slot"]


def test_commit_without_outbox(git_state):
    path, remote, _ = git_state
    assert main(["--state", str(path), "pause", "voice/W1", "--commit"]) == 0
    assert git(remote, "log", "-1", "--format=%s") == "cplane: pause voice/W1"


def test_rebase_conflict_retains_local_commit(git_state, capsys):
    path, remote, other = git_state
    data = json.loads((other / "slices.json").read_text())
    data["slices"][0]["paused"] = True
    (other / "slices.json").write_text(json.dumps(data))
    git(other, "add", "slices.json")
    git(other, "commit", "-m", "competing state change")
    git(other, "push")
    assert main(["--state", str(path), "--commit", "start", "voice/W1"]) == 1
    err = capsys.readouterr().err
    assert "local commit retained" in err
    assert len(err.splitlines()) == 1
    assert git(path.parent, "log", "-1", "--format=%s") == "cplane: start voice/W1"
    assert state.load(path)["slices"][0]["slot"]
    assert git(path.parent, "diff", "--name-only", "--diff-filter=U") == ""
    assert git(remote, "log", "-1", "--format=%s") == "competing state change"


@pytest.mark.parametrize("failures", [2, 3])
def test_push_retries(git_state, monkeypatch, failures):
    path, remote, _ = git_state
    original = state.git
    calls = {"push": 0, "pull": 0}
    def flaky(root, *args):
        if args[0] in calls:
            calls[args[0]] += 1
        if args[0] == "push" and calls["push"] <= failures:
            raise state.StateError("simulated concurrent push rejection")
        return original(root, *args)
    monkeypatch.setattr(state, "git", flaky)
    if failures == 3:
        with pytest.raises(state.StateError, match="after 3 attempts; local commit retained"):
            state.update(path, "voice/W1", "start", "owner", change_for("start"), commit=True)
    else:
        state.update(path, "voice/W1", "start", "owner", change_for("start"), commit=True)
        assert git(remote, "rev-parse", "main") == git(path.parent, "rev-parse", "HEAD")
    assert calls == {"push": 3, "pull": 3}


def test_commit_outside_git(make_state):
    path = make_state()
    state.update(path, "voice/W1", "pause", "owner", change_for("pause"), commit=True)
    assert state.load(path)["slices"][0]["paused"]


def test_unrelated_staged_changes_not_committed(git_state):
    path, remote, _ = git_state
    (path.parent / "unrelated.txt").write_text("unrelated\n")
    git(path.parent, "add", "unrelated.txt")
    with pytest.raises(state.StateError, match="local commit retained"):
        state.update(path, "voice/W1", "pause", "owner", change_for("pause"), commit=True)
    assert git(path.parent, "show", "--format=", "--name-only", "HEAD") == "slices.json"
    assert git(path.parent, "diff", "--cached", "--name-only") == "unrelated.txt"
    assert git(remote, "log", "-1", "--format=%s") == "fixture"


@pytest.mark.parametrize("failures", [2, 3])
def test_pull_retries(git_state, monkeypatch, failures):
    path, _, _ = git_state
    original = state.git
    attempts = 0
    def flaky(root, *args):
        nonlocal attempts
        if args[0] == "pull":
            attempts += 1
            if attempts <= failures:
                raise state.StateError("simulated pull transport failure")
        return original(root, *args)
    monkeypatch.setattr(state, "git", flaky)
    if failures == 3:
        with pytest.raises(state.StateError, match="local commit retained"):
            state.update(path, "voice/W1", "pause", "owner", change_for("pause"), commit=True)
    else:
        state.update(path, "voice/W1", "pause", "owner", change_for("pause"), commit=True)
    assert attempts == 3
