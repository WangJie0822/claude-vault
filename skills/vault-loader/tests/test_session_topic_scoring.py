# -*- coding: utf-8 -*-
"""会话主题词作为独立打分信号。

刻意**不并入 prompt_keywords**（spec §3.3.2 C5-1）：并进去会同时进
prompt_submit_load 的 shown_hits_str 回显路径（该行不经 sanitize），
且按 _hit_keywords 全口径膨胀（实测裸主题词使 admitted 涨 5.5 倍）。
"""
from __future__ import annotations

import copy

from scripts._config_loader import DEFAULT_CONFIG
from scripts._frontmatter_reader import Entry
from scripts._scorer import Signals, topical_score

W = DEFAULT_CONFIG["scoring"]
E = Entry(path="n.md", tags=("召回",), summary="讲召回闸门的笔记",
          keywords=("闸门",), mtime=0)


def test_topic_word_adds_score() -> None:
    base = topical_score(E, Signals(prompt_keywords=set()), W)
    hit = topical_score(E, Signals(prompt_keywords=set(),
                                   session_topic_words={"召回"}), W)
    assert hit == base + W["session_topic_hit"]


def test_topic_word_miss_adds_nothing() -> None:
    base = topical_score(E, Signals(prompt_keywords=set()), W)
    miss = topical_score(E, Signals(prompt_keywords=set(),
                                    session_topic_words={"完全无关的词"}), W)
    assert miss == base


def test_multiple_topic_hits_do_not_stack() -> None:
    """多个主题词命中同一篇只加一次 —— 与 tag 面「取 max 不累加」同构，防堆砌刷分。"""
    one = topical_score(E, Signals(session_topic_words={"召回"}), W)
    many = topical_score(E, Signals(session_topic_words={"召回", "闸门", "笔记"}), W)
    assert one == many


def test_topic_word_hits_via_tag_only() -> None:
    """仅经由 tag 面命中（summary/keywords/path 均不含该词）。"""
    e = Entry(path="tag-only-note.md", tags=("仅标签命中词",),
              summary="不相关的摘要内容", keywords=("不相关关键词",), mtime=0)
    base = topical_score(e, Signals(prompt_keywords=set()), W)
    hit = topical_score(e, Signals(prompt_keywords=set(),
                                   session_topic_words={"仅标签命中词"}), W)
    assert hit == base + W["session_topic_hit"]


def test_topic_word_hits_via_summary_only() -> None:
    """仅经由 summary 面命中（tag/keywords/path 均不含该词）。"""
    e = Entry(path="summary-only-note.md", tags=("不相关标签",),
              summary="正文含仅摘要命中词在其中", keywords=("不相关关键词",), mtime=0)
    base = topical_score(e, Signals(prompt_keywords=set()), W)
    hit = topical_score(e, Signals(prompt_keywords=set(),
                                   session_topic_words={"仅摘要命中词"}), W)
    assert hit == base + W["session_topic_hit"]


def test_topic_word_hits_via_keywords_only() -> None:
    """仅经由 keywords 面命中（tag/summary/path 均不含该词）。

    评审 Finding 1 的直接回归守卫：旧实现用 `_keyword_hits_entry` 判命中，该函数
    docstring 自陈只查 tags/summary/path，**不查 keywords 字段**——本用例在修复前会失败
    （delta=0 而非 +session_topic_hit），因为 fixture 之前用的共享 `E` 让 "闸门" 同时出现
    在 summary 与 keywords 里，互相掩盖了这个漏洞。
    """
    e = Entry(path="keywords-only-note.md", tags=("不相关标签",),
              summary="不相关的摘要内容", keywords=("仅关键词命中词",), mtime=0)
    base = topical_score(e, Signals(prompt_keywords=set()), W)
    hit = topical_score(e, Signals(prompt_keywords=set(),
                                   session_topic_words={"仅关键词命中词"}), W)
    assert hit == base + W["session_topic_hit"]


def test_topic_word_via_path_only_does_not_score() -> None:
    """仅经由 path 命中——**不得**计入话题相关性，与 `_decision.py::_hit_keywords`
    的既有设计一致（其 docstring："path 命中不计入话题相关性——避免向主模型展示
    仅靠文件名命中的词、高估相关性"）。

    评审 Finding 1 的直接回归守卫：旧实现用 `_keyword_hits_entry` 判命中，该函数会检查
    path 字段，本用例在修复前会失败（delta=+session_topic_hit 而非 0）。
    """
    e = Entry(path="仅路径命中词.md", tags=("不相关标签",),
              summary="不相关的摘要内容", keywords=("不相关关键词",), mtime=0)
    base = topical_score(e, Signals(prompt_keywords=set()), W)
    hit = topical_score(e, Signals(prompt_keywords=set(),
                                   session_topic_words={"仅路径命中词"}), W)
    assert hit == base


def test_empty_topic_is_exactly_current_behavior() -> None:
    """空主题必须与改动前**逐值相同**，否则 opt-in 边界破了。

    **能力边界（评审 Finding 3）**：本测试两侧构造的 `Signals` 在 `session_topic_words`
    上取值完全相同（都是空 set，一边显式传、一边靠默认值），故测不出早退条件本身的
    布尔逻辑对错——例如把 `_prompt_topical_hits` 的早退条件从 `and` 误改成 `or`
    （模拟"要求两信号皆非空"的错误实现）后，本测试两侧仍同样短路成 0、照样 PASS。
    那类回归由 `test_prompt_keywords_alone_still_scores_when_topic_absent` 单独钉住。
    """
    for kws in (set(), {"召回"}, {"闸门", "笔记"}):
        a = topical_score(E, Signals(prompt_keywords=kws), W)
        b = topical_score(E, Signals(prompt_keywords=kws, session_topic_words=set()), W)
        assert a == b


def test_prompt_keywords_alone_still_scores_when_topic_absent() -> None:
    """守早退条件的「与」语义：`prompt_keywords` 非空、`session_topic_words` 空时，
    仍必须走完整打分路径，不能被误早退成 0。

    评审 Finding 3：这是 `test_empty_topic_is_exactly_current_behavior` 承诺但没能力
    兑现的那部分。若早退条件被误改成 `or`（要求两信号皆非空才继续），本断言会失败
    （错误地早退为 0）；正确的 `and` 语义下 E 的 tag/summary 命中"召回"，分数必然 > 0。
    """
    assert topical_score(E, Signals(prompt_keywords={"召回"}), W) > 0


def test_weight_zero_disables_the_signal() -> None:
    cfg = copy.deepcopy(W)
    cfg["session_topic_hit"] = 0
    base = topical_score(E, Signals(), cfg)
    assert topical_score(E, Signals(session_topic_words={"召回"}), cfg) == base


def test_upper_bound_invariant_documented() -> None:
    """上界不变量：新信号把 topical 上界从 11 抬到 11+w，而 relevance 段的三个阈值
    假定的是 11。本断言钉住「阈值仍小于原上界」——若日后有人把 session_topic_hit
    调到能单独越过 min_topical_score，召回集会被主题词单独撑开，那是另一个设计。
    """
    rel = DEFAULT_CONFIG["relevance"]
    assert W["session_topic_hit"] < rel["min_topical_score"], (
        "session_topic_hit 不得单独越过精度闸门：主题词是辅助信号，"
        "不能仅凭它把一篇笔记拉进召回集")
    legacy_max = (W["prompt_tag_hit"] + W["prompt_summary_hit"]
                  + W["prompt_keyword_hit"])
    assert legacy_max == 11, "原上界变了，spec §3.3.3 与本断言需同步复核"
    assert rel["fulltext_topical_threshold"] <= legacy_max


def test_topic_word_alone_can_cross_fulltext_threshold() -> None:
    """F3（整分支终审，2026-09-02，Ruling 16）：会话主题词**无法**单独越过
    `min_topical_score`（上一条测试钉住），但**可以**叠加 prompt 关键词已算出的
    topical 分，把一篇笔记推过 `fulltext_topical_threshold`（触发数千字全文注入）。

    `_config_loader.py` 与 SKILL.md 此前都写着"只能锦上添花"——只对精度闸门成立，
    对全文阈值不成立，这是假话。已裁定不改 `select_fulltext` 引入「含/不含主题词」
    两套 topical（成本大于收益），本测试把该行为**锁定**为已知且被测试钉住的行为：
    日后若判定不可接受，改 `select_fulltext` 时本测试会明确指出要改哪里。

    场景来自真实复现（tag-IDF 折扣场景）：一个被 5/10 篇笔记共享的 tag 命中，
    IDF 折扣后 `prompt_tag_hit` 打了约 0.73 折，加上一次 summary 命中，
    topical 落在 `[min_topical_score, fulltext_topical_threshold)` 区间内
    （4.916，< 6）——此时零主题词就不会被全文提升；叠加一次主题词命中
    （+`session_topic_hit`=2）后变成 6.916，跨过阈值。
    """
    from scripts._decision import StateView, decide_injection

    def _entry(path: str, tags: tuple[str, ...], summary: str = "不相关") -> Entry:
        return Entry(path=path, tags=tags, summary=summary, keywords=(), mtime=0)

    # target 与 4 篇 distractor 共享同一个泛 tag（df=5），另加 5 篇无关 filler
    # 凑齐 n_docs=10，使 tag-IDF 因子约为 0.73（math.log 折扣，非整数，故意不取整）。
    entries = {"target.md": _entry("target.md", ("热门标签",), "不相关摘要含次要关键词")}
    for i in range(1, 5):
        entries[f"distractor{i}.md"] = _entry(f"distractor{i}.md", ("热门标签",))
    for i in range(1, 6):
        entries[f"filler{i}.md"] = _entry(f"filler{i}.md", (f"filler-tag-{i}",))
    assert len(entries) == 10

    kws = {"热门标签", "次要关键词"}
    rel = DEFAULT_CONFIG["relevance"]

    without_topic = decide_injection(
        entries, Signals(prompt_keywords=kws), W, DEFAULT_CONFIG, StateView())
    target_topical = next(ed.topical for ed in without_topic.admitted if ed.path == "target.md")
    assert rel["min_topical_score"] <= target_topical < rel["fulltext_topical_threshold"], (
        f"前提场景不成立：target.md 的 topical={target_topical} 不在闸门区间内，"
        f"场景需重新构造")
    assert without_topic.fulltext_path is None, "前提场景不成立：零主题词时不该有全文候选"

    with_topic = decide_injection(
        entries, Signals(prompt_keywords=kws, session_topic_words={"热门标签"}),
        W, DEFAULT_CONFIG, StateView())
    assert with_topic.fulltext_path == "target.md", (
        "会话主题词应能把 target.md 推过全文阈值——若本断言失败，"
        "说明 select_fulltext 判据已变，需同步更新本测试与 Ruling 16 的文档化处置")
    assert with_topic.fulltext_arm.startswith("topical>=")


def test_gold_recall_not_degraded_with_topic_signal() -> None:
    """承重守卫：主题信号不得打穿 gold recall（评审 R-C1 的教训）。"""
    from scripts._decision import StateView, decide_injection
    from scripts._scorer import is_archived
    from scripts._signal_collect import collect_signal_j_prompt_keywords
    from tests.fixtures.gold_corpus import build_gold_corpus

    corpus, queries = build_gold_corpus()
    rel = DEFAULT_CONFIG["relevance"]
    exclude = {t.lower() for t in rel.get("exclude_note_tags", [])}
    active = {e.path: e for e in corpus if not is_archived(e, exclude)}
    total, zero = 0.0, 0
    for q in queries:
        kws = collect_signal_j_prompt_keywords(q.prompt, max_keywords=30)
        # 拿该 query 的前两个词冒充"会话主题"，模拟真实的主题信号
        topic = set(sorted(kws)[:2])
        d = decide_injection(active, Signals(prompt_keywords=kws,
                                             session_topic_words=topic),
                             W, DEFAULT_CONFIG, StateView())
        paths = {ed.path for ed in d.admitted}
        hit = sum(1 for p in q.relevant if p in paths)
        if hit == 0:
            zero += 1
        total += hit / len(q.relevant)
    recall = total / len(queries)
    assert recall >= 0.95, f"主题信号打穿 gold recall：{recall:.4f}"
    assert zero == 0, f"{zero} 条 query 的相关笔记被全丢"
