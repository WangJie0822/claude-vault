"""B5：扩展词召回的 precision/recall 对照——召回升、噪音不升。"""
from __future__ import annotations

from scripts._frontmatter_reader import Entry
from scripts._scorer import Signals, topical_score


def _w():
    return {
        "prompt_tag_hit": 4, "prompt_summary_hit": 2, "prompt_keyword_hit": 3,
    }


def test_recall_paraphrase_hits_via_keyword():
    # 用户说"召回"，目标笔记 tag/summary 都没有"召回"，但 keywords 有 → 命中
    target = Entry(path="t.md", tags=("scoring",), summary="打分权重设计",
                   keywords=("召回", "recall"))
    sigs = Signals(prompt_keywords={"召回"})
    assert topical_score(target, sigs, _w()) == 3  # keyword-only 命中，可进候选


def test_precision_noise_note_not_surfaced_by_unrelated_keyword():
    # 噪音笔记的 keywords 与本次 prompt 无任何交集 → 0 分，不被顶上来
    noise = Entry(path="n.md", tags=("ios",), summary="完全无关",
                  keywords=("swift", "xcode"))
    sigs = Signals(prompt_keywords={"召回", "扩展词"})
    assert topical_score(noise, sigs, _w()) == 0


def test_single_char_keyword_filtered_at_read(write_frontmatter_cache, tmp_vault):
    from scripts._frontmatter_reader import load_cache
    write_frontmatter_cache({"b.md": {"keywords": ["回", "回归测试"]}})
    assert load_cache(tmp_vault)["b.md"].keywords == ("回归测试",)
