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
    # 2.4.0 增量键：没人填时也恒在（mod 按「缺键 = 旧快照」区别对待）
    assert snap["supps"] == [] and snap["settings"] is None
    assert snap["persisted"] == [] and snap["vault_root"] is None


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


# ── 契约 v1 增量（spec 2026-10-13 §10.6）──────────────────────────────────────────────

def test_max_rejected_counts_settings_and_persist():
    # 每类逐条至多一条再加一条 over_cap：exclude 51 + pin 9 + unpin 9 + 标注 51 + settings 11 + persist 11
    assert ops_mod.MAX_REJECTED == 142


def test_supp_item_matches_fixture_and_whitelists_values():
    fixture = json.loads(SNAP_FIXTURE.read_text(encoding="utf-8"))
    it = ops_mod.supp_item("项目笔记/demo/f" + chr(0x202E) + "x.md", 3, "passed", True)
    assert set(it) == set(fixture["supps"][0])
    assert it["title"] == "fx" and it["src_seq"] == 3 and it["recheck"] == "passed"
    assert it["rendered"] is True
    assert ops_mod.supp_item("a.md", True, "passed", True)["src_seq"] is None    # bool 不是轮次
    assert ops_mod.supp_item("a.md", 2, "maybe", True)["recheck"] is None
    assert ops_mod.supp_item("a.md", 2, "skipped", 1)["rendered"] is False      # 只认 True
    assert set(ops_mod.SUPP_RECHECKS) == {"passed", "skipped", "failed"}


def test_settings_snapshot_shape_matches_fixture_and_cleans_values():
    from scripts._fine_config import PANEL_KEYS
    fixture = json.loads(SNAP_FIXTURE.read_text(encoding="utf-8"))
    assert tuple(fixture["settings"]["effective"]) == tuple(PANEL_KEYS)
    eff = dict(fixture["settings"]["effective"])
    eff["fine_model"] = "son" + chr(0x202E) + "net"
    eff["fine_pin_max"] = float("inf")
    snap = ops_mod.settings_snapshot(dict(eff, extra=1), {"fine_max_notes": 2, "bogus": 1},
                                     ["fine_rank", "bogus"],
                                     {"fine_rank": "config", "fine_model": "elsewhere"})
    assert set(snap) == set(fixture["settings"])
    assert tuple(snap["effective"]) == tuple(PANEL_KEYS)
    assert snap["effective"]["fine_model"] == "sonnet"
    assert snap["effective"]["fine_pin_max"] is None
    assert snap["session"] == {"fine_max_notes": 2}
    assert snap["locked"] == ["fine_rank"]
    assert snap["source"]["fine_rank"] == "config" and snap["source"]["fine_model"] is None
    json.dumps(snap, allow_nan=False)          # mod 的 JSON.parse 不认非有限数


def test_settings_snapshot_keeps_bools_caps_strings_and_orders_locked():
    from scripts._fine_config import PANEL_KEYS
    eff = dict(json.loads(SNAP_FIXTURE.read_text(encoding="utf-8"))["settings"]["effective"])
    eff.update(fine_rank=True, fine_supplement=False, fine_model="m" * 70)
    snap = ops_mod.settings_snapshot(eff, {"fine_model": "n" * 70},
                                     ["fine_supplement", "fine_rank"], {})
    # bool 原样：开关值被丢成 None，面板会把五个开关全显示成「未知」
    assert snap["effective"]["fine_rank"] is True
    assert snap["effective"]["fine_supplement"] is False
    # 字符串按 64 字符截（硬编码，不从 MAX_SETTING_STR_CHARS 反算），有效值与会话值都截
    assert snap["effective"]["fine_model"] == "m" * 64
    assert snap["session"]["fine_model"] == "n" * 64
    # locked 按 PANEL_KEYS 的顺序输出，不按入参顺序
    assert PANEL_KEYS.index("fine_rank") < PANEL_KEYS.index("fine_supplement")
    assert snap["locked"] == ["fine_rank", "fine_supplement"]


def test_settings_snapshot_huge_int_becomes_none():
    # finite_num 内部的 math.isfinite 对超大 int 抛 OverflowError；快照值不能因此让整轮出错
    snap = ops_mod.settings_snapshot({"fine_pin_max": 10 ** 400}, {"fine_max_notes": 10 ** 400}, [], {})
    assert snap["effective"]["fine_pin_max"] is None
    assert snap["session"]["fine_max_notes"] is None
    json.dumps(snap, allow_nan=False)


def test_vault_root_keeps_long_paths():
    assert ops_mod.MAX_ROOT_CHARS == 1024
    long = "/v/" + "目录" * 200                # 403 字，超过标题上限 120
    assert ops_mod.vault_root_text(long) == long
    assert ops_mod.vault_root_text("/v/" + "x" * 1021) == "/v/" + "x" * 1021   # 恰好 1024 字照发
    assert ops_mod.vault_root_text("/v/" + "x" * 2000) is None                 # 超长不发，不截断
    assert ops_mod.vault_root_text("/v/a" + chr(0x202E) + "b") == "/v/ab"
    assert ops_mod.vault_root_text(None) is None
    assert ops_mod.vault_root_text("") is None


def test_snapshot_carries_filled_new_keys(tmp_path):
    _write_request(tmp_path, {"v": 1})
    rep = ops_mod.TurnReport.for_ups(_hook(), tmp_path)
    rep.take_ops()
    rep.status = "gate:deictic_only"
    rep.supps = [ops_mod.supp_item("a.md", 4, "skipped", True)]
    rep.persisted = ["fine_max_notes"]
    rep.vault_root = ops_mod.vault_root_text("/v")
    rep.finalize()
    snap = json.loads(ops_mod.snapshot_path(SID, tmp_path).read_text(encoding="utf-8"))
    assert snap["status"] == "gate:deictic_only"
    assert snap["supps"] == [{"path": "a.md", "title": "a", "src_seq": 4, "recheck": "skipped",
                              "rendered": True}]
    assert snap["persisted"] == ["fine_max_notes"] and snap["vault_root"] == "/v"


def test_snapshot_caps_new_list_keys(tmp_path):
    assert ops_mod.MAX_LIST_ITEMS == 50 and ops_mod.MAX_PERSIST == 10
    _write_request(tmp_path, {"v": 1})
    rep = ops_mod.TurnReport.for_ups(_hook(), tmp_path)
    rep.take_ops()
    rep.status = "gate:deictic_only"
    rep.supps = [ops_mod.supp_item(f"n{i}.md", i, "passed", True) for i in range(60)]
    rep.persisted = [f"k{i}" for i in range(15)]
    rep.finalize()
    snap = json.loads(ops_mod.snapshot_path(SID, tmp_path).read_text(encoding="utf-8"))
    assert [s["path"] for s in snap["supps"]] == [f"n{i}.md" for i in range(50)]
    assert snap["persisted"] == [f"k{i}" for i in range(10)]


def test_mod_setting_rows_match_panel_keys():
    """mod 面板「设置」节的键与 Python `_fine_config.PANEL_KEYS` 逐一相同、同序（spec §10.3）。"""
    from scripts._fine_config import PANEL_KEYS
    src = MOD_JS.read_text(encoding="utf-8")
    m = re.search(r"const SETTING_ROWS = \[(.*?)\n\]", src, re.S)
    assert m, "找不到 SETTING_ROWS"
    keys = re.findall(r"\['([a-z_.]+)', '(?:bool|int|model)'", m.group(1))
    assert keys, "SETTING_ROWS 解析出空表"
    assert tuple(keys) == tuple(PANEL_KEYS)


def test_mod_disabled_and_recheck_enums_match_python():
    from scripts._fine_state import DISABLED_REASONS
    src = MOD_JS.read_text(encoding="utf-8")
    assert _js_object_keys(src, "DISABLED_TEXT") == set(DISABLED_REASONS)
    assert _js_object_keys(src, "RECHECK_TEXT") == set(ops_mod.SUPP_RECHECKS)
    assert "const LLM_DISABLED = new Set(Object.keys(DISABLED_TEXT))" in src


def _js_int(src: str, name: str) -> int:
    m = re.search(r"const %s = (\d+)\b" % name, src)
    assert m, f"找不到 {name}"
    return int(m.group(1))


def _js_set_words(src: str, name: str) -> tuple:
    """取 `const NAME = new Set(['a', 'b'])` 里的字符串，保持出现顺序。"""
    m = re.search(r"const %s = new Set\(\[(.*?)\]\)" % name, src, re.S)
    assert m, f"找不到 {name}"
    words = tuple(re.findall(r"'([^']*)'", m.group(1)))
    assert words, f"{name} 解析出空集"                               # 阳性对照：解析没失效
    return words


def test_mod_limits_and_caps_match_python():
    """mod 的上限常量与 `_session_ops` 的 MAX_* 逐一相同（此前只在 `_session_ops.py` 的注释里写着「两侧镜像」）。"""
    src = MOD_JS.read_text(encoding="utf-8")
    m = re.search(r"const LIMITS = \{(.*?)\}", src, re.S)
    assert m, "找不到 LIMITS"
    limits = {k: int(v) for k, v in re.findall(r"(\w+): (\d+)", m.group(1))}
    assert limits == {"exclude": ops_mod.MAX_EXCLUDE, "pin": ops_mod.MAX_PIN, "unpin": ops_mod.MAX_UNPIN,
                      "annotations": ops_mod.MAX_ANNOTATIONS, "settings": ops_mod.MAX_SETTINGS,
                      "persist": ops_mod.MAX_PERSIST, "reads": ops_mod.MAX_READS}
    assert _js_int(src, "MAX_ITEM_CHARS") == ops_mod.MAX_ITEM_CHARS


def test_mod_sources_and_modes_match_python():
    from scripts import _topic
    src = MOD_JS.read_text(encoding="utf-8")
    assert set(_js_set_words(src, "SOURCES")) == set(ops_mod.SETTING_SOURCES)
    assert _js_object_keys(src, "MODE_TEXT") == set(_topic._MODES)
    assert "const LLM_MODES = new Set(Object.keys(MODE_TEXT))" in src


def test_mod_setting_row_types_and_int_bounds_match_fine_config():
    """`SETTING_ROWS` 的类型列与 `_fine_config.BOOL_KEYS` / `INT_RANGES` 一致；整数项 mod 夹到 [0, INT_MAX]，
    与 Python 区间的两端相同，否则 mod 允许按出的值会被 Python 当 bad_value 拒收。"""
    from scripts._fine_config import BOOL_KEYS, INT_RANGES, PANEL_KEYS
    src = MOD_JS.read_text(encoding="utf-8")
    m = re.search(r"const SETTING_ROWS = \[(.*?)\n\]", src, re.S)
    assert m, "找不到 SETTING_ROWS"
    rows = re.findall(r"\['([a-z_.]+)', '(bool|int|model)'", m.group(1))
    assert tuple(k for k, _ in rows) == tuple(PANEL_KEYS)
    for key, kind in rows:
        want = "bool" if key in BOOL_KEYS else "int" if key in INT_RANGES else "model"
        assert kind == want, (key, kind, want)
    ints = [k for k, kind in rows if kind == "int"]
    assert ints, "SETTING_ROWS 里没有整数项"                         # 阳性对照：下面的循环不是空转
    int_max = _js_int(src, "INT_MAX")
    for key in ints:
        assert INT_RANGES[key] == (0, int_max), (key, INT_RANGES[key], int_max)
