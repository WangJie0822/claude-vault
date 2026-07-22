"""prompt_submit_load 单元测试（Task 4：keyword-only 进候选 + _hit_keywords 口径扩展）。"""
from __future__ import annotations


def test_hit_keywords_includes_keyword_matches():
    from scripts._frontmatter_reader import Entry
    from scripts.prompt_submit_load import _hit_keywords
    e = Entry(path="x.md", tags=("android",), keywords=("回归测试",))
    hits = _hit_keywords(e, {"android", "回归测试", "无关词"})
    assert "android" in hits and "回归测试" in hits and "无关词" not in hits


def test_keyword_only_entry_enters_candidates_not_fulltext(tmp_vault, write_frontmatter_cache):
    # keyword-only 命中：Task 8 起 prompt_keyword_hit 3→5，单靠 keyword 命中
    # （topical=5）已 ≥ min_topical(4)，无需再靠"keyword override"分支进候选——
    # 这正是 Task 8 的设计意图（精确 keywords 信号足够强，能独立通过精度闸门）。
    # 但仍应低于 fulltext_topical_threshold(6)，不触发全文。
    from scripts import prompt_submit_load as P
    from scripts._frontmatter_reader import load_cache
    from scripts._scorer import Signals, topical_score
    from scripts._config_loader import load_config
    write_frontmatter_cache({
        "kw.md": {"tags": [], "summary": "无关摘要", "keywords": ["扩展词召回"]},
    })
    cfg = load_config()
    entries = load_cache(tmp_vault)
    sigs = Signals(prompt_keywords={"扩展词召回"})
    e = entries["kw.md"]
    t = topical_score(e, sigs, cfg["scoring"])
    assert t == 5  # keyword-only，Task 8 新权重
    min_topical = cfg["relevance"]["min_topical_score"]
    ft = cfg["relevance"]["fulltext_topical_threshold"]
    has_kw = bool(e.keywords) and any(
        P._keyword_hits_keywords(kw, e) for kw in sigs.prompt_keywords)
    assert has_kw                              # 靠 keywords 字段命中（非 tag/summary）
    assert t >= min_topical                    # 现在单凭 topical 本身即可过闸门
    assert t < ft                               # 不达全文阈值
