"""`--review` 的标注上下文：优先挑可读的、如实归因、附上命中词。

实测背景（2026-09-01）：精度侧 13 条人工标注里 12 条是 `unsure`（92.3%），而
召回侧旧批 20 条是 0 unsure。差别不在人，在界面——标注池 40 条、105 行上下文里
**33.3% 是乱码、12.4% 回查不到**，近一半没有信息量。

两类不可读的成因完全不同，此前被同一句「（transcript 里找不到，可能已被清理）」
一并解释掉了：

- **乱码**：transcript 文件里存的就是 U+FFFD（写入方在存盘前已解码坏），文件本身
  是合法 UTF-8。不可逆，改编码救不回来。
- **回查不到**：实测 50 条未解析里 48 条 transcript **就在盘上**，只是 hook 拿到的是
  原始 prompt、transcript 存的是 slash command 展开后的形态，hash 必然不等。

把两者都说成「可能已被清理」，标注者会以为是历史被清了，而真正原因永远不会浮现。
"""

from scripts.analyze_metrics import (format_context_lines, pick_readable_contexts,
                                     sample_events_with_hits)

BAD = "�"


def _rec(path, hits, session="s", ph="h", kind="admitted_list"):
    base = {"session": session, "prompt_h": ph, "near_miss_scorelow": []}
    if kind == "admitted_fulltext":
        base["ft"] = {"path": path, "arm": "x"}
        base["admitted"] = [{"path": path, "topical": 9.0, "hits": hits}]
    elif kind == "admitted_list":
        base["admitted"] = [{"path": path, "topical": 9.0, "hits": hits}]
    else:
        base["near_miss_scorelow"] = [{"path": path, "topical": 3.5}]
    return base


# ── 命中词：已经落盘的明文，白付了隐私代价却零展示 ──────────────────────

def test_events_carry_hits():
    """事件要带上该笔记当轮的命中词。

    `admitted[].hits` 是设计上刻意用明文换来的字段（占 7.2% 落盘体积），
    而在此之前 `analyze_metrics` 对它的引用数是 0 —— 代价付了、收益一次没兑现。

    变异验证：让返回值丢掉 hits，本用例转红。
    """
    recs = [_rec("n/a.md", ["内存", "泄露"], session="s1", ph="h1")]
    got = sample_events_with_hits(recs, "n/a.md", "admitted_list")
    assert got == [("s1", "h1", ["内存", "泄露"])]


def test_events_hits_empty_for_near_miss():
    """near-miss 条目在 _decision 里 hits 是未计算的占位值，不能假装有。"""
    recs = [_rec("n/b.md", [], session="s1", ph="h1", kind="near_miss")]
    got = sample_events_with_hits(recs, "n/b.md", "near_miss")
    assert got == [("s1", "h1", [])]


# ── 优先挑可读的上下文 ──────────────────────────────────────────────────

def _resolver(mapping):
    return lambda s, ph: mapping.get((s, ph), "")


def test_picks_readable_and_skips_corrupt():
    """乱码与查不到的都跳过，从候选池里补足可读的。

    变异验证：去掉可读性筛选（按原顺序取前 want 条），本用例转红。
    """
    events = [("s1", "h1", []), ("s2", "h2", []), ("s3", "h3", []), ("s4", "h4", [])]
    items, reasons, _ids = pick_readable_contexts(
        events,
        _resolver({("s1", "h1"): "# " + BAD + BAD + " 元信息",   # 乱码
                   ("s2", "h2"): "",                              # 查不到
                   ("s3", "h3"): "这次问的是内存泄露",
                   ("s4", "h4"): "另一个可读的提问"}),
        want=2)
    assert [t for t, _ in items] == ["这次问的是内存泄露", "另一个可读的提问"]
    assert reasons == {"corrupt": 1, "unresolved": 1}


def test_stops_at_want_without_scanning_rest():
    """凑够 want 条就停 —— 每条都要回查 transcript，不能白扫。"""
    calls = []

    def resolve(s, ph):
        calls.append(s)
        return "可读的提问"

    events = [(f"s{i}", f"h{i}", []) for i in range(10)]
    items, reasons, _ids = pick_readable_contexts(events, resolve, want=3)
    assert len(items) == 3
    assert len(calls) == 3, f"多回查了 {len(calls) - 3} 次"
    assert reasons == {"corrupt": 0, "unresolved": 0}


def test_all_unreadable_reports_both_reasons():
    events = [("s1", "h1", []), ("s2", "h2", [])]
    items, reasons, _ids = pick_readable_contexts(
        events, _resolver({("s1", "h1"): BAD * 3, ("s2", "h2"): ""}), want=3)
    assert items == []
    assert reasons == {"corrupt": 1, "unresolved": 1}


# ── 归因文案：不得再把两类原因都说成「可能已被清理」 ────────────────────

def test_lines_state_specific_reasons_not_cleanup():
    """两类不可读要分别如实归因。

    变异验证：把文案改回「可能已被清理」，本用例转红。
    """
    lines = "\n".join(format_context_lines([], {"corrupt": 2, "unresolved": 1}))
    assert "已被清理" not in lines, "仍在用那句会误导的归因"
    assert "编码" in lines or "损坏" in lines, "没说明乱码那一类的真实成因"
    assert "展开" in lines or "对不上" in lines, "没说明回查不到那一类的真实成因"


def test_lines_show_hits():
    lines = "\n".join(format_context_lines(
        [("这次问的是内存泄露", ["内存", "泄露"])], {"corrupt": 0, "unresolved": 0}))
    assert "内存" in lines and "泄露" in lines
    assert "命中" in lines, "没有把命中词标出来，标注者看不出这篇为什么被召回"


# ── 零可读上下文的条目直接剔除 ──────────────────────────────────────────
#
# 用户实测反馈：标注界面出现「── 无提问上下文（该条目写于 prompt_h 落盘之前）──」
# 的条目，只能盲标 unsure。这类条目对精度评估零贡献，却占满标注池的名额，而人工
# 标注是唯一不可再生的数据——把名额让给能判断的条目。

def test_drops_items_without_readable_context():
    """一条上下文都读不出来的条目不进标注池。

    变异验证：去掉过滤（零上下文也保留），本用例转红。
    """
    from scripts.analyze_metrics import attach_contexts
    recs = [_rec("n/ok.md", ["内存"], session="s1", ph="h1"),
            _rec("n/blind.md", [], session="s2", ph="h2")]
    items = [{"path": "n/ok.md", "kind": "admitted_list", "count": 3},
             {"path": "n/blind.md", "kind": "admitted_list", "count": 9}]
    got = attach_contexts(items, lambda: iter(recs),
                          _resolver({("s1", "h1"): "这次问的是内存泄露"}))
    assert [x["path"] for x in got] == ["n/ok.md"],         f"零可读上下文的条目没被剔除：{[x['path'] for x in got]}"


def test_keeps_original_fields_and_attaches_context():
    from scripts.analyze_metrics import attach_contexts
    recs = [_rec("n/ok.md", ["内存", "泄露"], session="s1", ph="h1")]
    items = [{"path": "n/ok.md", "kind": "admitted_list", "count": 7}]
    got = attach_contexts(items, lambda: iter(recs),
                          _resolver({("s1", "h1"): "问题是内存泄露"}))
    assert len(got) == 1
    it = got[0]
    assert it["count"] == 7 and it["kind"] == "admitted_list"
    assert it["contexts"] == [("问题是内存泄露", ["内存", "泄露"])]
    assert it["unreadable"] == {"corrupt": 0, "unresolved": 0}
