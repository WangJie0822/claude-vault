# -*- coding: utf-8 -*-
"""near_miss_scorelow 生成侧分流 + annotate 注入长度，两组新行为的守卫。

背景（真实数据实测，2026-08-25，1018 条记录）：达 nudge 阈值的 211 篇笔记里
78.2% 其实已被注入过、43.6% 甚至被全文注入过——提示的语义整个是反的。
根因不是"消费侧忘了过滤"，而是**过滤时机**：`build_record` 先按 topical 取 top-k
才落盘，而被去重的条目 topical 结构性更高，于是 39% 的轮次里 score-low 一条都
进不了样本。在消费侧再过滤只能拿到残差。
"""
import json

import pytest

from scripts import _metrics
from scripts import analyze_metrics
from scripts._decision import Decision, EntryDecision


def _ed(path, topical, dedup=""):
    return EntryDecision(path=path, topical=topical, total=0.0, hits=[],
                         admitted=False, admit_arm="", dedup=dedup)


def _decision(excluded):
    return Decision(admitted=[], excluded=excluded, fulltext_path=None,
                    fulltext_arm="", gate_reason="", relaxed=False,
                    any_relevant=False)


def _build(excluded, tmp_path, near_miss_k=10):
    return _metrics.build_record(
        _decision(excluded), ["kw"], tmp_path,
        session_id="s", prompt_id="p", salt=_metrics.get_salt(tmp_path),
        src="", near_miss_k=near_miss_k)


# ── 生成侧分流 ────────────────────────────────────────────────────────────

def test_scorelow_survives_topk_occupied_by_suppressed(tmp_path):
    """**本轮的核心守卫**：即使 top-k 被去重条目占满，score-low 仍须落盘。

    构造复刻真实的结构性分层——被去重的 topical 一律 9.5+（它们过了闸门才会被
    去重），score-low 一律 <4（`_decision.py` 里新篇落 excluded 的条件就是
    `t < min_topical`）。k=10 而去重条目有 12 个 ⇒ 旧的 `near_miss` 会被占满。

    变异验证：把 build_record 的 near_scorelow 改回从 `ranked[:k]` 里筛，本用例
    立刻转红（scorelow 为空）。
    """
    excluded = [_ed(f"sup{i}.md", 9.5 + i * 0.1, "fulltext_injected") for i in range(12)]
    excluded += [_ed(f"low{i}.md", 3.0 + i * 0.1) for i in range(5)]
    rec = _build(excluded, tmp_path, near_miss_k=10)

    near_paths = [nm["path"] for nm in rec["near_miss"]]
    assert all(p.startswith("sup") for p in near_paths), (
        "前提失效：本用例要复刻的正是『top-k 被去重条目占满』，"
        f"实际 near_miss={near_paths}")

    scorelow = [nm["path"] for nm in rec["near_miss_scorelow"]]
    assert scorelow, "top-k 被占满时 score-low 必须仍有独立样本（这正是缺陷所在）"
    assert set(scorelow) == {f"low{i}.md" for i in range(5)}
    assert all("dedup" not in nm for nm in rec["near_miss_scorelow"]), \
        "scorelow 按定义只含 dedup 为空的条目，无需再落该字段"


def test_scorelow_excludes_every_dedup_kind(tmp_path):
    """三种 dedup 取值只有 `\"\"` 算真擦肩 —— `_decision.py` 的三处赋值各覆盖一次。"""
    excluded = [_ed("a.md", 3.5), _ed("b.md", 9.0, "fulltext_injected"),
                _ed("c.md", 5.0, "candidate_injected")]
    rec = _build(excluded, tmp_path)
    assert [nm["path"] for nm in rec["near_miss_scorelow"]] == ["a.md"]


def test_scorelow_respects_k(tmp_path):
    rec = _build([_ed(f"n{i}.md", 3.0) for i in range(30)], tmp_path, near_miss_k=4)
    assert len(rec["near_miss_scorelow"]) == 4


def test_scorelow_sorted_by_topical_desc(tmp_path):
    # 三条都取 floor 之上：本用例只测排序，不该被 floor 干扰
    # （floor 本身由 test_scorelow_applies_floor_at_generation 单独钉）。
    rec = _build([_ed("lo.md", 3.1), _ed("hi.md", 3.9), _ed("mid.md", 3.5)], tmp_path)
    assert [nm["path"] for nm in rec["near_miss_scorelow"]] == ["hi.md", "mid.md", "lo.md"]


def test_scorelow_applies_floor_at_generation(tmp_path):
    """**落盘时**就排除 topical < NUDGE_TOPICAL_FLOOR 的条目，不留给消费侧过滤。

    这是本轮 High finding 的修复：上一版只在 `scorelow_paths` 里过滤，而
    `summarize` 榜单与 `sample_near_miss` 抽样池各自内联读裸键、都没施加下限。
    判据下沉到生成侧后，磁盘上根本不存在低于 floor 的条目，三处无从分叉。

    变异验证：把 build_record 的 `and ed.topical >= NUDGE_TOPICAL_FLOOR` 去掉，
    本用例转红（below/zero 会出现在落盘样本里）。
    """
    excluded = [_ed("above.md", 3.5), _ed("exact.md", _metrics.NUDGE_TOPICAL_FLOOR),
                _ed("below.md", 2.9), _ed("zero.md", 0.0)]
    rec = _build(excluded, tmp_path)
    assert [nm["path"] for nm in rec["near_miss_scorelow"]] == ["above.md", "exact.md"], \
        "floor 是闭区间下界：恰好等于 floor 的条目必须留下，低于的必须落掉"


def test_scorelow_floor_value_is_persisted(tmp_path):
    """生效阈值随记录落盘 —— 否则日后调 floor，新旧记录混在一起分不清口径。"""
    rec = _build([_ed("a.md", 3.5)], tmp_path)
    assert rec["scorelow_floor"] == _metrics.NUDGE_TOPICAL_FLOOR


def test_max_notes_is_persisted_by_build_record(tmp_path):
    """渲染层的 max_notes 随记录落盘，供 analyze_metrics 用（它不读 config）。

    ⚠️ 本用例必须从 **build_record 的真实产出**验证，不能用 `write_record` 手写记录。
    最初 `test_render_span_uses_persisted_max_notes` 就是手写的，于是「删掉
    build_record 的 max_notes 落盘」这个变异**不转红** —— 用例绕过了被测的那一层，
    看起来像守卫无判别力，实际是它压根没测生产通路。同一个坑本仓库已记过一次
    （`test_sample_admitted_accepts_real_build_record_output` 就是当时补的）。

    变异验证：删掉 build_record 里的 `"max_notes": max_notes,`，本用例转红。
    """
    rec = _build([_ed("a.md", 3.5)], tmp_path)
    assert rec["max_notes"] == 3, "未显式传参时落默认值"
    rec7 = _metrics.build_record(
        _decision([_ed("a.md", 3.5)]), ["kw"], tmp_path,
        session_id="s", prompt_id="p", salt=b"salt", src="", max_notes=7)
    assert rec7["max_notes"] == 7, "显式传入的配置值必须原样落盘"


def test_all_three_consumers_share_one_scorelow_judgement(tmp_path):
    """nudge 计数 / 报表榜单 / 标注抽样池对同一条记录必须给出同一批 path。

    **这条守卫是本轮 High finding 的直接产物**：上一版四处文档（含 CLAUDE.md 与
    commit message）声明「三个消费者一律走 scorelow_paths 单点」，实际只有 flush
    在调，另两个各自内联、双双漏掉 floor。当时没有任何用例断言这三者一致 ——
    有的话，那条声明当场就会被证伪。

    刻意混入一条低于 floor 的记录（模拟判据下沉**之前**落盘的旧记录）：
    生成侧那道防线对它不起作用，只有消费侧的二次判据能挡住。

    变异验证：把 `summarize` 或 `sample_near_miss` 改回内联读裸键，本用例转红。
    """
    rec = {"_schema": _metrics.SCHEMA, "gate": "",
           "near_miss_scorelow": [{"path": "zero.md", "topical": 0.0},
                                  {"path": "ok.md", "topical": 3.5}]}
    nudge = set(_metrics.scorelow_paths(rec))
    board = {p for p, _ in analyze_metrics.summarize([rec])["near_miss_top"]}
    pool = {x["path"] for x in analyze_metrics.sample_near_miss([rec])}
    assert nudge == board == pool == {"ok.md"}, \
        f"三处判据分叉：nudge={nudge} board={board} pool={pool}"


# ── scorelow_paths：三个消费者的共用判据 ─────────────────────────────────

def test_scorelow_paths_applies_topical_floor():
    """topical 下限：score-low 的**下界是 0**，不是"差一点"。

    实测该批 topical 中位数只有 2.0、9.5% 恰为 0，而 nudge 文案写的是「反复接近
    召回闸门」并建议「检查 tags/keywords」——对 topical=0 的笔记这是错的指引。
    """
    rec = {"near_miss_scorelow": [
        {"path": "high.md", "topical": 3.5},
        {"path": "atfloor.md", "topical": _metrics.NUDGE_TOPICAL_FLOOR},
        {"path": "zero.md", "topical": 0.0},
        {"path": "below.md", "topical": 2.9},
    ]}
    assert _metrics.scorelow_paths(rec) == ["high.md", "atfloor.md"]


def test_nudge_topical_floor_is_pinned():
    """钉死取值：fixture 里的边界数（2.9 / 3.5）是按它挑的，改常量必须同步改用例。

    不写成 `>= FLOOR` 的自证式断言——那样改常量时测试恒绿，钉住的就不再是这个值。
    """
    assert _metrics.NUDGE_TOPICAL_FLOOR == 3.0


def test_scorelow_paths_returns_empty_for_legacy_records():
    """旧记录不回退过滤 `near_miss`：那份样本已被去重条目挤占，过滤只得偏斜残差。"""
    assert _metrics.scorelow_paths(
        {"near_miss": [{"path": "old.md", "topical": 3.5, "dedup": ""}]}) == []
    assert _metrics.scorelow_paths({}) == []


def test_scorelow_paths_tolerates_malformed_entries():
    """手工改坏的 jsonl 不得让 **CLI** 抛异常。

    ⚠️ 本用例上一版是一张**空网**：docstring 写「不得让 hook 抛异常」，fixture 却只有
    非 dict / 缺 path / 空 path / topical=None 四种——**每一种都落在已有守卫内侧，
    一条会抛异常的都没有**。实测真正会抛的是 `topical` 为字符串（ValueError）、
    非空 dict / list（TypeError），全部没被覆盖。

    docstring 的**理由**当时也是错的：`scorelow_paths` 的 hook 侧调用点（`flush`）
    喂的是 `build_record` 的内存产物、类型恒正确，那条路径根本不可达。真实理由是
    CLI 侧——`summarize` / `sample_near_miss` 消费的是磁盘 jsonl，可被手工编辑或
    位翻转，且 `--report`/`--review` 崩掉等于排障入口自己先罢工。
    """
    rec = {"near_miss_scorelow": [
        "not-a-dict", {"no_path": 1}, {"path": "", "topical": 9},
        {"path": "ok.md", "topical": 3.5}, {"path": "null.md", "topical": None},
        # ↓ 这三种在加守卫前会抛异常，是这张网原先漏掉的全部形态
        {"path": "str.md", "topical": "high"},      # ValueError
        {"path": "obj.md", "topical": {"a": 1}},    # TypeError（空 dict 走 `or 0` 不抛）
        {"path": "list.md", "topical": [1]},        # TypeError
    ]}
    assert _metrics.scorelow_paths(rec) == ["ok.md"]


@pytest.mark.parametrize("bad", ["high", {"a": 1}, [1], object()])
def test_scorelow_entries_never_raises_on_bad_topical(bad):
    """逐个形态单独断言不抛 —— 上一条是聚合断言，一条不抛就掩盖其余。

    变异验证：把 `scorelow_entries` 的 try/except 去掉，本用例的前三个参数转红。
    """
    rec = {"near_miss_scorelow": [{"path": "x.md", "topical": bad}]}
    assert _metrics.scorelow_entries(rec) == []


# ── flush 侧接线 ─────────────────────────────────────────────────────────

def test_flush_bumps_only_scorelow(tmp_path):
    """端到端：flush 落盘并只为 score-low 计数。

    变异验证：把 flush 的 `scorelow_paths(rec)` 改回
    `[nm.get("path","") for nm in rec.get("near_miss") or []]`，本用例转红
    （sup.md 会出现在计数里）。
    """
    _metrics.reset()
    excluded = [_ed("sup.md", 9.9, "fulltext_injected"), _ed("low.md", 3.5)]
    _metrics.stage(_build(excluded, tmp_path))
    _metrics.flush(tmp_path)
    counts = _metrics.load_near_miss_counts(tmp_path)
    assert counts == {"low.md": 1}, f"被去重抑制的笔记不得计入 nudge 计数：{counts}"


def test_flush_on_gate_record_touches_nothing(tmp_path):
    """极简 gate 记录（无 near_miss* 键）不得让 flush 抛异常或建计数文件。

    实测 gate 轮次占 25.5%，这条路径每天都在走。
    """
    _metrics.reset()
    _metrics.stage({"_schema": _metrics.SCHEMA, "ts": 1.0, "session": "s",
                    "prompt_id": "p", "gate": "too_few_keywords"})
    _metrics.flush(tmp_path)          # 不抛异常即通过
    assert not (_metrics.metrics_dir(tmp_path) / "near_miss_counts.json").exists()


# ── annotate ─────────────────────────────────────────────────────────────

def test_annotate_noop_when_nothing_staged(tmp_path):
    """**opt-in 边界**：metrics 关闭时 `_PENDING` 恒为 None，annotate 必须直接返回。

    若写成 `_PENDING = _PENDING or {}`，会凭空造出一条非空记录并被 flush()
    （只看 `_PENDING` 真值）落盘，正好从这个缝里破掉零足迹。
    """
    _metrics.reset()
    _metrics.annotate(inj_chars=123)
    _metrics.flush(tmp_path)
    assert not list(_metrics.metrics_dir(tmp_path).rglob("*.jsonl")), \
        "metrics 未 stage 时 annotate 不得产生任何落盘"


def test_annotate_writes_allowed_key(tmp_path):
    _metrics.reset()
    _metrics.stage({"_schema": _metrics.SCHEMA, "session": "s"})
    _metrics.annotate(inj_chars=4096)
    _metrics.flush(tmp_path)
    rec = json.loads(next(_metrics.metrics_dir(tmp_path).rglob("*.jsonl"))
                     .read_text(encoding="utf-8").splitlines()[0])
    assert rec["inj_chars"] == 4096


def test_annotate_rejects_keys_outside_allowlist(tmp_path):
    """白名单而非任意 kwargs：`_PENDING` 是隐私域记录（含 kw_h/cwd_h），
    无约束的 update 等于给它开一个通用可写入口。"""
    _metrics.reset()
    _metrics.stage({"_schema": _metrics.SCHEMA, "session": "s"})
    _metrics.annotate(prompt_text="用户原文", inj_chars=10)
    assert _metrics._PENDING is not None
    assert "prompt_text" not in _metrics._PENDING
    assert _metrics._PENDING["inj_chars"] == 10


def test_annotate_does_not_overwrite_existing_key():
    _metrics.reset()
    _metrics.stage({"_schema": _metrics.SCHEMA, "inj_chars": 1})
    _metrics.annotate(inj_chars=999)
    assert _metrics._PENDING["inj_chars"] == 1


# ── reset_counts ─────────────────────────────────────────────────────────

def test_reset_counts_clears_counts_and_cooldown_but_keeps_annotations(tmp_path):
    """它是**派生数据**清理，不是 purge：annotations 与事件记录必须原样保留。

    `purge()` 的 docstring 自己把 near_miss_counts.json / nudge_ts.json 归为
    「派生 json」，与「删了不可重新生成」的 annotations 明确分开。
    """
    from scripts.analyze_metrics import save_annotation, load_annotations
    _metrics.bump_near_miss_counts(tmp_path, ["a.md", "b.md"])
    _metrics.mark_nudged(tmp_path)
    save_annotation(tmp_path, "a.md", "relevant")
    _metrics.reset()
    _metrics.stage({"_schema": _metrics.SCHEMA, "session": "s"})
    _metrics.flush(tmp_path)

    n = _metrics.reset_counts(tmp_path)
    assert n == 2
    assert _metrics.load_near_miss_counts(tmp_path) == {}
    assert not (_metrics.metrics_dir(tmp_path) / "nudge_ts.json").exists()
    assert load_annotations(tmp_path), "人工标注不可再生，reset_counts 不得删它"
    assert list(_metrics.metrics_dir(tmp_path).rglob("*/*.jsonl")), "事件记录必须保留"


def test_reset_counts_on_empty_dir_is_safe(tmp_path):
    assert _metrics.reset_counts(tmp_path) == 0
