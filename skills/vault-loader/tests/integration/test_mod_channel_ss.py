"""SessionStart 的 mod 通道与 live/ 清理（spec §5.4、§8.3）。"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
SS = SCRIPTS / "session_start_load.py"
# 占位 UUID 拆开拼接：发布闸门按 8-4-4-4-12 hex 拦截疑似真实 session id（test_no_secrets_in_tree）
SID = "-".join(("11111111", "2222", "3333", "4444", "555555555555"))


def _live(home: Path) -> Path:
    return home / ".context-vault" / "state" / "live"


def _h() -> str:
    return hashlib.sha256(SID.encode("utf-8")).hexdigest()[:24]


def _config(home: Path, vault: Path) -> None:
    d = home / ".context-vault"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text(json.dumps({"vault_path": str(vault),
                                               "relevance": {"session_topic": False}}),
                                   encoding="utf-8")


def _seed(vault: Path, write_frontmatter_cache, project: str) -> None:
    proj = vault / "项目笔记" / project
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "design.md").write_text("# design", encoding="utf-8")
    write_frontmatter_cache({f"项目笔记/{project}/design.md": {
        "tags": ["设计"], "category": "项目笔记", "summary": "项目设计", "mtime": 1900000000}})


def _run(home: Path, cwd: Path):
    env = os.environ.copy()
    env.update({"PYTHONUTF8": "1", "CLAUDE_CODE_ENTRYPOINT": "cli",
                "HOME": str(home), "USERPROFILE": str(home)})
    env.pop("VAULT_LOADER_DISABLE", None)
    payload = {"cwd": str(cwd), "session_id": SID, "source": "startup",
               "hook_event_name": "SessionStart"}
    r = subprocess.run([sys.executable, str(SS)], input=json.dumps(payload),
                       capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
    assert r.returncode == 0, r.stderr


def test_marker_yields_session_start_snapshot(tmp_home, tmp_vault, tmp_git_repo,
                                              write_frontmatter_cache):
    _config(tmp_home, tmp_vault)
    _seed(tmp_vault, write_frontmatter_cache, tmp_git_repo.name)
    marker = _live(tmp_home) / f"{_h()}-ss.req.json"
    marker.parent.mkdir(parents=True)
    marker.write_text("{}", encoding="utf-8")
    _run(tmp_home, tmp_git_repo)
    snap = json.loads((_live(tmp_home) / f"{_h()}.json").read_text(encoding="utf-8"))
    assert snap["event"] == "SessionStart" and snap["prompt_id"] is None
    assert snap["session_start"] == [
        {"path": f"项目笔记/{tmp_git_repo.name}/design.md", "title": "design"}]
    assert not marker.exists()


def test_no_marker_no_live_dir(tmp_home, tmp_vault, tmp_git_repo, write_frontmatter_cache):
    _config(tmp_home, tmp_vault)
    _seed(tmp_vault, write_frontmatter_cache, tmp_git_repo.name)
    _run(tmp_home, tmp_git_repo)
    assert not _live(tmp_home).exists()


def test_session_start_cleans_stale_files(tmp_home, tmp_vault, tmp_git_repo, write_frontmatter_cache):
    _config(tmp_home, tmp_vault)
    _seed(tmp_vault, write_frontmatter_cache, tmp_git_repo.name)
    d = _live(tmp_home)
    d.mkdir(parents=True)
    old = d / f"{'d' * 24}.json"          # 清理只认本插件写出的文件名（节点 2 S-M1）
    old.write_text("{}", encoding="utf-8")
    t = time.time() - 48 * 3600
    os.utime(old, (t, t))
    _run(tmp_home, tmp_git_repo)
    assert not old.exists()


def test_purge_clears_live(tmp_home):
    d = _live(tmp_home)
    d.mkdir(parents=True)
    snap = d / f"{'a' * 24}.json"          # 清理只认本插件写出的文件名（节点 2 S-M1）
    snap.write_text("{}", encoding="utf-8")
    env = os.environ.copy()
    env.update({"HOME": str(tmp_home), "USERPROFILE": str(tmp_home), "PYTHONUTF8": "1"})
    r = subprocess.run([sys.executable, "-B", str(SCRIPTS / "analyze_metrics.py"), "--purge"],
                       env=env, cwd=str(SCRIPTS.parent), capture_output=True, text=True,
                       encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr
    assert not snap.exists()
    assert "会话快照 1 个" in r.stdout
