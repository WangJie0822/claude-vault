# -*- coding: utf-8 -*-
"""tag-IDF 加权：泛 tag 降权、singleton tag 保持满分、开关可回退。"""
from scripts._frontmatter_reader import Entry
from scripts._scorer import (Signals, topical_score, build_tag_df, tag_idf_factor)

W = {"prompt_tag_hit": 4, "prompt_summary_hit": 2, "prompt_keyword_hit": 5}


def _corpus():
    entries = {}
    # 泛 tag：100 篇都打 broad
    for i in range(100):
        entries[f"b{i}.md"] = Entry(path=f"b{i}.md", tags=("broad",), summary="无关内容")
    # singleton tag：1 篇
    entries["rare.md"] = Entry(path="rare.md", tags=("rare",), summary="无关内容")
    return entries


def test_build_tag_df_counts_documents():
    df = build_tag_df(_corpus())
    assert df["broad"] == 100
    assert df["rare"] == 1


def test_singleton_tag_gets_full_weight():
    df = build_tag_df(_corpus())
    f = tag_idf_factor("rare", df, n_docs=101, floor=0.5)
    assert f > 0.95, f"singleton tag 应接近满权重，实际 {f}"


def test_broad_tag_is_downweighted_but_not_zero():
    df = build_tag_df(_corpus())
    f = tag_idf_factor("broad", df, n_docs=101, floor=0.5)
    assert 0.5 <= f < 0.7, f"泛 tag 应降权但保底 floor，实际 {f}"


def test_precise_tag_outranks_broad_tag():
    df = build_tag_df(_corpus())
    sig = Signals(prompt_keywords={"broad", "rare"})
    broad_e = Entry(path="b0.md", tags=("broad",), summary="无关内容")
    rare_e = Entry(path="rare.md", tags=("rare",), summary="无关内容")
    s_broad = topical_score(broad_e, sig, W, tag_df=df, n_docs=101)
    s_rare = topical_score(rare_e, sig, W, tag_df=df, n_docs=101)
    assert s_rare > s_broad, f"精确 tag({s_rare}) 应高于泛 tag({s_broad})"


def test_keywords_hit_outranks_broad_tag_hit():
    """§4.2 的核心病灶：精确 keywords 必须能打过泛 tag。"""
    df = build_tag_df(_corpus())
    sig = Signals(prompt_keywords={"broad", "mangle"})
    broad_e = Entry(path="b0.md", tags=("broad",), summary="无关内容")
    kw_e = Entry(path="k.md", tags=("other",), summary="无关内容", keywords=("mangle",))
    s_broad = topical_score(broad_e, sig, W, tag_df=df, n_docs=101)
    s_kw = topical_score(kw_e, sig, W, tag_df=df, n_docs=101)
    assert s_kw > s_broad, f"keywords 命中({s_kw}) 必须高于泛 tag 命中({s_broad})"


def test_tag_df_none_preserves_legacy_behavior():
    """止血开关：不传 tag_df 时逐字节回到旧行为（tag 命中拿满分）。"""
    sig = Signals(prompt_keywords={"broad"})
    e = Entry(path="b0.md", tags=("broad",), summary="无关内容")
    assert topical_score(e, sig, W) == W["prompt_tag_hit"]


def test_orphan_df2_tag_drops_below_gate():
    """df=2 孤 tag 命中（无 summary/keyword）经 tag-IDF 降权后须 <min_topical(4)，
    被候选闸门剔除——这是 tag-IDF 收窄召回集的边界，最易被 floor/公式改动静默破坏。
    legacy 路径（tag_df=None）则保持在 4.0（不剔除）。"""
    df = {"pair": 2}
    e = Entry(path="x.md", tags=("pair",), summary="无关内容")
    sig = Signals(prompt_keywords={"pair"})
    t = topical_score(e, sig, W, tag_df=df, n_docs=100, tag_idf_floor=0.5)
    assert t < 4, f"df=2 孤 tag 应被 tag-IDF 压到 <min_topical，实际 {t}"   # 实测 3.70
    assert topical_score(e, sig, W) == 4, "legacy 路径（无 tag-IDF）孤 tag 应保持 4.0 不剔除"
