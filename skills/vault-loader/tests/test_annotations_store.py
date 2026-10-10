"""标注写入单点（spec §6.3）：锁内查 id、按 ts 合并、服务端核对。"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import _annotations as ann
from scripts import _metrics

# 占位 UUID 拆开拼接：发布闸门按 8-4-4-4-12 hex 拦截疑似真实 session id（test_no_secrets_in_tree）
SID = "-".join(("11111111", "2222", "3333", "4444", "555555555555"))
PH = "0123456789abcdef"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    _metrics.configure_context("legacy")
    return tmp_path


def _lines(home):
    p = ann.annotations_path(home)
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_analyze_metrics_reexports_same_objects():
    from scripts import analyze_metrics as am
    assert am.save_annotation is ann.save_annotation
    assert am.load_annotations is ann.load_annotations
    assert am.annotations_path is ann.annotations_path
    assert am.KINDS is ann.KINDS and am.VERDICTS is ann.VERDICTS


def test_save_with_id_is_idempotent(home):
    assert ann.save_annotation(home, "a.md", "relevant", kind="admitted_list",
                               ann_id="ann-00000001", ts=100) is True
    assert ann.save_annotation(home, "a.md", "relevant", kind="admitted_list",
                               ann_id="ann-00000001", ts=100) is False
    rows = _lines(home)
    assert len(rows) == 1 and rows[0]["id"] == "ann-00000001" and rows[0]["ts"] == 100


def test_save_without_id_keeps_legacy_shape(home):
    ann.save_annotation(home, "a.md", "irrelevant", kind="near_miss")
    assert set(_lines(home)[0]) == {"_schema", "path", "verdict", "kind"}


def test_load_prefers_newer_ts_over_file_order(home):
    ann.save_annotation(home, "a.md", "irrelevant", kind="admitted_list", ann_id="ann-new00001", ts=200)
    ann.save_annotation(home, "a.md", "relevant", kind="admitted_list", ann_id="ann-old00001", ts=100)
    assert ann.load_annotations(home)[("admitted_list", "a.md")] == "irrelevant"


def test_load_without_ts_keeps_file_order(home):
    ann.save_annotation(home, "a.md", "irrelevant", kind="admitted_list")
    ann.save_annotation(home, "a.md", "relevant", kind="admitted_list")
    assert ann.load_annotations(home)[("admitted_list", "a.md")] == "relevant"


def test_save_timeout_propagates(home, monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def _busy(_t):
        raise TimeoutError("busy")
        yield

    monkeypatch.setattr(ann, "_lock", _busy)
    with pytest.raises(TimeoutError):
        ann.save_annotation(home, "a.md", "relevant", kind="admitted_list", ann_id="ann-00000002", ts=1)


def _write_turn(home, *, shown, ft="", llm_pinned=None, prompt_h=PH):
    rec = {"_schema": 1, "session": SID, "prompt_id": "p", "prompt_h": prompt_h,
           "shown": shown, "ft": {"path": ft, "arm": ""}, "gate": ""}
    if llm_pinned is not None:
        rec["llm_pinned"] = llm_pinned
    _metrics.write_record(home, SID, rec)


def _a(**kw):
    base = {"id": "ann-00000001", "path": "a.md", "kind": "admitted_list", "verdict": "relevant",
            "session": SID, "prompt_h": PH, "ts": 100}
    base.update(kw)
    return base


def test_apply_accepts_shown_list_item(home):
    _write_turn(home, shown=["a.md"])
    acked, rejected = ann.apply_annotations(home, [_a()])
    assert acked == ["ann-00000001"] and rejected == []
    row = _lines(home)[0]
    assert row["context_ids"] == [{"session": SID, "prompt_h": PH}]
    assert row["ts"] == 100          # 按 ts 合并依赖它（节点 2 T-H3 变异 M3）


@pytest.mark.parametrize("anno,reason", [
    (_a(prompt_h="ffffffffffffffff"), "no_record"),
    (_a(path="other.md"), "no_record"),
    (_a(kind="admitted_fulltext"), "no_record"),
])
def test_apply_rejects_unverifiable(home, anno, reason):
    _write_turn(home, shown=["a.md"])
    acked, rejected = ann.apply_annotations(home, [anno])
    assert acked == [] and rejected == [{"what": "annotation", "key": anno["id"], "reason": reason}]
    assert not ann.annotations_path(home).exists()


def test_apply_without_session_file_is_neither_acked_nor_rejected(home):
    """会话 metrics 文件一个都读不到：可能是 purge 之后，也可能是命名空间分裂（记录在另一棵树），
    两者分不开，不能判终局（用户拍板 2026-10-10，节点 2 S-L8）。"""
    acked, rejected = ann.apply_annotations(home, [_a()])
    assert acked == [] and rejected == []
    assert not ann.annotations_path(home).exists()


def test_apply_fulltext_kind_requires_ft_path(home):
    _write_turn(home, shown=["a.md"], ft="a.md")
    acked, _ = ann.apply_annotations(home, [_a(kind="admitted_fulltext")])
    assert acked == ["ann-00000001"]


def test_apply_llm_pinned_checks_any_record_in_session(home):
    _write_turn(home, shown=[], llm_pinned=["p.md"], prompt_h="1111111111111111")
    _write_turn(home, shown=["x.md"])
    acked, _ = ann.apply_annotations(home, [_a(kind="llm_pinned", path="p.md")])
    assert acked == ["ann-00000001"]


def test_apply_llm_pinned_rejects_path_never_pinned(home):
    _write_turn(home, shown=["x.md"], llm_pinned=["p.md"])
    acked, rejected = ann.apply_annotations(home, [_a(kind="llm_pinned", path="x.md")])
    assert acked == [] and rejected == [{"what": "annotation", "key": "ann-00000001",
                                         "reason": "no_record"}]


def test_apply_lock_timeout_is_not_acked(home, monkeypatch):
    from contextlib import contextmanager
    _write_turn(home, shown=["a.md"])

    @contextmanager
    def _boom(*_a, **_k):
        raise TimeoutError("busy")
        yield

    # 整批写入不经 save_annotation（节点 2 P-H1），超时从锁本身抛出
    monkeypatch.setattr(ann, "_lock", _boom)
    acked, rejected = ann.apply_annotations(home, [_a()])
    assert acked == [] and rejected == []          # 不 ack、不拒收：下一轮重投


_CHILD = r"""
import sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from scripts import _annotations as ann, _metrics
_metrics.configure_context("legacy")
home = Path(sys.argv[2])
for i in range(20):
    ann.save_annotation(home, "a.md", "relevant", kind="admitted_list",
                        ann_id=f"ann-{int(sys.argv[3]):04d}{i:04d}", ts=i)
    ann.save_annotation(home, "a.md", "relevant", kind="admitted_list",
                        ann_id=f"ann-shared{i:04d}", ts=i)
"""


def test_same_id_written_once_across_processes(home):
    root = str(Path(__file__).resolve().parents[1])
    procs = [subprocess.Popen([sys.executable, "-c", _CHILD, root, str(home), str(n)])
             for n in range(4)]
    for p in procs:
        assert p.wait(timeout=120) == 0
    ids = [r["id"] for r in _lines(home)]
    shared = [i for i in ids if i.startswith("ann-shared")]
    assert len(shared) == len(set(shared)) == 20
    assert len(ids) == 4 * 20 + 20


# ── 热路径代价（节点 2 P-H1 / P-M1 / T-H4）：按调用次数判定，与机器负载无关 ──

def _counting(monkeypatch, obj, name):
    calls = []
    orig = getattr(obj, name)

    def _wrap(*a, **k):
        calls.append(k)
        return orig(*a, **k)

    monkeypatch.setattr(obj, name, _wrap)
    return calls


def _five(home):
    _write_turn(home, shown=[f"n{i}.md" for i in range(5)])
    return [_a(id=f"ann-0000000{i}", path=f"n{i}.md") for i in range(5)]


def test_apply_batch_takes_lock_reads_ids_and_scans_session_once(home, monkeypatch):
    anns = _five(home)
    locks = _counting(monkeypatch, ann, "_lock")
    ids = _counting(monkeypatch, ann, "_existing_ids")
    scans = _counting(monkeypatch, _metrics, "event_month_dirs")
    acked, rejected = ann.apply_annotations(home, anns)
    assert acked == [a["id"] for a in anns] and rejected == []
    assert (len(locks), len(ids), len(scans)) == (1, 1, 1)
    assert [r["id"] for r in _lines(home)] == acked


def test_apply_lock_timeout_stops_whole_batch_with_short_wait(home, monkeypatch):
    from contextlib import contextmanager
    anns = _five(home)
    seen = []

    @contextmanager
    def _busy(_target, **kw):
        seen.append(kw)
        raise TimeoutError("busy")
        yield

    monkeypatch.setattr(ann, "_lock", _busy)
    acked, rejected = ann.apply_annotations(home, anns)
    assert acked == [] and rejected == []              # 整批留到下一轮
    assert len(seen) == 1                              # 首次超时即停，不逐条再等
    assert 0 < seen[0].get("timeout", 2.0) <= 0.25
    assert seen[0].get("hard_stale_after", 24 * 3600.0) <= 3600


def test_apply_against_held_lock_returns_within_bound(home):
    import time
    anns = _five(home)
    lock = ann.annotations_path(home).with_name("annotations.jsonl.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    # 新鲜锁：lease_lock 在 stale_after（30 s）内只等待、不做存活探测
    lock.write_text("held-by-test", encoding="ascii")
    t0 = time.monotonic()
    acked, rejected = ann.apply_annotations(home, anns)
    elapsed = time.monotonic() - t0
    assert acked == [] and rejected == []
    # 修复前逐条 2 s：N=5 实测 10.03 s（节点 2 1A）；上界取 2 s，与 0.25 s 的设计值留足余量
    assert elapsed < 2.0, elapsed
    assert lock.read_text(encoding="ascii") == "held-by-test"
    assert not ann.annotations_path(home).exists()


def test_mod_annotation_delivered_late_does_not_override_newer_web_review(home):
    """混合写者（节点 2 Q-M2 / T-M5）：网页标注写入时刻晚于 mod 标注的点击时刻，应以网页为准。
    两个写者都经真实调用点：`apply_web_annotations` 与 `apply_annotations`。"""
    import time
    from scripts.analyze_metrics import apply_web_annotations
    _write_turn(home, shown=["a.md"])
    n, errs = apply_web_annotations(
        home, [{"path": "a.md", "kind": "admitted_list", "verdict": "irrelevant"}])
    assert (n, errs) == (1, [])
    acked, _ = ann.apply_annotations(home, [_a(ts=int(time.time()) - 600)])
    assert acked == ["ann-00000001"]
    assert ann.load_annotations(home)[("admitted_list", "a.md")] == "irrelevant"
