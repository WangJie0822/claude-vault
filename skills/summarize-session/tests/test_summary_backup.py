"""备份 / 回滚、尝试计数、单实例锁（spec §6.3、§6.2）。"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import _summary_store as ST

V1 = "第一版摘要内容足够二十个字并且说明了问题与结论"
V2 = "第二版摘要内容同样足够二十个字并且说明了问题与结论"


def _vault(tmp_path):
    v = tmp_path / "V"
    (v / "技术笔记").mkdir(parents=True)
    (v / ".meta").mkdir()
    return v


def _cache(v, entries):
    (v / ".meta" / "frontmatter-cache.json").write_text(
        json.dumps({"_version": 1, "entries": entries}, ensure_ascii=False), encoding="utf-8")


def test_body_bytes_excludes_frontmatter(tmp_path):
    p = tmp_path / "a.md"
    p.write_bytes(b"---\na: b\n---\n" + b"x" * 250)
    assert ST.body_bytes(p) == 250
    assert ST.body_bytes(tmp_path / "missing.md") == -1


def test_new_backup_file_unique_within_same_second(tmp_path):
    v = _vault(tmp_path)
    a, b = ST.new_backup_file(v), ST.new_backup_file(v)
    assert a != b and a.exists() and b.exists()
    assert a.parent == (v / ".meta" / "summary-backups").resolve() or a.parent == v / ".meta" / "summary-backups"
    assert ST.gap_backup_file(v).name.startswith("gap-")


def test_backup_dir_refuses_redirected_directory(tmp_path):
    """备份目录被做成链到 vault 外的 junction / symlink 时拒绝，备份不得写到别处（T-M8）。"""
    import pytest
    v = _vault(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = v / ".meta" / "summary-backups"
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(outside), str(link))
    else:
        os.symlink(outside, link, target_is_directory=True)
    with pytest.raises(OSError):
        ST.backup_dir(v)


def test_prune_backups_respects_git_protect_and_names(tmp_path):
    v = _vault(tmp_path)
    d = ST.backup_dir(v)
    for i in range(25):
        (d / f"202601{i + 1:02d}-000000.jsonl").write_text("", encoding="utf-8")
    (d / "notes.txt").write_text("", encoding="utf-8")
    (d / "29990101-000000.jsonl").write_text("", encoding="utf-8")     # 未来时间戳：不参与
    protect = d / "20260101-000000.jsonl"
    assert ST.prune_backups(v, is_git=False) == 0
    removed = ST.prune_backups(v, keep=20, protect=[protect], is_git=True)
    assert removed == 4
    left = sorted(p.name for p in d.iterdir())
    assert "notes.txt" in left and "29990101-000000.jsonl" in left and protect.name in left
    assert "20260125-000000.jsonl" in left and "20260102-000000.jsonl" not in left


def _note(v, rel, summary_line):
    p = v / rel
    p.write_text(f"---\ncategory: 技术笔记\n{summary_line}---\n正文\n", encoding="utf-8")
    return p


def test_restore_backup_restores_only_when_current_equals_new(tmp_path):
    v = _vault(tmp_path)
    entries = {"技术笔记/a.md": {"category": "技术笔记"}, "技术笔记/b.md": {"category": "技术笔记"},
               "技术笔记/c.md": {"category": "技术笔记"}}
    _cache(v, entries)
    pa = _note(v, "技术笔记/a.md", f'summary: "{V2}"\n')        # 当前 == new → 恢复
    pb = _note(v, "技术笔记/b.md", 'summary: "用户之后又手改过的摘要内容足够二十个字"\n')  # 当前 != new → 跳过
    pc = _note(v, "技术笔记/c.md", f'summary: "{V2}"\n')        # old 为空 → 删键
    bf = ST.new_backup_file(v)
    ST.append_backup(bf, ST.backup_record("技术笔记/a.md", V1, [f'summary: "{V1}"'], V2, bf.name))
    ST.append_backup(bf, ST.backup_record("技术笔记/b.md", V1, [f'summary: "{V1}"'], V2, bf.name))
    ST.append_backup(bf, ST.backup_record("技术笔记/c.md", None, [], V2, bf.name))
    res = ST.restore_backup(v, bf, entries)
    assert res["status"] == "ok"
    assert sorted(res["restored"]) == ["技术笔记/a.md", "技术笔记/c.md"]
    assert [s["reason"] for s in res["skipped"]] == ["changed"]
    assert ST.current_summary(pa) == V1
    assert "summary" not in pc.read_text(encoding="utf-8")
    assert "手改" in pb.read_text(encoding="utf-8")
    cache = json.loads((v / ".meta" / "frontmatter-cache.json").read_text(encoding="utf-8"))["entries"]
    assert cache["技术笔记/a.md"]["summary"] == V1
    assert Path(res["backup"]).exists() and Path(res["backup"]) != bf


def test_restore_backup_rejects_outside_path_and_tampered_lines(tmp_path):
    v = _vault(tmp_path)
    entries = {"技术笔记/a.md": {"category": "技术笔记"}}
    _cache(v, entries)
    _note(v, "技术笔记/a.md", f'summary: "{V2}"\n')
    outside = tmp_path / "20260101-000000.jsonl"
    outside.write_text("", encoding="utf-8")
    assert ST.restore_backup(v, outside, entries)["status"] == "error"
    bf = ST.new_backup_file(v)
    ST.append_backup(bf, ST.backup_record("技术笔记/a.md", V1, ["tags: [evil]"], V2, bf.name))
    res = ST.restore_backup(v, bf, entries)
    assert res["restored"] == [] and res["skipped"][0]["reason"] == "bad_record"


def test_restore_to_no_summary_keeps_h1_fallback_in_cache(tmp_path):
    """回滚到「原本没有 summary」时 cache 要回到 H1 兜底，而不是整个删掉（P1-M9）。"""
    v = _vault(tmp_path)
    rel = "技术笔记/h.md"
    entries = {rel: {"category": "技术笔记"}}
    (v / rel).write_text(f'---\ncategory: 技术笔记\nsummary: "{V2}"\n---\n# 一个足够长的标题会被兜底当成摘要来显示\n',
                         encoding="utf-8")
    _cache(v, {rel: {"category": "技术笔记", "summary": V2, "summary_src": "fm"}})
    bf = ST.new_backup_file(v)
    ST.append_backup(bf, ST.backup_record(rel, None, [], V2, bf.name))
    res = ST.restore_backup(v, bf, entries)
    assert res["restored"] == [rel]
    e = json.loads((v / ".meta" / "frontmatter-cache.json").read_text(encoding="utf-8"))["entries"][rel]
    assert e.get("summary_src") == "h1" and e.get("summary", "").startswith("一个足够长的标题")


def test_restore_skips_bad_lines_and_keeps_the_rest(tmp_path):
    """备份里一行坏了（或旧值含 U+2028 被按行切断）不得让整份不可恢复（P1-M10）。"""
    v = _vault(tmp_path)
    entries = {"技术笔记/a.md": {"category": "技术笔记"}}
    _cache(v, entries)
    _note(v, "技术笔记/a.md", f'summary: "{V2}"\n')
    bf = ST.new_backup_file(v)
    with open(bf, "a", encoding="utf-8", newline="\n") as f:
        f.write("{not json\n")
    ST.append_backup(bf, ST.backup_record("技术笔记/a.md", V1 + chr(0x2028), [f'summary: "{V1}"'], V2, bf.name))
    res = ST.restore_backup(v, bf, entries)
    assert res["status"] == "ok" and res["restored"] == ["技术笔记/a.md"]
    assert [s["reason"] for s in res["skipped"]] == ["bad_record"]


def test_attempts_roundtrip_and_corrupt_file(tmp_path):
    v = _vault(tmp_path)
    assert ST.load_attempts(v) == {}
    assert ST.bump_attempt(v, "技术笔记/a.md") == 1
    assert ST.bump_attempt(v, "技术笔记/a.md") == 2
    (v / ".meta" / "summary-attempts.json").write_text("{bad", encoding="utf-8")
    assert ST.load_attempts(v) == {}


def test_single_instance_lock(tmp_path):
    """持锁进程还活着：锁再旧也不抢（跑超 2 h 的批次不会被第二个实例接管、重复付费）；
    持锁进程已死（被硬杀）：立即接管，不必等 2 h（PERF-M4 / DO-M3）。"""
    v = _vault(tmp_path)
    lock = ST.acquire_single_instance(v)
    assert lock is not None
    assert ST.acquire_single_instance(v) is None
    old = time.time() - 3 * 3600
    os.utime(lock, (old, old))
    assert ST.acquire_single_instance(v) is None
    info = ST.single_instance_holder(v)
    assert info["pid"] == os.getpid() and info["age_sec"] >= 3 * 3600 - 60 and info["lock"] == str(lock)
    # 不存在的 PID（Windows 上刚结束的子进程句柄未关时 OpenProcess 仍会成功，不能拿来当「已死」）
    lock.write_text("4194300\n", encoding="utf-8")
    assert ST.single_instance_holder(v) is None
    lock2 = ST.acquire_single_instance(v)
    assert lock2 is not None
    ST.release_single_instance(lock2)
    assert not lock2.exists()
