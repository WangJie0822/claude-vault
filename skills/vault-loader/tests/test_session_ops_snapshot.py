"""_session_ops：显示净化、TurnReport、快照写出与过期清理。"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import pytest

from scripts import _session_ops as ops_mod

# 占位 UUID 拆开拼接：发布闸门按 8-4-4-4-12 hex 拦截疑似真实 session id（test_no_secrets_in_tree）
SID = "-".join(("11111111", "2222", "3333", "4444", "555555555555"))
PID = "-".join(("aaaaaaaa", "bbbb", "cccc", "dddd", "eeeeeeeeeeee"))
SNAP_FIXTURE = Path(__file__).parent / "fixtures" / "mod_contract" / "snapshot_v1.json"


@pytest.mark.parametrize("raw", ["a\nb", "a b", "a‮b", "a\x9bb", "a​b", "a\x1b[31mb"])
def test_display_text_is_single_line_and_clean(raw):
    out = ops_mod.display_text(raw)
    assert "\n" not in out and " " not in out and "‮" not in out
    assert "\x9b" not in out and "​" not in out and "\x1b" not in out


def test_display_text_truncates_and_rejects_non_str():
    assert len(ops_mod.display_text("x" * 500)) == 120
    assert ops_mod.display_text(None) == ""


@pytest.mark.parametrize("cp", [0x061C, 0x2060, 0x2061, 0x2062, 0x2063, 0x2064])
def test_display_text_strips_added_invisibles(cp):
    assert chr(cp) not in ops_mod.display_text(f"a{chr(cp)}b")


def test_note_title_uses_stem():
    assert ops_mod.note_title("项目笔记/demo/设计.md") == "设计"


def _hook(sid=SID, pid=PID):
    return {"session_id": sid, "prompt_id": pid, "hook_event_name": "UserPromptSubmit"}


def test_report_without_request_writes_nothing(tmp_path):
    rep = ops_mod.TurnReport.for_ups(_hook(), tmp_path)
    assert not rep.request_seen
    rep.status = "scored"
    rep.finalize()
    assert not ops_mod.live_dir(tmp_path).exists()


def _write_request(home, obj):
    p = ops_mod.request_path(SID, PID, home)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj), encoding="utf-8")
    return p


def test_report_with_request_writes_snapshot_and_removes_request(tmp_path):
    req = _write_request(tmp_path, {"v": 1, "exclude": ["a.md"]})
    rep = ops_mod.TurnReport.for_ups(_hook(), tmp_path)
    assert rep.request_seen
    ops = rep.take_ops()
    assert ops.exclude == ("a.md",) and not req.exists()
    rep.status = "scored"
    rep.finalize()
    snap = json.loads(ops_mod.snapshot_path(SID, tmp_path).read_text(encoding="utf-8"))
    assert snap["v"] == 1 and snap["status"] == "scored" and snap["prompt_id"] == PID
    assert snap["request_digest"] == ops.digest
    assert "prompt_h" not in snap


def test_disabled_turn_still_reports_and_cleans_request(tmp_path):
    req = _write_request(tmp_path, {"v": 1})
    rep = ops_mod.TurnReport.for_ups(_hook(), tmp_path)
    rep.finalize()                       # 从未 take_ops：停用类出口
    snap = json.loads(ops_mod.snapshot_path(SID, tmp_path).read_text(encoding="utf-8"))
    assert snap["status"] == "disabled" and snap["request_digest"] is None
    assert not req.exists()


def test_duplicate_status_writes_no_snapshot(tmp_path):
    _write_request(tmp_path, {"v": 1})
    rep = ops_mod.TurnReport.for_ups(_hook(), tmp_path)
    rep.status = "duplicate"
    rep.finalize()
    assert not ops_mod.snapshot_path(SID, tmp_path).exists()


def test_duplicate_status_leaves_request_for_the_claiming_process(tmp_path):
    # 重复事件的进程没抢到事件：请求属于抢到的那个进程，这里删掉会让它读空
    req = _write_request(tmp_path, {"v": 1, "exclude": ["a.md"]})
    rep = ops_mod.TurnReport.for_ups(_hook(), tmp_path)
    rep.status = "duplicate"
    rep.finalize()
    assert req.exists()


def test_snapshot_keys_match_contract_fixture(tmp_path):
    _write_request(tmp_path, {"v": 1})
    rep = ops_mod.TurnReport.for_ups(_hook(), tmp_path)
    rep.take_ops()
    rep.status = "scored"
    rep.metrics_on = True
    rep.prompt_h = "0123456789abcdef"
    rep.finalize()
    snap = json.loads(ops_mod.snapshot_path(SID, tmp_path).read_text(encoding="utf-8"))
    fixture = json.loads(SNAP_FIXTURE.read_text(encoding="utf-8"))
    assert set(snap) == set(fixture)
    assert set(snap["pins"]) == set(fixture["pins"])


def test_snapshot_new_keys_default_to_unknown(tmp_path):
    _write_request(tmp_path, {"v": 1})
    rep = ops_mod.TurnReport.for_ups(_hook(), tmp_path)
    rep.take_ops()
    rep.status = "scored"
    rep.finalize()
    snap = json.loads(ops_mod.snapshot_path(SID, tmp_path).read_text(encoding="utf-8"))
    assert snap["llm"] is None and snap["thresholds"] is None     # 未计算：mod 显示「未知」
    assert isinstance(snap["ts"], int) and abs(snap["ts"] - time.time()) < 60


def test_reject_annotations_records_reason(tmp_path):
    rep = ops_mod.TurnReport.for_ups(_hook(), tmp_path)
    rep.reject_annotations([{"id": "ann-00000001"}], "metrics_off")
    assert rep.rejected == [{"what": "annotation", "key": "ann-00000001", "reason": "metrics_off"}]


def test_restrict_updates_ops_and_rejected(tmp_path):
    _write_request(tmp_path, {"v": 1, "exclude": ["a.md", "gone.md"]})
    rep = ops_mod.TurnReport.for_ups(_hook(), tmp_path)
    rep.take_ops()
    ops = rep.restrict({"a.md"})
    assert ops.exclude == ("a.md",)
    assert {"what": "exclude", "key": "gone.md", "reason": "not_in_index"} in rep.rejected


def test_ss_report_uses_marker_and_null_prompt(tmp_path):
    m = ops_mod.ss_request_path(SID, tmp_path)
    m.parent.mkdir(parents=True)
    m.write_text("{}", encoding="utf-8")
    rep = ops_mod.TurnReport.for_session_start({"session_id": SID}, tmp_path)
    assert rep.request_seen and rep.prompt_id is None
    rep.status = "scored"
    rep.session_start = [{"path": "p.md", "title": "p"}]
    rep.finalize()
    snap = json.loads(ops_mod.snapshot_path(SID, tmp_path).read_text(encoding="utf-8"))
    assert snap["event"] == "SessionStart" and snap["prompt_id"] is None
    assert snap["session_start"] == [{"path": "p.md", "title": "p"}]
    assert not m.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX 权限位")
def test_posix_permissions(tmp_path):
    p = ops_mod.snapshot_path(SID, tmp_path)
    ops_mod.write_snapshot(p, {"v": 1})
    assert (os.stat(p.parent).st_mode & 0o777) == 0o700
    assert (os.stat(p).st_mode & 0o777) == 0o600


def _aged(p: Path, seconds: float):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}", encoding="utf-8")
    t = time.time() - seconds
    os.utime(p, (t, t))


H1 = "a" * 24
H2 = "b" * 24


def test_cleanup_by_age(tmp_path):
    d = ops_mod.live_dir(tmp_path)
    old_snap, new_snap = d / f"{H1}.json", d / f"{H2}.json"
    old_req, new_req = d / f"{H1}-{PID}.req.json", d / f"{H2}-ss.req.json"
    _aged(old_snap, 25 * 3600)
    _aged(new_snap, 1 * 3600)
    _aged(old_req, 2 * 3600)
    _aged(new_req, 10 * 60)
    assert ops_mod.cleanup_live(tmp_path) == 2
    assert not old_snap.exists() and new_snap.exists()
    assert not old_req.exists() and new_req.exists()


def test_cleanup_missing_dir_is_noop(tmp_path):
    assert ops_mod.cleanup_live(tmp_path) == 0
    assert not ops_mod.live_dir(tmp_path).exists()


def test_cleanup_does_not_follow_linked_dir(tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    keep = target / "old.json"
    _aged(keep, 48 * 3600)
    link = ops_mod.live_dir(tmp_path)
    link.parent.mkdir(parents=True)
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        if os.name != "nt":
            pytest.skip("无权创建目录符号链接")
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    assert ops_mod.cleanup_live(tmp_path) == 0
    assert keep.exists()


def test_purge_live_removes_regular_files(tmp_path):
    d = ops_mod.live_dir(tmp_path)
    _aged(d / f"{H1}.json", 0)
    _aged(d / f"{H2}-{PID}.req.json", 0)
    assert ops_mod.purge_live(tmp_path) == 2
    assert list(d.iterdir()) == []


# ── 节点 2 S-M1：只删本插件写出的文件；链接判据不依赖 Python 3.12 的 os.path.isjunction ──

def test_cleanup_and_purge_leave_foreign_files(tmp_path):
    d = ops_mod.live_dir(tmp_path)
    foreign = [d / "notes.json", d / "x-y.req.json", d / "victim.txt", d / f"{H1}.json.bak"]
    for f in foreign:
        _aged(f, 48 * 3600)
    old_tmp = d / f".{H1}.json.123.0a1b2c3d.tmp"
    _aged(old_tmp, 2 * 3600)
    assert ops_mod.cleanup_live(tmp_path) == 1          # 只有超过 1 小时的插件临时文件
    assert not old_tmp.exists()
    assert ops_mod.purge_live(tmp_path) == 0
    assert all(f.exists() for f in foreign)


@pytest.mark.skipif(os.name != "nt", reason="junction 只在 Windows 上存在")
def test_cleanup_does_not_follow_junction_on_python_before_312(tmp_path, monkeypatch):
    """3.9–3.11 没有 os.path.isjunction，Path.is_symlink() 对 junction 返回 False。"""
    import _winapi
    target = tmp_path / "elsewhere"
    target.mkdir()
    victims = [target / f"{H1}.json", target / f"{H2}-{PID}.req.json"]
    for v in victims:
        _aged(v, 48 * 3600)
    link = ops_mod.live_dir(tmp_path)
    link.parent.mkdir(parents=True)
    _winapi.CreateJunction(str(target), str(link))
    monkeypatch.delattr(os.path, "isjunction", raising=False)
    assert ops_mod.cleanup_live(tmp_path) == 0
    assert ops_mod.purge_live(tmp_path) == 0
    assert all(v.exists() for v in victims)
    with pytest.raises(OSError):
        ops_mod.write_snapshot(link / f"{H1}.json", {"v": 1})


def test_snapshot_keeps_not_in_index_when_many_annotations_rejected(tmp_path):
    """节点 2 T-M4：rejected 截到 50 条时，排在 50 条标注拒收之后的 not_in_index 被挤掉，
    mod 收不到「这条操作已失效」，每轮重发、pane 一直挂着。"""
    rep = ops_mod.TurnReport(session_id=SID, prompt_id=PID, home=tmp_path)
    rep.ops = ops_mod.parse_request({"v": 1, "exclude": ["gone.md"]})
    rep.reject_annotations([{"id": f"ann-{i:08d}"} for i in range(50)], "metrics_off")
    rep.restrict(set())
    keys = [(r["what"], r["reason"]) for r in rep.to_snapshot()["rejected"]]
    assert ("exclude", "not_in_index") in keys
    assert keys.count(("annotation", "metrics_off")) == 50


def test_ensure_live_dir_creates_private_dir(tmp_path):
    ops_mod.ensure_live_dir(tmp_path)
    assert ops_mod.live_dir(tmp_path).is_dir()


MOD_JS = Path(__file__).resolve().parents[3] / "hooks" / "vault-mod.js"
INVISIBLES = [0x061C, *range(0x200B, 0x2010), 0x2028, 0x2029, *range(0x202A, 0x202F),
              *range(0x2060, 0x2065), *range(0x2066, 0x206A), 0xFEFF]


def test_invisible_char_sets_match_mod():
    """mod 的 STRIP_RE 与 Python display_text 剥同一批不可见字符（spec 2026-10-12 §3.5）。"""
    m = re.search(r"const STRIP_RE = /\[(.+?)\]/g", MOD_JS.read_text(encoding="utf-8"))
    assert m, "找不到 STRIP_RE"
    js = re.compile("[" + m.group(1) + "]")
    assert js.sub("", "a" + chr(7) + "b") == "ab"                   # 阳性对照：类解析没失效
    for cp in INVISIBLES:
        ch = chr(cp)
        assert ch not in js.sub("", f"a{ch}b"), f"mod 未剥 U+{cp:04X}"
        assert ch not in ops_mod.display_text(f"a{ch}b"), f"Python 未剥 U+{cp:04X}"


def _js_object_keys(src: str, name: str) -> set:
    """取 vault-mod.js 里 `const NAME = { k: '…', … }` 的键集合（值是单引号字符串）。"""
    m = re.search(r"const %s = \{(.*?)\}\n" % name, src, re.S)
    assert m, f"找不到 {name}"
    keys = set(re.findall(r"(\w+): '", m.group(1)))
    assert keys, f"{name} 解析出空集"                                # 阳性对照：解析没失效
    return keys


def test_llm_and_dedup_enums_match_mod():
    """快照 `llm.state` / `llm.fail`、`pins.llm[].dedup`、`session_start[].why` 的值域两侧逐一相同。
    此前 mod 的白名单是手抄的：Python 多出一个值时，mod 把它当 null 处理、保留上一次的状态（full-review A2）。"""
    from scripts import _decision, _topic
    from scripts.session_start_load import _REASON_LABELS
    src = MOD_JS.read_text(encoding="utf-8")
    assert _js_object_keys(src, "LLM_TEXT") == set(_topic.LLM_STATES)
    assert _js_object_keys(src, "FAIL_TEXT") == set(_topic.LLM_FAILS)
    assert _js_object_keys(src, "DEDUPS") == {"fulltext_injected", "candidate_injected",
                                               _decision.USER_EXCLUDED}
    assert _js_object_keys(src, "REASONS") == set(_REASON_LABELS)
    # mod 的白名单从文案表派生，不再手抄一份
    assert "const LLM_STATES = new Set(Object.keys(LLM_TEXT))" in src
    assert "const LLM_FAILS = new Set(Object.keys(FAIL_TEXT))" in src
