"""prompt_submit_load 单元测试（Task 4：keyword-only 进候选 + _hit_keywords 口径扩展）。"""
from __future__ import annotations


def test_hit_keywords_includes_keyword_matches():
    from scripts._frontmatter_reader import Entry
    from scripts.prompt_submit_load import _hit_keywords
    e = Entry(path="x.md", tags=("android",), keywords=("回归测试",))
    hits = _hit_keywords(e, {"android", "回归测试", "无关词"})
    assert "android" in hits and "回归测试" in hits and "无关词" not in hits


def test_keyword_only_entry_enters_candidates_not_fulltext(tmp_vault, write_frontmatter_cache):
    # keyword-only 命中（topical=3 < min_topical 4）应进候选清单，但不触发全文
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
    assert t == 3  # keyword-only
    min_topical = cfg["relevance"]["min_topical_score"]
    ft = cfg["relevance"]["fulltext_topical_threshold"]
    has_kw = bool(e.keywords) and any(
        P._keyword_hits_keywords(kw, e) for kw in sigs.prompt_keywords)
    assert (t < min_topical and has_kw)        # 靠 keyword override 进候选
    assert t < ft                              # 不达全文阈值
