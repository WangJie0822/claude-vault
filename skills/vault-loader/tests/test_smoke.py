"""Smoke：确认 fixture 工作。"""
from pathlib import Path


def test_tmp_home_isolated(tmp_home: Path) -> None:
    import os
    assert os.environ["HOME"] == str(tmp_home)
    assert (tmp_home / ".claude").is_dir()
    assert (tmp_home / "Vault" / ".meta").is_dir()


def test_write_frontmatter_cache(tmp_vault: Path, write_frontmatter_cache) -> None:
    p = write_frontmatter_cache({"foo/bar.md": {"tags": ["a"]}})
    assert p.exists()
    import json
    data = json.loads(p.read_text())
    assert data["entries"]["foo/bar.md"]["tags"] == ["a"]


def test_tmp_git_repo(tmp_git_repo: Path) -> None:
    import subprocess
    r = subprocess.run(
        ["git", "log", "--oneline"], cwd=tmp_git_repo, capture_output=True, text=True
    )
    assert r.returncode == 0
    assert "init" in r.stdout
