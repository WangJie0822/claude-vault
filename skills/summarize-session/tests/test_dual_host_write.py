"""双宿主共享真实临时 Vault；进程交错由文件 barrier 控制。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

import rebuild_index as rebuild
import git_commit_vault as commits
from archive_doc import archive_doc

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _wait(path, proc=None):
    deadline = time.monotonic() + 30
    while not path.exists():
        assert proc is None or proc.poll() is None, "child exited before barrier"
        assert time.monotonic() < deadline, "barrier timed out"
        time.sleep(.02)


def _note(vault, name):
    p = vault / "notes" / (name + ".md")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\ncategory: notes\nsummary: shared vault synthetic design\n---\n# " + name, encoding="utf-8")


def test_rebuild_serializes_scan_through_index_and_cache_commit(tmp_path):
    vault = tmp_path / "vault"; vault.mkdir()
    _note(vault, "a")
    (vault / "CLAUDE.md").write_text("<!-- 索引区 -->\n<!-- /索引区 -->\n", encoding="utf-8")
    child = tmp_path / "rebuild-child.py"
    child.write_text('''import sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import rebuild_index as r
vault,kind,barrier=Path(sys.argv[2]),sys.argv[3],Path(sys.argv[4])
original=r.scan_vault
def scan(*args,**kwargs):
 result=original(*args,**kwargs)
 if kind=='a':
  (barrier/'a-scanned').touch()
  end=time.monotonic()+30
  while not (barrier/'release-a').exists():
   if time.monotonic()>end:raise RuntimeError('barrier timeout')
   time.sleep(.02)
 return result
r.scan_vault=scan
(barrier/(kind+'-started')).touch()
sys.argv=['rebuild','--vault',str(vault),'--emit','all']
r.main()
''', encoding="utf-8")
    env = {**os.environ, "PYTHONUTF8": "1"}
    a = subprocess.Popen([sys.executable, str(child), str(SCRIPTS), str(vault), "a", str(tmp_path)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    b = None
    try:
        _wait(tmp_path / "a-scanned", a)
        _note(vault, "b")
        b = subprocess.Popen([sys.executable, str(child), str(SCRIPTS), str(vault), "b", str(tmp_path)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        _wait(tmp_path / "b-started", b)
        # 若 A 正持事务锁，释放它让 B 重读；旧实现无锁时强制 B 先提交。
        if (vault / ".meta/frontmatter-cache.json.lock").exists():
            (tmp_path / "release-a").touch()
        else:
            out, err = b.communicate(timeout=30)
            assert b.returncode == 0, out + err
            (tmp_path / "release-a").touch()
        ao, ae = a.communicate(timeout=30); bo, be = b.communicate(timeout=30)
        assert a.returncode == b.returncode == 0, (ao, ae, bo, be)
    finally:
        (tmp_path / "release-a").touch()
        for p in (a, b):
            if p is not None and p.poll() is None:
                p.terminate(); p.wait(timeout=5)
    cache = json.loads((vault / ".meta/frontmatter-cache.json").read_text(encoding="utf-8"))
    assert {"notes/a.md", "notes/b.md"} <= set(cache["entries"])
    index = (vault / "notes/notes 索引.md").read_text(encoding="utf-8")
    assert "| [[a]] |" in index and "| [[b]] |" in index
    assert "## notes    2 篇" in (vault / "CLAUDE.md").read_text(encoding="utf-8")


def test_lock_failure_preserves_all_outputs_and_exits_unsuccessfully(tmp_path, monkeypatch):
    _note(tmp_path, "a")
    index = tmp_path / "CLAUDE.md"
    index.write_text("<!-- 索引区 -->\noriginal\n<!-- /索引区 -->\n", encoding="utf-8")
    before = index.read_bytes()
    monkeypatch.setattr(rebuild, "_acquire_lock", lambda *a, **kw: False)
    monkeypatch.setattr(sys, "argv", ["rebuild", "--vault", str(tmp_path), "--emit", "all"])
    code = 0
    try:
        rebuild.main()
    except SystemExit as exc:
        code = exc.code
    assert code != 0
    assert index.read_bytes() == before
    assert not (tmp_path / ".meta/frontmatter-cache.json").exists()
    assert not (tmp_path / "notes/notes 索引.md").exists()


@pytest.mark.parametrize("part", ["auth.json", "config.toml", "sessions/feature-spec.md", "archived_sessions/design-plan.md"])
def test_codex_runtime_sources_never_archive_as_design_docs(tmp_path, monkeypatch, part):
    home = tmp_path / ".codex"
    monkeypatch.setenv("HOME", str(tmp_path)); monkeypatch.setenv("USERPROFILE", str(tmp_path))
    source = home / part; source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("# synthetic nonsecret source", encoding="utf-8")
    vault = tmp_path / "vault"; vault.mkdir()
    result = archive_doc({"path": str(source), "type": "spec", "context": "synthetic", "keywords": []}, str(vault))
    assert result["status"] == "denied_sensitive"
    assert not list(vault.rglob("*.md"))


def test_normal_vault_commit_does_not_include_unrelated_staged_file(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, timeout=15)
    for key, value in (("user.name", "synthetic"), ("user.email", "synthetic@example.invalid")):
        subprocess.run(["git", "-C", str(tmp_path), "config", key, value], check=True, timeout=15)
    (tmp_path / "unrelated.txt").write_text("keep staged", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "unrelated.txt"], check=True, timeout=15)
    _note(tmp_path, "a")
    result = commits.commit_vault(str(tmp_path), "dual host")
    assert result["status"] == "committed"
    files = subprocess.check_output(["git", "-C", str(tmp_path), "show", "--format=", "--name-only", "HEAD"], timeout=15).decode()
    assert "notes/a.md" in files and "unrelated.txt" not in files
    staged = subprocess.check_output(["git", "-C", str(tmp_path), "diff", "--cached", "--name-only"], timeout=15).decode()
    assert staged.strip() == "unrelated.txt"


def test_cache_keyword_writer_respects_shared_transaction_lock(tmp_path, monkeypatch):
    import _fs
    import keywords_gap
    _note(tmp_path, "a")
    cache = tmp_path / ".meta/frontmatter-cache.json"; cache.parent.mkdir()
    cache.write_text(json.dumps({"entries": {"notes/a.md": {"keywords": ["old"]}}}), encoding="utf-8")
    before = cache.read_bytes(); locks = []
    monkeypatch.setattr(_fs, "_acquire_lock", lambda path, **kw: locks.append(path) or False)
    assert keywords_gap._sync_cache_entry(tmp_path, "notes/a.md", ["new"]) is False
    assert locks == [str(cache) + ".lock"] and cache.read_bytes() == before


def test_two_host_scoped_commits_keep_both_notes_and_unrelated_index(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, timeout=15)
    for key, value in (("user.name", "synthetic"), ("user.email", "synthetic@example.invalid")):
        subprocess.run(["git", "-C", str(tmp_path), "config", key, value], check=True, timeout=15)
    (tmp_path / "unrelated.txt").write_text("keep staged", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "unrelated.txt"], check=True, timeout=15)
    for name in ("a", "b"):
        _note(tmp_path, name)
    command = "import sys,json;sys.path.insert(0,sys.argv[1]);import git_commit_vault as g;print(json.dumps(g.commit_vault(sys.argv[2],sys.argv[3],paths=['notes/'+sys.argv[3]+'.md'])))"
    processes = [subprocess.Popen([sys.executable, "-c", command, str(SCRIPTS), str(tmp_path), name],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE) for name in ("a", "b")]
    try:
        for process in processes:
            out, err = process.communicate(timeout=30)
            assert process.returncode == 0 and json.loads(out)["status"] == "committed", (out, err)
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate(); process.wait(timeout=5)
    files = subprocess.check_output(["git", "-C", str(tmp_path), "ls-tree", "-r", "--name-only", "HEAD"], timeout=15).decode()
    assert set(files.splitlines()) == {"notes/a.md", "notes/b.md"}
    assert all((tmp_path / "notes" / (name + ".md")).exists() for name in ("a", "b"))
    staged = subprocess.check_output(["git", "-C", str(tmp_path), "diff", "--cached", "--name-only"], timeout=15).decode()
    assert staged.strip() == "unrelated.txt"
