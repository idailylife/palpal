import sys

import pytest

from scripts import fetch_assets


def write_required_assets(path):
    path.mkdir(parents=True, exist_ok=True)
    for name in fetch_assets.REQUIRED:
        (path / name).write_bytes(b"asset")


def test_has_required_assets_requires_every_file(tmp_path):
    target = tmp_path / "pal"

    assert not fetch_assets.has_required_assets(target)
    write_required_assets(target)
    assert fetch_assets.has_required_assets(target)
    (target / fetch_assets.REQUIRED[0]).unlink()
    assert not fetch_assets.has_required_assets(target)


def test_main_returns_without_clone_when_assets_already_exist(tmp_path, monkeypatch, capsys):
    target = tmp_path / "pal"
    write_required_assets(target)

    def fail_clone(command):
        raise AssertionError(f"unexpected clone: {command}")

    monkeypatch.setattr(sys, "argv", ["fetch_assets.py", "--target", str(target)])
    monkeypatch.setattr(fetch_assets.subprocess, "check_call", fail_clone)

    assert fetch_assets.main() == 0
    assert "assets already present" in capsys.readouterr().out


def test_main_clones_and_copies_required_assets(tmp_path, monkeypatch):
    target = tmp_path / "target" / "pal"
    calls = []

    def fake_clone(command):
        calls.append(command)
        clone_dir = command[-1]
        write_required_assets(fetch_assets.Path(clone_dir) / "pal")

    monkeypatch.setattr(sys, "argv", ["fetch_assets.py", "--target", str(target), "--repo", "https://example.test/repo.git"])
    monkeypatch.setattr(fetch_assets.subprocess, "check_call", fake_clone)

    assert fetch_assets.main() == 0
    assert fetch_assets.has_required_assets(target)
    assert calls == [["git", "clone", "--depth", "1", "https://example.test/repo.git", calls[0][-1]]]


def test_main_reports_missing_files_from_cloned_repo(tmp_path, monkeypatch):
    target = tmp_path / "target" / "pal"

    def fake_clone(command):
        source = fetch_assets.Path(command[-1]) / "pal"
        source.mkdir(parents=True)
        (source / fetch_assets.REQUIRED[0]).write_bytes(b"asset")

    monkeypatch.setattr(sys, "argv", ["fetch_assets.py", "--target", str(target)])
    monkeypatch.setattr(fetch_assets.subprocess, "check_call", fake_clone)

    with pytest.raises(SystemExit, match="asset repo is missing required files"):
        fetch_assets.main()


def test_main_replaces_existing_incomplete_target(tmp_path, monkeypatch):
    target = tmp_path / "pal"
    target.mkdir()
    (target / "old.txt").write_text("old", encoding="utf-8")

    def fake_clone(command):
        write_required_assets(fetch_assets.Path(command[-1]) / "pal")

    monkeypatch.setattr(sys, "argv", ["fetch_assets.py", "--target", str(target)])
    monkeypatch.setattr(fetch_assets.subprocess, "check_call", fake_clone)

    assert fetch_assets.main() == 0
    assert fetch_assets.has_required_assets(target)
    assert not (target / "old.txt").exists()
