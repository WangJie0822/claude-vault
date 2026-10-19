"""_session_ops：路径、请求读取与校验（spec 2026-10-09 mod 注入可见与可控 §5.2–§6.4）。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts import _session_ops as ops_mod

# 占位 UUID 拆开拼接：发布闸门按 8-4-4-4-12 hex 拦截疑似真实 session id（test_no_secrets_in_tree）
SID = "-".join(("11111111", "2222", "3333", "4444", "555555555555"))
PID = "-".join(("aaaaaaaa", "bbbb", "cccc", "dddd", "eeeeeeeeeeee"))
FIXTURE = Path(__file__).parent / "fixtures" / "mod_contract" / "request_v1.json"


def _fixture_text() -> str:
    # JSON 里没法拆开拼接，夹具的 session 写成占位符，读时换成合法 UUID
    return FIXTURE.read_text(encoding="utf-8").replace("<SESSION_UUID>", SID)


def _sid_h(sid: str) -> str:
    # 与被测实现独立计算（不从被测常量反算）
    return hashlib.sha256(sid.encode("utf-8")).hexdigest()[:24]


def test_paths_follow_contract(tmp_path):
    live = tmp_path / ".context-vault" / "state" / "live"
    assert ops_mod.live_dir(tmp_path) == live
    assert ops_mod.request_path(SID, PID, tmp_path) == live / f"{_sid_h(SID)}-{PID}.req.json"
    assert ops_mod.ss_request_path(SID, tmp_path) == live / f"{_sid_h(SID)}-ss.req.json"
    assert ops_mod.snapshot_path(SID, tmp_path) == live / f"{_sid_h(SID)}.json"


@pytest.mark.parametrize("sid,pid", [("", PID), (SID, ""), ("sess-1", PID), (SID, "../x"),
                                      (None, PID), (SID, 123)])
def test_non_uuid_ids_yield_no_path(tmp_path, sid, pid):
    assert ops_mod.request_path(sid, pid, tmp_path) is None


def test_ss_and_snapshot_reject_non_uuid(tmp_path):
    assert ops_mod.ss_request_path("../evil", tmp_path) is None
    assert ops_mod.snapshot_path("abc", tmp_path) is None


def test_take_request_reads_and_deletes(tmp_path):
    p = ops_mod.request_path(SID, PID, tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text(_fixture_text(), encoding="utf-8")
    assert ops_mod.request_exists(p)
    obj, existed = ops_mod.take_request(p)
    assert existed and obj["v"] == 1
    assert not p.exists()


def test_take_request_missing_file(tmp_path):
    assert ops_mod.take_request(tmp_path / "nope.req.json") == (None, False)
    assert ops_mod.take_request(None) == (None, False)


def test_take_request_corrupt_json_is_deleted(tmp_path):
    p = tmp_path / "x.req.json"
    p.write_text("{not json", encoding="utf-8")
    assert ops_mod.take_request(p) == (None, True)
    assert not p.exists()


def test_take_request_oversize_is_ignored_and_deleted(tmp_path):
    p = tmp_path / "x.req.json"
    p.write_text(json.dumps({"v": 1, "pad": "x" * (ops_mod.MAX_REQUEST_BYTES + 10)}), encoding="utf-8")
    assert ops_mod.take_request(p) == (None, True)
    assert not p.exists()


def test_parse_fixture_has_no_rejections():
    ops = ops_mod.parse_request(json.loads(_fixture_text()))
    assert ops.exclude == ("项目笔记/demo/a.md",)
    assert ops.pin == ("项目笔记/demo/b.md",)
    assert ops.unpin_llm == ()
    assert len(ops.annotations) == 1 and ops.annotations[0]["id"] == "ann-00000001"
    assert ops.settings == {"fine_max_notes": 2}
    assert ops.persist == ("fine_max_notes",)
    assert ops.reads == ("项目笔记/demo/c.md",)
    assert ops.rejected == ()


@pytest.mark.parametrize("obj", [None, [], "x", {"v": 2}, {"exclude": []}])
def test_bad_request_rejected_whole(obj):
    ops = ops_mod.parse_request(obj)
    assert ops.exclude == ops.pin == ops.unpin_llm == ops.annotations == ()
    assert ops.settings == {} and ops.persist == ops.reads == ()
    assert [r["reason"] for r in ops.rejected] == ["bad_request"]


def test_non_list_fields_become_empty():
    ops = ops_mod.parse_request({"v": 1, "exclude": "a.md", "pin": {"a": 1}, "annotations": "x"})
    assert ops.exclude == () and ops.pin == () and ops.annotations == ()


def test_lists_are_truncated_before_validation_and_over_cap_reported():
    raw = [f"n{i}.md" for i in range(ops_mod.MAX_EXCLUDE + 5)]
    ops = ops_mod.parse_request({"v": 1, "exclude": raw})
    assert len(ops.exclude) == ops_mod.MAX_EXCLUDE
    assert {"what": "exclude", "key": "", "reason": "over_cap"} in ops.rejected


def test_bad_items_rejected_individually():
    ops = ops_mod.parse_request({"v": 1, "pin": ["ok.md", 3, "", "x" * 600, "ok.md"]})
    assert ops.pin == ("ok.md",)
    assert sum(1 for r in ops.rejected if r["reason"] == "bad_item") == 3


@pytest.mark.parametrize("field,value", [
    ("id", "short"), ("id", "bad id!"), ("kind", "near_miss"), ("kind", "x"),
    ("verdict", "maybe"), ("session", "sess-1"), ("prompt_h", "XYZ"), ("ts", "now"),
    ("ts", True), ("path", ""),
])
def test_annotation_field_validation(field, value):
    good = json.loads(_fixture_text())["annotations"][0]
    bad = dict(good, **{field: value})
    ops = ops_mod.parse_request({"v": 1, "annotations": [bad, good]})
    assert [a["id"] for a in ops.annotations] == ["ann-00000001"]
    assert any(r["what"] == "annotation" and r["reason"] == "bad_item" for r in ops.rejected)


def test_digest_matches_independent_canonical_text():
    obj = json.loads(_fixture_text())
    text = ('[1,["项目笔记/demo/a.md"],["项目笔记/demo/b.md"],[],["ann-00000001"],'
            '{"fine_max_notes":2},["fine_max_notes"],["项目笔记/demo/c.md"]]')
    want = hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
    # mod 侧 kit 用例钉同一个值（tests/mod/vault-mod.test.ts 的 matching digest 用例）
    assert want == "a0aa76f4f72077b40c8676494f9f2ca2"
    assert ops_mod.request_digest(obj) == want
    assert ops_mod.parse_request(obj).digest == want


def test_digest_counts_missing_new_keys_as_empty():
    # 2.3.0 形态的请求没有 settings / persist / reads：三项按空值参与摘要（mod 侧恒发三个键）
    text = '[1,["笔记/alpha.md"],[],[],[],{},[],[]]'
    want = hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
    assert want == "74499db0ee2aaa076f8e8954de1e6775"
    assert ops_mod.request_digest({"v": 1, "exclude": ["笔记/alpha.md"]}) == want


@pytest.mark.parametrize("bad", ["x", ["a"], 3])
def test_digest_counts_non_dict_settings_as_empty(bad):
    # settings 不是对象时与 {} 同值：mod 恒发对象，Python 对畸形请求不能算出另一个摘要
    assert ops_mod.request_digest({"v": 1, "exclude": ["笔记/alpha.md"], "settings": bad}) \
        == "74499db0ee2aaa076f8e8954de1e6775"


def test_restrict_to_index_moves_unknown_paths_to_rejected():
    ops = ops_mod.parse_request({"v": 1, "exclude": ["a.md", "gone.md"], "pin": ["b.md"],
                                 "unpin_llm": ["c.md"]})
    r = ops_mod.restrict_to_index(ops, {"a.md", "b.md"})
    assert r.exclude == ("a.md",) and r.pin == ("b.md",) and r.unpin_llm == ()
    keys = {(x["what"], x["key"], x["reason"]) for x in r.rejected}
    assert ("exclude", "gone.md", "not_in_index") in keys
    assert ("unpin_llm", "c.md", "not_in_index") in keys


def test_excluded_set_unions_exclude_and_unpin():
    ops = ops_mod.parse_request({"v": 1, "exclude": ["a.md"], "unpin_llm": ["c.md"]})
    assert ops_mod.excluded_set(ops) == frozenset({"a.md", "c.md"})
    assert ops_mod.excluded_set(None) == frozenset()


def test_ann_constants_track_analyze_metrics():
    from scripts import analyze_metrics as am
    assert set(ops_mod.ANN_KINDS) == set(am.KINDS) - {"near_miss"}
    assert tuple(ops_mod.ANN_VERDICTS) == tuple(am.VERDICTS)


# ── 契约 v1 增量（spec 2026-10-13 §10.6）：settings / persist / reads ─────────────────────

def test_settings_keep_structural_scalars_and_reject_the_rest():
    raw = {"fine_rank": False, "fine_max_notes": 2, "fine_model": "sonnet", "not_a_panel_key": True,
           "f": 1.5, "n": None, "l": [1], "long": "x" * 65, "": True}
    ops = ops_mod.parse_request({"v": 1, "settings": raw})
    # 结构合格的原样保留：键认不认识、值合不合法由 _fine_config 判，这里只拦结构
    assert ops.settings == {"fine_rank": False, "fine_max_notes": 2, "fine_model": "sonnet",
                            "not_a_panel_key": True}
    bad = {(r["what"], r["key"], r["reason"]) for r in ops.rejected}
    assert bad == {("settings", k, "bad_item") for k in ("f", "n", "l", "long", "")}


def test_settings_over_cap_is_truncated_and_reported():
    raw = {f"k{i}": True for i in range(ops_mod.MAX_SETTINGS + 3)}
    ops = ops_mod.parse_request({"v": 1, "settings": raw})
    assert len(ops.settings) == ops_mod.MAX_SETTINGS == 10
    assert {"what": "settings", "key": "", "reason": "over_cap"} in ops.rejected


@pytest.mark.parametrize("raw", ["x", [], 3, None])
def test_non_dict_settings_become_empty(raw):
    assert ops_mod.parse_request({"v": 1, "settings": raw}).settings == {}


def test_parse_request_marks_whether_settings_key_was_sent():
    """2.3.0 形态的请求不带 settings 键（缺键 = 沿用已存的会话设置），与显式的 `settings: {}`（清空）
    必须分得开：两者解析出的 `settings` 都是 {}。整份作废的请求不算带了键。"""
    assert ops_mod.parse_request({"v": 1, "exclude": ["a.md"]}).has_settings is False
    assert ops_mod.parse_request({"v": 1, "settings": {}}).has_settings is True
    assert ops_mod.parse_request({"v": 1, "settings": {"fine_rank": False}}).has_settings is True
    assert ops_mod.parse_request({"v": 2, "settings": {}}).has_settings is False


@pytest.mark.parametrize("raw", ["x", [], 3, None])
def test_non_dict_settings_count_as_not_sent(raw):
    """键在、值不是对象（null / 字符串 / 列表 / 数字）：按「没带」处理、沿用已存的会话设置——只有显式的
    `{}` 才清空（修复轮 Minor 3）。值仍按结构校验变成 {}，摘要也照旧把它算成 {}。"""
    ops = ops_mod.parse_request({"v": 1, "settings": raw})
    assert ops.has_settings is False and ops.settings == {}


@pytest.mark.parametrize("obj", [{"v": 1, "exclude": ["gone.md"]},
                                 {"v": 1, "exclude": ["gone.md"], "settings": {}}],
                         ids=["no_key", "empty"])
def test_restrict_to_index_keeps_has_settings(obj):
    ops = ops_mod.parse_request(obj)
    assert ops_mod.restrict_to_index(ops, set()).has_settings is ops.has_settings


def test_persist_accepts_only_panel_keys():
    ops = ops_mod.parse_request({"v": 1, "persist": ["fine_max_notes", "vault_path",
                                                      "fine_max_notes", 3]})
    assert ops.persist == ("fine_max_notes",)
    keys = {(r["what"], r["key"], r["reason"]) for r in ops.rejected}
    assert ("persist", "vault_path", "bad_item") in keys
    assert ("persist", "", "bad_item") in keys            # 非 str 的那条


def test_persist_over_cap_is_reported():
    ops = ops_mod.parse_request({"v": 1, "persist": ["fine_rank"] * (ops_mod.MAX_PERSIST + 1)})
    assert ops.persist == ("fine_rank",)
    assert {"what": "persist", "key": "", "reason": "over_cap"} in ops.rejected


def test_reads_are_filtered_silently():
    good = "项目笔记/demo/c.md"
    raw = [good, "/abs/a.md", "C:/x/a.md", "a/../b.md", "a.txt", "x" * 600 + ".md", good, 7]
    ops = ops_mod.parse_request({"v": 1, "reads": raw})
    assert ops.reads == (good,)
    assert ops.rejected == ()             # 观测数据：不合格的静默丢弃，不占 rejected 的名额


def test_reads_are_capped():
    raw = [f"n{i}.md" for i in range(ops_mod.MAX_READS + 5)]
    ops = ops_mod.parse_request({"v": 1, "reads": raw})
    assert len(ops.reads) == ops_mod.MAX_READS == 50


def test_settings_structural_boundaries():
    # 边界值硬编码，不从被测常量反算；常量本身另用字面量钉住
    assert ops_mod.MAX_SETTING_KEY_CHARS == ops_mod.MAX_SETTING_STR_CHARS == 64
    raw = {"k" * 64: 10 ** 9, "neg": -(10 ** 9), "s64": "x" * 64,       # 恰在边界上：保留
           "k" * 65: True, "big": 10 ** 9 + 1, "negbig": -(10 ** 9) - 1,
           "empty": ""}                                                 # 越界：拒收
    ops = ops_mod.parse_request({"v": 1, "settings": raw})
    assert ops.settings == {"k" * 64: 10 ** 9, "neg": -(10 ** 9), "s64": "x" * 64}
    bad = sorted((r["what"], r["key"], r["reason"]) for r in ops.rejected)
    # 65 字符的键在 rejected 里截到 64 字符
    assert bad == sorted([("settings", "k" * 64, "bad_item"), ("settings", "big", "bad_item"),
                          ("settings", "negbig", "bad_item"), ("settings", "empty", "bad_item")])
